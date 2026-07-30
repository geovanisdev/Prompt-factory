"""prism — `HannahRoseKirk/prism-alignment`, config **conversations** (8.011).

O `opening_prompt` é literalmente o primeiro turno que o participante escreveu,
o que faz desta a fonte mais barata de extrair do M2.

Pegadinha: a config é OBRIGATÓRIA. `load_dataset("HannahRoseKirk/prism-alignment")`
sem ela falha listando as opções (`conversations`, `survey`, `utterances`,
`metadata`) — e `utterances` (68.371) é turno a turno, não prompt de abertura.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .base import make_row, require_columns, to_iso

SOURCE = "prism"
CONFIG = "conversations"
SPLIT = "train"
NEEDED = ("conversation_id", "user_id", "conversation_type", "opening_prompt", "generated_datetime")


def iter_rows(cfg: dict[str, Any], max_rows: int | None = None) -> Iterator[dict[str, Any]]:
    from datasets import load_dataset

    ds = load_dataset(cfg["hf_id"], CONFIG, split=SPLIT)
    require_columns(ds.features, NEEDED, SOURCE)

    n = 0
    for rec in ds.select_columns(list(NEEDED)):
        yield make_row(
            source=SOURCE,
            source_id=rec["conversation_id"],
            source_split=SPLIT,
            text_raw=rec["opening_prompt"],
            lang_source="en",
            created_ts=to_iso(rec["generated_datetime"]),
            meta={
                "user_id": rec["user_id"],
                "conversation_type": rec["conversation_type"],
            },
        )
        n += 1
        if max_rows is not None and n >= max_rows:
            return
