"""P4c: a campanha de geração — material, sintéticas e o alvo escondido.

Cada família aqui existe por uma coisa que, se der errado, **não parece
quebrada**:

* **material que entra torto.** Uma rubrica sem âncora nas pontas ABRE na tela e
  mede outra coisa; um par A/B com duas respostas boas produz preferência que é
  ruído com cara de sinal; uma resposta em inglês para um prompt em português
  passa por qualquer validação de formato. Nenhum desses erros levanta exceção
  em lugar nenhum — eles só aparecem no dado, meses depois;
* **o alvo escondido**, agora vindo de uma sintética de verdade e não de um
  UPDATE de teste. Se ele vazar, a calibração continua funcionando e mede a
  capacidade de ler JSON;
* **a marca de sintética.** É UMA: ``gabarito_avaliacao_json IS NOT NULL``. Uma
  segunda marca divergiria da primeira, e no dia em que divergisse ninguém
  saberia qual acreditar;
* **o manifest retomável.** Uma falha de validação que mudasse o estado do lote
  faria o maestro perder o material bom que veio junto com o erro;
* **a migração v3→v4.** A mais fácil de dispensar por engano: nenhuma coluna
  nasce, então "não precisa migrar" parece verdade. Só que ``CREATE TABLE IF NOT
  EXISTS`` não reescreve um CHECK, e o banco continuaria recusando o INSERT da
  campanha com uma regra que nenhum arquivo do repositório mostra mais.
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
from prompt_factory.annotate import geracao as gmod
from prompt_factory.annotate import migracao
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
def gp(tmp_path: Path) -> gmod.GeracaoPaths:
    """A campanha inteira dentro do ``tmp_path``. **Nenhum teste toca `geracao/`.**"""
    return gmod.GeracaoPaths(tmp_path / "geracao")


def abrir(caminho: Path) -> sqlite3.Connection:
    return dbmod.connect(caminho)


def so_leitura(caminho: Path) -> sqlite3.Connection:
    return dbmod.connect(caminho, readonly=True)


# ---------------------------------------------------------------------------
# material de teste, no padrão do demo_pack (rubrica ancorada + par desigual)
# ---------------------------------------------------------------------------

PT_BOA = (
    "Dá para resolver com o que você tem em casa. O ponto que decide tudo é a "
    "consistência da massa: ela precisa ficar bem rala, quase como leite, porque "
    "é a diferença de densidade que faz a parte mais pesada decantar e assar por "
    "baixo enquanto o resto sobe. Bata os líquidos primeiro, junte o seco e pare "
    "assim que estiver homogêneo. Não use o relógio como critério: está pronto "
    "quando a borda estiver firme e o centro ainda tremer ao balançar a forma."
)
PT_RUIM = (
    "Entendo perfeitamente a sua frustração com esse resultado, e quero dizer que "
    "essa é uma dificuldade bastante comum. O importante é compreender que cada "
    "preparo tem as suas particularidades e que a prática leva à perfeição ao "
    "longo do tempo. Recomendo que você observe atentamente o comportamento da "
    "mistura durante o processo e faça os ajustes necessários conforme a sua "
    "percepção. Com paciência e dedicação, tenho certeza de que você vai chegar "
    "ao resultado desejado nas suas próximas tentativas."
)
EN_BOA = (
    "The behaviour you are describing comes from the join key, not from the data "
    "itself. When the right-hand frame has more than one row per key, every match "
    "is expanded, which is why the row count grows even though nothing looks "
    "duplicated when you inspect the frames separately. Check it directly before "
    "changing anything, then decide whether you want to collapse the right frame "
    "first or keep the expansion on purpose."
)
EN_RUIM = (
    "Thank you for the detailed question, this is certainly a common source of "
    "confusion when working with tabular data. The situation you describe usually "
    "indicates that something in the underlying structure is not quite behaving "
    "as expected. I would suggest carefully inspecting your data to understand "
    "what is happening, and reviewing the shape of each frame before and after "
    "the operation. Once you have a clearer picture, the right path forward "
    "should become much more apparent to you."
)


def rubrica_gerada(titulo: str = "Actionable answer for the stated situation") -> dict[str, Any]:
    """No contrato de fixture (``rubrica@2``): escala com as PONTAS ancoradas."""
    return {
        "titulo": titulo,
        "criterios": [
            {
                "nome": f"Criterion {i}",
                "descricao": "Long enough description for the validator to accept it.",
                "escala": {
                    "min": 1,
                    "max": 5,
                    "ancoras": [
                        {"valor": 1, "rotulo": "does not address it at all"},
                        {"valor": 5, "rotulo": "addresses it and shows where it is decided"},
                    ],
                },
            }
            for i in range(3)
        ],
    }


def par(boa: str, ruim: str, *, a_e_boa: bool = False) -> list[dict[str, Any]]:
    """Duas respostas, nunca igualmente boas. ``a_e_boa`` alterna o lado."""
    defensavel = {
        "texto": boa,
        "meta": {
            "modelo": "teste",
            "defeito_classe": "nenhum",
            "defeito_plantado": "Nenhum. É a resposta defensável do par.",
            "correta": True,
        },
    }
    defeituosa = {
        "texto": ruim,
        "meta": {
            "modelo": "teste",
            "defeito_classe": "fluencia-cobrindo-vazio",
            "defeito_plantado": "Prosa fluente que reafirma o problema e não nomeia a causa.",
            "correta": False,
        },
    }
    esquerda, direita = (defensavel, defeituosa) if a_e_boa else (defeituosa, defensavel)
    return [
        {"rotulo_modelo": "modelo-a", **esquerda},
        {"rotulo_modelo": "modelo-b", **direita},
    ]


def resposta_material(lote: dict[str, Any], **override: Any) -> dict[str, Any]:
    """Uma resposta de agente válida para o lote inteiro, na língua de cada item."""
    itens = []
    for i, item in enumerate(lote["items"]):
        pt = item["lang"] == "pt"
        itens.append(
            {
                "uid": item["uid"],
                "rubrica": rubrica_gerada(f"Actionable answer for the stated situation {i}"),
                "respostas": par(
                    PT_BOA if pt else EN_BOA, PT_RUIM if pt else EN_RUIM, a_e_boa=bool(i % 2)
                ),
            }
        )
    return {"lote_id": lote["lote_id"], "itens": itens, **override}


def resposta_anotacoes(lote: dict[str, Any]) -> dict[str, Any]:
    """Uma resposta válida para um lote B: um payload por tipo, com o eco certo."""
    itens = []
    for item in lote["items"]:
        itens.append(
            {
                "tarefa_id": item["tarefa_id"],
                "avaliacao_antes": item["avaliacao_antes"],
                "familia_defeito": item["familia_defeito"],
                "nota": "Planted exactly what the assignment asked for, in the justification.",
                "payload": payload_do_tipo(item),
            }
        )
    return {"lote_id": lote["lote_id"], "itens": itens}


def payload_do_tipo(item: dict[str, Any]) -> dict[str, Any]:
    tipo = item["tipo"]
    if tipo == "avaliar_rubrica":
        return {
            "notas": [
                {"criterio": c["nome"], "nota": 3, "justificativa": "Boilerplate justification."}
                for c in item["rubrica"]["criterios"]
            ]
        }
    if tipo == "comparar_ab":
        return {
            "preferencia": "modelo-b",
            "justificativa": "It names the cause instead of restating the symptom back.",
        }
    if tipo == "sft_resposta":
        return {"resposta": EN_BOA}
    return {
        "titulo": "Rubric written by a synthetic annotator",
        "criterios": [
            {
                "nome": f"Criterion {i}",
                "descricao": "Long enough description for the validator to accept it.",
                "escala_min": 1,
                "escala_max": 5,
                "rotulo_min": "does not address it",
                "rotulo_max": "addresses it fully",
            }
            for i in range(3)
        ],
    }


def preparar(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths, campanha: str, n: int = 3, **kw: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(relatório, arquivo do lote)`` — o atalho de quase todo teste daqui."""
    conn, corpo = abrir(semeado), so_leitura(corpus)
    try:
        rel = gmod.preparar(conn, corpo, campanha=campanha, n=n, gp=gp, **kw)
    finally:
        conn.close()
        corpo.close()
    assert rel["lote_id"], rel.get("motivo")
    return rel, json.loads(gp.lote(rel["lote_id"]).read_text(encoding="utf-8"))


def importar(
    semeado: Path, gp: gmod.GeracaoPaths, lote_id: str, corpo: Any, **kw: Any
) -> dict[str, Any]:
    conn = abrir(semeado)
    try:
        return gmod.importar(
            conn, lote_id, json.dumps(corpo, ensure_ascii=False), gp=gp, **kw
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. O CAMINHO FELIZ DAS DUAS CAMPANHAS
# ---------------------------------------------------------------------------


def test_material_importado_levanta_o_teto_das_duas_abas(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    """O número que este marco existe para mover: 0 → N prompts com material."""
    conn = abrir(semeado)
    try:
        antes = gmod.cobertura_do_pool(conn)
    finally:
        conn.close()
    assert antes["avaliar_rubrica"] == 0
    assert antes["comparar_ab"] == 0

    rel, lote = preparar(semeado, corpus, gp, "material", n=4)
    saida = importar(semeado, gp, rel["lote_id"], resposta_material(lote))
    assert saida["ok"], saida.get("erros")
    assert saida["gravado"]["rubricas"] == len(lote["items"])
    assert saida["gravado"]["respostas_modelo"] == 2 * len(lote["items"])

    conn = abrir(semeado)
    try:
        depois = gmod.cobertura_do_pool(conn)
        origens = {
            str(linha["origem"])
            for linha in conn.execute("SELECT DISTINCT origem FROM rubricas")
        }
    finally:
        conn.close()
    assert depois["avaliar_rubrica"] == len(lote["items"])
    assert depois["comparar_ab"] == len(lote["items"])
    # `importada` separa o material da campanha das fixtures do pacote — e é o
    # valor que a v4 acrescentou ao CHECK.
    assert "importada" in origens


def test_o_lote_de_material_alterna_as_duas_linguas(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    _, lote = preparar(semeado, corpus, gp, "material", n=6, lang="ambos")
    linguas = [item["lang"] for item in lote["items"]]
    # Intercalado, não cortado ao meio: com blocos grandes por fonte, cortar
    # daria um lote monolíngue — o defeito que `seed.fatias_por_passo` já
    # consertou duas vezes neste repositório.
    assert set(linguas) == {"pt", "en"}


def test_dois_preparar_seguidos_nao_repetem_prompt(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    _, um = preparar(semeado, corpus, gp, "material", n=3)
    _, dois = preparar(semeado, corpus, gp, "material", n=3)
    a = {i["uid"] for i in um["items"]}
    b = {i["uid"] for i in dois["items"]}
    assert not (a & b), "o segundo lote reservou prompts que o primeiro já cobre"


def test_anotacoes_sinteticas_nascem_com_o_alvo_escondido(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    rel, lote = preparar(semeado, corpus, gp, "anotacoes", n=5)
    saida = importar(semeado, gp, rel["lote_id"], resposta_anotacoes(lote))
    assert saida["ok"], saida.get("erros")

    conn = abrir(semeado)
    try:
        comp = gmod.composicao(conn)
        linhas = conn.execute(
            f"SELECT {adb.COLUNA_GABARITO_AVALIACAO} AS g, status FROM anotacoes "
            f"WHERE {adb.COLUNA_GABARITO_AVALIACAO} IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()

    assert comp["sinteticas"] == len(lote["items"])
    assert comp["humanas"] == comp["anotacoes"] - comp["sinteticas"]
    # A MARCA É UMA SÓ, e é ela que o export do P5b honra.
    assert comp["sinal"] == f"anotacoes.{adb.COLUNA_GABARITO_AVALIACAO} IS NOT NULL"
    for linha in linhas:
        assert str(linha["status"]) == "pendente_triagem"
        assert gmod.validar_gabarito(json.loads(str(linha["g"]))) == []


def test_o_status_de_entrada_e_escolhivel(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    """O dono vai querer exercitar os DOIS caminhos: triar e avaliar direto."""
    rel, lote = preparar(semeado, corpus, gp, "anotacoes", n=3)
    saida = importar(
        semeado, gp, rel["lote_id"], resposta_anotacoes(lote), status="pendente_avaliacao"
    )
    assert saida["ok"], saida.get("erros")
    conn = abrir(semeado)
    try:
        estados = {
            str(linha["status"])
            for linha in conn.execute(
                f"SELECT status FROM anotacoes WHERE {adb.COLUNA_GABARITO_AVALIACAO} IS NOT NULL"
            )
        }
        # A atribuição fica como a triagem a deixaria: escrever `submetida` faria
        # a aba "minhas tarefas" mostrar um item eternamente em revisão numa
        # passagem que ele pulou.
        atribuicoes = {
            str(linha["status"])
            for linha in conn.execute(
                "SELECT a.status FROM atribuicoes a JOIN anotacoes an ON an.atribuicao_id = a.id "
                f"WHERE an.{adb.COLUNA_GABARITO_AVALIACAO} IS NOT NULL"
            )
        }
    finally:
        conn.close()
    assert estados == {"pendente_avaliacao"}
    assert atribuicoes == {"aprovada"}


def test_a_distribuicao_exercita_as_quatro_posicoes_da_escala() -> None:
    """Cota inteira por faixa, e não sorteio independente item a item.

    Com n=6 e 15% de `inutilizavel`, sortear cada item de forma independente
    daria uma chance real de a faixa não aparecer NENHUMA vez — e o lote
    deixaria de exercitar o botão que ele existe para exercitar.
    """
    alvos = gmod.sortear_alvos(20, semente=7)
    assert len(alvos) == 20
    assert set(alvos) == set(gmod.distribuicao())
    assert set(alvos) <= set(adb.AVALIACOES_ANTES)
    # Determinístico: a mesma semente dá o mesmo lote em duas máquinas.
    assert gmod.sortear_alvos(20, semente=7) == alvos
    assert all(len(gmod.sortear_alvos(n, semente=1)) == n for n in range(1, 13))


def test_alvo_bom_nunca_recebe_defeito_plantado() -> None:
    """`adequado`/`excepcional` com defeito seria uma anotação boa que se
    declara ruim — e o revisor que acertasse contaria como errado no painel."""
    for i in range(8):
        assert gmod.familia_para("adequado", i) == "nenhum"
        assert gmod.familia_para("excepcional", i) == "nenhum"
        assert gmod.familia_para("ajustavel", i) != "nenhum"
        assert gmod.familia_para("inutilizavel", i) in gmod.DEFEITOS_ANOTACAO


# ---------------------------------------------------------------------------
# 2. A VALIDAÇÃO ESTRITA RECUSA — e nomeia a linha
# ---------------------------------------------------------------------------


def test_uid_fora_do_lote_e_recusado(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    rel, lote = preparar(semeado, corpus, gp, "material", n=2)
    corpo = resposta_material(lote)
    corpo["itens"][0]["uid"] = "uid_que_nunca_existiu"
    saida = importar(semeado, gp, rel["lote_id"], corpo)
    assert not saida["ok"]
    assert any("não pertence ao lote" in e for e in saida["erros"])
    assert any("faltam 1 uid" in e for e in saida["erros"])


def test_uid_faltando_e_recusado(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    rel, lote = preparar(semeado, corpus, gp, "material", n=3)
    corpo = resposta_material(lote)
    faltante = corpo["itens"].pop(1)
    saida = importar(semeado, gp, rel["lote_id"], corpo)
    assert not saida["ok"]
    assert any(faltante["uid"] in e for e in saida["erros"])


def test_resposta_na_lingua_errada_e_recusada(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    """O caso inequívoco: uma resposta inteira em inglês para um prompt em pt."""
    rel, lote = preparar(semeado, corpus, gp, "material", n=6, lang="pt")
    assert lote["items"], "o lote de teste precisa de ao menos um prompt em pt"
    corpo = resposta_material(lote)
    corpo["itens"][0]["respostas"][0]["texto"] = EN_RUIM
    saida = importar(semeado, gp, rel["lote_id"], corpo)
    assert not saida["ok"]
    assert any("LÍNGUA DO PROMPT" in e for e in saida["erros"]), saida["erros"]
    assert any(corpo["itens"][0]["uid"] in e for e in saida["erros"])


def test_recusar_por_idioma_exige_certeza(monkeypatch: pytest.MonkeyPatch) -> None:
    """Duas camadas, como o s02: texto curto, terceira língua ou baixa confiança
    passam. O corpus deste projeto já provou que payload longo afoga a instrução
    e que um árbitro sozinho devolve CATALÃO para texto em russo."""
    assert gmod.conferir_lingua("curto demais", "pt") is None
    monkeypatch.setattr(gmod, "_lingua", lambda _t: ("fr", 0.99))
    assert gmod.conferir_lingua(PT_BOA, "pt") is None
    monkeypatch.setattr(gmod, "_lingua", lambda _t: ("en", 0.10))
    assert gmod.conferir_lingua(PT_BOA, "pt") is None
    monkeypatch.setattr(gmod, "_lingua", lambda _t: ("en", 0.98))
    assert "LÍNGUA DO PROMPT" in str(gmod.conferir_lingua(PT_BOA, "pt"))


def test_resposta_vazia_e_recusada(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    rel, lote = preparar(semeado, corpus, gp, "material", n=2)
    corpo = resposta_material(lote)
    corpo["itens"][0]["respostas"][1]["texto"] = "   "
    saida = importar(semeado, gp, rel["lote_id"], corpo)
    assert not saida["ok"]
    assert any("caracteres" in e and "mínimo" in e for e in saida["erros"])


@pytest.mark.parametrize(
    ("estrago", "esperado"),
    [
        (lambda r: r["criterios"].pop(), "critérios"),
        (lambda r: r["criterios"][0]["escala"].update({"min": 1, "max": 2}), "posições"),
        # Duas âncoras, nenhuma na ponta de baixo: a escala PARECE ancorada e
        # não é. É este o caso que a tela abriria sem ninguém notar.
        (lambda r: r["criterios"][0]["escala"]["ancoras"][0].update({"valor": 3}), "PONTAS"),
        (lambda r: r["criterios"][0]["escala"]["ancoras"].pop(), "âncoras"),
        (lambda r: r["criterios"][0].update({"descricao": "curta"}), "descricao"),
        (lambda r: r["criterios"][1].update({"nome": r["criterios"][0]["nome"]}), "mesmo nome"),
        (lambda r: r["criterios"][0]["escala"].update({"max": True}), "boolean"),
    ],
)
def test_rubrica_torta_e_recusada(estrago: Any, esperado: str) -> None:
    """Uma rubrica sem âncora ABRE na tela e mede outra coisa — não levanta
    exceção em lugar nenhum. Por isso a recusa é aqui."""
    rub = rubrica_gerada()
    estrago(rub)
    limpa, erros = gmod._validar_rubrica("uidteste", rub)
    assert limpa is None
    assert any(esperado in e for e in erros), erros


def test_o_par_precisa_de_exatamente_uma_resposta_defensavel(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    """Um A/B sem resposta defensável não mede preferência, mede ruído. Duas
    boas é o mesmo problema pelo outro lado."""
    rel, lote = preparar(semeado, corpus, gp, "material", n=2)
    corpo = resposta_material(lote)
    for resposta in corpo["itens"][0]["respostas"]:
        resposta["meta"]["correta"] = True
        resposta["meta"]["defeito_classe"] = "nenhum"
    saida = importar(semeado, gp, rel["lote_id"], corpo)
    assert not saida["ok"]
    assert any("exatamente UMA" in e for e in saida["erros"]), saida["erros"]


def test_correta_true_com_defeito_declarado_e_recusado(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    rel, lote = preparar(semeado, corpus, gp, "material", n=2)
    corpo = resposta_material(lote)
    resposta = corpo["itens"][0]["respostas"][0]
    resposta["meta"]["correta"] = True
    resposta["meta"]["defeito_classe"] = "restricao-ignorada"
    saida = importar(semeado, gp, rel["lote_id"], corpo)
    assert not saida["ok"]
    assert any("não tem defeito plantado" in e for e in saida["erros"])


def test_correta_booleana_de_verdade(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    """`bool` é subclasse de `int`, quinta vez neste repositório."""
    rel, lote = preparar(semeado, corpus, gp, "material", n=2)
    corpo = resposta_material(lote)
    corpo["itens"][0]["respostas"][0]["meta"]["correta"] = 1
    saida = importar(semeado, gp, rel["lote_id"], corpo)
    assert not saida["ok"]
    assert any("booleano JSON" in e for e in saida["erros"])


def test_defeito_fora_do_catalogo_e_AVISO_e_nao_erro(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    """O catálogo é aberto de propósito: o defeito novo é o que a campanha
    descobre, e barrá-lo obrigaria a editar código para escrevê-lo."""
    rel, lote = preparar(semeado, corpus, gp, "material", n=2)
    corpo = resposta_material(lote)
    ruim = next(
        r for r in corpo["itens"][0]["respostas"] if not r["meta"]["correta"]
    )
    ruim["meta"]["defeito_classe"] = "familia-inedita-desta-rodada"
    saida = importar(semeado, gp, rel["lote_id"], corpo)
    assert saida["ok"], saida.get("erros")
    assert any("família nova" in a for a in saida["avisos"])


def test_payload_fora_do_contrato_e_recusado(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    """O contrato não é reimplementado aqui: é o MESMO modelo Pydantic da rota
    de submissão, escolhido pelo tipo lido do PLANO (que veio do banco)."""
    rel, lote = preparar(semeado, corpus, gp, "anotacoes", n=4)
    corpo = resposta_anotacoes(lote)
    alvo = next(i for i in corpo["itens"] if "notas" in i["payload"])
    alvo["payload"]["notas"][0]["nota"] = True  # nota 1 em silêncio, se passasse
    saida = importar(semeado, gp, rel["lote_id"], corpo)
    assert not saida["ok"]
    assert any(f"tarefa {alvo['tarefa_id']}" in e for e in saida["erros"])
    assert any("booleano" in e for e in saida["erros"]), saida["erros"]


def test_campo_desconhecido_no_payload_e_recusado(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    rel, lote = preparar(semeado, corpus, gp, "anotacoes", n=3)
    corpo = resposta_anotacoes(lote)
    corpo["itens"][0]["payload"]["campo_que_nao_existe"] = 1
    saida = importar(semeado, gp, rel["lote_id"], corpo)
    assert not saida["ok"]
    assert any("campo_que_nao_existe" in e for e in saida["erros"])


def test_o_eco_do_alvo_e_conferido_contra_o_plano(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    """Sem o eco, um agente que processa fora de ordem produz trabalho VÁLIDO
    casado com o alvo errado — e o painel do P5a mediria o revisor contra uma
    expectativa que ninguém teve."""
    rel, lote = preparar(semeado, corpus, gp, "anotacoes", n=4)
    corpo = resposta_anotacoes(lote)
    corpo["itens"][0]["familia_defeito"] = "justificativa-generica-que-nao-foi-pedida"
    corpo["itens"][1]["avaliacao_antes"] = "excepcional"
    saida = importar(semeado, gp, rel["lote_id"], corpo)
    assert not saida["ok"]
    assert sum("alvo errado" in e for e in saida["erros"]) >= 1
    assert any("familia_defeito" in e for e in saida["erros"])
    assert any("avaliacao_antes" in e for e in saida["erros"])


def test_nota_do_gabarito_curta_demais_e_recusada(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    rel, lote = preparar(semeado, corpus, gp, "anotacoes", n=3)
    corpo = resposta_anotacoes(lote)
    corpo["itens"][0]["nota"] = "ok"
    saida = importar(semeado, gp, rel["lote_id"], corpo)
    assert not saida["ok"]
    assert any("'nota' precisa DIZER" in e for e in saida["erros"])


@pytest.mark.parametrize(
    "torto",
    [
        {"avaliacao_antes": "ajustavel", "familia_defeito": "x"},          # sem `nota`
        {"avaliacao_antes": "otimo", "familia_defeito": "x", "nota": "y"},  # fora da escala
        {"avaliacao_antes": "ajustavel", "familia_defeito": "", "nota": "y"},
        {"avaliacao_antes": "ajustavel", "familia_defeito": "x", "nota": "y", "extra": 1},
        "isto nem é um objeto",
    ],
)
def test_gabarito_malformado_e_recusado(torto: Any) -> None:
    """O P5a vai LER esta coluna. Um gabarito torto não quebra nada hoje: ele
    quebra o painel meses depois, quando ninguém lembra de onde ele veio."""
    assert gmod.validar_gabarito(torto)


def test_o_gabarito_gravado_tem_exatamente_as_chaves_do_contrato(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    rel, lote = preparar(semeado, corpus, gp, "anotacoes", n=3)
    assert importar(semeado, gp, rel["lote_id"], resposta_anotacoes(lote))["ok"]
    conn = abrir(semeado)
    try:
        linha = conn.execute(
            f"SELECT {adb.COLUNA_GABARITO_AVALIACAO} AS g FROM anotacoes "
            f"WHERE {adb.COLUNA_GABARITO_AVALIACAO} IS NOT NULL LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert set(json.loads(str(linha["g"]))) == set(adb.CHAVES_GABARITO_AVALIACAO)


def test_lote_trocado_e_recusado(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    rel, lote = preparar(semeado, corpus, gp, "material", n=2)
    corpo = resposta_material(lote)
    corpo["lote_id"] = "mat_9999"
    saida = importar(semeado, gp, rel["lote_id"], corpo)
    assert not saida["ok"]
    assert any("arquivo trocado" in e for e in saida["erros"])


def test_o_parser_tolera_a_FORMA_e_e_estrito_no_CONTEUDO(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    """Um agente que embrulha a resposta em cerca de código não é motivo para
    jogar fora o material de um lote inteiro."""
    rel, lote = preparar(semeado, corpus, gp, "material", n=2)
    bruto = (
        "Claro! Aqui está o JSON pedido:\n\n```json\n"
        + json.dumps(resposta_material(lote), ensure_ascii=False)
        + "\n```\n"
    )
    conn = abrir(semeado)
    try:
        saida = gmod.importar(conn, rel["lote_id"], bruto, gp=gp)
    finally:
        conn.close()
    assert saida["ok"], saida.get("erros")
    assert saida["avisos"]


def test_nada_e_gravado_quando_a_validacao_recusa(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    """Meio-lote gravado é o estado que obrigaria alguém a descobrir à mão o que
    entrou e o que não entrou."""
    rel, lote = preparar(semeado, corpus, gp, "material", n=3)
    conn = abrir(semeado)
    try:
        antes = conn.execute("SELECT count(*) AS n FROM rubricas").fetchone()["n"]
    finally:
        conn.close()
    corpo = resposta_material(lote)
    corpo["itens"][2]["rubrica"]["criterios"] = []
    assert not importar(semeado, gp, rel["lote_id"], corpo)["ok"]
    conn = abrir(semeado)
    try:
        depois = conn.execute("SELECT count(*) AS n FROM rubricas").fetchone()["n"]
    finally:
        conn.close()
    assert depois == antes


# ---------------------------------------------------------------------------
# 3. O MANIFEST É RETOMÁVEL — claim, falha, retry
# ---------------------------------------------------------------------------


def test_falha_de_validacao_NAO_muda_o_estado_do_lote(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    """Mesma regra do `pf labels submit`: o lote segue reivindicado e o maestro
    corrige o arquivo, sem perder o material bom que veio junto."""
    rel, lote = preparar(semeado, corpus, gp, "material", n=2)
    corpo = resposta_material(lote)
    corpo["itens"][0]["respostas"][0]["texto"] = "curto"
    saida = importar(semeado, gp, rel["lote_id"], corpo)
    assert not saida["ok"]
    assert saida["status"] == gmod.CLAIMED
    assert saida["tentativas"] == 1

    # RETRY DIRIGIDO: o mesmo lote, o arquivo corrigido.
    ok = importar(semeado, gp, rel["lote_id"], resposta_material(lote))
    assert ok["ok"], ok.get("erros")
    manifest = gmod.carregar_manifest(gp)
    assert manifest["lotes"][rel["lote_id"]]["status"] == gmod.DONE
    assert manifest["lotes"][rel["lote_id"]]["tentativas"] == 1


def test_tentativas_esgotadas_mandam_o_lote_para_failed_e_o_revival_e_explicito(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gmod, "max_tentativas", lambda: 2)
    rel, lote = preparar(semeado, corpus, gp, "material", n=2)
    corpo = resposta_material(lote)
    corpo["itens"][0]["rubrica"]["criterios"] = []
    assert importar(semeado, gp, rel["lote_id"], corpo)["status"] == gmod.CLAIMED
    saida = importar(semeado, gp, rel["lote_id"], corpo)
    assert saida["status"] == gmod.FAILED

    # Reviver é um ato: `failed` -> `pending` -> `claimed`, e o ARQUIVO do lote
    # é reusado (regerá-lo escolheria outros prompts e a resposta pronta
    # deixaria de casar).
    voltou = gmod.reemitir(rel["lote_id"], gp)
    assert voltou["lote_id"] == rel["lote_id"]
    assert gmod.carregar_manifest(gp)["lotes"][rel["lote_id"]]["status"] == gmod.CLAIMED
    assert importar(semeado, gp, rel["lote_id"], resposta_material(lote))["ok"]


def test_claim_orfao_volta_para_pending_pelo_TTL(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    """Sem a varredura, uma sessão morta prenderia o lote e o `preparar`
    seguinte escolheria prompts NOVOS, deixando um buraco permanente."""
    rel, _ = preparar(semeado, corpus, gp, "material", n=2)
    manifest = gmod.carregar_manifest(gp)
    manifest["lotes"][rel["lote_id"]]["claimed_at"] = "2020-01-01T00:00:00Z"
    gmod.salvar_manifest(manifest, gp)

    manifest = gmod.carregar_manifest(gp)
    assert gmod.varrer_orfaos(manifest) == [rel["lote_id"]]
    assert manifest["lotes"][rel["lote_id"]]["status"] == gmod.PENDING


def test_importar_duas_vezes_o_mesmo_lote_e_recusado(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    rel, lote = preparar(semeado, corpus, gp, "material", n=2)
    assert importar(semeado, gp, rel["lote_id"], resposta_material(lote))["ok"]
    with pytest.raises(ValueError, match="já importado"):
        importar(semeado, gp, rel["lote_id"], resposta_material(lote))


def test_gravar_material_e_idempotente(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    """Duas rubricas ativas no mesmo prompt fariam `rubrica_ativa` devolver a
    mais nova, e uma avaliação anterior passaria a citar critérios que a tela
    não mostra mais."""
    rel, lote = preparar(semeado, corpus, gp, "material", n=2)
    itens, erros, _ = gmod.validar_material(lote, resposta_material(lote))
    assert not erros
    conn = abrir(semeado)
    try:
        primeira = gmod.gravar_material(conn, itens, rel["lote_id"])
        segunda = gmod.gravar_material(conn, itens, rel["lote_id"])
    finally:
        conn.close()
    assert primeira["rubricas"] == len(itens)
    assert segunda["rubricas"] == 0
    assert segunda["respostas_modelo"] == 0
    assert segunda["ja_existiam"] == 3 * len(itens)


def test_o_id_do_lote_vem_do_manifest_e_nunca_do_disco(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    """Mesma disciplina do part-file do WildChat: um arquivo órfão no disco
    faria `listdir` pular um número e o lote seguinte sobrescrever o anterior."""
    rel, _ = preparar(semeado, corpus, gp, "material", n=2)
    assert rel["lote_id"] == "mat_0001"
    gp.lote("mat_0007").write_text("{}", encoding="utf-8")
    manifest = gmod.carregar_manifest(gp)
    assert gmod.proximo_id(manifest, "material") == "mat_0002"
    assert gmod.proximo_id(manifest, "anotacoes") == "anot_0001"


def test_transicao_fora_da_maquina_e_recusada() -> None:
    registro = {"status": gmod.DONE}
    with pytest.raises(ValueError, match="não é permitida"):
        gmod._transicionar(registro, gmod.CLAIMED, "mat_0001")


def test_manifest_ausente_nao_e_erro(gp: gmod.GeracaoPaths) -> None:
    """A campanha começa pelo `preparar`; um `status` num clone limpo responde
    'nenhum lote', que é a verdade."""
    assert gmod.carregar_manifest(gp)["lotes"] == {}
    assert gmod.painel(gp)["n_lotes"] == 0


def test_contrato_divergente_para_a_campanha(gp: gmod.GeracaoPaths) -> None:
    gp.preparar()
    gmod.salvar_manifest({"contrato": "geracao@99", "lotes": {}}, gp)
    with pytest.raises(SystemExit, match="contrato"):
        gmod.carregar_manifest(gp)


# ---------------------------------------------------------------------------
# 4. A SINTÉTICA NÃO APARECE ONDE TRABALHO HUMANO APARECE
# ---------------------------------------------------------------------------


def test_o_alvo_de_uma_sintetica_real_nao_vaza_por_nenhuma_rota_do_revisor(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    """Varre a resposta INTEIRA por uma senha plantada, e não pela chave:
    conferir a chave passaria hoje e falharia em silêncio no dia em que alguém a
    renomeasse ao serializar."""
    rel, lote = preparar(semeado, corpus, gp, "anotacoes", n=3)
    corpo = resposta_anotacoes(lote)
    senha = "ALVOESCONDIDOQUENAOPODEVAZAR"
    for item in corpo["itens"]:
        item["nota"] = f"{senha} — o que foi plantado neste item, por extenso."
    assert importar(semeado, gp, rel["lote_id"], corpo)["ok"]

    with TestClient(criar_app(semeado, corpus)) as c:
        revisor = next(
            p for p in c.get("/api/perfis").json()["items"] if p["papel"] == "revisor"
        )["id"]
        fila = c.get("/api/revisao/fila", params={"revisor_id": revisor})
        assert fila.status_code == 200, fila.text
        alvos = [item["anotacao_id"] for item in fila.json()["items"]]
        assert alvos, "as sintéticas deveriam estar na fila da triagem"
        textos = [fila.text]
        for anotacao_id in alvos:
            detalhe = c.get(f"/api/revisao/{anotacao_id}", params={"revisor_id": revisor})
            assert detalhe.status_code == 200, detalhe.text
            textos.append(detalhe.text)
        for texto in textos:
            assert senha not in texto
            assert adb.COLUNA_GABARITO_AVALIACAO not in texto


def test_o_evento_da_sintetica_nao_carrega_o_alvo(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    """`eventos` é a trilha de auditoria: um alvo que vaza por aqui vaza por uma
    porta que ninguém está olhando."""
    rel, lote = preparar(semeado, corpus, gp, "anotacoes", n=3)
    corpo = resposta_anotacoes(lote)
    senha = "SEGREDODOALVO"
    for item in corpo["itens"]:
        item["nota"] = f"{senha} — o que foi plantado neste item, por extenso."
    assert importar(semeado, gp, rel["lote_id"], corpo)["ok"]

    conn = abrir(semeado)
    try:
        detalhes = "".join(
            str(linha["detalhe_json"])
            for linha in conn.execute(
                "SELECT detalhe_json FROM eventos WHERE acao = 'anotacao_sintetica_importada'"
            )
        )
        n = conn.execute(
            "SELECT count(*) AS n FROM eventos WHERE acao = 'anotacao_sintetica_importada'"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert int(n) == len(lote["items"])
    assert senha not in detalhes
    assert "avaliacao_antes" not in detalhes


def test_a_composicao_separa_humano_de_sintetico(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    """A composição é DECLARADA, não deduzida por quem lê: o projeto não entrega
    anotação de IA como humana."""
    conn = abrir(semeado)
    try:
        antes = gmod.composicao(conn)
    finally:
        conn.close()
    assert antes["sinteticas"] == 0
    assert antes["humanas"] == antes["anotacoes"]

    rel, lote = preparar(semeado, corpus, gp, "anotacoes", n=4)
    assert importar(semeado, gp, rel["lote_id"], resposta_anotacoes(lote))["ok"]
    conn = abrir(semeado)
    try:
        depois = gmod.composicao(conn)
    finally:
        conn.close()
    assert depois["sinteticas"] == len(lote["items"])
    assert depois["humanas"] == antes["humanas"]
    assert sum(depois["sinteticas_por_alvo"].values()) == len(lote["items"])
    assert set(depois["sinteticas_por_alvo"]) <= set(adb.AVALIACOES_ANTES)


def test_a_sintetica_e_atribuida_a_uma_persona_de_fixture(
    semeado: Path, corpus: Path, gp: gmod.GeracaoPaths
) -> None:
    """Uma anotação de agente carimbada com o nome de quem monta o portfólio é
    exatamente a mentira que este marco existe para não cometer."""
    rel, lote = preparar(semeado, corpus, gp, "anotacoes", n=3)
    assert importar(semeado, gp, rel["lote_id"], resposta_anotacoes(lote))["ok"]
    conn = abrir(semeado)
    try:
        papeis = {
            str(linha["papel"])
            for linha in conn.execute(
                "SELECT DISTINCT au.papel FROM anotacoes an "
                "JOIN atribuicoes a ON a.id = an.atribuicao_id "
                "JOIN anotadores au ON au.id = a.anotador_id "
                f"WHERE an.{adb.COLUNA_GABARITO_AVALIACAO} IS NOT NULL"
            )
        }
    finally:
        conn.close()
    assert papeis == {"anotador"}


# ---------------------------------------------------------------------------
# 5. SCHEMA v4 E A MIGRAÇÃO
# ---------------------------------------------------------------------------


def test_a_v4_aceita_rubrica_importada(tmp_path: Path) -> None:
    conn = dbmod.connect(tmp_path / "novo.sqlite")
    try:
        adb.init_db(conn)
        conn.execute(
            "INSERT INTO rubricas (prompt_uid, titulo, criterios_json, origem) "
            "VALUES ('x', 't', '{}', 'importada')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO rubricas (prompt_uid, titulo, criterios_json, origem) "
                "VALUES ('y', 't', '{}', 'inventada')"
            )
    finally:
        conn.close()


def test_todas_as_origens_conhecidas_tem_passo() -> None:
    assert set(migracao.PASSOS) == set(migracao.ORIGENS_CONHECIDAS)
    assert adb.SCHEMA_VERSION_ANOTACAO - 1 in migracao.PASSOS


def test_a_copia_da_v3_cobre_todas_as_tabelas(tmp_path: Path) -> None:
    """`app_meta` é escrita à parte; o resto tem de estar na lista, senão a
    migração perde uma tabela inteira em silêncio."""
    cobertas = {t for t, _ in migracao.COPIA_V3_ANTES + migracao.COPIA_V3_DEPOIS}
    assert cobertas == set(adb.TABELAS) - {"app_meta"}


def test_migrar_da_v3_preserva_o_alvo_escondido(
    tmp_path: Path, corpus: Path, semeado: Path, gp: gmod.GeracaoPaths
) -> None:
    """O pior desfecho possível desta migração: um banco em que TUDO parece
    humano porque a coluna do alvo ficou para trás."""
    rel, lote = preparar(semeado, corpus, gp, "anotacoes", n=3)
    assert importar(semeado, gp, rel["lote_id"], resposta_anotacoes(lote))["ok"]

    conn = abrir(semeado)
    try:
        antes = gmod.composicao(conn)
        # Volta o carimbo para a v3: a migração vai ler daqui.
        adb.set_meta(conn, adb.CHAVE_VERSAO, "3")
    finally:
        conn.close()

    relatorio = migracao.migrar(semeado)
    assert relatorio["de"] == 3
    assert relatorio["para"] == adb.SCHEMA_VERSION_ANOTACAO
    assert Path(relatorio["backup"]).is_file()

    conn = abrir(semeado)
    try:
        depois = gmod.composicao(conn)
    finally:
        conn.close()
    assert depois["sinteticas"] == antes["sinteticas"] > 0
    assert depois["humanas"] == antes["humanas"]
    assert depois["sinteticas_por_alvo"] == antes["sinteticas_por_alvo"]


def test_o_schema_migrado_da_v3_e_identico_ao_de_um_banco_novo(
    tmp_path: Path, semeado: Path
) -> None:
    conn = abrir(semeado)
    try:
        adb.set_meta(conn, adb.CHAVE_VERSAO, "3")
    finally:
        conn.close()
    migracao.migrar(semeado)

    novo = tmp_path / "novo.sqlite"
    conn = dbmod.connect(novo)
    try:
        adb.init_db(conn)
    finally:
        conn.close()

    def retrato(caminho: Path) -> list[tuple[str, str, str]]:
        conn = dbmod.connect(caminho)
        try:
            return sorted(
                (str(r["type"]), str(r["name"]), " ".join(str(r["sql"] or "").split()))
                for r in conn.execute(
                    "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                )
            )
        finally:
            conn.close()

    assert retrato(semeado) == retrato(novo)


# ---------------------------------------------------------------------------
# 6. CONFIGURAÇÃO
# ---------------------------------------------------------------------------


def test_a_distribuicao_do_settings_cobre_a_escala_inteira() -> None:
    dist = gmod.distribuicao()
    assert set(dist) == set(adb.AVALIACOES_ANTES)
    assert abs(sum(dist.values()) - 1.0) < 1e-9


def test_distribuicao_com_chave_fora_da_escala_para_a_campanha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Um alvo que a escala não conhece produziria uma sintética impossível de
    avaliar, e o defeito só apareceria na tela."""
    monkeypatch.setattr(gmod, "_cfg", lambda *a, **k: {"otimo": 1.0})
    with pytest.raises(ValueError, match="fora da escala"):
        gmod.distribuicao()


def test_status_inicial_fora_dos_dois_e_recusado() -> None:
    with pytest.raises(ValueError, match="status inicial"):
        gmod.validar_status_inicial("avaliada")
    assert gmod.status_inicial_padrao() in gmod.STATUS_INICIAIS


def test_os_catalogos_de_defeito_sao_disjuntos_menos_o_nenhum() -> None:
    """Defeito de RESPOSTA e defeito de ANOTAÇÃO são coisas diferentes — e é
    isso que faz o Rate and Review valer: um revisor treinado a pegar resposta
    ruim não é o mesmo que um treinado a pegar avaliação ruim."""
    a = set(gmod.DEFEITOS_RESPOSTA) - {"nenhum"}
    b = set(gmod.DEFEITOS_ANOTACAO) - {"nenhum"}
    assert not (a & b)
    assert gmod.familias_conhecidas("material") is gmod.DEFEITOS_RESPOSTA
    assert gmod.familias_conhecidas("anotacoes") is gmod.DEFEITOS_ANOTACAO
