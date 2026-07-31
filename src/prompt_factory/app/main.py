"""``criar_app()`` — a app FastAPI da interface local (M9).

**Fábrica, não ``app`` global.** Um ``app = FastAPI()`` de módulo leria
``paths.DB_FILE`` no import e tornaria impossível apontar os testes (ou um
segundo banco) para outro arquivo. ``criar_app(db_file=...)`` guarda o caminho
em ``app.state`` e ``deps.get_conn`` abre a conexão de lá.

O que o lifespan faz, e por quê:

1. **Recusa subir sem banco.** Um servidor que sobe e devolve 500 em toda rota
   é pior que um que não sobe: a mensagem útil (``pf load-db``) fica enterrada
   num traceback por request em vez de aparecer uma vez no terminal.
2. **Cria o índice de cobertura da interface** (idempotente, ~0,6 s em 200k
   linhas). Ele não está no DDL do ``db.py`` de propósito: é um índice de
   *consulta da app*, e a carga bulk do s11 derruba e recria só os índices que
   ela conhece. Sem ele, contar 12 facetas custa 1.204 ms; com ele, 166 ms.
3. **Confere o índice FTS** com ``integrity-check`` **e o argumento 1** — sem o
   argumento, um índice completamente vazio PASSA (ele só valida coerência
   interna). ``SELECT count(*) FROM prompts_fts`` também não detecta: numa
   tabela de conteúdo externo o count lê a tabela de conteúdo.
4. **Sonda o índice semântico** (M10) sem carregá-lo: contagem do ``.npy`` contra
   a do banco e 64 uids conferidos no SQL. Avisa alto e segue — a busca textual
   não depende dos embeddings, e um ``.npy`` de outra build precisa impedir a
   BUSCA, não a interface. Ver ``app/semantic.py``.

**CORS não é configurado.** A interface é servida pela mesma origem; abrir CORS
seria superfície de ataque de graça numa app que roda em ``127.0.0.1`` com o
corpus inteiro atrás.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .. import __version__, paths
from .. import db as dbmod
from ..schema import TAXONOMY_VERSION
from ..stages import UNIVERSE_EMB, UNIVERSE_UIDS
from . import queries
from .cache import CacheAgregados
from .deps import Conexao, Estado, rotulagem_pendente
from .semantic import IndiceSemantico

#: Índice de COBERTURA da interface. A ordem é a ordem em que a app filtra
#: (igualdade mais usada primeiro); ``n_chars`` e ``n_exact_dups`` entram no fim
#: para que a faceta de tamanho e o filtro anti-robô (``max_dups``) também sejam
#: respondidos sem tocar na tabela — e tocar na tabela, aqui, significa carregar
#: páginas de um ``text`` que chega a 1 MB.
#:
#: Medido em 200k linhas: 10 facetas 1.204 → 166 ms (7,3x), ``count(*)`` sob
#: filtro 65 → 5,0 ms (13x), e o EXPLAIN passa a dizer ``USING COVERING INDEX``.
DDL_INDICE_APP = """
CREATE INDEX IF NOT EXISTS idx_prompts_app ON prompts(
  lang, quality, nsfw, lang_variant, task_type, domain,
  source, license, needs_review, edited, commercial_ok, redistributable,
  pii_found, n_chars, n_exact_dups
)
"""

#: Índice de EXPRESSÃO do "isto parece conversa colada?".
#:
#: O ``/api/stats`` soma esse teste sobre o corpus inteiro, e ele é o ÚNICO
#: número do panorama que precisa ler a coluna ``text`` — ou seja, os 687 MB da
#: tabela. Medido no banco real: 1.462 ms de 1.877 ms da rota inteira eram esta
#: soma. Com o índice de expressão a soma vira uma varredura de cobertura de
#: 5,5 ms, e o SQLite ainda o mantém sozinho a cada UPDATE de ``text``.
#:
#: Ele NÃO está no ``db.DDL`` porque a janela (``[app] chat_markers_scan_chars``)
#: vem do ``settings.toml``: DDL que muda com configuração não é constante. E o
#: SQLite casa índice de expressão por TEXTO da expressão — mudar a janela no
#: settings sem refazer o índice não daria erro, daria os 1.462 ms de volta em
#: silêncio. Por isso ``_indice_marcadores`` compara o SQL guardado no
#: ``sqlite_master`` com o de agora e refaz quando diverge.
NOME_INDICE_MARCADORES = "idx_prompts_chatmark"

#: Diretório do ``index.html``. Fica DENTRO do pacote (e não na raiz do repo)
#: para que ``pf serve`` funcione de qualquer diretório de trabalho.
STATIC = Path(__file__).resolve().parent / "static"


def _indice_marcadores(conn: sqlite3.Connection) -> None:
    """Cria (ou refaz) o índice de expressão dos marcadores de chat.

    Idempotente e barato quando já está certo: uma leitura do ``sqlite_master``.
    Quando a janela do ``settings.toml`` muda, o índice velho indexa uma
    expressão que nenhuma consulta mais escreve — ele não erra, ele só deixa de
    ser usado. Derrubar e refazer (~1,5 s) é o único jeito de a mudança de
    configuração continuar valendo alguma coisa.
    """
    ddl = (
        f"CREATE INDEX {NOME_INDICE_MARCADORES} ON prompts("
        f"{queries.expr_marcadores_chat()})"
    )
    linha = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name = ?",
        (NOME_INDICE_MARCADORES,),
    ).fetchone()
    if linha is not None:
        if str(linha["sql"]).strip() == ddl:
            return
        conn.execute(f"DROP INDEX {NOME_INDICE_MARCADORES}")
    conn.execute(ddl)


def _preparar(db_file: Path, indice: IndiceSemantico | None = None) -> dict[str, Any]:
    """Guardas de partida + metadados do banco. Levanta ``RuntimeError`` se não dá."""
    if not db_file.is_file():
        raise RuntimeError(
            f"banco não encontrado em {db_file}.\n"
            "A interface só lê o SQLite — quem o constrói é a pipeline.\n"
            "Rode:  pf load-db            (ou, antes do M7, "
            "pf load-db --allow-unlabeled-pct 100)"
        )

    conn = dbmod.connect(db_file)
    try:
        versao = dbmod.get_meta(conn, "schema_version")
        if versao is not None and int(versao) != dbmod.SCHEMA_VERSION:
            raise RuntimeError(
                f"{db_file}: schema_version={versao} mas esta app fala "
                f"{dbmod.SCHEMA_VERSION} — recarregue com `pf load-db`"
            )
        conn.execute(DDL_INDICE_APP)
        # Os índices de ordenação vivem no `db.DDL` (o s11 já os constrói), mas
        # um banco carregado por uma versão ANTERIOR do load-db não os tem — e
        # é justamente esse o banco que está no disco hoje. `IF NOT EXISTS`
        # torna isto um no-op nas próximas cargas.
        conn.executescript(dbmod.DDL_INDICES_ORDENACAO)
        _indice_marcadores(conn)

        try:
            conn.execute(
                "INSERT INTO prompts_fts(prompts_fts, rank) VALUES('integrity-check', 1)"
            )
        except sqlite3.DatabaseError as exc:
            # Não silenciar e não abortar: a app funciona sem FTS (só a busca
            # textual não), e derrubar o servidor por causa disso deixaria o
            # usuário sem nem a lista.
            print(
                f"[app] AVISO: o índice FTS5 não passou no integrity-check ({exc}). "
                "A busca textual pode vir vazia — refaça com `pf load-db`."
            )

        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM app_meta")}
        n_rows = int(conn.execute("SELECT count(*) AS n FROM prompts").fetchone()["n"])

        # SONDA DO ÍNDICE SEMÂNTICO (M10). Barata de propósito — só o cabeçalho do
        # .npy, a contagem do txt e 64 uids conferidos no SQL — porque a carga de
        # verdade é preguiçosa. O que ela pega é o caso que importa: o `.npy` ser
        # de OUTRA build do universo. Nesse estado o cosseno continua saindo, os
        # vizinhos continuam plausíveis, e cada uid devolvido é de outra linha.
        #
        # AVISA ALTO, mas NÃO derruba a app: a busca textual não depende disto, e
        # recusar a interface inteira por causa de um arquivo auxiliar deixaria o
        # usuário sem nem a lista. Quem falha alto é a rota — 500 com o conserto
        # escrito. O silêncio é o único desfecho proibido.
        semantica = None
        if indice is not None:
            semantica = indice.sondar(conn)
            if not semantica["ok"] and semantica["n_vetores"]:
                print(
                    f"[app] AVISO: busca por sentido DESLIGADA — {semantica['motivo']}.\n"
                    "[app]        A busca textual (FTS) continua funcionando."
                )

        conn.execute("PRAGMA optimize")
    finally:
        conn.close()

    return {
        "semantica": semantica,
        "db_file": str(db_file),
        "db_size_bytes": db_file.stat().st_size,
        "n_rows": n_rows,
        "schema_version": dbmod.SCHEMA_VERSION,
        # A taxonomia do banco pode ser mais velha que a do código; a app diz as
        # duas em vez de escolher uma e mentir.
        "taxonomy_version": meta.get("taxonomy_version", TAXONOMY_VERSION),
        "taxonomy_version_app": TAXONOMY_VERSION,
        "db_build_id": meta.get("db_build_id"),
        "built_at": meta.get("built_at"),
        "embed_model": meta.get("embed_model"),
        "pf_version": __version__,
        "sqlite_version": sqlite3.sqlite_version,
    }


def criar_app(
    db_file: str | Path | None = None,
    exports_dir: str | Path | None = None,
    emb_dir: str | Path | None = None,
) -> FastAPI:
    """Monta a app.

    ``db_file`` padrão: ``data/db/prompts.sqlite``. ``exports_dir`` padrão:
    ``data/exports/``; ``emb_dir`` padrão: ``data/emb/`` — são parâmetros (e não
    constantes) para que os testes gerem arquivos de verdade, e apontem um índice
    semântico sintético, sem escrever uma linha em ``data/``.
    """
    alvo = Path(db_file) if db_file is not None else paths.DB_FILE
    saida = Path(exports_dir) if exports_dir is not None else paths.EXPORTS
    embeddings = Path(emb_dir) if emb_dir is not None else paths.EMB

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db_file = alvo
        app.state.exports_dir = saida
        # Antes do _preparar: o cache é por app, e nasce vazio junto com ela.
        app.state.cache = CacheAgregados()
        # O índice semântico nasce VAZIO e não carrega nada aqui. O e5 tem ~450 MB
        # e a matriz 212 MiB: aquecer no lifespan faria `pf serve` demorar segundos
        # para todo mundo, inclusive para quem nunca vai clicar em "sentido". Quem
        # paga é a primeira busca — ou o `POST /api/semantic/warmup`, que a
        # interface dispara no clique do modo.
        app.state.semantica = IndiceSemantico(
            alvo,
            embeddings / Path(UNIVERSE_EMB).name,
            embeddings / Path(UNIVERSE_UIDS).name,
        )
        app.state.meta = _preparar(alvo, app.state.semantica)
        try:
            yield
        finally:
            # Devolve o banco a um arquivo único ao sair. Sem isto, um Ctrl+C
            # deixa -wal/-shm ao lado, e um `-shm` órfão é exatamente o sinal
            # que o pré-voo do swap do `pf load-db` lê como "alguém está com
            # isto aberto" — o próximo load-db seria recusado sem motivo.
            try:
                conn = dbmod.connect(alvo)
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.close()
            except sqlite3.Error:  # pragma: no cover - banco sumiu no meio
                pass

    app = FastAPI(
        title="Prompt Factory",
        version=__version__,
        description=(
            "Interface local de curadoria do banco de prompts reais (pt-BR + EN). "
            "Só leitura do SQLite + edição manual e coleções."
        ),
        lifespan=lifespan,
    )

    # Import aqui dentro (e não no topo) para manter o grafo de import da app
    # acíclico: as rotas importam `deps`, que importa daqui.
    from . import (
        routes_collections,
        routes_export,
        routes_prompts,
        routes_semantic,
    )

    @app.get("/api/health", tags=["app"], summary="A app subiu e o banco abriu")
    def health(conn: Conexao, meta: Estado) -> dict[str, Any]:
        """O primeiro request que a interface faz. Barato de propósito."""
        return {
            "status": "ok",
            "rotulagem_pendente": rotulagem_pendente(conn),
            **meta,
        }

    app.include_router(routes_prompts.router)
    app.include_router(routes_semantic.router)
    app.include_router(routes_collections.router)
    app.include_router(routes_export.router)

    # POR ÚLTIMO. Um mount em "/" registrado antes dos routers engoliria
    # /api/* — o Starlette casa as rotas na ordem em que foram adicionadas.
    STATIC.mkdir(parents=True, exist_ok=True)
    app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
    return app


__all__ = ["DDL_INDICE_APP", "NOME_INDICE_MARCADORES", "STATIC", "criar_app"]
