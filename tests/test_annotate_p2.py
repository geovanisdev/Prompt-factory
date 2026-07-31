"""Testes do fluxo do anotador (P2): seed, trava, payloads, catálogo, submissão.

Os dois bancos são sintéticos e ficam em ``tmp_path``. O corpus é o MESMO banco
hostil de ``test_api.py`` — com ``<script>`` dentro de um prompt, um template de
robô com 120 duplicatas e prompt de 200 mil caracteres —, porque a plataforma vai
mostrar o corpus de verdade e o pool precisa aguentar isso.

Cinco provas carregam o marco inteiro, e cada uma existe por um defeito que
aconteceria de verdade:

* **a trava** — duas abas abertas na demonstração é o caso NORMAL, e sem
  ``BEGIN IMMEDIATE`` as duas recebem a mesma tarefa;
* **``n_anotacoes_alvo=2``** — sem a conta de vagas, ou ninguém consegue a
  segunda anotação (e o agreement do P5 fica sem matéria-prima) ou todo mundo
  consegue e o alvo não significa nada;
* **o TTL** — uma aba fechada no meio de uma tarefa não pode prendê-la para
  sempre;
* **``bool`` como nota** — ``true`` viraria 1 em silêncio, uma nota válida e
  errada;
* **o gabarito** — vazado, transforma a tarefa-ouro numa prova de leitura de
  JSON.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from prompt_factory import db as dbmod
from prompt_factory.annotate import catalogo as cat
from prompt_factory.annotate import db as adb
from prompt_factory.annotate import payloads
from prompt_factory.annotate import seed as seedmod
from prompt_factory.annotate import tarefas as tmod
from prompt_factory.annotate.main import criar_app

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
    """O banco da plataforma com as três camadas do seed já dentro."""
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


def perfis(cliente: TestClient) -> dict[str, int]:
    """``{nome: id}`` das personas."""
    return {p["nome"]: p["id"] for p in cliente.get("/api/perfis").json()["items"]}


def anotadores(cliente: TestClient) -> list[int]:
    itens = cliente.get("/api/perfis").json()["items"]
    return [p["id"] for p in itens if p["papel"] == "anotador"]


def pegar(cliente: TestClient, quem: int, tipo: str) -> dict[str, Any]:
    r = cliente.post("/api/tarefas/proxima", json={"anotador_id": quem, "tipo": tipo})
    assert r.status_code == 200, r.text
    return r.json()


def com_banco(caminho: Path):
    conn = dbmod.connect(caminho)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# 1. o pacote de demonstração
# ---------------------------------------------------------------------------


def test_pacote_tem_a_forma_prometida() -> None:
    """11 itens (8 do P2 + os 3 em inglês do P3i), 5+6 tarefas, 3 tarefas-ouro."""
    pacote = seedmod.carregar_pacote()
    assert len(pacote["itens"]) == 11
    tipos = [t["tipo"] for t in pacote["tarefas"]]
    assert tipos.count("avaliar_rubrica") == 5
    assert tipos.count("comparar_ab") == 6
    com_gabarito = [t for t in pacote["tarefas"] if t.get("gabarito")]
    assert len(com_gabarito) == 3
    assert all(int(t["n_anotacoes_alvo"]) == 2 for t in com_gabarito)


def test_toda_rubrica_e_profissional() -> None:
    """3 a 5 critérios, cada um com descrição e escala ANCORADA.

    Escala sem âncora não mede nada: "clareza de 1 a 5" significa cinco coisas
    diferentes para cinco anotadores, e o agreement despenca sem que ninguém
    saiba por quê. Esta é a prova de domínio que o pacote precisa carregar.
    """
    for item in seedmod.carregar_pacote()["itens"]:
        criterios = item["rubrica"]["criterios"]
        assert 3 <= len(criterios) <= 5, item["chave"]
        assert len(item["rubrica"]["titulo"]) > 10
        for c in criterios:
            assert len(c["descricao"]) > 60, (item["chave"], c["nome"])
            escala = c["escala"]
            assert escala["min"] < escala["max"] <= 9
            ancoras = escala["ancoras"]
            assert len(ancoras) >= 3
            assert {a["valor"] for a in ancoras} <= set(range(escala["min"], escala["max"] + 1))
            assert all(len(a["rotulo"]) > 5 for a in ancoras)


def test_todo_par_tem_uma_resposta_defensavel() -> None:
    """NUNCA as duas igualmente boas: um A/B empatado mede ruído, não preferência.

    E cada defeito plantado tem de estar catalogado, porque é ele que o painel
    do admin (P5) mostra para explicar por que o gabarito é o que é.
    """
    familias = set()
    for item in seedmod.carregar_pacote()["itens"]:
        corretas = [r for r in item["respostas"] if r["meta"]["correta"]]
        assert len(corretas) == 1, f"{item['chave']}: {len(corretas)} respostas 'corretas'"
        for r in item["respostas"]:
            assert len(r["meta"]["defeito_plantado"]) > 40
            familias.add(r["meta"]["defeito_classe"])
    # As quatro famílias que um linguista pega e um leitor apressado não.
    assert {
        "fato-inventado-com-precisao",
        "restricao-ignorada",
        "registro-errado",
        "fluencia-cobrindo-vazio",
    } <= familias


def test_o_pacote_e_bilingue() -> None:
    """P3i: cinco prompts em inglês, e não os dois do P2.

    O portfólio é lido por avaliadores estrangeiros, e ler a interface traduzida
    não é a mesma coisa que conseguir FAZER o trabalho. Com cinco itens em
    inglês, as filas de ``avaliar_rubrica`` e ``comparar_ab`` têm material em
    inglês nas primeiras posições — as outras duas abas vêm do pool, que também
    passou a aceitar as duas línguas.
    """
    langs = [item["lang"] for item in seedmod.carregar_pacote()["itens"]]
    assert langs.count("en") == 5
    assert langs.count("pt") == 6


def test_a_estrutura_do_pacote_e_bilingue_e_o_dado_nao() -> None:
    """Rubrica, critério, âncora e catálogo de defeitos têm as DUAS línguas.

    ``prompt`` e ``resposta.texto`` não: a língua deles é intrínseca ao dado, e
    traduzi-los inventaria um texto que ninguém escreveu.
    """
    for item in seedmod.carregar_pacote()["itens"]:
        rub = item["rubrica"]
        assert set(rub["titulo_i18n"]) == {"en", "pt"}, item["chave"]
        # O canônico é a IDENTIDADE, e ele tem de ser uma das duas versões —
        # nunca um terceiro texto que não aparece na tela em língua nenhuma.
        assert rub["titulo"] in rub["titulo_i18n"].values(), item["chave"]
        for c in rub["criterios"]:
            assert set(c["nome_i18n"]) == {"en", "pt"}, (item["chave"], c["nome"])
            assert set(c["descricao_i18n"]) == {"en", "pt"}, (item["chave"], c["nome"])
            assert c["nome"] in c["nome_i18n"].values(), (item["chave"], c["nome"])
            for a in c["escala"]["ancoras"]:
                assert set(a["rotulo_i18n"]) == {"en", "pt"}, (item["chave"], c["nome"])
        for r in item["respostas"]:
            assert set(r["meta"]["defeito_plantado_i18n"]) == {"en", "pt"}, item["chave"]
            assert set(r["meta"]["modelo_i18n"]) == {"en", "pt"}, item["chave"]
        assert "prompt_i18n" not in item, f"{item['chave']}: prompt não se traduz"
        assert all("texto_i18n" not in r for r in item["respostas"]), item["chave"]


# ---------------------------------------------------------------------------
# 2. seed
# ---------------------------------------------------------------------------


def test_seed_e_idempotente(banco: Path, corpus: Path) -> None:
    """Semear é COMEÇAR, não reinicializar: a segunda rodada não cria nada."""
    conn = dbmod.connect(banco)
    corpo = dbmod.connect(corpus, readonly=True)
    try:
        adb.init_db(conn)
        primeira = seedmod.semear(conn, corpo)
        segunda = seedmod.semear(conn, corpo)
    finally:
        conn.close()
        corpo.close()
    assert primeira["personas"] == len(seedmod.PERSONAS)
    assert primeira["pacote"] == {
        "prompts_demo": 11,
        "rubricas": 11,
        "respostas_modelo": 22,
        "tarefas": 11,
        # Nada a traduzir: o banco nasceu com o pacote já bilíngue.
        "traduzidas": 0,
    }
    # Os tipos que um PROMPT CRU sustenta, `seed_pool_tarefas` de cada. Lido de
    # `TIPOS_DO_POOL` e não escrito à mão: com o P4d passaram a ser quatro, e uma
    # lista literal aqui só diria "mudou" sem dizer se mudou para o certo.
    assert primeira["pool"] == dict.fromkeys(seedmod.TIPOS_DO_POOL, 8)
    assert segunda["personas"] == 0
    assert set(segunda["pacote"].values()) == {0}
    assert set(segunda["pool"].values()) == {0}
    assert segunda["contagens"] == primeira["contagens"]


def test_seed_sem_corpus_semeia_so_o_pacote_e_avisa(banco: Path) -> None:
    """Clone limpo antes do ``pf load-db`` é estado legítimo, não erro."""
    conn = dbmod.connect(banco)
    try:
        adb.init_db(conn)
        relatorio = seedmod.semear(conn, None)
    finally:
        conn.close()
    assert relatorio["pacote"]["tarefas"] == 11
    assert relatorio["pool"] == dict.fromkeys(seedmod.TIPOS_DO_POOL, 0)
    assert any("sem corpus" in a for a in relatorio["avisos"])


def test_seed_do_pool_e_deterministico(banco: Path, tmp_path: Path, corpus: Path) -> None:
    """Mesmo corpus, mesmas tarefas — em duas máquinas e em dois bancos."""
    outro = tmp_path / "outro.sqlite"
    saidas = []
    for alvo in (banco, outro):
        conn = dbmod.connect(alvo)
        corpo = dbmod.connect(corpus, readonly=True)
        try:
            adb.init_db(conn)
            seedmod.semear(conn, corpo)
            saidas.append(
                [
                    (r["tipo"], r["prompt_uid"], r["prioridade"])
                    for r in conn.execute(
                        "SELECT tipo, prompt_uid, prioridade FROM tarefas "
                        "WHERE origem = 'semente' AND prompt_uid NOT LIKE 'demo:%' "
                        "ORDER BY tipo, prioridade DESC"
                    )
                ]
            )
        finally:
            conn.close()
            corpo.close()
    assert saidas[0] == saidas[1]
    assert saidas[0], "o corpus sintético não gerou tarefa nenhuma do pool"


def test_seed_separa_os_prompts_de_rubrica_e_de_sft(semeado: Path) -> None:
    """Fatias disjuntas: com os mesmos prompts, a continuação rubrica→SFT nunca
    teria o que criar, e o mecanismo ficaria invisível na demonstração."""
    conn = com_banco(semeado)
    try:
        por_tipo = {
            tipo: {
                str(r["prompt_uid"])
                for r in conn.execute(
                    "SELECT prompt_uid FROM tarefas WHERE tipo = ? AND prompt_uid NOT LIKE 'demo:%'",
                    (tipo,),
                )
            }
            for tipo in ("escrever_rubrica", "sft_resposta")
        }
    finally:
        conn.close()
    assert not (por_tipo["escrever_rubrica"] & por_tipo["sft_resposta"])


def test_force_recusa_por_cima_de_trabalho_humano(semeado: Path, corpus: Path) -> None:
    """A única operação verdadeiramente irreversível desta app — e ela recusa.

    ``tarefas`` cascateia para ``atribuicoes`` e daí para ``anotacoes``: um
    ``--force`` distraído apagaria anotação submetida, que a pipeline não recria.
    """
    conn = dbmod.connect(semeado)
    corpo = dbmod.connect(corpus, readonly=True)
    try:
        tarefa = conn.execute("SELECT id FROM tarefas LIMIT 1").fetchone()["id"]
        quem = conn.execute("SELECT id FROM anotadores LIMIT 1").fetchone()["id"]
        conn.execute(
            "INSERT INTO atribuicoes (tarefa_id, anotador_id) VALUES (?, ?)", (tarefa, quem)
        )
        atribuicao = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        conn.execute(
            "INSERT INTO anotacoes (atribuicao_id, payload_schema, payload_json) "
            "VALUES (?, 'comparar_ab@1', '{}')",
            (atribuicao,),
        )
        assert seedmod.tem_trabalho(conn)
        with pytest.raises(RuntimeError, match="trabalho humano"):
            seedmod.semear(conn, corpo, force=True)
        # E nada foi apagado antes da recusa.
        assert int(conn.execute("SELECT count(*) AS n FROM tarefas").fetchone()["n"]) > 0
    finally:
        conn.close()
        corpo.close()


def test_force_refaz_quando_nao_ha_trabalho(semeado: Path, corpus: Path) -> None:
    conn = dbmod.connect(semeado)
    corpo = dbmod.connect(corpus, readonly=True)
    try:
        antes = adb.contagens(conn)
        relatorio = seedmod.semear(conn, corpo, force=True)
        assert relatorio["apagados"]["tarefas"] == antes["tarefas"]
        assert adb.contagens(conn)["tarefas"] == antes["tarefas"]
        # Personas NÃO são apagadas: são a identidade guardada no localStorage.
        assert relatorio["personas"] == 0
        assert adb.contagens(conn)["anotadores"] == antes["anotadores"]
    finally:
        conn.close()
        corpo.close()


# ---------------------------------------------------------------------------
# 3. a trava
# ---------------------------------------------------------------------------


def test_a_trava_impede_pegar_a_mesma_tarefa_duas_vezes(cliente: TestClient) -> None:
    """Nem duas pessoas na mesma tarefa (alvo 1), nem a mesma pessoa duas vezes."""
    ana, bruno, _ = anotadores(cliente)
    primeira = pegar(cliente, ana, "escrever_rubrica")["tarefa"]
    segunda = pegar(cliente, ana, "escrever_rubrica")["tarefa"]
    assert primeira["tarefa"]["id"] != segunda["tarefa"]["id"]

    # E a tarefa que a Ana está segurando não sai para o Bruno enquanto ela a tem.
    do_bruno = {pegar(cliente, bruno, "escrever_rubrica")["tarefa"]["tarefa"]["id"] for _ in range(2)}
    assert primeira["tarefa"]["id"] not in do_bruno
    assert segunda["tarefa"]["id"] not in do_bruno


def test_alvo_2_serve_a_dois_e_diz_fila_vazia_ao_terceiro(cliente: TestClient) -> None:
    """A matéria-prima do agreement do P5, provada ponta a ponta.

    Fila vazia responde **200 com ``tarefa: null``**, não 404: é o estado mais
    comum de uma plataforma bem servida, e um 404 faria o cliente tratar o
    caminho normal como falha.
    """
    ana, bruno, carla = anotadores(cliente)
    conn = com_banco(cliente.app.state.db_anotacao)
    try:
        # Deixa SÓ a tarefa-ouro de comparar_ab aberta, para isolar a conta de vagas.
        ouro = conn.execute(
            "SELECT id FROM tarefas WHERE tipo = 'comparar_ab' AND n_anotacoes_alvo = 2"
        ).fetchone()["id"]
        conn.execute(
            "UPDATE tarefas SET status = 'pausada' WHERE tipo = 'comparar_ab' AND id <> ?",
            (ouro,),
        )
    finally:
        conn.close()

    for quem in (ana, bruno):
        corpo = pegar(cliente, quem, "comparar_ab")
        assert corpo["tarefa"] is not None
        assert corpo["tarefa"]["tarefa"]["id"] == ouro

    terceiro = pegar(cliente, carla, "comparar_ab")
    assert terceiro["tarefa"] is None
    assert terceiro["motivo"]


def test_ttl_expirado_devolve_a_vaga(cliente: TestClient) -> None:
    """Uma aba fechada no meio de uma tarefa não pode prendê-la para sempre."""
    ana, bruno, _ = anotadores(cliente)
    conn = com_banco(cliente.app.state.db_anotacao)
    try:
        alvo = conn.execute(
            "SELECT id FROM tarefas WHERE tipo = 'avaliar_rubrica' AND n_anotacoes_alvo = 1"
        ).fetchone()["id"]
        conn.execute(
            "UPDATE tarefas SET status = 'pausada' WHERE tipo = 'avaliar_rubrica' AND id <> ?",
            (alvo,),
        )
    finally:
        conn.close()

    da_ana = pegar(cliente, ana, "avaliar_rubrica")["tarefa"]
    assert da_ana["tarefa"]["id"] == alvo
    assert pegar(cliente, bruno, "avaliar_rubrica")["tarefa"] is None

    # Empurra o prazo para o passado — é o que o relógio faria em 2 horas.
    conn = com_banco(cliente.app.state.db_anotacao)
    try:
        conn.execute(
            "UPDATE atribuicoes SET expira_em = '2000-01-01T00:00:00.000Z' WHERE id = ?",
            (da_ana["atribuicao_id"],),
        )
    finally:
        conn.close()

    do_bruno = pegar(cliente, bruno, "avaliar_rubrica")["tarefa"]
    assert do_bruno is not None
    assert do_bruno["tarefa"]["id"] == alvo

    conn = com_banco(cliente.app.state.db_anotacao)
    try:
        estado = conn.execute(
            "SELECT status FROM atribuicoes WHERE id = ?", (da_ana["atribuicao_id"],)
        ).fetchone()["status"]
    finally:
        conn.close()
    assert estado == "expirada"


def test_a_fila_respeita_prioridade_e_id(cliente: TestClient) -> None:
    """A ordem do claim é a do ``idx_tarefas_fila`` — prioridade DESC, id."""
    ana = anotadores(cliente)[0]
    vistas = []
    for _ in range(4):
        corpo = pegar(cliente, ana, "comparar_ab")
        if corpo["tarefa"] is None:
            break
        vistas.append(corpo["tarefa"]["tarefa"]["prioridade"])
    assert vistas == sorted(vistas, reverse=True)


def test_abandonar_devolve_a_vaga_na_hora(cliente: TestClient) -> None:
    ana, bruno, _ = anotadores(cliente)
    conn = com_banco(cliente.app.state.db_anotacao)
    try:
        alvo = conn.execute(
            "SELECT id FROM tarefas WHERE tipo = 'sft_resposta'"
        ).fetchone()["id"]
        conn.execute(
            "UPDATE tarefas SET status = 'pausada' WHERE tipo = 'sft_resposta' AND id <> ?",
            (alvo,),
        )
    finally:
        conn.close()

    da_ana = pegar(cliente, ana, "sft_resposta")["tarefa"]
    assert pegar(cliente, bruno, "sft_resposta")["tarefa"] is None
    r = cliente.post(
        f"/api/atribuicoes/{da_ana['atribuicao_id']}/abandonar", json={"anotador_id": ana}
    )
    assert r.status_code == 200, r.text
    assert pegar(cliente, bruno, "sft_resposta")["tarefa"] is not None


def test_contagens_sao_pessoais(cliente: TestClient) -> None:
    """"12 na fila" para quem já anotou as 12 é a forma mais rápida de a tela
    parecer quebrada."""
    ana = anotadores(cliente)[0]
    antes = cliente.get("/api/tarefas/contagens", params={"anotador_id": ana}).json()["contagens"]
    assert set(antes) == set(adb.TIPOS_TAREFA)
    pegar(cliente, ana, "comparar_ab")
    depois = cliente.get("/api/tarefas/contagens", params={"anotador_id": ana}).json()["contagens"]
    assert depois["comparar_ab"] == antes["comparar_ab"] - 1


# ---------------------------------------------------------------------------
# 4. o gabarito e o meta não vazam
# ---------------------------------------------------------------------------


def test_gabarito_nao_sai_no_envelope(cliente: TestClient) -> None:
    """Gabarito visível transforma a tarefa-ouro numa prova de leitura de JSON."""
    ana = anotadores(cliente)[0]
    conn = com_banco(cliente.app.state.db_anotacao)
    try:
        ouro = conn.execute(
            "SELECT id, gabarito_json FROM tarefas "
            "WHERE gabarito_json IS NOT NULL AND tipo = 'comparar_ab' LIMIT 1"
        ).fetchone()
        marca = json.loads(str(ouro["gabarito_json"]))["sobre"][:30]
        conn.execute(
            "UPDATE tarefas SET status = 'pausada' WHERE tipo = 'comparar_ab' AND id <> ?",
            (ouro["id"],),
        )
    finally:
        conn.close()

    resposta = cliente.post(
        "/api/tarefas/proxima", json={"anotador_id": ana, "tipo": "comparar_ab"}
    )
    assert resposta.json()["tarefa"]["tarefa"]["id"] == ouro["id"]
    # Busca no JSON CRU: qualquer aninhamento novo que passasse a carregar o
    # gabarito seria pego aqui, e não só a chave que existe hoje.
    assert "gabarito" not in resposta.text
    assert marca not in resposta.text


def test_o_defeito_plantado_nao_sai_para_o_anotador(cliente: TestClient) -> None:
    """O ``meta_json`` é o gabarito do exercício: vazá-lo transformaria o A/B
    numa prova de leitura de metadado."""
    ana = anotadores(cliente)[0]
    corpo = pegar(cliente, ana, "comparar_ab")
    resposta = corpo["tarefa"]["respostas"]
    assert len(resposta) == 2
    assert {r["rotulo"] for r in resposta} == set(adb.ROTULOS_MODELO)
    assert all(set(r) == {"rotulo", "texto"} for r in resposta)
    assert "defeito_plantado" not in json.dumps(corpo, ensure_ascii=False)


def test_o_envelope_traz_a_proveniencia(cliente: TestClient) -> None:
    """Fonte e licença por prompt: é o argumento inteiro deste projeto."""
    ana = anotadores(cliente)[0]
    prompt = pegar(cliente, ana, "escrever_rubrica")["tarefa"]["prompt"]
    assert prompt["fonte"] and prompt["licenca"] and prompt["licenca_classe"]
    assert prompt["demo"] is False

    demo = pegar(cliente, ana, "comparar_ab")["tarefa"]["prompt"]
    assert demo["demo"] is True
    assert demo["fonte"] == "pacote de demonstração"


# ---------------------------------------------------------------------------
# 5. uid sumido do corpus
# ---------------------------------------------------------------------------


def test_uid_sumido_do_corpus_nao_gera_500(cliente: TestClient, corpus: Path) -> None:
    """O universo desta máquina foi de 144.754 para 159.733 numa recarga.

    Uma tarefa criada antes aponta para um uid que não existe mais. O claim pula
    e marca a tarefa como indisponível; a resposta é 200, nunca 500.
    """
    ana = anotadores(cliente)[0]
    conn = com_banco(cliente.app.state.db_anotacao)
    try:
        conn.execute(
            "UPDATE tarefas SET status = 'pausada' WHERE tipo = 'escrever_rubrica'"
        )
        conn.execute(
            "INSERT INTO tarefas (tipo, prompt_uid, origem, prioridade) "
            "VALUES ('escrever_rubrica', 'sumiu-na-recarga-000', 'semente', 999)"
        )
        orfa = int(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
    finally:
        conn.close()

    r = cliente.post("/api/tarefas/proxima", json={"anotador_id": ana, "tipo": "escrever_rubrica"})
    assert r.status_code == 200, r.text
    assert r.json()["tarefa"] is None
    assert "corpus" in r.json()["motivo"]

    conn = com_banco(cliente.app.state.db_anotacao)
    try:
        linha = conn.execute(
            "SELECT status, payload_json FROM tarefas WHERE id = ?", (orfa,)
        ).fetchone()
    finally:
        conn.close()
    assert str(linha["status"]) == "pausada"
    assert tmod.CHAVE_INDISPONIVEL in json.loads(str(linha["payload_json"]))


def test_uid_sumido_e_pulado_e_a_proxima_boa_sai(cliente: TestClient) -> None:
    """A órfã não pode bloquear a fila: ela é marcada e o claim continua."""
    ana = anotadores(cliente)[0]
    conn = com_banco(cliente.app.state.db_anotacao)
    try:
        conn.execute(
            "INSERT INTO tarefas (tipo, prompt_uid, origem, prioridade) "
            "VALUES ('escrever_rubrica', 'sumiu-tambem-0001', 'semente', 999)"
        )
    finally:
        conn.close()
    corpo = pegar(cliente, ana, "escrever_rubrica")
    assert corpo["tarefa"] is not None
    assert corpo["tarefa"]["prompt"]["uid"] != "sumiu-tambem-0001"


# ---------------------------------------------------------------------------
# 6. payloads
# ---------------------------------------------------------------------------


def rubrica_ok() -> dict[str, Any]:
    return {
        "titulo": "Rubrica de teste para este prompt",
        "criterios": [
            {
                "nome": f"Critério {i}",
                "descricao": "Descrição longa o suficiente para o validador aceitar.",
                "escala_min": 1,
                "escala_max": 5,
                "rotulo_min": "não atende",
                "rotulo_max": "atende plenamente",
            }
            for i in range(3)
        ],
    }


def notas_da_rubrica(env: dict[str, Any], nota: int = 4) -> dict[str, Any]:
    return {
        "notas": [
            {"criterio": c["nome"], "nota": nota} for c in env["rubrica"]["criterios"]
        ]
    }


def submeter(cliente: TestClient, env: dict[str, Any], quem: int, payload: dict[str, Any]):
    return cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
        json={"anotador_id": quem, "payload": payload, "tempo_ativo_ms": 12_345},
    )


@pytest.mark.parametrize(
    ("tipo", "payload"),
    [
        # A pegadinha central: `bool` é subclasse de `int` E o Pydantic coage
        # true -> 1 antes de qualquer validador comum.
        ("avaliar_rubrica", {"notas": [{"criterio": "x", "nota": True}]}),
        ("avaliar_rubrica", {"notas": [{"criterio": "x", "nota": 0}]}),
        ("avaliar_rubrica", {"notas": [{"criterio": "x", "nota": 10}]}),
        ("avaliar_rubrica", {"notas": []}),
        ("avaliar_rubrica", {"notas": [{"criterio": "x", "nota": 3}], "extra": 1}),
        ("escrever_rubrica", {"titulo": "curto", "criterios": []}),
        ("escrever_rubrica", {**rubrica_ok(), "criterios": rubrica_ok()["criterios"][:2]}),
        ("escrever_rubrica", {**rubrica_ok(), "criterios": rubrica_ok()["criterios"] * 4}),
        ("sft_resposta", {"resposta": "curta"}),
        ("sft_resposta", {"resposta": "   "}),
        ("sft_resposta", {"resposta": "x" * 60, "checklist": [{"criterio": "a", "atendido": 3}]}),
        ("comparar_ab", {"preferencia": "modelo-c", "justificativa": "y" * 40}),
        ("comparar_ab", {"preferencia": "modelo-a", "justificativa": "curta"}),
        ("comparar_ab", {"preferencia": "modelo-a"}),
        ("comparar_ab", {"preferencia": "empate", "justificativa": "y" * 40, "nota": 1}),
    ],
)
def test_payload_invalido_por_tipo(tipo: str, payload: dict[str, Any]) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        payloads.escolher(tipo).model_validate(payload)


def test_nota_booleana_da_422_pela_rota(cliente: TestClient) -> None:
    """A prova que importa: não o modelo isolado, a ROTA.

    ``true`` viraria nota 1 em silêncio — uma nota válida, e errada.
    """
    ana = anotadores(cliente)[0]
    env = pegar(cliente, ana, "avaliar_rubrica")["tarefa"]
    nomes = [c["nome"] for c in env["rubrica"]["criterios"]]
    payload = {"notas": [{"criterio": n, "nota": True} for n in nomes]}
    r = submeter(cliente, env, ana, payload)
    assert r.status_code == 422, r.text
    assert "boolean" in r.text.lower() or "booleano" in r.text.lower()


def test_payload_do_tipo_errado_da_422(cliente: TestClient) -> None:
    """O tipo vem do SERVIDOR. Um ``comparar_ab@1`` válido numa tarefa de rubrica
    seria um payload perfeito gravado no schema errado — e o defeito só
    apareceria no export, meses depois."""
    ana = anotadores(cliente)[0]
    env = pegar(cliente, ana, "avaliar_rubrica")["tarefa"]
    r = submeter(cliente, env, ana, {"preferencia": "modelo-a", "justificativa": "z" * 40})
    assert r.status_code == 422, r.text


def test_o_botao_e_a_rota_concordam_sobre_o_que_falta(cliente: TestClient) -> None:
    """Faltando critério, o 422 DIZ quantos e quais — é a mesma frase que o botão
    da tela mostra antes do clique."""
    ana = anotadores(cliente)[0]
    env = pegar(cliente, ana, "avaliar_rubrica")["tarefa"]
    parcial = notas_da_rubrica(env)
    parcial["notas"] = parcial["notas"][:1]
    r = submeter(cliente, env, ana, parcial)
    assert r.status_code == 422
    assert "sem nota" in r.json()["detail"]


def test_nota_fora_da_escala_do_criterio_da_422(cliente: TestClient) -> None:
    """1..9 é o teto da plataforma; a escala REAL é a da rubrica, e quem a
    confere é a rota, que tem a rubrica em mãos."""
    ana = anotadores(cliente)[0]
    env = pegar(cliente, ana, "avaliar_rubrica")["tarefa"]
    r = submeter(cliente, env, ana, notas_da_rubrica(env, nota=9))
    assert r.status_code == 422
    assert "aceita de" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 7. submissão
# ---------------------------------------------------------------------------


def test_submeter_grava_versao_1_e_o_schema(cliente: TestClient) -> None:
    ana = anotadores(cliente)[0]
    env = pegar(cliente, ana, "avaliar_rubrica")["tarefa"]
    r = submeter(cliente, env, ana, notas_da_rubrica(env))
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["versao"] == 1

    conn = com_banco(cliente.app.state.db_anotacao)
    try:
        linha = conn.execute(
            "SELECT payload_schema, payload_json, tempo_ativo_ms FROM anotacoes WHERE id = ?",
            (corpo["anotacao_id"],),
        ).fetchone()
        estado = conn.execute(
            "SELECT status FROM atribuicoes WHERE id = ?", (env["atribuicao_id"],)
        ).fetchone()["status"]
    finally:
        conn.close()
    assert str(linha["payload_schema"]) == "avaliar_rubrica@1"
    assert int(linha["tempo_ativo_ms"]) == 12_345
    assert json.loads(str(linha["payload_json"]))["notas"]
    assert str(estado) == "submetida"


def test_submeter_duas_vezes_da_409(cliente: TestClient) -> None:
    ana = anotadores(cliente)[0]
    env = pegar(cliente, ana, "avaliar_rubrica")["tarefa"]
    assert submeter(cliente, env, ana, notas_da_rubrica(env)).status_code == 200
    assert submeter(cliente, env, ana, notas_da_rubrica(env)).status_code == 409


def test_atribuicao_de_outra_pessoa_da_403(cliente: TestClient) -> None:
    ana, bruno, _ = anotadores(cliente)
    env = pegar(cliente, ana, "avaliar_rubrica")["tarefa"]
    r = submeter(cliente, env, bruno, notas_da_rubrica(env))
    assert r.status_code == 403


def test_prazo_vencido_nao_descarta_trabalho_submetido(cliente: TestClient) -> None:
    """O TTL devolve a VAGA; ele não existe para punir quem demorou.

    Jogar fora uma anotação que já chegou por causa de um relógio é o pior
    desfecho possível numa plataforma cujo insumo é trabalho humano.
    """
    ana = anotadores(cliente)[0]
    env = pegar(cliente, ana, "avaliar_rubrica")["tarefa"]
    conn = com_banco(cliente.app.state.db_anotacao)
    try:
        conn.execute(
            "UPDATE atribuicoes SET expira_em = '2000-01-01T00:00:00.000Z' WHERE id = ?",
            (env["atribuicao_id"],),
        )
    finally:
        conn.close()
    r = submeter(cliente, env, ana, notas_da_rubrica(env))
    assert r.status_code == 200, r.text
    assert r.json()["expirou"] is True


def test_escrever_rubrica_oferece_a_continuacao(cliente: TestClient) -> None:
    """As duas abas se encadeiam: quem escreveu a rubrica é quem melhor sabe
    qual resposta ela pede."""
    ana = anotadores(cliente)[0]
    env = pegar(cliente, ana, "escrever_rubrica")["tarefa"]
    r = submeter(cliente, env, ana, rubrica_ok())
    assert r.status_code == 200, r.text
    cont = r.json()["continuacao"]
    assert cont["tipo"] == "sft_resposta"
    assert cont["prompt_uid"] == env["prompt"]["uid"]
    assert cont["origem"] == "continuacao"

    aceita = cliente.post(
        "/api/tarefas/livre",
        json={
            "anotador_id": ana,
            "tipo": cont["tipo"],
            "prompt_uid": cont["prompt_uid"],
            "origem": "continuacao",
        },
    )
    assert aceita.status_code == 200, aceita.text
    seguinte = aceita.json()["tarefa"]
    assert seguinte["tarefa"]["origem"] == "continuacao"
    # A rubrica que ela acabou de propor aparece ao lado, como checklist — antes
    # de qualquer revisor aprovar coisa nenhuma.
    assert seguinte["rubrica"] is not None
    assert seguinte["rubrica"]["origem"] == "anotacao"
    assert len(seguinte["rubrica"]["criterios"]) == 3


def test_os_quatro_tipos_fecham_o_ciclo(cliente: TestClient) -> None:
    """Uma tarefa de CADA tipo, completada pela fila. É a DoD do P2 em teste.

    **Os quatro de TEXTO PARADO.** Os dois de conversa (P4d) só existem depois de
    um modelo local responder, e o ciclo deles é fechado em
    ``test_annotate_p4d.py``, com o cliente HTTP trocado — um teste que precisasse
    do Ollama de pé passaria ou falharia conforme a máquina de quem o rodou.
    """
    ana = anotadores(cliente)[0]
    gravados: dict[str, str] = {}
    for tipo in [t for t in adb.TIPOS_TAREFA if t not in adb.TIPOS_CONVERSA]:
        env = pegar(cliente, ana, tipo)["tarefa"]
        assert env is not None, tipo
        if tipo == "avaliar_rubrica":
            payload: dict[str, Any] = notas_da_rubrica(env)
        elif tipo == "escrever_rubrica":
            payload = rubrica_ok()
        elif tipo == "sft_resposta":
            payload = {"resposta": "Uma resposta de referência com tamanho suficiente. " * 3}
        else:
            payload = {
                "preferencia": "modelo-b",
                "justificativa": "A resposta B responde ao pedido e a A inventa um dado.",
            }
        r = submeter(cliente, env, ana, payload)
        assert r.status_code == 200, (tipo, r.text)
        gravados[tipo] = f"{tipo}@1"

    conn = com_banco(cliente.app.state.db_anotacao)
    try:
        esquemas = {
            str(r["payload_schema"]) for r in conn.execute("SELECT payload_schema FROM anotacoes")
        }
    finally:
        conn.close()
    assert esquemas == set(gravados.values())


# ---------------------------------------------------------------------------
# 8. catálogo e modo livre
# ---------------------------------------------------------------------------


def test_catalogo_lista_o_pool_com_ja_anotei(cliente: TestClient) -> None:
    ana = anotadores(cliente)[0]
    r = cliente.get(
        "/api/catalogo", params={"anotador_id": ana, "tipo": "comparar_ab", "page": 1}
    )
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["total"] > 0
    assert corpo["page_size"] == cat.page_size()
    assert all("ja_anotei" in item for item in corpo["items"])
    assert corpo["fontes"], "sem fontes o terceiro filtro seria um select vazio"


def test_catalogo_marca_o_que_eu_ja_anotei(cliente: TestClient) -> None:
    """Por (tipo, anotador): avaliar com rubrica e comparar A/B sobre o mesmo
    texto são trabalhos diferentes."""
    ana = anotadores(cliente)[0]
    env = pegar(cliente, ana, "escrever_rubrica")["tarefa"]
    uid = env["prompt"]["uid"]

    def marcado(tipo: str) -> bool:
        corpo = cliente.get(
            "/api/catalogo", params={"anotador_id": ana, "tipo": tipo, "page": 1}
        ).json()
        for pagina in range(1, corpo["pages"] + 1):
            atual = cliente.get(
                "/api/catalogo", params={"anotador_id": ana, "tipo": tipo, "page": pagina}
            ).json()
            for item in atual["items"]:
                if item["uid"] == uid:
                    return bool(item["ja_anotei"])
        raise AssertionError(f"{uid} não apareceu no catálogo")

    assert marcado("escrever_rubrica") is True
    assert marcado("comparar_ab") is False


def test_catalogo_sobrevive_a_busca_hostil(cliente: TestClient) -> None:
    """O sanitizador do FTS5 é reusado de ``app/queries``, não reimplementado.

    ``e-mail`` sozinho derrubaria a busca com ``no such column: mail``.
    """
    ana = anotadores(cliente)[0]
    for q in ("e-mail", 'a" OR b', "***", "NEAR(", "^inicio", "  ", "coração*"):
        r = cliente.get(
            "/api/catalogo", params={"anotador_id": ana, "tipo": "comparar_ab", "q": q}
        )
        assert r.status_code == 200, (q, r.text)


def test_catalogo_filtra_por_tamanho_e_idioma(cliente: TestClient) -> None:
    ana = anotadores(cliente)[0]
    base = {"anotador_id": ana, "tipo": "comparar_ab"}
    curtos = cliente.get("/api/catalogo", params={**base, "faixa": "curto"}).json()
    for item in curtos["items"]:
        assert item["n_chars"] < cat.FAIXAS["curto"][1]
    so_pt = cliente.get("/api/catalogo", params={**base, "lang": "pt"}).json()
    assert all(item["lang"] == "pt" for item in so_pt["items"])
    assert cliente.get("/api/catalogo", params={**base, "faixa": "gigante"}).status_code == 422


def test_livre_cria_a_tarefa_e_recusa_repetir(cliente: TestClient) -> None:
    ana = anotadores(cliente)[0]
    # Um uid que o seed NÃO cobriu: os oito primeiros do pool já têm tarefa de
    # `escrever_rubrica`, e sobre eles o find-or-create REUSA a da semente (que é
    # o comportamento certo, e é o que o teste seguinte cobra). Aqui o que se
    # prova é a CRIAÇÃO.
    conn = com_banco(cliente.app.state.db_anotacao)
    try:
        ja_tem = {str(r["prompt_uid"]) for r in conn.execute("SELECT prompt_uid FROM tarefas")}
    finally:
        conn.close()
    corpo = cliente.get(
        "/api/catalogo", params={"anotador_id": ana, "tipo": "comparar_ab", "page": 2}
    ).json()
    uid = next(item["uid"] for item in corpo["items"] if item["uid"] not in ja_tem)

    # `escrever_rubrica`: é o que um prompt cru do corpus sustenta — ele não tem
    # rubrica nem resposta de modelo, e é justamente por isso que alguém escreve
    # a rubrica dele.
    r = cliente.post(
        "/api/tarefas/livre",
        json={"anotador_id": ana, "tipo": "escrever_rubrica", "prompt_uid": uid},
    )
    assert r.status_code == 200, r.text
    env = r.json()["tarefa"]
    assert env["tarefa"]["origem"] == "livre"
    # Sem prazo: ninguém está esperando por uma tarefa que a pessoa escolheu.
    assert env["expira_em"] is None

    # Reabrir a mesma antes de enviar devolve a MESMA atribuição (retomada).
    de_novo = cliente.post(
        "/api/tarefas/livre",
        json={"anotador_id": ana, "tipo": "escrever_rubrica", "prompt_uid": uid},
    )
    assert de_novo.json()["tarefa"]["atribuicao_id"] == env["atribuicao_id"]
    assert de_novo.json()["novo"] is False

    assert submeter(cliente, env, ana, rubrica_ok()).status_code == 200
    conflito = cliente.post(
        "/api/tarefas/livre",
        json={"anotador_id": ana, "tipo": "escrever_rubrica", "prompt_uid": uid},
    )
    assert conflito.status_code == 409
    assert "já anotou" in conflito.json()["detail"]


def test_livre_reusa_a_tarefa_da_semente_em_vez_de_duplicar(cliente: TestClient) -> None:
    """Find-or-create pela chave ``(tipo, prompt_uid)``, não pela origem.

    Criar uma segunda tarefa "livre" sobre um par que a semente já cobriu faria a
    mesma anotação contar duas vezes em toda métrica e quebraria o ``ja_anotei``.
    """
    ana = anotadores(cliente)[0]
    conn = com_banco(cliente.app.state.db_anotacao)
    try:
        linha = conn.execute(
            "SELECT id, prompt_uid FROM tarefas WHERE tipo = 'escrever_rubrica' "
            "AND origem = 'semente' AND prompt_uid NOT LIKE 'demo:%' LIMIT 1"
        ).fetchone()
        antes = int(conn.execute("SELECT count(*) AS n FROM tarefas").fetchone()["n"])
    finally:
        conn.close()

    r = cliente.post(
        "/api/tarefas/livre",
        json={"anotador_id": ana, "tipo": "escrever_rubrica", "prompt_uid": str(linha["prompt_uid"])},
    )
    assert r.status_code == 200, r.text
    assert r.json()["tarefa"]["tarefa"]["id"] == int(linha["id"])
    assert r.json()["tarefa"]["tarefa"]["origem"] == "semente"

    conn = com_banco(cliente.app.state.db_anotacao)
    try:
        assert int(conn.execute("SELECT count(*) AS n FROM tarefas").fetchone()["n"]) == antes
    finally:
        conn.close()


def test_catalogo_diz_o_que_cada_prompt_sustenta(cliente: TestClient) -> None:
    """Um prompt cru do corpus não tem resposta de modelo nenhuma.

    Sem o ``pode``, o botão "Anotar este" numa aba de comparação A/B abriria um
    workspace de duas colunas que nunca vão existir — o beco que esta plataforma
    não pode ter. O catálogo desabilita ANTES do clique.
    """
    ana = anotadores(cliente)[0]
    corpo = cliente.get(
        "/api/catalogo", params={"anotador_id": ana, "tipo": "comparar_ab"}
    ).json()
    for item in corpo["items"]:
        # Os quatro que um prompt CRU sustenta: os dois de escrita e os dois de
        # conversa (P4d), que geram a resposta na hora e cuja rubrica é da
        # plataforma. `avaliar_rubrica` e `comparar_ab` continuam de fora — eles
        # exigem material que um prompt cru do corpus não tem.
        assert set(item["pode"]) == {
            "escrever_rubrica",
            "sft_resposta",
            "conversa_modelo",
            "duelo_modelos",
        }, item["uid"]
        assert item["n_respostas"] == 0
        assert item["tem_rubrica"] is False


def test_material_do_pacote_sustenta_os_quatro_tipos(cliente: TestClient) -> None:
    """O simétrico: as fixtures TÊM rubrica e duas respostas, e por isso o
    pacote é o que abre a demonstração."""
    conn = com_banco(cliente.app.state.db_anotacao)
    try:
        uid = str(conn.execute("SELECT uid FROM prompts_demo LIMIT 1").fetchone()["uid"])
        item: dict[str, Any] = {"uid": uid}
        cat.marcar_material(conn, [item])
    finally:
        conn.close()
    assert set(item["pode"]) == set(adb.TIPOS_TAREFA)
    assert item["n_respostas"] == 2 and item["tem_rubrica"] is True


def test_livre_recusa_tipo_que_o_prompt_nao_sustenta(cliente: TestClient) -> None:
    """E o servidor recusa pela MESMA regra — um botão desabilitado que, forçado
    pela API, criasse a tarefa mesmo assim seria duas verdades diferentes."""
    ana = anotadores(cliente)[0]
    uid = cliente.get(
        "/api/catalogo", params={"anotador_id": ana, "tipo": "comparar_ab"}
    ).json()["items"][0]["uid"]
    r = cliente.post(
        "/api/tarefas/livre",
        json={"anotador_id": ana, "tipo": "comparar_ab", "prompt_uid": uid},
    )
    assert r.status_code == 409
    assert "duas" in r.json()["detail"]
    # E o que ele sustenta continua abrindo normalmente.
    ok = cliente.post(
        "/api/tarefas/livre",
        json={"anotador_id": ana, "tipo": "escrever_rubrica", "prompt_uid": uid},
    )
    assert ok.status_code == 200, ok.text


def test_livre_recusa_uid_fora_do_pool(cliente: TestClient) -> None:
    """O modo livre não pode ser uma porta lateral para os prompts que o filtro
    do pool exclui de propósito (NSFW, robô repetido, 1 MB de texto)."""
    ana = anotadores(cliente)[0]
    for uid in ("nsfw000000000001", "nao-existe-em-lugar-nenhum", "demo:naoexiste"):
        r = cliente.post(
            "/api/tarefas/livre",
            json={"anotador_id": ana, "tipo": "comparar_ab", "prompt_uid": uid},
        )
        assert r.status_code == 404, (uid, r.text)


def test_livre_recusa_carimbar_origem_de_semente(cliente: TestClient) -> None:
    """Deixar o cliente dizer "isto é da semente" falsificaria a origem do
    trabalho em todo relatório do admin."""
    ana = anotadores(cliente)[0]
    uid = cliente.get(
        "/api/catalogo", params={"anotador_id": ana, "tipo": "comparar_ab"}
    ).json()["items"][0]["uid"]
    r = cliente.post(
        "/api/tarefas/livre",
        json={"anotador_id": ana, "tipo": "comparar_ab", "prompt_uid": uid, "origem": "semente"},
    )
    assert r.status_code == 422


def test_prompt_fora_do_pool_da_404(cliente: TestClient) -> None:
    assert cliente.get("/api/prompts/nsfw000000000001").status_code == 404
    ana = anotadores(cliente)[0]
    uid = cliente.get(
        "/api/catalogo", params={"anotador_id": ana, "tipo": "comparar_ab"}
    ).json()["items"][0]["uid"]
    corpo = cliente.get(f"/api/prompts/{uid}").json()
    assert corpo["uid"] == uid and corpo["text"]


def test_prompt_demo_resolve_pelo_prefixo(cliente: TestClient) -> None:
    conn = com_banco(cliente.app.state.db_anotacao)
    try:
        uid = str(conn.execute("SELECT uid FROM prompts_demo LIMIT 1").fetchone()["uid"])
    finally:
        conn.close()
    assert adb.e_demo(uid)
    corpo = cliente.get(f"/api/prompts/{uid}").json()
    assert corpo["demo"] is True


# ---------------------------------------------------------------------------
# 9. papéis e listagem
# ---------------------------------------------------------------------------


def test_so_anotador_anota(cliente: TestClient) -> None:
    """Papel sem senha é teatro de autorização — mas teatro coerente, e o
    servidor lê o papel da LINHA, nunca do que o cliente afirma."""
    ids = perfis(cliente)
    for nome in ("Diego Prado", "Marina Alves"):
        r = cliente.post(
            "/api/tarefas/proxima", json={"anotador_id": ids[nome], "tipo": "comparar_ab"}
        )
        assert r.status_code == 403, nome
    r = cliente.post("/api/tarefas/proxima", json={"anotador_id": 9999, "tipo": "comparar_ab"})
    assert r.status_code == 404


def test_minhas_tarefas_lista_com_trecho_e_estado(cliente: TestClient) -> None:
    ana = anotadores(cliente)[0]
    env = pegar(cliente, ana, "comparar_ab")["tarefa"]
    submeter(
        cliente,
        env,
        ana,
        {"preferencia": "modelo-b", "justificativa": "B cumpre o pedido e A ignora a restrição."},
    )
    corpo = cliente.get("/api/atribuicoes", params={"anotador_id": ana}).json()
    assert corpo["total"] == 1
    item = corpo["items"][0]
    assert item["status"] == "submetida"
    assert item["versao"] == 1
    assert item["disponivel"] is True
    assert item["prompt"]
    # O comentário do revisor já vem no contrato (P3 preenche).
    assert "comentario_revisor" in item

    filtrado = cliente.get(
        "/api/atribuicoes", params={"anotador_id": ana, "status": "abandonada"}
    ).json()
    assert filtrado["total"] == 0
    assert (
        cliente.get("/api/atribuicoes", params={"anotador_id": ana, "status": "xpto"}).status_code
        == 422
    )


def test_minhas_tarefas_aguenta_uid_sumido(cliente: TestClient) -> None:
    """Uid sumido vira ``prompt: null`` com ``disponivel: false``, nunca 500."""
    ana = anotadores(cliente)[0]
    env = pegar(cliente, ana, "comparar_ab")["tarefa"]
    conn = com_banco(cliente.app.state.db_anotacao)
    try:
        conn.execute(
            "UPDATE tarefas SET prompt_uid = 'evaporou000001' "
            "WHERE id = (SELECT tarefa_id FROM atribuicoes WHERE id = ?)",
            (env["atribuicao_id"],),
        )
    finally:
        conn.close()
    corpo = cliente.get("/api/atribuicoes", params={"anotador_id": ana})
    assert corpo.status_code == 200, corpo.text
    item = corpo.json()["items"][0]
    assert item["disponivel"] is False
    assert item["prompt"] is None


def test_reabrir_atribuicao_devolve_o_mesmo_envelope(cliente: TestClient) -> None:
    """"Continuar de onde parei" — e o caminho do re-trabalho do P3."""
    ana = anotadores(cliente)[0]
    env = pegar(cliente, ana, "sft_resposta")["tarefa"]
    r = cliente.get(
        f"/api/atribuicoes/{env['atribuicao_id']}", params={"anotador_id": ana}
    )
    assert r.status_code == 200, r.text
    assert r.json()["tarefa"]["tarefa"]["id"] == env["tarefa"]["id"]
    assert r.json()["tarefa"]["versao_anterior"] is None


def test_o_corpus_continua_intocado_depois_de_tudo(cliente: TestClient, corpus: Path) -> None:
    """A garantia central da app, reconferida DEPOIS de um ciclo inteiro de
    escrita no banco da plataforma."""
    ana = anotadores(cliente)[0]
    env = pegar(cliente, ana, "escrever_rubrica")["tarefa"]
    submeter(cliente, env, ana, rubrica_ok())
    conn = dbmod.connect(corpus, readonly=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE prompts SET text = 'destruido'")
        assert int(conn.execute("SELECT count(*) AS n FROM prompts").fetchone()["n"]) > 0
    finally:
        conn.close()
