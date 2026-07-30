"""dolly — `databricks/databricks-dolly-15k`, split train (15.011 linhas).

Instruções escritas por ~5.000 funcionários da Databricks. Quando a linha tem
`context` (as categorias closed_qa / information_extraction / summarization
sempre têm), o prompt real é instrução + contexto, porque sem o contexto a
instrução fica sem referente ("Qual time ganhou?" sozinho não é um prompt).

A decisão de concatenar usa `context.strip()`, mas a concatenação usa o
`context` ORIGINAL: raw não normaliza espaço em branco.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .base import make_row, require_columns

SOURCE = "dolly"
SPLIT = "train"
NEEDED = ("instruction", "context", "response", "category")


def iter_rows(cfg: dict[str, Any], max_rows: int | None = None) -> Iterator[dict[str, Any]]:
    from datasets import load_dataset

    ds = load_dataset(cfg["hf_id"], split=SPLIT)
    require_columns(ds.features, NEEDED, SOURCE)

    n = 0
    for i, rec in enumerate(ds.select_columns(list(NEEDED))):
        instruction = rec["instruction"] or ""
        context = rec["context"] or ""
        tem_contexto = context.strip() != ""
        texto = f"{instruction}\n\n{context}" if tem_contexto else instruction
        yield make_row(
            source=SOURCE,
            source_id=f"{SPLIT}-{i:05d}",
            source_split=SPLIT,
            text_raw=texto,
            lang_source="en",
            native_category=rec["category"],
            meta={"has_context": int(tem_contexto)},
        )
        n += 1
        if max_rows is not None and n >= max_rows:
            return
