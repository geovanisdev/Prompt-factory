"""Testes da cadeia de normalização (M1).

Regra dos casos: o que TEM de colidir no hash e o que NÃO PODE colidir. Cada
falso positivo aqui vira prompt distinto apagado no dedup exato (s04); cada
falso negativo vira duplicata no banco.
"""

from __future__ import annotations

import pytest

from prompt_factory.textnorm import norm_display, norm_for_hash

# Caracteres escritos como escape porque são invisíveis no editor.
BOM = "\ufeff"
ZWSP = "\u200b"
NUL = "\x00"
BEL = "\x07"


def test_hash_accents_fold() -> None:
    # Acento, caixa e pontuação terminal somem; o resto continua igual.
    assert norm_for_hash("Olá!!") == norm_for_hash("ola") == "ola"
    assert norm_for_hash("coração") == norm_for_hash("coracao") == "coracao"
    assert norm_for_hash("CAFÉ") == norm_for_hash("café") == "cafe"


def test_hash_casefold_eszett() -> None:
    # casefold() vai além de lower(): o eszett alemão vira "ss".
    assert norm_for_hash("weiß") == norm_for_hash("weiss") == "weiss"


def test_hash_nfkc_compat() -> None:
    # U+FB01 = ligadura "fi"; U+FF21.. = maiusculas de largura total.
    assert norm_for_hash("ﬁle") == "file"
    assert norm_for_hash("\uff21\uff22\uff23") == "abc"
    # NBSP (\xa0) vira espaço comum já no NFKC.
    assert norm_for_hash("bom\xa0dia") == "bom dia"


def test_hash_whitespace_and_terminal_punct() -> None:
    assert norm_for_hash("  Como   fazer\n\nbolo?  ") == "como fazer bolo"
    assert norm_for_hash("Reunião às 15h…") == "reuniao as 15h"


def test_hash_no_collision_distinct() -> None:
    assert norm_for_hash("bolo de cenoura") != norm_for_hash("bolo de chocolate")
    assert norm_for_hash("bolo") != norm_for_hash("bolos")  # sem stemming, de propósito
    assert norm_for_hash("😀 Olá") != norm_for_hash("Olá")


def test_hash_only_punct_is_empty() -> None:
    # Aviso ao s04: chave vazia NÃO agrupa duplicatas exatas.
    assert norm_for_hash("!!!") == ""
    assert norm_for_hash("") == ""
    assert norm_for_hash("   ") == ""


def test_display_crlf_collapse() -> None:
    assert norm_display("a\r\nb\r\n\r\n\r\n\r\nc") == "a\nb\n\nc"
    assert norm_display("a\rb") == "a\nb"


def test_display_removes_bom_zero_width_controls() -> None:
    assert norm_display(f"{BOM}Olá{ZWSP} mundo{NUL}{BEL}") == "Olá mundo"


def test_display_preserves_tab_and_single_newlines() -> None:
    # O display é conservador: acento, caixa, tab e parágrafo continuam lá.
    assert norm_display("linha 1\n\tindentada\nlinha 3") == "linha 1\n\tindentada\nlinha 3"
    assert norm_display("  Olá, Mundo!  ") == "Olá, Mundo!"


@pytest.mark.parametrize(
    "texto",
    [
        "Olá!!",
        "  Como   fazer\n\nbolo?  ",
        "a\r\nb\r\n\r\n\r\n\r\nc",
        f"{BOM}Reunião às 15h…{ZWSP}",
        "",
        "!!!",
    ],
)
def test_idempotent(texto: str) -> None:
    display = norm_display(texto)
    assert norm_display(display) == display
    chave = norm_for_hash(display)
    assert norm_for_hash(chave) == chave
