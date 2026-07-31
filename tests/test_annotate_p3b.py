"""P3b: o Rate and Review, o diff campo a campo, a escalação e o alvo escondido.

Cada família aqui existe por uma coisa que, se der errado, **não parece
quebrada** — que é o único tipo de defeito que este projeto persegue com teste:

* **o alvo escondido vazando.** ``anotacoes.gabarito_avaliacao_json`` guarda a
  avaliação que uma anotação sintética *deveria* receber. Se ele sair pela API do
  revisor, a calibração continua funcionando, os números continuam plausíveis e
  medem outra coisa: a capacidade de ler JSON. É a mesma disciplina do
  ``gabarito_json`` das tarefas-ouro, e por isso o teste varre a resposta INTEIRA,
  e não só as chaves que ele lembra de conferir;
* **``devolvida`` a partir de ``pendente_avaliacao``.** A regra "só a triagem
  devolve" mora em ``db.TRANSICOES``. Um ``if`` na rota que a repetisse
  divergiria dela na primeira mudança — então o teste lê a máquina E bate na
  rota;
* **mudança sem motivo.** ``edicoes_avaliacao.motivo`` é NOT NULL, mas o CHECK só
  pega quem chega até o INSERT. Quem decide o que mudou é o servidor, e é isso
  que impede o cliente de alterar trabalho alheio declarando um diff menor;
* **``incorrigivel`` reabrindo a atribuição.** Seria "só a triagem devolve"
  quebrada por um caminho que ninguém olha: a anotação encerrada e a tarefa de
  volta na mesa do anotador;
* **a migração** — o banco do dono já guarda trabalho humano, e a v3 acrescenta
  uma coluna. O schema migrado tem de ser **idêntico** ao de um banco novo.
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
from prompt_factory.annotate import avaliacoes as avmod
from prompt_factory.annotate import db as adb
from prompt_factory.annotate import eventos as evmod
from prompt_factory.annotate import migracao
from prompt_factory.annotate import seed as seedmod
from prompt_factory.annotate.main import criar_app

from .fixtures_annotate_v1 import init_v1
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
    conn = dbmod.connect(caminho)
    conn.row_factory = sqlite3.Row
    return conn


def perfis_por_papel(cliente: TestClient) -> dict[str, list[dict[str, Any]]]:
    saida: dict[str, list[dict[str, Any]]] = {}
    for p in cliente.get("/api/perfis").json()["items"]:
        saida.setdefault(p["papel"], []).append(p)
    return saida


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


def _triar(cliente: TestClient, revisor: int, anotacao_id: int) -> None:
    """Aprova na passagem 1 — é o que põe o item na fila da passagem 2."""
    r = cliente.post(
        f"/api/revisao/{anotacao_id}", json={"revisor_id": revisor, "veredito": "aprovada"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pendente_avaliacao"


def _pronto_para_avaliar(
    cliente: TestClient, tipo: str = "avaliar_rubrica"
) -> tuple[dict[str, Any], int, int]:
    """Um item aprovado na triagem + os ids do anotador e do revisor."""
    papeis = perfis_por_papel(cliente)
    ana = papeis["anotador"][0]["id"]
    revisor = papeis["revisor"][0]["id"]
    a = _anotar(cliente, ana, tipo)
    _triar(cliente, revisor, a["anotacao_id"])
    return a, ana, revisor


def _corpo(**extra: Any) -> dict[str, Any]:
    """Um corpo de avaliação válido, com o mínimo para passar."""
    corpo: dict[str, Any] = {
        "avaliacao_antes": "adequado",
        "avaliacao_depois": "adequado",
        "justificativa": "As notas acompanham a rubrica e o texto sustenta cada uma delas.",
    }
    corpo.update(extra)
    return corpo


# ---------------------------------------------------------------------------
# 1. O ALVO ESCONDIDO NÃO VAZA
# ---------------------------------------------------------------------------


def test_a_coluna_do_alvo_existe_e_nasce_nula(semeado: Path) -> None:
    """NULL é o que significa "trabalho humano" — nada marca "sintética" duas vezes."""
    conn = com_banco(semeado)
    try:
        colunas = {
            str(linha["name"]) for linha in conn.execute("PRAGMA table_info(anotacoes)")
        }
        assert adb.COLUNA_GABARITO_AVALIACAO in colunas
        assert adb.CHAVES_GABARITO_AVALIACAO == (
            "avaliacao_antes", "familia_defeito", "nota"
        )
    finally:
        conn.close()


def _plantar_alvo(banco: Path, anotacao_id: int) -> dict[str, Any]:
    """Escreve um alvo escondido, como a campanha do P4c vai fazer."""
    alvo = {
        "avaliacao_antes": "inutilizavel",
        "familia_defeito": "nota-nao-bate-com-a-justificativa",
        "nota": "SENHA-DO-TESTE-9f2a",
    }
    conn = com_banco(banco)
    try:
        conn.execute(
            f"UPDATE anotacoes SET {adb.COLUNA_GABARITO_AVALIACAO} = ? WHERE id = ?",
            (json.dumps(alvo, ensure_ascii=False), anotacao_id),
        )
    finally:
        conn.close()
    return alvo


def test_o_alvo_escondido_nao_vaza_por_nenhuma_rota_do_revisor(
    cliente: TestClient, semeado: Path
) -> None:
    """A prova é por VARREDURA, não por chave.

    Conferir ``"gabarito_avaliacao_json" not in resposta`` passaria hoje e
    falharia em silêncio no dia em que alguém renomeasse o campo ao serializar.
    O teste procura a **senha** que só existe dentro do alvo, no corpo inteiro
    das quatro rotas por onde um revisor passa.
    """
    a, ana, revisor = _pronto_para_avaliar(cliente)
    alvo = _plantar_alvo(semeado, a["anotacao_id"])
    senha = alvo["nota"]

    respostas = [
        cliente.get("/api/avaliacao/fila", params={"revisor_id": revisor}),
        cliente.get(f"/api/avaliacao/{a['anotacao_id']}", params={"revisor_id": revisor}),
        cliente.get("/api/revisao/fila", params={"revisor_id": revisor}),
        cliente.get("/api/atribuicoes", params={"anotador_id": ana}),
    ]
    for r in respostas:
        assert r.status_code == 200, r.text
        assert senha not in r.text, f"o alvo escondido vazou em {r.request.url}"
        assert "gabarito" not in r.text.lower()
        assert "familia_defeito" not in r.text

    # E ele CONTINUA no banco: o teste prova que não vaza, não que sumiu.
    conn = com_banco(semeado)
    try:
        bruto = conn.execute(
            f"SELECT {adb.COLUNA_GABARITO_AVALIACAO} AS g FROM anotacoes WHERE id = ?",
            (a["anotacao_id"],),
        ).fetchone()["g"]
        assert json.loads(str(bruto))["nota"] == senha
    finally:
        conn.close()


def test_a_hidratacao_nao_le_a_coluna_do_alvo(semeado: Path, corpus: Path) -> None:
    """A garantia não é a chave que falta — é a coluna que nem entra no SELECT.

    ``hidratar_anotacao`` é o envelope por onde passa TODA leitura do revisor
    (triagem e Rate and Review). Um ``dict(linha)`` ali dentro passaria no teste
    de cima enquanto o SELECT não tivesse a coluna, e vazaria no dia seguinte.
    """
    import inspect
    import re

    from prompt_factory.annotate import tarefas as tmod

    def sem_prosa(fn: Any) -> str:
        """O CÓDIGO, sem docstring nem comentário — que é onde a prosa deste
        repo explica justamente por que a coluna não está aqui."""
        fonte = inspect.getsource(fn)
        fonte = fonte.replace(fn.__doc__ or "\x00", "")
        return re.sub(r"^\s*#.*$", "", fonte, flags=re.M)

    for fn in (tmod.hidratar_anotacao, tmod.hidratar):
        codigo = sem_prosa(fn)
        assert adb.COLUNA_GABARITO_AVALIACAO not in codigo, fn.__name__
        assert "gabarito" not in codigo, fn.__name__
        # `dict(linha)` passaria no teste de hoje e vazaria no dia em que
        # alguém acrescentasse uma coluna ao SELECT.
        assert "dict(linha)" not in codigo, fn.__name__


# ---------------------------------------------------------------------------
# 2. A MÁQUINA DE STATUS
# ---------------------------------------------------------------------------


def test_devolvida_nao_e_transicao_de_pendente_avaliacao() -> None:
    """A regra é DADO, e é aqui que ela está escrita.

    Um item aprovado na triagem que volta ao anotador dias depois mede o revisor
    da triagem, não quem anotou — e a métrica de QC que nasce disso ficaria
    embaralhada. Só a triagem devolve.
    """
    assert "devolvida" not in adb.TRANSICOES["pendente_avaliacao"]
    assert set(adb.TRANSICOES["pendente_avaliacao"]) == {
        "avaliada", "escalada", "descartada"
    }
    # E o admin, sobre um item ESCALADO, pode — é o caminho de escalação.
    assert "devolvida" in adb.TRANSICOES["escalada"]


def test_os_desfechos_cobrem_as_escalas_e_so_elas() -> None:
    """Uma escala nova sem desfecho quebraria no import, não numa avaliação."""
    assert set(adb.DESFECHO_AVALIACAO) == set(adb.AVALIACOES_DEPOIS)
    assert set(adb.DESFECHO_DECISAO) == set(adb.DECISOES_ADMIN)
    for destino in adb.DESFECHO_AVALIACAO.values():
        assert destino in adb.TRANSICOES["pendente_avaliacao"]
    for destino in adb.DESFECHO_DECISAO.values():
        assert destino in adb.TRANSICOES["escalada"]


def test_a_rota_recusa_avaliar_o_que_nao_esta_na_passagem_2(cliente: TestClient) -> None:
    """409 sobre uma anotação que ainda está na triagem — e a mensagem diz por quê."""
    papeis = perfis_por_papel(cliente)
    ana, revisor = papeis["anotador"][0]["id"], papeis["revisor"][0]["id"]
    a = _anotar(cliente, ana)  # submetida, mas NÃO triada
    r = cliente.post(f"/api/avaliacao/{a['anotacao_id']}", json=_corpo(revisor_id=revisor))
    assert r.status_code == 409
    assert "pendente_triagem" in r.text


def test_avaliar_duas_vezes_a_mesma_anotacao_da_409(cliente: TestClient) -> None:
    """``UNIQUE(anotacao_id)``: duas abas do revisor sobre a mesma linha."""
    a, _, revisor = _pronto_para_avaliar(cliente)
    primeira = cliente.post(
        f"/api/avaliacao/{a['anotacao_id']}", json=_corpo(revisor_id=revisor)
    )
    assert primeira.status_code == 200, primeira.text
    segunda = cliente.post(
        f"/api/avaliacao/{a['anotacao_id']}", json=_corpo(revisor_id=revisor)
    )
    assert segunda.status_code == 409


# ---------------------------------------------------------------------------
# 3. O DIFF CAMPO A CAMPO
# ---------------------------------------------------------------------------


def test_achatar_desce_ate_a_folha_e_para_ali() -> None:
    """Só folhas viram caminho: um dicionário intermediário não é campo editado.

    Sem isso, "mudei a nota do critério 2" apareceria três vezes (``notas``,
    ``notas.2`` e ``notas.2.nota``) e pediria três motivos para uma mudança.
    """
    plano = avmod.achatar({"notas": [{"criterio": "x", "nota": 3}], "geral": None})
    assert plano == {"notas.0.criterio": "x", "notas.0.nota": 3, "geral": None}
    # Lista vazia é FOLHA: sem isso, apagar o último item não seria mudança.
    assert avmod.achatar({"por_criterio": []}) == {"por_criterio": []}


def test_diferencas_mantem_a_ordem_do_payload_original() -> None:
    """Ordem alfabética misturaria o critério 10 com o 1; esta acompanha o
    formulário, que é onde a pessoa está olhando."""
    antes = {"notas": [{"nota": 1}, {"nota": 2}, {"nota": 3}], "geral": "a"}
    depois = {"notas": [{"nota": 9}, {"nota": 2}, {"nota": 7}], "geral": "b"}
    campos = [d["campo"] for d in avmod.diferencas(antes, depois)]
    assert campos == ["notas.0.nota", "notas.2.nota", "geral"]


def test_como_texto_distingue_ausente_de_texto_None() -> None:
    """NULL e a string ``"None"`` são coisas diferentes, e a diferença precisa
    sobreviver ao export."""
    assert avmod.como_texto(None) is None
    assert avmod.como_texto("x") == "x"
    assert avmod.como_texto(3) == "3"
    assert avmod.como_texto(True) == "true"  # JSON, não `True` do Python
    assert len(avmod.como_texto("x" * 5_000)) == avmod.MAX_VALOR


def _com_uma_nota_trocada(env: dict[str, Any], anotacao: dict[str, Any]) -> dict[str, Any]:
    """O payload submetido, com a nota do primeiro critério mudada."""
    corrigido = json.loads(json.dumps(anotacao["payload"]))
    atual = int(corrigido["notas"][0]["nota"])
    corrigido["notas"][0]["nota"] = 1 if atual != 1 else 2
    return corrigido


def test_mudanca_sem_motivo_e_422(cliente: TestClient) -> None:
    """O diff é do SERVIDOR: declarar um diff menor não passa trabalho alheio."""
    a, _, revisor = _pronto_para_avaliar(cliente)
    det = cliente.get(
        f"/api/avaliacao/{a['anotacao_id']}", params={"revisor_id": revisor}
    ).json()
    corrigido = _com_uma_nota_trocada(det["tarefa"], det["tarefa"]["anotacao"])

    r = cliente.post(
        f"/api/avaliacao/{a['anotacao_id']}",
        json=_corpo(revisor_id=revisor, payload_corrigido=corrigido, edicoes=[]),
    )
    assert r.status_code == 422
    assert "sem motivo" in r.text
    assert "notas.0.nota" in r.text


def test_motivo_para_campo_que_nao_mudou_e_422(cliente: TestClient) -> None:
    """O simétrico, e o pior dos dois: a auditoria descreveria uma correção que
    não houve, e ninguém teria como saber."""
    a, _, revisor = _pronto_para_avaliar(cliente)
    det = cliente.get(
        f"/api/avaliacao/{a['anotacao_id']}", params={"revisor_id": revisor}
    ).json()
    corrigido = json.loads(json.dumps(det["tarefa"]["anotacao"]["payload"]))

    r = cliente.post(
        f"/api/avaliacao/{a['anotacao_id']}",
        json=_corpo(
            revisor_id=revisor,
            payload_corrigido=corrigido,
            edicoes=[{"campo": "notas.0.nota", "motivo": "inventei uma mudanca"}],
        ),
    )
    assert r.status_code == 422
    assert "nao mudou" in r.text.replace("ã", "a").replace("é", "e")


def test_motivo_curto_demais_e_422(cliente: TestClient) -> None:
    """O piso vive no ``settings.toml`` e é lido na hora da validação."""
    a, _, revisor = _pronto_para_avaliar(cliente)
    det = cliente.get(
        f"/api/avaliacao/{a['anotacao_id']}", params={"revisor_id": revisor}
    ).json()
    corrigido = _com_uma_nota_trocada(det["tarefa"], det["tarefa"]["anotacao"])
    r = cliente.post(
        f"/api/avaliacao/{a['anotacao_id']}",
        json=_corpo(
            revisor_id=revisor,
            payload_corrigido=corrigido,
            edicoes=[{"campo": "notas.0.nota", "motivo": "  x  "}],
        ),
    )
    assert r.status_code == 422


def test_justificativa_geral_vazia_e_422(cliente: TestClient) -> None:
    """Obrigatória SEMPRE, inclusive quando nada mudou e tudo pareceu ótimo."""
    a, _, revisor = _pronto_para_avaliar(cliente)
    for justificativa in ("", "   ", "curta"):
        r = cliente.post(
            f"/api/avaliacao/{a['anotacao_id']}",
            json=_corpo(revisor_id=revisor, justificativa=justificativa),
        )
        assert r.status_code == 422, justificativa


def test_o_payload_corrigido_passa_pelo_mesmo_modelo_da_submissao(
    cliente: TestClient,
) -> None:
    """O revisor corrige DENTRO do contrato: uma nota fora da escala não é
    correção, é um payload que o export não saberia ler."""
    a, _, revisor = _pronto_para_avaliar(cliente)
    det = cliente.get(
        f"/api/avaliacao/{a['anotacao_id']}", params={"revisor_id": revisor}
    ).json()
    corrigido = json.loads(json.dumps(det["tarefa"]["anotacao"]["payload"]))
    corrigido["notas"][0]["nota"] = 99
    r = cliente.post(
        f"/api/avaliacao/{a['anotacao_id']}",
        json=_corpo(
            revisor_id=revisor,
            payload_corrigido=corrigido,
            edicoes=[{"campo": "notas.0.nota", "motivo": "fora de qualquer escala"}],
        ),
    )
    assert r.status_code == 422
    assert "payload_corrigido" in r.text


def test_duas_edicoes_com_motivo_ficam_gravadas_campo_a_campo(
    cliente: TestClient, semeado: Path
) -> None:
    """O caso central do marco: duas correções, dois motivos, duas linhas.

    E os valores gravados são os do SERVIDOR — a trilha descreve o payload, não
    o que a tela afirmou ter mostrado.
    """
    a, _, revisor = _pronto_para_avaliar(cliente)
    det = cliente.get(
        f"/api/avaliacao/{a['anotacao_id']}", params={"revisor_id": revisor}
    ).json()
    original = det["tarefa"]["anotacao"]["payload"]
    corrigido = json.loads(json.dumps(original))
    nota_antes = int(corrigido["notas"][0]["nota"])
    corrigido["notas"][0]["nota"] = 1 if nota_antes != 1 else 2
    corrigido["comentario_geral"] = "The scores now match what the answer actually does."

    r = cliente.post(
        f"/api/avaliacao/{a['anotacao_id']}",
        json=_corpo(
            revisor_id=revisor,
            avaliacao_antes="ajustavel",
            payload_corrigido=corrigido,
            # O cliente MENTE no `valor_antes` de propósito: o servidor grava o
            # dele, calculado do payload.
            edicoes=[
                {"campo": "notas.0.nota", "valor_antes": "mentira", "motivo": "a nota nao cabe na escala do criterio"},
                {"campo": "comentario_geral", "motivo": "o comentario estava vazio"},
            ],
        ),
    )
    assert r.status_code == 200, r.text
    assert r.json()["n_edicoes"] == 2
    assert r.json()["status"] == "avaliada"

    conn = com_banco(semeado)
    try:
        av = conn.execute("SELECT * FROM avaliacoes").fetchone()
        assert av["avaliacao_antes"] == "ajustavel"
        assert av["avaliacao_depois"] == "adequado"
        assert json.loads(str(av["payload_corrigido_json"]))["notas"][0]["nota"] == (
            1 if nota_antes != 1 else 2
        )
        linhas = avmod.edicoes(conn, int(av["id"]))
        assert [x["campo"] for x in linhas] == ["notas.0.nota", "comentario_geral"]
        assert linhas[0]["valor_antes"] == str(nota_antes)
        assert linhas[0]["valor_antes"] != "mentira"
        assert all(x["motivo"] for x in linhas)
        # E o evento saiu na MESMA transação, com os campos alterados dentro.
        trilha = evmod.da_entidade(conn, "anotacao", a["anotacao_id"])
        registro = [x for x in trilha if x["acao"] == "avaliacao_registrada"]
        assert len(registro) == 1
        assert registro[0]["detalhe"]["campos"] == ["notas.0.nota", "comentario_geral"]
        assert registro[0]["detalhe"]["n_edicoes"] == 2
    finally:
        conn.close()


def test_avaliar_sem_mexer_em_nada_nao_grava_diff(
    cliente: TestClient, semeado: Path
) -> None:
    """``payload_corrigido_json`` fica NULL: uma cópia idêntica diria que houve
    correção onde não houve."""
    a, _, revisor = _pronto_para_avaliar(cliente)
    r = cliente.post(
        f"/api/avaliacao/{a['anotacao_id']}",
        json=_corpo(revisor_id=revisor, avaliacao_depois="excepcional"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["n_edicoes"] == 0

    conn = com_banco(semeado)
    try:
        av = conn.execute("SELECT * FROM avaliacoes").fetchone()
        assert av["payload_corrigido_json"] is None
        assert conn.execute(
            "SELECT count(*) AS n FROM edicoes_avaliacao"
        ).fetchone()["n"] == 0
    finally:
        conn.close()


def test_um_payload_corrigido_identico_tambem_nao_grava_diff(
    cliente: TestClient, semeado: Path
) -> None:
    """A tela pode mandar o payload sem ter mudado nada (o revisor abriu, olhou
    e fechou). O servidor não inventa uma correção a partir disso."""
    a, _, revisor = _pronto_para_avaliar(cliente)
    det = cliente.get(
        f"/api/avaliacao/{a['anotacao_id']}", params={"revisor_id": revisor}
    ).json()
    r = cliente.post(
        f"/api/avaliacao/{a['anotacao_id']}",
        json=_corpo(
            revisor_id=revisor,
            payload_corrigido=det["tarefa"]["anotacao"]["payload"],
        ),
    )
    assert r.status_code == 200, r.text
    assert r.json()["n_edicoes"] == 0
    conn = com_banco(semeado)
    try:
        assert conn.execute(
            "SELECT payload_corrigido_json AS p FROM avaliacoes"
        ).fetchone()["p"] is None
    finally:
        conn.close()


def test_edicoes_sem_payload_corrigido_e_422() -> None:
    """Motivos sem o payload de onde tirar o diff: o Pydantic barra com o campo."""
    from pydantic import ValidationError

    from prompt_factory.annotate.models import AvaliarIn

    with pytest.raises(ValidationError, match="payload corrigido"):
        AvaliarIn(
            revisor_id=1,
            avaliacao_antes="adequado",
            avaliacao_depois="adequado",
            justificativa="x" * 40,
            edicoes=[{"campo": "notas.0.nota", "motivo": "um motivo qualquer"}],
        )


def test_o_mesmo_campo_duas_vezes_e_422() -> None:
    """Duas linhas para o mesmo caminho fariam a auditoria contar a mesma
    mudança duas vezes."""
    from pydantic import ValidationError

    from prompt_factory.annotate.models import AvaliarIn

    with pytest.raises(ValidationError, match="duas vezes"):
        AvaliarIn(
            revisor_id=1,
            avaliacao_antes="adequado",
            avaliacao_depois="adequado",
            justificativa="x" * 40,
            payload_corrigido={"a": 1},
            edicoes=[
                {"campo": "notas.0.nota", "motivo": "um motivo qualquer"},
                {"campo": "notas.0.nota", "motivo": "outro motivo qualquer"},
            ],
        )


# ---------------------------------------------------------------------------
# 4. OS DESFECHOS
# ---------------------------------------------------------------------------


def test_incorrigivel_descarta_e_nao_reabre_a_atribuicao(
    cliente: TestClient, semeado: Path
) -> None:
    """TERMINAL. O item é descartado e a tarefa **não** volta para o anotador."""
    a, _, revisor = _pronto_para_avaliar(cliente)
    r = cliente.post(
        f"/api/avaliacao/{a['anotacao_id']}",
        json=_corpo(
            revisor_id=revisor, avaliacao_antes="inutilizavel", avaliacao_depois="incorrigivel"
        ),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "descartada"
    assert r.json()["escalada"] is False

    conn = com_banco(semeado)
    try:
        anot = conn.execute(
            "SELECT status FROM anotacoes WHERE id = ?", (a["anotacao_id"],)
        ).fetchone()
        assert anot["status"] == "descartada"
        # A ATRIBUIÇÃO continua como a triagem a deixou: aprovada, terminada.
        atr = conn.execute(
            "SELECT status FROM atribuicoes WHERE id = ?", (a["atribuicao_id"],)
        ).fetchone()
        assert atr["status"] == "aprovada"
        assert atr["status"] != "em_andamento"
        # E `descartada` é folha da máquina: não há para onde ir depois.
        assert adb.TRANSICOES["descartada"] == ()
    finally:
        conn.close()

    # O anotador não recebe a tarefa de volta pela fila (ela segue concluída
    # para ele: a atribuição não reabriu).
    minhas = cliente.get(
        "/api/atribuicoes", params={"anotador_id": perfis_por_papel(cliente)["anotador"][0]["id"]}
    ).json()
    da_tarefa = [x for x in minhas["items"] if x["atribuicao_id"] == a["atribuicao_id"]]
    assert da_tarefa and da_tarefa[0]["status"] != "em_andamento"


def test_adequado_e_excepcional_encerram(cliente: TestClient, semeado: Path) -> None:
    a, _, revisor = _pronto_para_avaliar(cliente)
    r = cliente.post(
        f"/api/avaliacao/{a['anotacao_id']}",
        json=_corpo(revisor_id=revisor, avaliacao_depois="excepcional"),
    )
    assert r.json()["status"] == "avaliada"
    assert adb.TRANSICOES["avaliada"] == ()


# ---------------------------------------------------------------------------
# 5. A ESCALAÇÃO
# ---------------------------------------------------------------------------


def _escalar(cliente: TestClient) -> tuple[dict[str, Any], int, int, int]:
    """Um item marcado ``borderline_admin``. Devolve (item, ana, revisor, admin)."""
    a, ana, revisor = _pronto_para_avaliar(cliente)
    admin = perfis_por_papel(cliente)["admin"][0]["id"]
    r = cliente.post(
        f"/api/avaliacao/{a['anotacao_id']}",
        json=_corpo(
            revisor_id=revisor,
            avaliacao_antes="ajustavel",
            avaliacao_depois="borderline_admin",
            justificativa="A escala do segundo criterio nao cobre este caso; prefiro escalar.",
        ),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "escalada"
    assert r.json()["escalada"] is True
    return {**a, "avaliacao_id": r.json()["avaliacao_id"]}, ana, revisor, admin


def test_borderline_entra_na_fila_do_admin_com_o_parecer_inteiro(
    cliente: TestClient,
) -> None:
    """O admin decide sobre a AVALIAÇÃO, não sobre o item cru — então o parecer
    (as duas notas, a justificativa e o diff) viaja junto."""
    item, _, revisor, admin = _escalar(cliente)
    fila = cliente.get("/api/escalacao/fila", params={"admin_id": admin})
    assert fila.status_code == 200, fila.text
    dados = fila.json()
    assert dados["total"] == 1
    linha = dados["items"][0]
    assert linha["avaliacao_id"] == item["avaliacao_id"]
    assert linha["anotacao_id"] == item["anotacao_id"]
    assert linha["avaliacao_antes"] == "ajustavel"
    assert linha["avaliacao_depois"] == "borderline_admin"
    assert linha["justificativa"]
    assert linha["revisor_id"] == revisor
    assert isinstance(linha["edicoes"], list)
    # E o revisor NÃO decide a própria escalação: se ele pudesse, escalar não
    # significaria nada.
    assert cliente.get(
        "/api/escalacao/fila", params={"admin_id": revisor}
    ).status_code == 403


def test_a_decisao_do_admin_encerra_e_esvazia_a_fila(
    cliente: TestClient, semeado: Path
) -> None:
    item, _, _, admin = _escalar(cliente)
    r = cliente.post(
        f"/api/escalacao/{item['avaliacao_id']}",
        json={"admin_id": admin, "decisao": "aprovada", "comentario": "vale como esta"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "avaliada"
    assert r.json()["reabriu"] is False
    assert r.json()["na_fila"] == 0

    assert cliente.get("/api/escalacao/fila", params={"admin_id": admin}).json()["total"] == 0
    # Uma segunda decisão é 409: UNIQUE(avaliacao_id) — decisão é final.
    de_novo = cliente.post(
        f"/api/escalacao/{item['avaliacao_id']}",
        json={"admin_id": admin, "decisao": "descartada"},
    )
    assert de_novo.status_code == 409

    conn = com_banco(semeado)
    try:
        assert conn.execute(
            "SELECT count(*) AS n FROM decisoes_admin"
        ).fetchone()["n"] == 1
        trilha = evmod.da_entidade(conn, "avaliacao", item["avaliacao_id"])
        assert [x["acao"] for x in trilha] == ["decisao_admin"]
        assert trilha[0]["detalhe"]["para"] == "avaliada"
    finally:
        conn.close()


def test_o_admin_pode_devolver_e_isso_reabre_a_atribuicao(
    cliente: TestClient, semeado: Path
) -> None:
    """Devolver AQUI é permitido — é o admin, não a segunda passagem.

    E reabre a MESMA atribuição, como a triagem: uma segunda faria o mesmo
    trabalho contar duas vezes em toda métrica.
    """
    item, ana, _, admin = _escalar(cliente)
    r = cliente.post(
        f"/api/escalacao/{item['avaliacao_id']}",
        json={
            "admin_id": admin,
            "decisao": "devolvida",
            "comentario": "refaca o segundo criterio com a escala certa",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "devolvida"
    assert r.json()["reabriu"] is True

    conn = com_banco(semeado)
    try:
        atr = conn.execute(
            "SELECT status, terminada_em FROM atribuicoes WHERE id = ?",
            (item["atribuicao_id"],),
        ).fetchone()
        assert atr["status"] == "em_andamento"
        assert atr["terminada_em"] is None
        assert conn.execute(
            "SELECT count(*) AS n FROM atribuicoes WHERE anotador_id = ?", (ana,)
        ).fetchone()["n"] >= 1
    finally:
        conn.close()


def test_o_comentario_do_admin_chega_ao_anotador(cliente: TestClient) -> None:
    """São DOIS caminhos de volta, e o segundo some sem ninguém notar.

    A rota exige o comentário com a frase "é a única instrução que ele recebe".
    Sem o JOIN de ``decisoes_admin`` em ``_versao_anterior``, essa frase seria
    falsa: o anotador reabriria o formulário preenchido e sem instrução nenhuma.
    """
    item, ana, _, admin = _escalar(cliente)
    cliente.post(
        f"/api/escalacao/{item['avaliacao_id']}",
        json={
            "admin_id": admin,
            "decisao": "devolvida",
            "comentario": "refaca o segundo criterio com a escala certa",
        },
    )
    minhas = cliente.get("/api/atribuicoes", params={"anotador_id": ana}).json()
    devolvida = next(
        x for x in minhas["items"] if x["atribuicao_id"] == item["atribuicao_id"]
    )
    assert devolvida["status"] == "em_andamento"
    # O comentário do ADMIN sai em campo próprio: deduzir "veio da triagem" pela
    # ausência do outro faria a lista atribuir ao revisor uma frase do admin.
    assert devolvida["decisao_admin"] == "devolvida"
    assert "refaca o segundo criterio" in devolvida["comentario_admin"]
    assert devolvida["veredito"] == "aprovada"  # a triagem aprovou; quem devolveu foi o admin
    env = cliente.get(
        f"/api/atribuicoes/{item['atribuicao_id']}", params={"anotador_id": ana}
    )
    assert env.status_code == 200, env.text
    anterior = env.json()["tarefa"]["versao_anterior"]
    assert anterior["decisao_admin"] == "devolvida"
    assert "refaca o segundo criterio" in anterior["comentario_admin"]


def test_devolver_sem_comentario_e_422(cliente: TestClient) -> None:
    """O comentário atravessa dois saltos (admin → anotador) e é tudo que chega."""
    item, _, _, admin = _escalar(cliente)
    r = cliente.post(
        f"/api/escalacao/{item['avaliacao_id']}",
        json={"admin_id": admin, "decisao": "devolvida"},
    )
    assert r.status_code == 422
    curto = cliente.post(
        f"/api/escalacao/{item['avaliacao_id']}",
        json={"admin_id": admin, "decisao": "devolvida", "comentario": "nao"},
    )
    assert curto.status_code == 422


def test_devolver_renova_o_prazo_da_atribuicao(
    cliente: TestClient, semeado: Path
) -> None:
    """Uma devolução chega HORAS depois do claim — e o prazo era do claim.

    Medido no banco real: devolver um item aprovado dias antes reabria a
    atribuição com ``expira_em`` no passado, o ``expirar_vencidas`` da listagem
    seguinte a marcava ``expirada`` no mesmo request, e o anotador via
    "prazo expirado" **sem botão** no lugar da instrução. Beco.
    """
    item, ana, _, admin = _escalar(cliente)
    # Envelhece o prazo, como o relógio faria.
    conn = com_banco(semeado)
    try:
        conn.execute(
            "UPDATE atribuicoes SET expira_em = '2020-01-01T00:00:00.000Z' WHERE id = ?",
            (item["atribuicao_id"],),
        )
    finally:
        conn.close()

    cliente.post(
        f"/api/escalacao/{item['avaliacao_id']}",
        json={
            "admin_id": admin,
            "decisao": "devolvida",
            "comentario": "refaca o segundo criterio com a escala certa",
        },
    )
    # A LISTAGEM roda `expirar_vencidas` antes de responder: é ela que expunha o
    # defeito, e é ela que prova o conserto.
    minhas = cliente.get("/api/atribuicoes", params={"anotador_id": ana}).json()
    linha = next(x for x in minhas["items"] if x["atribuicao_id"] == item["atribuicao_id"])
    assert linha["status"] == "em_andamento"
    assert linha["expira_em"] > "2026"


def test_a_triagem_tambem_renova_o_prazo_ao_devolver(
    cliente: TestClient, semeado: Path
) -> None:
    """O MESMO conserto no outro caminho de volta — os dois usam
    ``tarefas.reabrir``, que é o único lugar onde a regra do prazo mora."""
    papeis = perfis_por_papel(cliente)
    ana, revisor = papeis["anotador"][0]["id"], papeis["revisor"][0]["id"]
    a = _anotar(cliente, ana)
    conn = com_banco(semeado)
    try:
        conn.execute(
            "UPDATE atribuicoes SET expira_em = '2020-01-01T00:00:00.000Z' WHERE id = ?",
            (a["atribuicao_id"],),
        )
    finally:
        conn.close()
    r = cliente.post(
        f"/api/revisao/{a['anotacao_id']}",
        json={
            "revisor_id": revisor,
            "veredito": "devolvida",
            "comentario": "descreva as duas pontas da escala antes de reenviar",
        },
    )
    assert r.status_code == 200, r.text
    minhas = cliente.get("/api/atribuicoes", params={"anotador_id": ana}).json()
    linha = next(x for x in minhas["items"] if x["atribuicao_id"] == a["atribuicao_id"])
    assert linha["status"] == "em_andamento"
    assert linha["expira_em"] > "2026"


def test_o_modo_livre_devolvido_continua_sem_prazo(
    cliente: TestClient, semeado: Path
) -> None:
    """``expira_em IS NULL`` é o modo livre — reabrir não inventa um relógio
    para quem escolheu a tarefa no catálogo."""
    papeis = perfis_por_papel(cliente)
    ana, revisor = papeis["anotador"][0]["id"], papeis["revisor"][0]["id"]
    a = _anotar(cliente, ana)
    conn = com_banco(semeado)
    try:
        conn.execute(
            "UPDATE atribuicoes SET expira_em = NULL WHERE id = ?", (a["atribuicao_id"],)
        )
    finally:
        conn.close()
    cliente.post(
        f"/api/revisao/{a['anotacao_id']}",
        json={
            "revisor_id": revisor,
            "veredito": "devolvida",
            "comentario": "descreva as duas pontas da escala antes de reenviar",
        },
    )
    conn = com_banco(semeado)
    try:
        assert conn.execute(
            "SELECT expira_em FROM atribuicoes WHERE id = ?", (a["atribuicao_id"],)
        ).fetchone()["expira_em"] is None
    finally:
        conn.close()


def test_descartar_pelo_admin_encerra_sem_reabrir(
    cliente: TestClient, semeado: Path
) -> None:
    item, _, _, admin = _escalar(cliente)
    r = cliente.post(
        f"/api/escalacao/{item['avaliacao_id']}",
        json={"admin_id": admin, "decisao": "descartada"},
    )
    assert r.json()["status"] == "descartada"
    assert r.json()["reabriu"] is False
    conn = com_banco(semeado)
    try:
        assert conn.execute(
            "SELECT status FROM atribuicoes WHERE id = ?", (item["atribuicao_id"],)
        ).fetchone()["status"] == "aprovada"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6. A FILA DA PASSAGEM 2
# ---------------------------------------------------------------------------


def test_a_fila_traz_o_que_a_triagem_aprovou_com_quem_aprovou(
    cliente: TestClient,
) -> None:
    """``triador`` sai em cada item: a métrica "a triagem deixou passar" precisa
    dos dois nomes juntos desde a fila."""
    a, ana, revisor = _pronto_para_avaliar(cliente)
    fila = cliente.get("/api/avaliacao/fila", params={"revisor_id": revisor}).json()
    ids = {i["anotacao_id"] for i in fila["items"]}
    assert a["anotacao_id"] in ids
    item = next(i for i in fila["items"] if i["anotacao_id"] == a["anotacao_id"])
    assert item["triador"]
    assert item["triada_em"]
    assert item["prompt"], "sem o trecho do prompt, um uid não diz qual item é qual"
    assert item["minha"] is False
    # E o anotador não abre a fila do revisor.
    assert cliente.get(
        "/api/avaliacao/fila", params={"revisor_id": ana}
    ).status_code == 403


def test_a_fila_da_passagem_2_exclui_o_proprio_trabalho(
    cliente: TestClient, semeado: Path
) -> None:
    """Quem avalia nunca é quem anotou — e o número das próprias é declarado,
    senão a fila vazia parece trabalho perdido."""
    papeis = perfis_por_papel(cliente)
    ana = papeis["anotador"][0]["id"]
    revisor = papeis["revisor"][0]["id"]
    a = _anotar(cliente, ana)
    _triar(cliente, revisor, a["anotacao_id"])
    # A autoria passa a ser do revisor (o mesmo estado de quem anotou antes de
    # virar revisor).
    conn = com_banco(semeado)
    try:
        conn.execute(
            "UPDATE atribuicoes SET anotador_id = ? WHERE id = ?",
            (revisor, a["atribuicao_id"]),
        )
    finally:
        conn.close()

    fila = cliente.get("/api/avaliacao/fila", params={"revisor_id": revisor}).json()
    assert a["anotacao_id"] not in {i["anotacao_id"] for i in fila["items"]}
    assert fila["minhas_excluidas"] == 1
    assert fila["modo_solo"] is False

    r = cliente.get(f"/api/avaliacao/{a['anotacao_id']}", params={"revisor_id": revisor})
    assert r.status_code == 403
    assert "permitir_autorrevisao" in r.text


def test_modo_solo_deixa_avaliar_o_proprio_e_GRAVA_isso(
    cliente: TestClient, semeado: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regra afrouxada é DECLARADA: fila marcada, ``autorrevisao = 1`` gravado.

    Ela é gravada, e não deduzida depois, porque ``atribuicoes.anotador_id`` pode
    ser corrigido por um admin e a derivação passaria a mentir sobre uma
    avaliação que já aconteceu.
    """
    monkeypatch.setitem(config.settings()["annotate"], "permitir_autorrevisao", True)
    papeis = perfis_por_papel(cliente)
    ana = papeis["anotador"][0]["id"]
    a = _anotar(cliente, ana)
    _triar(cliente, ana, a["anotacao_id"])  # no modo solo o papel é vista

    fila = cliente.get("/api/avaliacao/fila", params={"revisor_id": ana}).json()
    assert fila["modo_solo"] is True
    assert fila["minhas_excluidas"] == 0
    meu = [i for i in fila["items"] if i["anotacao_id"] == a["anotacao_id"]]
    assert meu and meu[0]["minha"] is True

    r = cliente.post(
        f"/api/avaliacao/{a['anotacao_id']}", json=_corpo(revisor_id=ana)
    )
    assert r.status_code == 200, r.text
    assert r.json()["autorrevisao"] is True
    conn = com_banco(semeado)
    try:
        assert conn.execute(
            "SELECT autorrevisao FROM avaliacoes"
        ).fetchone()["autorrevisao"] == 1
    finally:
        conn.close()


def test_o_health_conta_as_avaliacoes(cliente: TestClient) -> None:
    """O card do painel do admin lê daqui, e os dois pisos novos saem no health
    — senão o botão "faltam 12 caracteres" divergiria do 422."""
    a, _, revisor = _pronto_para_avaliar(cliente)
    cliente.post(f"/api/avaliacao/{a['anotacao_id']}", json=_corpo(revisor_id=revisor))
    saude = cliente.get("/api/health").json()
    assert saude["anotacao"]["contagens"]["avaliacoes"] == 1
    assert saude["limites"]["min_chars_avaliacao"] >= 1
    assert saude["limites"]["min_chars_motivo_edicao"] >= 1


# ---------------------------------------------------------------------------
# 7. A MIGRAÇÃO 2 -> 3
# ---------------------------------------------------------------------------


def _banco_v2_com_trabalho(caminho: Path, corpus: Path) -> dict[str, int]:
    """Um banco na v2 com anotação, triagem, avaliação, edição e decisão.

    Construído a partir de um banco de HOJE e rebaixado a v2 (removendo a coluna
    da v3), que é exatamente o estado do banco do dono antes deste marco.
    """
    conn = dbmod.connect(caminho)
    corpo = dbmod.connect(corpus, readonly=True)
    try:
        adb.init_db(conn)
        seedmod.semear(conn, corpo)
    finally:
        conn.close()
        corpo.close()

    with TestClient(criar_app(caminho, corpus)) as c:
        papeis = perfis_por_papel(c)
        ana, revisor = papeis["anotador"][0]["id"], papeis["revisor"][0]["id"]
        admin = papeis["admin"][0]["id"]
        a = _anotar(c, ana)
        _triar(c, revisor, a["anotacao_id"])
        det = c.get(f"/api/avaliacao/{a['anotacao_id']}", params={"revisor_id": revisor}).json()
        corrigido = _com_uma_nota_trocada(det["tarefa"], det["tarefa"]["anotacao"])
        r = c.post(
            f"/api/avaliacao/{a['anotacao_id']}",
            json=_corpo(
                revisor_id=revisor,
                avaliacao_depois="borderline_admin",
                payload_corrigido=corrigido,
                edicoes=[{"campo": "notas.0.nota", "motivo": "a nota nao bate com o texto"}],
            ),
        )
        assert r.status_code == 200, r.text
        avaliacao_id = r.json()["avaliacao_id"]
        d = c.post(
            f"/api/escalacao/{avaliacao_id}",
            json={"admin_id": admin, "decisao": "aprovada", "comentario": "ok"},
        )
        assert d.status_code == 200, d.text

    # Rebaixa para a v2: tira a coluna da v3 e carimba a versão antiga.
    conn = com_banco(caminho)
    try:
        conn.execute(
            f"ALTER TABLE anotacoes DROP COLUMN {adb.COLUNA_GABARITO_AVALIACAO}"
        )
        adb.set_meta(conn, adb.CHAVE_VERSAO, 2)
        antes = adb.contagens(conn)
    finally:
        conn.close()
    return antes


def test_migrar_da_v2_preserva_o_trabalho_inteiro(banco: Path, corpus: Path) -> None:
    """O caso REAL deste marco: um banco com avaliações e decisões já dentro."""
    antes = _banco_v2_com_trabalho(banco, corpus)
    assert antes["avaliacoes"] == 1 and antes["decisoes_admin"] == 1

    rel = migracao.migrar(banco)

    assert rel["ja_estava"] is False
    assert rel["de"] == 2 and rel["para"] == adb.SCHEMA_VERSION_ANOTACAO
    assert rel["backup"].endswith(".v2.bak")
    assert Path(rel["backup"]).is_file()

    conn = com_banco(banco)
    try:
        assert adb.versao_do_banco(conn) == adb.SCHEMA_VERSION_ANOTACAO
        depois = adb.contagens(conn)
        for tabela, n in antes.items():
            if tabela in migracao.TABELAS_QUE_CRESCEM:
                # `app_meta` ganha chaves e `eventos` ganha o `banco_migrado`.
                assert depois[tabela] >= n, tabela
                continue
            assert depois[tabela] == n, tabela
        assert [
            str(x["acao"])
            for x in conn.execute("SELECT acao FROM eventos ORDER BY id DESC LIMIT 1")
        ] == ["banco_migrado"]
        # O CONTEÚDO, não só a contagem.
        av = conn.execute("SELECT * FROM avaliacoes").fetchone()
        assert av["justificativa"]
        assert json.loads(str(av["payload_corrigido_json"]))
        ed = conn.execute("SELECT * FROM edicoes_avaliacao").fetchone()
        assert ed["campo"] == "notas.0.nota" and ed["motivo"]
        dec = conn.execute("SELECT * FROM decisoes_admin").fetchone()
        assert dec["decisao"] == "aprovada"
        # E a coluna nova nasce NULL em todo trabalho humano.
        nulos = conn.execute(
            f"SELECT count(*) AS n FROM anotacoes "
            f"WHERE {adb.COLUNA_GABARITO_AVALIACAO} IS NOT NULL"
        ).fetchone()["n"]
        assert nulos == 0
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()


def test_o_schema_migrado_da_v2_e_identico_ao_de_um_banco_novo(
    banco: Path, corpus: Path, tmp_path: Path
) -> None:
    """A propriedade que o ``ALTER TABLE`` não daria — e a razão de reconstruir
    ao lado. Um CHECK velho sobrevivendo passa em todo teste de existência."""
    _banco_v2_com_trabalho(banco, corpus)
    migracao.migrar(banco)

    novo = tmp_path / "novo.sqlite"
    conn_novo = dbmod.connect(novo)
    try:
        adb.init_db(conn_novo)
        esperado = _sqlite_master(conn_novo)
    finally:
        conn_novo.close()
    conn = com_banco(banco)
    try:
        assert _sqlite_master(conn) == esperado
    finally:
        conn.close()


def _sqlite_master(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return [
        (str(x["type"]), str(x["name"]), " ".join(str(x["sql"] or "").split()))
        for x in conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
    ]


def test_migrar_da_v1_continua_chegando_na_versao_de_hoje(banco: Path) -> None:
    """A v1 pula direto para o schema corrente — sem passo intermediário.

    Encadear 1→2→3 exigiria manter vivo o DDL de cada versão do meio, e o teste
    que compara ``sqlite_master`` com um banco novo deixaria de valer no meio da
    cadeia.
    """
    conn = dbmod.connect(banco)
    try:
        init_v1(conn)
    finally:
        conn.close()
    rel = migracao.migrar(banco)
    assert rel["de"] == 1 and rel["para"] == adb.SCHEMA_VERSION_ANOTACAO
    assert rel["backup"].endswith(".v1.bak")
    conn = com_banco(banco)
    try:
        assert adb.versao_do_banco(conn) == adb.SCHEMA_VERSION_ANOTACAO
        colunas = {str(x["name"]) for x in conn.execute("PRAGMA table_info(anotacoes)")}
        assert adb.COLUNA_GABARITO_AVALIACAO in colunas
    finally:
        conn.close()


def test_as_duas_origens_conhecidas_tem_passo() -> None:
    """Uma versão listada e sem função seria um ``KeyError`` numa migração."""
    assert set(migracao.PASSOS) == set(migracao.ORIGENS_CONHECIDAS)
    assert adb.SCHEMA_VERSION_ANOTACAO not in migracao.PASSOS


def test_a_copia_da_v2_cobre_todas_as_tabelas(banco: Path) -> None:
    """Uma tabela nova que ninguém copiasse sairia vazia da migração — e a
    conferência de contagens só a pegaria se ela tivesse linhas."""
    cobertas = (
        {t for t, _ in migracao.COPIA_V2_ANTES}
        | {t for t, _ in migracao.COPIA_V2_DEPOIS}
        | {"anotacoes", "app_meta"}
    )
    assert cobertas == set(adb.TABELAS)
