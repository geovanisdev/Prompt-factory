"""Parsers dos ingesters — testes OFFLINE (nenhum acesso a rede).

As funções aqui são puras de propósito: extrair texto de um transcript ou de um
`content` aninhado é onde as fontes mudam de formato sem avisar, e é o único
lugar do M2 que dá para travar com teste rápido.
"""

from __future__ import annotations

import pytest

from prompt_factory.ingest.arena140k import content_to_text, first_user_text
from prompt_factory.ingest.hh_rlhf import first_human_turn

# ---------------------------------------------------------------------------
# hh_rlhf.first_human_turn
# ---------------------------------------------------------------------------


def test_first_human_turn_transcript_completo() -> None:
    chosen = "\n\nHuman: Como faço X?\n\nAssistant: Assim.\n\nHuman: e Y?\n\nAssistant: Assado."
    assert first_human_turn(chosen) == "Como faço X?"


def test_first_human_turn_sem_newlines_iniciais() -> None:
    assert first_human_turn("Human: sem newlines iniciais\n\nAssistant: ok") == (
        "sem newlines iniciais"
    )


def test_first_human_turn_preserva_quebras_internas() -> None:
    chosen = "\n\nHuman: linha 1\nlinha 2\n\nAssistant: ok"
    assert first_human_turn(chosen) == "linha 1\nlinha 2"


def test_first_human_turn_sem_resposta_do_assistente() -> None:
    # Sem o separador, o turno é o transcript inteiro (sem prefixo, stripped).
    assert first_human_turn("\n\nHuman: só a pergunta  ") == "só a pergunta"


def test_first_human_turn_turno_vazio_vira_none() -> None:
    assert first_human_turn("\n\nHuman: \n\nAssistant: oi") is None


@pytest.mark.parametrize("valor", ["", None, "   \n\n  "])
def test_first_human_turn_entrada_vazia(valor: str | None) -> None:
    assert first_human_turn(valor) is None


# ---------------------------------------------------------------------------
# arena140k.content_to_text
# ---------------------------------------------------------------------------


def test_content_to_text_string_passa_intocada() -> None:
    assert content_to_text("um prompt qualquer") == "um prompt qualquer"


def test_content_to_text_dict_com_type() -> None:
    assert content_to_text({"type": "text", "text": "oi"}) == "oi"


def test_content_to_text_dict_sem_type() -> None:
    assert content_to_text({"text": "oi"}) == "oi"


def test_content_to_text_lista_mista_ignora_imagem() -> None:
    content = [
        {"type": "text", "text": "a"},
        {"type": "image", "image": "b64...", "mimeType": "image/png", "text": None},
        {"type": "text", "text": "b"},
    ]
    assert content_to_text(content) == "a\nb"


def test_content_to_text_lista_de_strings() -> None:
    assert content_to_text(["a", "b"]) == "a\nb"


@pytest.mark.parametrize(
    "content",
    [None, {}, [], [{"type": "image", "image": "b64...", "text": None}], 42],
)
def test_content_to_text_vazio(content: object) -> None:
    assert content_to_text(content) == ""


# ---------------------------------------------------------------------------
# arena140k.first_user_text
# ---------------------------------------------------------------------------


def test_first_user_text_pega_o_primeiro_user() -> None:
    conversa = [
        {"role": "system", "content": "seja útil"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    # Primeiro turno de usuário, nunca a concatenação dos dois.
    assert first_user_text(conversa) == "u1"


def test_first_user_text_content_em_lista() -> None:
    conversa = [{"role": "user", "content": [{"type": "text", "text": "olá"}]}]
    assert first_user_text(conversa) == "olá"


def test_first_user_text_turno_agrupado_do_full_conversation() -> None:
    # Forma REAL de `full_conversation`: turno agrupado, não mensagem plana.
    conversa = [
        {
            "user": {"role": "user", "content": [{"type": "text", "text": "Compare X com Y"}]},
            "model_side_a": {"role": "assistant", "content": [{"type": "text", "text": "resposta A"}]},
            "model_side_b": {"role": "assistant", "content": [{"type": "text", "text": "resposta B"}]},
        },
        {"user": {"role": "user", "content": [{"type": "text", "text": "segundo turno"}]}},
    ]
    assert first_user_text(conversa) == "Compare X com Y"


def test_first_user_text_pula_turno_sem_usuario() -> None:
    conversa = [
        {"user": None, "model_side_a": {"role": "assistant", "content": "só modelo"}},
        {"user": {"role": "user", "content": "aqui"}},
    ]
    assert first_user_text(conversa) == "aqui"


@pytest.mark.parametrize("conversa", [None, [], [{"role": "assistant", "content": "a"}], ["lixo"]])
def test_first_user_text_sem_usuario(conversa: object) -> None:
    assert first_user_text(conversa) == ""
