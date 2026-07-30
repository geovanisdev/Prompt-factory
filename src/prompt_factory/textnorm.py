"""Normalização de texto — a cadeia canônica do Prompt Factory.

Duas funções, dois propósitos distintos:

``norm_display`` limpa o texto **que será exibido e exportado**: conserta
quebras de linha, remove controles e caracteres invisíveis, colapsa parágrafos
vazios em excesso. É conservadora — preserva acentuação, caixa e pontuação.

``norm_for_hash`` produz a **chave de comparação** usada no dedup exato (s04) e
na coluna ``hash_norm``. É agressiva: dobra acentos, caixa, espaços e pontuação
terminal, então "Olá!!" e "ola" colidem de propósito.

Cadeia canônica (todo estágio que cria linha usa exatamente esta ordem)::

    text = norm_display(text_raw)
    hash_norm = sha256(norm_for_hash(text).encode("utf-8")).hexdigest()

Repare que ``norm_for_hash`` roda SOBRE o texto já normalizado para exibição,
nunca sobre o cru — do contrário duas linhas idênticas na tela poderiam gerar
hashes diferentes.

Avisos para quem consome:

* ``norm_for_hash("!!!") == ""``. Prompts só de pontuação (e só de emoji com
  ZWJ) caem numa chave vazia; o s04 NÃO deve agrupar chaves vazias como
  duplicatas exatas — trate-as como lixo (quality=1) ou agrupe pelo texto cru.
* Emoji composto degrada: ``norm_display`` remove o ZWJ (U+200D) que une
  sequências como a família 👨‍👩‍👧, então elas viram os emojis soltos. É um
  trade-off aceito para eliminar invisíveis usados em spam; como o hash roda
  sobre o display, os dois lados continuam consistentes entre si.
* Ambas são idempotentes: ``f(f(x)) == f(x)``.
"""

from __future__ import annotations

import re
import unicodedata

# ZWSP, ZWNJ, ZWJ, WORD JOINER e ZWNBSP/BOM. Escritos como escapes de propósito:
# no código-fonte estes caracteres seriam literalmente invisíveis.
_ZERO_WIDTH = "\u200b\u200c\u200d\u2060\ufeff"
_ZERO_WIDTH_TABLE = dict.fromkeys(map(ord, _ZERO_WIDTH), None)

_RE_3PLUS_NEWLINES = re.compile(r"\n{3,}")
_RE_WHITESPACE = re.compile(r"\s+")
_RE_TERMINAL_PUNCT = re.compile(r"[.!?…]+$")  # ponto, exclamação, interrogação, reticências


def norm_display(text: str) -> str:
    """Normaliza para exibição/exportação, preservando o conteúdo visível.

    CRLF/CR viram LF; invisíveis de largura zero e caracteres de controle
    (exceto ``\\n`` e ``\\t``) somem; 3+ quebras seguidas viram uma linha em
    branco só; sobra é aparada nas pontas.
    """
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = t.translate(_ZERO_WIDTH_TABLE)
    t = "".join(ch for ch in t if ch in "\n\t" or unicodedata.category(ch) != "Cc")
    t = _RE_3PLUS_NEWLINES.sub("\n\n", t)
    return t.strip()


def norm_for_hash(text: str) -> str:
    """Reduz o texto à chave de comparação do dedup exato.

    Ordem fixa (mexer aqui invalida todo ``hash_norm`` já gravado):
    NFKC → casefold → NFD → remove marcas de combinação → colapsa espaços →
    strip → remove pontuação terminal.

    O NFKC vem primeiro porque é ele que converte NBSP em espaço comum, formas
    de largura total em ASCII e ligaduras em letras soltas; só depois o casefold
    (que é mais forte que ``lower()``: "weiß" vira "weiss") e a remoção de
    acentos.
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = t.casefold()
    t = unicodedata.normalize("NFD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = _RE_WHITESPACE.sub(" ", t)
    t = t.strip()
    t = _RE_TERMINAL_PUNCT.sub("", t).rstrip()
    return t


__all__ = ["norm_display", "norm_for_hash"]
