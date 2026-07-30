"""no_robots — `HuggingFaceH4/no_robots`, splits train (9.500) + test (500).

10.000 instruções escritas por profissionais contratados. Licença
**CC-BY-NC-4.0**: `commercial_ok = false` no `sources.toml`, e o export só
inclui esta fonte com `--include-nc`.

As categorias nativas (Generation, Open QA, Closed QA, Brainstorm, Chat,
Rewrite, Summarize, Coding, Classify, Extract) viajam em `native_category` e
viram rótulos grátis de `task_type` no s08, via `labeling/mappings/`.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .base import make_row, require_columns

SOURCE = "no_robots"
SPLITS = ("train", "test")
NEEDED = ("prompt", "prompt_id", "category")


def iter_rows(cfg: dict[str, Any], max_rows: int | None = None) -> Iterator[dict[str, Any]]:
    from datasets import load_dataset

    n = 0
    for split in SPLITS:
        ds = load_dataset(cfg["hf_id"], split=split)
        require_columns(ds.features, NEEDED, SOURCE)
        for rec in ds.select_columns(list(NEEDED)):
            yield make_row(
                source=SOURCE,
                source_id=rec["prompt_id"],
                source_split=split,
                text_raw=rec["prompt"],
                lang_source="en",
                native_category=rec["category"],
            )
            n += 1
            if max_rows is not None and n >= max_rows:
                return
