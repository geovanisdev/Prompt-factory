"""P5a: o painel do admin — as métricas e a geração de tarefas em lote.

Cada família aqui existe por uma coisa que, se der errado, **não parece
quebrada** — que é o único tipo de defeito que este projeto persegue com teste:

* **o painel calculando por conta própria.** Os números da tela e os do
  ``quality-report`` que o cliente recebe têm de sair da MESMA função. Duas
  implementações da mesma pergunta não dão erro: elas dão dois números, e a
  divergência só aparece como "a tela dizia 12 e o lote entregue tinha 9". Por
  isso os testes recomputam ``entrega.panorama``/``calibracao_do_revisor``/
  ``triagem_deixou_passar`` sobre o mesmo banco e exigem **igualdade de dict**;
* **a prévia escrevendo.** Um ``GET`` que cria tarefa é a pior variante do
  problema: ninguém desconfia de uma prévia, e a fila cresceria a cada olhada;
* **a geração duplicando a fila.** Dois cliques no mesmo lote dobrariam o
  trabalho sobre os mesmos prompts, o ``ja_anotei`` do catálogo passaria a
  mentir e toda métrica por pessoa contaria o mesmo item duas vezes;
* **tarefa gerada sobre prompt sem material.** ``avaliar_rubrica`` sem rubrica
  ativa abre num workspace vazio. A regra é de ``catalogo.marcar_material`` — a
  MESMA que desabilita o botão do catálogo —, e uma segunda cópia aqui produziria
  botão habilitado que leva a beco;
* **o evento fora da transação.** Um INSERT depois do COMMIT some quando o
  commit falha, e a trilha passa a mentir justamente nos casos interessantes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from prompt_factory import db as dbmod
from prompt_factory.annotate import concordancia as conmod
from prompt_factory.annotate import db as adb
from prompt_factory.annotate import entrega as entmod
from prompt_factory.annotate import eventos as evmod
from prompt_factory.annotate import geracao as gmod
from prompt_factory.annotate import seed as seedmod
from prompt_factory.annotate.main import criar_app

from .test_annotate_p2 import notas_da_rubrica, rubrica_ok
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
    return dbmod.connect(caminho)


# ---------------------------------------------------------------------------
# helpers de cenário
# ---------------------------------------------------------------------------


def papeis(cliente: TestClient) -> dict[str, list[int]]:
    """``{papel: [ids]}`` das seis personas semeadas."""
    saida: dict[str, list[int]] = {}
    for p in cliente.get("/api/perfis").json()["items"]:
        saida.setdefault(p["papel"], []).append(p["id"])
    return saida


def admin_de(cliente: TestClient) -> int:
    return papeis(cliente)["admin"][0]


def anotar(cliente: TestClient, quem: int, tipo: str = "avaliar_rubrica") -> dict[str, Any]:
    """Pega da fila e submete. Devolve ``{atribuicao_id, anotacao_id, env}``."""
    env = cliente.post(
        "/api/tarefas/proxima", json={"anotador_id": quem, "tipo": tipo}
    ).json()["tarefa"]
    assert env is not None, f"fila de {tipo} vazia para {quem}"
    payload = notas_da_rubrica(env) if tipo == "avaliar_rubrica" else rubrica_ok()
    r = cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
        json={"anotador_id": quem, "payload": payload, "tempo_ativo_ms": 30_000},
    )
    assert r.status_code == 200, r.text
    return {"atribuicao_id": env["atribuicao_id"], "env": env, **r.json()}


def triar(cliente: TestClient, revisor: int, anotacao_id: int) -> None:
    r = cliente.post(
        f"/api/revisao/{anotacao_id}", json={"revisor_id": revisor, "veredito": "aprovada"}
    )
    assert r.status_code == 200, r.text


def avaliar(
    cliente: TestClient,
    revisor: int,
    anotacao_id: int,
    *,
    antes: str = "adequado",
    depois: str = "adequado",
) -> dict[str, Any]:
    r = cliente.post(
        f"/api/avaliacao/{anotacao_id}",
        json={
            "revisor_id": revisor,
            "avaliacao_antes": antes,
            "avaliacao_depois": depois,
            "justificativa": (
                "The scores follow the guide and the text supports each one of them."
            ),
            "tempo_ativo_ms": 9_000,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def ciclo_completo(
    cliente: TestClient,
    quem: int,
    revisor: int,
    tipo: str = "avaliar_rubrica",
    **notas: str,
) -> dict[str, Any]:
    """Anotar → triar → avaliar. O item sai em ``avaliada`` (ou escalado)."""
    a = anotar(cliente, quem, tipo)
    triar(cliente, revisor, a["anotacao_id"])
    return {**a, "avaliacao": avaliar(cliente, revisor, a["anotacao_id"], **notas)}


def uids_do_pool_gravado(banco: Path) -> list[str]:
    """O pool como a plataforma o materializou, na ordem determinística dele.

    Lido da tabela, e não resolvido de novo no corpus: é exatamente a lista de
    onde a geração parte, e recomputá-la aqui testaria outra coisa.
    """
    conn = com_banco(banco)
    try:
        return [str(linha["uid"]) for linha in conn.execute("SELECT uid FROM pool ORDER BY ordem")]
    finally:
        conn.close()


def plantar_material(banco: Path, uid: str) -> None:
    """Rubrica ativa + duas respostas num prompt, pela MESMA função da campanha.

    ``geracao.gravar_material`` é quem o P4c usa; escrever o INSERT à mão aqui
    criaria material com ``origem`` diferente e o teste deixaria de descrever o
    caminho real.
    """
    conn = com_banco(banco)
    try:
        gmod.gravar_material(
            conn,
            [
                {
                    "uid": uid,
                    "rubrica": {
                        "titulo": "Scoring guide planted by the batch test",
                        "criterios": [
                            {
                                "nome": f"Criterion {i}",
                                "descricao": "Long enough description for the validator.",
                                "escala": {
                                    "min": 1,
                                    "max": 5,
                                    "ancoras": [
                                        {"valor": 1, "rotulo": "does not address it"},
                                        {"valor": 5, "rotulo": "addresses it fully"},
                                    ],
                                },
                            }
                            for i in range(3)
                        ],
                    },
                    "respostas": [
                        {
                            "rotulo_modelo": rotulo,
                            "texto": "A model answer long enough to be judged on its merits.",
                            "meta": {"modelo": "teste", "defeito_classe": "nenhum"},
                        }
                        for rotulo in ("modelo-a", "modelo-b")
                    ],
                }
            ],
            "p5a",
        )
    finally:
        conn.close()


def itens_entregaveis(banco: Path, corpus: Path) -> list[dict[str, Any]]:
    """O que ``entrega.coletar`` vê — a MESMA fonte que a rota de métricas usa."""
    conn = com_banco(banco)
    corpo = dbmod.connect(corpus, readonly=True)
    try:
        return entmod.coletar(
            conn, corpo, incluir_sinteticas=True, incluir_pendentes=True
        )
    finally:
        conn.close()
        corpo.close()


# ---------------------------------------------------------------------------
# 1. O PAPEL É DO SERVIDOR
# ---------------------------------------------------------------------------

#: As quatro rotas do painel, com o mínimo de parâmetros para chegarem à guarda.
ROTAS_ADMIN: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("GET", "/api/admin/metricas", {}),
    ("GET", "/api/admin/tarefas/previa", {"tipo": "sft_resposta"}),
    ("GET", "/api/admin/eventos", {"entidade": "anotacao", "entidade_id": 1}),
)


def test_metricas_exige_admin(cliente: TestClient) -> None:
    """403 para quem não é admin, e o papel vem da LINHA em ``anotadores``.

    O painel mostra a calibração de quem revisa e a taxa de re-trabalho por
    pessoa: são números sobre o trabalho dos outros. Sem senha, a identidade é
    escolhida na tela — a autorização, nunca.
    """
    p = papeis(cliente)
    admin = p["admin"][0]
    for metodo, rota, extra in ROTAS_ADMIN:
        for quem in (p["anotador"][0], p["revisor"][0]):
            r = cliente.request(metodo, rota, params={"admin_id": quem, **extra})
            assert r.status_code == 403, f"{rota} com {quem}: {r.status_code}"
        r = cliente.request(metodo, rota, params={"admin_id": admin, **extra})
        assert r.status_code == 200, f"{rota}: {r.text}"

    # E o POST, que é o único que escreve.
    corpo = {"tipo": "sft_resposta", "n": 1}
    assert cliente.post(
        "/api/admin/tarefas", json={**corpo, "admin_id": p["anotador"][0]}
    ).status_code == 403
    assert cliente.post(
        "/api/admin/tarefas", json={**corpo, "admin_id": p["revisor"][0]}
    ).status_code == 403
    assert cliente.post(
        "/api/admin/tarefas", json={**corpo, "admin_id": admin}
    ).status_code == 201


# ---------------------------------------------------------------------------
# 2. OS NÚMEROS DA TELA SÃO OS DO ARTEFATO ENTREGUE
# ---------------------------------------------------------------------------


def test_metricas_batem_com_cenario(
    cliente: TestClient, semeado: Path, corpus: Path
) -> None:
    """Um ciclo completo pela API, e o painel recomputado sobre o mesmo banco.

    A comparação é de **dict inteiro**, e não de dois ou três campos escolhidos
    a dedo: o defeito que este teste persegue é alguém reimplementar uma das
    contas no lado da rota, e uma comparação parcial passaria enquanto a conta
    reimplementada divergisse num campo que ninguém lembrou de conferir.
    """
    p = papeis(cliente)
    ana, bruno = p["anotador"][0], p["anotador"][1]
    diego = p["revisor"][0]
    admin = p["admin"][0]

    ciclo_completo(cliente, ana, diego)
    ciclo_completo(cliente, bruno, diego)
    # Um terceiro que passa na triagem e PARA — é o `pendente_avaliacao` que só
    # entra no painel porque ele é uma prova de QC, não um artefato de dado.
    parado = anotar(cliente, ana, "escrever_rubrica")
    triar(cliente, diego, parado["anotacao_id"])

    r = cliente.get("/api/admin/metricas", params={"admin_id": admin})
    assert r.status_code == 200, r.text
    painel = r.json()

    itens = itens_entregaveis(semeado, corpus)
    assert painel["n_itens"] == len(itens) == 3
    assert painel["panorama"] == entmod.panorama(itens)
    assert painel["calibracao_revisor"] == entmod.calibracao_do_revisor(itens)
    assert painel["vazamento_triagem"] == entmod.triagem_deixou_passar(itens)

    # SEM SINTÉTICA, a calibração é "não medida" — nunca zero. A mesma lição do
    # agreement do M5: confundir os dois reprovaria o revisor antes de medi-lo.
    assert painel["calibracao_revisor"]["n"] == 0
    assert "exact_rate" not in painel["calibracao_revisor"]

    # ---- por pessoa: a medida que não existe no artefato ----
    por_nome = {linha["anotador"]: linha for linha in painel["por_anotador"]}
    assert len(por_nome) == 2, painel["por_anotador"]
    nome_ana = next(x["anotador"] for x in painel["por_anotador"] if x["anotador_id"] == ana)
    assert por_nome[nome_ana]["n"] == 2          # a avaliada + a que parou na triagem
    assert por_nome[nome_ana]["mediana_tempo_ativo_ms"] == 30_000
    assert por_nome[nome_ana]["retrabalho"] == 0
    assert por_nome[nome_ana]["taxa_retrabalho"] == 0.0
    # Ordenado por volume: quem tem mais vem primeiro.
    assert painel["por_anotador"][0]["n"] >= painel["por_anotador"][-1]["n"]

    # ---- e o relatório de qualidade lê os MESMOS itens ----
    texto = entmod.relatorio_qualidade(
        itens,
        {
            "created_at": "2026-08-03T00:00:00Z",
            "statuses": list(entmod.STATUS_ENTREGUE) + list(entmod.STATUS_PARCIAL),
            "synthetic_signal": "x",
        },
    )
    assert f"{painel['panorama']['total']} annotation(s)" in texto
    assert "cannot be measured" in texto


def test_metricas_declaram_a_composicao_e_a_cobertura(cliente: TestClient) -> None:
    """O painel carrega o SINAL ÚNICO de sintética e a cobertura do pool.

    ``geracao.composicao`` é onde ``gabarito_avaliacao_json IS NOT NULL`` vira
    número, e é a mesma função que o dataset card chama. Uma segunda contagem no
    painel divergiria dela — e no dia em que divergisse ninguém saberia qual
    acreditar.
    """
    r = cliente.get("/api/admin/metricas", params={"admin_id": admin_de(cliente)})
    comp = r.json()["composicao"]
    assert comp["sinal"].endswith("IS NOT NULL")
    assert comp["humanas"] + comp["sinteticas"] == comp["anotacoes"]
    cobertura = r.json()["cobertura_pool"]
    assert set(cobertura) >= {"pool", "avaliar_rubrica", "comparar_ab"}
    assert r.json()["duelos"]["n_rodadas"] == 0


# ---------------------------------------------------------------------------
# 3. A PRÉVIA NÃO ESCREVE
# ---------------------------------------------------------------------------


def test_previa_nao_escreve(cliente: TestClient, semeado: Path) -> None:
    """Um ``GET`` que cria tarefa é a pior variante do problema: ninguém
    desconfia de uma prévia, e a fila cresceria a cada olhada.

    O ``/api/health`` vem ANTES do retrato: é ele que materializa o pool (a
    tabela ``pool`` + o evento ``pool_materializado``), e contar antes disso
    acusaria a materialização como escrita da prévia.
    """
    admin = admin_de(cliente)
    assert cliente.get("/api/health").status_code == 200

    conn = com_banco(semeado)
    try:
        antes = adb.contagens(conn)
    finally:
        conn.close()

    for tipo in ("sft_resposta", "avaliar_rubrica", "comparar_ab"):
        r = cliente.get(
            "/api/admin/tarefas/previa",
            params={"admin_id": admin, "tipo": tipo, "n": 50},
        )
        assert r.status_code == 200, r.text

    conn = com_banco(semeado)
    try:
        depois = adb.contagens(conn)
    finally:
        conn.close()
    assert antes == depois, "a prévia escreveu no banco"


def test_a_previa_promete_o_que_o_post_entrega(cliente: TestClient) -> None:
    """Um formulário que estima com um conjunto de parâmetros e cria com outro é
    um formulário que mente — e ninguém confere prévia depois."""
    admin = admin_de(cliente)
    params = {"admin_id": admin, "tipo": "sft_resposta", "n": 4, "lang": "pt"}
    previa = cliente.get("/api/admin/tarefas/previa", params=params).json()
    criado = cliente.post("/api/admin/tarefas", json=params).json()
    assert criado["criadas"] == previa["criaria"]
    assert criado["puladas_material"] == previa["puladas_material"]
    assert criado["puladas_existentes"] == previa["puladas_existentes"]
    assert criado["fora_filtro"] == previa["fora_filtro"]
    # E a amostra é DO recorte: no máximo dez, e todas no idioma pedido.
    assert 0 < len(previa["amostra"]) <= 10
    assert {item["lang"] for item in previa["amostra"]} == {"pt"}


# ---------------------------------------------------------------------------
# 4. A GERAÇÃO
# ---------------------------------------------------------------------------


def test_gerar_cria_com_origem_admin(cliente: TestClient, semeado: Path) -> None:
    """``origem='admin'`` separa esta população da do seed, e o ``db_build_id``
    é o do POOL — não NULL.

    O seed grava NULL porque as tarefas dele nasceram antes de o pool ser
    materializado (verdade histórica, e não se conserta por baixo). As geradas
    aqui saem de um pool que tem build id, e gravá-lo é o que permite dizer,
    meses depois, sobre qual corpus esta fila foi montada.
    """
    admin = admin_de(cliente)
    r = cliente.post(
        "/api/admin/tarefas",
        json={"admin_id": admin, "tipo": "sft_resposta", "n": 3, "prioridade": 77},
    )
    assert r.status_code == 201, r.text
    corpo = r.json()
    assert corpo["criadas"] == 3 and len(corpo["ids"]) == 3

    conn = com_banco(semeado)
    try:
        linhas = conn.execute(
            "SELECT t.origem, t.status, t.prioridade, t.n_anotacoes_alvo, t.db_build_id, "
            "       pj.nome AS projeto "
            "FROM tarefas t LEFT JOIN projetos pj ON pj.id = t.projeto_id "
            f"WHERE t.id IN ({','.join('?' * len(corpo['ids']))})",
            corpo["ids"],
        ).fetchall()
        assert len(linhas) == 3
        for linha in linhas:
            assert str(linha["origem"]) == "admin"
            assert str(linha["status"]) == "aberta"
            assert int(linha["prioridade"]) == 77
            assert int(linha["n_anotacoes_alvo"]) == 1
            assert str(linha["projeto"]) == "Portfólio"
            assert linha["db_build_id"], "o build do pool não foi gravado"
        # E o seed continua com NULL — as duas populações não se misturam.
        assert conn.execute(
            "SELECT count(*) AS n FROM tarefas WHERE origem = 'semente' "
            "AND db_build_id IS NOT NULL"
        ).fetchone()["n"] == 0
    finally:
        conn.close()

    # Projeto inexistente é 404, e antes de qualquer INSERT.
    ruim = cliente.post(
        "/api/admin/tarefas",
        json={"admin_id": admin, "tipo": "sft_resposta", "n": 1, "projeto_id": 9_999},
    )
    assert ruim.status_code == 404


def test_gerar_respeita_material(cliente: TestClient, semeado: Path) -> None:
    """``avaliar_rubrica`` exige rubrica ativa **e** uma resposta.

    A regra é de ``catalogo.marcar_material`` — a MESMA que desabilita o botão
    do catálogo. Duas cópias divergiriam, e a divergência apareceria como um
    botão habilitado que abre um workspace de duas colunas vazias.
    """
    admin = admin_de(cliente)
    assert cliente.get("/api/health").status_code == 200
    uids = uids_do_pool_gravado(semeado)
    assert uids, "sem pool não há o que gerar"

    params = {"admin_id": admin, "tipo": "avaliar_rubrica", "n": 10}
    antes = cliente.get("/api/admin/tarefas/previa", params=params).json()
    assert antes["criaria"] == 0
    assert antes["puladas_material"] == antes["n_pool"]

    plantar_material(semeado, uids[0])

    depois = cliente.get("/api/admin/tarefas/previa", params=params).json()
    assert depois["criaria"] == 1
    assert depois["amostra"][0]["uid"] == uids[0]
    assert depois["puladas_material"] == antes["puladas_material"] - 1

    r = cliente.post("/api/admin/tarefas", json=params)
    assert r.status_code == 201 and r.json()["criadas"] == 1

    conn = com_banco(semeado)
    try:
        linha = conn.execute(
            "SELECT prompt_uid FROM tarefas WHERE id = ?", (r.json()["ids"][0],)
        ).fetchone()
        assert str(linha["prompt_uid"]) == uids[0]
    finally:
        conn.close()


def test_gerar_nao_duplica_tarefa_aberta(cliente: TestClient, semeado: Path) -> None:
    """Dois cliques no mesmo lote dobrariam o trabalho sobre os mesmos prompts.

    O ``ja_anotei`` do catálogo passaria a mentir e toda métrica por pessoa
    contaria o mesmo item duas vezes — e nada disso levanta exceção em lugar
    nenhum.
    """
    admin = admin_de(cliente)
    params = {"admin_id": admin, "tipo": "sft_resposta", "n": 5}
    primeira = cliente.post("/api/admin/tarefas", json=params).json()
    segunda = cliente.post("/api/admin/tarefas", json=params).json()

    assert set(primeira["ids"]).isdisjoint(segunda["ids"])
    # As do SEED já ocupavam prompts deste tipo: a contagem de puladas não é
    # zero nem na primeira passagem.
    assert primeira["puladas_existentes"] >= 1
    assert segunda["puladas_existentes"] >= primeira["puladas_existentes"] + primeira["criadas"]

    conn = com_banco(semeado)
    try:
        repetidos = conn.execute(
            "SELECT prompt_uid, count(*) AS n FROM tarefas "
            "WHERE tipo = 'sft_resposta' AND status = 'aberta' "
            "GROUP BY prompt_uid HAVING n > 1"
        ).fetchall()
        assert not repetidos, [str(linha["prompt_uid"]) for linha in repetidos]
    finally:
        conn.close()


def test_gerar_com_alvo_2_permite_duas_atribuicoes(cliente: TestClient) -> None:
    """``n_anotacoes_alvo = 2`` é o que torna o agreement mensurável.

    Sem ele, duas pessoas nunca veem o mesmo item e a concordância entre
    anotadores deixa de existir como medida — o número mais citado de qualquer
    proposta de anotação.
    """
    p = papeis(cliente)
    r = cliente.post(
        "/api/admin/tarefas",
        json={
            "admin_id": p["admin"][0],
            "tipo": "escrever_rubrica",
            "n": 1,
            "n_anotacoes_alvo": 2,
            # Acima do pacote de demonstração (60 a 90): a fila serve esta
            # primeiro, e as duas personas caem na MESMA tarefa.
            "prioridade": 100,
        },
    )
    assert r.status_code == 201, r.text
    tarefa_id = r.json()["ids"][0]

    vistos = []
    for quem in (p["anotador"][0], p["anotador"][1]):
        env = cliente.post(
            "/api/tarefas/proxima",
            json={"anotador_id": quem, "tipo": "escrever_rubrica"},
        ).json()["tarefa"]
        assert env is not None
        assert env["tarefa"]["id"] == tarefa_id
        vistos.append(env["atribuicao_id"])
    assert len(set(vistos)) == 2, "as duas pessoas receberam a mesma vaga"

    # E o terceiro NÃO entra: as duas vagas estão ocupadas.
    terceiro = cliente.post(
        "/api/tarefas/proxima",
        json={"anotador_id": p["anotador"][2], "tipo": "escrever_rubrica"},
    ).json()["tarefa"]
    assert terceiro is None or terceiro["tarefa"]["id"] != tarefa_id


@pytest.mark.parametrize(
    "corpo",
    [
        {"tipo": "inventado"},
        {"tipo": "sft_resposta", "n": 0},
        {"tipo": "sft_resposta", "n": 201},
        {"tipo": "sft_resposta", "n_anotacoes_alvo": 6},
        {"tipo": "sft_resposta", "prioridade": 101},
        # `bool` é subclasse de `int` e o Pydantic coage `true` -> `1` ANTES de
        # qualquer validador comum: sem o `mode="before"`, isto geraria UMA
        # tarefa em silêncio, com cara de sucesso.
        {"tipo": "sft_resposta", "n": True},
        {"tipo": "sft_resposta", "n_anotacoes_alvo": True},
        # `extra="forbid"`: um campo digitado errado tem de dizer o nome dele.
        {"tipo": "sft_resposta", "quantas": 3},
    ],
)
def test_gerar_422s(cliente: TestClient, corpo: dict[str, Any]) -> None:
    r = cliente.post("/api/admin/tarefas", json={"admin_id": admin_de(cliente), **corpo})
    assert r.status_code == 422, r.text


def test_lang_em_branco_nao_e_um_filtro(cliente: TestClient) -> None:
    """O formulário manda ``""`` no campo não preenchido, e ``WHERE lang = ''``
    casaria zero linhas — uma prévia de "0 tarefas" que parece falta de material
    e é campo em branco."""
    admin = admin_de(cliente)
    base = {"admin_id": admin, "tipo": "sft_resposta", "n": 3}
    vazio = cliente.get("/api/admin/tarefas/previa", params={**base, "lang": "  "}).json()
    ausente = cliente.get("/api/admin/tarefas/previa", params=base).json()
    assert vazio["criaria"] == ausente["criaria"] > 0
    assert vazio["filtros"]["lang"] is None


# ---------------------------------------------------------------------------
# 5. A TRILHA DE AUDITORIA
# ---------------------------------------------------------------------------


def test_evento_na_transacao(cliente: TestClient, semeado: Path) -> None:
    """Um evento por lote, com o que ele criou — e as ações do inventário.

    ``eventos.ACOES`` não é CHECK do DDL (o vocabulário cresce a cada marco),
    então ele é documentação — e documentação que não lista o que existe no
    banco mente justamente para quem a lê para saber o que procurar. Três ações
    do P4d estavam gravadas nas rotas e faltavam na lista.
    """
    admin = admin_de(cliente)
    r = cliente.post(
        "/api/admin/tarefas",
        json={"admin_id": admin, "tipo": "sft_resposta", "n": 2, "lang": "pt"},
    )
    assert r.status_code == 201, r.text

    conn = com_banco(semeado)
    try:
        linhas = conn.execute(
            "SELECT ator_id, entidade, detalhe_json FROM eventos WHERE acao = 'tarefas_geradas'"
        ).fetchall()
        assert len(linhas) == 1, "um lote, um evento"
        assert int(linhas[0]["ator_id"]) == admin
        assert str(linhas[0]["entidade"]) == "banco"
        detalhe = json.loads(str(linhas[0]["detalhe_json"]))
        assert detalhe["ids"] == r.json()["ids"]
        assert detalhe["n_criadas"] == r.json()["criadas"]
        assert detalhe["n_pedidas"] == 2
        assert detalhe["lang"] == "pt"
        assert detalhe["tipo"] == "sft_resposta"
        assert detalhe["db_build_id"]
    finally:
        conn.close()

    for acao in ("turno_gerado", "rodada_gerada", "rodada_decidida", "tarefas_geradas"):
        assert acao in evmod.ACOES, acao


def test_trilha_da_escalacao(cliente: TestClient) -> None:
    """A cadeia inteira de um item, em ordem, sem remontá-la por inferência.

    É o que o admin lê ao decidir uma escalação: ele julga um trabalho que
    atravessou duas passagens, e sem a trilha a única saída seria reconstruir a
    história a partir de carimbos de tempo espalhados por cinco tabelas.
    """
    p = papeis(cliente)
    ana, diego, admin = p["anotador"][0], p["revisor"][0], p["admin"][0]
    feito = ciclo_completo(cliente, ana, diego, depois="borderline_admin")
    assert feito["avaliacao"]["escalada"] is True

    r = cliente.get(
        "/api/admin/eventos",
        params={
            "admin_id": admin,
            "entidade": "anotacao",
            "entidade_id": feito["anotacao_id"],
        },
    )
    assert r.status_code == 200, r.text
    acoes = [item["acao"] for item in r.json()["items"]]
    assert acoes == ["anotacao_submetida", "triagem_aprovada", "avaliacao_registrada"]
    assert r.json()["total"] == 3
    # Cada linha traz QUEM fez, resolvido pelo nome — a trilha é lida por gente.
    assert {item["ator"] for item in r.json()["items"]} == {
        item["nome"]
        for item in cliente.get("/api/perfis").json()["items"]
        if item["id"] in (ana, diego)
    }

    # E o outro eixo: a decisão do admin fica pendurada na AVALIAÇÃO.
    fila = cliente.get("/api/escalacao/fila", params={"admin_id": admin}).json()
    assert fila["total"] == 1
    avaliacao_id = fila["items"][0]["avaliacao_id"]
    decidida = cliente.post(
        f"/api/escalacao/{avaliacao_id}",
        json={"admin_id": admin, "decisao": "aprovada", "comentario": None},
    )
    assert decidida.status_code == 200, decidida.text
    da_avaliacao = cliente.get(
        "/api/admin/eventos",
        params={"admin_id": admin, "entidade": "avaliacao", "entidade_id": avaliacao_id},
    ).json()
    assert [item["acao"] for item in da_avaliacao["items"]] == ["decisao_admin"]


def test_entidade_inventada_da_422_com_o_vocabulario(cliente: TestClient) -> None:
    """Um nome errado devolveria ``{"items": []}`` — uma trilha vazia, que é
    indistinguível de "nada aconteceu com este item". É a leitura mais perigosa
    que uma auditoria pode induzir, então ela é 422 com a lista no detalhe."""
    r = cliente.get(
        "/api/admin/eventos",
        params={"admin_id": admin_de(cliente), "entidade": "coisa", "entidade_id": 1},
    )
    assert r.status_code == 422
    for nome in evmod.ENTIDADES:
        assert nome in r.json()["detail"]


# ---------------------------------------------------------------------------
# agreement entre anotadores — o instrumento que o parecer disse estar errado
# ---------------------------------------------------------------------------


ESCALA5 = [1, 2, 3, 4, 5]


def test_o_peso_quadratico_normaliza_pela_escala_DECLARADA() -> None:
    """Um degrau numa escala de 3 posições vale mais que um degrau numa de 5, e
    é a verdade: em três posições, um degrau é metade do instrumento."""
    w5 = conmod._matriz_de_pesos(5, "quadratico")
    w3 = conmod._matriz_de_pesos(3, "quadratico")
    assert w5[2][3] == pytest.approx(0.9375)
    assert w3[0][1] == pytest.approx(0.75)
    # As pontas não contam NADA como concordância, em qualquer escala.
    assert w5[0][4] == 0.0 and w3[0][2] == 0.0
    # E no nominal a distância não significa nada: só a diagonal conta.
    assert conmod._matriz_de_pesos(5, "nominal")[2][3] == 0.0


def test_os_dois_extremos_dao_os_dois_extremos() -> None:
    conc = conmod._pi([("a", "a")] * 6 + [("b", "b")] * 6, ["a", "b"], "nominal")
    assert conc["kappa"] == 1.0 and conc["observada"] == 1.0
    disc = conmod._pi([("a", "b")] * 6 + [("b", "a")] * 6, ["a", "b"], "nominal")
    assert disc["kappa"] == -1.0 and disc["observada"] == 0.0


def test_sem_variancia_e_None_com_MOTIVO_e_nunca_zero() -> None:
    """O paradoxo clássico: todos na mesma categoria ⟹ esperança 1 ⟹ 0/0.

    As duas saídas preguiçosas mentem. ``1.0`` afirmaria concordância perfeita
    onde não houve escolha a fazer; ``0.0`` afirmaria concordância ao nível do
    acaso — a acusação mais forte que este número sabe fazer — justamente quando
    as duas pessoas concordaram em tudo.
    """
    r = conmod._pi([("a", "a")] * 12, ["a", "b"], "nominal")
    assert r["kappa"] is None
    assert r["motivo"] == conmod.MOTIVO_SEM_VARIANCIA
    assert r["observada"] == 1.0, "a observada continua sendo um fato sobre os pares"


def test_a_observada_sobrevive_ao_piso_e_o_kappa_nao() -> None:
    """A separação que o piso governa: a observada é DESCRITIVA (vale com um par
    só), o kappa é INFERENCIAL (o acaso sai das marginais, que precisam de n)."""
    r = conmod._pi([(3, 3), (4, 5)], ESCALA5, "quadratico")
    assert r["n_pares"] == 2
    assert r["observada"] > 0, "a observada é publicada em qualquer n"
    assert r["kappa"] is None and r["motivo"] == conmod.MOTIVO_POUCOS


def test_o_PONDERADO_e_o_EXATO_discordam_e_e_por_isso_que_o_modulo_existe() -> None:
    """O número medido que justifica o marco.

    Mesma equipe, mesmo trabalho: oito pares exatos e quatro errando por UM
    degrau numa escala de cinco. Ponderado 0,894 — descreve o que aconteceu.
    Igualdade exata 0,571 — descreveria uma equipe com problema de calibração.
    O plano original publicaria o segundo.
    """
    pares = [(5, 5), (4, 4), (3, 3), (2, 2), (1, 1), (5, 5), (4, 4), (3, 3),
             (4, 5), (3, 2), (2, 3), (5, 4)]
    ponderado = conmod._pi(pares, ESCALA5, "quadratico")
    exato = conmod._pi(pares, ESCALA5, "nominal")
    assert ponderado["kappa"] == pytest.approx(0.8943, abs=1e-4)
    assert exato["kappa"] == pytest.approx(0.5714, abs=1e-4)
    assert ponderado["kappa"] > exato["kappa"] + 0.3


def test_discordancia_SISTEMATICA_da_observada_alta_e_kappa_negativo() -> None:
    """Os dois números respondem perguntas diferentes, e os dois estão certos.

    Sempre a um degrau de distância (3 contra 4): concordam quase sempre em
    valor (0,9375) e **nunca** mais que o acaso já daria — usando só duas
    posições da escala, o acaso sozinho produziria 0,9688. É por isso que a tela
    nunca mostra um destes números sem o outro.
    """
    r = conmod._pi([(3, 4)] * 6 + [(4, 3)] * 6, ESCALA5, "quadratico")
    assert r["observada"] == pytest.approx(0.9375)
    assert r["esperada"] == pytest.approx(0.9688, abs=1e-4)
    assert r["kappa"] == -1.0


def test_sem_par_nenhum_nao_vira_linha_com_zero() -> None:
    assert conmod.medir("x", [], ESCALA5, "quadratico") is None


# --- o caminho pelo banco ---------------------------------------------------


def duas_pessoas_na_mesma_tarefa(
    cliente: TestClient, tipo: str = "avaliar_rubrica"
) -> dict[str, Any]:
    """Duas anotações independentes sobre a MESMA tarefa.

    Passa pelo modo livre (``/api/tarefas/livre``) porque a fila do locked
    entrega tarefas diferentes a pessoas diferentes — que é o desenho correto
    dela e o oposto do que um par de agreement precisa.
    """
    quem = papeis(cliente)["anotador"][:2]
    assert len(quem) >= 2
    env0 = cliente.post(
        "/api/tarefas/proxima", json={"anotador_id": quem[0], "tipo": tipo}
    ).json()["tarefa"]
    assert env0 is not None
    # O uid mora no PROMPT do envelope: `tarefa` só carrega as chaves que a tela
    # usa, e o uid do prompt não é uma delas.
    uid = env0["prompt"]["uid"]
    env1 = cliente.post(
        "/api/tarefas/livre", json={"anotador_id": quem[1], "tipo": tipo, "prompt_uid": uid}
    ).json()["tarefa"]
    assert env1 is not None
    assert env1["tarefa"]["id"] == env0["tarefa"]["id"], "find-or-create devia reusar"
    return {"quem": quem, "envs": [env0, env1], "uid": uid}


def payload_com_notas(env: dict[str, Any], notas: dict[str, int]) -> dict[str, Any]:
    """O payload que o FORMULÁRIO produziria com estas notas por critério.

    Baixar a nota abaixo do topo obriga, num critério com catálogo, a marcar um
    tipo de issue **e** escrever a frase (a regra do P8). O helper faz o que a
    tela faz — sem isso o cenário morreria num 422 que não tem nada a ver com o
    que estes testes medem.
    """
    corpo = notas_da_rubrica(env)
    por_nome = {c["nome"]: c for c in env["rubrica"]["criterios"]}
    for nota in corpo["notas"]:
        if nota["criterio"] not in notas:
            continue
        nota["nota"] = notas[nota["criterio"]]
        criterio = por_nome[nota["criterio"]]
        catalogo = criterio.get("tipos_issue") or []
        if catalogo and nota["nota"] < int(criterio["escala"]["max"]):
            nota["tipos_issue"] = {str(t["id"]): (i == 0) for i, t in enumerate(catalogo)}
            nota["justificativa"] = (
                "The answer drops a constraint the prompt states, and the gap is visible "
                "in the second paragraph."
            )
    return corpo


def submeter_notas(cliente: TestClient, quem: int, env: dict[str, Any], notas: dict[str, int]):
    return cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
        json={
            "anotador_id": quem,
            "payload": payload_com_notas(env, notas),
            "tempo_ativo_ms": 20_000,
        },
    )


def test_duas_pessoas_no_mesmo_item_viram_UM_par(cliente: TestClient, semeado: Path) -> None:
    cenario = duas_pessoas_na_mesma_tarefa(cliente)
    for quem, env in zip(cenario["quem"], cenario["envs"], strict=True):
        assert submeter_notas(cliente, quem, env, {}).status_code == 200

    conn = com_banco(semeado)
    try:
        comparaveis, resumo = conmod.coletar(conn)
    finally:
        conn.close()
    assert resumo["tarefas_com_2_ou_mais"] == 1
    assert resumo["anotadores_distintos"] == 2
    assert len(comparaveis[0]["pessoas"]) == 2


def test_a_medida_usa_o_payload_COMO_SUBMETIDO_e_nao_o_corrigido(
    cliente: TestClient, semeado: Path
) -> None:
    """O revisor convergir as duas pessoas não é as duas pessoas concordarem.

    Aqui elas discordam num critério, e o Rate and Review reescreve a nota de
    uma delas para a da outra. Medir pelo corrigido devolveria concordância
    perfeita — uma medida que responde "o quanto o revisor trabalhou" com o nome
    de "o quanto a equipe concorda", e cuja resposta é sempre lisonjeira.
    """
    from prompt_factory.annotate import avaliacoes as avmod

    cenario = duas_pessoas_na_mesma_tarefa(cliente)
    env0, env1 = cenario["envs"]
    alvo = env0["rubrica"]["criterios"][0]
    baixo = {alvo["nome"]: int(alvo["escala"]["min"])}
    submetido = payload_com_notas(env0, baixo)

    ids = []
    for quem, env, notas in (
        (cenario["quem"][0], env0, baixo),
        (cenario["quem"][1], env1, {}),
    ):
        r = submeter_notas(cliente, quem, env, notas)
        assert r.status_code == 200, r.text
        ids.append(r.json()["anotacao_id"])

    conn = com_banco(semeado)
    try:
        antes = conmod.relatorio(conn)
    finally:
        conn.close()
    linha_notas = next(x for x in antes["linhas"] if x["campo"].startswith("avaliar_rubrica.notas"))
    assert linha_notas["observada"] < 1.0, "elas discordam num critério"

    # O revisor tria e depois CORRIGE a nota divergente para a da outra pessoa.
    # A correção é MÍNIMA (só a nota sobe), então o diff é um caminho só — e é o
    # servidor quem o confere contra os motivos declarados.
    revisor = papeis(cliente)["revisor"][0]
    triar(cliente, revisor, ids[0])
    corrigido = json.loads(json.dumps(submetido))
    for nota in corrigido["notas"]:
        if nota["criterio"] == alvo["nome"]:
            nota["nota"] = int(alvo["escala"]["max"])
    # `valor_antes`/`valor_depois` viajam como TEXTO no contrato (é o que a tela
    # mostrou); quem confere os valores de verdade é o servidor, contra o diff
    # que ele mesmo calcula.
    edicoes = [
        {
            "campo": d["campo"],
            "valor_antes": None if d["valor_antes"] is None else str(d["valor_antes"]),
            "valor_depois": None if d["valor_depois"] is None else str(d["valor_depois"]),
            "motivo": "aligned the score with what the text supports",
        }
        for d in avmod.diferencas(submetido, corrigido)
    ]
    assert edicoes, "a correção tem de produzir diff"
    r = cliente.post(
        f"/api/avaliacao/{ids[0]}",
        json={
            "revisor_id": revisor,
            "avaliacao_antes": "ajustavel",
            "avaliacao_depois": "adequado",
            "justificativa": "The low score did not match the evidence in the answer.",
            "payload_corrigido": corrigido,
            "edicoes": edicoes,
            "tempo_ativo_ms": 9_000,
        },
    )
    assert r.status_code == 200, r.text

    conn = com_banco(semeado)
    try:
        depois = conmod.relatorio(conn)
    finally:
        conn.close()
    igual = next(x for x in depois["linhas"] if x["campo"].startswith("avaliar_rubrica.notas"))
    assert igual["observada"] == linha_notas["observada"], (
        "a correção do revisor não pode mexer no agreement dos anotadores"
    )


def test_sintetica_fica_de_FORA_e_o_numero_dela_aparece(
    cliente: TestClient, semeado: Path
) -> None:
    """Concordar com um defeito plantado não é virtude, e discordar não é falha.

    Sem a exclusão, a medida responderia a quantas sintéticas a campanha gerou.
    """
    cenario = duas_pessoas_na_mesma_tarefa(cliente)
    for quem, env in zip(cenario["quem"], cenario["envs"], strict=True):
        assert submeter_notas(cliente, quem, env, {}).status_code == 200

    conn = com_banco(semeado)
    try:
        # Carimba UMA das duas como sintética — o sinal único de sempre.
        alvo = conn.execute("SELECT id FROM anotacoes ORDER BY id DESC LIMIT 1").fetchone()["id"]
        conn.execute(
            f"UPDATE anotacoes SET {adb.COLUNA_GABARITO_AVALIACAO} = ? WHERE id = ?",
            (json.dumps({"avaliacao_antes": "ajustavel", "familia_defeito": "x", "nota": 1}), alvo),
        )
        conn.commit()
        _, resumo = conmod.coletar(conn)
    finally:
        conn.close()
    assert resumo["sinteticas_excluidas"] == 1
    assert resumo["tarefas_com_2_ou_mais"] == 0, "sobrou uma pessoa só na tarefa"


def test_NA_sai_do_kappa_numerico_e_vira_linha_propria(
    cliente: TestClient, semeado: Path
) -> None:
    """N/A não é um ponto da escala — é justamente por isso que ele é campo
    próprio no contrato. Empurrá-lo para dentro exigiria escolher um número, que
    é a sentinela numérica que ``NotaCriterio`` existe para não ter."""
    cenario = duas_pessoas_na_mesma_tarefa(cliente)
    env0, env1 = cenario["envs"]
    alvo = env0["rubrica"]["criterios"][0]["nome"]
    for quem, env, na in ((cenario["quem"][0], env0, True), (cenario["quem"][1], env1, False)):
        corpo = notas_da_rubrica(env)
        if na:
            for nota in corpo["notas"]:
                if nota["criterio"] == alvo:
                    nota.pop("nota", None)
                    nota["nao_aplicavel"] = True
                    nota["motivo_na"] = "This criterion does not apply to the answer given."
        r = cliente.post(
            f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
            json={"anotador_id": quem, "payload": corpo, "tempo_ativo_ms": 20_000},
        )
        assert r.status_code == 200, r.text

    conn = com_banco(semeado)
    try:
        rel = conmod.relatorio(conn)
    finally:
        conn.close()
    campos = {x["campo"]: x for x in rel["linhas"]}
    na = campos["avaliar_rubrica.nao_aplicavel"]
    assert na["observada"] < 1.0, "uma disse N/A e a outra não"
    notas = next(v for k, v in campos.items() if k.startswith("avaliar_rubrica.notas"))
    # O critério com N/A de um lado não entrou no numérico: sobraram os outros.
    assert notas["n_pares"] == len(env0["rubrica"]["criterios"]) - 1


def test_escalas_DIFERENTES_nao_entram_na_mesma_matriz(cliente: TestClient) -> None:
    """Um par de uma rubrica 1..5 e um do instrumento de severidade 1..3 não
    cabem juntos: ou se inventam categorias que ninguém podia escolher, ou se
    achata a distância entre as que existem. Uma linha por escala, com a escala
    no rótulo."""
    linha3 = conmod.medir("x", [(1, 2)] * 12, [1, 2, 3], "quadratico", escala={"min": 1, "max": 3})
    linha5 = conmod.medir("y", [(1, 2)] * 12, ESCALA5, "quadratico", escala={"min": 1, "max": 5})
    assert linha3 and linha5
    assert linha3["categorias"] == 3 and linha5["categorias"] == 5
    # A MESMA discordância de um degrau pesa diferente, e é essa a razão da separação.
    assert linha3["observada"] < linha5["observada"]


def test_a_rota_devolve_a_concordancia_com_o_piso_declarado(cliente: TestClient) -> None:
    r = cliente.get("/api/admin/metricas", params={"admin_id": admin_de(cliente)})
    assert r.status_code == 200, r.text
    conc = r.json()["concordancia"]
    assert conc["piso_pares"] == conmod.piso_de_pares()
    assert set(conc["resumo"]) == {
        "tarefas_com_2_ou_mais",
        "tarefas_com_1",
        "anotadores_distintos",
        "sinteticas_excluidas",
    }
    # Toda linha carrega o instrumento pelo nome: um agreement sem o nome do
    # instrumento que o produziu não é conferível.
    for linha in conc["linhas"]:
        assert linha["instrumento"].startswith("pi_")
        assert "observada" in linha and "kappa" in linha


def test_a_concordancia_e_so_do_admin(cliente: TestClient) -> None:
    anotador = papeis(cliente)["anotador"][0]
    r = cliente.get("/api/admin/metricas", params={"admin_id": anotador})
    assert r.status_code == 403


def test_a_tela_nunca_mostra_o_kappa_sem_a_observada() -> None:
    """A regra dura do card, e ela é de LEITURA, não de estética.

    Medido: pares sempre a um degrau de distância dão observada 0,9375 e kappa
    -1,0. Um painel com "-1,0" sozinho faria alguém refazer um lote que está
    bom; um com "94%" sozinho esconderia um viés real entre dois anotadores.
    """
    from .test_annotate_i18n import _sem_comentarios

    js = (
        Path(__file__).resolve().parents[1]
        / "src" / "prompt_factory" / "annotate" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    assert "function blocoConcordancia(" in js
    # As duas colunas são declaradas juntas, na mesma lista de cabeçalhos.
    cabecalho = js[js.index("function blocoConcordancia(") :][:2000]
    assert '"metricas.col_observada"' in cabecalho and '"metricas.col_kappa"' in cabecalho
    # E o motivo do kappa ausente sai de uma TABELA de chaves inteiras: uma
    # chave montada por concatenação seria invisível para o teste de chave órfã.
    #
    # A varredura ignora COMENTÁRIO pela mesma razão que a do i18n ignora: a
    # prosa deste arquivo cita o padrão proibido para explicar por que ele é
    # proibido, e uma varredura ingênua acusaria a própria explicação.
    assert "const MOTIVO_CONC" in js
    assert 't("conc." +' not in _sem_comentarios(js)
