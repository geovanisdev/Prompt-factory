"""P3a: as cinco correções, o schema v2 com a migração, e a triagem.

Cada família de teste aqui existe por um defeito **medido contra os bancos
reais**, não por simetria:

* **licença** — trocar ``pool_fallback_lang`` para ``"en"`` trazia 9.148 linhas
  ``cc-by-nc-4.0`` (``commercial_ok = 0``) para dentro do trabalho de anotação,
  sem aviso. O teste troca o idioma e CONTA;
* **seed monofonte** — 16 das 17 tarefas do corpus eram ``arena140k``, porque o
  seed pegava ``uids[:8]`` de uma lista ordenada por id num universo gravado
  agrupado por fonte;
* **migração** — o banco real já guarda 6 anotações, e a v1 recusa subir na v2.
  Migrar tem de preservar tudo, e o schema migrado tem de ser **idêntico** ao de
  um banco novo (senão os CHECKs velhos sobrevivem e nada parece quebrado);
* **o ciclo** — devolver → corrigir → aprovar grava ``versao = 2``;
* **quem revisa não é quem anotou** — sem isso, a taxa de aprovação do painel
  do admin não mede coisa nenhuma.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from prompt_factory import config
from prompt_factory import db as dbmod
from prompt_factory.annotate import db as adb
from prompt_factory.annotate import diretrizes as dirmod
from prompt_factory.annotate import eventos as evmod
from prompt_factory.annotate import migracao
from prompt_factory.annotate import pool as poolmod
from prompt_factory.annotate import projetos as projmod
from prompt_factory.annotate import seed as seedmod
from prompt_factory.annotate.main import criar_app

from .fixtures_annotate_v1 import init_v1
from .test_annotate_p2 import notas_da_rubrica, rubrica_ok
from .test_api import montar_banco


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    caminho = tmp_path / "prompts.sqlite"
    montar_banco(caminho)
    return caminho


@pytest.fixture
def banco(tmp_path: Path) -> Path:
    return tmp_path / "annotate.sqlite"


@pytest.fixture
def semeado(banco: Path, corpus: Path) -> Path:
    conn = dbmod.connect(banco)
    corpo = dbmod.connect(corpus, readonly=True)
    try:
        adb.init_db(conn)
        seedmod.semear(conn, corpo)
    finally:
        conn.close()
        corpo.close()
    return banco


@pytest.fixture
def cliente(semeado: Path, corpus: Path):
    with TestClient(criar_app(semeado, corpus)) as c:
        yield c


def com_banco(caminho: Path) -> sqlite3.Connection:
    conn = dbmod.connect(caminho)
    conn.row_factory = sqlite3.Row
    return conn


def perfis_por_papel(cliente: TestClient) -> dict[str, list[dict[str, Any]]]:
    saida: dict[str, list[dict[str, Any]]] = {}
    for p in cliente.get("/api/perfis").json()["items"]:
        saida.setdefault(p["papel"], []).append(p)
    return saida


# ---------------------------------------------------------------------------
# 1.1 LICENÇA: o pool não entrega o que o export exclui
# ---------------------------------------------------------------------------


def _licencas_do_pool(conn_corpus: sqlite3.Connection) -> dict[str, int]:
    uids = poolmod.resolver(conn_corpus, com_uids=True).uids
    if not uids:
        return {}
    marcas = ", ".join("?" * len(uids))
    return {
        str(linha["license"]): int(linha["n"])
        for linha in conn_corpus.execute(
            f"SELECT license, count(*) AS n FROM prompts WHERE uid IN ({marcas}) "
            "GROUP BY license",
            uids,
        )
    }


def test_pool_em_ingles_nao_traz_cc_by_nc(corpus: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A prova do defeito 1.1, do jeito que ele foi medido: **trocando o idioma**.

    O corpus sintético tem uma linha ``cc-by-nc-4.0`` com ``commercial_ok = 0``
    em inglês (``naocomercial0001``), do mesmo jeito que o real tem 9.148. Com o
    filtro em ``pt`` ela nem seria candidata — o defeito só aparecia ao mexer
    numa chave que parece não ter nada a ver com licença. É exatamente por isso
    que a política precisa estar no filtro, e não na cabeça de quem configura.
    """
    monkeypatch.setitem(config.settings()["annotate"], "pool_fallback_lang", "en")
    # O piso de caracteres é afrouxado para que a ÚNICA diferença entre os dois
    # lados do teste seja a política de licença. Com o piso de 40, a linha
    # cc-by-nc do corpus sintético (32 caracteres) sairia pelo tamanho, e o
    # teste passaria sem provar nada.
    monkeypatch.setitem(config.settings()["annotate"], "pool_fallback_min_chars", 10)
    conn = dbmod.connect(corpus, readonly=True)
    try:
        # Com a política LIGADA (o default): a licença não comercial fica fora.
        com_politica = _licencas_do_pool(conn)
        assert com_politica, "o pool em inglês não pode sair vazio — o teste perderia o sentido"
        assert com_politica.get("cc-by-nc-4.0", 0) == 0
        # E a política é MESMO o que a exclui: desligada, ela entra.
        monkeypatch.setitem(config.settings()["annotate"], "pool_exigir_commercial_ok", False)
        sem_politica = _licencas_do_pool(conn)
        assert sem_politica.get("cc-by-nc-4.0", 0) >= 1
        assert sum(sem_politica.values()) > sum(com_politica.values())
    finally:
        conn.close()


def test_pool_nao_traz_o_que_o_export_exclui(corpus: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``redistributable = 0`` fica fora — a MESMA regra do ``export.py``.

    O corpus tem ``lmsysbloqueado01``, a linha de fonte que proíbe
    redistribuição. Ela está em ``pt``, então cai no filtro padrão: se a
    política não valesse, ela entraria no trabalho de anotação por baixo.
    """
    conn = dbmod.connect(corpus, readonly=True)
    try:
        uids = set(poolmod.resolver(conn, com_uids=True).uids)
        assert "lmsysbloqueado01" not in uids
        monkeypatch.setitem(config.settings()["annotate"], "pool_exigir_redistributable", False)
        monkeypatch.setitem(config.settings()["annotate"], "pool_exigir_commercial_ok", False)
        assert "lmsysbloqueado01" in set(poolmod.resolver(conn, com_uids=True).uids)
    finally:
        conn.close()


def test_a_politica_e_o_numero_dela_aparecem_no_health(cliente: TestClient) -> None:
    """Política sem número é promessa; com número, é medida."""
    p = cliente.get("/api/health").json()["pool"]
    assert p["politica_licenca"]["commercial_ok"] is True
    assert p["politica_licenca"]["redistributable"] is True
    assert p["politica_licenca"]["fechada"] is True
    assert p["excluidas_por_licenca"] >= 1  # o lmsys do corpus sintético
    assert "commercial_ok = 1" in p["politica_licenca"]["descricao"]


def test_a_politica_tambem_vale_para_a_colecao_curada(
    corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Um humano curando na outra ferramenta também pode escolher uma cc-by-nc.

    A regra é do PRODUTO, não do fallback: se ela valesse só no filtro
    automático, bastaria alguém montar a coleção para furá-la sem perceber.
    """
    monkeypatch.setitem(config.settings()["annotate"], "pool_fallback_lang", "en")
    conn = dbmod.connect(corpus)
    try:
        conn.execute("INSERT INTO collections (name) VALUES ('anotacao')")
        cid = int(conn.execute("SELECT id FROM collections WHERE name='anotacao'").fetchone()["id"])
        conn.executemany(
            "INSERT INTO collection_items (collection_id, prompt_id) "
            "SELECT ?, id FROM prompts WHERE uid = ?",
            [(cid, "naocomercial0001"), (cid, "soemoji000000001"), (cid, "nsfwzero00000001")],
        )
    finally:
        conn.close()
    ro = dbmod.connect(corpus, readonly=True)
    try:
        p = poolmod.resolver(ro, com_uids=True)
        assert p.origem == "colecao:anotacao"
        assert "naocomercial0001" not in p.uids
        assert p.excluidas_licenca == 1
        assert "licença" in p.motivo
    finally:
        ro.close()


# ---------------------------------------------------------------------------
# 1.2 NSFW: a garantia é honesta, não vazia
# ---------------------------------------------------------------------------


def test_a_tela_diz_a_verdade_sobre_nsfw(cliente: TestClient) -> None:
    """A cláusula ``nsfw IS NOT 1`` fica; a PROMESSA muda.

    Medido no corpus real: ``nsfw`` é nulo nas 159.733 linhas, então a cláusula
    exclui zero. Dizer "sem NSFW" seria vender uma garantia que o dado não
    sustenta — e este projeto prefere a frase constrangedora à frase falsa.
    """
    p = cliente.get("/api/health").json()["pool"]
    assert "campanha de rotulagem" in p["nota_nsfw"]
    assert "zero linhas" in p["nota_nsfw"]
    assert "sem NSFW" not in p["filtro_fallback"]


def test_a_clausula_de_nsfw_continua_valendo_quando_ha_rotulo(corpus: Path) -> None:
    """O filtro não foi removido: no dia em que a rotulagem rodar, ele age.

    O corpus sintético tem as três situações (NULL, 0 e 1), que é justamente o
    que o corpus real vai ter depois do M6.
    """
    conn = dbmod.connect(corpus, readonly=True)
    try:
        uids = set(poolmod.resolver(conn, com_uids=True).uids)
    finally:
        conn.close()
    assert "nsfwum0000000001" not in uids  # nsfw = 1 -> fora


# ---------------------------------------------------------------------------
# 1.3 SEED: o passo constante, e não a fatia inicial
# ---------------------------------------------------------------------------


def test_fatias_por_passo_espalham_pelo_pool_inteiro() -> None:
    """A função sozinha: nada de ``[:8]`` e ``[8:16]``.

    Com um pool de 100 e 4 por tipo, as fatias têm de tocar o fim da lista —
    era exatamente isso que a versão antiga não fazia.
    """
    uids = [f"u{i:03d}" for i in range(100)]
    a, b = seedmod.fatias_por_passo(uids, 4)
    assert len(a) == len(b) == 4
    assert not set(a) & set(b), "as fatias precisam ser disjuntas"
    juntas = set(a) | set(b)
    assert max(uids.index(u) for u in juntas) >= 80
    assert min(uids.index(u) for u in juntas) <= 20
    # Determinístico: mesma lista, mesmas fatias.
    assert (a, b) == seedmod.fatias_por_passo(uids, 4)


def test_fatias_com_pool_menor_que_o_pedido() -> None:
    """Pool pequeno (teste, ou corpus minúsculo) não pode explodir nem repetir."""
    assert seedmod.fatias_por_passo([], 8) == ([], [])
    assert seedmod.fatias_por_passo(["a"], 8) == (["a"], [])
    a, b = seedmod.fatias_por_passo(["a", "b", "c"], 8)
    assert not set(a) & set(b)
    assert len(a) + len(b) == 3


def test_as_tarefas_do_corpus_vem_de_pelo_menos_tres_fontes(
    semeado: Path, corpus: Path
) -> None:
    """O aceite do P3a. Medido antes: 16 das 17 eram ``arena140k``.

    Um pool monofonte faz a plataforma mentir sobre o corpus que ela representa
    logo na primeira tela que um avaliador abre.
    """
    conn = com_banco(semeado)
    ro = dbmod.connect(corpus, readonly=True)
    try:
        uids = [
            str(linha["prompt_uid"])
            for linha in conn.execute(
                "SELECT DISTINCT prompt_uid FROM tarefas WHERE prompt_uid NOT LIKE 'demo:%'"
            )
        ]
        assert uids, "o seed do pool não gerou tarefa nenhuma"
        marcas = ", ".join("?" * len(uids))
        fontes = {
            str(linha["source"])
            for linha in ro.execute(
                f"SELECT DISTINCT source FROM prompts WHERE uid IN ({marcas})", uids
            )
        }
    finally:
        conn.close()
        ro.close()
    assert len(fontes) >= 3, f"tarefas do corpus vindas de {len(fontes)} fonte(s): {fontes}"


# ---------------------------------------------------------------------------
# 1.4 POOL MATERIALIZADO
# ---------------------------------------------------------------------------


def test_o_pool_e_materializado_e_a_tabela_bate_com_o_health(cliente: TestClient) -> None:
    saude = cliente.get("/api/health").json()
    assert saude["pool"]["n_pool"] > 0
    assert saude["pool"]["materializado_em"]
    assert saude["pool"]["build_do_pool"] == "0123456789abcdef"


def test_a_assinatura_invalida_o_pool_quando_a_config_muda(
    semeado: Path, corpus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mexer no ``settings.toml`` refaz o pool. Sem isso, ajustar a chave não
    mudaria nada e pareceria que ela não funciona."""
    conn = com_banco(semeado)
    ro = dbmod.connect(corpus, readonly=True)
    try:
        primeiro = poolmod.uids(conn, ro)
        assin1 = adb.get_meta(conn, poolmod.CHAVE_ASSINATURA)
        monkeypatch.setitem(config.settings()["annotate"], "pool_fallback_lang", "en")
        segundo = poolmod.uids(conn, ro)
        assin2 = adb.get_meta(conn, poolmod.CHAVE_ASSINATURA)
        assert assin1 != assin2
        assert set(primeiro) != set(segundo)
        # E a tabela é REESCRITA, não acrescida: nada do pool velho sobra.
        assert len(segundo) == int(
            conn.execute("SELECT count(*) AS n FROM pool").fetchone()["n"]
        )
    finally:
        conn.close()
        ro.close()


def test_a_assinatura_invalida_o_pool_quando_o_corpus_troca(
    semeado: Path, corpus: Path
) -> None:
    """``pf load-db`` troca o ``db_build_id`` — e é assim que "o corpus mudou
    embaixo da app" vira uma reconstrução, em vez de um pool que mente."""
    conn = com_banco(semeado)
    ro = dbmod.connect(corpus, readonly=True)
    try:
        poolmod.uids(conn, ro)
        antes = adb.get_meta(conn, poolmod.CHAVE_ASSINATURA)
    finally:
        ro.close()
    escrita = dbmod.connect(corpus)
    try:
        dbmod.set_meta(escrita, "db_build_id", "fedcba9876543210")
    finally:
        escrita.close()
    ro = dbmod.connect(corpus, readonly=True)
    try:
        poolmod.uids(conn, ro)
        assert adb.get_meta(conn, poolmod.CHAVE_ASSINATURA) != antes
        assert adb.get_meta(conn, poolmod.CHAVE_BUILD) == "fedcba9876543210"
    finally:
        conn.close()
        ro.close()


def test_materializar_duas_vezes_nao_refaz_nada(semeado: Path, corpus: Path) -> None:
    """O caminho comum é duas leituras indexadas — e o registro de auditoria
    prova que a reconstrução aconteceu UMA vez."""
    conn = com_banco(semeado)
    ro = dbmod.connect(corpus, readonly=True)
    try:
        poolmod.materializar(conn, ro)
        carimbo = adb.get_meta(conn, poolmod.CHAVE_ATUALIZADO)
        for _ in range(3):
            poolmod.materializar(conn, ro)
        assert adb.get_meta(conn, poolmod.CHAVE_ATUALIZADO) == carimbo
        n = int(
            conn.execute(
                "SELECT count(*) AS n FROM eventos WHERE acao = 'pool_materializado'"
            ).fetchone()["n"]
        )
        assert n == 1
    finally:
        conn.close()
        ro.close()


# ---------------------------------------------------------------------------
# 2. SCHEMA v2 + MIGRAÇÃO
# ---------------------------------------------------------------------------


def _banco_v1_com_trabalho(caminho: Path) -> dict[str, int]:
    """Um banco v1 com trabalho humano dentro — o estado real do disco."""
    conn = dbmod.connect(caminho)
    try:
        init_v1(conn)
        conn.executemany(
            "INSERT INTO anotadores (nome, papel) VALUES (?, ?)",
            [("Ana", "anotador"), ("Bruno", "anotador"), ("Diego", "revisor")],
        )
        conn.execute(
            "INSERT INTO prompts_demo (uid, text, lang) VALUES ('demo:abcdef123456', 'oi', 'pt')"
        )
        conn.execute(
            "INSERT INTO tarefas (tipo, prompt_uid, origem) "
            "VALUES ('escrever_rubrica', 'demo:abcdef123456', 'semente')"
        )
        conn.execute(
            "INSERT INTO atribuicoes (tarefa_id, anotador_id, status) VALUES (1, 1, 'submetida')"
        )
        conn.execute(
            "INSERT INTO tarefas (tipo, prompt_uid, origem) "
            "VALUES ('sft_resposta', 'demo:abcdef123456', 'semente')"
        )
        conn.execute(
            "INSERT INTO atribuicoes (tarefa_id, anotador_id, status) VALUES (2, 2, 'aprovada')"
        )
        # Os três status da v1, para que o backfill seja exercitado inteiro.
        for i, (atrib, versao, status) in enumerate(
            [(1, 1, "rejeitada"), (1, 2, "pendente_revisao"), (2, 1, "aprovada")]
        ):
            conn.execute(
                "INSERT INTO anotacoes (atribuicao_id, versao, payload_schema, payload_json, "
                "                       status) VALUES (?, ?, ?, ?, ?)",
                (atrib, versao, "escrever_rubrica@1", json.dumps({"i": i}), status),
            )
        conn.execute(
            "INSERT INTO revisoes (anotacao_id, revisor_id, veredito, comentario) "
            "VALUES (1, 3, 'rejeitada', 'faltou descrever as pontas da escala')"
        )
        conn.execute(
            "INSERT INTO rubricas (prompt_uid, titulo, criterios_json, origem, anotacao_id) "
            "VALUES ('demo:abcdef123456', 'r', '{}', 'anotacao', 3)"
        )
        conn.execute(
            "INSERT INTO respostas_modelo (prompt_uid, rotulo_modelo, texto, origem) "
            "VALUES ('demo:abcdef123456', 'modelo-a', 'resposta', 'fixture')"
        )
        dbmod.set_meta(conn, "algo_antigo", "preservar")
        return {
            "anotacoes": 3,
            "revisoes": 1,
            "tarefas": 2,
            "anotadores": 3,
            "rubricas": 1,
        }
    finally:
        conn.close()


def test_a_v1_recusa_subir_e_diz_o_conserto(banco: Path, corpus: Path) -> None:
    """A guarda do P1 continua valendo — agora com um caminho na mensagem."""
    _banco_v1_com_trabalho(banco)
    with pytest.raises(RuntimeError) as exc:
        with TestClient(criar_app(banco, corpus)):
            pass
    assert "pf annotate migrate" in str(exc.value)
    # E o banco NÃO foi tocado: nem meio-DDL, nem versão reescrita.
    conn = com_banco(banco)
    try:
        assert adb.versao_do_banco(conn) == 1
        tabelas = {
            str(r["name"])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "avaliacoes" not in tabelas, "o DDL da v2 vazou para um banco v1"
    finally:
        conn.close()


def test_a_migracao_preserva_o_trabalho_humano(banco: Path) -> None:
    """O aceite do marco: NADA de trabalho humano se perde, e o backfill acerta."""
    esperado = _banco_v1_com_trabalho(banco)
    rel = migracao.migrar(banco)

    assert rel["de"] == 1 and rel["para"] == adb.SCHEMA_VERSION_ANOTACAO
    assert rel["copiadas"]["anotacoes"] == esperado["anotacoes"]
    assert Path(rel["backup"]).is_file(), "a migração precisa deixar o arquivo antigo ao lado"

    conn = com_banco(banco)
    try:
        assert adb.versao_do_banco(conn) == adb.SCHEMA_VERSION_ANOTACAO
        contagens = adb.contagens(conn)
        for tabela, n in esperado.items():
            assert contagens[tabela] == n, tabela
        # O backfill dos três status da v1.
        status = {
            int(r["id"]): str(r["status"])
            for r in conn.execute("SELECT id, status FROM anotacoes")
        }
        assert status == {1: "devolvida", 2: "pendente_triagem", 3: "pendente_avaliacao"}
        # O veredito da triagem mudou de nome junto.
        assert str(
            conn.execute("SELECT veredito FROM revisoes WHERE id = 1").fetchone()["veredito"]
        ) == "devolvida"
        # As colunas de operação nasceram preenchidas.
        assert all(
            r["projeto_id"] is not None for r in conn.execute("SELECT projeto_id FROM tarefas")
        )
        assert all(
            int(r["versao_diretriz"]) == migracao.VERSAO_DIRETRIZ_HERDADA
            for r in conn.execute("SELECT versao_diretriz FROM anotacoes")
        )
        assert contagens["diretrizes"] == len(adb.TIPOS_TAREFA)
        # DOIS projetos: fixture e trabalho real não moram na mesma lista.
        assert contagens["projetos"] == 2
        nomes = {
            str(r["nome"]): int(r["id"]) for r in conn.execute("SELECT id, nome FROM projetos")
        }
        assert set(nomes) == {projmod.nome_padrao(), projmod.nome_demonstracao()}
        # E a ALOCAÇÃO segue o prefixo do uid, que é o contrato do resolvedor:
        # as duas tarefas do banco v1 são sobre um prompt `demo:`.
        por_projeto = {
            int(r["projeto_id"]): int(r["n"])
            for r in conn.execute(
                "SELECT projeto_id, count(*) AS n FROM tarefas GROUP BY projeto_id"
            )
        }
        assert por_projeto == {nomes[projmod.nome_demonstracao()]: esperado["tarefas"]}
        # A autorrevisão das revisões antigas é derivada UMA vez (aqui, o
        # revisor não era o autor).
        assert int(
            conn.execute("SELECT autorrevisao FROM revisoes WHERE id = 1").fetchone()[
                "autorrevisao"
            ]
        ) == 0
        # O payload continua legível, byte a byte.
        payloads = [
            json.loads(str(r["payload_json"]))
            for r in conn.execute("SELECT payload_json FROM anotacoes ORDER BY id")
        ]
        assert payloads == [{"i": 0}, {"i": 1}, {"i": 2}]
        # E as chaves de app_meta que não eram da versão sobreviveram.
        assert adb.get_meta(conn, "algo_antigo") == "preservar"
        # A própria migração fica na trilha de auditoria, com o que ela salvou.
        evento = conn.execute(
            "SELECT detalhe_json FROM eventos WHERE acao = 'banco_migrado'"
        ).fetchone()
        assert evento is not None
        detalhe = json.loads(str(evento["detalhe_json"]))
        assert detalhe["de"] == 1 and detalhe["para"] == adb.SCHEMA_VERSION_ANOTACAO
        assert detalhe["anotacoes_preservadas"] == esperado["anotacoes"]
    finally:
        conn.close()


def test_o_schema_migrado_e_identico_ao_de_um_banco_novo(tmp_path: Path) -> None:
    """A propriedade que só a reconstrução ao lado dá.

    Um ``ALTER TABLE`` deixaria os CHECKs velhos vivos e as colunas em outra
    ordem — um meio-schema que passa em todo teste de existência e falha na
    primeira transição de status. Comparar os dois ``sqlite_master`` é a única
    forma de provar que isso não aconteceu.
    """
    velho = tmp_path / "migrado.sqlite"
    _banco_v1_com_trabalho(velho)
    migracao.migrar(velho)

    novo = tmp_path / "novo.sqlite"
    c = dbmod.connect(novo)
    try:
        adb.init_db(c)
    finally:
        c.close()

    def schema(caminho: Path) -> list[tuple[str, str]]:
        conn = dbmod.connect(caminho)
        try:
            return [
                (str(r["name"]), " ".join(str(r["sql"] or "").split()))
                for r in conn.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                )
            ]
        finally:
            conn.close()

    assert schema(velho) == schema(novo)


def test_migrar_duas_vezes_e_no_op(banco: Path) -> None:
    _banco_v1_com_trabalho(banco)
    migracao.migrar(banco)
    rel = migracao.migrar(banco)
    assert rel["ja_estava"] is True


def test_migracao_recusa_status_desconhecido(banco: Path) -> None:
    """Migrar "quase tudo" é o desfecho que este módulo existe para impedir."""
    _banco_v1_com_trabalho(banco)
    conn = dbmod.connect(banco)
    try:
        # Contorna o CHECK da v1 para simular um banco adulterado à mão.
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute(
            "UPDATE sqlite_master SET sql = replace(sql, "
            "'''pendente_revisao'', ''aprovada'', ''rejeitada''', "
            "'''pendente_revisao'', ''aprovada'', ''rejeitada'', ''inventado''') "
            "WHERE name = 'anotacoes'"
        )
        conn.execute("PRAGMA writable_schema=OFF")
    finally:
        conn.close()
    conn = dbmod.connect(banco)
    try:
        conn.execute("UPDATE anotacoes SET status = 'inventado' WHERE id = 2")
    finally:
        conn.close()

    with pytest.raises(migracao.MigracaoImpossivel) as exc:
        migracao.migrar(banco)
    assert "inventado" in str(exc.value)
    # E nada foi alterado.
    conn = com_banco(banco)
    try:
        assert adb.versao_do_banco(conn) == 1
        assert int(conn.execute("SELECT count(*) AS n FROM anotacoes").fetchone()["n"]) == 3
    finally:
        conn.close()


def test_a_maquina_de_status_esta_no_codigo_como_dado() -> None:
    """``TRANSICOES`` é a máquina do plano, e as rotas consultam DELA."""
    assert set(adb.TRANSICOES) == set(adb.STATUS_ANOTACAO)
    for destinos in adb.TRANSICOES.values():
        assert set(destinos) <= set(adb.STATUS_ANOTACAO)
    assert adb.TRANSICOES["pendente_triagem"] == ("devolvida", "pendente_avaliacao")
    assert adb.TRANSICOES["devolvida"] == ("pendente_triagem",)
    # Terminais não saem de si mesmos.
    assert adb.TRANSICOES["avaliada"] == ()
    assert adb.TRANSICOES["descartada"] == ()
    # `incorrigivel` NÃO volta ao anotador: só a triagem devolve.
    assert "devolvida" not in adb.TRANSICOES["pendente_avaliacao"]


# ---------------------------------------------------------------------------
# 2b. as colunas de operação
# ---------------------------------------------------------------------------


def test_a_fixture_e_o_trabalho_real_ficam_em_projetos_diferentes(semeado: Path) -> None:
    """O que dá valor ao portfólio é a distinção entre o que é fixture e o que
    é autoria. Misturar os dois na mesma lista apagaria justamente isso."""
    conn = com_banco(semeado)
    try:
        assert int(
            conn.execute(
                "SELECT count(*) AS n FROM tarefas WHERE projeto_id IS NULL"
            ).fetchone()["n"]
        ) == 0
        nomes = {
            int(r["id"]): str(r["nome"]) for r in conn.execute("SELECT id, nome FROM projetos")
        }
        assert set(nomes.values()) == {projmod.nome_padrao(), projmod.nome_demonstracao()}
        por_projeto: dict[str, set[str]] = {}
        for r in conn.execute("SELECT projeto_id, prompt_uid FROM tarefas"):
            por_projeto.setdefault(nomes[int(r["projeto_id"])], set()).add(str(r["prompt_uid"]))
        # Demonstração: só uids do pacote. Portfólio: só uids do corpus.
        assert all(u.startswith("demo:") for u in por_projeto[projmod.nome_demonstracao()])
        assert not any(u.startswith("demo:") for u in por_projeto[projmod.nome_padrao()])
        assert por_projeto[projmod.nome_padrao()], "o portfólio nasceu vazio"
    finally:
        conn.close()


def test_a_fila_e_as_contagens_respeitam_o_projeto(cliente: TestClient) -> None:
    """"Trabalhar só num projeto" tem de valer para a fila E para o contador.

    Um contador que promete 12 e uma fila que entrega 0 é pior que contador
    nenhum: ele faz a tela parecer quebrada justo quando está certa.
    """
    projetos = {p["nome"]: p["id"] for p in cliente.get("/api/projetos").json()["items"]}
    quem = perfis_por_papel(cliente)["anotador"][0]["id"]

    geral = cliente.get(
        "/api/tarefas/contagens", params={"anotador_id": quem}
    ).json()["contagens"]
    demo = cliente.get(
        "/api/tarefas/contagens",
        params={"anotador_id": quem, "projeto_id": projetos[projmod.nome_demonstracao()]},
    ).json()["contagens"]
    port = cliente.get(
        "/api/tarefas/contagens",
        params={"anotador_id": quem, "projeto_id": projetos[projmod.nome_padrao()]},
    ).json()["contagens"]
    for tipo in demo:
        assert demo[tipo] + port[tipo] == geral[tipo], tipo
    # O portfólio não tem tarefa de A/B (o pacote é quem traz as respostas de
    # modelo) e a demonstração não tem as do corpus — a soma acima já prova a
    # partição, e esta linha prova que ela não é trivial.
    assert demo["comparar_ab"] > 0 and port["escrever_rubrica"] > 0

    # E a fila entrega do projeto pedido, não de qualquer um.
    r = cliente.post(
        "/api/tarefas/proxima",
        json={
            "anotador_id": quem,
            "tipo": "escrever_rubrica",
            "projeto_id": projetos[projmod.nome_padrao()],
        },
    ).json()
    assert r["tarefa"] is not None
    assert r["tarefa"]["tarefa"]["projeto"] == projmod.nome_padrao()
    assert not r["tarefa"]["prompt"]["uid"].startswith("demo:")

    # Projeto inexistente é 404, não uma fila silenciosamente vazia.
    assert cliente.post(
        "/api/tarefas/proxima",
        json={"anotador_id": quem, "tipo": "escrever_rubrica", "projeto_id": 9999},
    ).status_code == 404


def test_a_rota_de_projetos_conta_o_trabalho_de_cada_um(cliente: TestClient) -> None:
    """Um seletor que não diz quanto trabalho tem cada projeto obriga a entrar
    em todos para descobrir onde está o que se procura."""
    r = cliente.get("/api/projetos")
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["padrao"] == projmod.nome_padrao()
    assert corpo["demonstracao"] == projmod.nome_demonstracao()
    por_nome = {p["nome"]: p for p in corpo["items"]}
    assert set(por_nome) == {projmod.nome_padrao(), projmod.nome_demonstracao()}
    for p in por_nome.values():
        assert p["n_tarefas"] > 0
        assert p["n_abertas"] <= p["n_tarefas"]
        assert p["descricao"], "projeto sem descrição obriga a adivinhar qual é qual"


def test_a_diretriz_e_versionada_e_a_anotacao_a_carimba(cliente: TestClient) -> None:
    """O cenário do plano: sem isto, separar "antes" de "depois" é impossível."""
    r = cliente.get("/api/diretrizes")
    assert r.status_code == 200
    itens = r.json()["items"]
    assert set(itens) == set(adb.TIPOS_TAREFA)
    assert all(i["versao"] == 1 and i["linhas"] for i in itens.values())

    quem = perfis_por_papel(cliente)["anotador"][0]["id"]
    env = cliente.post(
        "/api/tarefas/proxima", json={"anotador_id": quem, "tipo": "avaliar_rubrica"}
    ).json()["tarefa"]
    envio = cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
        json={"anotador_id": quem, "payload": notas_da_rubrica(env)},
    )
    assert envio.status_code == 200, envio.text
    assert envio.json()["versao_diretriz"] == 1


def test_a_diretriz_ja_usada_nao_se_reescreve(semeado: Path) -> None:
    """Editar uma versão já usada transformaria em mentira todo
    ``versao_diretriz`` que aponta para ela."""
    conn = com_banco(semeado)
    try:
        # Semear de novo o MESMO texto é no-op.
        assert dirmod.semear(conn) == 0
        adulterado = {"versao": 1, "tipos": {t: ["outra regra"] for t in adb.TIPOS_TAREFA}}
        with pytest.raises(RuntimeError) as exc:
            dirmod.semear(conn, adulterado)
        assert "suba a 'versao'" in str(exc.value)
        # Uma versão NOVA, sim: a antiga fica e a vigente passa a ser a nova.
        nova = {"versao": 2, "tipos": {t: ["regra nova"] for t in adb.TIPOS_TAREFA}}
        assert dirmod.semear(conn, nova) == len(adb.TIPOS_TAREFA)
        assert dirmod.versao_vigente(conn, "comparar_ab") == 2
        assert dirmod.vigentes(conn)["comparar_ab"]["linhas"] == ["regra nova"]
    finally:
        conn.close()


def test_as_qualificacoes_sao_escritas_no_seed(cliente: TestClient) -> None:
    """A coluna existe e É escrita; barrar quem não é qualificado vem depois."""
    por_nome = {p["nome"]: p for p in cliente.get("/api/perfis").json()["items"]}
    assert por_nome["Ana Ribeiro"]["qualificacoes"]["comparar_ab"] == "aprovada"
    assert por_nome["Carla Nunes"]["qualificacoes"]["comparar_ab"] == "pendente"
    # Quem entra pela tela nasce sem qualificação — e isso é um dado, não um erro.
    novo = cliente.post("/api/perfis", json={"nome": "Zeca Novo", "papel": "anotador"})
    assert novo.status_code == 201
    assert novo.json()["qualificacoes"] == {}


def test_a_trilha_de_auditoria_registra_as_transicoes(cliente: TestClient, semeado: Path) -> None:
    """Sem os eventos, a auditoria de um item teria de ser remontada por
    inferência — e "por inferência" é o que uma auditoria não pode ser."""
    quem = perfis_por_papel(cliente)["anotador"][0]["id"]
    env = cliente.post(
        "/api/tarefas/proxima", json={"anotador_id": quem, "tipo": "avaliar_rubrica"}
    ).json()["tarefa"]
    cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
        json={"anotador_id": quem, "payload": notas_da_rubrica(env)},
    )
    conn = com_banco(semeado)
    try:
        acoes = [
            str(r["acao"]) for r in conn.execute("SELECT acao FROM eventos ORDER BY id")
        ]
        assert "tarefa_reivindicada" in acoes
        assert "anotacao_submetida" in acoes
        trilha = evmod.da_entidade(conn, "atribuicao", int(env["atribuicao_id"]))
        assert trilha and trilha[0]["acao"] == "tarefa_reivindicada"
        assert trilha[0]["ator"] is not None
        assert trilha[0]["detalhe"]["tipo"] == "avaliar_rubrica"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. A TRIAGEM
# ---------------------------------------------------------------------------


def _anotar(cliente: TestClient, quem: int, tipo: str = "avaliar_rubrica") -> dict[str, Any]:
    """Pega da fila e submete. Devolve ``{atribuicao_id, anotacao_id, env}``."""
    env = cliente.post(
        "/api/tarefas/proxima", json={"anotador_id": quem, "tipo": tipo}
    ).json()["tarefa"]
    assert env is not None, f"fila de {tipo} vazia para {quem}"
    payload = notas_da_rubrica(env) if tipo == "avaliar_rubrica" else rubrica_ok()
    r = cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
        json={"anotador_id": quem, "payload": payload},
    )
    assert r.status_code == 200, r.text
    return {"atribuicao_id": env["atribuicao_id"], "env": env, **r.json()}


def test_a_fila_traz_o_trabalho_dos_outros_e_so_papel_de_revisao_a_abre(
    cliente: TestClient,
) -> None:
    """A fila serve quem revisa, e ninguém mais.

    O anotador não abre a fila do revisor (403 por papel) — se abrisse, "o
    revisor não vê as próprias" seria uma regra que qualquer um contornaria
    trocando o id no seletor da barra.
    """
    papeis = perfis_por_papel(cliente)
    ana, bruno = papeis["anotador"][0]["id"], papeis["anotador"][1]["id"]
    revisor = papeis["revisor"][0]["id"]
    a1 = _anotar(cliente, ana)
    a2 = _anotar(cliente, bruno)

    fila = cliente.get("/api/revisao/fila", params={"revisor_id": revisor}).json()
    ids = {i["anotacao_id"] for i in fila["items"]}
    assert {a1["anotacao_id"], a2["anotacao_id"]} <= ids
    assert fila["minhas_excluidas"] == 0

    assert cliente.get("/api/revisao/fila", params={"revisor_id": ana}).status_code == 403
    assert cliente.get(
        f"/api/revisao/{a1['anotacao_id']}", params={"revisor_id": ana}
    ).status_code == 403


def _virar_autor(banco: Path, atribuicao_id: int, quem: int) -> None:
    """Transfere a autoria de uma atribuição — é o mesmo estado que o banco
    teria se a pessoa tivesse anotado antes de ganhar o papel de revisor."""
    conn = com_banco(banco)
    try:
        conn.execute(
            "UPDATE atribuicoes SET anotador_id = ? WHERE id = ?", (quem, atribuicao_id)
        )
    finally:
        conn.close()


def test_modo_solo_desligado_esconde_as_proprias(cliente: TestClient, semeado: Path) -> None:
    """O DEFAULT: a regra de QC de qualquer operação com equipe.

    Sem ela, uma pessoa revisando o que ela mesma escreveu faz a taxa de
    aprovação do painel do admin deixar de medir qualquer coisa.
    """
    papeis = perfis_por_papel(cliente)
    ana = papeis["anotador"][0]["id"]
    revisor, outro = papeis["revisor"][0]["id"], papeis["revisor"][1]["id"]
    minha = _anotar(cliente, ana)
    _virar_autor(semeado, minha["atribuicao_id"], revisor)

    fila = cliente.get("/api/revisao/fila", params={"revisor_id": revisor}).json()
    assert fila["modo_solo"] is False
    assert minha["anotacao_id"] not in {i["anotacao_id"] for i in fila["items"]}
    assert fila["minhas_excluidas"] == 1

    # O item não sumiu da plataforma, só dos olhos de quem não pode julgá-lo.
    de_outro = cliente.get("/api/revisao/fila", params={"revisor_id": outro}).json()
    assert minha["anotacao_id"] in {i["anotacao_id"] for i in de_outro["items"]}

    # Detalhe e triagem recusam igual — e a recusa ENSINA o caminho.
    r = cliente.get(f"/api/revisao/{minha['anotacao_id']}", params={"revisor_id": revisor})
    assert r.status_code == 403
    assert "permitir_autorrevisao" in r.text
    r = cliente.post(
        f"/api/revisao/{minha['anotacao_id']}",
        json={"revisor_id": revisor, "veredito": "aprovada"},
    )
    assert r.status_code == 403


def test_modo_solo_ligado_permite_e_declara(
    cliente: TestClient, semeado: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O caso de uso do autor sozinho — permitido, **e declarado em três lugares**.

    A regra não é afrouxada em silêncio: ela é substituída por uma que se
    anuncia. A tela mostra a faixa (``modo_solo`` na resposta), o item vem
    marcado (``minha``) e o banco grava ``autorrevisao = 1``. Um QC que se
    esconde não é QC — e é justamente o que este projeto existe para não fazer.
    """
    monkeypatch.setitem(config.settings()["annotate"], "permitir_autorrevisao", True)
    papeis = perfis_por_papel(cliente)
    ana = papeis["anotador"][0]["id"]
    revisor = papeis["revisor"][0]["id"]
    minha = _anotar(cliente, ana)
    de_outrem = _anotar(cliente, papeis["anotador"][1]["id"])
    _virar_autor(semeado, minha["atribuicao_id"], revisor)

    fila = cliente.get("/api/revisao/fila", params={"revisor_id": revisor}).json()
    assert fila["modo_solo"] is True
    por_id = {i["anotacao_id"]: i for i in fila["items"]}
    assert minha["anotacao_id"] in por_id
    # Cada item diz se é MEU: numa fila mista, saber quais são as minhas é o
    # que torna o aviso acionável em vez de decorativo.
    assert por_id[minha["anotacao_id"]]["minha"] is True
    assert por_id[de_outrem["anotacao_id"]]["minha"] is False
    assert fila["minhas_na_fila"] == 1

    det = cliente.get(f"/api/revisao/{minha['anotacao_id']}", params={"revisor_id": revisor})
    assert det.status_code == 200
    assert det.json()["minha"] is True and det.json()["modo_solo"] is True

    r = cliente.post(
        f"/api/revisao/{minha['anotacao_id']}",
        json={"revisor_id": revisor, "veredito": "aprovada"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["autorrevisao"] is True

    # E o BANCO guarda a marca — para o export e o painel poderem declarar o
    # número separado, em vez de somar autorrevisão com revisão cruzada.
    conn = com_banco(semeado)
    try:
        linhas = {
            int(x["anotacao_id"]): int(x["autorrevisao"])
            for x in conn.execute("SELECT anotacao_id, autorrevisao FROM revisoes")
        }
        assert linhas == {minha["anotacao_id"]: 1}
        evento = conn.execute(
            "SELECT detalhe_json FROM eventos WHERE acao = 'triagem_aprovada'"
        ).fetchone()
        assert json.loads(str(evento["detalhe_json"]))["autorrevisao"] is True
    finally:
        conn.close()

    # E revisar o trabalho de OUTRA pessoa continua sendo revisão cruzada.
    r2 = cliente.post(
        f"/api/revisao/{de_outrem['anotacao_id']}",
        json={"revisor_id": revisor, "veredito": "aprovada"},
    )
    assert r2.status_code == 200
    assert r2.json()["autorrevisao"] is False


def test_o_modo_solo_aparece_no_health(
    cliente: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """É do health que a tela monta a faixa. Um modo que muda a regra de QC e
    não aparece seria exatamente a mentira que este projeto não conta."""
    assert cliente.get("/api/health").json()["modo_solo"] is False
    monkeypatch.setitem(config.settings()["annotate"], "permitir_autorrevisao", True)
    assert cliente.get("/api/health").json()["modo_solo"] is True


def test_o_modo_solo_e_uma_pessoa_nos_tres_papeis(
    cliente: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O papel deixa de ser AUTORIZAÇÃO e passa a ser VISTA — e é isso que
    desbloqueia o caso de uso.

    Sem esta metade, afrouxar só a autorrevisão não resolveria nada: ao trocar
    para o papel de revisor, a interface teria de vestir outra persona, e o
    trabalho do autor apareceria revisado por uma fixture. Atribuição falsa num
    portfólio é pior que permissão larga numa app de 127.0.0.1.
    """
    ana = perfis_por_papel(cliente)["anotador"][0]["id"]
    # Fora do modo solo, uma anotadora não abre a fila do revisor.
    assert cliente.get("/api/revisao/fila", params={"revisor_id": ana}).status_code == 403

    monkeypatch.setitem(config.settings()["annotate"], "permitir_autorrevisao", True)
    r = cliente.get("/api/revisao/fila", params={"revisor_id": ana})
    assert r.status_code == 200, r.text
    assert r.json()["modo_solo"] is True
    # E a mesma pessoa continua anotando: a vista muda, a identidade não.
    a = _anotar(cliente, ana)
    assert a["versao"] == 1
    aprovada = cliente.post(
        f"/api/revisao/{a['anotacao_id']}", json={"revisor_id": ana, "veredito": "aprovada"}
    )
    assert aprovada.status_code == 200, aprovada.text
    assert aprovada.json()["autorrevisao"] is True


def test_devolver_exige_comentario(cliente: TestClient) -> None:
    """Três camadas: Pydantic, rota e CHECK. O comentário é a única instrução
    que o anotador recebe sobre o que corrigir."""
    papeis = perfis_por_papel(cliente)
    ana = papeis["anotador"][0]["id"]
    revisor = papeis["revisor"][0]["id"]
    a = _anotar(cliente, ana)

    for corpo in (
        {"revisor_id": revisor, "veredito": "devolvida"},
        {"revisor_id": revisor, "veredito": "devolvida", "comentario": "   "},
        {"revisor_id": revisor, "veredito": "devolvida", "comentario": "refaça"},
    ):
        r = cliente.post(f"/api/revisao/{a['anotacao_id']}", json=corpo)
        assert r.status_code == 422, r.text
    r = cliente.post(
        f"/api/revisao/{a['anotacao_id']}",
        json={"revisor_id": revisor, "veredito": "inventado", "comentario": "x" * 40},
    )
    assert r.status_code == 422


def test_o_detalhe_traz_o_mesmo_envelope_do_anotador(cliente: TestClient) -> None:
    """Reconhecimento em vez de memória: MESMA rubrica, MESMAS escalas."""
    papeis = perfis_por_papel(cliente)
    ana = papeis["anotador"][0]["id"]
    revisor = papeis["revisor"][0]["id"]
    a = _anotar(cliente, ana)

    det = cliente.get(f"/api/revisao/{a['anotacao_id']}", params={"revisor_id": revisor})
    assert det.status_code == 200
    t = det.json()["tarefa"]
    assert t["prompt"]["uid"] == a["env"]["prompt"]["uid"]
    assert t["rubrica"] == a["env"]["rubrica"]
    assert t["respostas"] == a["env"]["respostas"]
    assert t["anotacao"]["payload"]["notas"]
    assert t["anotacao"]["anotador"]
    # E o gabarito continua fora do envelope, inclusive para o revisor.
    assert "gabarito" not in json.dumps(t)


def test_o_ciclo_devolver_corrigir_aprovar_grava_versao_2(
    cliente: TestClient, semeado: Path
) -> None:
    """O ACEITE do marco: o ciclo inteiro, de ponta a ponta.

    Devolver reabre a MESMA atribuição (o ``UNIQUE(tarefa_id, anotador_id)`` é a
    trava anti-repetição — uma segunda atribuição faria o mesmo trabalho contar
    duas vezes em toda métrica), e o ``submeter`` grava ``max + 1`` sozinho.
    """
    papeis = perfis_por_papel(cliente)
    ana = papeis["anotador"][0]["id"]
    revisor = papeis["revisor"][0]["id"]
    a = _anotar(cliente, ana)
    assert a["versao"] == 1

    # 1. devolver
    r = cliente.post(
        f"/api/revisao/{a['anotacao_id']}",
        json={
            "revisor_id": revisor,
            "veredito": "devolvida",
            "comentario": "as notas 5 não conversam com a justificativa; releia o critério 2",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "devolvida"

    # 2. o anotador vê a devolução com o comentário, sem abrir a tarefa
    minhas = cliente.get("/api/atribuicoes", params={"anotador_id": ana}).json()["items"]
    devolvida = next(i for i in minhas if i["atribuicao_id"] == a["atribuicao_id"])
    assert devolvida["status"] == "em_andamento"
    assert devolvida["veredito"] == "devolvida"
    assert "critério 2" in devolvida["comentario_revisor"]

    # 3. o formulário volta preenchido (é o que `formInicial` repopula)
    reaberta = cliente.get(
        f"/api/atribuicoes/{a['atribuicao_id']}", params={"anotador_id": ana}
    ).json()["tarefa"]
    assert reaberta["versao_anterior"]["veredito"] == "devolvida"
    assert reaberta["versao_anterior"]["payload"]["notas"]

    # 4. corrigir e reenviar -> versão 2
    corrigido = notas_da_rubrica(reaberta, nota=3)
    r2 = cliente.post(
        f"/api/atribuicoes/{a['atribuicao_id']}/submeter",
        json={"anotador_id": ana, "payload": corrigido},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["versao"] == 2

    # 5. a v2 volta para a triagem e é aprovada
    fila = cliente.get("/api/revisao/fila", params={"revisor_id": revisor}).json()
    v2 = next(i for i in fila["items"] if i["anotacao_id"] == r2.json()["anotacao_id"])
    assert v2["versao"] == 2
    r3 = cliente.post(
        f"/api/revisao/{v2['anotacao_id']}", json={"revisor_id": revisor, "veredito": "aprovada"}
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["status"] == "pendente_avaliacao"

    conn = com_banco(semeado)
    try:
        # A HISTÓRIA FICA: duas versões na MESMA atribuição, nenhuma apagada.
        linhas = conn.execute(
            "SELECT versao, status FROM anotacoes WHERE atribuicao_id = ? ORDER BY versao",
            (a["atribuicao_id"],),
        ).fetchall()
        assert [(int(x["versao"]), str(x["status"])) for x in linhas] == [
            (1, "devolvida"),
            (2, "pendente_avaliacao"),
        ]
        # Uma atribuição só — a trava anti-repetição não foi contornada.
        assert int(
            conn.execute(
                "SELECT count(*) AS n FROM atribuicoes WHERE tarefa_id = ?",
                (int(a["env"]["tarefa"]["id"]),),
            ).fetchone()["n"]
        ) == 1
        # E duas triagens, uma por versão.
        assert int(conn.execute("SELECT count(*) AS n FROM revisoes").fetchone()["n"]) == 2
        acoes = [str(x["acao"]) for x in conn.execute("SELECT acao FROM eventos ORDER BY id")]
        assert "triagem_devolvida" in acoes and "triagem_aprovada" in acoes
    finally:
        conn.close()


def test_triar_duas_vezes_a_mesma_versao_da_409(cliente: TestClient) -> None:
    """Duas abas do revisor na mesma linha é corrida, não defeito — e a resposta
    certa é dizer que já foi triada."""
    papeis = perfis_por_papel(cliente)
    ana = papeis["anotador"][0]["id"]
    revisor, outro = papeis["revisor"][0]["id"], papeis["revisor"][1]["id"]
    a = _anotar(cliente, ana)
    assert cliente.post(
        f"/api/revisao/{a['anotacao_id']}", json={"revisor_id": revisor, "veredito": "aprovada"}
    ).status_code == 200
    r = cliente.post(
        f"/api/revisao/{a['anotacao_id']}", json={"revisor_id": outro, "veredito": "aprovada"}
    )
    assert r.status_code == 409
    assert "pendente_avaliacao" in r.text or "já" in r.text


def test_aprovar_uma_rubrica_materializa_o_instrumento(cliente: TestClient, semeado: Path) -> None:
    """É o que encadeia as quatro abas: a rubrica aprovada vira o instrumento
    com que OUTRAS pessoas avaliam aquele prompt."""
    papeis = perfis_por_papel(cliente)
    ana = papeis["anotador"][0]["id"]
    revisor = papeis["revisor"][0]["id"]
    a = _anotar(cliente, ana, tipo="escrever_rubrica")
    uid = a["env"]["prompt"]["uid"]

    conn = com_banco(semeado)
    try:
        antes = int(
            conn.execute(
                "SELECT count(*) AS n FROM rubricas WHERE prompt_uid = ?", (uid,)
            ).fetchone()["n"]
        )
    finally:
        conn.close()

    r = cliente.post(
        f"/api/revisao/{a['anotacao_id']}", json={"revisor_id": revisor, "veredito": "aprovada"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["rubrica_id"] is not None

    conn = com_banco(semeado)
    try:
        linha = conn.execute(
            "SELECT titulo, origem, anotacao_id, status FROM rubricas "
            "WHERE prompt_uid = ? ORDER BY id DESC LIMIT 1",
            (uid,),
        ).fetchone()
        assert int(
            conn.execute(
                "SELECT count(*) AS n FROM rubricas WHERE prompt_uid = ?", (uid,)
            ).fetchone()["n"]
        ) == antes + 1
        assert str(linha["origem"]) == "anotacao"
        assert int(linha["anotacao_id"]) == a["anotacao_id"]
        assert str(linha["status"]) == "ativa"
    finally:
        conn.close()


def test_a_fila_da_triagem_e_cronologica(cliente: TestClient) -> None:
    """Quem esperou mais é atendido primeiro. Ordenar por prioridade da tarefa
    deixaria uma submissão de terça atrás de uma de hoje."""
    papeis = perfis_por_papel(cliente)
    ana, bruno = papeis["anotador"][0]["id"], papeis["anotador"][1]["id"]
    revisor = papeis["revisor"][0]["id"]
    ordem = [_anotar(cliente, ana)["anotacao_id"], _anotar(cliente, bruno)["anotacao_id"]]
    fila = cliente.get("/api/revisao/fila", params={"revisor_id": revisor}).json()
    assert [i["anotacao_id"] for i in fila["items"]][: len(ordem)] == ordem
