"""Testes da busca por sentido (M10).

**Nenhum teste aqui carrega o e5.** O modelo tem ~450 MB e leva ~16 s para subir
nesta máquina; o que precisa de prova não é a qualidade dos vetores dele (isso é
`test_embedder.py`), é a mecânica em volta: o alinhamento posicional, o filtro
exato, o top-k e os códigos de erro. Por isso os vetores são sintéticos e
``embedder.embed_texts`` é substituído por uma função determinística.

Os vetores de teste vivem num CÍRCULO dentro do espaço de 384 dimensões: a linha
``j`` recebe o ângulo ``j * passo``, então o cosseno com a consulta de ângulo 0
decresce monotonicamente com ``j``. Isso torna o ranking esperado **conhecido e
exato**, e não "o que o modelo achou" — que é o único jeito de um teste de busca
semântica falhar por regressão em vez de por humor do modelo.

O teste que justifica o arquivo inteiro é
``test_desalinhamento_com_o_banco_falha_alto``: um ``.npy`` de outra build devolve
vizinhos plausíveis com os uids errados, e ninguém percebe. Ele prova que esse
estado levanta em vez de responder.
"""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from prompt_factory import db as dbmod
from prompt_factory import embedder
from prompt_factory.app import semantic
from prompt_factory.app.main import criar_app
from prompt_factory.app.semantic import (
    DesalinhamentoEmbeddings,
    IndiceSemantico,
    SemanticaIndisponivel,
)

from .test_api import montar_banco

DIM = 384
NOME_EMB = "universe.f16.npy"
NOME_UIDS = "universe_uids.txt"


def _vetor(angulo: float) -> np.ndarray:
    """Unitário no plano (e0, e1) de um espaço de ``DIM`` dimensões."""
    v = np.zeros(DIM, dtype=np.float32)
    v[0] = math.cos(angulo)
    v[1] = math.sin(angulo)
    return v


def _uids_do_banco(caminho: Path) -> list[str]:
    conn = dbmod.connect(caminho)
    try:
        return [str(r["uid"]) for r in conn.execute("SELECT uid FROM prompts ORDER BY id")]
    finally:
        conn.close()


def escrever_indice(
    emb_dir: Path, uids: list[str], *, angulos: list[float] | None = None
) -> None:
    """Grava o par ``.npy`` + ``uids.txt`` alinhado por POSIÇÃO, como faz o s06."""
    emb_dir.mkdir(parents=True, exist_ok=True)
    if angulos is None:
        # Passo pequeno: 52 linhas cabem folgadas no primeiro quadrante, então o
        # cosseno com o ângulo 0 é estritamente decrescente e não há empate.
        angulos = [i * (math.pi / 2) / max(1, len(uids)) for i in range(len(uids))]
    matriz = np.stack([_vetor(a) for a in angulos]).astype(np.float16)
    with (emb_dir / NOME_EMB).open("wb") as fh:
        np.save(fh, matriz)
    (emb_dir / NOME_UIDS).write_text("\n".join(uids) + "\n", encoding="utf-8")


@pytest.fixture
def banco(tmp_path: Path) -> Path:
    caminho = tmp_path / "prompts.sqlite"
    montar_banco(caminho)
    return caminho


@pytest.fixture
def emb_dir(tmp_path: Path, banco: Path) -> Path:
    """Índice ALINHADO, mas com os uids em ordem embaralhada.

    Embaralhar não é capricho: é o que separa "casei uid com uid" de "assumi
    ``id == linha + 1``". Com os uids em ordem de id, os dois caminhos dariam o
    mesmo resultado e o teste não provaria nada.
    """
    uids = _uids_do_banco(banco)
    embaralhados = uids[::-1]
    destino = tmp_path / "emb"
    escrever_indice(destino, embaralhados)
    return destino


@pytest.fixture
def indice(banco: Path, emb_dir: Path) -> IndiceSemantico:
    return IndiceSemantico(banco, emb_dir / NOME_EMB, emb_dir / NOME_UIDS)


@pytest.fixture
def consulta_falsa(monkeypatch: pytest.MonkeyPatch) -> None:
    """Substitui o e5 por uma função determinística.

    A consulta vira o vetor de ângulo 0 — ou seja, o vizinho mais próximo é a
    PRIMEIRA linha do ``.npy``, seja qual for o uid que estiver lá.
    """
    monkeypatch.setattr(
        embedder, "embed_texts", lambda textos, **_: np.stack([_vetor(0.0) for _ in textos])
    )


# ---------------------------------------------------------------------------
# 1. alinhamento — a guarda que justifica o módulo
# ---------------------------------------------------------------------------


def test_carrega_e_casa_uid_com_id_e_nao_posicao(
    indice: IndiceSemantico, banco: Path, emb_dir: Path
) -> None:
    indice.carregar()
    uids = (emb_dir / NOME_UIDS).read_text(encoding="utf-8").split("\n")[:-1]
    conn = dbmod.connect(banco)
    try:
        esperado = {str(r["uid"]): int(r["id"]) for r in conn.execute("SELECT id, uid FROM prompts")}
    finally:
        conn.close()
    # linha i do .npy -> id do uid da linha i do txt. Como os uids estão ao
    # contrário, `id == linha + 1` daria tudo errado e este assert pegaria.
    for linha, uid in enumerate(uids):
        assert int(indice._id_da_linha[linha]) == esperado[uid]
    assert indice._id_da_linha[0] != 1, "os uids do fixture estão em ordem — o teste não prova nada"


def test_desalinhamento_com_o_banco_falha_alto(banco: Path, tmp_path: Path) -> None:
    """``.npy`` de OUTRA build: levanta, e a mensagem diz o conserto.

    Este é o defeito que o módulo inteiro existe para impedir. Sem a guarda, cada
    vetor apontaria para a linha errada do banco: os cossenos continuariam
    saindo, os vizinhos continuariam parecendo razoáveis, e cada resultado seria
    de outro prompt. Ninguém percebe — é por isso que tem de levantar.
    """
    destino = tmp_path / "emb-de-outro-universo"
    uids = _uids_do_banco(banco)
    forasteiros = [f"outrouniverso{i:04d}" for i in range(len(uids))]
    escrever_indice(destino, forasteiros)

    ix = IndiceSemantico(banco, destino / NOME_EMB, destino / NOME_UIDS)
    with pytest.raises(DesalinhamentoEmbeddings) as erro:
        ix.carregar()
    texto = str(erro.value)
    assert "outrouniverso0000" in texto, "a mensagem tem de mostrar uids que não existem"
    assert "pf load-db" in texto and "s05" in texto, "a mensagem tem de dizer o conserto"
    assert not ix.carregado and not ix.aquecido


def test_desalinhamento_parcial_tambem_falha(banco: Path, tmp_path: Path) -> None:
    """Metade casando é pior que nenhuma: parece funcionar."""
    destino = tmp_path / "emb-meio-a-meio"
    uids = _uids_do_banco(banco)
    metade = uids[: len(uids) // 2] + [f"fantasma{i:08d}" for i in range(len(uids) // 2)]
    escrever_indice(destino, metade)
    ix = IndiceSemantico(banco, destino / NOME_EMB, destino / NOME_UIDS)
    with pytest.raises(DesalinhamentoEmbeddings):
        ix.carregar()


def test_banco_maior_que_o_npy_falha(banco: Path, tmp_path: Path) -> None:
    """A outra direção: linhas do banco SEM vetor somem da busca em silêncio."""
    destino = tmp_path / "emb-parcial"
    uids = _uids_do_banco(banco)
    escrever_indice(destino, uids[:5])
    ix = IndiceSemantico(banco, destino / NOME_EMB, destino / NOME_UIDS)
    with pytest.raises(DesalinhamentoEmbeddings, match="têm vetor"):
        ix.carregar()


def test_contagem_divergente_entre_npy_e_txt_falha(banco: Path, tmp_path: Path) -> None:
    """Os dois nascem juntos no s06; divergir é sinal de escrita interrompida."""
    destino = tmp_path / "emb-torto"
    uids = _uids_do_banco(banco)
    escrever_indice(destino, uids)
    # tira uma linha só do txt: o `.npy` continua com N vetores
    (destino / NOME_UIDS).write_text("\n".join(uids[:-1]) + "\n", encoding="utf-8")
    ix = IndiceSemantico(banco, destino / NOME_EMB, destino / NOME_UIDS)
    with pytest.raises(DesalinhamentoEmbeddings, match="vetores"):
        ix.carregar()


def test_sem_arquivos_e_indisponivel_nao_desalinhado(banco: Path, tmp_path: Path) -> None:
    """Clone limpo que não rodou o s05: falta de insumo, não defeito."""
    ix = IndiceSemantico(banco, tmp_path / "nada.npy", tmp_path / "nada.txt")
    assert ix.disponivel() is False
    with pytest.raises(SemanticaIndisponivel, match="s05"):
        ix.carregar()


def test_sonda_de_partida_aprova_o_par_bom(
    indice: IndiceSemantico, banco: Path
) -> None:
    conn = dbmod.connect(banco)
    try:
        sonda = indice.sondar(conn)
    finally:
        conn.close()
    assert sonda["ok"] is True and sonda["motivo"] is None
    assert sonda["n_vetores"] == sonda["n_uids"] == sonda["n_banco"]
    assert sonda["amostra_casada"] == sonda["amostra"] > 0
    # A sonda NÃO carrega nada: é o ponto dela.
    assert not indice.carregado


def test_sonda_de_partida_denuncia_o_par_de_outro_universo(
    banco: Path, tmp_path: Path
) -> None:
    destino = tmp_path / "emb-alheio"
    escrever_indice(destino, [f"alheio{i:010d}" for i in range(len(_uids_do_banco(banco)))])
    ix = IndiceSemantico(banco, destino / NOME_EMB, destino / NOME_UIDS)
    conn = dbmod.connect(banco)
    try:
        sonda = ix.sondar(conn)
    finally:
        conn.close()
    assert sonda["ok"] is False
    assert "OUTRO universo" in str(sonda["motivo"])


def test_troca_do_banco_embaixo_da_app_descarrega_a_matriz(
    indice: IndiceSemantico, banco: Path
) -> None:
    """``pf load-db`` troca o arquivo; o mapa uid → id na RAM vira de outro corpus."""
    indice.carregar()
    assert indice.carregado
    conn = dbmod.connect(banco)
    try:
        indice.conferir_build(conn)
        assert indice.carregado, "sem troca, nada pode ser descartado"
        dbmod.set_meta(conn, "db_build_id", "feedfeedfeedfeed")
        indice.conferir_build(conn)
    finally:
        conn.close()
    assert not indice.carregado


# ---------------------------------------------------------------------------
# 2. pontuação: top-k exato dentro do filtro
# ---------------------------------------------------------------------------


def test_ranking_segue_o_cosseno_e_nao_a_ordem_do_banco(indice: IndiceSemantico) -> None:
    indice.carregar()
    ids, escores, n = indice.pontuar(_vetor(0.0), indice._id_da_linha, k=5)
    assert n == int(indice._id_da_linha.size)
    assert escores == sorted(escores, reverse=True)
    assert escores[0] == pytest.approx(1.0, abs=1e-3)
    # a linha 0 do .npy é o ângulo 0 -> é o vizinho mais próximo, e o id dela NÃO
    # é o menor id do banco (os uids do fixture estão ao contrário).
    assert ids[0] == int(indice._id_da_linha[0])


def test_o_filtro_e_exato_e_nao_um_over_fetch(indice: IndiceSemantico) -> None:
    """Tirar os 3 melhores do conjunto permitido devolve os 3 SEGUINTES.

    É a prova de que a máscara entra ANTES do top-k. Numa implementação que pega
    os N melhores e filtra depois, um recorte que exclui justamente os primeiros
    devolveria menos itens do que pediu — ou, pior, nenhum.
    """
    indice.carregar()
    todos = [int(x) for x in indice._id_da_linha]
    completo, _, _ = indice.pontuar(_vetor(0.0), todos, k=6)
    permitidos = [i for i in todos if i not in completo[:3]]
    recortado, _, n = indice.pontuar(_vetor(0.0), permitidos, k=3)
    assert recortado == completo[3:6]
    assert n == len(permitidos)
    assert all(i not in completo[:3] for i in recortado)


def test_k_maior_que_o_conjunto_devolve_o_conjunto(indice: IndiceSemantico) -> None:
    indice.carregar()
    tres = [int(x) for x in indice._id_da_linha[:3]]
    ids, escores, n = indice.pontuar(_vetor(0.0), tres, k=500)
    assert len(ids) == len(escores) == 3 and n == 3


def test_id_que_nao_tem_vetor_e_ignorado_sem_estourar(indice: IndiceSemantico) -> None:
    indice.carregar()
    ids, _, n = indice.pontuar(_vetor(0.0), [10**9, -5, int(indice._id_da_linha[0])], k=5)
    assert ids == [int(indice._id_da_linha[0])] and n == 1


def test_conjunto_vazio_devolve_vazio(indice: IndiceSemantico) -> None:
    indice.carregar()
    assert indice.pontuar(_vetor(0.0), [], k=5) == ([], [], 0)


def test_f16_da_o_mesmo_ranking_que_f32(banco: Path, emb_dir: Path) -> None:
    """O modo de baixa RAM é 34x mais lento, não menos correto."""
    alvo = [_vetor(0.0)]
    saidas = []
    for precisao in ("f32", "f16"):
        ix = IndiceSemantico(
            banco, emb_dir / NOME_EMB, emb_dir / NOME_UIDS, precisao=precisao
        )
        ix.carregar()
        ids, escores, _ = ix.pontuar(alvo[0], ix._id_da_linha, k=10)
        saidas.append((ids, escores))
    assert saidas[0][0] == saidas[1][0]
    assert saidas[0][1] == pytest.approx(saidas[1][1], abs=1e-4)


def test_precisao_invalida_explode_na_construcao(monkeypatch: pytest.MonkeyPatch) -> None:
    """``semantic_precision = "float32"`` é erro de configuração, não fallback.

    Cair para f32 em silêncio numa máquina onde alguém pediu f16 por falta de RAM
    é trocar 106 MiB por 212 sem avisar; cair para f16 seria multiplicar o custo
    por 34. Nenhum dos dois pode acontecer sem uma linha na tela.
    """
    from prompt_factory import config

    monkeypatch.setitem(config.settings()["app"], "semantic_precision", "float32")
    with pytest.raises(ValueError, match="semantic_precision"):
        semantic.precisao_configurada()
    with pytest.raises(ValueError, match="semantic_precision"):
        IndiceSemantico("x.sqlite")


# ---------------------------------------------------------------------------
# 3. a rota
# ---------------------------------------------------------------------------


@pytest.fixture
def cliente(tmp_path: Path, banco: Path, emb_dir: Path, consulta_falsa: None):
    with TestClient(criar_app(banco, tmp_path / "exports", emb_dir)) as c:
        yield c


def test_busca_devolve_o_mesmo_shape_da_listagem(cliente: TestClient) -> None:
    """O front desenha os dois com o mesmo código: os itens têm de bater."""
    sem = cliente.get("/api/semantic", params={"q": "bolo de cenoura", "k": 5})
    assert sem.status_code == 200, sem.text
    corpo = sem.json()
    lista = cliente.get("/api/prompts", params={"page_size": 5}).json()

    chaves_lista = set(lista["items"][0])
    chaves_sem = set(corpo["items"][0])
    faltando = chaves_lista - chaves_sem
    assert not faltando, f"a busca semântica perdeu campos do contrato: {faltando}"
    assert chaves_sem - chaves_lista == {"similaridade"}

    assert corpo["sort_efetivo"] == "semantic"
    assert corpo["truncated_total"] is True
    assert corpo["total"] == len(corpo["items"]) == 5
    assert corpo["n_candidatos"] > 5
    assert corpo["n_sem_vetor"] == 0
    assert corpo["q_fts"] is None
    for item in corpo["items"]:
        assert item["score"] == item["similaridade"]
        assert item["snippet"] is None
    escores = [i["similaridade"] for i in corpo["items"]]
    assert escores == sorted(escores, reverse=True)


def test_a_primeira_chamada_reporta_o_aquecimento_e_a_segunda_nao(
    cliente: TestClient,
) -> None:
    """É o que a interface usa para dizer 'estou carregando o modelo'."""
    primeira = cliente.get("/api/semantic", params={"q": "receita"}).json()
    segunda = cliente.get("/api/semantic", params={"q": "receita"}).json()
    assert primeira["aquecimento_ms"] is not None
    assert segunda["aquecimento_ms"] is None
    assert [i["uid"] for i in primeira["items"]] == [i["uid"] for i in segunda["items"]]


def test_o_filtro_da_listagem_vale_na_busca_semantica(cliente: TestClient) -> None:
    corpo = cliente.get("/api/semantic", params={"q": "qualquer coisa", "lang": "pt", "k": 50})
    assert corpo.status_code == 200, corpo.text
    dados = corpo.json()
    assert dados["items"], "o filtro não pode zerar o corpus de teste"
    assert {i["lang"] for i in dados["items"]} == {"pt"}
    sem_filtro = cliente.get("/api/semantic", params={"q": "qualquer coisa", "k": 50}).json()
    assert dados["n_candidatos"] < sem_filtro["n_candidatos"]


def test_ordenacao_e_paginacao_sao_recusadas_e_nao_ignoradas(cliente: TestClient) -> None:
    """422, não silêncio: ordenar dentro de uma busca por sentido não existe."""
    for param in ({"sort": "newest"}, {"page": 2}, {"page_size": 10}):
        r = cliente.get("/api/semantic", params={"q": "teste", **param})
        assert r.status_code == 422, f"{param} passou batido: {r.text}"


def test_consulta_curta_demais_e_422(cliente: TestClient) -> None:
    assert cliente.get("/api/semantic", params={"q": "a"}).status_code == 422
    assert cliente.get("/api/semantic").status_code == 422
    assert cliente.get("/api/semantic", params={"q": "teste", "k": 9999}).status_code == 422


def test_status_e_warmup(cliente: TestClient) -> None:
    frio = cliente.get("/api/semantic/status").json()
    assert frio["disponivel"] is True
    assert frio["aquecido"] is False
    assert frio["matriz_carregada"] is False
    assert frio["sonda"]["ok"] is True

    aquecido = cliente.post("/api/semantic/warmup").json()
    assert "aquecendo" in aquecido and "iniciou_agora" in aquecido

    cliente.get("/api/semantic", params={"q": "aquece"})
    quente = cliente.get("/api/semantic/status").json()
    assert quente["aquecido"] is True
    assert quente["n_vetores"] == quente["casados"] == quente["n_banco"]
    assert quente["mb_residentes"] > 0
    assert quente["carga_ms"] is not None


def test_aquecido_exige_matriz_E_modelo(
    indice: IndiceSemantico, consulta_falsa: None
) -> None:
    """Regressão de um bug pego no navegador contra o corpus real.

    A matriz carrega em 417 ms e o e5 em ~16 s. Com ``aquecido`` respondendo só
    pela matriz, a interface anunciava "modelo pronto" meio segundo depois do
    clique e disparava a busca — que travava 16 s com a tela dizendo o contrário.
    """
    indice.carregar()
    assert indice.carregado is True
    assert indice.aquecido is False, "matriz carregada NÃO é 'a próxima busca é rápida'"
    indice.vetor_da_consulta("agora sim")
    assert indice.aquecido is True


def test_sem_embeddings_a_rota_e_503_e_o_fts_continua(
    tmp_path: Path, banco: Path, consulta_falsa: None
) -> None:
    """Falta de insumo não pode derrubar a interface nem a busca textual."""
    vazio = tmp_path / "sem-emb"
    vazio.mkdir()
    with TestClient(criar_app(banco, tmp_path / "exports", vazio)) as c:
        resposta = c.get("/api/semantic", params={"q": "bolo"})
        assert resposta.status_code == 503
        assert "s05" in resposta.json()["detail"]
        assert c.get("/api/semantic/status").json()["disponivel"] is False
        # o FTS não depende disto
        fts = c.get("/api/prompts", params={"q": "coracao"})
        assert fts.status_code == 200 and fts.json()["total"] > 0


def test_desalinhamento_na_rota_e_500_e_nao_resultado(
    tmp_path: Path, banco: Path, consulta_falsa: None
) -> None:
    """O teste que o marco pede: desalinhado FALHA, não devolve item nenhum.

    Um 503 aqui seria errado de propósito: "tente de novo mais tarde" convida a
    ignorar. O que existe é um banco e uma matriz discordando sobre o que é cada
    linha, e a única saída é refazer um dos dois.
    """
    torto = tmp_path / "emb-torto"
    escrever_indice(torto, [f"deoutrabuild{i:04d}" for i in range(len(_uids_do_banco(banco)))])
    with TestClient(criar_app(banco, tmp_path / "exports", torto)) as c:
        resposta = c.get("/api/semantic", params={"q": "bolo de cenoura", "k": 5})
        assert resposta.status_code == 500
        detalhe = resposta.json()["detail"]
        assert "pf load-db" in detalhe
        assert "items" not in resposta.json()
        # e o estado é visível no diagnóstico, não só no erro da busca
        assert c.get("/api/semantic/status").json()["sonda"]["ok"] is False
        # a busca textual continua de pé
        assert c.get("/api/prompts", params={"q": "coracao"}).status_code == 200


def test_a_subida_denuncia_o_desalinhamento_no_health(
    tmp_path: Path, banco: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sonda do lifespan aparece no ``/api/health`` e no terminal."""
    torto = tmp_path / "emb-torto"
    escrever_indice(torto, [f"deoutrabuild{i:04d}" for i in range(len(_uids_do_banco(banco)))])
    with TestClient(criar_app(banco, tmp_path / "exports", torto)) as c:
        saude = c.get("/api/health").json()
        assert saude["semantica"]["ok"] is False
        assert "OUTRO universo" in saude["semantica"]["motivo"]
    assert "busca por sentido DESLIGADA" in capsys.readouterr().out


def test_nenhuma_rota_semantica_e_async() -> None:
    """A regra dura de ``deps.py``, aplicada ao módulo novo.

    Aqui vale em dobro: além do SQL síncrono, o produto de matrizes segura o GIL
    por ~6 ms e a carga por segundos. No event loop isso seria o servidor inteiro
    parado a cada busca.
    """
    import inspect

    from prompt_factory.app import routes_semantic

    for rota in routes_semantic.router.routes:
        alvo = rota.endpoint  # type: ignore[attr-defined]
        assert not inspect.iscoroutinefunction(alvo), f"{alvo.__name__} é async def"


def test_a_conexao_do_indice_e_propria_e_nao_a_do_request(
    indice: IndiceSemantico,
) -> None:
    """``carregar`` roda também numa thread de aquecimento: precisa abrir a sua.

    A conexão do request morre com ele, e a de outra thread nem existe. Se um dia
    alguém "simplificar" passando a conexão da rota, o warmup em thread quebra —
    e quebra em produção, não aqui.
    """
    conn = indice._abrir()
    try:
        assert isinstance(conn, sqlite3.Connection)
        assert conn.execute("SELECT count(*) AS n FROM prompts").fetchone()["n"] > 0
    finally:
        conn.close()


def test_o_indice_e_por_app_e_nao_um_global(
    tmp_path: Path, banco: Path, emb_dir: Path
) -> None:
    """Duas apps no mesmo processo apontam para bancos diferentes."""
    outro = tmp_path / "outro.sqlite"
    montar_banco(outro)
    with (
        TestClient(criar_app(banco, tmp_path / "e1", emb_dir)) as a,
        TestClient(criar_app(outro, tmp_path / "e2", emb_dir)) as b,
    ):
        assert a.app.state.semantica is not b.app.state.semantica  # type: ignore[attr-defined]
        assert a.app.state.semantica.db_file != b.app.state.semantica.db_file  # type: ignore[attr-defined]


def test_a_consulta_passa_pelo_embedder_e_nao_pelo_modelo_cru(
    monkeypatch: pytest.MonkeyPatch, indice: IndiceSemantico
) -> None:
    """Regressão mais cara do projeto: o prefixo ``query: `` mora no embedder.

    Chamar o ``SentenceTransformer`` direto daqui produziria vetores de outro
    espaço — e cossenos que continuam saindo, plausíveis e errados.
    """
    chamadas: list[Any] = []

    def espiao(textos: Any, **kwargs: Any) -> np.ndarray:
        chamadas.append(list(textos))
        return np.stack([_vetor(0.0) for _ in textos])

    monkeypatch.setattr(embedder, "embed_texts", espiao)
    indice.vetor_da_consulta("como faço um bolo")
    assert chamadas == [["como faço um bolo"]]
