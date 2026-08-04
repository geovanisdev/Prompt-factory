"""P5b: os artefatos de entrega — o que sai da Bancada e o que NUNCA sai.

Cada família aqui existe por um erro que **não parece quebrado** no arquivo
gerado:

* **entregar o que não terminou o QC.** Um item aprovado na triagem e nunca
  avaliado sai idêntico a um que atravessou as duas passagens — mesmo prompt,
  mesmo payload, mesmo formato. O que muda é a única coisa que o cliente está
  comprando: a prova de que alguém conferiu;
* **entregar a sintética como humana.** Ela tem a mesma forma de uma anotação de
  gente. A marca é uma coluna (``gabarito_avaliacao_json``) e nada no JSON
  denuncia a origem se o export não a declarar;
* **vazar o alvo escondido** num artefato. O arquivo continua válido e a
  calibração passa a medir quem leu o JSON;
* **entregar o payload como submetido** depois de a passagem 2 o ter corrigido —
  ou entregar só o corrigido, apagando a metade da trilha que prova o QC;
* **par de preferência com escolhido == rejeitado** (o empate). Não levanta erro
  em lugar nenhum e envenena qualquer treino de recompensa que o leia;
* **linha sem licença e sem atribuição.** ODC-BY, CC-BY e CC-BY-SA exigem
  crédito a cada uso: um JSONL sem ele é um problema jurídico com cara de
  arquivo pronto.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from prompt_factory import db as dbmod
from prompt_factory.annotate import db as adb
from prompt_factory.annotate import entrega
from prompt_factory.annotate import seed as seedmod
from prompt_factory.annotate.main import criar_app
from prompt_factory.export import IDIOMA_DOS_ARTEFATOS, sha256_arquivo

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


@pytest.fixture
def conexoes(semeado: Path, corpus: Path):
    """As duas conexões como o ``pf annotate export`` as abre."""
    conn = dbmod.connect(semeado)
    corpo = dbmod.connect(corpus, readonly=True)
    try:
        yield conn, corpo
    finally:
        conn.close()
        corpo.close()


# ---------------------------------------------------------------------------
# condutores: levar um item por toda a esteira, pela API
# ---------------------------------------------------------------------------

SFT_LONGA = (
    "Comece medindo antes de mudar qualquer coisa: rode a consulta com EXPLAIN e "
    "confira se o plano diz COVERING INDEX. Se disser apenas USING INDEX, o índice "
    "tem a coluna do agrupamento mas não a do filtro, e cada entrada vira uma busca "
    "na tabela — que é o caso mais lento dos três possíveis aqui."
)

JUSTIFICATIVA = "The scores follow the rubric and each one is supported by the text."


def _papeis(cliente: TestClient) -> dict[str, list[int]]:
    saida: dict[str, list[int]] = {}
    for p in cliente.get("/api/perfis").json()["items"]:
        saida.setdefault(p["papel"], []).append(p["id"])
    return saida


def _payload_para(tipo: str, env: dict[str, Any]) -> dict[str, Any]:
    if tipo == "avaliar_rubrica":
        return notas_da_rubrica(env)
    if tipo == "escrever_rubrica":
        return rubrica_ok()
    if tipo == "sft_resposta":
        return {"resposta": SFT_LONGA}
    if tipo == "comparar_ab":
        return {
            "preferencia": "modelo-a",
            "justificativa": "A answers the actual question; B is fluent and empty.",
        }
    raise AssertionError(f"sem payload de teste para {tipo}")  # pragma: no cover


def _anotar(cliente: TestClient, quem: int, tipo: str) -> dict[str, Any]:
    env = cliente.post(
        "/api/tarefas/proxima", json={"anotador_id": quem, "tipo": tipo}
    ).json()["tarefa"]
    assert env is not None, f"fila de {tipo} vazia"
    r = cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
        json={"anotador_id": quem, "payload": _payload_para(tipo, env), "tempo_ativo_ms": 9_000},
    )
    assert r.status_code == 200, r.text
    return {"env": env, **r.json()}


def _triar(cliente: TestClient, revisor: int, anotacao_id: int) -> None:
    r = cliente.post(
        f"/api/revisao/{anotacao_id}", json={"revisor_id": revisor, "veredito": "aprovada"}
    )
    assert r.status_code == 200, r.text


def _avaliar(
    cliente: TestClient,
    revisor: int,
    anotacao_id: int,
    *,
    antes: str = "adequado",
    depois: str = "adequado",
    corpo_extra: dict[str, Any] | None = None,
) -> None:
    corpo: dict[str, Any] = {
        "revisor_id": revisor,
        "avaliacao_antes": antes,
        "avaliacao_depois": depois,
        "justificativa": JUSTIFICATIVA,
        **(corpo_extra or {}),
    }
    r = cliente.post(f"/api/avaliacao/{anotacao_id}", json=corpo)
    assert r.status_code == 200, r.text


def _esteira(cliente: TestClient, tipo: str, **avaliacao: Any) -> dict[str, Any]:
    """Anotar → triar → avaliar. Devolve o dicionário da submissão."""
    papeis = _papeis(cliente)
    quem, revisor = papeis["anotador"][0], papeis["revisor"][0]
    a = _anotar(cliente, quem, tipo)
    _triar(cliente, revisor, a["anotacao_id"])
    _avaliar(cliente, revisor, a["anotacao_id"], **avaliacao)
    return a


def _exportar(conexoes: Any, perfil: str, destino: Path, **kw: Any) -> entrega.Resultado:
    conn, corpo = conexoes
    return entrega.executar(conn, corpo, perfil, destino_dir=destino, **kw)


def _linhas(res: entrega.Resultado) -> list[dict[str, Any]]:
    texto = res.arquivo.read_text(encoding="utf-8")
    return [json.loads(linha) for linha in texto.splitlines() if linha.strip()]


# ---------------------------------------------------------------------------
# 1. O QUE CONTA COMO ENTREGUE
# ---------------------------------------------------------------------------


def test_o_que_nao_atravessou_as_duas_passagens_fica_de_fora(
    cliente: TestClient, conexoes: Any, tmp_path: Path
) -> None:
    """Aprovado na triagem e nunca avaliado NÃO é entregável.

    É o defeito mais silencioso do marco: o item sai idêntico a um que passou
    pelo QC inteiro, e a única diferença é justamente o que o cliente compra.
    """
    papeis = _papeis(cliente)
    a = _anotar(cliente, papeis["anotador"][0], "avaliar_rubrica")
    _triar(cliente, papeis["revisor"][0], a["anotacao_id"])

    res = _exportar(conexoes, "annotations", tmp_path / "a")
    assert res.row_count == 0
    assert res.manifest["statuses"] == list(entrega.STATUS_ENTREGUE)

    # A pedido, entra — e o manifesto diz em letra maiúscula que entrou.
    com = _exportar(conexoes, "annotations", tmp_path / "b", incluir_pendentes=True)
    assert com.row_count == 1
    assert "NOTE_PENDING" in com.manifest
    assert _linhas(com)[0]["qc"]["rating"] is None


def test_o_descartado_nunca_e_entregue(
    cliente: TestClient, conexoes: Any, tmp_path: Path
) -> None:
    _esteira(cliente, "avaliar_rubrica", antes="inutilizavel", depois="incorrigivel")
    for pendentes in (False, True):
        res = _exportar(
            conexoes, "annotations", tmp_path / str(pendentes), incluir_pendentes=pendentes
        )
        assert res.row_count == 0, "item `descartada` vazou para a entrega"


def test_o_avaliado_sai_com_a_trilha_de_qc_inteira(
    cliente: TestClient, conexoes: Any, tmp_path: Path
) -> None:
    _esteira(cliente, "avaliar_rubrica")
    (linha,) = _linhas(_exportar(conexoes, "annotations", tmp_path))
    qc = linha["qc"]
    assert qc["triage"]["verdict"] == "aprovada"
    assert qc["rating"]["before_edit"] == "adequado"
    assert qc["rating"]["after_edit"] == "adequado"
    assert qc["rating"]["rationale"] == JUSTIFICATIVA
    assert qc["status"] == "avaliada"
    assert linha["guideline_version"] is not None, "a diretriz vigente tem de viajar junto"


# ---------------------------------------------------------------------------
# 2. LICENÇA E ATRIBUIÇÃO POR LINHA
# ---------------------------------------------------------------------------


def test_toda_linha_de_dado_carrega_proveniencia(
    cliente: TestClient, conexoes: Any, tmp_path: Path
) -> None:
    _esteira(cliente, "avaliar_rubrica")
    _esteira(cliente, "sft_resposta")
    for perfil, caminho in (("annotations", "prompt"), ("sft", None)):
        for linha in _linhas(_exportar(conexoes, perfil, tmp_path / perfil)):
            bloco = linha[caminho] if caminho else linha
            for campo in ("uid", "lang", "source", "license", "license_class"):
                assert bloco.get(campo), f"{perfil}: {campo} ausente"
            assert "attribution" in bloco
            assert "is_demo" in bloco


def test_o_pacote_de_demonstracao_sai_marcado_e_nao_disfarcado(
    cliente: TestClient, conexoes: Any, tmp_path: Path
) -> None:
    """``is_demo`` e ``synthetic`` são eixos DIFERENTES.

    Um fala de quem escreveu o PROMPT, o outro de quem escreveu a ANOTAÇÃO.
    Confundi-los descartaria trabalho humano feito sobre fixture.
    """
    _esteira(cliente, "avaliar_rubrica")
    (linha,) = _linhas(_exportar(conexoes, "annotations", tmp_path))
    assert linha["prompt"]["is_demo"] is True
    assert linha["synthetic"] is False


# ---------------------------------------------------------------------------
# 3. A SINTÉTICA
# ---------------------------------------------------------------------------


def _plantar(banco: Path, anotacao_id: int, alvo: dict[str, Any]) -> None:
    conn = dbmod.connect(banco)
    try:
        conn.execute(
            f"UPDATE anotacoes SET {adb.COLUNA_GABARITO_AVALIACAO} = ? WHERE id = ?",
            (json.dumps(alvo, ensure_ascii=False), anotacao_id),
        )
        conn.commit()
    finally:
        conn.close()


SENHA = "ALVO-QUE-NAO-PODE-VAZAR-7f3a"


def test_a_sintetica_fica_fora_dos_perfis_de_dado_por_padrao(
    cliente: TestClient, semeado: Path, conexoes: Any, tmp_path: Path
) -> None:
    a = _esteira(cliente, "avaliar_rubrica")
    _plantar(
        semeado,
        a["anotacao_id"],
        {"avaliacao_antes": "adequado", "familia_defeito": "nenhum", "nota": SENHA},
    )
    assert _exportar(conexoes, "annotations", tmp_path / "sem").row_count == 0

    com = _exportar(conexoes, "annotations", tmp_path / "com", incluir_sinteticas=True)
    assert com.row_count == 1
    assert _linhas(com)[0]["synthetic"] is True
    assert "WARNING" in com.manifest, "incluir sintética sem aviso no manifesto"


@pytest.mark.parametrize("perfil", ["annotations", "sft", "preference"])
def test_o_alvo_escondido_nao_vaza_para_perfil_de_dado_nenhum(
    cliente: TestClient, semeado: Path, conexoes: Any, tmp_path: Path, perfil: str
) -> None:
    """Varre o arquivo INTEIRO pela senha, e não pelo nome da chave.

    Conferir a chave passaria hoje e falharia em silêncio no dia em que alguém a
    renomeasse ao serializar — é a mesma disciplina do teste do P3b.
    """
    a = _esteira(cliente, "avaliar_rubrica")
    _plantar(
        semeado,
        a["anotacao_id"],
        {"avaliacao_antes": "ajustavel", "familia_defeito": "nota_inflada", "nota": SENHA},
    )
    res = _exportar(conexoes, perfil, tmp_path, incluir_sinteticas=True)
    assert SENHA not in res.arquivo.read_text(encoding="utf-8")
    assert SENHA not in json.dumps(res.manifest, ensure_ascii=False)


def test_a_calibracao_do_revisor_mede_distancia_e_nao_so_acerto(
    cliente: TestClient, semeado: Path, conexoes: Any, tmp_path: Path
) -> None:
    """A escala é ORDINAL: errar por um passo não é errar por três."""
    papeis = _papeis(cliente)
    revisor = papeis["revisor"][0]
    # O revisor diz `adequado` (índice 2) nas duas; os alvos são `adequado`
    # (acerto, distância 0) e `inutilizavel` (índice 0, distância 2). Média 1,0 —
    # e é justamente essa média que uma taxa de acerto exato não mostraria.
    for alvo in ("adequado", "inutilizavel"):
        a = _anotar(cliente, papeis["anotador"][0], "avaliar_rubrica")
        _plantar(
            semeado,
            a["anotacao_id"],
            {"avaliacao_antes": alvo, "familia_defeito": "generica", "nota": "x"},
        )
        _triar(cliente, revisor, a["anotacao_id"])
        _avaliar(cliente, revisor, a["anotacao_id"], antes="adequado", depois="adequado")

    conn, corpo = conexoes
    itens = entrega.coletar(conn, corpo, incluir_sinteticas=True)
    cal = entrega.calibracao_do_revisor(itens)
    assert cal["n"] == 2
    assert cal["exact"] == 1
    assert cal["within_one"] == 1, "um acerto e um erro de dois passos"
    assert cal["mean_absolute_error"] == pytest.approx(1.0)
    assert cal["scale"] == list(adb.AVALIACOES_ANTES)

    md = _exportar(conexoes, "quality-report", tmp_path).arquivo.read_text(encoding="utf-8")
    assert "mean absolute error" in md


def test_sem_sintetica_avaliada_a_calibracao_e_NAO_MEDIDA(
    cliente: TestClient, conexoes: Any, tmp_path: Path
) -> None:
    """``None`` e ``0.0`` são afirmações diferentes — a mesma lição do agreement."""
    _esteira(cliente, "avaliar_rubrica")
    conn, corpo = conexoes
    cal = entrega.calibracao_do_revisor(entrega.coletar(conn, corpo, incluir_sinteticas=True))
    assert cal["n"] == 0
    assert "exact_rate" not in cal
    md = _exportar(conexoes, "quality-report", tmp_path).arquivo.read_text(encoding="utf-8")
    assert "not measured" in md


# ---------------------------------------------------------------------------
# 4. A MÉTRICA QUE MEDE A TRIAGEM
# ---------------------------------------------------------------------------


def test_o_que_a_triagem_deixou_passar_aparece_nomeado(
    cliente: TestClient, conexoes: Any, tmp_path: Path
) -> None:
    """O caso interessante NÃO é o descartado — é o resgatado.

    Um item que a triagem aprovou, chegou ``inutilizavel`` à passagem 2 e saiu
    ``adequado`` continua entregue: o revisor o consertou. Mas ele passou por um
    portão que devia tê-lo barrado, e é o portão que essa métrica mede. Se ela só
    contasse os descartados, o caso mais comum de falha da triagem — aquele em
    que alguém pagou para consertar — ficaria invisível.
    """
    _esteira(cliente, "avaliar_rubrica")  # chegou bem
    ruim = _esteira(cliente, "avaliar_rubrica", antes="inutilizavel", depois="adequado")

    conn, corpo = conexoes
    vaz = entrega.triagem_deixou_passar(entrega.coletar(conn, corpo))
    assert vaz["approved_by_triage"] == 2
    assert vaz["then_rated"] == 2
    assert vaz["arrived_unusable"] == 1
    assert vaz["ended_unsalvageable"] == 0
    assert vaz["leak_rate"] == pytest.approx(0.5)
    assert vaz["annotation_ids"]["arrived_unusable"] == [ruim["anotacao_id"]]

    res = _exportar(conexoes, "quality-report", tmp_path)
    md = res.arquivo.read_text(encoding="utf-8")
    assert "measures **the triage reviewer**" in md
    assert res.manifest["triage_leak"]["arrived_unusable"] == 1


# ---------------------------------------------------------------------------
# 5. O PAYLOAD ENTREGUE É O CORRIGIDO — E O SUBMETIDO NÃO SE PERDE
# ---------------------------------------------------------------------------


def test_a_correcao_da_passagem_2_e_o_que_sai_com_o_original_ao_lado(
    cliente: TestClient, conexoes: Any, tmp_path: Path
) -> None:
    papeis = _papeis(cliente)
    quem, revisor = papeis["anotador"][0], papeis["revisor"][0]
    a = _anotar(cliente, quem, "avaliar_rubrica")
    _triar(cliente, revisor, a["anotacao_id"])

    env = cliente.get(
        f"/api/avaliacao/{a['anotacao_id']}", params={"revisor_id": revisor}
    ).json()
    original = env["tarefa"]["anotacao"]["payload"]
    submetida = int(original["notas"][0]["nota"])
    corrigido = json.loads(json.dumps(original))
    corrigido["notas"][0]["nota"] = submetida - 1
    caminho = "notas.0.nota"
    _avaliar(
        cliente,
        revisor,
        a["anotacao_id"],
        antes="ajustavel",
        corpo_extra={
            "payload_corrigido": corrigido,
            "edicoes": [{"campo": caminho, "motivo": "The text does not support the top mark."}],
        },
    )

    (linha,) = _linhas(_exportar(conexoes, "annotations", tmp_path))
    assert linha["corrected_by_reviewer"] is True
    assert linha["payload"]["notas"][0]["nota"] == submetida - 1, "o entregue é o corrigido"
    assert linha["payload_as_submitted"]["notas"][0]["nota"] == submetida
    (edicao,) = linha["qc"]["rating"]["edits"]
    assert edicao == {
        "field": caminho,
        "from": str(submetida),
        "to": str(submetida - 1),
        "reason": "The text does not support the top mark.",
    }


def test_sem_correcao_o_payload_original_nao_e_duplicado(
    cliente: TestClient, conexoes: Any, tmp_path: Path
) -> None:
    """Repetir a árvore inteira para dizer "nada mudou" dobraria o arquivo."""
    _esteira(cliente, "avaliar_rubrica")
    (linha,) = _linhas(_exportar(conexoes, "annotations", tmp_path))
    assert linha["corrected_by_reviewer"] is False
    assert "payload_as_submitted" not in linha


# ---------------------------------------------------------------------------
# 6. SFT E PREFERÊNCIA
# ---------------------------------------------------------------------------


def test_o_par_de_sft_traz_prompt_e_resposta(
    cliente: TestClient, conexoes: Any, tmp_path: Path
) -> None:
    _esteira(cliente, "sft_resposta")
    (linha,) = _linhas(_exportar(conexoes, "sft", tmp_path))
    assert linha["response"] == SFT_LONGA
    assert linha["prompt"]
    assert linha["lang"]


def test_o_perfil_sft_ignora_os_outros_tipos(
    cliente: TestClient, conexoes: Any, tmp_path: Path
) -> None:
    _esteira(cliente, "avaliar_rubrica")
    _esteira(cliente, "sft_resposta")
    res = _exportar(conexoes, "sft", tmp_path)
    assert res.row_count == 1
    assert res.manifest["task_types"] == ["sft_resposta"]


def test_o_par_de_preferencia_separa_escolhida_de_rejeitada(
    cliente: TestClient, conexoes: Any, tmp_path: Path
) -> None:
    _esteira(cliente, "comparar_ab")
    (linha,) = _linhas(_exportar(conexoes, "preference", tmp_path))
    assert linha["chosen"] and linha["rejected"]
    assert linha["chosen"] != linha["rejected"]
    assert linha["chosen_label"] == "modelo-a"
    assert linha["multi_turn"] is False
    assert linha["rationale"]


def test_empate_nao_vira_par_de_preferencia(
    cliente: TestClient, conexoes: Any, tmp_path: Path
) -> None:
    """Escolhido == rejeitado não treina nada e envenena quem não confere."""
    papeis = _papeis(cliente)
    quem, revisor = papeis["anotador"][0], papeis["revisor"][0]
    env = cliente.post(
        "/api/tarefas/proxima", json={"anotador_id": quem, "tipo": "comparar_ab"}
    ).json()["tarefa"]
    r = cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
        json={
            "anotador_id": quem,
            "payload": {
                "preferencia": "empate",
                "justificativa": "Both answers cover the same ground with the same gaps.",
            },
        },
    )
    assert r.status_code == 200, r.text
    _triar(cliente, revisor, r.json()["anotacao_id"])
    _avaliar(cliente, revisor, r.json()["anotacao_id"])

    res = _exportar(conexoes, "preference", tmp_path)
    assert res.row_count == 0
    assert res.manifest["empates_excluidos"] == 1, "o empate tem de ficar CONTADO"


# ---------------------------------------------------------------------------
# 7. AUDITORIA
# ---------------------------------------------------------------------------


def test_a_auditoria_mostra_a_cadeia_inteira_de_um_item(
    cliente: TestClient, conexoes: Any, tmp_path: Path
) -> None:
    a = _esteira(cliente, "avaliar_rubrica")
    res = _exportar(conexoes, "audit", tmp_path, anotacao_id=a["anotacao_id"])
    md = res.arquivo.read_text(encoding="utf-8")
    for secao in (
        "## 1. The prompt",
        "## 2. The annotation, as submitted",
        "## 3. Triage (pass 1)",
        "## 4. Rate and Review (pass 2)",
        "## 5. Outcome",
    ):
        assert secao in md, f"a auditoria perdeu a seção {secao!r}"
    assert JUSTIFICATIVA in md
    assert res.manifest["annotation_id"] == a["anotacao_id"]


def test_a_auditoria_revela_o_alvo_so_depois_de_a_avaliacao_existir(
    cliente: TestClient, semeado: Path, conexoes: Any, tmp_path: Path
) -> None:
    """Antes da avaliação, revelar o gabarito destruiria a calibração."""
    papeis = _papeis(cliente)
    revisor = papeis["revisor"][0]
    a = _anotar(cliente, papeis["anotador"][0], "avaliar_rubrica")
    _plantar(
        semeado,
        a["anotacao_id"],
        {"avaliacao_antes": "ajustavel", "familia_defeito": "generica", "nota": SENHA},
    )
    _triar(cliente, revisor, a["anotacao_id"])

    antes = _exportar(
        conexoes,
        "audit",
        tmp_path / "antes",
        anotacao_id=a["anotacao_id"],
        incluir_pendentes=True,
    )
    assert SENHA not in antes.arquivo.read_text(encoding="utf-8")

    _avaliar(cliente, revisor, a["anotacao_id"], antes="ajustavel")
    depois = _exportar(
        conexoes, "audit", tmp_path / "depois", anotacao_id=a["anotacao_id"]
    )
    texto = depois.arquivo.read_text(encoding="utf-8")
    assert SENHA in texto
    assert "the hidden target, now revealed" in texto


def test_a_tabela_da_auditoria_sobrevive_a_pipe_e_quebra_de_linha_no_motivo(
    cliente: TestClient, conexoes: Any, tmp_path: Path
) -> None:
    """O motivo de cada edição é campo LIVRE do revisor — e ele quebra a tabela.

    Um ``|`` ou um ``\\n`` no motivo não dá erro nenhum: a tabela renderiza
    torta e as colunas seguintes escorregam. Como a coluna que escorrega é
    justamente "stated reason", a auditoria passaria a atribuir a um campo o
    motivo de outro.
    """
    papeis = _papeis(cliente)
    quem, revisor = papeis["anotador"][0], papeis["revisor"][0]
    a = _anotar(cliente, quem, "avaliar_rubrica")
    _triar(cliente, revisor, a["anotacao_id"])
    env = cliente.get(
        f"/api/avaliacao/{a['anotacao_id']}", params={"revisor_id": revisor}
    ).json()
    corrigido = json.loads(json.dumps(env["tarefa"]["anotacao"]["payload"]))
    corrigido["notas"][0]["nota"] = 2
    motivo = "the scale is 1|2|3 here,\nnot 1..5 as the sheet assumed"
    _avaliar(
        cliente,
        revisor,
        a["anotacao_id"],
        antes="ajustavel",
        corpo_extra={
            "payload_corrigido": corrigido,
            "edicoes": [{"campo": "notas.0.nota", "motivo": motivo}],
        },
    )

    md = _exportar(
        conexoes, "audit", tmp_path, anotacao_id=a["anotacao_id"]
    ).arquivo.read_text(encoding="utf-8")
    linha = next(ln for ln in md.splitlines() if "notas.0.nota" in ln and ln.startswith("|"))
    # 4 colunas ⇒ exatamente 5 barras NÃO escapadas. As do motivo entram
    # escapadas e a quebra de linha vira espaço — sem isso a linha teria 7
    # barras, ou seriam duas linhas.
    assert linha.replace("\\|", "").count("|") == 5, linha
    assert "1\\|2\\|3" in linha
    assert "here, not 1..5" in linha, "a quebra de linha tem de virar espaço"


def test_auditoria_de_item_fora_do_recorte_falha_com_instrucao(
    cliente: TestClient, conexoes: Any, tmp_path: Path
) -> None:
    _esteira(cliente, "avaliar_rubrica")
    with pytest.raises(ValueError, match="não está entre as entregáveis"):
        _exportar(conexoes, "audit", tmp_path, anotacao_id=9_999)
    with pytest.raises(ValueError, match="precisa de uma anotação"):
        _exportar(conexoes, "audit", tmp_path)


# ---------------------------------------------------------------------------
# 8. MANIFESTO E CONTRATOS
# ---------------------------------------------------------------------------


def test_o_manifesto_confere_com_o_arquivo(
    cliente: TestClient, conexoes: Any, tmp_path: Path
) -> None:
    _esteira(cliente, "avaliar_rubrica")
    res = _exportar(conexoes, "annotations", tmp_path)
    assert res.manifest["sha256"] == sha256_arquivo(res.arquivo)
    assert res.manifest["bytes"] == res.arquivo.stat().st_size
    assert res.manifest["row_count"] == len(_linhas(res))
    assert res.manifest["language"] == IDIOMA_DOS_ARTEFATOS
    assert res.manifest["annotation_schema_version"] == adb.SCHEMA_VERSION_ANOTACAO
    assert res.manifest["composition"]["sinal"].endswith("IS NOT NULL")


def test_perfil_desconhecido_lista_os_que_existem() -> None:
    with pytest.raises(KeyError, match="dataset-card"):
        entrega.perfil("relatorio")


def test_todo_perfil_de_dado_declara_o_container_jsonl() -> None:
    """Perfil de dado é para carregar; perfil de prova é para ler."""
    for pf in entrega.PERFIS.values():
        assert pf.container == ("jsonl" if pf.dado else "md")
        assert pf.titulo and pf.descricao


def test_os_tipos_declarados_por_perfil_existem_na_taxonomia_de_tarefas() -> None:
    for pf in entrega.PERFIS.values():
        assert set(pf.tipos) <= set(adb.TIPOS_TAREFA), pf.chave


def test_o_glossario_cobre_as_chaves_dos_contratos() -> None:
    """Chave de payload sem entrada no glossário sai opaca para quem recebe.

    O arquivo entregue guarda as chaves como o contrato as gravou (renomeá-las
    faria o JSONL divergir do ``payload_schema`` que ele declara), então o
    glossário é a ÚNICA coisa que as explica.
    """
    from prompt_factory.annotate import payloads

    faltando: set[str] = set()
    for modelo in payloads.MODELOS.values():
        faltando |= set(modelo.model_fields) - set(entrega.GLOSSARIO)
    # `meta` e `instrumento` são metadados do envelope, não campos que o
    # anotador preenche — o dataset card os descreve em prosa.
    assert faltando <= {"instrumento"}, f"sem entrada no glossário: {sorted(faltando)}"


# ---------------------------------------------------------------------------
# 9. A LÍNGUA DOS ARTEFATOS
# ---------------------------------------------------------------------------


#: Palavras que só existem em português e que denunciariam cromo esquecido na
#: prosa dos artefatos. Nomes de campo do schema (`avaliacao_antes`) e valores do
#: vocabulário fechado (`inutilizavel`) NÃO contam: eles são identificadores, e
#: traduzi-los faria o artefato descrever um banco que não existe.
PALAVRAS_PT = (
    " anotação",
    " revisão",
    " qualidade",
    " arquivo",
    " linha(s)",
    " prompt do corpus",
    "pacote de demonstração",
    "escrito à mão",
    "não foi",
    "está",
)


@pytest.mark.parametrize("perfil", ["quality-report", "dataset-card"])
def test_os_artefatos_de_prosa_saem_em_ingles(
    cliente: TestClient, conexoes: Any, tmp_path: Path, perfil: str
) -> None:
    _esteira(cliente, "avaliar_rubrica")
    texto = _exportar(conexoes, perfil, tmp_path).arquivo.read_text(encoding="utf-8").lower()
    achadas = [p for p in PALAVRAS_PT if p in texto]
    assert not achadas, f"{perfil} tem prosa em português: {achadas}"


def test_o_rotulo_do_pacote_de_demonstracao_sai_em_ingles(
    cliente: TestClient, conexoes: Any, tmp_path: Path
) -> None:
    """O rótulo do pacote é PROSA NOSSA, e prosa nossa sai em inglês.

    ``tarefas._do_demo`` o escreve em português porque quem o lê é a interface —
    e é por isso que a tradução mora aqui, e não lá. Nome de fonte real
    (``wildchat_pt``) continua intocado: é identificador.
    """
    a = _esteira(cliente, "avaliar_rubrica")
    (linha,) = _linhas(_exportar(conexoes, "annotations", tmp_path / "j"))
    assert linha["prompt"]["source"] == entrega.ROTULO_DEMO["source"]
    assert linha["prompt"]["attribution"] == entrega.ROTULO_DEMO["attribution"]

    md = _exportar(
        conexoes, "audit", tmp_path / "a", anotacao_id=a["anotacao_id"]
    ).arquivo.read_text(encoding="utf-8")
    assert entrega.ROTULO_DEMO["source"] in md
    assert "pacote de demonstração" not in md

    # A tabela de fontes do dataset card cruza pelo MESMO nome: duas leituras
    # diferentes a deixariam vazia sem erro nenhum.
    card = _exportar(conexoes, "dataset-card", tmp_path / "c").arquivo.read_text(
        encoding="utf-8"
    )
    assert f"| {entrega.ROTULO_DEMO['source']} | 1 | fixture |" in card


def test_o_dataset_card_declara_a_composicao_humano_sintetico(
    cliente: TestClient, semeado: Path, conexoes: Any, tmp_path: Path
) -> None:
    """Declarada, nunca deduzida por quem lê — é a política do projeto."""
    a = _esteira(cliente, "avaliar_rubrica")
    _plantar(
        semeado,
        a["anotacao_id"],
        {"avaliacao_antes": "adequado", "familia_defeito": "nenhum", "nota": "x"},
    )
    md = _exportar(conexoes, "dataset-card", tmp_path).arquivo.read_text(encoding="utf-8")
    assert "Human vs synthetic, declared" in md
    assert "gabarito_avaliacao_json IS NOT NULL" in md
    assert "appear in the data exports" in md


def test_o_dataset_card_nao_promete_a_garantia_de_nsfw_que_o_dado_nao_sustenta(
    cliente: TestClient, conexoes: Any, tmp_path: Path
) -> None:
    _esteira(cliente, "avaliar_rubrica")
    md = _exportar(conexoes, "dataset-card", tmp_path).arquivo.read_text(encoding="utf-8")
    assert "excludes zero rows" in md


# ---------------------------------------------------------------------------
# 10. ESCRITA ATÔMICA
# ---------------------------------------------------------------------------


def test_a_escrita_e_atomica_e_nao_deixa_tmp(
    cliente: TestClient, conexoes: Any, tmp_path: Path
) -> None:
    _esteira(cliente, "avaliar_rubrica")
    for perfil in ("annotations", "quality-report"):
        res = _exportar(conexoes, perfil, tmp_path)
        assert res.arquivo.is_file()
        assert res.manifesto.is_file()
    assert not list(tmp_path.glob("*.tmp")), "sobrou .tmp de uma escrita atômica"


def test_prompt_sumido_do_corpus_nao_derruba_o_export(
    cliente: TestClient, semeado: Path, conexoes: Any, tmp_path: Path
) -> None:
    """O corpus é reconstruído e trocado por swap: uid some, e isso é normal."""
    a = _esteira(cliente, "avaliar_rubrica")
    conn = dbmod.connect(semeado)
    try:
        conn.execute(
            "UPDATE tarefas SET prompt_uid = 'sumiu-na-recarga' WHERE id = ("
            "  SELECT t.id FROM tarefas t JOIN atribuicoes at ON at.tarefa_id = t.id "
            "  JOIN anotacoes an ON an.atribuicao_id = at.id WHERE an.id = ?)",
            (a["anotacao_id"],),
        )
        conn.commit()
    finally:
        conn.close()

    res = _exportar(conexoes, "annotations", tmp_path)
    assert res.row_count == 1, "a linha sai, com o bloco de prompt vazio"
    assert res.manifest["prompt_sumido"] == 1
    assert "NOTE_MISSING_PROMPTS" in res.manifest


def test_a_conexao_do_corpus_continua_somente_leitura(conexoes: Any) -> None:
    _, corpo = conexoes
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        corpo.execute("UPDATE prompts SET lang = 'xx'")
