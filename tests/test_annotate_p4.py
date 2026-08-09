"""P4: o modo criar — o anotador escreve o prompt e ele entra no corpus.

É o marco em que as duas trilhas se encontram, e por isso os testes daqui
perseguem uma classe de defeito específica: **a divergência silenciosa entre a
plataforma e a pipeline**. Três valores têm de ser calculados pelas MESMAS
funções dos dois lados, e cada um deles falha sem erro nenhum se divergir:

* ``hash_norm`` — divergiu, o aviso de duplicata nunca dispara, e a plataforma
  passa a aceitar como inédito um texto que o dedup vai colapsar;
* ``uid_previsto`` — divergiu, o badge "no corpus" nunca acende, e ninguém sabe
  se a ingestão funcionou;
* o texto gravado — divergiu da ``norm_display``, e o uid calculado sobre ele
  deixa de ser o uid da linha que a pipeline produz.

Nenhum dos três levanta exceção quando quebra. Por isso eles são testados contra
as funções canônicas do corpus, e não contra um valor literal copiado.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from prompt_factory import db as dbmod
from prompt_factory import schema, textnorm
from prompt_factory.annotate import criacoes as crimod
from prompt_factory.annotate import db as adb
from prompt_factory.annotate import eventos as evmod
from prompt_factory.annotate import seed as seedmod
from prompt_factory.annotate.main import criar_app

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


def papeis(cliente: TestClient) -> dict[str, list[int]]:
    saida: dict[str, list[int]] = {}
    for p in cliente.get("/api/perfis").json()["items"]:
        saida.setdefault(p["papel"], []).append(p["id"])
    return saida


TEXTO = (
    "Preciso montar um roteiro de duas semanas pelo interior de Minas saindo de "
    "Belo Horizonte, sem carro, usando só ônibus intermunicipal."
)


def escrever(cliente: TestClient, quem: int, texto: str = TEXTO, **extra: Any):
    corpo = {"autor_id": quem, "texto": texto, "lang": "pt", **extra}
    return cliente.post("/api/criacoes", json=corpo)


def um_prompt_do_corpus(caminho: Path) -> dict[str, str]:
    conn = dbmod.connect(caminho, readonly=True)
    try:
        linha = conn.execute("SELECT uid, text, hash_norm FROM prompts LIMIT 1").fetchone()
        return {k: str(linha[k]) for k in ("uid", "text", "hash_norm")}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. a cadeia canônica — os três valores que falham em silêncio
# ---------------------------------------------------------------------------


def test_o_hash_e_o_MESMO_que_a_pipeline_calcula() -> None:
    """Contra as funções do corpus, nunca contra um literal copiado.

    ``sha256(norm_for_hash(norm_display(t)))`` é o que o s01 grava. Um atalho
    aqui produziria um hash que não casa com nada — e o sintoma seria um aviso
    de duplicata que nunca dispara, que ninguém percebe.
    """
    texto = "  Olá,   MUNDO!!!  \r\n\r\n\r\n  fim  "
    display, hash_norm = crimod.chave(texto)
    assert display == textnorm.norm_display(texto)
    assert hash_norm == hashlib.sha256(
        textnorm.norm_for_hash(display).encode("utf-8")
    ).hexdigest()


def test_o_uid_previsto_e_o_MESMO_que_o_make_uid_da_pipeline() -> None:
    """Se os dois divergirem, o badge "no corpus" nunca acende — e é assim que
    o erro aparece, semanas depois, sem nada apontando para a causa."""
    assert crimod.uid_previsto(42, TEXTO) == schema.make_uid(crimod.FONTE, "42", TEXTO)
    # Com `source_id` presente o uid NÃO depende do texto: é o que faz reingerir
    # a mesma criação reproduzir o mesmo uid.
    assert crimod.uid_previsto(42, TEXTO) == crimod.uid_previsto(42, "outro texto")
    assert crimod.uid_previsto(42, TEXTO) != crimod.uid_previsto(43, TEXTO)


def test_chave_vazia_nao_acusa_duplicata(corpus: Path) -> None:
    """``norm_for_hash("!!!") == ""``: todo prompt só de pontuação compartilha o
    sha256 da string vazia. Sem a guarda, escrever "???" acusaria duplicata
    contra uma linha do corpus que não tem nada a ver — o mesmo caso que o s04
    trata deixando essas linhas passarem inteiras."""
    conn = dbmod.connect(corpus, readonly=True)
    try:
        _, vazio = crimod.chave("!!!")
        assert vazio == hashlib.sha256(b"").hexdigest()
        assert crimod.duplicata_no_corpus(conn, vazio) is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. o aviso de duplicata — aviso, nunca bloqueio
# ---------------------------------------------------------------------------


def test_texto_identico_ao_corpus_AVISA_e_deixa_passar(
    cliente: TestClient, corpus: Path
) -> None:
    """Bloquear trataria como erro o momento em que o dedup prova que funciona.

    A mesma cadeia que junta 44 mil duplicatas do WildChat acabou de reconhecer
    que duas pessoas escreveram a mesma coisa. Isso é resultado, e a resposta é
    201 com o aviso dentro.
    """
    do_corpus = um_prompt_do_corpus(corpus)
    quem = papeis(cliente)["anotador"][0]
    r = escrever(cliente, quem, do_corpus["text"])
    assert r.status_code == 201, r.text
    corpo = r.json()
    assert corpo["duplicata_corpus"] is True
    assert corpo["uid_duplicata"] == do_corpus["uid"]
    assert corpo["status"] == "submetida", "avisou, e deixou passar"


def test_texto_inedito_nao_avisa(cliente: TestClient) -> None:
    quem = papeis(cliente)["anotador"][0]
    corpo = escrever(cliente, quem).json()
    assert corpo["duplicata_corpus"] is False
    assert corpo["uid_duplicata"] is None


def test_o_aviso_vai_para_a_TRILHA_e_carrega_o_uid_da_colisao(
    cliente: TestClient, semeado: Path, corpus: Path
) -> None:
    """Sem a prova no evento, "avisamos" é uma afirmação que ninguém confere."""
    do_corpus = um_prompt_do_corpus(corpus)
    quem = papeis(cliente)["anotador"][0]
    novo = escrever(cliente, quem, do_corpus["text"]).json()

    conn = dbmod.connect(semeado)
    try:
        eventos = evmod.da_entidade(conn, "criacao", int(novo["id"]))
    finally:
        conn.close()
    assert [e["acao"] for e in eventos] == ["criacao_submetida"]
    assert eventos[0]["detalhe"]["duplicata_corpus"] is True
    assert eventos[0]["detalhe"]["uid_duplicata"] == do_corpus["uid"]


def test_a_duplicata_e_RECHECADA_ao_vivo_na_listagem(
    cliente: TestClient, semeado: Path, corpus: Path
) -> None:
    """O corpus troca por swap embaixo desta app (o universo desta máquina foi
    de 144.754 para 159.733 numa recarga). O campo gravado é o registro do
    momento em que a pessoa escreveu; a listagem confere de novo, e quando os
    dois divergem é exatamente isso que se quer ver."""
    quem = papeis(cliente)["anotador"][0]
    novo = escrever(cliente, quem).json()
    assert novo["duplicata_corpus"] is False

    # O texto passa a existir no corpus DEPOIS da criação.
    _, hash_norm = crimod.chave(TEXTO)
    conn = dbmod.connect(corpus)
    try:
        modelo = conn.execute("SELECT * FROM prompts LIMIT 1").fetchone()
        colunas = [d[0] for d in conn.execute("SELECT * FROM prompts LIMIT 0").description]
        valores = {c: modelo[c] for c in colunas}
        valores.update({"id": None, "uid": "u" * 16, "text": TEXTO, "hash_norm": hash_norm})
        conn.execute(
            f"INSERT INTO prompts ({','.join(colunas)}) "
            f"VALUES ({','.join(':' + c for c in colunas)})",
            valores,
        )
        conn.commit()
    finally:
        conn.close()

    itens = cliente.get("/api/criacoes", params={"anotador_id": quem}).json()["items"]
    achada = next(i for i in itens if i["id"] == novo["id"])
    assert achada["duplicata_corpus"] is False, "o registro histórico não muda"
    assert achada["uid_duplicata"] == "u" * 16, "a checagem ao vivo enxerga o corpus de hoje"


# ---------------------------------------------------------------------------
# 3. o contrato de entrada
# ---------------------------------------------------------------------------


def test_texto_curto_demais_e_422_com_o_piso_na_frase(cliente: TestClient) -> None:
    quem = papeis(cliente)["anotador"][0]
    r = escrever(cliente, quem, "oi")
    assert r.status_code == 422
    assert "15" in r.text


def test_idioma_fora_do_par_e_422_e_nao_IntegrityError(cliente: TestClient) -> None:
    """Um `fr` chegaria ao CHECK do DDL e viraria IntegrityError sem nome de
    campo — o 422 diz qual campo e qual vocabulário."""
    quem = papeis(cliente)["anotador"][0]
    r = cliente.post(
        "/api/criacoes", json={"autor_id": quem, "texto": TEXTO, "lang": "fr"}
    )
    assert r.status_code == 422
    assert "pt" in r.text and "en" in r.text


def test_sugestao_fora_da_taxonomia_e_recusada(cliente: TestClient) -> None:
    """Um id inventado viajaria até o painel do admin parecendo uma classe do
    corpus. As sugestões falam a língua da taxonomia — ou não vêm."""
    quem = papeis(cliente)["anotador"][0]
    r = escrever(cliente, quem, task_type_sugerido="viagens-de-onibus")
    assert r.status_code == 422
    assert "taxonomia" in r.json()["detail"]


def test_sugestao_da_taxonomia_passa_e_e_gravada(cliente: TestClient) -> None:
    quem = papeis(cliente)["anotador"][0]
    r = escrever(cliente, quem, task_type_sugerido="planejamento", domain_sugerido="viagens")
    assert r.status_code == 201, r.text
    assert r.json()["task_type_sugerido"] == "planejamento"
    assert r.json()["domain_sugerido"] == "viagens"


def test_um_autor_id_booleano_e_recusado(cliente: TestClient) -> None:
    """Quinta aparição: `bool` é subclasse de `int`, e `true` viraria o autor 1."""
    r = cliente.post("/api/criacoes", json={"autor_id": True, "texto": TEXTO, "lang": "pt"})
    assert r.status_code == 422


def test_campo_desconhecido_e_422(cliente: TestClient) -> None:
    quem = papeis(cliente)["anotador"][0]
    r = cliente.post(
        "/api/criacoes",
        json={"autor_id": quem, "texto": TEXTO, "lang": "pt", "licenca": "mit"},
    )
    assert r.status_code == 422, "a licença é do projeto, não do formulário"


def test_o_texto_gravado_ja_esta_NORMALIZADO(cliente: TestClient) -> None:
    """O que entra no banco é ``norm_display``, e não o que veio do formulário.

    Se o texto gravado divergir da normalização, o uid calculado sobre ele
    deixa de ser o uid da linha que a pipeline vai produzir.
    """
    quem = papeis(cliente)["anotador"][0]
    sujo = "  " + TEXTO + "  \r\n\r\n\r\n\r\n  "
    corpo = escrever(cliente, quem, sujo).json()
    assert corpo["texto"] == textnorm.norm_display(sujo)
    assert corpo["texto"] == TEXTO


# ---------------------------------------------------------------------------
# 4. a revisão
# ---------------------------------------------------------------------------


def test_aprovar_grava_o_uid_previsto(cliente: TestClient) -> None:
    quem = papeis(cliente)["anotador"][0]
    revisor = papeis(cliente)["revisor"][0]
    nova = escrever(cliente, quem).json()
    assert nova["uid_previsto"] is None, "promessa nenhuma antes da decisão"

    r = cliente.post(
        f"/api/criacoes/{nova['id']}/revisar", json={"revisor_id": revisor, "aprovar": True}
    )
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["status"] == "aprovada"
    assert corpo["uid_previsto"] == schema.make_uid(crimod.FONTE, str(nova["id"]), TEXTO)
    assert corpo["licenca"] == "cc0-1.0"


def test_recusar_sem_comentario_e_422(cliente: TestClient) -> None:
    """Recusar sem dizer por quê devolve a decisão sem devolver a informação."""
    quem = papeis(cliente)["anotador"][0]
    revisor = papeis(cliente)["revisor"][0]
    nova = escrever(cliente, quem).json()
    r = cliente.post(
        f"/api/criacoes/{nova['id']}/revisar", json={"revisor_id": revisor, "aprovar": False}
    )
    assert r.status_code == 422


def test_recusar_com_comentario_encerra_sem_uid(cliente: TestClient) -> None:
    quem = papeis(cliente)["anotador"][0]
    revisor = papeis(cliente)["revisor"][0]
    nova = escrever(cliente, quem).json()
    r = cliente.post(
        f"/api/criacoes/{nova['id']}/revisar",
        json={
            "revisor_id": revisor,
            "aprovar": False,
            "comentario": "O pedido tem duas tarefas misturadas; separe em dois prompts.",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejeitada"
    assert r.json()["uid_previsto"] is None, "recusada não promete linha nenhuma"


def test_revisar_duas_vezes_e_409(cliente: TestClient) -> None:
    quem = papeis(cliente)["anotador"][0]
    revisor = papeis(cliente)["revisor"][0]
    nova = escrever(cliente, quem).json()
    corpo = {"revisor_id": revisor, "aprovar": True}
    assert cliente.post(f"/api/criacoes/{nova['id']}/revisar", json=corpo).status_code == 200
    repetida = cliente.post(f"/api/criacoes/{nova['id']}/revisar", json=corpo)
    assert repetida.status_code == 409
    assert "aprovada" in repetida.json()["detail"]


def test_o_anotador_nao_revisa(cliente: TestClient) -> None:
    quem = papeis(cliente)["anotador"][0]
    nova = escrever(cliente, quem).json()
    r = cliente.post(
        f"/api/criacoes/{nova['id']}/revisar", json={"revisor_id": quem, "aprovar": True}
    )
    assert r.status_code == 403


def test_o_revisor_nao_decide_sobre_o_que_ele_mesmo_escreveu(cliente: TestClient) -> None:
    """A mesma regra da triagem, e a mesma exceção declarada: o modo solo."""
    revisor = papeis(cliente)["revisor"][0]
    nova = escrever(cliente, revisor).json()
    r = cliente.post(
        f"/api/criacoes/{nova['id']}/revisar", json={"revisor_id": revisor, "aprovar": True}
    )
    assert r.status_code == 403
    assert "permitir_autorrevisao" in r.json()["detail"]


def test_a_fila_exclui_as_proprias_e_exige_o_papel(cliente: TestClient) -> None:
    quem = papeis(cliente)["anotador"][0]
    revisor = papeis(cliente)["revisor"][0]
    escrever(cliente, quem)
    escrever(cliente, revisor, TEXTO + " Prefiro trechos curtos entre cidades.")

    fila = cliente.get(
        "/api/criacoes", params={"anotador_id": revisor, "fila": True}
    ).json()
    assert [i["autor_id"] for i in fila["items"]] == [quem]

    # E ver a fila é a autorização: um anotador não a lê.
    negada = cliente.get("/api/criacoes", params={"anotador_id": quem, "fila": True})
    assert negada.status_code == 403


def test_as_minhas_sao_so_as_minhas_e_qualquer_um_ve_as_suas(cliente: TestClient) -> None:
    a, b = papeis(cliente)["anotador"][:2]
    escrever(cliente, a)
    escrever(cliente, b, TEXTO + " Com paradas de no máximo duas horas.")
    minhas = cliente.get("/api/criacoes", params={"anotador_id": a}).json()
    assert [i["autor_id"] for i in minhas["items"]] == [a]


def test_o_funil_traz_TODOS_os_status_inclusive_os_zerados(cliente: TestClient) -> None:
    """Um degrau que some da tela porque ninguém chegou nele parece um degrau
    que não existe."""
    quem = papeis(cliente)["anotador"][0]
    escrever(cliente, quem)
    funil = cliente.get("/api/criacoes", params={"anotador_id": quem}).json()["funil"]
    assert set(funil) == set(adb.STATUS_CRIACAO)
    assert funil["submetida"] == 1 and funil["exportada"] == 0


def test_criacao_inexistente_e_404(cliente: TestClient) -> None:
    revisor = papeis(cliente)["revisor"][0]
    r = cliente.post(
        "/api/criacoes/99999/revisar", json={"revisor_id": revisor, "aprovar": True}
    )
    assert r.status_code == 404
