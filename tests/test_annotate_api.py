"""Testes da API da Bancada (P1) sobre DOIS bancos sintéticos em ``tmp_path``.

O corpus é o MESMO banco hostil de ``test_api.py`` — reaproveitá-lo não é
economia, é o ponto: a plataforma lê o corpus de verdade, com prompt de 200 mil
caracteres, ``<script>`` embutido, template de robô com 120 duplicatas e as três
formas de ``nsfw``. Se o pool ou a saúde quebrarem com isso, quebram no corpus
real também.

O teste que carrega mais peso aqui é ``test_a_conexao_do_corpus_e_readonly``: a
app de curadoria assume ser a única escritora do ``prompts.sqlite``, e é essa
suposição que autoriza o cache de agregados dela. Uma segunda app escrevendo por
baixo transformaria contagens certas em contagens plausíveis e erradas — o pior
defeito possível numa ferramenta de curadoria. A garantia não é disciplina
nossa: é o ``mode=ro`` do SQLite, e este teste prova que ele está ligado.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from prompt_factory import db as dbmod
from prompt_factory.annotate import db as adb
from prompt_factory.annotate import pool as poolmod
from prompt_factory.annotate import seed as seedmod
from prompt_factory.annotate.deps import get_conn_corpus
from prompt_factory.annotate.main import criar_app

from .test_api import montar_banco  # o mesmo banco sintético hostil


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    caminho = tmp_path / "prompts.sqlite"
    montar_banco(caminho)
    return caminho


@pytest.fixture
def cliente(tmp_path: Path, corpus: Path):
    """App com os dois bancos. O ``with`` roda o lifespan (que cria o annotate)."""
    with TestClient(criar_app(tmp_path / "annotate.sqlite", corpus)) as c:
        yield c


def saude(cliente: TestClient) -> dict[str, Any]:
    r = cliente.get("/api/health")
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# partida
# ---------------------------------------------------------------------------


def test_app_recusa_subir_sem_corpus(tmp_path: Path) -> None:
    """Servidor que sobe e dá 500 em tudo é pior que servidor que não sobe.

    E a mensagem tem de trazer o conserto — é a mesma linha do ``pf serve``.
    """
    with pytest.raises(RuntimeError, match="pf load-db"):
        with TestClient(criar_app(tmp_path / "a.sqlite", tmp_path / "nao-existe.sqlite")):
            pass


def test_o_banco_da_plataforma_nasce_sozinho(tmp_path: Path, corpus: Path) -> None:
    """O oposto da app de curadoria, de propósito: esta app é a DONA deste banco.

    A curadoria se recusa a criar o ``prompts.sqlite`` porque quem o constrói é
    a pipeline. Aqui ninguém mais o produz — exigir um comando antes de abrir a
    plataforma seria fricção sem contrapartida.
    """
    alvo = tmp_path / "sub" / "annotate.sqlite"
    assert not alvo.exists()
    with TestClient(criar_app(alvo, corpus)) as c:
        assert alvo.is_file()
        assert saude(c)["anotacao"]["schema_version"] == adb.SCHEMA_VERSION_ANOTACAO
    # E subir de novo sobre o banco existente é um no-op.
    with TestClient(criar_app(alvo, corpus)) as c:
        assert saude(c)["status"] == "ok"


def test_app_recusa_banco_de_outro_schema(tmp_path: Path, corpus: Path) -> None:
    """Este banco guarda trabalho humano e não se recria a partir da pipeline:
    a única resposta honesta a um schema desconhecido é recusar, nunca apagar."""
    alvo = tmp_path / "annotate.sqlite"
    conn = dbmod.connect(alvo)
    adb.init_db(conn)
    adb.set_meta(conn, adb.CHAVE_VERSAO, "99")
    conn.close()
    with pytest.raises(RuntimeError, match="trabalho humano"):
        with TestClient(criar_app(alvo, corpus)):
            pass


def test_estaticos_nao_engolem_a_api(cliente: TestClient) -> None:
    """O mount em "/" é registrado POR ÚLTIMO: se viesse antes dos routers, o
    /api/health acima já teria devolvido o HTML."""
    assert saude(cliente)["app"] == "Bancada"
    pagina = cliente.get("/")
    assert pagina.status_code == 200
    assert "Bancada" in pagina.text


# ---------------------------------------------------------------------------
# saúde: os dois bancos
# ---------------------------------------------------------------------------


def test_health_fala_dos_dois_bancos(cliente: TestClient, corpus: Path) -> None:
    s = saude(cliente)
    assert s["status"] == "ok"
    assert s["corpus"]["arquivo"] == str(corpus)
    assert s["corpus"]["somente_leitura"] is True
    assert s["corpus"]["n_prompts"] > 0
    assert s["corpus"]["db_build_id"] == "0123456789abcdef"
    assert s["anotacao"]["contagens"]["anotadores"] == 0
    assert set(s["anotacao"]["contagens"]) == set(adb.TABELAS)


def test_health_le_o_corpus_ao_vivo(cliente: TestClient, corpus: Path) -> None:
    """Nada do corpus é congelado no lifespan: ele troca por swap embaixo da app.

    Aqui o teste escreve no corpus POR FORA (como o ``pf load-db`` faria) e
    exige que o próximo health já diga o número novo. Um valor guardado na
    subida passaria a mentir a partir deste ponto, sem erro nenhum.
    """
    antes = saude(cliente)["corpus"]["n_prompts"]
    conn = dbmod.connect(corpus)
    try:
        conn.execute(
            "INSERT INTO prompts (uid, text, lang, source, license, hash_norm, n_chars, n_words) "
            "VALUES ('novodepois0001', 'linha nova entrou depois da subida', 'pt', "
            "'aya', 'apache-2.0', 'deadbeef', 34, 6)"
        )
    finally:
        conn.close()
    assert saude(cliente)["corpus"]["n_prompts"] == antes + 1


# ---------------------------------------------------------------------------
# o corpus é somente leitura — a garantia central desta app
# ---------------------------------------------------------------------------


def test_a_conexao_do_corpus_e_readonly(cliente: TestClient) -> None:
    """Escrever pela dependência do corpus levanta ``OperationalError``.

    O teste usa a dependência REAL (``get_conn_corpus``), com um objeto que só
    precisa expor ``.app.state`` — é tudo o que ela lê do ``Request``. Assim a
    prova é sobre o caminho que as rotas usam, não sobre um ``db.connect`` solto
    montado pelo teste.
    """
    gerador = get_conn_corpus(SimpleNamespace(app=cliente.app))
    conn = next(gerador)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE prompts SET text = 'destruido'")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO collections (name) VALUES ('x')")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DROP TABLE prompts")
        # ... e ler continua funcionando (senão o teste acima seria vácuo).
        assert conn.execute("SELECT count(*) AS n FROM prompts").fetchone()["n"] > 0
    finally:
        gerador.close()


def test_a_conexao_da_plataforma_escreve(cliente: TestClient) -> None:
    """O simétrico do teste acima: sem ele, "tudo é read-only" passaria igual."""
    r = cliente.post("/api/perfis", json={"nome": "Ana", "papel": "anotador"})
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# pool: coleção curada x fallback
# ---------------------------------------------------------------------------


def test_pool_cai_no_fallback_sem_colecao(cliente: TestClient) -> None:
    """O estado real de hoje: ``pf load-db`` reconstrói o banco e APAGA coleções."""
    p = saude(cliente)["pool"]
    assert p["origem"] == poolmod.FALLBACK
    assert p["n_pool"] > 0, "um pool vazio numa demonstração é uma tela morta"
    assert "coleção" in p["motivo"] and p["colecao"] == "anotacao"
    assert "caracteres" in p["filtro_fallback"]


def test_pool_usa_a_colecao_curada_quando_ela_existe(cliente: TestClient, corpus: Path) -> None:
    """E a origem MUDA no health — pool trocando em silêncio seria mentira."""
    conn = dbmod.connect(corpus)
    try:
        cur = conn.execute("INSERT INTO collections (name) VALUES ('anotacao')")
        colecao = int(cur.lastrowid or 0)
        conn.executemany(
            "INSERT INTO collection_items (collection_id, prompt_id) VALUES (?, ?)",
            [(colecao, i) for i in (1, 2, 3)],
        )
    finally:
        conn.close()
    p = saude(cliente)["pool"]
    assert p["origem"] == "colecao:anotacao"
    assert p["n_pool"] == 3


def test_colecao_vazia_vale_como_ausente(cliente: TestClient, corpus: Path) -> None:
    """Uma coleção de zero itens não é uma origem, é uma tela morta — e o motivo
    diz exatamente o que aconteceu, em vez de fingir que a curadoria vale."""
    conn = dbmod.connect(corpus)
    try:
        conn.execute("INSERT INTO collections (name) VALUES ('anotacao')")
    finally:
        conn.close()
    p = saude(cliente)["pool"]
    assert p["origem"] == poolmod.FALLBACK
    assert "vazia" in p["motivo"]


def test_fallback_respeita_o_filtro_e_e_deterministico(corpus: Path) -> None:
    """Os uids do fallback saem sempre na mesma ordem, e todos passam no filtro."""
    conn = dbmod.connect(corpus, readonly=True)
    try:
        primeira = poolmod.resolver(conn, com_uids=True)
        segunda = poolmod.resolver(conn, com_uids=True)
        assert primeira.uids == segunda.uids
        assert len(primeira.uids) == primeira.n_pool
        cfg = poolmod.cfg_pool()
        marcas = ", ".join("?" * len(primeira.uids))
        linhas = conn.execute(
            f"SELECT lang, n_chars, n_exact_dups, nsfw FROM prompts WHERE uid IN ({marcas})",
            primeira.uids,
        ).fetchall()
        assert linhas, "o corpus sintético não produziu pool nenhum"
        for linha in linhas:
            assert linha["lang"] == cfg["lang"]
            assert cfg["min_chars"] <= int(linha["n_chars"]) <= cfg["max_chars"]
            assert int(linha["n_exact_dups"]) <= cfg["max_dups"]
            # O robô com 120 duplicatas e o NSFW explícito ficam de fora.
            assert linha["nsfw"] != 1
    finally:
        conn.close()


def test_contagem_do_pool_bate_com_a_lista(corpus: Path) -> None:
    """O ``n_pool`` do health é contado com LIMIT dentro da subconsulta (0,2 ms).
    Ele PRECISA ser o mesmo número que a lista completa entrega."""
    conn = dbmod.connect(corpus, readonly=True)
    try:
        assert poolmod.resolver(conn).n_pool == len(poolmod.resolver(conn, com_uids=True).uids)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# perfis
# ---------------------------------------------------------------------------


def test_perfis_vazio_no_comeco(cliente: TestClient) -> None:
    assert cliente.get("/api/perfis").json() == {"items": []}


def test_criar_e_listar_perfil(cliente: TestClient) -> None:
    r = cliente.post("/api/perfis", json={"nome": "  Ana  Ribeiro ", "papel": "anotador"})
    assert r.status_code == 201, r.text
    criado = r.json()
    # Espaço nas pontas (e duplicado no meio) some: senão "Ana" e "Ana " seriam
    # duas pessoas que o UNIQUE do banco não veria como iguais.
    assert criado["nome"] == "Ana Ribeiro"
    assert criado["papel"] == "anotador"
    assert criado["ativo"] is True  # booleano de verdade, não 0/1
    assert isinstance(criado["id"], int)

    itens = cliente.get("/api/perfis").json()["items"]
    assert [p["nome"] for p in itens] == ["Ana Ribeiro"]


def test_nome_repetido_devolve_409(cliente: TestClient) -> None:
    """Quem decide é o UNIQUE do DDL — conferir antes com um SELECT seria uma
    corrida, e com duas abas abertas na demonstração é uma corrida que acontece."""
    assert cliente.post("/api/perfis", json={"nome": "Ana", "papel": "anotador"}).status_code == 201
    r = cliente.post("/api/perfis", json={"nome": "Ana", "papel": "revisor"})
    assert r.status_code == 409
    assert "Ana" in r.json()["detail"]


@pytest.mark.parametrize(
    "corpo",
    [
        {"nome": "X", "papel": "chefe"},          # papel fora do vocabulário
        {"nome": "", "papel": "admin"},           # nome vazio
        {"nome": "   ", "papel": "admin"},        # só espaço
        {"nome": "X"},                            # sem papel
        {"nome": "X", "papel": "admin", "id": 1}, # extra="forbid"
    ],
)
def test_perfil_invalido_e_422(cliente: TestClient, corpo: dict[str, Any]) -> None:
    assert cliente.post("/api/perfis", json=corpo).status_code == 422


def test_perfis_saem_agrupados_por_papel(cliente: TestClient, tmp_path: Path) -> None:
    """A ordem da rota é a ordem em que a tela de entrada agrupa as personas."""
    conn = dbmod.connect(tmp_path / "annotate.sqlite")
    try:
        seedmod.semear_personas(conn)
    finally:
        conn.close()
    itens = cliente.get("/api/perfis").json()["items"]
    assert [p["papel"] for p in itens] == sorted(p["papel"] for p in itens)
    assert len(itens) == len(seedmod.PERSONAS)


# ---------------------------------------------------------------------------
# encerramento: o -shm órfão do corpus
# ---------------------------------------------------------------------------


def test_saida_limpa_o_shm_do_corpus_e_nao_toca_no_wal(tmp_path: Path, corpus: Path) -> None:
    """Uma conexão READ-ONLY em WAL cria o ``-shm`` e não consegue removê-lo ao
    fechar (removê-lo é uma escrita). O arquivo órfão é exatamente o sinal que o
    pré-voo do swap do ``pf load-db`` lê como "alguém está com isto aberto" — e a
    próxima recarga do corpus seria recusada por causa de uma app que já morreu.

    O ``-wal`` NUNCA é tocado: ele pode conter transações commitadas que ainda
    não foram para o arquivo principal.
    """
    shm = corpus.with_name(corpus.name + "-shm")
    wal = corpus.with_name(corpus.name + "-wal")
    wal.write_bytes(b"")  # um -wal qualquer, para provar que ninguém encosta nele

    with TestClient(criar_app(tmp_path / "annotate.sqlite", corpus)) as c:
        saude(c)
        assert shm.is_file(), "a leitura do corpus deveria ter criado o -shm"
    assert not shm.exists(), "o -shm órfão ficou para trás e vai travar o próximo load-db"
    assert wal.exists(), "o -wal do corpus não pode ser removido por esta app"


def test_saida_nao_deixa_wal_da_plataforma(tmp_path: Path, corpus: Path) -> None:
    """``wal_checkpoint(TRUNCATE)``: um Ctrl+C não deixa -wal/-shm ao lado."""
    alvo = tmp_path / "annotate.sqlite"
    with TestClient(criar_app(alvo, corpus)) as c:
        c.post("/api/perfis", json={"nome": "Ana", "papel": "anotador"})
    assert alvo.is_file()
    assert not alvo.with_name(alvo.name + "-wal").exists()
