"""aya — `CohereLabs/aya_dataset`, split train (202.362 linhas, ~8.997 em pt).

Prompts escritos à mão por anotadores humanos: a melhor razão qualidade/licença
do corpus. O filtro é `language == "Portuguese"` (a coluna vem por extenso; o
`language_code` é ISO 639-3, `"por"` — não confundir com o `"pt"` minúsculo do
arena140k). O split `test` (1.750) fica de fora por decisão de plano.

`source_id` é posicional sobre o split COMPLETO (antes do filtro), então re-rodar
a ingestão reproduz os mesmos ids.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from typing import Any

from .base import column_values, make_row, require_columns

SOURCE = "aya"
SPLIT = "train"
LANGUAGE = "Portuguese"
NEEDED = ("inputs", "targets", "language", "language_code", "annotation_type", "user_id")


def iter_rows(cfg: dict[str, Any], max_rows: int | None = None) -> Iterator[dict[str, Any]]:
    from datasets import load_dataset

    ds = load_dataset(cfg["hf_id"], split=SPLIT)
    require_columns(ds.features, NEEDED, SOURCE)

    idiomas = column_values(ds, "language")
    manter = [i for i, lang in enumerate(idiomas) if lang == LANGUAGE]
    print(f"[{SOURCE}] {len(idiomas)} linhas no split {SPLIT}, {len(manter)} com language={LANGUAGE!r}")
    if not manter:
        # Diagnóstico: quase sempre significa que a fonte renomeou os idiomas.
        print(f"[{SOURCE}] idiomas vistos (top 20): {Counter(idiomas).most_common(20)}")
        return

    sub = ds.select_columns(list(NEEDED)).select(manter)
    n = 0
    for i, rec in zip(manter, sub, strict=True):
        yield make_row(
            source=SOURCE,
            source_id=f"{SPLIT}-{i:06d}",
            source_split=SPLIT,
            text_raw=rec["inputs"],
            lang_source="pt",
            meta={
                "language_code": rec["language_code"],
                "annotation_type": rec["annotation_type"],
                "user_id": rec["user_id"],
            },
        )
        n += 1
        if max_rows is not None and n >= max_rows:
            return
