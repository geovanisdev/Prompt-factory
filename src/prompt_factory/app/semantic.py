"""Busca por SENTIDO (M10): cosseno contra a matriz inteira, filtro exato.

A busca textual do M9 responde "quais prompts contêm esta palavra". Esta responde
"quais prompts falam desta coisa" — e é a diferença entre achar *pedir aumento* e
achar *negociação salarial* num corpus onde ninguém escreve com o vocabulário de
quem procura.

TRÊS DECISÕES, TODAS MEDIDAS
============================

1. **f32 residente na RAM, e não o f16 do disco.** O ``.npy`` é f16 porque assim
   cabe (106 MiB em vez de 212); a CONTA, porém, é sempre f32 — numpy não tem
   BLAS para f16. Medido no corpus real desta máquina, 144.754 x 384:

   ======================================  ==========  =========
   estratégia                               consulta      RAM
   ======================================  ==========  =========
   f16 no memmap, convertido em blocos      199,3 ms    106 MiB
   **f32 convertido uma vez, residente**    **5,8 ms**  **212 MiB**
   ======================================  ==========  =========

   **34x**, e o gargalo do f16 não é a multiplicação: é converter 55,6 milhões de
   elementos a cada consulta. Os dois resultados diferem em 6e-08 — nada. Os
   212 MiB são ruído num processo que já carrega torch (~1 GB).

2. **Por causa do item 1, a filtragem é EXATA.** A 5,8 ms dá para pontuar o corpus
   inteiro, apagar o que o SQL não deixou passar e tirar o top-k verdadeiro. A
   alternativa comum (pegar os 5.000 melhores e filtrar depois) devolveria, num
   recorte estreito, um top-k que *parece* certo e não é. Numa ferramenta de
   pesquisa linguística, aproximação silenciosa é pior que lentidão.

3. **Carregamento preguiçoso, e não é uma economia de 3 s.** Medido: importar
   ``sentence_transformers`` + torch custa **10,6 s** nesta máquina e carregar o
   e5 mais **4,7 s** — a primeira consulta paga ~16 s, contra 87 ms das
   seguintes. Aquecer no lifespan colocaria esses 16 s no ``pf serve`` de TODO
   mundo, inclusive de quem só vai usar a busca textual. Por isso a interface
   dispara ``POST /api/semantic/warmup`` no clique que liga o modo e mostra o
   aviso, em vez de deixar a tela parada sem explicação.

   Orçamento de uma consulta quente, medido no corpus real:
   SQL dos ids **63 ms** (sem filtro) ou 20 ms (com ``lang=pt``) + embedding da
   consulta **10 ms** + produto e top-k **7,8 ms** + hidratação 0,1 ms = **~87 ms**.

A INVARIANTE POSICIONAL É VIDA OU MORTE AQUI
============================================
``emb/universe.f16.npy`` está alinhado **por posição** a
``emb/universe_uids.txt``, que por sua vez está alinhado a ``final/universe.parquet``,
que é o que o ``pf load-db`` carrega no SQLite. A linha ``i`` da matriz é o vetor
do uid da linha ``i`` do txt — e é só isso que amarra um vetor a uma linha do
banco. **Nunca assuma ``id == linha + 1``**: a ordem de carga do s11 não é
contrato, e um banco recarregado a partir de outro universo produziria vizinhos
plausíveis e errados, que é o defeito mais caro que um corpus pode ter (ninguém
percebe).

Por isso há duas guardas, e as duas **falham alto**:

* **na subida** (``sondar``): contagem do ``.npy`` contra a do banco + uma amostra
  de uids conferida no SQL. Custa milissegundos e aparece no terminal e no
  ``/api/semantic/status``. Não derruba a app: o FTS continua funcionando e
  recusar a interface inteira por causa do índice semântico seria exagero.
* **na carga** (``carregar``): o casamento uid → id de TODAS as linhas, nos dois
  sentidos. Abaixo de ``[app] semantic_min_alinhamento`` levanta
  ``DesalinhamentoEmbeddings`` e a rota devolve 500 explicando o conserto. O que
  não acontece nunca é responder com resultado.

**Todo vetor sai de ``embedder.embed_texts``**, nunca de um ``SentenceTransformer``
chamado direto: o prefixo ``query: `` é parte do espaço vetorial, e sem ele os
cossenos continuam saindo — plausíveis e errados. Os dois lados (o s05 que gerou o
``.npy`` e a consulta daqui) usam ``query: ``; é assimétrico em relação à receita
canônica do e5 (``query:``/``passage:``), mas **autoconsistente**, que é o que
importa. Se um dia o s05 passar a usar ``passage: ``, esta função muda junto.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .. import db as dbmod
from .. import embedder
from ..config import get as _cfg
from ..stages import UNIVERSE_EMB, UNIVERSE_UIDS


class SemanticaIndisponivel(RuntimeError):
    """Não há ``.npy`` (ou ``.txt``) para carregar. A rota devolve **503**.

    É o estado de um clone limpo que ainda não rodou o s05/s06 — falta de
    insumo, não defeito. O FTS continua funcionando.
    """


class DesalinhamentoEmbeddings(RuntimeError):
    """O ``.npy`` não descreve as linhas deste banco. A rota devolve **500**.

    Separada da anterior de propósito: "ainda não gerei" e "gerei de outro
    universo" pedem conserto diferente, e a segunda é a que devolveria resultado
    plausível e errado se alguém a tratasse como indisponibilidade temporária.
    """


def precisao_configurada() -> str:
    valor = str(_cfg("app", "semantic_precision", default="f32")).lower()
    if valor not in ("f32", "f16"):
        raise ValueError(
            f"[app] semantic_precision={valor!r} — só 'f32' ou 'f16' existem"
        )
    return valor


def k_padrao() -> int:
    return int(_cfg("app", "semantic_k", default=50))


def k_maximo() -> int:
    return int(_cfg("app", "semantic_max_k", default=200))


def min_chars() -> int:
    return int(_cfg("app", "semantic_min_chars", default=3))


def _min_alinhamento() -> float:
    return float(_cfg("app", "semantic_min_alinhamento", default=0.99))


#: Quantos uids a sonda de partida confere no banco. 64 é barato (um ``IN`` de 64
#: parâmetros num índice único) e já é suficiente: o modo de falha real não é
#: "alguns uids mudaram", é "o arquivo é de outra build" — nesse caso os 64 erram.
AMOSTRA_SONDA = 64


class IndiceSemantico:
    """A matriz de embeddings do universo, alinhada aos ids do SQLite.

    Uma instância por app (mora em ``app.state.semantica``), como o cache de
    agregados e pelo mesmo motivo: dois ``criar_app`` no mesmo processo apontam
    para bancos diferentes, e uma matriz compartilhada entre eles responderia com
    os vetores de um banco na tela do outro.

    Em produção só existe uma app por processo — e é por isso que ``pf serve``
    roda com **um worker**: N workers seriam N cópias de 212 MiB e N modelos e5,
    para servir um usuário numa máquina só.
    """

    def __init__(
        self,
        db_file: str | Path,
        emb_file: str | Path | None = None,
        uids_file: str | Path | None = None,
        *,
        precisao: str | None = None,
    ) -> None:
        from .. import paths

        self.db_file = Path(db_file)
        raiz = paths.DATA
        self.emb_file = Path(emb_file) if emb_file is not None else raiz / UNIVERSE_EMB
        self.uids_file = (
            Path(uids_file) if uids_file is not None else raiz / UNIVERSE_UIDS
        )
        self.precisao = precisao or precisao_configurada()

        self._trava = threading.Lock()
        self._matriz: Any = None  # np.ndarray (f32 residente) ou np.memmap (f16)
        self._id_da_linha: Any = None  # np.ndarray int64, id do banco por linha
        self._linha_do_id: Any = None  # np.ndarray int32, linha por id (-1 = sem vetor)
        self._n_banco = 0
        self._casados = 0
        self._build_id: str | None = None
        self._carregado_em: float | None = None
        self._custo_carga_ms: float | None = None
        self._aquecendo = False
        #: O e5 já foi exercitado POR ESTE ÍNDICE. Rastreado aqui em vez de
        #: perguntar ao ``embedder`` porque é o estado que interessa à interface
        #: ("a próxima consulta vai ser rápida?") e porque um teste que substitui
        #: o modelo por uma função falsa continua descrevendo a verdade.
        self._modelo_pronto = False
        self._erro: str | None = None
        self._sonda: dict[str, Any] | None = None

    # -- estado ------------------------------------------------------------

    @property
    def carregado(self) -> bool:
        """A MATRIZ está na RAM (417 ms). Não diz nada sobre o modelo."""
        return self._matriz is not None

    @property
    def aquecido(self) -> bool:
        """A próxima consulta é rápida — matriz **e** modelo prontos.

        **As duas coisas, e isto foi um bug de verdade.** A matriz carrega em
        417 ms e o e5 leva ~16 s; com ``aquecido`` respondendo só pela matriz, a
        interface anunciava "modelo pronto" meio segundo depois do clique e
        mandava a busca — que então travava 16 s com a tela dizendo que estava
        tudo certo. Pego no navegador, contra o corpus real.
        """
        return self._matriz is not None and self._modelo_pronto

    def disponivel(self) -> bool:
        """Os dois arquivos existem? (não abre nem lê nada)"""
        return self.emb_file.is_file() and self.uids_file.is_file()

    def _abrir(self) -> sqlite3.Connection:
        """Conexão PRÓPRIA, e não a do request.

        ``carregar`` roda tanto dentro de uma requisição quanto numa thread de
        aquecimento, e a conexão do request morre com ele. Abrir custa ~0,1 ms e
        acontece uma vez por carga.
        """
        return dbmod.connect(self.db_file, check_same_thread=False)

    # -- sonda de partida --------------------------------------------------

    def sondar(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Conferência BARATA do alinhamento, para o lifespan.

        Lê só o cabeçalho do ``.npy`` (o ``mmap_mode`` não toca nos dados), conta
        as linhas do txt e confere ``AMOSTRA_SONDA`` uids no banco. Devolve um
        dicionário com ``ok`` e ``motivo``; **não levanta**: um índice semântico
        torto não pode impedir a interface de abrir, só de responder.
        """
        import numpy as np

        sonda: dict[str, Any] = {
            "ok": False,
            "motivo": None,
            "n_vetores": 0,
            "n_uids": 0,
            "dim": 0,
            "n_banco": 0,
            "amostra": 0,
            "amostra_casada": 0,
        }
        if not self.disponivel():
            sonda["motivo"] = (
                f"{self.emb_file.name} / {self.uids_file.name} ausentes — "
                "rode `pf run s05` e `pf run s06`"
            )
            self._sonda = sonda
            return sonda
        try:
            forma = np.load(self.emb_file, mmap_mode="r")
            sonda["n_vetores"], sonda["dim"] = int(forma.shape[0]), int(forma.shape[1])
            del forma
            uids = self.uids_file.read_text(encoding="utf-8").split("\n")
            if uids and not uids[-1]:
                uids.pop()
            sonda["n_uids"] = len(uids)
            sonda["n_banco"] = int(
                conn.execute("SELECT count(*) AS n FROM prompts").fetchone()["n"]
            )
        except (OSError, ValueError) as exc:
            sonda["motivo"] = f"não deu para ler os embeddings: {exc}"
            self._sonda = sonda
            return sonda

        if sonda["n_vetores"] != sonda["n_uids"]:
            sonda["motivo"] = (
                f"{self.emb_file.name} tem {sonda['n_vetores']} vetores e "
                f"{self.uids_file.name} tem {sonda['n_uids']} uids"
            )
            self._sonda = sonda
            return sonda

        passo = max(1, len(uids) // AMOSTRA_SONDA)
        amostra = uids[::passo][:AMOSTRA_SONDA]
        sonda["amostra"] = len(amostra)
        if amostra:
            marcas = ", ".join("?" * len(amostra))
            sonda["amostra_casada"] = int(
                conn.execute(
                    f"SELECT count(*) AS n FROM prompts WHERE uid IN ({marcas})",
                    amostra,
                ).fetchone()["n"]
            )
        if sonda["amostra"] and sonda["amostra_casada"] < sonda["amostra"]:
            sonda["motivo"] = (
                f"{sonda['amostra_casada']} de {sonda['amostra']} uids da amostra "
                "não existem neste banco — o .npy é de OUTRO universo"
            )
        elif sonda["n_banco"] != sonda["n_vetores"]:
            sonda["motivo"] = (
                f"o banco tem {sonda['n_banco']} linhas e o .npy {sonda['n_vetores']} "
                "vetores — um dos dois é de outra build"
            )
        else:
            sonda["ok"] = True
        self._sonda = sonda
        return sonda

    # -- carga -------------------------------------------------------------

    def carregar(self) -> None:
        """Carrega a matriz e o casamento uid → id. Idempotente e thread-safe.

        É o passo caro (~1 s para converter 111 MB de f16 em 212 MiB de f32 mais
        ~110 ms para o mapa de uids), e por isso acontece **uma vez**.
        """
        if self._matriz is not None:
            return
        with self._trava:
            if self._matriz is not None:
                return
            inicio = time.perf_counter()
            try:
                self._carregar_sem_trava()
            except Exception as exc:
                self._erro = str(exc)
                raise
            self._erro = None
            self._custo_carga_ms = (time.perf_counter() - inicio) * 1000
            self._carregado_em = time.time()

    def _carregar_sem_trava(self) -> None:
        import numpy as np

        if not self.disponivel():
            raise SemanticaIndisponivel(
                f"embeddings do universo não encontrados ({self.emb_file}). "
                "A busca por sentido precisa do par gerado pelo s06: rode "
                "`pf run s05` e `pf run s06`. A busca textual não depende disto."
            )

        uids = self.uids_file.read_text(encoding="utf-8").split("\n")
        if uids and not uids[-1]:
            uids.pop()
        bruto = np.load(self.emb_file, mmap_mode="r")
        if bruto.shape[0] != len(uids):
            raise DesalinhamentoEmbeddings(
                f"{self.emb_file.name} tem {bruto.shape[0]} vetores e "
                f"{self.uids_file.name} tem {len(uids)} uids. Os dois nascem juntos "
                "no s06 — rode `pf run s06` de novo."
            )
        if bruto.shape[0] == 0:
            raise DesalinhamentoEmbeddings(
                f"{self.emb_file.name} está vazio — rode `pf run s05` e `pf run s06`."
            )

        conn = self._abrir()
        try:
            self._build_id = self._ler_build(conn)
            cur = conn.cursor()
            # row_factory=None nesta consulta e só nesta: `sqlite3.Row` custa a
            # construção de um objeto por linha e aqui são 144.754. Medido no
            # banco real, o mesmo SELECT: 117 ms com Row, 109 ms com tuplas.
            cur.row_factory = None
            mapa: dict[str, int] = {
                str(uid): int(ident)
                for ident, uid in cur.execute("SELECT id, uid FROM prompts")
            }
            self._n_banco = len(mapa)
        finally:
            conn.close()

        id_da_linha = np.fromiter(
            (mapa.get(u, -1) for u in uids), dtype=np.int64, count=len(uids)
        )
        casados = int(np.count_nonzero(id_da_linha >= 0))
        self._casados = casados
        minimo = _min_alinhamento()

        # DIREÇÃO 1: vetor apontando para linha que não existe. É o caso perigoso
        # — devolveria um uid fantasma, ou pior, silenciaria o item certo.
        if casados < minimo * len(uids):
            fora = [u for u, i in zip(uids, id_da_linha.tolist(), strict=True) if i < 0]
            raise DesalinhamentoEmbeddings(
                f"só {casados} de {len(uids)} uids de {self.uids_file.name} existem "
                f"neste banco ({casados / len(uids):.1%} < "
                f"{minimo:.0%} de [app] semantic_min_alinhamento). "
                f"Exemplos que não existem: {', '.join(fora[:5])}. "
                "O .npy é de outra build do universo: recarregue o banco com "
                "`pf load-db` a partir do MESMO final/universe.parquet que gerou "
                "os embeddings, ou refaça o s05/s06."
            )
        # DIREÇÃO 2: linha do banco sem vetor nenhum. Não devolve nada errado, mas
        # some com parte do corpus da busca — e sumir em silêncio é o que esta
        # guarda inteira existe para impedir.
        if self._n_banco and casados < minimo * self._n_banco:
            raise DesalinhamentoEmbeddings(
                f"o banco tem {self._n_banco} linhas e só {casados} delas têm vetor "
                f"({casados / self._n_banco:.1%} < {minimo:.0%}). A busca por "
                "sentido enxergaria uma fração do corpus sem dizer nada — refaça o "
                "s05/s06 sobre o universo que está carregado."
            )

        maior = int(id_da_linha.max())
        linha_do_id = np.full(maior + 1, -1, dtype=np.int32)
        validas = np.flatnonzero(id_da_linha >= 0)
        linha_do_id[id_da_linha[validas]] = validas.astype(np.int32)

        if self.precisao == "f32":
            # A conversão inteira, de uma vez. `np.asarray` sobre o memmap lê os
            # 111 MB e devolve 212 MiB de f32 contíguo — é ele que o BLAS come.
            matriz = np.asarray(bruto, dtype=np.float32)
            # O round-trip por f16 tira as normas de 1 na terceira casa. O cosseno
            # é produto interno SÓ quando os dois lados são unitários; sem isto os
            # escores ficam levemente ordenados por norma, não por sentido.
            normas = np.linalg.norm(matriz, axis=1, keepdims=True)
            np.maximum(normas, 1e-12, out=normas)
            matriz /= normas
        else:
            matriz = bruto  # memmap f16: a conversão sai por bloco, na consulta

        self._matriz = matriz
        self._id_da_linha = id_da_linha
        self._linha_do_id = linha_do_id

    def _ler_build(self, conn: sqlite3.Connection) -> str | None:
        linha = conn.execute(
            "SELECT value FROM app_meta WHERE key = 'db_build_id'"
        ).fetchone()
        return None if linha is None else str(linha["value"])

    def conferir_build(self, conn: sqlite3.Connection) -> None:
        """Descarta a matriz se o arquivo do banco foi trocado embaixo da app.

        Mesma guarda do ``app/cache.py`` e pelo mesmo motivo, com a aposta
        invertida: lá um número velho seria só errado; aqui o mapa uid → id
        inteiro passa a apontar para as linhas de OUTRO banco. Custa uma busca por
        chave primária numa tabela de 6 linhas.
        """
        if self._matriz is None:
            return
        atual = self._ler_build(conn)
        if atual == self._build_id:
            return
        with self._trava:
            self._matriz = None
            self._id_da_linha = None
            self._linha_do_id = None
            self._build_id = atual

    def aquecer(self) -> None:
        """Carrega a matriz **e** o modelo e5. É o que a primeira consulta paga."""
        self._aquecendo = True
        try:
            self.carregar()
            self.vetor_da_consulta("aquecimento do índice semântico")
        finally:
            self._aquecendo = False

    def aquecer_em_thread(self) -> bool:
        """Dispara ``aquecer`` numa thread daemon. ``False`` se já está a caminho.

        A interface chama isto no clique que liga o modo "sentido", e depois
        pergunta o ``status`` — assim a tela diz "carregando o modelo" em vez de
        parecer travada por 16 s na primeira busca.
        """
        if self._aquecendo or self.aquecido:
            return False
        self._aquecendo = True

        def _rodar() -> None:
            try:
                self.aquecer()
            except Exception as exc:  # a thread NUNCA pode derrubar a app
                self._erro = str(exc)
            finally:
                self._aquecendo = False

        threading.Thread(target=_rodar, name="pf-semantica-aquece", daemon=True).start()
        return True

    # -- consulta ----------------------------------------------------------

    def vetor_da_consulta(self, texto: str) -> Any:
        """O texto do usuário no MESMO espaço vetorial do ``.npy``.

        Passa por ``embedder.embed_texts`` e por mais nada: é ele que aplica o
        prefixo ``query: ``, a normalização L2 e o corte de caracteres. Chamar o
        ``SentenceTransformer`` direto daqui produziria vetores de outro espaço e
        cossenos que continuam saindo — errados e em silêncio.
        """
        vetor = embedder.embed_texts([texto])[0]
        self._modelo_pronto = True
        return vetor

    def pontuar(
        self, vetor: Any, ids_permitidos: Any, k: int
    ) -> tuple[list[int], list[float], int]:
        """``(ids, escores, n_candidatos)`` — o top-k EXATO dentro do filtro.

        ``ids_permitidos`` é o conjunto que o SQL deixou passar. Ids sem vetor
        (linha do banco que não está no ``.npy``) somem daqui e são contados fora,
        para a resposta poder dizer quantos ficaram de fora em vez de fingir que
        o filtro casava menos.

        O produto é feito contra a matriz INTEIRA e a máscara é aplicada depois.
        Parece desperdício e não é: a 5,8 ms de sgemv, mascarar 144 mil floats
        (0,9 ms) sai mais barato do que juntar as linhas permitidas num buffer
        novo — e, principalmente, o custo deixa de depender do filtro, então não
        há tentação de aproximar quando o recorte é grande.
        """
        import numpy as np

        if self._matriz is None:  # pragma: no cover - defensivo
            raise SemanticaIndisponivel("índice semântico não carregado")

        ids = np.asarray(ids_permitidos, dtype=np.int64)
        dentro = ids[(ids >= 0) & (ids < self._linha_do_id.size)]
        linhas = self._linha_do_id[dentro]
        linhas = linhas[linhas >= 0]
        if linhas.size == 0 or k <= 0:
            return [], [], 0

        escores = self._produto(vetor)
        mascara = np.zeros(escores.shape[0], dtype=bool)
        mascara[linhas] = True
        escores = np.where(mascara, escores, -np.inf)

        k_ef = min(int(k), int(linhas.size))
        # `argpartition` é O(n) e não ordena nada além do necessário — só depois
        # os k escolhidos vão para um argsort de k elementos. Todo este bloco
        # (produto + máscara + top-k) mede 7,8 ms sobre os 144.754 do corpus real.
        parciais = np.argpartition(-escores, k_ef - 1)[:k_ef]
        ordem = parciais[np.argsort(-escores[parciais], kind="stable")]
        return (
            [int(self._id_da_linha[i]) for i in ordem],
            [float(escores[i]) for i in ordem],
            int(linhas.size),
        )

    def _produto(self, vetor: Any) -> Any:
        """Cosseno de todo o corpus contra a consulta (os dois lados unitários)."""
        import numpy as np

        v = np.asarray(vetor, dtype=np.float32)
        if self.precisao == "f32":
            return self._matriz @ v
        # f16: a matriz continua no disco e a conversão sai por bloco. É o modo
        # "não tenho 212 MiB de RAM"; medido, custa 34x mais por consulta
        # (199,3 ms contra 5,8 ms) para economizar 106 MiB.
        n = self._matriz.shape[0]
        saida = np.empty(n, dtype=np.float32)
        passo = 32_768
        for i in range(0, n, passo):
            bloco = np.asarray(self._matriz[i : i + passo], dtype=np.float32)
            normas = np.linalg.norm(bloco, axis=1)
            np.maximum(normas, 1e-12, out=normas)
            saida[i : i + passo] = (bloco @ v) / normas
        return saida

    # -- diagnóstico -------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """O que a interface precisa saber antes de deixar buscar."""
        mb = None
        if self._matriz is not None and self.precisao == "f32":
            mb = round(self._matriz.nbytes / (1024 * 1024), 1)
        return {
            "disponivel": self.disponivel(),
            # `aquecido` é a matriz E o modelo — é o que a interface espera para
            # dizer "pronto". `matriz_carregada` sozinha aparece separada porque
            # ela fica pronta 40x antes e confundir as duas foi um bug real.
            "aquecido": self.aquecido,
            "matriz_carregada": self.carregado,
            "aquecendo": self._aquecendo,
            "modelo_carregado": embedder.modelo_carregado(),
            "modelo_em_cache": embedder.modelo_em_cache(),
            "modelo": embedder.model_name(),
            "precisao": self.precisao,
            "n_vetores": 0 if self._matriz is None else int(self._matriz.shape[0]),
            "dim": 0 if self._matriz is None else int(self._matriz.shape[1]),
            "n_banco": self._n_banco,
            "casados": self._casados,
            "mb_residentes": mb,
            "carga_ms": None
            if self._custo_carga_ms is None
            else round(self._custo_carga_ms, 1),
            "k_padrao": k_padrao(),
            "k_max": k_maximo(),
            "min_chars": min_chars(),
            "erro": self._erro,
            "sonda": self._sonda,
            "arquivo": str(self.emb_file),
        }


__all__ = [
    "AMOSTRA_SONDA",
    "DesalinhamentoEmbeddings",
    "IndiceSemantico",
    "SemanticaIndisponivel",
    "k_maximo",
    "k_padrao",
    "min_chars",
    "precisao_configurada",
]
