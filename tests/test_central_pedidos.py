"""Central de Briefs Pedagógicos, F1: o schema v7 e a tabela ``pedidos``.

Este marco não tem tela nem CLI própria — ele é só o chão. E é justamente por
isso que ele precisa de teste: a v7 junta os **dois** modos de falha que as
migrações anteriores ensinaram um de cada vez, e os dois são silenciosos.

* A v6 ensinou que ``CREATE TABLE IF NOT EXISTS`` **não acrescenta coluna** em
  tabela que já existe. Aqui são duas, em ``criacoes``.
* A v4 ensinou que ele também **não reescreve um CHECK**. Aqui é
  ``rubricas.origem`` ganhando ``'criacao'`` — e nada neste marco o exercita, o
  que torna o engano mais provável, não menos: o banco pareceria bem até a
  primeira aprovação de uma criação vinda de pedido, meses depois, e o erro
  apareceria como um bug do código que está tentando escrever.

As famílias aqui:

* **o vocabulário fechado é o CHECK**, e a prova é o banco recusando o valor
  inventado — não uma comparação de tuplas com elas mesmas;
* **NULL é a verdade na criação livre.** Ela não veio de pedido nenhum, e o modo
  criar do P4 continua funcionando sem saber que a v7 existe;
* **a migração preserva e não produz.** ``pedidos`` nasce vazia: destilar no meio
  de uma migração misturaria preservar o que existe com produzir material novo.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from prompt_factory import db as dbmod
from prompt_factory.annotate import db as adb
from prompt_factory.annotate import migracao
from prompt_factory.annotate import seed as seedmod
from prompt_factory.annotate.main import criar_app

from .fixtures_annotate_v1 import contagens_do_disco, rebaixar
from .test_annotate_p2 import notas_da_rubrica
from .test_api import montar_banco

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    caminho = tmp_path / "prompts.sqlite"
    montar_banco(caminho)
    return caminho


@pytest.fixture
def semeado(tmp_path: Path, corpus: Path) -> Path:
    banco = tmp_path / "annotate.sqlite"
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


@pytest.fixture
def conn(tmp_path: Path):
    """Um banco novo, no schema de hoje, sem seed — para exercitar os CHECKs."""
    c = dbmod.connect(tmp_path / "vazio.sqlite")
    try:
        adb.init_db(c)
        yield c
    finally:
        c.close()


def _papeis(cliente: TestClient) -> dict[str, list[int]]:
    saida: dict[str, list[int]] = {}
    for p in cliente.get("/api/perfis").json()["items"]:
        saida.setdefault(p["papel"], []).append(p["id"])
    return saida


#: Um pedido plausível. Os campos com default do DDL ficam de fora de propósito:
#: metade dos testes daqui existe para provar que o default é o que se diz.
PEDIDO = {
    "arquivo_fonte": "DOSEUJEITO_PNLD26_Filosofia_VU_MP.txt",
    "offset_inicio": 412_000,
    "recorte": "O apolíneo e o dionisíaco não são, em Nietzsche, dois polos que se "
    "excluem: são duas forças que só produzem tragédia quando tensionadas.",
    "task_type": "redacao-pratica",
    "tema": "Apolíneo e dionisíaco",
    "meta_pedagogica": "Exercitar a leitura de um par conceitual sem reduzi-lo a "
    "uma oposição simples.",
    "papel": "professor",
    "lote_id": "ped_0001",
}


def _inserir(c: sqlite3.Connection, **extra: object) -> int:
    """Insere um pedido, com ``chave`` derivada do que varia entre chamadas."""
    campos = {**PEDIDO, **extra}
    campos.setdefault("chave", f"chave-{campos['lote_id']}-{campos['offset_inicio']}")
    nomes = ", ".join(campos)
    marcas = ", ".join("?" * len(campos))
    cur = c.execute(f"INSERT INTO pedidos ({nomes}) VALUES ({marcas})", tuple(campos.values()))
    return int(cur.lastrowid or 0)


# ---------------------------------------------------------------------------
# 1. a tabela existe, e o `pf annotate status` a enxerga sozinho
# ---------------------------------------------------------------------------


def test_pedidos_nasce_no_banco_novo_e_entra_nas_contagens(conn: sqlite3.Connection) -> None:
    """A DoD do F1: ``pf annotate status`` lista ``pedidos`` com 0.

    Ele conta por ``db.TABELAS``, então a única coisa a provar é que a tabela
    está na lista — o relatório a mostra de graça.
    """
    assert "pedidos" in adb.TABELAS
    contagens = adb.contagens(conn)
    assert contagens["pedidos"] == 0
    assert set(contagens) == set(adb.TABELAS)


def test_a_versao_do_schema_subiu(conn: sqlite3.Connection) -> None:
    assert adb.SCHEMA_VERSION_ANOTACAO == 7
    assert adb.get_meta(conn, adb.CHAVE_VERSAO) == "7"


# ---------------------------------------------------------------------------
# 2. o vocabulário fechado é o CHECK
# ---------------------------------------------------------------------------


def test_um_pedido_plausivel_entra_com_os_defaults_declarados(conn: sqlite3.Connection) -> None:
    """Série e dificuldade têm default porque o material raramente amarra os dois
    — e ``indefinido`` é resposta legítima, não ausência de dado."""
    pid = _inserir(conn)
    linha = conn.execute("SELECT * FROM pedidos WHERE id = ?", (pid,)).fetchone()
    assert linha["status"] == "disponivel"
    assert linha["serie"] == "indefinido"
    assert linha["dificuldade"] == "intermediaria"
    assert json.loads(linha["habilidades_json"]) == []
    assert json.loads(linha["avisos_json"]) == []
    assert linha["reservado_por"] is None and linha["reservado_em"] is None


@pytest.mark.parametrize(
    ("coluna", "valores"),
    [
        ("papel", adb.PAPEIS_PEDIDO),
        ("status", adb.STATUS_PEDIDO),
        ("serie", adb.SERIES_PEDIDO),
        ("dificuldade", adb.DIFICULDADES_PEDIDO),
    ],
)
def test_cada_tupla_do_modulo_e_exatamente_o_CHECK_do_banco(
    conn: sqlite3.Connection, coluna: str, valores: tuple[str, ...]
) -> None:
    """Banco e API concordam por CONSTRUÇÃO, e a prova é o banco recusando.

    Comparar a tupla com ela mesma provaria nada; o que se prova aqui é que todo
    valor da tupla entra e que um de fora não entra.
    """
    for i, valor in enumerate(valores):
        _inserir(conn, offset_inicio=i, **{coluna: valor})
    with pytest.raises(sqlite3.IntegrityError):
        _inserir(conn, offset_inicio=999, **{coluna: "inventado"})


def test_offset_negativo_e_recusado(conn: sqlite3.Connection) -> None:
    """O offset é a proveniência do recorte: um negativo não aponta para lugar
    nenhum do arquivo, e um pedido inconferível não vale nada."""
    with pytest.raises(sqlite3.IntegrityError):
        _inserir(conn, offset_inicio=-1)


def test_task_type_nao_tem_CHECK_e_isso_e_decisao(conn: sqlite3.Connection) -> None:
    """A mesma razão do ``task_type`` do corpus: a taxonomia evolui, e migrar um
    banco que guarda trabalho humano por causa de uma classe nova seria absurdo.

    Quem valida é o import, contra a taxonomia carregada — não o schema.
    """
    _inserir(conn, task_type="uma-classe-que-a-taxonomia-nao-tem")


def test_a_chave_natural_e_UNIQUE_e_e_o_que_torna_o_import_idempotente(
    conn: sqlite3.Connection,
) -> None:
    """A idempotência de ``geracao.gravar_material``, repetida aqui: reimportar
    um lote não pode duplicar o banco de pedidos."""
    _inserir(conn, chave="sha-de-um-recorte")
    with pytest.raises(sqlite3.IntegrityError):
        _inserir(conn, offset_inicio=1, chave="sha-de-um-recorte")


# ---------------------------------------------------------------------------
# 3. as colunas novas de `criacoes`
# ---------------------------------------------------------------------------


def test_a_criacao_livre_continua_nascendo_com_as_duas_colunas_NULL(
    cliente: TestClient, semeado: Path
) -> None:
    """O P4 não sabe que a v7 existe, e é assim que tem de continuar até o F4.

    NULL ali é a verdade, não um buraco: aquela criação não veio de pedido
    nenhum.
    """
    quem = _papeis(cliente)["anotador"][0]
    r = cliente.post(
        "/api/criacoes",
        json={
            "autor_id": quem,
            "texto": "Escreva um roteiro de aula de 50 minutos sobre juros compostos "
            "para uma turma que já sabe progressão geométrica.",
            "lang": "pt",
        },
    )
    assert r.status_code == 201, r.text

    conn = dbmod.connect(semeado)
    try:
        linha = conn.execute(
            "SELECT pedido_id, material_json FROM criacoes WHERE id = ?", (r.json()["id"],)
        ).fetchone()
        assert linha["pedido_id"] is None
        assert linha["material_json"] is None
    finally:
        conn.close()


def test_a_criacao_pode_apontar_para_um_pedido_e_a_FK_morde(conn: sqlite3.Connection) -> None:
    """Sem a FK, uma criação órfã sobreviveria a um pedido arquivado e o painel
    do revisor mostraria o recorte de coisa nenhuma."""
    conn.execute(
        "INSERT INTO anotadores (id, nome, papel) VALUES (1, 'quem escreve', 'anotador')"
    )
    pid = _inserir(conn)
    conn.execute(
        "INSERT INTO criacoes (autor_id, texto, lang, hash_norm, pedido_id, material_json) "
        "VALUES (1, 'um prompt qualquer', 'pt', 'abc', ?, ?)",
        (pid, json.dumps({"schema": "material_criacao@1"})),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO criacoes (autor_id, texto, lang, hash_norm, pedido_id) "
            "VALUES (1, 'outro', 'pt', 'def', 987654)"
        )


def test_dois_pedidos_diferentes_podem_render_criacoes_e_um_pedido_pode_render_duas(
    conn: sqlite3.Connection,
) -> None:
    """Não há UNIQUE em ``criacoes.pedido_id``, e é decisão: uma criação recusada
    devolve o pedido à fila, e a próxima pessoa escreve outra sobre o mesmo
    recorte. Um UNIQUE transformaria uma recusa em pedido queimado."""
    conn.execute("INSERT INTO anotadores (id, nome, papel) VALUES (1, 'a', 'anotador')")
    pid = _inserir(conn)
    for i, texto in enumerate(("primeira tentativa", "segunda tentativa")):
        conn.execute(
            "INSERT INTO criacoes (autor_id, texto, lang, hash_norm, pedido_id) "
            "VALUES (1, ?, 'pt', ?, ?)",
            (texto, f"h{i}", pid),
        )


# ---------------------------------------------------------------------------
# 4. o CHECK que este marco alarga e não exercita
# ---------------------------------------------------------------------------


def test_rubricas_aceita_a_origem_criacao_e_recusa_a_inventada(
    conn: sqlite3.Connection,
) -> None:
    """O valor que a v7 acrescenta ao CHECK — e o único jeito de trocar um CHECK
    no SQLite é reconstruir a tabela, que é o que a migração faz de graça.

    ``criacao`` não cabe em nenhuma das três anteriores: ``anotacao`` diria que
    saiu da aba escrever-rubrica; ``importada``, que um lote a gerou sobre um
    prompt que já estava no corpus (este ainda nem chegou lá).
    """
    assert "criacao" in adb.ORIGENS_RUBRICA
    for origem in adb.ORIGENS_RUBRICA:
        conn.execute(
            "INSERT INTO rubricas (prompt_uid, titulo, criterios_json, origem) "
            "VALUES (?, 't', '{}', ?)",
            (f"uid-{origem}", origem),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO rubricas (prompt_uid, titulo, criterios_json, origem) "
            "VALUES ('x', 't', '{}', 'inventada')"
        )


# ---------------------------------------------------------------------------
# 5. a migração
# ---------------------------------------------------------------------------


def test_todas_as_origens_conhecidas_tem_passo() -> None:
    """Uma versão listada e sem função seria um ``KeyError`` numa migração."""
    assert set(migracao.PASSOS) == set(migracao.ORIGENS_CONHECIDAS)
    assert adb.SCHEMA_VERSION_ANOTACAO - 1 in migracao.PASSOS
    assert adb.SCHEMA_VERSION_ANOTACAO not in migracao.PASSOS


def test_a_copia_da_v6_cobre_todas_as_tabelas_que_existiam() -> None:
    """Uma tabela nova que ninguém copiasse sairia VAZIA da migração, e a
    conferência de contagens só a pegaria se ela tivesse linhas."""
    cobertas = {t for t, _ in migracao.COPIA_V6_ANTES + migracao.COPIA_V6_DEPOIS}
    assert cobertas == set(adb.TABELAS) - {"app_meta"} - migracao.SEM_ORIGEM[6]


def test_migrar_da_v6_preserva_o_trabalho_e_deixa_as_colunas_novas_NULL(
    cliente: TestClient, semeado: Path
) -> None:
    """O caso normal: um banco v6 com trabalho humano dentro sobe para a v7.

    O que se cobra é o de sempre — contagem por contagem, e as colunas novas
    nascendo NULL, que é a verdade sobre trabalho feito antes de existir pedido.
    """
    quem = _papeis(cliente)["anotador"][0]
    env = cliente.post(
        "/api/tarefas/proxima", json={"anotador_id": quem, "tipo": "avaliar_rubrica"}
    ).json()["tarefa"]
    r = cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
        json={"anotador_id": quem, "payload": notas_da_rubrica(env)},
    )
    assert r.status_code == 200, r.text
    anotacao_id = r.json()["anotacao_id"]

    criacao = cliente.post(
        "/api/criacoes",
        json={
            "autor_id": quem,
            "texto": "Explique a diferença entre calor e temperatura usando um "
            "exemplo de cozinha, para quem nunca viu termodinâmica.",
            "lang": "pt",
        },
    )
    assert criacao.status_code == 201, criacao.text
    criacao_id = criacao.json()["id"]

    conn = dbmod.connect(semeado)
    try:
        rebaixar(conn, 6)
        adb.set_meta(conn, adb.CHAVE_VERSAO, 6)
        antes = contagens_do_disco(conn)
        assert "pedidos" not in antes, "o rebaixamento tem de produzir um v6 fiel"
    finally:
        conn.close()

    relatorio = migracao.migrar(semeado)
    assert relatorio["de"] == 6 and relatorio["para"] == adb.SCHEMA_VERSION_ANOTACAO
    assert Path(relatorio["backup"]).is_file()
    assert any("pedidos" in aviso for aviso in relatorio["avisos"])

    conn = dbmod.connect(semeado)
    try:
        depois = contagens_do_disco(conn)
        for tabela, n in antes.items():
            if tabela in migracao.TABELAS_QUE_CRESCEM:
                assert depois[tabela] >= n, tabela
                continue
            assert depois[tabela] == n, tabela

        assert depois["pedidos"] == 0, "nenhum pedido é destilado por uma migração"
        assert depois["anotacoes"] >= 1

        linha = conn.execute(
            "SELECT pedido_id, material_json FROM criacoes WHERE id = ?", (criacao_id,)
        ).fetchone()
        assert linha["pedido_id"] is None and linha["material_json"] is None
        # e o trabalho de anotação atravessou inteiro, com os dois ponteiros
        anot = conn.execute(
            "SELECT payload_json, versao_diretriz FROM anotacoes WHERE id = ?", (anotacao_id,)
        ).fetchone()
        assert anot is not None and json.loads(anot["payload_json"])
    finally:
        conn.close()


def test_o_brief_sobrevive_a_migracao_da_v6(cliente: TestClient, semeado: Path) -> None:
    """``briefs`` entra pela PRIMEIRA vez numa lista de cópia — a situação exata
    de ``turnos_conversa`` na v5→v6.

    Esquecê-la seria pior que perder uma tabela qualquer: as
    ``anotacoes.versao_brief`` continuariam apontando para versões que já não
    existem, e a trilha seguiria parecendo íntegra enquanto responde errado.
    """
    conn = dbmod.connect(semeado)
    try:
        antes = [
            (int(x["projeto_id"]), int(x["versao"]))
            for x in conn.execute("SELECT projeto_id, versao FROM briefs ORDER BY id")
        ]
        assert antes, "o seed publica os briefs; sem eles este teste não prova nada"
        rebaixar(conn, 6)
        adb.set_meta(conn, adb.CHAVE_VERSAO, 6)
    finally:
        conn.close()

    migracao.migrar(semeado)

    conn = dbmod.connect(semeado)
    try:
        depois = [
            (int(x["projeto_id"]), int(x["versao"]))
            for x in conn.execute("SELECT projeto_id, versao FROM briefs ORDER BY id")
        ]
        assert depois == antes
    finally:
        conn.close()


def test_o_schema_migrado_da_v6_e_identico_ao_de_um_banco_novo(
    tmp_path: Path, semeado: Path
) -> None:
    """A propriedade que o ``ALTER TABLE`` não daria — e que este módulo mantém
    desde a v1. É ela que garante que o CHECK novo de ``rubricas.origem`` chegou
    ao banco migrado: um CHECK velho sobrevivente não aparece em contagem
    nenhuma."""
    conn = dbmod.connect(semeado)
    try:
        rebaixar(conn, 6)
        adb.set_meta(conn, adb.CHAVE_VERSAO, 6)
    finally:
        conn.close()
    migracao.migrar(semeado)

    novo = tmp_path / "novo.sqlite"
    conn = dbmod.connect(novo)
    try:
        adb.init_db(conn)
        esperado = sorted(
            str(r["sql"])
            for r in conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")
        )
    finally:
        conn.close()

    conn = dbmod.connect(semeado)
    try:
        obtido = sorted(
            str(r["sql"])
            for r in conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")
        )
    finally:
        conn.close()

    assert obtido == esperado


def test_a_rubrica_de_criacao_entra_no_banco_MIGRADO(semeado: Path) -> None:
    """O modo de falha que nenhuma contagem pega.

    Sem a v7, o banco do dono continuaria recusando esta linha com um CHECK que
    nenhum arquivo do repositório mostra mais — e o sintoma apareceria como um
    bug do código que está tentando escrever, não como um banco desatualizado.
    """
    conn = dbmod.connect(semeado)
    try:
        rebaixar(conn, 6)
        adb.set_meta(conn, adb.CHAVE_VERSAO, 6)
    finally:
        conn.close()
    migracao.migrar(semeado)

    conn = dbmod.connect(semeado)
    try:
        conn.execute(
            "INSERT INTO rubricas (prompt_uid, titulo, criterios_json, origem) "
            "VALUES ('uid-previsto-de-uma-criacao', 'r', '{}', 'criacao')"
        )
        assert int(
            conn.execute(
                "SELECT count(*) AS n FROM rubricas WHERE origem = 'criacao'"
            ).fetchone()["n"]
        ) == 1
    finally:
        conn.close()


def test_migrar_duas_vezes_e_no_op(semeado: Path) -> None:
    """Idempotência: o segundo comando não tem o que fazer e diz isso."""
    conn = dbmod.connect(semeado)
    try:
        rebaixar(conn, 6)
        adb.set_meta(conn, adb.CHAVE_VERSAO, 6)
    finally:
        conn.close()
    migracao.migrar(semeado)
    assert migracao.migrar(semeado)["ja_estava"] is True
    assert migracao.precisa_migrar(semeado) is None
