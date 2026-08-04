"""P8: o instrumento de severidade e a rubrica como DADO.

Cada família aqui existe por um defeito que **não parece quebrado**:

* **a escala que some.** `routes_revisao` gravava a rubrica do anotador com o
  rótulo de um contrato e o conteúdo de outro. A tela desenhava cinco botões
  para uma rubrica de três, e o servidor passava a aceitar nota 9 numa escala
  que ia até 5 — as duas coisas em silêncio, e nenhuma delas levanta exceção;
* **`N/A` confundido com "não avaliei".** São afirmações opostas ("a régua não
  vale aqui" contra "não olhei") e, se compartilharem uma representação, o export
  nunca mais consegue separá-las;
* **o rating sem explicação.** É a falha mais cara do trabalho real. Um checkbox
  sozinho não explica; uma frase sozinha não diz o quê. A regra é o **E** dos
  dois — e ela só pode valer onde a rubrica declara um catálogo, senão dar 3 de
  5 em "clareza" passaria a exigir um tipo de issue que não existe;
* **o diff que cobra dois motivos por uma marcação.** `achatar` trata lista
  vazia como folha, então `tipos_issue` como lista faria o Rate and Review pedir
  duas justificativas para um clique. É por isso que ele é um objeto;
* **a versão global.** `VERSAO_PAYLOAD` era uma constante para os seis
  contratos, e subir um renomearia os outros cinco — que passariam a declarar,
  dentro do JSONL entregue, uma versão que nunca existiu.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from prompt_factory import db as dbmod
from prompt_factory.annotate import avaliacoes as avmod
from prompt_factory.annotate import db as adb
from prompt_factory.annotate import payloads
from prompt_factory.annotate import seed as seedmod
from prompt_factory.annotate import tarefas as tmod
from prompt_factory.annotate.main import criar_app

from .test_annotate_p2 import rubrica_ok
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


def _papeis(cliente: TestClient) -> dict[str, list[int]]:
    saida: dict[str, list[int]] = {}
    for p in cliente.get("/api/perfis").json()["items"]:
        saida.setdefault(p["papel"], []).append(p["id"])
    return saida


# ---------------------------------------------------------------------------
# 1. A VERSÃO É POR TIPO
# ---------------------------------------------------------------------------


def test_subir_um_contrato_nao_renomeia_os_outros_cinco() -> None:
    """Era uma constante global lida por `nome_schema` para os seis.

    `entrega.py` grava `payload_schema` dentro do JSONL entregue: com a global,
    cinco contratos passariam a declarar publicamente uma versão que nunca
    existiu.
    """
    assert payloads.nome_schema("avaliar_rubrica") == "avaliar_rubrica@2"
    for tipo in adb.TIPOS_TAREFA:
        if tipo == "avaliar_rubrica":
            continue
        assert payloads.nome_schema(tipo) == f"{tipo}@1", tipo


def test_o_schema_reserva_a_identidade_do_instrumento() -> None:
    """O construtor do admin (P5a) faz dois instrumentos do MESMO tipo.

    Esta string é gravada em toda linha de `anotacoes` e nada a reescreve, então
    o formato se decide antes de existir alguma linha com instrumento.
    """
    completo = payloads.nome_schema("avaliar_rubrica", "severidade@1")
    assert completo == "avaliar_rubrica@2+severidade@1"
    assert payloads.partes_do_schema(completo) == ("avaliar_rubrica", "2", "severidade@1")
    # E o formato de hoje continua legível pelo mesmo leitor.
    assert payloads.partes_do_schema("comparar_ab@1") == ("comparar_ab", "1", None)


def test_quem_chaveia_por_tipo_ignora_o_instrumento() -> None:
    """É o que faz uma linha com instrumento continuar legível por código antigo."""
    tipo, _, _ = payloads.partes_do_schema("avaliar_rubrica@2+qualquer-coisa@7")
    assert tipo in payloads.MODELOS


# ---------------------------------------------------------------------------
# 2. OS QUATRO ESTADOS DE UM CRITÉRIO
# ---------------------------------------------------------------------------


def _nota(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"criterio": "x", "nota": 3}
    base.update(extra)
    return base


@pytest.mark.parametrize(
    ("payload", "trecho_do_erro"),
    [
        # N/A e nota juntos: dois estados que se contradizem na mesma linha.
        (_nota(nao_aplicavel=True, motivo_na="não pediram tabela"), "não pode ter nota"),
        # N/A sem motivo: o critério desapareceria do conjunto sem explicação.
        ({"criterio": "x", "nao_aplicavel": True}, "exige o motivo"),
        # Nem nota nem N/A: "não avaliado" é estado de FORMULÁRIO e não atravessa.
        ({"criterio": "x"}, "sem nota e sem N/A"),
        # N/A não deve nada, então não pode afirmar nada.
        (
            {
                "criterio": "x",
                "nao_aplicavel": True,
                "motivo_na": "n/a",
                "tipos_issue": {"a": True},
            },
            "não pode ter tipo de issue",
        ),
        # Ou existe trecho, ou o defeito é difuso.
        (_nota(trecho="um trecho", trecho_difuso=True), "não os dois"),
        # A pegadinha de sempre: bool é subclasse de int.
        (_nota(nota=True), "não é booleano"),
    ],
)
def test_estados_que_se_contradizem_sao_recusados(
    payload: dict[str, Any], trecho_do_erro: str
) -> None:
    with pytest.raises(ValueError, match=trecho_do_erro):
        payloads.NotaCriterio.model_validate(payload)


def test_na_com_motivo_e_valido_e_nao_carrega_nota() -> None:
    n = payloads.NotaCriterio.model_validate(
        {"criterio": "x", "nao_aplicavel": True, "motivo_na": "the prompt asked for prose"}
    )
    assert n.nota is None
    assert n.nao_aplicavel is True


# ---------------------------------------------------------------------------
# 3. tipos_issue É OBJETO — E É POR CAUSA DO DIFF
# ---------------------------------------------------------------------------


def test_marcar_um_tipo_de_issue_produz_exatamente_um_caminho_de_diff() -> None:
    """A razão de `tipos_issue` ser objeto e não lista.

    `achatar` trata lista vazia como FOLHA, então `[] -> ["a"]` produziria dois
    caminhos (um sumindo, outro nascendo) e o Rate and Review cobraria **dois
    motivos por uma marcação**. Com todos os ids sempre presentes, todo toggle é
    um caminho, `false` -> `true`.
    """
    antes = {"notas": [{"criterio": "x", "nota": 2, "tipos_issue": {"a": False, "b": False}}]}
    depois = {"notas": [{"criterio": "x", "nota": 2, "tipos_issue": {"a": True, "b": False}}]}
    diff = avmod.diferencas(antes, depois)
    assert [d["campo"] for d in diff] == ["notas.0.tipos_issue.a"]

    # E a prova do contrário: como LISTA, o mesmo clique custaria dois motivos.
    como_lista = avmod.diferencas(
        {"notas": [{"criterio": "x", "tipos_issue": []}]},
        {"notas": [{"criterio": "x", "tipos_issue": ["a"]}]},
    )
    assert len(como_lista) == 2


# ---------------------------------------------------------------------------
# 4. O NORMALIZADOR DE RUBRICA
# ---------------------------------------------------------------------------


def test_o_formulario_do_anotador_vira_a_forma_canonica() -> None:
    c = tmod.normalizar_criterio(
        {
            "nome": "Clareza",
            "descricao": "…",
            "escala_min": 1,
            "escala_max": 3,
            "rotulo_min": "não atende",
            "rotulo_max": "atende plenamente",
        }
    )
    assert c["escala"] == {
        "min": 1,
        "max": 3,
        "ancoras": [
            {"valor": 1, "rotulo": "não atende"},
            {"valor": 3, "rotulo": "atende plenamente"},
        ],
    }
    assert c["escala_declarada"] is True


def test_a_forma_de_fixture_passa_intacta() -> None:
    escala = {"min": 1, "max": 5, "ancoras": [{"valor": 1, "rotulo": "ruim"}]}
    c = tmod.normalizar_criterio({"nome": "x", "escala": escala})
    assert c["escala"]["max"] == 5
    assert c["escala"]["ancoras"] == escala["ancoras"]
    assert c["escala_declarada"] is True


def test_rubrica_sem_escala_nenhuma_e_MARCADA_e_nao_inventada() -> None:
    """Inventar em silêncio foi exatamente o que produziu o defeito original."""
    c = tmod.normalizar_criterio({"nome": "x"})
    assert c["escala_declarada"] is False
    assert "escala" not in c


def test_o_catalogo_curto_ganha_id_canonico() -> None:
    c = tmod.normalizar_criterio({"nome": "x", "tipos_issue": ["too formal"]})
    assert c["tipos_issue"] == [{"id": "too formal", "rotulo": "too formal"}]


# ---------------------------------------------------------------------------
# 5. O DEFEITO REAL, PONTA A PONTA
# ---------------------------------------------------------------------------


def _rubrica_de_tres_pontos() -> dict[str, Any]:
    base = rubrica_ok()
    for c in base["criterios"]:
        c["escala_min"], c["escala_max"] = 1, 3
    return base


def test_rubrica_aprovada_conserva_a_escala_que_declarou(
    cliente: TestClient, semeado: Path
) -> None:
    """Antes do P8: uma rubrica de 1 a 3 virava 1 a 5 sem âncora na tela E
    passava a aceitar nota 9 no servidor. As duas coisas, em silêncio."""
    papeis = _papeis(cliente)
    quem, revisor = papeis["anotador"][0], papeis["revisor"][0]

    env = cliente.post(
        "/api/tarefas/proxima", json={"anotador_id": quem, "tipo": "escrever_rubrica"}
    ).json()["tarefa"]
    assert env is not None
    r = cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
        json={"anotador_id": quem, "payload": _rubrica_de_tres_pontos()},
    )
    assert r.status_code == 200, r.text
    ok = cliente.post(
        f"/api/revisao/{r.json()['anotacao_id']}",
        json={"revisor_id": revisor, "veredito": "aprovada"},
    )
    assert ok.status_code == 200, ok.text

    conn = dbmod.connect(semeado)
    try:
        rubrica = tmod.rubrica_ativa(conn, str(env["prompt"]["uid"]))
    finally:
        conn.close()
    assert rubrica is not None
    criterio = rubrica["criterios"][0]
    assert criterio["escala"]["max"] == 3, "a escala declarada pelo anotador sumiu"
    assert [a["valor"] for a in criterio["escala"]["ancoras"]] == [1, 3]

    # E a consequência que ninguém via: o servidor recusa a nota fora da escala.
    problema = tmod.erro_contra_a_rubrica(
        rubrica, [{"criterio": c["nome"], "nota": 9} for c in rubrica["criterios"]]
    )
    assert problema is not None
    assert "de 1 a 3" in problema


# ---------------------------------------------------------------------------
# 6. A REGRA DE OBRIGAÇÃO
# ---------------------------------------------------------------------------


def _rubrica_severidade() -> dict[str, Any]:
    return {
        "criterios": [
            tmod.normalizar_criterio(
                {
                    "nome": "Register",
                    "escala": {
                        "min": 1,
                        "max": 3,
                        "ancoras": [
                            {"valor": 1, "rotulo": "Major issues"},
                            {"valor": 2, "rotulo": "Minor issues"},
                            {"valor": 3, "rotulo": "No issues"},
                        ],
                    },
                    "tipos_issue": ["wrong register", "over-hedging"],
                }
            )
        ]
    }


def test_sinalizar_sem_tipo_de_issue_e_recusado() -> None:
    problema = tmod.erro_contra_a_rubrica(
        _rubrica_severidade(), [{"criterio": "Register", "nota": 2}]
    )
    assert problema is not None
    assert "sem nenhum tipo de issue" in problema


def test_sinalizar_com_tipo_e_sem_frase_e_recusado() -> None:
    """O `E` da regra: o checkbox diz O QUÊ, a frase diz POR QUÊ."""
    problema = tmod.erro_contra_a_rubrica(
        _rubrica_severidade(),
        [{"criterio": "Register", "nota": 2, "tipos_issue": {"wrong register": True}}],
    )
    assert problema is not None
    assert "sem a frase" in problema


def test_sinalizar_com_tipo_e_frase_passa() -> None:
    assert (
        tmod.erro_contra_a_rubrica(
            _rubrica_severidade(),
            [
                {
                    "criterio": "Register",
                    "nota": 2,
                    "tipos_issue": {"wrong register": True},
                    "justificativa": "It opens in slang after a formal letter was asked for.",
                }
            ],
        )
        is None
    )


def test_o_topo_da_escala_nao_deve_nada() -> None:
    """"No issues" é uma afirmação legítima, e cobrar dela ensinaria a
    plantar um "Minor" falso para destravar o botão."""
    assert (
        tmod.erro_contra_a_rubrica(
            _rubrica_severidade(), [{"criterio": "Register", "nota": 3}]
        )
        is None
    )


def test_tipo_de_issue_fora_do_catalogo_e_recusado() -> None:
    """Vocabulário FECHADO aqui — ao contrário dos catálogos do P4c.

    Lá, fechar obrigaria a editar código para nomear um defeito novo; aqui o
    catálogo mora na rubrica no banco, então estendê-lo é editar DADO.
    """
    problema = tmod.erro_contra_a_rubrica(
        _rubrica_severidade(),
        [
            {
                "criterio": "Register",
                "nota": 2,
                "tipos_issue": {"inventado": True},
                "justificativa": "uma frase suficientemente longa para passar",
            }
        ],
    )
    assert problema is not None
    assert "fora do catálogo" in problema


def test_na_satisfaz_a_cobertura_e_sai_das_checagens_de_nota() -> None:
    assert (
        tmod.erro_contra_a_rubrica(
            _rubrica_severidade(),
            [
                {
                    "criterio": "Register",
                    "nao_aplicavel": True,
                    "motivo_na": "no voice output was requested",
                }
            ],
        )
        is None
    )


def test_a_obrigacao_NAO_vale_para_rubrica_sem_catalogo() -> None:
    """O gatilho é o catálogo, e é isso que preserva a rubrica de qualidade.

    Sem ele, dar 3 de 5 em "clareza" passaria a exigir um tipo de issue que a
    rubrica nunca declarou — e o pacote de demonstração inteiro pararia.
    """
    sem_catalogo = {
        "criterios": [
            tmod.normalizar_criterio(
                {"nome": "Clareza", "escala": {"min": 1, "max": 5, "ancoras": []}}
            )
        ]
    }
    assert tmod.erro_contra_a_rubrica(sem_catalogo, [{"criterio": "Clareza", "nota": 3}]) is None


def test_a_tupla_do_contrato_antigo_continua_aceita() -> None:
    """Uma linha `@1` é avaliada pela regra que valia quando ela nasceu."""
    assert (
        tmod.erro_contra_a_rubrica(_rubrica_severidade(), [("Register", 3)]) is None
    )


# ---------------------------------------------------------------------------
# 7. O GLOSSÁRIO DA ENTREGA COBRE OS CAMPOS NOVOS
# ---------------------------------------------------------------------------


def test_todo_campo_novo_tem_entrada_no_glossario() -> None:
    """As chaves saem em pt-BR no JSONL entregue (é o schema gravado), então o
    glossário é a ÚNICA coisa que as explica para quem recebe."""
    from prompt_factory.annotate import entrega

    novos = set(payloads.NotaCriterio.model_fields) - {"criterio"}
    assert novos <= set(entrega.GLOSSARIO), sorted(novos - set(entrega.GLOSSARIO))


def test_o_payload_entregue_declara_a_versao_do_contrato(
    cliente: TestClient, semeado: Path
) -> None:
    """`payload_schema` viaja com o dado — é o motivo de a coluna existir."""
    papeis = _papeis(cliente)
    quem = papeis["anotador"][0]
    env = cliente.post(
        "/api/tarefas/proxima", json={"anotador_id": quem, "tipo": "avaliar_rubrica"}
    ).json()["tarefa"]
    # O topo da escala DE CADA critério: a fila do `avaliar_rubrica` abre no
    # instrumento de severidade, que vai de 1 a 3, e um `4` literal aqui só
    # testaria que todas as rubricas do pacote têm a mesma régua.
    notas = [
        {"criterio": c["nome"], "nota": int(c["escala"]["max"])}
        for c in env["rubrica"]["criterios"]
    ]
    r = cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
        json={"anotador_id": quem, "payload": {"notas": notas}},
    )
    assert r.status_code == 200, r.text

    conn = dbmod.connect(semeado)
    try:
        linha = conn.execute(
            "SELECT payload_schema, payload_json FROM anotacoes WHERE id = ?",
            (r.json()["anotacao_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert str(linha["payload_schema"]) == payloads.nome_schema("avaliar_rubrica")
    # Os campos novos não são inventados numa submissão que não os usou.
    gravado = json.loads(str(linha["payload_json"]))
    assert gravado["notas"][0]["nao_aplicavel"] is False
    assert gravado["notas"][0]["tipos_issue"] == {}


# ---------------------------------------------------------------------------
# 8. o instrumento DENTRO do banco: o pacote referencia o arsenal
# ---------------------------------------------------------------------------


def test_o_pacote_referencia_o_arsenal_em_vez_de_copiar_o_instrumento() -> None:
    """Uma definição só do instrumento, e ela mora no banco de perguntas.

    Copiá-lo para dentro do ``demo_pack.json`` daria DUAS definições da mesma
    coisa, e a divergência entre elas apareceria como uma tarefa que valida
    contra critérios que a tela não desenha — do lado errado do fio, onde nada
    levanta exceção.
    """
    from prompt_factory.annotate import rubricas_modelo

    bruto = json.loads(seedmod.PACOTE.read_text(encoding="utf-8"))
    cru = next(
        i for i in bruto["itens"] if i["chave"] == "procedimento-incidente-academia"
    )
    assert cru["rubrica"] == {"modelo": "severidade"}, "o pacote reenunciou o instrumento"

    # E na LEITURA ele vira a rubrica inteira, na mesma forma dos outros onze.
    item = next(
        i
        for i in seedmod.carregar_pacote()["itens"]
        if i["chave"] == "procedimento-incidente-academia"
    )
    modelo = next(m for m in rubricas_modelo.carregar() if m["id"] == "severidade")
    assert item["rubrica"]["criterios"] == modelo["criterios"]
    assert item["rubrica"]["titulo"] == modelo["titulo"]


def test_modelo_inexistente_no_pacote_falha_alto(tmp_path: Path) -> None:
    """Um id errado tem de morrer no ``pf annotate seed``, no terminal — não
    virar uma tarefa com rubrica vazia que ninguém consegue anotar."""
    caminho = tmp_path / "pack.json"
    caminho.write_text(
        json.dumps(
            {
                "schema": "demo_pack@1",
                "itens": [
                    {
                        "chave": "x",
                        "lang": "en",
                        "prompt": "oi",
                        "rubrica": {"modelo": "nao-existe"},
                        "respostas": [],
                    }
                ],
                "tarefas": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="nao-existe"):
        seedmod.carregar_pacote(caminho)


def test_a_rubrica_semeada_carimba_o_contrato_que_ela_de_fato_fala(
    semeado: Path,
) -> None:
    """O rótulo tem de casar com o conteúdo, que é a lição do ``routes_revisao``.

    Uma rubrica com ``grupo``/``tipos_issue``/``resumo`` carimbada ``rubrica@2``
    seria a mesma mentira silenciosa: o rótulo prometendo um contrato e o
    conteúdo falando outro.
    """
    conn = dbmod.connect(semeado)
    try:
        linha = conn.execute(
            "SELECT criterios_json FROM rubricas WHERE titulo = 'Severity review'"
        ).fetchone()
    finally:
        conn.close()
    assert linha is not None, "o instrumento de severidade não foi semeado"
    conteudo = json.loads(str(linha["criterios_json"]))
    assert conteudo["schema"] == seedmod.SCHEMA_RUBRICA == "rubrica@3"
    criterios = conteudo["criterios"]
    assert any(c.get("grupo") for c in criterios), "sem grupo não há aba nem cabeçalho"
    assert any(c.get("tipos_issue") for c in criterios), "sem catálogo não há obrigação"
    assert any(c.get("resumo") for c in criterios), "sem overall o rail não tranca nada"


def test_a_fila_do_avaliar_abre_no_instrumento_e_ele_cobra_evidencia(
    cliente: TestClient,
) -> None:
    """O caminho inteiro, do banco à recusa: a tarefa de maior prioridade da
    fila é a de severidade, e uma nota abaixo do topo não passa sem o tipo de
    issue **e** a frase.

    É a mesma função (``tarefas.erro_contra_a_rubrica``) que a rota de submissão
    e o import da campanha do P4c chamam — e é ela que o botão da tela espelha.
    """
    quem = _papeis(cliente)["anotador"][0]
    env = cliente.post(
        "/api/tarefas/proxima", json={"anotador_id": quem, "tipo": "avaliar_rubrica"}
    ).json()["tarefa"]
    assert env["rubrica"]["titulo"] == "Severity review"
    criterios = env["rubrica"]["criterios"]
    topo = [{"criterio": c["nome"], "nota": int(c["escala"]["max"])} for c in criterios]

    def enviar(notas: list[dict[str, Any]]) -> Any:
        return cliente.post(
            f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
            json={"anotador_id": quem, "payload": {"notas": notas}},
        )

    alvo = next(i for i, c in enumerate(criterios) if c.get("tipos_issue"))
    tipo = criterios[alvo]["tipos_issue"][0]["id"]

    # 1. abaixo do topo, sem nada: falta o tipo de issue.
    sem_nada = json.loads(json.dumps(topo))
    sem_nada[alvo]["nota"] = 1
    r = enviar(sem_nada)
    assert r.status_code == 422 and "tipo de issue" in r.json()["detail"]

    # 2. com o tipo e sem a frase: o checkbox diz o QUÊ, a frase diz o PORQUÊ.
    so_o_tipo = json.loads(json.dumps(sem_nada))
    so_o_tipo[alvo]["tipos_issue"] = {tipo: True}
    r = enviar(so_o_tipo)
    assert r.status_code == 422, r.text
    assert "tipo de issue" not in r.json()["detail"]

    # 3. com os dois: passa.
    completo = json.loads(json.dumps(so_o_tipo))
    completo[alvo]["justificativa"] = "It drops the near-miss question entirely."
    assert enviar(completo).status_code == 200, enviar(completo).text
