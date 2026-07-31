"""Classificador pt-BR x pt-PT — 32 casos curados + as guardas dos padrões.

Os casos cobrem as sete famílias nos dois sentidos, os empates, o piso de
``min_score`` e a margem de ``min_conf``. Os quatro últimos blocos testam as
guardas que já custaram falso positivo em revisão: "a partir de" não é
progressivo, "segunda-feira"/"dia-a-dia" não são ênclise, "tu" sozinho não é
português europeu e "gelado"/"time" só contam com determinante.
"""

from __future__ import annotations

import pytest

from prompt_factory.variant_ptbr import classify_variant, placar, prior_pais

BR = "pt-BR"
PT = "pt-PT"
INDEF = "pt-indef"

#: (id, texto, país, variante esperada)
CASOS: tuple[tuple[str, str, str, str], ...] = (
    # --- S1 progressivo ---------------------------------------------------
    ("s1-br", "Estou fazendo um bolo e estou pensando em você.", "", BR),
    ("s1-pt", "Estou a fazer um bolo e estou a pensar no assunto.", "", PT),
    ("s1-uma-pista-so", "Estou a fazer um bolo.", "", INDEF),
    ("s1-guarda-partir", "Estou a partir de casa às oito da manhã.", "", INDEF),
    # --- S2 segunda pessoa -------------------------------------------------
    ("s2-pt", "Tu podes ajudar-me com o texto?", "", PT),
    ("s2-pt-verbos", "Sabes se tens tempo e queres falar comigo?", "", PT),
    ("s2-voce-sozinho", "Você pode me ajudar com o texto?", "", INDEF),
    ("s2-tu-gaucho", "Tu foi no mercado ontem, tchê?", "", INDEF),
    # --- S3 ênclise --------------------------------------------------------
    ("s3-enclise", "Ele chama-se João e disse-me a verdade.", "", PT),
    ("s3-guarda-feira", "Na segunda-feira eu vou ao mercado.", "", INDEF),
    ("s3-guarda-dia-a-dia", "É um problema do dia-a-dia da empresa.", "", INDEF),
    # --- S4 coloquialismos -------------------------------------------------
    ("s4-br", "Tá tudo bem, né? Vou pra casa.", "", BR),
    ("s4-pt", "Isso é fixe, malta, vamos lá.", "", PT),
    ("s4-br-a-gente", "A gente vai pro trabalho de trem.", "", BR),
    # --- S5 léxico ---------------------------------------------------------
    ("s5-onibus", "Preciso de um ônibus para o aeroporto.", "", BR),
    ("s5-autocarro", "Preciso de um autocarro para o aeroporto.", "", PT),
    ("s5-comboio", "Vamos apanhar o comboio para o trabalho.", "", PT),
    ("s5-banheiro", "Onde fica o banheiro?", "", BR),
    ("s5-casa-de-banho", "Onde fica a casa de banho?", "", PT),
    ("s5-informatica-br", "Baixe o arquivo e digite a senha do usuário.", "", BR),
    (
        "s5-informatica-pt",
        "Descarregue o ficheiro e escreva a palavra-passe do utilizador.",
        "",
        PT,
    ),
    ("s5-empate", "Vou pegar o ônibus e depois usar o telemóvel.", "", INDEF),
    # --- S6 ortografia -----------------------------------------------------
    ("s6-br", "O gênero do texto e o registro do contato.", "", BR),
    ("s6-pt", "O género do texto e o registo do contacto.", "", PT),
    ("s6-aspectos-br", "Aspectos econômicos do fenômeno.", "", BR),
    ("s6-aspetos-pt", "Aspetos económicos do fenómeno.", "", PT),
    # --- S7 prior de país --------------------------------------------------
    ("s7-sem-pais", "Escreva um texto sobre a história do país.", "", INDEF),
    ("s7-brasil", "Escreva um texto sobre a história do país.", "Brazil", BR),
    ("s7-portugal", "Escreva um texto sobre a história do país.", "Portugal", PT),
    ("s7-angola-fraco", "Escreva um texto sobre a história do país.", "Angola", INDEF),
    # --- conflito entre texto e país ---------------------------------------
    ("conflito-empate", "Preciso de um autocarro.", "Brazil", INDEF),
    (
        "conflito-texto-vence",
        "Vou de autocarro para o emprego e falo ao telemóvel.",
        "Brazil",
        PT,
    ),
)


@pytest.mark.parametrize(
    ("texto", "pais", "esperado"),
    [(t, p, e) for _, t, p, e in CASOS],
    ids=[c[0] for c in CASOS],
)
def test_casos_curados(texto: str, pais: str, esperado: str) -> None:
    variante, _conf = classify_variant(texto, pais)
    assert variante == esperado, placar(texto, pais)


def test_sao_32_casos() -> None:
    # O número não é decoração: cada caso cobre uma família, uma guarda ou um
    # empate. Cortar um caso "que parece repetido" tira cobertura de verdade.
    assert len(CASOS) == 32


# ---------------------------------------------------------------------------
# confiança
# ---------------------------------------------------------------------------


def test_conf_maxima_quando_so_um_lado_pontua() -> None:
    variante, conf = classify_variant("Preciso de um ônibus para o aeroporto.")
    assert (variante, conf) == ("pt-BR", 1.0)


def test_conf_maxima_no_bloco_de_ortografia() -> None:
    variante, conf = classify_variant("O género do texto e o registo do contacto.")
    assert (variante, conf) == ("pt-PT", 1.0)


def test_empate_devolve_conf_zero() -> None:
    assert classify_variant("Vou pegar o ônibus e depois usar o telemóvel.") == (
        "pt-indef",
        0.0,
    )


def test_prior_de_palop_sozinho_nao_decide() -> None:
    # 1 ponto < min_score = 2: Angola/Moçambique inclinam, não decidem.
    assert classify_variant("Texto neutro qualquer.", "Angola") == ("pt-indef", 0.0)
    assert prior_pais("Angola") == (0.0, 1.0)
    assert prior_pais("Brasil") == (2.0, 0.0)
    assert prior_pais("PT") == (0.0, 2.0)
    assert prior_pais("") == (0.0, 0.0)
    assert prior_pais("France") == (0.0, 0.0)


def test_pais_contra_texto_empata_em_pt_indef() -> None:
    assert classify_variant("Preciso de um autocarro.", "Brazil") == ("pt-indef", 0.0)


def test_margem_baixa_ainda_decide_acima_de_min_conf() -> None:
    variante, conf = classify_variant(
        "Vou de autocarro para o emprego e falo ao telemóvel.", "Brazil"
    )
    assert variante == "pt-PT"
    assert conf == pytest.approx(1 / 3, abs=1e-4)


def test_margem_baixa_no_sentido_br() -> None:
    variante, conf = classify_variant("O celular está na geladeira.", "Portugal")
    assert variante == "pt-BR"
    assert conf == pytest.approx(1 / 3, abs=1e-4)


# ---------------------------------------------------------------------------
# guardas dos padrões
# ---------------------------------------------------------------------------


def test_guarda_a_partir_de_nao_e_progressivo() -> None:
    assert placar("Estou a partir de casa.")["S1"] == (0.0, 0.0)
    assert placar("Estou a sair de casa.")["S1"] == (0.0, 1.5)


def test_guarda_enclise_ignora_dias_da_semana_e_reduplicacao() -> None:
    for texto in ("segunda-feira", "terça-feira", "dia-a-dia", "bem-te-vi", "couve-flor"):
        assert placar(texto)["S3"] == (0.0, 0.0), texto
    assert placar("diga-me a verdade")["S3"] == (0.0, 1.5)


def test_guarda_gelado_e_time_exigem_determinante() -> None:
    assert placar("O chá está gelado.")["S5"] == (0.0, 0.0)
    assert placar("Comprei um gelado de morango.")["S5"] == (0.0, 2.0)
    assert placar("O time jogou bem nesta prova.")["S5"] == (2.0, 0.0)
    # Sem determinante não pontua: "time" aparece solto em texto técnico
    # ("time out", "time series") e não é pista de variante.
    assert placar("Configure time out de 30 segundos.")["S5"] == (0.0, 0.0)


def test_nfc_e_casefold_nao_dependem_da_forma_de_entrada() -> None:
    # "ônibus" com o circunflexo DECOMPOSTO (o+U+0302) tem de casar igual: é o
    # que o norm_display deixa passar.
    decomposto = "O ÔNIBUS chegou."
    assert classify_variant(decomposto)[0] == "pt-BR"


def test_ingles_nao_pontua() -> None:
    assert classify_variant("Hello, this is an English sentence.") == ("pt-indef", 0.0)
