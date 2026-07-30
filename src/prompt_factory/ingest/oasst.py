"""oasst — raízes em português de `OpenAssistant/oasst1` UNIÃO `oasst2`.

Só interessa a **raiz** da árvore de conversa (`role == "prompter"` e
`parent_id is None`): é o prompt original de uma pessoa. Respostas e turnos
seguintes não entram.

oasst1 é praticamente subconjunto de oasst2, e os dois compartilham
`message_id` — a união é por id, com oasst1 lido primeiro (ganha o registro mais
antigo). Em pt a fonte usa `"pt-BR"`; `"pt"` também é aceito. Qualquer outro
código `pt*` (ex.: `pt-PT`) é REPORTADO no fim, nunca incluído em silêncio.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from typing import Any

from .base import make_row, require_columns, to_iso

SOURCE = "oasst"
SPLITS = ("train", "validation")
PT = ("pt-BR", "pt")
NEEDED = (
    "message_id",
    "parent_id",
    "text",
    "role",
    "lang",
    "deleted",
    "created_date",
    "review_count",
    "review_result",
    "tree_state",
)


def iter_rows(cfg: dict[str, Any], max_rows: int | None = None) -> Iterator[dict[str, Any]]:
    from datasets import load_dataset

    hf_ids = list(cfg.get("hf_ids") or [cfg["hf_id"]])
    vistos: set[str] = set()
    raizes_pt: set[str] = set()
    codigos_pt: Counter[str] = Counter()
    n = 0

    for hf_id in hf_ids:
        apelido = hf_id.rsplit("/", 1)[-1]
        for split in SPLITS:
            ds = load_dataset(hf_id, split=split)
            require_columns(ds.features, NEEDED, SOURCE)
            for rec in ds.select_columns(list(NEEDED)):
                if rec["role"] != "prompter" or rec["parent_id"] is not None or rec["deleted"]:
                    continue
                lang = rec["lang"] or ""
                message_id = rec["message_id"] or ""
                if lang.lower().startswith("pt") and message_id not in raizes_pt:
                    # Conta cada raiz uma vez só: oasst1 e oasst2 se sobrepõem.
                    raizes_pt.add(message_id)
                    codigos_pt[lang] += 1
                if lang not in PT or message_id in vistos:
                    continue
                vistos.add(message_id)
                yield make_row(
                    source=SOURCE,
                    source_id=message_id,
                    source_split=split,
                    text_raw=rec["text"],
                    lang_source=lang,
                    created_ts=to_iso(rec["created_date"]),
                    meta={
                        "review_result": rec["review_result"],
                        "review_count": rec["review_count"],
                        "tree_state": rec["tree_state"],
                        "dataset": apelido,
                    },
                )
                n += 1
                if max_rows is not None and n >= max_rows:
                    _relatorio(codigos_pt)
                    return

    _relatorio(codigos_pt)


def _relatorio(codigos_pt: Counter[str]) -> None:
    """Quantas raízes `pt*` existiam e quantas ficaram de fora do conjunto PT."""
    if not codigos_pt:
        return
    print(f"[{SOURCE}] raizes por codigo pt*: {dict(sorted(codigos_pt.items()))}")
    fora = {c: n for c, n in sorted(codigos_pt.items()) if c not in PT}
    if fora:
        print(f"[{SOURCE}] ATENCAO: {sum(fora.values())} raizes fora do conjunto PT={PT}: {fora}")
