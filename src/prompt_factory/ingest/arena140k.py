"""arena140k — `lmarena-ai/arena-human-preference-140k`, prompts pt do split train.

Conversas reais da LMArena (135.634 batalhas). Só o **primeiro turno do usuário**
de `full_conversation` entra; os prompts são CC-BY-4.0.

Duas pegadinhas verificadas contra o Hub:

* `language` usa código MINÚSCULO (`"pt"`, `"en"`, `"de"`), NÃO `"Portuguese"`
  como o aya. O matching aqui é case-insensitive sobre um conjunto de apelidos.
* `content` de cada mensagem é uma LISTA de itens `{type, text, image, mimeType}`
  no schema arrow. `content_to_text` continua tratando `str` e `dict` porque o
  formato já mudou uma vez e é barato ser defensivo.
* `full_conversation` NÃO é uma lista de mensagens `{role, content}` (isso é o
  formato de `conversation_a`/`conversation_b`): cada item é um TURNO agrupado,
  `{"user": {...}, "model_side_a": {...}, "model_side_b": {...}}`. Assumir a
  forma plana faz o extractor devolver vazio nas 135k linhas, silenciosamente.

`full_conversation` inclui o contexto dos `evaluation_order` anteriores da mesma
sessão, então o prompt de abertura se REPETE entre linhas. Não deduplicamos aqui
(o s04 faz isso, com contagem): `distintas < linhas` no `pf report raw` é o
comportamento esperado desta fonte.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from typing import Any

from .base import column_values, make_row, require_columns, to_iso

SOURCE = "arena140k"
SPLIT = "train"
#: Apelidos de português aceitos (comparados em minúsculas).
PT = frozenset({"pt", "pt-br", "por", "portuguese"})
NEEDED = (
    "id",
    "language",
    "full_conversation",
    "timestamp",
    "is_code",
    "category_tag",
    "evaluation_session_id",
    "evaluation_order",
)


def content_to_text(content: Any) -> str:
    """`content` de uma mensagem -> texto. Itens sem texto (imagem) somem."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        texto = content.get("text")
        return texto if isinstance(texto, str) else ""
    if isinstance(content, list):
        partes: list[str] = []
        for item in content:
            if isinstance(item, str):
                partes.append(item)
            elif isinstance(item, dict):
                texto = item.get("text")
                if isinstance(texto, str) and texto:
                    partes.append(texto)
        return "\n".join(partes)
    return ""


def user_message(turno: Any) -> dict[str, Any] | None:
    """Mensagem do usuário dentro de um item de conversa, ou `None`.

    Duas formas convivem no dataset e as duas aparecem aqui:

    * **turno agrupado** — o que `full_conversation` usa de verdade:
      ``{"user": {role, content}, "model_side_a": {...}, "model_side_b": {...}}``;
    * **mensagem plana** — o que `conversation_a`/`conversation_b` usam:
      ``{"role": "user", "content": ...}``.
    """
    if not isinstance(turno, dict):
        return None
    interna = turno.get("user")
    if isinstance(interna, dict):
        return interna
    if turno.get("role") == "user":
        return turno
    return None


def first_user_text(full_conversation: Any) -> str:
    """Texto do PRIMEIRO turno do usuário. Não concatena turnos."""
    for turno in full_conversation or []:
        msg = user_message(turno)
        if msg is None:
            continue
        return content_to_text(msg.get("content"))
    return ""


def iter_rows(cfg: dict[str, Any], max_rows: int | None = None) -> Iterator[dict[str, Any]]:
    from datasets import load_dataset

    ds = load_dataset(cfg["hf_id"], split=SPLIT)
    require_columns(ds.features, NEEDED, SOURCE)

    idiomas = column_values(ds, "language")
    manter = [i for i, lang in enumerate(idiomas) if (lang or "").lower() in PT]
    print(f"[{SOURCE}] {len(idiomas)} batalhas no split {SPLIT}, {len(manter)} em pt")
    if not manter:
        raise SystemExit(
            f"[{SOURCE}] nenhuma linha em pt (esperado ~1.684). "
            f"Idiomas vistos (top 20): {Counter(idiomas).most_common(20)}"
        )

    sub = ds.select_columns(list(NEEDED)).select(manter)
    n = 0
    n_sem_texto = 0
    for rec in sub:
        texto = first_user_text(rec["full_conversation"])
        if not texto:
            n_sem_texto += 1
            continue
        yield make_row(
            source=SOURCE,
            source_id=rec["id"],
            source_split=SPLIT,
            text_raw=texto,
            lang_source=rec["language"] or "",
            created_ts=to_iso(rec["timestamp"]),
            meta={
                "is_code": rec["is_code"],
                "category_tag": rec["category_tag"],
                "timestamp": to_iso(rec["timestamp"]),
                "evaluation_session_id": rec["evaluation_session_id"],
                "evaluation_order": rec["evaluation_order"],
            },
        )
        n += 1
        if max_rows is not None and n >= max_rows:
            return
    if n_sem_texto:
        print(f"[{SOURCE}] {n_sem_texto} batalhas pt sem turno de usuario com texto")
