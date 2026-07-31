"""Remoção de PII — positivos, negativos e a ordem das substituições.

Os negativos são a parte séria do arquivo: substituir errado destrói o prompt.
Número de processo judicial, valor em reais e índice de array têm formas que
lembram CPF/telefone e **não** podem casar.
"""

from __future__ import annotations

import pytest

from prompt_factory.pii import PII_TIPOS, PLACEHOLDERS, cpf_dv_ok, scrub

#: CPF sintético com dígito verificador válido (não pertence a ninguém: é o
#: exemplo clássico usado em documentação da Receita).
CPF_VALIDO = "52998224725"

POSITIVOS: tuple[tuple[str, str, dict[str, int]], ...] = (
    (
        "e-mail simples",
        "meu email é joao.silva+spam@empresa.com.br, responda",
        {"email": 1},
    ),
    (
        "dois e-mails",
        "escreva para a@b.com e para c@d.org",
        {"email": 2},
    ),
    (
        "url com credencial",
        "acesse https://user:senha123@intranet.local/rel e veja",
        {"url": 1},
    ),
    (
        "url com usuário sem senha",
        "ftp://admin@10.0.0.1/backup tem o arquivo",
        {"url": 1},
    ),
    ("cnpj formatado", "CNPJ 12.345.678/0001-95 da empresa", {"cnpj": 1}),
    ("cpf formatado", "CPF 123.456.789-09 dele", {"cpf": 1}),
    ("cpf cru com dv válido", f"cpf {CPF_VALIDO} confere", {"cpf": 1}),
    ("telefone internacional", "ligue +55 11 91234-5678 agora", {"telefone": 1}),
    ("telefone com ddd", "ligue (11) 91234-5678 agora", {"telefone": 1}),
    ("telefone fixo com ddd", "atende em (21) 3222-1000", {"telefone": 1}),
    ("móvel português", "o meu telemóvel é 912 345 678", {"telefone": 1}),
    (
        "telefone solto com contexto",
        "meu telefone é 11 91234-5678",
        {"telefone": 1},
    ),
)

NEGATIVOS: tuple[tuple[str, str], ...] = (
    ("url comum fica", "veja https://exemplo.com/artigo?x=1 sobre isso"),
    ("cpf cru com dv inválido", "id 52998224726 inválido"),
    ("cpf cru de dígitos repetidos", "id 11111111111 inválido"),
    ("número de processo", "processo 0001234-56.2024.8.26.0100 no TJ"),
    ("valor em reais", "lucro de R$ 1.234.567,89 no trimestre"),
    ("índice de array", "array[11] = 3456 no código"),
    ("telefone solto sem contexto", "o item 11 91234-5678 da planilha"),
    ("sequência de zeros", "ligue para 00 00000-0000"),
    ("cep não é PII", "moro no CEP 01310-100, São Paulo"),
    ("data", "a reunião foi em 12/03/2024 às 15h"),
    ("versão", "atualize para a versão 1.2.3-beta"),
)


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [(t, e) for _, t, e in POSITIVOS],
    ids=[c[0] for c in POSITIVOS],
)
def test_positivos(texto: str, esperado: dict[str, int]) -> None:
    limpo, contagens = scrub(texto)
    assert contagens == esperado
    for tipo in esperado:
        assert PLACEHOLDERS[tipo] in limpo


@pytest.mark.parametrize("texto", [t for _, t in NEGATIVOS], ids=[c[0] for c in NEGATIVOS])
def test_negativos_nao_mudam_o_texto(texto: str) -> None:
    limpo, contagens = scrub(texto)
    assert contagens == {}
    assert limpo == texto


def test_url_com_credencial_vem_antes_do_email() -> None:
    """A ordem importa: e-mail primeiro comeria o ``user:senha@host`` e deixaria
    o resto da URL (com o caminho privado) no texto."""
    limpo, contagens = scrub("veja https://ana:1234@servidor.interno/rel.pdf hoje")
    assert contagens == {"url": 1}
    assert limpo == "veja [URL] hoje"
    assert "ana" not in limpo and "1234" not in limpo


def test_dv_do_cpf() -> None:
    assert cpf_dv_ok(CPF_VALIDO)
    assert not cpf_dv_ok("52998224726")
    assert not cpf_dv_ok("00000000000")
    assert not cpf_dv_ok("1234567890")  # 10 dígitos
    assert not cpf_dv_ok("abcdefghijk")


def test_cpf_formatado_nao_valida_dv() -> None:
    # A forma pontuada já é conclusiva; exigir DV perderia CPF digitado errado,
    # que continua sendo dado pessoal.
    limpo, contagens = scrub("CPF 111.222.333-44")
    assert contagens == {"cpf": 1}
    assert limpo == "CPF [CPF]"


def test_texto_vazio_e_sem_pii() -> None:
    assert scrub("") == ("", {})
    assert scrub("um prompt qualquer sem nada") == (
        "um prompt qualquer sem nada",
        {},
    )


def test_todos_os_tipos_tem_placeholder() -> None:
    assert set(PII_TIPOS) == set(PLACEHOLDERS)


def test_varias_pii_na_mesma_linha() -> None:
    limpo, contagens = scrub(
        f"contato: a@b.com, CPF {CPF_VALIDO}, CNPJ 12.345.678/0001-95, "
        "telefone (11) 91234-5678"
    )
    assert contagens == {"email": 1, "cpf": 1, "cnpj": 1, "telefone": 1}
    assert "@" not in limpo
    assert limpo.count("[") == 4
