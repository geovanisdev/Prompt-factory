"""Central de Briefs, F4 (backend): a fila de pedidos e a tríade.

O que este marco acrescenta ao modo criar é um INSUMO e um PRODUTO. O insumo é o
pedido — reservado, devolvível, expirável — e ele é para a vista criar o que o
brief do P9 é para as telas de anotação: enquadramento, não unidade de trabalho.
O produto é a tríade **prompt + rubrica + gold descritiva**, que é a razão de a
Central existir; um prompt solto qualquer pessoa escreve.

As famílias:

* **fila vazia é 200, não 404** — a mesma decisão do claim de tarefa;
* **uma reserva por pessoa**, porque a vista tem um formulário só;
* **pedido e material andam juntos**, e os dois sentidos são erros diferentes;
* **a marca do pedido entra na MESMA transação da criação** — fora dela existiria
  um estado em que a criação aponta para um pedido que a fila ainda oferece;
* **a criação LIVRE continua exatamente como estava**: o P4 não sabe que a v7
  existe.
"""

from __future__ import annotations

import inspect
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from prompt_factory import db as dbmod
from prompt_factory.annotate import db as adb
from prompt_factory.annotate import pedidos as pedmod
from prompt_factory.annotate import seed as seedmod
from prompt_factory.annotate.main import criar_app
from prompt_factory.annotate.models import SCHEMA_MATERIAL_CRIACAO

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


PEDIDO = {
    "arquivo_fonte": "DOSEUJEITO_PNLD26_Filosofia_VU_MP.txt",
    "offset_inicio": 412_000,
    "recorte": "O apolineo e o dionisiaco nao sao dois polos que se excluem.",
    "task_type": "redacao-pratica",
    "tema": "Apolineo e dionisiaco",
    "meta_pedagogica": "Exercitar a leitura de um par conceitual sem reduzi-lo.",
    "papel": "professor",
    "disciplina": "Filosofia",
    "colecao": "Do Seu Jeito",
    "lote_id": "ped_0001",
}


def semear_pedidos(banco: Path, quantos: int = 3, **extra: Any) -> list[int]:
    """Insere pedidos direto no banco — a campanha do F2 é testada em outro lugar."""
    conn = dbmod.connect(banco)
    ids: list[int] = []
    try:
        for i in range(quantos):
            campos = {**PEDIDO, **extra}
            campos["offset_inicio"] = int(campos["offset_inicio"]) + i
            campos["chave"] = f"chave-{campos['disciplina']}-{campos['papel']}-{i}"
            nomes = ", ".join(campos)
            marcas = ", ".join("?" * len(campos))
            cur = conn.execute(
                f"INSERT INTO pedidos ({nomes}) VALUES ({marcas})", tuple(campos.values())
            )
            ids.append(int(cur.lastrowid or 0))
        conn.commit()
    finally:
        conn.close()
    return ids


@pytest.fixture
def cliente(semeado: Path, corpus: Path):
    with TestClient(criar_app(semeado, corpus)) as c:
        yield c


def _papeis(cliente: TestClient) -> dict[str, list[int]]:
    saida: dict[str, list[int]] = {}
    for p in cliente.get("/api/perfis").json()["items"]:
        saida.setdefault(p["papel"], []).append(p["id"])
    return saida


def material(**extra: Any) -> dict[str, Any]:
    """Uma tríade plausível. Os campos que os testes torcem entram por ``extra``."""
    base: dict[str, Any] = {
        "rubrica": {
            "titulo": "Quality of the exercise prompt",
            "criterios": [
                {
                    "nome": f"Criterion {i}",
                    "descricao": "Whether the answer does what the prompt asked for.",
                    "escala": {
                        "min": 1,
                        "max": 5,
                        "ancoras": [
                            {"valor": 1, "rotulo": "does not meet it"},
                            {"valor": 5, "rotulo": "meets it fully"},
                        ],
                    },
                }
                for i in range(1, 4)
            ],
        },
        "gold": {
            "deve_conter": [
                "names the tension between the two concepts",
                "gives one example taken from the classroom",
            ],
            "nao_pode": ["reduces the pair to a simple opposition"],
            "armadilhas": ["fluent prose that only restates the definitions"],
            "observacoes": None,
        },
    }
    for chave, valor in extra.items():
        alvo, _, campo = chave.partition("__")
        if campo:
            base[alvo][campo] = valor
        else:
            base[alvo] = valor
    return base


def corpo_criacao(autor: int, pedido_id: int | None = None, **extra: Any) -> dict[str, Any]:
    corpo: dict[str, Any] = {
        "autor_id": autor,
        "texto": (
            "Monte tres questoes dissertativas sobre a tensao entre apolineo e "
            "dionisiaco, com gabarito comentado, para uma turma de 1o ano."
        ),
        "lang": "pt",
    }
    if pedido_id is not None:
        corpo["pedido_id"] = pedido_id
        corpo["material"] = material()
    corpo.update(extra)
    return corpo


# ---------------------------------------------------------------------------
# 1. a fila
# ---------------------------------------------------------------------------


def test_fila_vazia_e_200_com_pedido_null(cliente: TestClient) -> None:
    """A mesma decisão do ``POST /api/tarefas/proxima``: fila vazia é o estado
    mais comum de uma plataforma bem servida, e um 404 faria o cliente tratar o
    caminho normal como falha.

    O ``motivo_chave`` é o que a TELA traduz; o ``motivo`` em português é para o
    ``/docs`` — frase de tela mora na tela, a lição do P3i.
    """
    quem = _papeis(cliente)["anotador"][0]
    r = cliente.post("/api/pedidos/proximo", json={"anotador_id": quem})
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["pedido"] is None
    assert corpo["motivo_chave"] == "vazio"
    assert "destile" in corpo["motivo"]


def test_reservar_tira_o_pedido_da_fila_e_grava_evento(
    cliente: TestClient, semeado: Path
) -> None:
    ids = semear_pedidos(semeado, 2)
    quem = _papeis(cliente)["anotador"][0]
    corpo = cliente.post("/api/pedidos/proximo", json={"anotador_id": quem}).json()
    assert corpo["pedido"]["id"] == ids[0]
    assert corpo["pedido"]["status"] == "reservado"
    # o recorte VIAJA para a tela: é a razão de o pedido existir
    assert corpo["pedido"]["recorte"] == PEDIDO["recorte"]

    conn = dbmod.connect(semeado)
    try:
        linha = conn.execute("SELECT * FROM pedidos WHERE id = ?", (ids[0],)).fetchone()
        assert linha["reservado_por"] == quem and linha["reservado_em"]
        evento = conn.execute(
            "SELECT * FROM eventos WHERE acao = 'pedido_reservado'"
        ).fetchone()
        assert evento is not None and int(evento["entidade_id"]) == ids[0]
        # a trilha NÃO carrega o recorte: material de terceiros não sai da
        # tabela `pedidos` nem por porta lateral
        assert PEDIDO["recorte"][:30] not in evento["detalhe_json"]
    finally:
        conn.close()


def test_uma_reserva_por_pessoa(cliente: TestClient, semeado: Path) -> None:
    """Dois recortes abertos ao mesmo tempo produziriam dois rascunhos
    concorrentes numa vista que tem um formulário só."""
    ids = semear_pedidos(semeado, 3)
    quem = _papeis(cliente)["anotador"][0]
    primeiro = cliente.post("/api/pedidos/proximo", json={"anotador_id": quem}).json()
    segundo = cliente.post("/api/pedidos/proximo", json={"anotador_id": quem}).json()
    assert segundo["pedido"]["id"] == primeiro["pedido"]["id"] == ids[0]
    assert segundo["motivo_chave"] == "ja_reservado"


def test_duas_pessoas_pegam_pedidos_DIFERENTES(cliente: TestClient, semeado: Path) -> None:
    ids = semear_pedidos(semeado, 2)
    pessoas = _papeis(cliente)["anotador"][:2]
    pegos = [
        cliente.post("/api/pedidos/proximo", json={"anotador_id": p}).json()["pedido"]["id"]
        for p in pessoas
    ]
    assert sorted(pegos) == sorted(ids)


def test_o_filtro_da_fila_e_por_disciplina_e_papel(
    cliente: TestClient, semeado: Path
) -> None:
    semear_pedidos(semeado, 1, disciplina="Filosofia", papel="professor")
    alvo = semear_pedidos(semeado, 1, disciplina="Biologia", papel="aluno")
    quem = _papeis(cliente)["anotador"][0]
    r = cliente.post(
        "/api/pedidos/proximo",
        json={"anotador_id": quem, "disciplina": "Biologia", "papel": "aluno"},
    ).json()
    assert r["pedido"]["id"] == alvo[0]


def test_filtro_que_nao_casa_nada_diz_que_foi_o_FILTRO(
    cliente: TestClient, semeado: Path
) -> None:
    """"Nenhum pedido disponível" e "nenhum com esses filtros" mandam a pessoa
    fazer coisas diferentes."""
    semear_pedidos(semeado, 1, disciplina="Filosofia")
    quem = _papeis(cliente)["anotador"][0]
    r = cliente.post(
        "/api/pedidos/proximo", json={"anotador_id": quem, "disciplina": "Quimica"}
    ).json()
    assert r["pedido"] is None and r["motivo_chave"] == "vazio_filtrado"


def test_papel_inventado_e_422(cliente: TestClient) -> None:
    """Um papel fora da tupla devolveria fila vazia em silêncio, que é
    indistinguível de "acabaram os pedidos"."""
    quem = _papeis(cliente)["anotador"][0]
    r = cliente.post(
        "/api/pedidos/proximo", json={"anotador_id": quem, "papel": "coordenador"}
    )
    assert r.status_code == 422


def test_as_facetas_contam_SO_o_que_esta_disponivel(
    cliente: TestClient, semeado: Path
) -> None:
    """Contar sobre a tabela inteira ofereceria "Filosofia (3)" numa fila em que
    os três já foram usados — um filtro que devolve vazio depois de prometer
    três."""
    semear_pedidos(semeado, 3)
    quem = _papeis(cliente)["anotador"][0]
    cliente.post("/api/pedidos/proximo", json={"anotador_id": quem})
    facetas = cliente.get("/api/pedidos", params={"anotador_id": quem}).json()["facetas"]
    assert facetas["disponiveis"] == 2
    assert facetas["disciplina"] == [{"valor": "Filosofia", "n": 2}]


# ---------------------------------------------------------------------------
# 2. devolver e expirar
# ---------------------------------------------------------------------------


def test_devolver_repoe_o_pedido_na_fila(cliente: TestClient, semeado: Path) -> None:
    ids = semear_pedidos(semeado, 1)
    quem = _papeis(cliente)["anotador"][0]
    cliente.post("/api/pedidos/proximo", json={"anotador_id": quem})
    r = cliente.post(f"/api/pedidos/{ids[0]}/devolver", json={"anotador_id": quem})
    assert r.status_code == 200 and r.json()["status"] == "disponivel"
    assert r.json()["reservado_por"] is None
    # e o próximo claim o pega de volta, na mesma posição (a ordem é por id)
    de_novo = cliente.post("/api/pedidos/proximo", json={"anotador_id": quem}).json()
    assert de_novo["pedido"]["id"] == ids[0]


def test_so_quem_reservou_devolve(cliente: TestClient, semeado: Path) -> None:
    ids = semear_pedidos(semeado, 1)
    pessoas = _papeis(cliente)["anotador"][:2]
    cliente.post("/api/pedidos/proximo", json={"anotador_id": pessoas[0]})
    r = cliente.post(f"/api/pedidos/{ids[0]}/devolver", json={"anotador_id": pessoas[1]})
    assert r.status_code == 403


def test_devolver_o_que_nao_esta_reservado_e_409(
    cliente: TestClient, semeado: Path
) -> None:
    ids = semear_pedidos(semeado, 1)
    quem = _papeis(cliente)["anotador"][0]
    r = cliente.post(f"/api/pedidos/{ids[0]}/devolver", json={"anotador_id": quem})
    assert r.status_code == 409 and "disponivel" in r.json()["detail"]


def test_a_reserva_expira_sozinha(cliente: TestClient, semeado: Path) -> None:
    """Preguiçosa, como em ``atribuicoes``: um UPDATE antes de cada listagem e de
    cada claim, sem thread de fundo."""
    ids = semear_pedidos(semeado, 1)
    pessoas = _papeis(cliente)["anotador"][:2]
    cliente.post("/api/pedidos/proximo", json={"anotador_id": pessoas[0]})

    conn = dbmod.connect(semeado)
    try:
        conn.execute(
            "UPDATE pedidos SET reservado_em = '2020-01-01T00:00:00.000Z' WHERE id = ?",
            (ids[0],),
        )
        conn.commit()
    finally:
        conn.close()

    outro = cliente.post("/api/pedidos/proximo", json={"anotador_id": pessoas[1]}).json()
    assert outro["pedido"]["id"] == ids[0]


def test_meu_pedido_expirado_some_do_painel(cliente: TestClient, semeado: Path) -> None:
    """Uma reserva vencida que a tela continuasse mostrando faria alguém escrever
    um prompt para um pedido que já voltou para a fila."""
    ids = semear_pedidos(semeado, 1)
    quem = _papeis(cliente)["anotador"][0]
    cliente.post("/api/pedidos/proximo", json={"anotador_id": quem})
    conn = dbmod.connect(semeado)
    try:
        conn.execute(
            "UPDATE pedidos SET reservado_em = '2020-01-01T00:00:00.000Z' WHERE id = ?",
            (ids[0],),
        )
        conn.commit()
    finally:
        conn.close()
    assert cliente.get("/api/pedidos", params={"anotador_id": quem}).json()["meu"] is None


# ---------------------------------------------------------------------------
# 3. pedido e material andam juntos
# ---------------------------------------------------------------------------


def test_pedido_sem_material_e_422(cliente: TestClient, semeado: Path) -> None:
    """Entregaria metade do que a Central existe para produzir — e o pedido
    ficaria ``usado`` sem ter rendido a tríade."""
    ids = semear_pedidos(semeado, 1)
    quem = _papeis(cliente)["anotador"][0]
    cliente.post("/api/pedidos/proximo", json={"anotador_id": quem})
    corpo = corpo_criacao(quem, ids[0])
    del corpo["material"]
    r = cliente.post("/api/criacoes", json=corpo)
    assert r.status_code == 422 and "tríade" in r.text


def test_material_sem_pedido_e_422(cliente: TestClient) -> None:
    """Alguém mandando rubrica e gold por uma vista que não os coleta: aceitar
    gravaria um blob que nenhuma tela mostra e nenhum export lê."""
    quem = _papeis(cliente)["anotador"][0]
    corpo = corpo_criacao(quem)
    corpo["material"] = material()
    r = cliente.post("/api/criacoes", json=corpo)
    assert r.status_code == 422


def test_criacao_LIVRE_continua_exatamente_como_estava(cliente: TestClient) -> None:
    """O P4 não sabe que a v7 existe, e é assim que tem de continuar até a tela."""
    quem = _papeis(cliente)["anotador"][0]
    r = cliente.post("/api/criacoes", json=corpo_criacao(quem))
    assert r.status_code == 201, r.text
    assert r.json()["pedido_id"] is None and r.json()["material"] is None


def test_pedido_de_outra_pessoa_e_403(cliente: TestClient, semeado: Path) -> None:
    ids = semear_pedidos(semeado, 1)
    pessoas = _papeis(cliente)["anotador"][:2]
    cliente.post("/api/pedidos/proximo", json={"anotador_id": pessoas[0]})
    r = cliente.post("/api/criacoes", json=corpo_criacao(pessoas[1], ids[0]))
    assert r.status_code == 403


def test_reserva_expirada_no_meio_da_escrita_e_409_com_o_conserto(
    cliente: TestClient, semeado: Path
) -> None:
    """O caso que MAIS vai acontecer, e por isso a mensagem diz o que fazer em
    vez de só recusar."""
    ids = semear_pedidos(semeado, 1)
    quem = _papeis(cliente)["anotador"][0]
    r = cliente.post("/api/criacoes", json=corpo_criacao(quem, ids[0]))
    assert r.status_code == 409
    assert "Puxe-o de novo" in r.json()["detail"]


def test_pedido_inexistente_e_404(cliente: TestClient) -> None:
    quem = _papeis(cliente)["anotador"][0]
    r = cliente.post("/api/criacoes", json=corpo_criacao(quem, 9876))
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 4. a tríade gravada
# ---------------------------------------------------------------------------


def test_a_triade_entra_e_o_pedido_vira_usado_na_MESMA_transacao(
    cliente: TestClient, semeado: Path
) -> None:
    ids = semear_pedidos(semeado, 1)
    quem = _papeis(cliente)["anotador"][0]
    cliente.post("/api/pedidos/proximo", json={"anotador_id": quem})
    r = cliente.post("/api/criacoes", json=corpo_criacao(quem, ids[0]))
    assert r.status_code == 201, r.text
    criacao_id = r.json()["id"]

    conn = dbmod.connect(semeado)
    try:
        linha = conn.execute(
            "SELECT pedido_id, material_json FROM criacoes WHERE id = ?", (criacao_id,)
        ).fetchone()
        assert int(linha["pedido_id"]) == ids[0]
        blob = json.loads(linha["material_json"])
        # o rótulo do contrato é carimbado pelo SERVIDOR
        assert blob["schema"] == SCHEMA_MATERIAL_CRIACAO
        assert len(blob["rubrica"]["criterios"]) == 3
        assert len(blob["gold"]["deve_conter"]) == 2

        pedido = conn.execute("SELECT * FROM pedidos WHERE id = ?", (ids[0],)).fetchone()
        assert pedido["status"] == "usado"
        assert pedido["reservado_por"] is None
        assert conn.execute(
            "SELECT 1 FROM eventos WHERE acao = 'pedido_usado'"
        ).fetchone() is not None
    finally:
        conn.close()


def test_um_pedido_usado_sai_da_fila(cliente: TestClient, semeado: Path) -> None:
    ids = semear_pedidos(semeado, 1)
    quem = _papeis(cliente)["anotador"][0]
    cliente.post("/api/pedidos/proximo", json={"anotador_id": quem})
    cliente.post("/api/criacoes", json=corpo_criacao(quem, ids[0]))
    r = cliente.post("/api/pedidos/proximo", json={"anotador_id": quem}).json()
    assert r["pedido"] is None


def test_a_criacao_lista_COM_o_pedido_ao_lado(cliente: TestClient, semeado: Path) -> None:
    """É o que permite ao revisor julgar originalidade sem sair da tela: ele
    precisa do recorte para saber se o texto foi escrito ou copiado."""
    ids = semear_pedidos(semeado, 1)
    quem = _papeis(cliente)["anotador"][0]
    cliente.post("/api/pedidos/proximo", json={"anotador_id": quem})
    cliente.post("/api/criacoes", json=corpo_criacao(quem, ids[0]))

    item = cliente.get("/api/criacoes", params={"anotador_id": quem}).json()["items"][0]
    assert item["pedido"]["id"] == ids[0]
    assert item["pedido"]["recorte"] == PEDIDO["recorte"]
    assert item["pedido"]["tema"] == PEDIDO["tema"]
    assert item["material"]["gold"]["nao_pode"]


def test_a_criacao_livre_lista_com_pedido_null(cliente: TestClient) -> None:
    quem = _papeis(cliente)["anotador"][0]
    cliente.post("/api/criacoes", json=corpo_criacao(quem))
    item = cliente.get("/api/criacoes", params={"anotador_id": quem}).json()["items"][0]
    assert item["pedido"] is None and item["material"] is None


# ---------------------------------------------------------------------------
# 5. o contrato da tríade
# ---------------------------------------------------------------------------


def _recusa(cliente: TestClient, semeado: Path, **torcido: Any) -> Any:
    ids = semear_pedidos(semeado, 1)
    quem = _papeis(cliente)["anotador"][0]
    cliente.post("/api/pedidos/proximo", json={"anotador_id": quem})
    corpo = corpo_criacao(quem, ids[0])
    corpo["material"] = material(**torcido)
    return cliente.post("/api/criacoes", json=corpo)


def test_escala_sem_as_duas_pontas_ancoradas_e_recusada(
    cliente: TestClient, semeado: Path
) -> None:
    """As PONTAS são o que faz uma escala ancorada ser ancorada: sem elas,
    "clareza de 1 a 5" significa cinco coisas para cinco anotadores."""
    rubrica = material()["rubrica"]
    rubrica["criterios"][0]["escala"]["ancoras"] = [{"valor": 1, "rotulo": "ruim"}]
    r = _recusa(cliente, semeado, rubrica=rubrica)
    assert r.status_code == 422 and "PONTAS" in r.text


def test_escala_booleana_e_recusada(cliente: TestClient, semeado: Path) -> None:
    """``bool`` é subclasse de ``int``: ``true`` viraria escala 1."""
    rubrica = material()["rubrica"]
    rubrica["criterios"][0]["escala"]["min"] = True
    assert _recusa(cliente, semeado, rubrica=rubrica).status_code == 422


def test_escala_fora_de_1_a_9_e_recusada(cliente: TestClient, semeado: Path) -> None:
    rubrica = material()["rubrica"]
    rubrica["criterios"][0]["escala"]["max"] = 12
    assert _recusa(cliente, semeado, rubrica=rubrica).status_code == 422


def test_criterios_demais_sao_recusados(cliente: TestClient, semeado: Path) -> None:
    """5 é o limite prático de quantas escalas alguém compara sem perder o fio —
    e esta rubrica vai ser aplicada por outra pessoa."""
    rubrica = material()["rubrica"]
    modelo = rubrica["criterios"][0]
    rubrica["criterios"] = [{**modelo, "nome": f"Criterion {i}"} for i in range(9)]
    r = _recusa(cliente, semeado, rubrica=rubrica)
    assert r.status_code == 422 and "critérios" in r.text


def test_dois_criterios_com_o_mesmo_nome_sao_recusados(
    cliente: TestClient, semeado: Path
) -> None:
    rubrica = material()["rubrica"]
    rubrica["criterios"][1]["nome"] = rubrica["criterios"][0]["nome"]
    assert _recusa(cliente, semeado, rubrica=rubrica).status_code == 422


def test_gold_com_um_item_so_e_recusada(cliente: TestClient, semeado: Path) -> None:
    """Dois pontos que a resposta precisa apresentar e um erro que a desqualifica
    são o mínimo para o revisor conferir item a item."""
    gold = material()["gold"]
    gold["deve_conter"] = gold["deve_conter"][:1]
    r = _recusa(cliente, semeado, gold=gold)
    assert r.status_code == 422 and "deve_conter" in r.text


def test_item_de_gold_curto_demais_e_recusado(cliente: TestClient, semeado: Path) -> None:
    """Um item que não diz o que conferir não é conferível."""
    gold = material()["gold"]
    gold["nao_pode"] = ["errado"]
    assert _recusa(cliente, semeado, gold=gold).status_code == 422


def test_campo_extra_no_material_e_recusado(cliente: TestClient, semeado: Path) -> None:
    """``extra="forbid"`` em tudo: um campo digitado errado vira 422 com o nome
    do campo, nunca silêncio."""
    gold = material()["gold"]
    gold["deve_conterr"] = ["typo"]
    assert _recusa(cliente, semeado, gold=gold).status_code == 422


# ---------------------------------------------------------------------------
# 6. o contrato da rota
# ---------------------------------------------------------------------------


def test_as_rotas_de_pedido_existem_no_openapi(cliente: TestClient) -> None:
    """``app.routes`` não lista mais as rotas dos routers incluídos — a
    pegadinha registrada no P2."""
    caminhos = set(cliente.app.openapi()["paths"])
    assert {"/api/pedidos", "/api/pedidos/proximo"} <= caminhos
    assert "/api/pedidos/{pedido_id}/devolver" in caminhos


def test_o_ttl_da_reserva_sai_do_settings(cliente: TestClient) -> None:
    """A tela mostra o prazo antes do clique; uma cópia no JS divergiria do
    servidor na primeira vez que alguém ajustasse o arquivo."""
    quem = _papeis(cliente)["anotador"][0]
    corpo = cliente.get("/api/pedidos", params={"anotador_id": quem}).json()
    assert corpo["reserva_ttl_min"] == pedmod.reserva_ttl_min()


def test_os_limites_da_triade_saem_da_rota_e_batem_com_o_settings(
    cliente: TestClient,
) -> None:
    """A armadilha que esta rota existe para fechar: o ``max_criterios_rubrica``
    do ``/api/health`` é o da aba escrever_rubrica (``[annotate]``, 8), e a
    tríade valida contra o de ``[pedidos]`` (5) — uma tela que lesse o health
    deixaria a pessoa chegar a oito critérios e tomar 422 no envio. Os valores
    têm de bater com os que ``models.RubricaCriacaoIn``/``GoldCriacaoIn`` leem.
    """
    quem = _papeis(cliente)["anotador"][0]
    corpo = cliente.get("/api/pedidos", params={"anotador_id": quem}).json()
    assert corpo["limites"] == pedmod.limites_da_triade()
    lim = corpo["limites"]
    assert set(lim) == {
        "min_criterios_rubrica",
        "max_criterios_rubrica",
        "gold_min_deve_conter",
        "gold_min_nao_pode",
        "gold_min_chars_item",
    }
    # O teto da tríade é o de [pedidos] — MENOR que o do health. Se um dia os
    # dois coincidirem, este assert deixa de provar a separação; a igualdade
    # contra `limites_da_triade()` acima continua provando a fonte.
    saude = cliente.get("/api/health").json()
    assert lim["max_criterios_rubrica"] <= saude["limites"]["max_criterios_rubrica"]


def test_o_apresentar_devolve_listas_de_verdade(semeado: Path) -> None:
    """O front escreve ``pedido.avisos.length`` e precisa que isso signifique o
    que parece — a mesma regra do ``ativo`` booleano em ``models.perfil``."""
    ids = semear_pedidos(semeado, 1)
    conn: sqlite3.Connection = dbmod.connect(semeado)
    try:
        conn.execute(
            "UPDATE pedidos SET habilidades_json = ?, avisos_json = ? WHERE id = ?",
            (json.dumps(["EM13CHS101"]), json.dumps(["contem 'Resposta:'"]), ids[0]),
        )
        linha = conn.execute(
            f"SELECT {pedmod.COLUNAS} FROM pedidos p WHERE p.id = ?", (ids[0],)
        ).fetchone()
    finally:
        conn.close()
    item = pedmod.apresentar(linha)
    assert item["habilidades"] == ["EM13CHS101"]
    assert item["avisos"] == ["contem 'Resposta:'"]


# ---------------------------------------------------------------------------
# 7. as tabelas da tela (F4-2) — três pontas: JS x backend x dicionário
# ---------------------------------------------------------------------------
#
# O padrão do MOTIVOS_SKIP do P9: a tabela do JS guarda a chave INTEIRA (chave
# montada por concatenação é invisível para o teste de chave órfã), e este
# teste é o que impede as duas listas da mesma coisa — uma em Python, uma em
# JavaScript — de divergirem em silêncio. A existência das chaves nos DOIS
# idiomas do dicionário é cobrada pelos testes de paridade de
# `test_annotate_i18n.py`, que enxergam os literais destas tabelas.


@pytest.fixture
def js() -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "src/prompt_factory/annotate/static/index.html"
    ).read_text(encoding="utf-8")


def test_a_tabela_de_papeis_do_JS_bate_com_o_enum_do_BACKEND(js: str) -> None:
    """Um papel novo em ``db.PAPEIS_PEDIDO`` sem linha na tabela sairia CRU na
    tela (o fallback `.dado` existe para isso, mas ele é degradação, não
    convenção); uma linha sem enum seria um selo que nenhum pedido produz."""
    bloco = js.split("const PAPEIS_PEDIDO_ROTULO = {", 1)[1].split("};", 1)[0]
    ids = re.findall(r'"([a-z_]+)": "pedido\.papel_[a-z_]+"', bloco)
    assert tuple(ids) == adb.PAPEIS_PEDIDO


def test_a_tabela_de_motivos_do_JS_cobre_o_que_o_proximo_emite(js: str) -> None:
    """As chaves que ``pedidos.proximo`` devolve em ``motivo_chave``. Uma chave
    emitida sem linha na tabela apareceria como frase nenhuma na tela (a fila
    vazia ficaria muda); uma linha sem emissor é chave morta no dicionário.

    O contrato não tem tupla no backend (as chaves nascem nos ``return`` de
    ``proximo``), então a ponta do Python é conferida contra o FONTE do módulo:
    renomear uma chave lá quebra aqui, que é o que se quer.
    """
    bloco = js.split("const MOTIVOS_PEDIDO = {", 1)[1].split("};", 1)[0]
    ids = re.findall(r'"([a-z_]+)": "pedido\.motivo_[a-z_]+"', bloco)
    assert set(ids) == {"ja_reservado", "vazio", "vazio_filtrado"}
    fonte = inspect.getsource(pedmod)
    for chave in ids:
        assert f'"{chave}"' in fonte, f"{chave!r} não aparece mais em pedidos.py"
