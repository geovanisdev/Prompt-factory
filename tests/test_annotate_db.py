"""Testes do schema da Bancada (``annotate/db.py``).

Três coisas que só o banco pode provar, e que nenhum teste de API pegaria:

1. **O DDL é idempotente.** Ele roda a cada subida da app (o lifespan chama
   ``init_db``), então rodar duas vezes tem de ser um no-op — e não um
   ``table already exists``.
2. **Os CHECKs recusam de verdade.** Um vocabulário fechado que só existe em
   tuplas do Python não protege nada: o dia em que uma rota nova esquecer de
   validar, é o CHECK que impede um ``papel='chefe'`` entrar no banco.
3. **Os CASCADE funcionam.** ``foreign_keys`` é PRAGMA **por conexão**: abrir
   com ``sqlite3.connect`` direto deixaria o CASCADE inerte e este teste
   passaria em falso. Por isso todo teste daqui abre por ``db.connect``.

Nenhum teste toca ``data/``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from prompt_factory import db as dbmod
from prompt_factory.annotate import db as adb
from prompt_factory.annotate import seed as seedmod


@pytest.fixture
def conn(tmp_path: Path):
    c = dbmod.connect(tmp_path / "annotate.sqlite")
    adb.init_db(c)
    try:
        yield c
    finally:
        c.close()


# ---------------------------------------------------------------------------
# 1. DDL idempotente
# ---------------------------------------------------------------------------


def test_ddl_roda_duas_vezes(tmp_path: Path) -> None:
    """O lifespan aplica o DDL em TODA subida: a segunda não pode explodir."""
    c = dbmod.connect(tmp_path / "a.sqlite")
    try:
        c.executescript(adb.DDL)
        c.executescript(adb.DDL)  # <- o que este teste existe para provar
        adb.init_db(c)
        adb.init_db(c)
        tabelas = {
            str(r["name"])
            for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert set(adb.TABELAS) <= tabelas
        assert adb.get_meta(c, adb.CHAVE_VERSAO) == str(adb.SCHEMA_VERSION_ANOTACAO)
    finally:
        c.close()


def test_init_db_nao_reescreve_a_versao(conn: sqlite3.Connection) -> None:
    """``DO NOTHING``: um banco de schema antigo continua dizendo o que é.

    Se ``init_db`` sobrescrevesse a versão, um banco de schema 1 aberto por uma
    app de schema 2 passaria a se declarar 2 sem que nada tivesse migrado — e a
    guarda do lifespan nunca dispararia.
    """
    adb.set_meta(conn, adb.CHAVE_VERSAO, "99")
    adb.init_db(conn)
    assert adb.get_meta(conn, adb.CHAVE_VERSAO) == "99"


def test_contagens_cobre_todas_as_tabelas(conn: sqlite3.Connection) -> None:
    contagens = adb.contagens(conn)
    assert set(contagens) == set(adb.TABELAS)
    # app_meta já tem a versão; o resto nasce vazio.
    assert contagens["app_meta"] == 1
    assert contagens["anotadores"] == 0


# ---------------------------------------------------------------------------
# 2. CHECKs
# ---------------------------------------------------------------------------


def test_papel_invalido_e_recusado(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO anotadores (nome, papel) VALUES ('X', 'chefe')")
    for papel in adb.PAPEIS:
        conn.execute("INSERT INTO anotadores (nome, papel) VALUES (?, ?)", (papel, papel))


def test_nome_de_anotador_e_unico(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO anotadores (nome, papel) VALUES ('Ana', 'anotador')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO anotadores (nome, papel) VALUES ('Ana', 'revisor')")


def test_status_e_tipo_de_tarefa_invalidos_sao_recusados(conn: sqlite3.Connection) -> None:
    base = "INSERT INTO tarefas (tipo, prompt_uid, origem, status) VALUES (?, 'u1', 'semente', ?)"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(base, ("classificar", "aberta"))  # tipo fora do vocabulário
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(base, ("comparar_ab", "arquivada"))  # status inventado
    for tipo in adb.TIPOS_TAREFA:
        conn.execute(base, (tipo, "aberta"))


def test_n_anotacoes_alvo_precisa_ser_pelo_menos_um(conn: sqlite3.Connection) -> None:
    """Zero anotações-alvo é uma tarefa que nunca sai da fila — um buraco mudo."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tarefas (tipo, prompt_uid, origem, n_anotacoes_alvo) "
            "VALUES ('comparar_ab', 'u1', 'semente', 0)"
        )


def test_status_de_atribuicao_invalido_e_recusado(conn: sqlite3.Connection) -> None:
    ids = _cenario(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE atribuicoes SET status = 'sumiu' WHERE id = ?", (ids["atribuicao"],)
        )
    for status in adb.STATUS_ATRIBUICAO:
        conn.execute(
            "UPDATE atribuicoes SET status = ? WHERE id = ?", (status, ids["atribuicao"])
        )


def test_mesma_pessoa_nao_pega_a_mesma_tarefa_duas_vezes(conn: sqlite3.Connection) -> None:
    """A trava anti-repetição. O re-trabalho REUSA a linha, não cria outra."""
    ids = _cenario(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO atribuicoes (tarefa_id, anotador_id) VALUES (?, ?)",
            (ids["tarefa"], ids["anotador"]),
        )


def test_duas_versoes_da_mesma_anotacao_convivem(conn: sqlite3.Connection) -> None:
    """Rejeitar gera ``versao+1``; a versão 1 continua no banco."""
    ids = _cenario(conn)
    conn.execute(
        "INSERT INTO anotacoes (atribuicao_id, versao, payload_schema, payload_json) "
        "VALUES (?, 2, 'comparar_ab@1', '{}')",
        (ids["atribuicao"],),
    )
    versoes = [
        int(r["versao"])
        for r in conn.execute(
            "SELECT versao FROM anotacoes WHERE atribuicao_id = ? ORDER BY versao",
            (ids["atribuicao"],),
        )
    ]
    assert versoes == [1, 2]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO anotacoes (atribuicao_id, versao, payload_schema, payload_json) "
            "VALUES (?, 2, 'comparar_ab@1', '{}')",
            (ids["atribuicao"],),
        )


def test_rejeitar_sem_comentario_e_recusado_pelo_banco(conn: sqlite3.Connection) -> None:
    """Regra de produto escrita no DDL: devolver trabalho sem devolver informação.

    A tela também cobra o comentário, mas quem garante é o CHECK — uma rota nova
    que esqueça a validação não consegue gravar uma devolução muda.
    """
    ids = _cenario(conn)
    base = (
        "INSERT INTO revisoes (anotacao_id, revisor_id, veredito, comentario) "
        "VALUES (?, ?, ?, ?)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(base, (ids["anotacao"], ids["revisor"], "rejeitada", None))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(base, (ids["anotacao"], ids["revisor"], "rejeitada", "   "))
    # Aprovar sem comentário, sim: não há nada a corrigir.
    conn.execute(base, (ids["anotacao"], ids["revisor"], "aprovada", None))
    with pytest.raises(sqlite3.IntegrityError):
        # UNIQUE: uma revisão por VERSÃO da anotação.
        conn.execute(base, (ids["anotacao"], ids["revisor"], "aprovada", None))


def test_uid_demo_tem_prefixo_e_o_banco_exige(conn: sqlite3.Connection) -> None:
    """O prefixo é contrato do resolvedor corpus x demo, e o DDL o cobra."""
    uid = adb.uid_demo("Explique a diferença entre juros simples e compostos.")
    assert uid.startswith(adb.PREFIXO_DEMO)
    assert adb.e_demo(uid) and not adb.e_demo("0123456789abcdef")
    # Endereçado pelo conteúdo: o mesmo texto dá sempre o mesmo uid.
    assert uid == adb.uid_demo("Explique a diferença entre juros simples e compostos.")
    conn.execute(
        "INSERT INTO prompts_demo (uid, text, lang) VALUES (?, 'x', 'pt')", (uid,)
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO prompts_demo (uid, text, lang) VALUES "
            "('0123456789abcdef', 'x', 'pt')"
        )


# ---------------------------------------------------------------------------
# 3. CASCADE (só funciona com foreign_keys=ON, que é por conexão)
# ---------------------------------------------------------------------------


def _cenario(conn: sqlite3.Connection) -> dict[str, int]:
    """Uma tarefa, uma pessoa, uma atribuição e uma anotação — a cadeia inteira."""
    anotador = int(
        conn.execute(
            "INSERT INTO anotadores (nome, papel) VALUES ('Ana', 'anotador')"
        ).lastrowid
        or 0
    )
    revisor = int(
        conn.execute(
            "INSERT INTO anotadores (nome, papel) VALUES ('Diego', 'revisor')"
        ).lastrowid
        or 0
    )
    tarefa = int(
        conn.execute(
            "INSERT INTO tarefas (tipo, prompt_uid, origem) "
            "VALUES ('comparar_ab', 'abc0123456789def', 'semente')"
        ).lastrowid
        or 0
    )
    atribuicao = int(
        conn.execute(
            "INSERT INTO atribuicoes (tarefa_id, anotador_id) VALUES (?, ?)",
            (tarefa, anotador),
        ).lastrowid
        or 0
    )
    anotacao = int(
        conn.execute(
            "INSERT INTO anotacoes (atribuicao_id, payload_schema, payload_json) "
            "VALUES (?, 'comparar_ab@1', '{\"preferencia\":\"A\"}')",
            (atribuicao,),
        ).lastrowid
        or 0
    )
    return {
        "anotador": anotador,
        "revisor": revisor,
        "tarefa": tarefa,
        "atribuicao": atribuicao,
        "anotacao": anotacao,
    }


def test_apagar_tarefa_cascateia_ate_a_anotacao(conn: sqlite3.Connection) -> None:
    """tarefas -> atribuicoes -> anotacoes, em duas pontes de CASCADE."""
    _cenario(conn)
    assert adb.contagens(conn)["anotacoes"] == 1
    conn.execute("DELETE FROM tarefas")
    depois = adb.contagens(conn)
    assert depois["atribuicoes"] == 0, "o CASCADE de tarefas->atribuicoes não disparou"
    assert depois["anotacoes"] == 0, "o CASCADE de atribuicoes->anotacoes não disparou"
    # A pessoa NÃO some junto: o histórico dela é o que as métricas somam.
    assert depois["anotadores"] == 2


def test_anotador_nao_pode_ser_apagado_com_trabalho(conn: sqlite3.Connection) -> None:
    """Sem CASCADE nesta ponta de propósito: apagar quem anotou apagaria a prova."""
    _cenario(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM anotadores WHERE nome = 'Ana'")


def test_rubrica_sobrevive_a_anotacao_que_a_originou(conn: sqlite3.Connection) -> None:
    """``ON DELETE SET NULL``: o instrumento de medida não morre com a origem."""
    ids = _cenario(conn)
    conn.execute(
        "INSERT INTO rubricas (prompt_uid, titulo, criterios_json, origem, anotacao_id) "
        "VALUES ('abc0123456789def', 'Clareza e correção', '[]', 'anotacao', ?)",
        (ids["anotacao"],),
    )
    conn.execute("DELETE FROM anotacoes WHERE id = ?", (ids["anotacao"],))
    linha = conn.execute("SELECT anotacao_id FROM rubricas").fetchone()
    assert linha is not None and linha["anotacao_id"] is None


# ---------------------------------------------------------------------------
# 4. seed
# ---------------------------------------------------------------------------


def test_seed_das_personas_e_idempotente(conn: sqlite3.Connection) -> None:
    assert seedmod.semear_personas(conn) == len(seedmod.PERSONAS)
    assert seedmod.semear_personas(conn) == 0
    papeis = dict(
        (str(r["papel"]), int(r["n"]))
        for r in conn.execute("SELECT papel, count(*) AS n FROM anotadores GROUP BY papel")
    )
    # 3 anotadores, 2 revisores, 1 admin — e os dois revisores existem porque o
    # revisor não pode revisar a própria anotação.
    assert papeis == {"anotador": 3, "revisor": 2, "admin": 1}
    assert not seedmod.tem_trabalho(conn)


def test_seed_nao_sobrescreve_papel_mudado_na_tela(conn: sqlite3.Connection) -> None:
    seedmod.semear_personas(conn)
    conn.execute("UPDATE anotadores SET papel = 'admin' WHERE nome = 'Ana Ribeiro'")
    seedmod.semear_personas(conn)
    linha = conn.execute(
        "SELECT papel FROM anotadores WHERE nome = 'Ana Ribeiro'"
    ).fetchone()
    assert linha["papel"] == "admin"
