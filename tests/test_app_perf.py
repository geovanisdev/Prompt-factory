"""Performance da interface contra o corpus real (M9, parte C).

Aqui não se mede tempo — teste de relógio numa máquina compartilhada é ruído. O
que se prova são as **invariantes de que a velocidade depende**, e que somem sem
sintoma nenhum quando alguém "arruma" o código:

1. **O plano.** Que o ``GROUP BY`` das facetas e a soma dos marcadores de chat
   caiam em varredura de COBERTURA, sem B-tree temporário. Trocar a ordem de uma
   tupla derruba o primeiro de 37 para 162 ms e nenhum teste de valor percebe.
2. **A equivalência.** Que juntar doze ``GROUP BY`` numa varredura só devolva
   EXATAMENTE as mesmas contagens que as doze consultas separadas devolviam, em
   toda combinação de filtro que a interface produz.
3. **A invalidação.** Que o cache não minta. Um cache errado é pior que
   lentidão: a barra lateral continua desenhando um corpus que não existe mais e
   ninguém desconfia de um número.

Os números medidos que motivaram cada mudança estão nos comentários do código
(``db.DDL_INDICES_ORDENACAO``, ``queries.PREFIXO_FACETAS``, ``app/cache.py``).
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from prompt_factory import db as dbmod
from prompt_factory.app import queries
from prompt_factory.app.cache import CHAVE_BUILD, CacheAgregados
from prompt_factory.app.main import DDL_INDICE_APP, NOME_INDICE_MARCADORES, criar_app
from prompt_factory.app.models import ConsultaPrompts, Filtros
from prompt_factory.app.routes_prompts import _contagens_das_facetas, _normalizar

from .test_api import montar_banco  # o mesmo banco sintético hostil da API


@pytest.fixture
def cliente(tmp_path: Path):
    montar_banco(tmp_path / "prompts.sqlite")
    with TestClient(criar_app(tmp_path / "prompts.sqlite", tmp_path / "exports")) as c:
        yield c


@pytest.fixture
def conexao(tmp_path: Path):
    """Banco já passado pelo lifespan (índices da app criados), sem cliente."""
    montar_banco(tmp_path / "prompts.sqlite")
    with TestClient(criar_app(tmp_path / "prompts.sqlite", tmp_path / "exports")):
        pass
    conn = dbmod.connect(tmp_path / "prompts.sqlite")
    try:
        yield conn
    finally:
        conn.close()


def plano(conn: sqlite3.Connection, sql: str, params: Any = ()) -> str:
    return " | ".join(linha[3] for linha in conn.execute("EXPLAIN QUERY PLAN " + sql, params))


# ---------------------------------------------------------------------------
# 1. as invariantes estruturais dos índices
# ---------------------------------------------------------------------------


def _colunas_do_indice(ddl: str) -> list[str]:
    dentro = ddl[ddl.index("(") + 1 : ddl.rindex(")")]
    return [c.strip() for c in dentro.replace("\n", " ").split(",")]


def test_prefixo_das_facetas_e_prefixo_contiguo_do_indice_da_app() -> None:
    """A invariante MAIS frágil e mais cara do módulo de facetas.

    ``GROUP BY`` só é resolvido sem B-tree temporário quando as colunas são o
    prefixo CONTÍGUO de um índice. Medido no banco real, as mesmas 11 dimensões:
    prefixo contíguo 37 ms, o mesmo prefixo com um buraco 162 ms, ordem
    arbitrária 202 ms. Nada disso muda o resultado — só o custo —, então este é o
    único teste que percebe a regressão.
    """
    colunas = _colunas_do_indice(DDL_INDICE_APP)
    assert colunas[: len(queries.PREFIXO_FACETAS)] == list(queries.PREFIXO_FACETAS)


def test_toda_faceta_de_coluna_crua_esta_no_prefixo() -> None:
    """Faceta nova que seja coluna crua tem de entrar no prefixo, não virar a
    décima terceira varredura em silêncio."""
    fora = [
        f.nome
        for f in queries.FACETAS
        if f.expr.startswith("p.") and f.expr[2:] not in queries.PREFIXO_FACETAS
    ]
    assert fora == [], f"facetas de coluna crua fora do prefixo: {fora}"


def test_indices_de_ordenacao_existem_e_o_s11_os_conhece() -> None:
    for nome in ("created", "nchars", "dups", "labelm"):
        assert f"idx_prompts_{nome}" in dbmod.DDL_INDICES_ORDENACAO
        # Sem estar em INDEXES, a carga bulk do s11 não os derruba antes de
        # inserir — e construir a B-tree linha a linha custa mais que refazê-la.
        assert f"idx_prompts_{nome}" in dbmod.INDEXES
    assert dbmod.DDL_INDICES_ORDENACAO in dbmod.DDL


@pytest.mark.parametrize(
    ("sort", "indice"),
    [
        ("newest", "idx_prompts_created"),
        ("oldest", "idx_prompts_created"),
        ("shortest", "idx_prompts_nchars"),
        ("longest", "idx_prompts_nchars"),
        ("dups", "idx_prompts_dups"),
    ],
)
def test_ordenacao_usa_indice_e_nao_ordena_o_corpus(
    conexao: sqlite3.Connection, sort: str, indice: str
) -> None:
    """Sem estes índices, TODA ordenação da listagem era ``SCAN p`` +
    ``USE TEMP B-TREE FOR ORDER BY`` — 144.754 linhas de uma tabela de 687 MB
    ordenadas para mostrar 50 (479 ms em ``newest``, 557 em ``longest``)."""
    sql, params, _ = queries.sql_lista(ConsultaPrompts(sort=sort))
    texto = plano(conexao, sql, params)
    assert indice in texto, texto
    # "LAST TERM OF ORDER BY" é aceitável: ordena só dentro de cada grupo de
    # valor igual, e o `LIMIT` corta cedo. B-tree do ORDER BY inteiro, não.
    assert "USE TEMP B-TREE FOR ORDER BY" not in texto, texto


def test_facetas_juntas_saem_de_uma_varredura_de_cobertura(
    conexao: sqlite3.Connection,
) -> None:
    sql, params = queries.sql_facetas_prefixo(Filtros())
    texto = plano(conexao, sql, params)
    assert "COVERING INDEX idx_prompts_app" in texto, texto
    assert "TEMP B-TREE" not in texto, texto


def test_marcadores_de_chat_saem_do_indice_de_expressao(
    conexao: sqlite3.Connection,
) -> None:
    """Era o número mais caro do /api/stats: a única soma que lê a coluna
    ``text``, ou seja, os 687 MB da tabela (1.462 ms dos 1.877 da rota)."""
    expr = queries.expr_marcadores_chat()
    texto = plano(conexao, f"SELECT sum({expr}) FROM prompts")
    assert NOME_INDICE_MARCADORES in texto, texto


def test_indice_de_marcadores_e_refeito_quando_a_janela_muda(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O SQLite casa índice de expressão por TEXTO. Mudar
    ``chat_markers_scan_chars`` sem refazer o índice não daria erro — daria os
    1.462 ms de volta, em silêncio. Por isso a app compara e refaz."""
    montar_banco(tmp_path / "p.sqlite")
    with TestClient(criar_app(tmp_path / "p.sqlite", tmp_path / "exports")):
        pass

    def _guardado() -> str:
        conn = dbmod.connect(tmp_path / "p.sqlite")
        try:
            return str(
                conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name = ?",
                    (NOME_INDICE_MARCADORES,),
                ).fetchone()["sql"]
            )
        finally:
            conn.close()

    antes = _guardado()
    assert "20000" in antes

    monkeypatch.setattr(queries, "limites", lambda: (1200, 777, 100_000))
    with TestClient(criar_app(tmp_path / "p.sqlite", tmp_path / "exports")):
        pass
    depois = _guardado()
    assert "777" in depois and depois != antes


# ---------------------------------------------------------------------------
# 2. equivalência: uma varredura == doze consultas
# ---------------------------------------------------------------------------


def _facetas_uma_a_uma(
    conn: sqlite3.Connection, filtros: Filtros
) -> dict[str, dict[Any, int]]:
    """O ORÁCULO: a implementação anterior, doze ``GROUP BY`` independentes.

    Mantida aqui de propósito, e só aqui. É contra ela que a passada única é
    conferida — sem um oráculo, "as contagens batem" seria a passada única
    concordando consigo mesma.
    """
    saida: dict[str, dict[Any, int]] = {}
    for faceta in queries.FACETAS:
        sql, params = queries.sql_faceta(filtros, faceta)
        bruto = {
            linha["v"]: int(linha["n"])
            for linha in queries.executar_fts(conn, sql, params, filtros.q)
        }
        saida[faceta.nome] = _normalizar(faceta.nome, bruto)
    return saida


#: Os recortes que a interface realmente produz, mais os que exercitam cada
#: caminho do ``build_where``: dimensão facetada (que força a faceta a sair da
#: passada única), booleano anulável, faixa numérica, busca textual e coleção.
FILTROS_DE_PROVA: tuple[dict[str, Any], ...] = (
    {},
    {"lang": ["pt"]},
    {"lang": ["pt", "en"]},
    {"lang_variant": ["pt-BR"]},
    {"source": ["aya"]},
    {"license": ["mit"]},
    {"nsfw": "include"},
    {"nsfw": "only"},
    {"pii": "only"},
    {"pii": "none"},
    {"needs_review": True},
    {"needs_review": False},
    {"edited": False},
    {"quality": [1]},
    {"quality_min": 2},
    {"task_type": ["outro"]},
    {"domain": ["geral"]},
    {"commercial_only": True},
    {"redistributable_only": True},
    {"max_dups": 0},
    {"n_chars_min": 30},
    {"n_chars_max": 60},
    {"n_chars_min": 20, "n_chars_max": 80},
    {"q": "prompt"},
    {"q": "prompt", "lang": ["pt"]},
    {"lang": ["pt"], "source": ["aya"], "needs_review": True},
    {"lang": ["pt"], "quality_min": 1, "pii": "none", "n_chars_min": 10},
)


@pytest.mark.parametrize("recorte", FILTROS_DE_PROVA, ids=lambda r: str(sorted(r)) or "vazio")
def test_facetas_juntas_batem_com_as_separadas(
    conexao: sqlite3.Connection, recorte: dict[str, Any]
) -> None:
    """A prova de que a otimização não mudou nenhum número.

    Cada dimensão é contada com o filtro DELA MESMA removido, então nem todas
    podem dividir a mesma varredura — ``particionar_facetas`` decide quais. Se
    essa decisão errar para o lado errado, alguma contagem sai com o filtro que
    não devia, e é exatamente isso que este teste pega.
    """
    filtros = Filtros(**recorte)
    assert _contagens_das_facetas(conexao, filtros) == _facetas_uma_a_uma(conexao, filtros)


@pytest.mark.parametrize("recorte", FILTROS_DE_PROVA, ids=lambda r: str(sorted(r)) or "vazio")
def test_hits_materializados_dao_a_mesma_contagem_que_o_cte(
    conexao: sqlite3.Connection, recorte: dict[str, Any]
) -> None:
    """A tabela temporária de acertos não pode mudar NENHUM número — ela só troca
    o plano. O oráculo aqui é o caminho do CTE, que é o que a listagem continua
    usando."""
    filtros = Filtros(**recorte)
    esperado = _facetas_uma_a_uma(conexao, filtros)
    hits = queries.materializar_hits(conexao, filtros)
    assert hits is bool(queries.sanitize_fts(filtros.q))
    assert _contagens_das_facetas(conexao, filtros, hits_prontos=hits) == esperado

    sql_cte, p_cte = queries.sql_contagem(filtros)
    sql_tmp, p_tmp = queries.sql_contagem(filtros, hits_prontos=hits)
    assert (
        queries.executar_fts(conexao, sql_tmp, p_tmp, filtros.q)[0]["n"]
        == queries.executar_fts(conexao, sql_cte, p_cte, filtros.q)[0]["n"]
    )


def test_a_listagem_continua_no_cte_e_nao_na_temporaria() -> None:
    """A armadilha do cabeçalho de ``queries`` continua valendo para a LISTAGEM:
    lá o ``LIMIT`` corta cedo e materializar apaga a saída antecipada. As duas
    decisões opostas coexistem porque as duas consultas são diferentes."""
    sql, _, _ = queries.sql_lista(ConsultaPrompts(q="prompt"))
    assert "prompts_fts MATCH" in sql
    assert queries.TABELA_HITS not in sql
    assert "MATERIALIZED" not in sql


def test_faceta_com_busca_nao_toca_a_tabela_de_prompts(
    conexao: sqlite3.Connection,
) -> None:
    """O ganho inteiro está aqui: com o CTE o plano carrega a LINHA COMPLETA de
    cada acerto (``SEARCH p USING INTEGER PRIMARY KEY``), com o ``text`` junto,
    só para contar. Com a temporária, o índice de cobertura responde sozinho."""
    filtros = Filtros(q="prompt", lang=["pt"])
    queries.materializar_hits(conexao, filtros)
    sql, params = queries.sql_facetas_prefixo(filtros, hits_prontos=True)
    texto = plano(conexao, sql, params)
    assert "COVERING INDEX idx_prompts_app" in texto, texto
    assert "USING INTEGER PRIMARY KEY" in texto, texto  # a sonda em pf_hits


def test_materializar_sobrevive_a_query_hostil(conexao: sqlite3.Connection) -> None:
    """O paraquedas do ``executar_fts`` mudou de lugar: agora acontece UMA vez,
    na materialização, em vez de uma vez por consulta de faceta."""
    for entrada in ('a" OR b', "e-mail", "NEAR(", "*", "^^^", 'aspas"soltas'):
        filtros = Filtros(q=entrada)
        if not queries.sanitize_fts(entrada):
            continue
        assert queries.materializar_hits(conexao, filtros) is True
        assert _contagens_das_facetas(conexao, filtros, hits_prontos=True) is not None


@pytest.mark.parametrize("recorte", FILTROS_DE_PROVA, ids=lambda r: str(sorted(r)) or "vazio")
def test_particao_cobre_todas_as_facetas_sem_repetir(recorte: dict[str, Any]) -> None:
    juntas, sozinhas = queries.particionar_facetas(Filtros(**recorte))
    nomes = [f.nome for f in juntas] + [f.nome for f in sozinhas]
    assert sorted(nomes) == sorted(f.nome for f in queries.FACETAS)
    assert len(nomes) == len(set(nomes))


def test_faceta_com_filtro_proprio_ativo_sai_da_passada_unica() -> None:
    """Marcar "pt" não pode zerar a contagem de "en": a faceta ``lang`` passa a
    ter um WHERE diferente do das outras e precisa de consulta própria."""
    juntas, sozinhas = queries.particionar_facetas(Filtros(lang=["pt"]))
    assert "lang" in [f.nome for f in sozinhas]
    assert "source" in [f.nome for f in juntas]

    # Sem filtro nenhum — o primeiro paint — sobram DUAS de fora, e as duas por
    # motivo bom: o bucket de tamanho não é coluna crua, e a faceta `nsfw` tem o
    # WHERE vazio porque ignora o `nsfw="exclude"` que é DEFAULT e produz
    # condição. Nove das doze dimensões saem de uma varredura só.
    juntas, sozinhas = queries.particionar_facetas(Filtros())
    assert [f.nome for f in sozinhas] == ["nsfw", "n_chars_bucket"]
    assert len(juntas) == 10


def test_campos_ativos_sai_do_build_where_e_nao_de_uma_segunda_lista() -> None:
    # `nsfw="exclude"` é o DEFAULT e AINDA ASSIM produz condição
    # (`p.nsfw IS NOT 1`). Uma lista de "campos preenchidos pelo usuário" diria
    # que nada está ativo aqui, juntaria a faceta `nsfw` com as outras e contaria
    # o NSFW sob o filtro que exclui NSFW. É por isso que `campos_ativos` sai do
    # próprio `build_where`, por diferença, e não de uma segunda lista.
    assert queries.campos_ativos(Filtros()) == frozenset({"nsfw"})
    assert queries.campos_ativos(Filtros(nsfw="include")) == frozenset()
    assert queries.campos_ativos(Filtros(lang=["pt"])) == frozenset({"lang", "nsfw"})
    assert "nsfw" in queries.campos_ativos(Filtros(nsfw="only"))
    assert queries.campos_ativos(Filtros(nsfw="include", quality_min=2)) == frozenset(
        {"quality_min"}
    )
    assert queries.campos_ativos(Filtros(nsfw="include", n_chars_min=10)) == frozenset(
        {"n_chars_min"}
    )


def test_a_resposta_da_rota_de_facetas_nao_mudou(cliente: TestClient) -> None:
    """Contrato: a soma de uma dimensão NÃO filtrada é o total."""
    corpo = cliente.get("/api/facets").json()
    total = corpo["total"]
    for dimensao in ("lang", "source", "license", "quality", "n_chars_bucket"):
        soma = sum(o["count"] for o in corpo["facets"][dimensao])
        assert soma == total, dimensao


# ---------------------------------------------------------------------------
# 3. paginação profunda
# ---------------------------------------------------------------------------


def test_offset_profundo_forca_o_indice_de_ordenacao() -> None:
    raso, _, _ = queries.sql_lista(ConsultaPrompts(sort="newest", page=2))
    assert "INDEXED BY" not in raso

    fundo, _, _ = queries.sql_lista(ConsultaPrompts(sort="newest", page=2000))
    assert "INDEXED BY idx_prompts_created" in fundo


def test_a_dica_nao_vale_para_busca_nem_para_outras_ordens() -> None:
    """Com o CTE do FTS o laço externo são os acertos; forçar o índice ali
    inverteria a junção e varreria o corpus a cada acerto. E ``longest`` /
    ``dups`` / ``random`` não saem ordenados de ``idx_prompts_created``."""
    com_busca, _, _ = queries.sql_lista(
        ConsultaPrompts(sort="newest", page=2000, q="prompt")
    )
    assert "INDEXED BY" not in com_busca
    for sort in ("longest", "shortest", "dups", "random"):
        sql, _, _ = queries.sql_lista(ConsultaPrompts(sort=sort, page=2000))
        assert "INDEXED BY" not in sql, sort


def test_offset_profundo_devolve_as_mesmas_linhas_com_e_sem_a_dica(
    conexao: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dica de planner não pode mudar resultado. Se mudar, é bug do índice."""
    consulta = ConsultaPrompts(sort="newest", page=2, page_size=5)
    sem, params, _ = queries.sql_lista(consulta)
    monkeypatch.setattr(queries, "_limiar_offset_profundo", lambda: 1)
    com, params2, _ = queries.sql_lista(consulta)
    assert "INDEXED BY idx_prompts_created" in com and "INDEXED BY" not in sem
    esperado = [r["uid"] for r in conexao.execute(sem, params)]
    assert [r["uid"] for r in conexao.execute(com, params2)] == esperado
    assert esperado  # senão o teste passaria comparando duas listas vazias


# ---------------------------------------------------------------------------
# 4. o cache não mente
# ---------------------------------------------------------------------------


def _contagem(corpo: dict[str, Any], dimensao: str, valor: Any) -> int:
    for opcao in corpo["facets"][dimensao]:
        if opcao["value"] == valor:
            return int(opcao["count"])
    raise AssertionError(f"{dimensao}={valor!r} não está na resposta")


def test_o_cache_realmente_serve_a_segunda_chamada(cliente: TestClient) -> None:
    """Sem esta asserção, todos os testes de invalidação abaixo passariam com o
    cache desligado — e não provariam nada."""
    cache = cliente.app.state.cache
    cliente.get("/api/facets")
    acertos = cache.acertos
    cliente.get("/api/facets")
    assert cache.acertos == acertos + 1


def test_editar_um_rotulo_muda_a_faceta_na_chamada_seguinte(cliente: TestClient) -> None:
    """O caso do enunciado, e o que a curadoria faz o dia inteiro: a interface
    re-consulta /api/facets logo depois de todo PATCH."""
    antes = cliente.get("/api/facets").json()
    uid = cliente.get("/api/prompts", params={"needs_review": True}).json()["items"][0]["uid"]

    resposta = cliente.patch(f"/api/prompts/{uid}", json={"quality": 3})
    assert resposta.status_code == 200

    depois = cliente.get("/api/facets").json()
    # Rotular à mão tira o item da fila de revisão (needs_review = 0) e o põe em
    # quality=3. As duas contagens têm de andar, na MESMA chamada seguinte.
    assert _contagem(depois, "quality", 3) == _contagem(antes, "quality", 3) + 1
    assert _contagem(depois, "needs_review", True) == _contagem(antes, "needs_review", True) - 1
    assert _contagem(depois, "task_type", None) == _contagem(antes, "task_type", None)


def test_editar_o_texto_muda_stats_e_a_faceta_de_edicao(cliente: TestClient) -> None:
    antes_stats = cliente.get("/api/stats").json()
    antes_fac = cliente.get("/api/facets").json()
    uid = cliente.get("/api/prompts").json()["items"][0]["uid"]

    novo = "User: isto agora parece uma conversa colada\nAssistant: parece mesmo"
    assert cliente.patch(f"/api/prompts/{uid}", json={"text": novo}).status_code == 200

    depois_stats = cliente.get("/api/stats").json()
    depois_fac = cliente.get("/api/facets").json()
    assert depois_stats["editados"] == antes_stats["editados"] + 1
    assert depois_stats["com_marcadores_chat"] == antes_stats["com_marcadores_chat"] + 1
    assert _contagem(depois_fac, "edited", True) == _contagem(antes_fac, "edited", True) + 1


def test_reverter_desfaz_tambem_no_cache(cliente: TestClient) -> None:
    uid = cliente.get("/api/prompts").json()["items"][0]["uid"]
    antes = cliente.get("/api/stats").json()["editados"]
    cliente.patch(f"/api/prompts/{uid}", json={"text": "texto novo qualquer"})
    assert cliente.get("/api/stats").json()["editados"] == antes + 1
    assert cliente.post(f"/api/prompts/{uid}/revert").status_code == 200
    assert cliente.get("/api/stats").json()["editados"] == antes


def test_mexer_na_colecao_muda_as_facetas_daquela_colecao(cliente: TestClient) -> None:
    """``collection_id`` é campo de ``Filtros``: a faceta SOB a coleção muda
    quando a lista de membros muda."""
    colecao = cliente.post("/api/collections", json={"name": "curadoria"}).json()
    cid = colecao["id"]
    assert cliente.get("/api/facets", params={"collection_id": cid}).json()["total"] == 0

    uids = [i["uid"] for i in cliente.get("/api/prompts").json()["items"][:3]]
    cliente.post(f"/api/collections/{cid}/items", json={"uids": uids})
    assert cliente.get("/api/facets", params={"collection_id": cid}).json()["total"] == 3

    cliente.post(f"/api/collections/{cid}/items/remove", json={"uids": uids[:1]})
    assert cliente.get("/api/facets", params={"collection_id": cid}).json()["total"] == 2

    cliente.delete(f"/api/collections/{cid}")
    assert cliente.get("/api/facets", params={"collection_id": cid}).json()["total"] == 0


def test_filtros_diferentes_nao_compartilham_entrada(cliente: TestClient) -> None:
    """A chave é o dump inteiro de ``Filtros``. Chave frouxa aqui = a contagem de
    um recorte aparecendo na tela de outro."""
    pt = cliente.get("/api/facets", params={"lang": "pt"}).json()
    en = cliente.get("/api/facets", params={"lang": "en"}).json()
    assert pt["total"] != en["total"]
    assert cliente.get("/api/facets", params={"lang": "pt"}).json() == pt
    assert cliente.get("/api/facets", params={"lang": "en"}).json() == en


def test_trocar_o_banco_por_swap_derruba_o_cache(cliente: TestClient) -> None:
    """``pf load-db`` constrói o SQLite ao lado e faz ``os.replace``. O servidor
    continua de pé, e um cache que sobrevive a isso serve a contagem de um corpus
    que não existe mais. A guarda é o ``db_build_id`` do ``app_meta``."""
    antes = cliente.get("/api/stats").json()
    cache = cliente.app.state.cache
    assert len(cache) > 0

    # Simula a troca: mesmo arquivo, outro build_id, uma linha a menos.
    conn = dbmod.connect(cliente.app.state.db_file)
    try:
        conn.execute("DELETE FROM prompts WHERE id = (SELECT max(id) FROM prompts)")
        dbmod.set_meta(conn, CHAVE_BUILD, "outro-build-000")
    finally:
        conn.close()

    depois = cliente.get("/api/stats").json()
    assert depois["total"] == antes["total"] - 1


def test_escrita_de_fora_nao_e_detectada_e_isso_esta_documentado(
    cliente: TestClient,
) -> None:
    """A guarda cobre escrita PELA app e troca de arquivo, não um segundo
    processo escrevendo no mesmo banco. Este teste existe para a limitação ser
    uma decisão registrada, e não uma surpresa em produção."""
    antes = cliente.get("/api/stats").json()["editados"]
    conn = dbmod.connect(cliente.app.state.db_file)
    try:
        conn.execute("UPDATE prompts SET edited = 1 WHERE id = (SELECT min(id) FROM prompts)")
    finally:
        conn.close()

    assert cliente.get("/api/stats").json()["editados"] == antes  # serve o valor velho
    cliente.app.state.cache.invalidar()
    assert cliente.get("/api/stats").json()["editados"] == antes + 1


def test_dois_apps_no_mesmo_processo_nao_trocam_contagem(tmp_path: Path) -> None:
    montar_banco(tmp_path / "a.sqlite")
    montar_banco(tmp_path / "b.sqlite")
    conn = dbmod.connect(tmp_path / "b.sqlite")
    try:
        conn.execute("DELETE FROM prompts WHERE id = (SELECT max(id) FROM prompts)")
    finally:
        conn.close()

    with TestClient(criar_app(tmp_path / "a.sqlite", tmp_path / "x")) as a:
        with TestClient(criar_app(tmp_path / "b.sqlite", tmp_path / "y")) as b:
            assert a.app.state.cache is not b.app.state.cache
            assert a.get("/api/stats").json()["total"] != b.get("/api/stats").json()["total"]


# ---------------------------------------------------------------------------
# 5. o cache por dentro
# ---------------------------------------------------------------------------


@pytest.fixture
def conn_meta(tmp_path: Path):
    conn = dbmod.connect(tmp_path / "c.sqlite")
    dbmod.init_db(conn)
    dbmod.set_meta(conn, CHAVE_BUILD, "build-1")
    try:
        yield conn
    finally:
        conn.close()


def test_lru_respeita_o_teto(conn_meta: sqlite3.Connection) -> None:
    cache = CacheAgregados(max_entradas=2)
    for i in range(3):
        cache.obter(conn_meta, ("k", i), lambda i=i: i)
    assert len(cache) == 2


def test_teto_zero_desliga_o_cache(conn_meta: sqlite3.Connection) -> None:
    """``cache_max_entradas = 0`` é como medir o custo real de uma consulta sem
    mexer no código."""
    cache = CacheAgregados(max_entradas=0)
    chamadas = []
    for _ in range(3):
        cache.obter(conn_meta, ("k",), lambda: chamadas.append(1))
    assert len(chamadas) == 3
    assert len(cache) == 0


def test_escrita_durante_o_calculo_nao_entra_no_cache(
    conn_meta: sqlite3.Connection,
) -> None:
    """A corrida que faria o cache mentir para sempre: o SELECT já rodou sobre o
    estado velho quando chega a escrita. A resposta desta requisição sai como
    está (era válida quando o SELECT rodou), mas guardá-la fixaria o valor velho
    e nenhuma invalidação futura o alcançaria."""
    cache = CacheAgregados(max_entradas=8)

    def calcular() -> str:
        cache.invalidar()  # a escrita concorrente
        return "velho"

    assert cache.obter(conn_meta, ("k",), calcular) == "velho"
    assert len(cache) == 0
    assert cache.obter(conn_meta, ("k",), lambda: "novo") == "novo"


def test_build_id_ausente_nao_derruba_o_cache_a_cada_leitura(
    tmp_path: Path,
) -> None:
    """Banco sem ``db_build_id`` (um ``init_db`` cru) tem de cachear igual: o
    ``None`` é um valor estável, não uma invalidação por leitura."""
    conn = dbmod.connect(tmp_path / "d.sqlite")
    dbmod.init_db(conn)
    try:
        cache = CacheAgregados(max_entradas=8)
        cache.obter(conn, ("k",), lambda: 1)
        cache.obter(conn, ("k",), lambda: 2)
        assert cache.acertos == 1
    finally:
        conn.close()


def test_ruff_nao_deixa_o_sql_do_prefixo_virar_string_solta() -> None:
    """As colunas do ``GROUP BY`` e as do ``SELECT`` têm de ser as MESMAS, na
    mesma ordem — o SQLite só dispensa a B-tree quando coincidem."""
    sql, _ = queries.sql_facetas_prefixo(Filtros())
    achado = re.search(r"SELECT (.+?), count\(\*\) AS n .*GROUP BY (.+)$", sql, re.S)
    assert achado is not None
    assert achado.group(1).strip() == achado.group(2).strip()
