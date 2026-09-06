"""Central de Briefs Pedagógicos, F2: a campanha que destila PEDIDOS.

O molde é a campanha de geração (P4c) e o que se prova aqui é que ele foi
copiado onde importa — manifest como livro-caixa, id pelo manifest e nunca por
``listdir``, arquivo antes do manifest, falha de validação não muda estado — e
adaptado onde o insumo é outro: aqui não há pool nem corpus, há **arquivos de
terceiros com copyright integral**.

As famílias:

* **verbatim significa verbatim.** A conferência de substring não normaliza
  nada. Normalizar produziria um recorte que "confere" e não existe no arquivo,
  e a proveniência do pedido deixaria de ser auditável;
* **recusar exige certeza; deixar passar, não** (a lição do s02, quarta
  aplicação). Enum, cap e substring são erro; material sujo é aviso GRAVADO;
* **o cursor por arquivo é o que torna a campanha retomável**, e duas
  preparações seguidas nunca entregam a mesma janela;
* **a chave natural é a idempotência.** O mesmo recorte chegando por outro lote
  não vira um segundo pedido.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from prompt_factory import config
from prompt_factory import db as dbmod
from prompt_factory.annotate import db as adb
from prompt_factory.annotate import destilacao as dmod
from prompt_factory.textnorm import norm_for_hash

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

#: Um "capítulo" sintético, com as sujeiras que o material real tem. Escrito à
#: mão e não recortado das apostilas: o teste não pode carregar texto da editora,
#: que é a regra que este marco inteiro existe para respeitar.
CAPITULO = (
    "UNIDADE 2 — O CONHECIMENTO E SUAS FORMAS (EM13CHS101, EM13LP16)\n\n"
    "A distincao entre saber e opiniao atravessa a historia da filosofia. "
    "Quando alguem afirma que sabe algo, esta comprometido com razoes que "
    "sustentem a afirmacao; quando apenas opina, o compromisso e outro. "
    "Este capitulo trabalha essa fronteira a partir de exemplos do cotidiano "
    "escolar, e nao a partir de definicoes prontas de dicionario.\n\n"
    "ATIVIDADE 1. Formule, com suas palavras, a diferenca entre saber e opinar.\n"
)


def _texto_material(n_blocos: int = 12) -> str:
    """Um arquivo grande o bastante para render várias janelas."""
    return "".join(f"[bloco {i:02d}]\n{CAPITULO}\n" for i in range(n_blocos))


@pytest.fixture
def material(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Uma pasta de material FALSA, com janelas pequenas para o teste ser rápido."""
    raiz = tmp_path / "material"
    raiz.mkdir()
    arquivo = raiz / "DOSEUJEITO_PNLD26_Filosofia_VU_MP.txt"
    arquivo.write_text(_texto_material(), encoding="utf-8")
    secao = config.settings()["pedidos"]
    monkeypatch.delenv("PF_MATERIAL_DIR", raising=False)
    monkeypatch.setitem(secao, "material_dir", str(raiz))
    monkeypatch.setitem(secao, "janela_chars", 1_000)
    monkeypatch.setitem(secao, "janela_overlap", 100)
    monkeypatch.setitem(secao, "recorte_min_chars", 60)
    monkeypatch.setitem(secao, "recorte_max_chars", 400)
    return arquivo


@pytest.fixture
def dp(tmp_path: Path) -> dmod.DestilacaoPaths:
    return dmod.DestilacaoPaths(tmp_path / "destilacao")


@pytest.fixture
def conn(tmp_path: Path):
    c = dbmod.connect(tmp_path / "annotate.sqlite")
    try:
        adb.init_db(c)
        yield c
    finally:
        c.close()


def _pedido(lote: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Um pedido válido montado a partir de uma janela REAL do lote."""
    janela = lote["janelas"][extra.pop("_janela", 0)]
    texto = str(janela["texto"])
    base = {
        "janela_id": janela["janela_id"],
        "recorte": texto[100:400],
        "task_type": "redacao-pratica",
        "papel": "professor",
        "tema": "Saber e opiniao",
        "meta_pedagogica": (
            "Exercitar a distincao entre saber e opinar a partir de exemplos do "
            "cotidiano escolar."
        ),
        "habilidades": ["EM13CHS101"],
        "justificativa": "O trecho apresenta a fronteira com exemplo.",
    }
    base.update(extra)
    return base


def _importar(conn: sqlite3.Connection, dp: dmod.DestilacaoPaths, lote_id: str, corpo: Any):
    return dmod.importar(conn, lote_id, json.dumps(corpo, ensure_ascii=False), dp=dp)


# ---------------------------------------------------------------------------
# 1. o nome do arquivo → coleção e disciplina
# ---------------------------------------------------------------------------

#: Os casos REAIS que quebram um ``split("_")``. Foram medidos nos 35 arquivos: a
#: nomenclatura não tem padrão único, e os três primeiros são a prova. A quarta
#: coluna é a SÉRIE do marcador de volume (``VOL2``, ``_1_``); ``VU`` e o nome
#: hifenizado (cujo ``2026`` não é um ``[123]`` isolado) devolvem vazio.
NOMES = [
    # a disciplina vem ANTES da coleção
    ("ESPANHOL_SINTESIS_PNLD26_VU_LP.txt", "Síntesis", "Espanhol", ""),
    # tudo minúsculo e hifenizado, sem "PNLD26" no lugar de sempre
    (
        "identidade-saraiva-projetos-integradores-matematica-pnld-ensino-medio-2026.txt",
        "Identidade",
        "Projetos Integradores",
        "",
    ),
    # a mesma coleção com ESPAÇO em vez de underscore
    ("IDENTIDADE SARAIVA_PNLD26_REDACAO_VU_MP.txt", "Identidade", "Redação", ""),
    ("CIENCIA VIVA_PNLD26_BIOLOGIA_VU_PR.txt", "Ciência Viva", "Biologia", ""),
    ("DOSEUJEITO_PNLD26_Filosofia_VU_MP.txt", "Do Seu Jeito", "Filosofia", ""),
    ("DOSEUJEITO_PNLD26_CIE_HUMANAS_VU_MP.txt", "Do Seu Jeito", "Ciências Humanas", ""),
    ("DOSEUJEITO_PNLD26_LP_VOL2_MP.txt", "Do Seu Jeito", "Língua Portuguesa", "2"),
    ("DOSEUJEITO_PNLD26_MATEM_VOL3_MP.txt", "Do Seu Jeito", "Matemática", "3"),
    # o dígito SOLTO entre underscores — a outra grafia real de volume
    ("IDENTIDADE_SARAIVA_PNLD26_MATEM_1_MP.txt", "Identidade", "Matemática", "1"),
    # `ED_FISICA` tem de vencer `FISICA` — é o caso que a ordem por tamanho salva
    ("IDENTIDADE_SARAIVA_PNLD26_EM_ED_FISICA_VU_MP.txt", "Identidade", "Educação Física", ""),
    ("IDENTIDADE_SARAIVA_PNLD26_EM_ED_DIGITAL_VU_MP.txt", "Identidade", "Educação Digital", ""),
]


@pytest.mark.parametrize(("nome", "colecao", "disciplina", "serie"), NOMES)
def test_o_nome_do_arquivo_revela_colecao_disciplina_e_serie(
    nome: str, colecao: str, disciplina: str, serie: str
) -> None:
    saida = dmod.metadados_do_nome(nome)
    assert saida == {"colecao": colecao, "disciplina": disciplina, "serie": serie}


def test_nome_desconhecido_devolve_vazio_em_vez_de_chutar() -> None:
    """Vazio é resposta, e as colunas têm ``DEFAULT ''`` por causa disso.

    Um rótulo errado numa faceta que o operador usa para escolher o que destilar
    é pior que um campo em branco — o branco pelo menos se vê. Vale para a
    série também: o ``2026`` deste nome contém ``2``, mas não é um ``[123]``
    isolado entre fronteiras.
    """
    assert dmod.metadados_do_nome("apostila_qualquer_2026.txt") == {
        "colecao": "",
        "disciplina": "",
        "serie": "",
    }


def test_LP_nao_casa_dentro_de_PNLD() -> None:
    """A fronteira de ``_`` existe para isto: ``PNLD26`` contém ``LP``."""
    assert dmod.metadados_do_nome("COLECAO_PNLD26_BIOLOGIA_VU.txt")["disciplina"] == "Biologia"


# ---------------------------------------------------------------------------
# 2. a forma do código BNCC (medida, não suposta)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("codigo", "vale"),
    [
        ("EM13CNT301", True),   # a forma dominante: 12.800 ocorrências
        ("EM13LP16", True),     # 4.410 — as que o regex do plano recusaria
        ("EM13LG101", True),    # 6
        ("EM13MAT", False),     # sem dígito: artefato de extração
        ("EM13L34", False),     # uma letra só
        ("EM13LGG2103", False), # um dígito a mais
        ("EF15AR01", False),    # Ensino Fundamental — outra etapa
        ("", False),
    ],
)
def test_o_padrao_BNCC_cobre_a_forma_MEDIDA_no_material(codigo: str, vale: bool) -> None:
    """O plano propunha ``EM13[A-Z]{3}\\d{3}``, que recusaria 4.426 de 17.226
    ocorrências — quase todas os códigos de Língua Portuguesa, que têm duas
    letras e dois dígitos. Recusar por uma certeza que não se tem é o defeito
    que o s02 já custou uma vez."""
    assert bool(dmod.BNCC.fullmatch(codigo)) is vale


# ---------------------------------------------------------------------------
# 3. fatiar e o cursor
# ---------------------------------------------------------------------------


def test_as_janelas_se_sobrepoem_e_o_cursor_anda_pelo_PASSO() -> None:
    """A sobreposição existe para um trecho bom não morrer cortado na fronteira.

    Se o cursor andasse pelo TAMANHO da janela, ele reintroduziria exatamente o
    corte que a sobreposição existe para evitar.
    """
    texto = "x" * 10_000
    janelas = dmod.fatiar(texto, 0, 3, tamanho=1_000, overlap=100)
    assert [j["offset_inicio"] for j in janelas] == [0, 900, 1_800]
    assert all(len(j["texto"]) == 1_000 for j in janelas)
    assert dmod.proximo_cursor(0, 3, tamanho=1_000, overlap=100) == 2_700


def test_arquivo_esgotado_devolve_lista_vazia() -> None:
    assert dmod.fatiar("x" * 100, 500, 3, tamanho=50, overlap=10) == []


def test_a_ultima_janela_sai_curta_em_vez_de_sumir() -> None:
    """Fatia final menor que a janela ainda é material de trabalho."""
    janelas = dmod.fatiar("x" * 120, 0, 5, tamanho=100, overlap=10)
    assert [len(j["texto"]) for j in janelas] == [100, 30]


def test_overlap_maior_que_a_janela_e_recusado() -> None:
    """Sem a guarda o passo seria zero ou negativo e o cursor nunca andaria — a
    campanha entregaria a mesma janela para sempre."""
    with pytest.raises(ValueError, match="overlap"):
        dmod.fatiar("x" * 100, 0, 1, tamanho=100, overlap=100)


# ---------------------------------------------------------------------------
# 4. a chave natural
# ---------------------------------------------------------------------------


def test_a_chave_usa_a_normalizacao_CANONICA_do_corpus() -> None:
    """Comparada contra a função importada, nunca contra um literal copiado.

    É a mesma disciplina de ``criacoes.chave``: um digest recalculado à mão no
    teste passaria a validar a cópia, e não a cadeia.
    """
    import hashlib

    recorte = "Um trecho   com   espaço  irregular."
    esperado = hashlib.sha256(
        f"arq.txt\x00{norm_for_hash(recorte)}".encode()
    ).hexdigest()
    assert dmod.chave("arq.txt", recorte) == esperado


def test_espaco_irregular_colapsa_na_mesma_chave() -> None:
    """A chave normaliza (é a pergunta "é o mesmo pedido?"); a conferência de
    substring não normaliza nada (é "este texto existe no arquivo?")."""
    assert dmod.chave("a.txt", "saber e opiniao") == dmod.chave("a.txt", "saber  e   opiniao")


def test_arquivos_diferentes_nao_colidem() -> None:
    assert dmod.chave("a.txt", "mesmo texto") != dmod.chave("b.txt", "mesmo texto")


def test_recorte_so_de_pontuacao_nao_colapsa_com_outro() -> None:
    """``norm_for_hash`` devolve string VAZIA para pontuação pura — a pegadinha
    que o s04 e o ``criacoes.chave`` já documentam. Sem a guarda, dois recortes
    degenerados virariam um pedido só por acidente."""
    assert norm_for_hash("!!!") == ""
    assert dmod.chave("a.txt", "!!!") != dmod.chave("a.txt", "???")


# ---------------------------------------------------------------------------
# 5. os avisos do material sujo
# ---------------------------------------------------------------------------


def test_marcador_de_gabarito_e_de_credito_viram_AVISO_com_o_porque() -> None:
    """Aviso e não erro: o material é o Manual do Professor e é sujo por
    natureza. O juiz certo é o humano que vai ler o pedido na tela."""
    avisos = dmod.avisos_do_recorte("... Resposta: espera-se dois exemplos. Acesso em: 23 fev.")
    assert len(avisos) == 2
    assert any("Manual do Professor" in a for a in avisos)
    assert any("citação de terceiro" in a or "referência" in a for a in avisos)


def test_a_hifenizacao_e_avisada_por_DENSIDADE_e_nao_por_presenca() -> None:
    """MEDIDO na primeira rodada real: avisar a partir de UMA quebra disparava em
    **92% dos 42 recortes** — o aviso passava a dizer "este livro veio de um PDF"
    (verdade sobre o material inteiro) em vez de "este recorte é difícil de ler".

    A distribuição: mediana 3,0 quebras/mil, p80 5,6. O limiar de 6,0 avisa ~19%.
    """
    tipico = "a natureza huma-\nna e o mundo. " + "x" * 640  # 1 quebra em ~670
    assert not any("hifeniza" in a for a in dmod.avisos_do_recorte(tipico))

    denso = ("pala-\nvra " * 12) + "y" * 500  # 12 quebras em ~620
    avisos = dmod.avisos_do_recorte(denso)
    assert any("hifeniza" in a for a in avisos)
    # o aviso diz o NÚMERO e a densidade: "está sujo" sem quanto não ajuda a triar
    assert any("por mil" in a and "12" in a for a in avisos)


def test_o_limiar_da_hifenizacao_sai_do_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    tipico = "a natureza huma-\nna e o mundo. " + "x" * 640
    monkeypatch.setitem(config.settings()["pedidos"], "hifenizacao_por_mil", 0.5)
    assert any("hifeniza" in a for a in dmod.avisos_do_recorte(tipico))


def test_recorte_limpo_nao_gera_aviso() -> None:
    assert dmod.avisos_do_recorte("Um paragrafo didatico comum, sem sujeira nenhuma.") == []


# ---------------------------------------------------------------------------
# 6. preparar
# ---------------------------------------------------------------------------


def test_preparar_escreve_o_lote_e_anda_o_cursor(
    material: Path, dp: dmod.DestilacaoPaths
) -> None:
    rel = dmod.preparar(arquivo=material.name, n=3, dp=dp)
    assert rel["lote_id"] == "ped_0001" and rel["n"] == 3
    assert rel["disciplina"] == "Filosofia" and rel["colecao"] == "Do Seu Jeito"

    lote = json.loads(dp.lote("ped_0001").read_text(encoding="utf-8"))
    assert lote["contrato"] == dmod.CONTRATO
    assert lote["arquivo_fonte"] == material.name
    assert [j["janela_id"] for j in lote["janelas"]] == ["j01", "j02", "j03"]

    manifest = dmod.carregar_manifest(dp)
    assert manifest["lotes"]["ped_0001"]["status"] == dmod.CLAIMED
    assert manifest["cursores"][material.name] == 3 * (1_000 - 100)


def test_duas_preparacoes_seguidas_NUNCA_entregam_a_mesma_janela(
    material: Path, dp: dmod.DestilacaoPaths
) -> None:
    """O cursor por arquivo é o que torna a campanha retomável — a disciplina do
    checkpoint do WildChat."""
    a = dmod.preparar(arquivo=material.name, n=2, dp=dp)
    b = dmod.preparar(arquivo=material.name, n=2, dp=dp)
    assert b["lote_id"] == "ped_0002"
    offsets = {
        lid: [
            j["offset_inicio"]
            for j in json.loads(dp.lote(lid).read_text(encoding="utf-8"))["janelas"]
        ]
        for lid in ("ped_0001", "ped_0002")
    }
    assert set(offsets["ped_0001"]) & set(offsets["ped_0002"]) == set()
    assert a["cursor"] == offsets["ped_0002"][0]


def test_arquivo_percorrido_ate_o_fim_devolve_motivo_em_vez_de_lote_vazio(
    material: Path, dp: dmod.DestilacaoPaths
) -> None:
    while dmod.preparar(arquivo=material.name, n=50, dp=dp)["lote_id"]:
        pass
    rel = dmod.preparar(arquivo=material.name, n=2, dp=dp)
    assert rel["lote_id"] is None and "percorrido até o fim" in rel["motivo"]


def test_o_lote_carrega_o_vocabulario_e_os_limites(
    material: Path, dp: dmod.DestilacaoPaths
) -> None:
    """O agente é puro cômputo: ele não consulta a taxonomia nem o settings.toml.
    O que não viajar no lote, ele inventa."""
    dmod.preparar(arquivo=material.name, n=1, dp=dp)
    lote = json.loads(dp.lote("ped_0001").read_text(encoding="utf-8"))
    ids = {op["id"] for op in lote["vocabulario"]["task_type"]}
    assert ids and ids.isdisjoint(set(dmod.task_types_excluidos()))
    assert lote["vocabulario"]["papel"] == list(adb.PAPEIS_PEDIDO)
    assert lote["vocabulario"]["serie"] == list(adb.SERIES_PEDIDO)
    assert lote["limites"]["recorte_min_chars"] == 60
    # as excluídas são DITAS, não só recusadas em silêncio pelo import
    assert set(lote["task_types_excluidos"]["ids"]) == set(dmod.task_types_excluidos())


def test_o_lote_nao_carrega_o_arquivo_inteiro(
    material: Path, dp: dmod.DestilacaoPaths
) -> None:
    """Janela é janela. Mandar o arquivo todo estouraria o contexto do agente e
    não deixaria o offset significar nada."""
    dmod.preparar(arquivo=material.name, n=2, dp=dp)
    lote = json.loads(dp.lote("ped_0001").read_text(encoding="utf-8"))
    assert all(len(j["texto"]) <= 1_000 for j in lote["janelas"])
    assert sum(len(j["texto"]) for j in lote["janelas"]) < len(
        material.read_text(encoding="utf-8")
    )


def test_nome_com_caminho_e_recusado(material: Path, dp: dmod.DestilacaoPaths) -> None:
    """``--arquivo ../../etc/senha`` sairia da pasta configurada, e
    ``arquivo_fonte`` deixaria de significar "um arquivo do material"."""
    with pytest.raises(ValueError, match="sem caminho"):
        dmod.preparar(arquivo=f"../{material.name}", n=1, dp=dp)


def test_material_dir_nao_configurado_diz_o_que_fazer(
    monkeypatch: pytest.MonkeyPatch, dp: dmod.DestilacaoPaths
) -> None:
    monkeypatch.delenv("PF_MATERIAL_DIR", raising=False)
    monkeypatch.setitem(config.settings()["pedidos"], "material_dir", "")
    with pytest.raises(ValueError, match="material_dir"):
        dmod.preparar(arquivo="x.txt", n=1, dp=dp)


def test_o_id_do_lote_vem_do_MANIFEST_e_nunca_de_listdir(
    material: Path, dp: dmod.DestilacaoPaths
) -> None:
    """A disciplina do part-file do WildChat: um arquivo órfão no disco (sessão
    que caiu antes de salvar o manifest) faria ``listdir`` pular um número e o
    lote seguinte sobrescrever o anterior."""
    dmod.preparar(arquivo=material.name, n=1, dp=dp)
    (dp.lotes / "ped_0009.json").write_text("{}", encoding="utf-8")
    assert dmod.preparar(arquivo=material.name, n=1, dp=dp)["lote_id"] == "ped_0002"


def test_o_arquivo_do_lote_e_escrito_ANTES_do_manifest(
    material: Path, dp: dmod.DestilacaoPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invertida, a ordem deixaria um lote ``claimed`` sem arquivo nenhum — e o
    maestro não teria o que despachar nem como descobrir o que faltou. Do jeito
    certo o pior caso é um arquivo órfão, que o ``preparar`` seguinte
    sobrescreve."""
    def explodir(*a: Any, **k: Any) -> None:
        raise RuntimeError("crash entre as duas escritas")

    monkeypatch.setattr(dmod, "salvar_manifest", explodir)
    with pytest.raises(RuntimeError):
        dmod.preparar(arquivo=material.name, n=1, dp=dp)
    assert dp.lote("ped_0001").is_file()
    assert dmod.carregar_manifest(dp)["lotes"] == {}


# ---------------------------------------------------------------------------
# 7. validação — o que é ERRO
# ---------------------------------------------------------------------------


@pytest.fixture
def lote(material: Path, dp: dmod.DestilacaoPaths) -> dict[str, Any]:
    dmod.preparar(arquivo=material.name, n=2, dp=dp)
    return json.loads(dp.lote("ped_0001").read_text(encoding="utf-8"))


def test_um_pedido_plausivel_passa(lote: dict[str, Any]) -> None:
    pedidos, erros, _ = dmod.validar(lote, {"pedidos": [_pedido(lote)]})
    assert erros == []
    assert len(pedidos) == 1
    p = pedidos[0]
    assert p["offset_inicio"] == lote["janelas"][0]["offset_inicio"]
    assert p["serie"] == "indefinido" and p["dificuldade"] == "intermediaria"
    assert p["habilidades"] == ["EM13CHS101"]
    assert p["disciplina"] == "Filosofia"


def test_zero_pedidos_numa_janela_e_resposta_LEGITIMA(lote: dict[str, Any]) -> None:
    """Janela de créditos, de sumário ou de gabarito não rende pedido nenhum, e
    exigir cobertura de todas as janelas — como a campanha do P4c faz com os
    uids — obrigaria o agente a inventar."""
    pedidos, erros, _ = dmod.validar(lote, {"pedidos": []})
    assert (pedidos, erros) == ([], [])


def test_recorte_adulterado_em_UM_caractere_e_recusado_nomeando_a_janela(
    lote: dict[str, Any],
) -> None:
    """Verbatim significa verbatim: a conferência não normaliza NADA. Normalizar
    criaria um recorte que "confere" e não existe no arquivo."""
    bom = _pedido(lote)
    adulterado = bom["recorte"][:50] + "X" + bom["recorte"][51:]
    _, erros, _ = dmod.validar(lote, {"pedidos": [{**bom, "recorte": adulterado}]})
    assert len(erros) == 1
    assert "substring EXATA" in erros[0] and "j01" in erros[0]


def test_ate_um_espaco_a_mais_e_recusado(lote: dict[str, Any]) -> None:
    bom = _pedido(lote)
    _, erros, _ = dmod.validar(lote, {"pedidos": [{**bom, "recorte": bom["recorte"] + " "}]})
    assert erros and "substring EXATA" in erros[0]


def test_recorte_que_saiu_do_ARQUIVO_desde_o_preparar_e_recusado(
    lote: dict[str, Any],
) -> None:
    """A janela gravada no lote continua dizendo que o trecho existiu; o arquivo
    no disco diz que não existe mais. Sem esta segunda conferência o pedido
    nasceria inconferível e ninguém notaria."""
    _, erros, _ = dmod.validar(
        lote, {"pedidos": [_pedido(lote)]}, texto_do_arquivo="um arquivo completamente outro"
    )
    assert erros and "mudou desde o `preparar`" in erros[0]


def test_task_type_inventado_e_recusado(lote: dict[str, Any]) -> None:
    _, erros, _ = dmod.validar(
        lote, {"pedidos": [_pedido(lote, task_type="prova-dissertativa")]}
    )
    assert erros and "não é uma classe da taxonomia" in erros[0]


@pytest.mark.parametrize("excluido", ["qa-contexto", "resumo", "reescrita-edicao"])
def test_as_classes_que_exigem_material_colado_sao_recusadas(
    lote: dict[str, Any], excluido: str
) -> None:
    """A §6 do plano aplicada: elas só funcionam com o texto-base DENTRO do
    prompt, e o material é de editora comercial. A frase de erro DIZ isso — um
    "task_type inválido" seco faria parecer bug."""
    _, erros, _ = dmod.validar(lote, {"pedidos": [_pedido(lote, task_type=excluido)]})
    assert erros and "fora do escopo" in erros[0] and "editora comercial" in erros[0]


@pytest.mark.parametrize(
    ("campo", "valor"),
    [("papel", "coordenador"), ("serie", "4"), ("dificuldade", "impossivel")],
)
def test_enum_fora_do_vocabulario_e_recusado(
    lote: dict[str, Any], campo: str, valor: str
) -> None:
    _, erros, _ = dmod.validar(lote, {"pedidos": [_pedido(lote, **{campo: valor})]})
    assert erros and campo in erros[0]


# ---------------------------------------------------------------------------
# 7b. a série sugerida pelo nome do arquivo (decisão fechada em 2026-08-13)
# ---------------------------------------------------------------------------
#
# Na rodada do F3, `serie` saiu `indefinido` em 42 de 42: os arquivos eram de
# volume único E o ano não aparece no texto — mas as coleções seriadas carregam
# o volume no NOME do arquivo, onde o destilador não enxerga. A premissa
# "volume N = N-ª série" é o formato seriado do PNLD-EM, e ela entra como
# SUGESTÃO: viaja no lote, o agente ainda decide por janela, e o import só a
# usa quando o agente não amarrou nada.


def test_o_lote_carrega_a_serie_sugerida_do_nome(lote: dict[str, Any]) -> None:
    """O material da fixture é volume único: a sugestão viaja VAZIA — presente
    como chave (o despacho a preenche com "nenhuma") e sem valor inventado."""
    assert lote["serie_sugerida"] == ""


def test_montar_lote_propaga_a_serie_do_meta() -> None:
    lote = dmod.montar_lote(
        [], "ped_0001", arquivo_fonte="X_VOL2_MP.txt",
        meta=dmod.metadados_do_nome("X_MATEM_VOL2_MP.txt"),
    )
    assert lote["serie_sugerida"] == "2"


def test_serie_omitida_pelo_agente_cai_na_sugestao_do_arquivo(
    lote: dict[str, Any],
) -> None:
    """`indefinido` do agente significa "a janela não amarra o ano" — que não
    apaga um fato do ARQUIVO: o volume amarra a série no nível do livro."""
    lote["serie_sugerida"] = "2"
    pedidos, erros, _ = dmod.validar(lote, {"pedidos": [_pedido(lote)]})
    assert erros == [] and pedidos[0]["serie"] == "2"


def test_serie_explicita_do_agente_VENCE_a_sugestao(lote: dict[str, Any]) -> None:
    """É para isso que ela é sugestão e não valor forçado: um capítulo de
    revisão pode amarrar outro ano que o do volume."""
    lote["serie_sugerida"] = "2"
    pedidos, erros, _ = dmod.validar(lote, {"pedidos": [_pedido(lote, serie="3")]})
    assert erros == [] and pedidos[0]["serie"] == "3"


def test_indefinido_explicito_tambem_cai_na_sugestao(lote: dict[str, Any]) -> None:
    """As duas formas de "não amarrei" — omitir e escrever `indefinido` — são a
    mesma resposta, e tratá-las diferente faria o resultado depender do estilo
    do modelo."""
    lote["serie_sugerida"] = "1"
    pedidos, erros, _ = dmod.validar(
        lote, {"pedidos": [_pedido(lote, serie="indefinido")]}
    )
    assert erros == [] and pedidos[0]["serie"] == "1"


def test_sugestao_torta_e_ignorada_sem_recusar_o_pedido(lote: dict[str, Any]) -> None:
    """Um `serie_sugerida` inválido (lote editado à mão) não pode derrubar o
    pedido: o agente nem escreveu esse campo — seria punir a resposta pela
    pergunta. Ignorado, o pedido sai `indefinido`, que é a verdade disponível."""
    lote["serie_sugerida"] = "9"
    pedidos, erros, _ = dmod.validar(lote, {"pedidos": [_pedido(lote)]})
    assert erros == [] and pedidos[0]["serie"] == "indefinido"


def test_recorte_fora_dos_caps_e_recusado(lote: dict[str, Any]) -> None:
    texto = str(lote["janelas"][0]["texto"])
    _, curto, _ = dmod.validar(lote, {"pedidos": [_pedido(lote, recorte=texto[:10])]})
    assert curto and "aceito: 60..400" in curto[0]
    _, longo, _ = dmod.validar(lote, {"pedidos": [_pedido(lote, recorte=texto[:900])]})
    assert longo and "aceito: 60..400" in longo[0]


def test_o_teto_por_janela_e_respeitado(lote: dict[str, Any]) -> None:
    """Zero é legítimo; três é o teto. Ele existe contra o agente que "acha"
    pedidos em toda janela para parecer produtivo."""
    texto = str(lote["janelas"][0]["texto"])
    pedidos = [_pedido(lote, recorte=texto[i * 100 : i * 100 + 200]) for i in range(5)]
    _, erros, _ = dmod.validar(lote, {"pedidos": pedidos})
    assert erros and "que é o teto" in erros[0]


def test_dois_pedidos_sobre_o_MESMO_recorte_sao_o_mesmo_pedido(
    lote: dict[str, Any],
) -> None:
    p = _pedido(lote)
    _, erros, _ = dmod.validar(lote, {"pedidos": [p, {**p, "tema": "Outro tema"}]})
    assert erros and "recorte repetido" in erros[0]


def test_janela_inexistente_e_recusada(lote: dict[str, Any]) -> None:
    _, erros, _ = dmod.validar(lote, {"pedidos": [_pedido(lote, janela_id="j99")]})
    assert erros and "não existe neste lote" in erros[0]


@pytest.mark.parametrize("campo", ["tema", "meta_pedagogica"])
def test_booleano_onde_se_espera_texto_e_recusado(lote: dict[str, Any], campo: str) -> None:
    """``bool`` é subclasse de ``int`` e ``str(True)`` seria "True" — uma resposta
    válida e inventada. Sexta aparição desta pegadinha no repositório."""
    _, erros, _ = dmod.validar(lote, {"pedidos": [_pedido(lote, **{campo: True})]})
    assert erros and any(campo in e and "booleano" in e for e in erros)


def test_meta_pedagogica_curta_demais_e_recusada(lote: dict[str, Any]) -> None:
    _, erros, _ = dmod.validar(lote, {"pedidos": [_pedido(lote, meta_pedagogica="ensinar")]})
    assert erros and "meta_pedagogica" in erros[0]


def test_codigo_BNCC_malformado_e_recusado_e_o_duplicado_colapsa(
    lote: dict[str, Any],
) -> None:
    _, erros, _ = dmod.validar(lote, {"pedidos": [_pedido(lote, habilidades=["EM13MAT"])]})
    assert erros and "código BNCC" in erros[0]
    pedidos, erros, _ = dmod.validar(
        lote, {"pedidos": [_pedido(lote, habilidades=["EM13LP16", "em13lp16"])]}
    )
    assert erros == [] and pedidos[0]["habilidades"] == ["EM13LP16"]


def test_habilidades_ausente_e_valido(lote: dict[str, Any]) -> None:
    """Nem toda janela traz código de habilidade, e exigir um faria o agente
    inventar — que é o pior desfecho possível para um campo de referência."""
    p = _pedido(lote)
    p.pop("habilidades")
    pedidos, erros, _ = dmod.validar(lote, {"pedidos": [p]})
    assert erros == [] and pedidos[0]["habilidades"] == []


def test_toda_mensagem_de_erro_nomeia_o_lote_e_a_janela(lote: dict[str, Any]) -> None:
    """Um "pedido inválido" seco obrigaria o maestro a reler o lote inteiro para
    achar o item quebrado, e o retry dirigido deixaria de ser dirigido."""
    _, erros, _ = dmod.validar(
        lote,
        {"pedidos": [_pedido(lote, task_type="inventado"), _pedido(lote, papel="ninguem")]},
    )
    assert len(erros) == 2
    assert all(re.search(r"ped_0001/j\d\d, pedido \d", e) for e in erros)


# ---------------------------------------------------------------------------
# 8. validação — o que é AVISO
# ---------------------------------------------------------------------------


def test_material_sujo_entra_com_AVISO_gravado_no_pedido(lote: dict[str, Any]) -> None:
    """Avisos não bloqueiam porque o material é sujo por natureza — mas ficam
    GRAVADOS, porque um aviso descartado no terminal é um aviso que nunca
    existiu."""
    texto = str(lote["janelas"][0]["texto"])
    # o capítulo sintético tem "ATIVIDADE 1." e o marcador entra pelo recorte
    sujo = "Resposta: espera-se que o estudante cite dois exemplos. " + texto[:200]
    # o recorte tem de continuar existindo na janela, então injetamos na janela
    lote_sujo = json.loads(json.dumps(lote))
    lote_sujo["janelas"][0]["texto"] = sujo + texto
    pedidos, erros, avisos = dmod.validar(
        lote_sujo, {"pedidos": [_pedido(lote, recorte=sujo)]}
    )
    assert erros == []
    assert pedidos and any("Manual do Professor" in a for a in pedidos[0]["avisos"])
    assert any("ped_0001/j01" in a for a in avisos)


def test_conversa_social_num_pedido_de_professor_avisa_mas_passa(
    lote: dict[str, Any],
) -> None:
    """O agente pode estar certo (um professor querendo quebrar o gelo numa turma
    nova), e barrar exigiria uma certeza que não temos."""
    pedidos, erros, _ = dmod.validar(
        lote, {"pedidos": [_pedido(lote, task_type="conversa-social", papel="professor")]}
    )
    assert erros == []
    assert any("combinação rara" in a for a in pedidos[0]["avisos"])


# ---------------------------------------------------------------------------
# 9. a forma da resposta (tolerante) e o import
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("molde", "aviso"),
    [
        # cerca pura: o corte é por linha, e a primeira linha some inteira
        ("```json\n{corpo}\n```", "cerca de código"),
        # prosa em volta (com ou sem cerca dentro): o corte é do primeiro `{`
        # ao último `}`, e é ele que resolve os dois de uma vez
        ("Aqui vai:\n```json\n{corpo}\n```\n", "prosa em volta"),
        ("Segue o resultado do lote.\n{corpo}\nEspero ter ajudado!", "prosa em volta"),
        ("﻿{corpo}", ""),
    ],
)
def test_a_forma_da_resposta_e_tolerante(
    conn: sqlite3.Connection,
    lote: dict[str, Any],
    dp: dmod.DestilacaoPaths,
    molde: str,
    aviso: str,
) -> None:
    """Tolerante na FORMA e estrito no CONTEÚDO — a disciplina do
    ``labeling_io.parse_jsonl``. Um agente que embrulha a resposta em ```json ou
    diz "aqui vai" antes dela não é motivo para jogar fora um lote inteiro.

    O BOM é anunciado: toda tolerância vira aviso, e não silêncio. Um JSON que
    chegou embrulhado é um sinal sobre o agente, e o maestro precisa vê-lo.
    """
    corpo = json.dumps({"lote_id": "ped_0001", "pedidos": [_pedido(lote)]}, ensure_ascii=False)
    rel = dmod.importar(conn, "ped_0001", molde.replace("{corpo}", corpo), dp=dp)
    assert rel["ok"] and rel["n_pedidos"] == 1
    if aviso:
        assert any(aviso in a for a in rel["avisos"])


def test_import_bom_grava_a_linha_e_o_evento(
    conn: sqlite3.Connection, lote: dict[str, Any], dp: dmod.DestilacaoPaths
) -> None:
    rel = _importar(conn, dp, "ped_0001", {"lote_id": "ped_0001", "pedidos": [_pedido(lote)]})
    assert rel["ok"] and rel["gravado"] == {"pedidos": 1, "ja_existiam": 0}

    linha = conn.execute("SELECT * FROM pedidos").fetchone()
    assert linha["lote_id"] == "ped_0001"
    assert linha["status"] == "disponivel"
    assert linha["disciplina"] == "Filosofia" and linha["colecao"] == "Do Seu Jeito"
    assert linha["chave"] == dmod.chave(lote["arquivo_fonte"], linha["recorte"])
    assert dmod.carregar_manifest(dp)["lotes"]["ped_0001"]["status"] == dmod.DONE

    evento = conn.execute(
        "SELECT * FROM eventos WHERE acao = 'pedidos_importados'"
    ).fetchone()
    assert evento is not None
    detalhe = json.loads(evento["detalhe_json"])
    assert detalhe["pedidos"] == 1
    # O evento é trilha de auditoria e NÃO carrega recorte: o texto da editora
    # não sai da tabela `pedidos` nem por uma porta lateral.
    assert linha["recorte"][:40] not in evento["detalhe_json"]


def test_o_MESMO_recorte_por_OUTRO_lote_nao_cria_pedido_novo(
    conn: sqlite3.Connection, material: Path, dp: dmod.DestilacaoPaths
) -> None:
    """A idempotência por chave natural, a de ``geracao.gravar_material``. Quem
    garante é o ``UNIQUE`` da coluna; o ``if`` do ``gravar`` só existe para o
    relatório saber dizer quantos já existiam."""
    dmod.preparar(arquivo=material.name, n=2, dp=dp)
    lote = json.loads(dp.lote("ped_0001").read_text(encoding="utf-8"))
    assert _importar(conn, dp, "ped_0001", {"pedidos": [_pedido(lote)]})["ok"]

    # um lote NOVO, com um manifest novo (cursor em zero), mesmas janelas
    dp2 = dmod.DestilacaoPaths(dp.raiz.parent / "outra-campanha")
    dmod.preparar(arquivo=material.name, n=2, dp=dp2)
    rel = _importar(conn, dp2, "ped_0001", {"pedidos": [_pedido(lote)]})
    assert rel["ok"] and rel["gravado"] == {"pedidos": 0, "ja_existiam": 1}
    assert int(conn.execute("SELECT count(*) AS n FROM pedidos").fetchone()["n"]) == 1


def test_falha_de_validacao_NAO_muda_o_estado_do_lote(
    conn: sqlite3.Connection, lote: dict[str, Any], dp: dmod.DestilacaoPaths
) -> None:
    """O lote segue ``claimed`` e o retry dirigido é a correção normal — a mesma
    regra do ``pf labels submit``. O que muda é a CONTAGEM."""
    rel = _importar(
        conn, dp, "ped_0001", {"pedidos": [_pedido(lote, task_type="inventado")]}
    )
    assert rel["ok"] is False and rel["status"] == dmod.CLAIMED and rel["tentativas"] == 1
    assert int(conn.execute("SELECT count(*) AS n FROM pedidos").fetchone()["n"]) == 0
    # e o retry dirigido funciona sem preparar nada de novo
    assert _importar(conn, dp, "ped_0001", {"pedidos": [_pedido(lote)]})["ok"]


def test_tentativas_esgotadas_mandam_o_lote_para_failed_e_o_revival_o_traz(
    conn: sqlite3.Connection, lote: dict[str, Any], dp: dmod.DestilacaoPaths
) -> None:
    """O teto existe para não travar o ciclo num lote só; reviver é ato
    explícito."""
    ruim = {"pedidos": [_pedido(lote, papel="ninguem")]}
    for _ in range(dmod.max_tentativas()):
        rel = _importar(conn, dp, "ped_0001", ruim)
    assert rel["status"] == dmod.FAILED

    revivido = dmod.reemitir("ped_0001", dp)
    assert revivido["lote_id"] == "ped_0001"
    registro = dmod.carregar_manifest(dp)["lotes"]["ped_0001"]
    assert registro["status"] == dmod.CLAIMED and registro["tentativas"] == 0
    assert _importar(conn, dp, "ped_0001", {"pedidos": [_pedido(lote)]})["ok"]


def test_lote_id_divergente_na_resposta_e_recusado(
    conn: sqlite3.Connection, lote: dict[str, Any], dp: dmod.DestilacaoPaths
) -> None:
    rel = _importar(
        conn, dp, "ped_0001", {"lote_id": "ped_0007", "pedidos": [_pedido(lote)]}
    )
    assert rel["ok"] is False and "arquivo trocado" in rel["erros"][0]


def test_importar_duas_vezes_o_mesmo_lote_e_recusado(
    conn: sqlite3.Connection, lote: dict[str, Any], dp: dmod.DestilacaoPaths
) -> None:
    assert _importar(conn, dp, "ped_0001", {"pedidos": [_pedido(lote)]})["ok"]
    with pytest.raises(ValueError, match="já importado"):
        _importar(conn, dp, "ped_0001", {"pedidos": [_pedido(lote)]})


def test_lote_desconhecido_e_recusado(conn: sqlite3.Connection, dp: dmod.DestilacaoPaths) -> None:
    with pytest.raises(ValueError, match="desconhecido"):
        _importar(conn, dp, "ped_9999", {"pedidos": []})


def test_material_indisponivel_no_import_AVISA_em_vez_de_derrubar(
    conn: sqlite3.Connection, lote: dict[str, Any], dp: dmod.DestilacaoPaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A janela gravada no lote continua sendo prova de que o recorte existiu. O
    que se perde é a checagem de drift — recusar aqui exigiria a pasta montada
    para importar trabalho que já foi feito."""
    monkeypatch.delenv("PF_MATERIAL_DIR", raising=False)
    monkeypatch.setitem(config.settings()["pedidos"], "material_dir", str(dp.raiz / "sumiu"))
    rel = _importar(conn, dp, "ped_0001", {"pedidos": [_pedido(lote)]})
    assert rel["ok"]
    assert any("conferência de drift não rodou" in a for a in rel["avisos"])


# ---------------------------------------------------------------------------
# 10. manifest: TTL, transições e contrato
# ---------------------------------------------------------------------------


def test_claim_orfao_volta_para_pending_depois_do_TTL(
    material: Path, dp: dmod.DestilacaoPaths
) -> None:
    dmod.preparar(arquivo=material.name, n=1, dp=dp)
    manifest = dmod.carregar_manifest(dp)
    manifest["lotes"]["ped_0001"]["claimed_at"] = "2020-01-01T00:00:00Z"
    assert dmod.varrer_orfaos(manifest) == ["ped_0001"]
    assert manifest["lotes"]["ped_0001"]["status"] == dmod.PENDING


def test_o_cursor_NAO_volta_atras_quando_um_claim_expira(
    material: Path, dp: dmod.DestilacaoPaths
) -> None:
    """Recuar o cursor entregaria as mesmas janelas a dois lotes vivos ao mesmo
    tempo. O lote órfão continua revivível pelo ``preparar --lote``."""
    dmod.preparar(arquivo=material.name, n=2, dp=dp)
    manifest = dmod.carregar_manifest(dp)
    antes = manifest["cursores"][material.name]
    manifest["lotes"]["ped_0001"]["claimed_at"] = "2020-01-01T00:00:00Z"
    dmod.varrer_orfaos(manifest)
    assert manifest["cursores"][material.name] == antes


@pytest.mark.parametrize(
    ("origem", "destino", "vale"),
    [
        (dmod.PENDING, dmod.CLAIMED, True),
        (dmod.CLAIMED, dmod.DONE, True),
        (dmod.FAILED, dmod.PENDING, True),
        (dmod.DONE, dmod.PENDING, False),
        (dmod.PENDING, dmod.DONE, False),
        (dmod.DONE, dmod.CLAIMED, False),
    ],
)
def test_a_maquina_de_estados_e_DADO(origem: str, destino: str, vale: bool) -> None:
    assert ((origem, destino) in dmod.TRANSICOES) is vale


def test_contrato_divergente_no_manifest_para_a_campanha(
    dp: dmod.DestilacaoPaths,
) -> None:
    """O formato do lote mudar no meio de uma campanha é decisão humana, não
    algo que o código resolve sozinho."""
    dp.preparar()
    dp.manifest.write_text(
        json.dumps({"contrato": "destilacao@0", "lotes": {}}), encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="contrato"):
        dmod.carregar_manifest(dp)


def test_manifest_ausente_nao_e_erro(tmp_path: Path) -> None:
    """Um ``status`` num clone limpo responde "nenhum lote", que é a verdade."""
    vazio = dmod.carregar_manifest(dmod.DestilacaoPaths(tmp_path / "nao-existe"))
    assert vazio["lotes"] == {} and vazio["cursores"] == {}


# ---------------------------------------------------------------------------
# 11. o painel
# ---------------------------------------------------------------------------


def test_o_painel_conta_a_campanha_e_o_funil(
    conn: sqlite3.Connection, lote: dict[str, Any], dp: dmod.DestilacaoPaths
) -> None:
    _importar(conn, dp, "ped_0001", {"pedidos": [_pedido(lote)]})
    p = dmod.painel(dp)
    assert p["n_lotes"] == 1 and p["por_estado"][dmod.DONE] == 1
    assert p["janelas"] == 2
    assert p["excluidos"] == list(dmod.task_types_excluidos())

    f = dmod.funil(conn)
    assert f["total"] == 1
    assert f["por_status"] == {"disponivel": 1}
    assert f["por_disciplina"] == {"Filosofia": 1}
    assert f["por_papel"] == {"professor": 1}


# ---------------------------------------------------------------------------
# 12. o despacho da skill
# ---------------------------------------------------------------------------


def _runner():
    """O ``rodar_lote.py`` da skill, importado como módulo.

    Ele não é pacote (mora em ``.claude/skills/``), então entra por
    ``importlib``. Vale o incômodo: o casamento entre os placeholders do
    ``despacho.md`` e as substituições do runner **falha em silêncio** — um
    ``<<TASK_TYPES>>`` não preenchido viaja literalmente para o modelo, e o
    resultado é um destilador sem vocabulário nenhum que ainda assim responde.
    """
    import importlib.util

    caminho = (
        Path(__file__).resolve().parents[1]
        / ".claude"
        / "skills"
        / "destilar-pedidos"
        / "rodar_lote.py"
    )
    spec = importlib.util.spec_from_file_location("rodar_lote_destilacao", caminho)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_o_despacho_nao_deixa_placeholder_nenhum_por_preencher(
    lote: dict[str, Any],
) -> None:
    """A prova é sobre um lote REAL, não sobre uma lista de nomes escrita à mão:
    a lista poderia estar certa e o preenchimento errado."""
    runner = _runner()
    prompt, janelas = runner.compor_despacho(lote, None, None)
    assert "<<" not in prompt and ">>" not in prompt
    assert len(janelas) == len(lote["janelas"])


def test_o_despacho_carrega_o_vocabulario_COM_as_definicoes(
    lote: dict[str, Any],
) -> None:
    """Sem a definição o agente classifica pelo nome, e ``redacao-pratica``
    contra ``geracao-criativa`` é justamente a fronteira que a campanha de
    rotulagem do corpus precisou de convenção escrita para resolver."""
    prompt, _ = _runner().compor_despacho(lote, None, None)
    for op in lote["vocabulario"]["task_type"]:
        assert f"`{op['id']}`" in prompt
        assert op["definicao"][:40] in prompt
    for excluido in dmod.task_types_excluidos():
        assert excluido in prompt


def test_o_despacho_repassa_os_limites_do_LOTE_e_nao_de_uma_copia(
    lote: dict[str, Any],
) -> None:
    """Mexer em ``[pedidos] recorte_max_chars`` tem de mudar o que o despacho
    promete, sem editar uma linha da skill."""
    prompt, _ = _runner().compor_despacho(lote, None, None)
    assert str(lote["limites"]["recorte_max_chars"]) in prompt
    assert str(lote["limites"]["max_pedidos_por_janela"]) in prompt


def test_o_retry_dirigido_manda_SO_as_janelas_pedidas(lote: dict[str, Any]) -> None:
    runner = _runner()
    prompt, janelas = runner.compor_despacho(lote, ["j02"], "recorte com espaço no fim")
    assert [j["janela_id"] for j in janelas] == ["j02"]
    assert "RETRY" in prompt and "espaço no fim" in prompt
    with pytest.raises(SystemExit, match="fora do lote"):
        runner.compor_despacho(lote, ["j99"], None)


def test_a_nota_do_retry_vale_SEM_janelas_tambem(lote: dict[str, Any]) -> None:
    """Uma escalada de modelo sobre o lote inteiro também é um retry, e a lição
    da tentativa anterior é o que ela tem a acrescentar. Uma flag que o usuário
    passa e o programa ignora em silêncio é pior que uma flag que não existe."""
    runner = _runner()
    prompt, janelas = runner.compor_despacho(lote, None, "achatou a quebra de linha")
    assert len(janelas) == len(lote["janelas"])
    assert "RETRY" in prompt and "achatou a quebra de linha" in prompt
    # e sem nada, nenhum bloco de retry aparece
    limpo, _ = runner.compor_despacho(lote, None, None)
    assert "RETRY" not in limpo


def test_o_extrator_e_tolerante_na_forma_e_NAO_toca_no_conteudo() -> None:
    """Ao contrário do runner da rotulagem, este NÃO re-serializa: um dos campos
    é uma cópia verbatim que a validação confere caractere a caractere, e
    qualquer passe de normalização poderia ser a diferença entre conferir e
    não conferir."""
    runner = _runner()
    recorte = "linha um\ncom  espaço  duplo e hífen-\nquebrado"
    saida = (
        "Aqui vão os pedidos:\n"
        "```json\n"
        + json.dumps({"janela_id": "j01", "recorte": recorte}, ensure_ascii=False)
        + "\nlinha quebrada que não é json\n"
        + json.dumps({"sem": "janela_id"}, ensure_ascii=False)
        + "\n```\n"
    )
    pedidos = runner.extrair_pedidos(saida)
    assert len(pedidos) == 1
    assert pedidos[0]["recorte"] == recorte


def test_a_campanha_nao_toca_o_corpus(material: Path, dp: dmod.DestilacaoPaths) -> None:
    """A única das três campanhas do repositório que roda sem ``prompts.sqlite``:
    o insumo são arquivos e o destino é o ``annotate.sqlite``."""
    import inspect

    # A prova é a AUSÊNCIA das duas coisas que uma leitura do corpus exige aqui:
    # uma conexão read-only e o parâmetro que a carrega. (Varrer o texto do
    # módulo por "prompts.sqlite" não serviria — a docstring cita o nome
    # justamente para dizer que não o usa.)
    corpo = "\n".join(
        linha
        for linha in inspect.getsource(dmod).splitlines()
        if not linha.lstrip().startswith(("#", '"', "'"))
    )
    assert "readonly" not in corpo
    assert "conn_corpus" not in corpo
    # e as duas operações da CLI não recebem conexão de corpus nenhuma
    assert set(inspect.signature(dmod.preparar).parameters) == {"arquivo", "n", "dp"}
    assert "corpus" not in set(inspect.signature(dmod.importar).parameters)


def test_pf_material_dir_vence_o_settings(monkeypatch, tmp_path):
    """A variável de ambiente é o caminho recomendado, e ela VENCE o arquivo.

    O ``settings.toml`` versionado deixa ``material_dir`` vazio de propósito
    (R13: caminho pessoal em arquivo rastreado volta ao HEAD no primeiro commit
    distraído). Se a chave do arquivo vencesse, a variável seria decoração.
    """
    from prompt_factory import config
    from prompt_factory.annotate import destilacao as dmod

    monkeypatch.setitem(config.settings()["pedidos"], "material_dir", str(tmp_path / "do-arquivo"))
    monkeypatch.setenv("PF_MATERIAL_DIR", str(tmp_path / "da-variavel"))
    assert dmod.material_dir() == tmp_path / "da-variavel"

    monkeypatch.setenv("PF_MATERIAL_DIR", "   ")
    assert dmod.material_dir() == tmp_path / "do-arquivo", "variável em branco cai no arquivo"
