"""Central de Briefs, F5: anti-cópia no servidor e a rubrica materializada.

As duas metades do marco, e por que cada uma existe:

* **Anti-cópia** — a §6 do plano decidiu que o prompt REFERENCIA o material e
  nunca o reproduz (opção c), e a regra do brief v2 ("in your own words") era,
  até aqui, só disciplina. Agora ela é exigível: a submissão compara prompt x
  recorte por sobreposição literal e recusa acima do limiar, e a listagem do
  revisor traz o número calculado pelo servidor — um revisor estimando
  sobreposição no olho não é verificação.
* **Materialização** — a rubrica da tríade vira linha em ``rubricas`` na
  aprovação, com ``origem='criacao'`` (a quarta procedência, o valor que custou
  o CHECK da v7) e ``prompt_uid = uid_previsto``. O prompt nasce sustentando
  ``avaliar_rubrica`` no dia em que chegar ao pool.

E a terceira família, a do funil: o P6 segue intacto — a criação de pedido
atravessa ``pf ingest plataforma`` como qualquer outra, e o RECORTE não viaja
(teste pela SENHA plantada, nunca pela chave — o padrão do alvo escondido).
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
from prompt_factory.annotate import criacoes as crimod
from prompt_factory.annotate import db as adb
from prompt_factory.annotate import entrega
from prompt_factory.annotate import seed as seedmod
from prompt_factory.annotate import tarefas as tmod
from prompt_factory.annotate.main import criar_app
from prompt_factory.ingest import plataforma

from .test_api import montar_banco
from .test_central_criacao import corpo_criacao, semear_pedidos

# ---------------------------------------------------------------------------
# fixtures — as mesmas do F4-1, e a duplicação é o preço de fixtures não serem
# importáveis entre módulos de teste sem conftest
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


def _papeis(cliente: TestClient) -> dict[str, list[int]]:
    saida: dict[str, list[int]] = {}
    for p in cliente.get("/api/perfis").json()["items"]:
        saida.setdefault(p["papel"], []).append(p["id"])
    return saida


#: Um recorte LONGO o bastante para uma colagem passar do limiar (60), com uma
#: SENHA plantada que nenhum texto legítimo conteria — é por ela que o teste do
#: P6 varre o parquet, nunca pela chave.
RECORTE_LONGO = (
    "A Filosofia nos da ferramentas para pensar e enfrentar esses problemas, "
    "mas e importante saber escolher as ferramentas certas para cada tarefa "
    "que a vida apresenta. XYZZY-SENHA-DO-RECORTE-F5"
)


def _reservar(cliente: TestClient, autor: int) -> int:
    r = cliente.post("/api/pedidos/proximo", json={"anotador_id": autor}).json()
    assert r["pedido"] is not None
    return int(r["pedido"]["id"])


# ---------------------------------------------------------------------------
# 1. a medida de sobreposição (unidade)
# ---------------------------------------------------------------------------


def test_a_comparacao_colapsa_caixa_e_espaco() -> None:
    """Trocar maiúscula ou quebrar a linha em outro lugar não pode "zerar" a
    sobreposição de um texto colado — é exatamente o conserto barato que quem
    cola faria primeiro."""
    trecho = crimod.maior_trecho_comum(
        "A  Filosofia\nNOS DA ferramentas", "a filosofia nos da ferramentas para pensar"
    )
    assert trecho == "a filosofia nos da ferramentas"


def test_o_maior_trecho_e_o_MAIOR_e_nao_o_primeiro() -> None:
    """O ``abc`` comum no começo (mais curto) não pode vencer o trecho longo do
    meio. Os espaços das bordas fazem parte do trecho — eles são literais
    também, e é a contagem por caractere que o limiar rege."""
    trecho = crimod.maior_trecho_comum(
        "abc um trecho comum bem mais longo xyz", "qq abc ww um trecho comum bem mais longo kk"
    )
    assert trecho == " um trecho comum bem mais longo "


def test_texto_vazio_da_sobreposicao_zero() -> None:
    assert crimod.maior_trecho_comum("", "qualquer coisa") == ""
    assert crimod.anticopia("", "")["chars"] == 0


def test_o_limiar_sai_do_settings() -> None:
    assert crimod.limiar_anticopia() == int(
        config.get("pedidos", "max_overlap_chars", default=60)
    )


# ---------------------------------------------------------------------------
# 2. a submissão recusa texto colado
# ---------------------------------------------------------------------------


def test_prompt_colado_do_recorte_e_recusado_com_a_frase_certa(
    cliente: TestClient, semeado: Path
) -> None:
    """A DoD do marco. 422 e não 409: a reserva está válida, o CONTEÚDO é que
    viola a regra — e a frase diz a regra, não só o número."""
    semear_pedidos(semeado, 1, recorte=RECORTE_LONGO)
    autor = _papeis(cliente)["anotador"][0]
    pid = _reservar(cliente, autor)
    corpo = corpo_criacao(autor, pid)
    corpo["texto"] = "Explique para a turma: " + RECORTE_LONGO[:150]
    r = cliente.post("/api/criacoes", json=corpo)
    assert r.status_code == 422
    detalhe = r.json()["detail"]
    assert "com as suas palavras" in detalhe
    assert str(crimod.limiar_anticopia()) in detalhe
    # A recusa não queimou o pedido: ele segue reservado para a mesma pessoa.
    meu = cliente.get("/api/pedidos", params={"anotador_id": autor}).json()["meu"]
    assert meu is not None and meu["id"] == pid


def test_prompt_com_palavras_proprias_passa(cliente: TestClient, semeado: Path) -> None:
    semear_pedidos(semeado, 1, recorte=RECORTE_LONGO)
    autor = _papeis(cliente)["anotador"][0]
    pid = _reservar(cliente, autor)
    r = cliente.post("/api/criacoes", json=corpo_criacao(autor, pid))
    assert r.status_code == 201


# ---------------------------------------------------------------------------
# 3. o revisor recebe o número, não a tarefa de estimá-lo
# ---------------------------------------------------------------------------


def test_a_listagem_traz_o_anticopia_calculado_pelo_servidor(
    cliente: TestClient, semeado: Path
) -> None:
    semear_pedidos(semeado, 1, recorte=RECORTE_LONGO)
    papeis = _papeis(cliente)
    autor = papeis["anotador"][0]
    pid = _reservar(cliente, autor)
    assert cliente.post("/api/criacoes", json=corpo_criacao(autor, pid)).status_code == 201

    revisor = papeis["revisor"][0]
    fila = cliente.get(
        "/api/criacoes", params={"anotador_id": revisor, "fila": True}
    ).json()["items"]
    meu = next(i for i in fila if i["pedido_id"] == pid)
    ac = meu["anticopia"]
    assert set(ac) == {"chars", "trecho", "limiar"}
    assert ac["limiar"] == crimod.limiar_anticopia()
    assert 0 < ac["chars"] < ac["limiar"]
    # E a criação LIVRE mantém o shape estável com `anticopia` nulo — o front
    # lê `item.anticopia` sem `in`-check, como o `chegou_ao_corpus`.
    livres = [i for i in fila if i["pedido_id"] is None]
    assert all(i["anticopia"] is None for i in livres)


# ---------------------------------------------------------------------------
# 4. a aprovação materializa a rubrica — e SÓ ela, e SÓ na aprovação
# ---------------------------------------------------------------------------


def _rubricas_de(banco: Path, origem: str = "criacao") -> list[sqlite3.Row]:
    conn = dbmod.connect(banco, readonly=True)
    try:
        return conn.execute(
            "SELECT * FROM rubricas WHERE origem = ? ORDER BY id", (origem,)
        ).fetchall()
    finally:
        conn.close()


def _criar_e_revisar(
    cliente: TestClient, semeado: Path, *, aprovar: bool, pedido: bool = True
) -> dict[str, Any]:
    papeis = _papeis(cliente)
    autor, revisor = papeis["anotador"][0], papeis["revisor"][0]
    if pedido:
        semear_pedidos(semeado, 1, recorte=RECORTE_LONGO)
        pid = _reservar(cliente, autor)
        criada = cliente.post("/api/criacoes", json=corpo_criacao(autor, pid)).json()
    else:
        criada = cliente.post("/api/criacoes", json=corpo_criacao(autor)).json()
    r = cliente.post(
        f"/api/criacoes/{criada['id']}/revisar",
        json={
            "revisor_id": revisor,
            "aprovar": aprovar,
            "comentario": None if aprovar else "needs to be in your own words",
        },
    )
    assert r.status_code == 200
    return r.json()


def test_aprovar_materializa_exatamente_1_rubrica_apontando_para_o_uid(
    cliente: TestClient, semeado: Path
) -> None:
    """A DoD do marco: 1 linha, `origem='criacao'`, `prompt_uid=uid_previsto`,
    `anotacao_id` NULL (não nasceu de anotação), critérios normalizados sob o
    rótulo `rubrica@3` — a MESMA forma que `rubrica_ativa` lê."""
    fim = _criar_e_revisar(cliente, semeado, aprovar=True)
    linhas = _rubricas_de(semeado)
    assert len(linhas) == 1
    rub = linhas[0]
    assert rub["prompt_uid"] == fim["uid_previsto"]
    assert rub["anotacao_id"] is None
    assert rub["status"] == "ativa"
    conteudo = json.loads(rub["criterios_json"])
    assert conteudo["schema"] == seedmod.SCHEMA_RUBRICA
    assert len(conteudo["criterios"]) == 3
    for c in conteudo["criterios"]:
        assert set(c["escala"]) >= {"min", "max", "ancoras"}


def test_a_rubrica_materializada_e_a_ativa_do_uid_previsto(
    cliente: TestClient, semeado: Path
) -> None:
    """O caminho de LEITURA fecha: `rubrica_ativa(uid_previsto)` devolve o
    instrumento — é o que fará o prompt sustentar `avaliar_rubrica` no dia em
    que a pipeline o levar ao pool."""
    fim = _criar_e_revisar(cliente, semeado, aprovar=True)
    conn = dbmod.connect(semeado, readonly=True)
    try:
        ativa = tmod.rubrica_ativa(conn, str(fim["uid_previsto"]))
    finally:
        conn.close()
    assert ativa is not None
    assert ativa["origem"] == "criacao"
    assert len(ativa["criterios"]) == 3


def test_recusar_nao_materializa_nada(cliente: TestClient, semeado: Path) -> None:
    _criar_e_revisar(cliente, semeado, aprovar=False)
    assert _rubricas_de(semeado) == []


def test_criacao_livre_aprovada_nao_materializa(cliente: TestClient, semeado: Path) -> None:
    """A livre não tem tríade — uma rubrica inventada aqui seria um instrumento
    sem autor medindo um prompt que ninguém pediu para medir."""
    fim = _criar_e_revisar(cliente, semeado, aprovar=True, pedido=False)
    assert fim["uid_previsto"]
    assert _rubricas_de(semeado) == []


# ---------------------------------------------------------------------------
# 5. o funil P6 segue intacto — e o recorte NÃO viaja
# ---------------------------------------------------------------------------


def test_ingest_plataforma_leva_a_criacao_de_pedido_SEM_o_recorte(
    cliente: TestClient, semeado: Path, tmp_path: Path
) -> None:
    """O smoke da DoD. A criação de pedido atravessa o P6 como qualquer outra
    (o ingester nem sabe que a v7 existe), e a SENHA plantada no recorte não
    aparece em byte nenhum do parquet — nem em `text`, nem em `meta_json`.
    Varre-se o ARQUIVO pela senha, não uma coluna pela chave: conferir a chave
    passaria hoje e falharia em silêncio no dia em que alguém a renomeasse.
    """
    fim = _criar_e_revisar(cliente, semeado, aprovar=True)
    conn = dbmod.connect(semeado)
    try:
        raw = tmp_path / "raw"
        resumo = plataforma.exportar(conn, raw_dir=raw)
    finally:
        conn.close()
    assert resumo["linhas"] >= 1
    parquet = Path(resumo["parquet"])
    bruto = parquet.read_bytes()
    assert b"XYZZY-SENHA-DO-RECORTE-F5" not in bruto

    import pyarrow.parquet as pq

    linhas = pq.read_table(parquet).to_pylist()
    minha = next(x for x in linhas if x["source_id"] == str(fim["id"]))
    assert crimod.uid_previsto(int(fim["id"]), minha["text_raw"]) == fim["uid_previsto"]
    meta = json.loads(minha["meta_json"])
    assert "material" not in meta and "recorte" not in meta


# ---------------------------------------------------------------------------
# 6. a ENTREGA (F6): o perfil `triads` e o dataset card
# ---------------------------------------------------------------------------


def _exportar(
    semeado: Path, corpus: Path, chave: str, destino: Path
) -> entrega.Resultado:
    conn = dbmod.connect(semeado)
    corpo = dbmod.connect(corpus, readonly=True)
    try:
        return entrega.executar(conn, corpo, chave, destino_dir=destino)
    finally:
        conn.close()
        corpo.close()


def test_o_perfil_triads_entrega_a_triade_SEM_o_recorte(
    cliente: TestClient, semeado: Path, corpus: Path, tmp_path: Path
) -> None:
    """A DoD do F6. A linha carrega prompt + rubrica + gold + a referência ao
    pedido (tema/meta/papel) e o NÚMERO do anti-cópia — e a SENHA plantada no
    recorte não aparece em byte nenhum do arquivo. Varre-se o ARQUIVO pela
    senha, nunca uma chave: conferir a chave passaria hoje e falharia em
    silêncio no dia em que alguém a renomeasse (o padrão do alvo escondido)."""
    fim = _criar_e_revisar(cliente, semeado, aprovar=True)
    res = _exportar(semeado, corpus, "triads", tmp_path / "out")
    assert res.row_count == 1

    bruto = res.arquivo.read_bytes()
    assert b"XYZZY-SENHA-DO-RECORTE-F5" not in bruto

    linha = json.loads(res.arquivo.read_text(encoding="utf-8").splitlines()[0])
    assert linha["uid"] == fim["uid_previsto"]
    assert linha["license"] == crimod.LICENCA
    assert linha["material_schema"] == "material_criacao@1"
    assert len(linha["rubrica"]["criterios"]) == 3
    assert len(linha["gold"]["deve_conter"]) == 2
    pedido_ref = linha["request"]
    assert pedido_ref["role"] == "professor"
    assert pedido_ref["theme"] and pedido_ref["teaching_goal"]
    assert "recorte" not in pedido_ref and "arquivo_fonte" not in pedido_ref
    # O anti-cópia sai como NÚMERO + régua; o trecho é texto do material e não
    # atravessa — nem sob outro nome, que é o que a varredura da senha prova.
    assert set(linha["anti_copy"]) == {"overlap_chars", "threshold"}
    assert linha["anti_copy"]["threshold"] == crimod.limiar_anticopia()


def test_criacao_livre_e_nao_revisada_ficam_fora_do_triads(
    cliente: TestClient, semeado: Path, corpus: Path, tmp_path: Path
) -> None:
    """A livre aprovada não tem tríade; a de pedido ainda `submetida` não passou
    pela (única) passagem de revisão. Nenhuma das duas é entregável."""
    _criar_e_revisar(cliente, semeado, aprovar=True, pedido=False)
    papeis = _papeis(cliente)
    autor = papeis["anotador"][0]
    semear_pedidos(semeado, 1, recorte=RECORTE_LONGO)
    pid = _reservar(cliente, autor)
    assert cliente.post("/api/criacoes", json=corpo_criacao(autor, pid)).status_code == 201
    res = _exportar(semeado, corpus, "triads", tmp_path / "out")
    assert res.row_count == 0


def test_o_manifesto_do_triads_declara_os_estados_da_CRIACAO(
    cliente: TestClient, semeado: Path, corpus: Path, tmp_path: Path
) -> None:
    """`statuses` descreve o que o arquivo contém: os estados da criação, nunca
    `avaliada` — dizer o terminal da anotação afirmaria uma passagem 2 que o
    modo criar não tem (decisão do P4)."""
    _criar_e_revisar(cliente, semeado, aprovar=True)
    res = _exportar(semeado, corpus, "triads", tmp_path / "out")
    assert res.manifest["statuses"] == list(entrega.STATUS_TRIADE)
    assert res.manifest["requests_referenced"] == 1
    assert "excerpt" in res.manifest["NOTE_MATERIAL"]


def test_o_dataset_card_declara_a_central_e_nao_vaza_o_recorte(
    cliente: TestClient, semeado: Path, corpus: Path, tmp_path: Path
) -> None:
    """A frase da §6 do plano, publicada: briefs pedagógicos derivados de
    material comercial, SEM reprodução do material. As âncoras são o que o
    texto tem de continuar DIZENDO, não a redação exata — a lição das âncoras
    do brief v2."""
    fim = _criar_e_revisar(cliente, semeado, aprovar=True)
    assert fim["uid_previsto"]
    res = _exportar(semeado, corpus, "dataset-card", tmp_path / "out")
    texto = res.arquivo.read_text(encoding="utf-8")
    assert "elicited by pedagogical briefs" in texto
    assert "without reproducing the material" in texto
    assert "triads.jsonl" in texto
    assert "XYZZY-SENHA-DO-RECORTE-F5" not in texto


def test_sem_triade_o_card_nao_inventa_a_secao(
    semeado: Path, corpus: Path, tmp_path: Path
) -> None:
    """Zero tríades = seção ausente. Num artefato DESCRITIVO, descrever um
    recorte vazio seria prometer um `triads.jsonl` que o `all` entrega vazio —
    diferente da regra da interface (desabilitar, nunca esconder), que vale
    para grupos de controle, não para prosa de entrega."""
    res = _exportar(semeado, corpus, "dataset-card", tmp_path / "out")
    assert "Central de Briefs" not in res.arquivo.read_text(encoding="utf-8")
