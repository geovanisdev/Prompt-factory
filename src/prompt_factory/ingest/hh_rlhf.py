"""hh_rlhf — primeiro turno humano dos subsets **helpful** de `Anthropic/hh-rlhf`.

Só `helpful-base`, `helpful-online` e `helpful-rejection-sampled`. O subset
`harmless-base` fica fora por decisão de plano, e `red-team-attempts` **nunca**
é tocado (schema diferente e conteúdo adversarial por construção).

Cada linha é um transcript inteiro (`\\n\\nHuman: ... \\n\\nAssistant: ...`); o
que interessa é só o primeiro turno humano. Como `chosen` e `rejected` só
divergem depois da primeira resposta do assistente, lemos apenas `chosen`.

Mesmo assim há sobreposição enorme entre os três diretórios, então o pool é
deduplicado por `sha256(texto)` — e o corte para `CAP` linhas é feito por ordem
de hash, não por ordem de leitura: assim duas execuções (ou duas máquinas)
produzem exatamente as mesmas 15.000 linhas.

`--max-rows` (smoke) pula o hash-gate e devolve as primeiras N linhas na ordem
de leitura: é uma amostra DIFERENTE da que o full produz, de propósito (o smoke
não pode pagar a leitura das 118k linhas).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .base import make_row, require_columns, sha256_hex

SOURCE = "hh_rlhf"
#: Nunca inclua "red-team-attempts" aqui. "harmless-base" fica fora por plano.
DATA_DIRS = ("helpful-base", "helpful-online", "helpful-rejection-sampled")
SPLIT = "train"
CAP = 15_000
NEEDED = ("chosen",)
_SEP = "\n\nAssistant:"
_HUMAN = "Human:"


def first_human_turn(chosen: str | None) -> str | None:
    """Primeiro turno humano de um transcript, ou `None` se não houver texto.

    Strip construtivo: é a única normalização permitida na camada raw, porque
    recortar um turno de dentro do transcript deixa `\\n\\n` sobrando nas pontas.
    """
    if not chosen:
        return None
    texto = chosen.split(_SEP)[0].strip()
    if texto.startswith(_HUMAN):
        texto = texto[len(_HUMAN) :].strip()
    return texto or None


def iter_rows(cfg: dict[str, Any], max_rows: int | None = None) -> Iterator[dict[str, Any]]:
    from datasets import load_dataset

    hf_id = cfg["hf_id"]

    def _turnos(data_dir: str) -> Iterator[tuple[str, str]]:
        ds = load_dataset(hf_id, data_dir=data_dir, split=SPLIT)
        require_columns(ds.features, NEEDED, SOURCE)
        for rec in ds.select_columns(list(NEEDED)):
            texto = first_human_turn(rec["chosen"])
            if texto:
                yield sha256_hex(texto), texto

    if max_rows is not None:
        # Smoke: curto-circuito sem hash-gate (amostra != a do full).
        vistos: set[str] = set()
        n = 0
        for data_dir in DATA_DIRS:
            for chave, texto in _turnos(data_dir):
                if chave in vistos:
                    continue
                vistos.add(chave)
                yield _row(chave, texto, data_dir)
                n += 1
                if n >= max_rows:
                    return
        return

    # Full: pool deduplicado; o primeiro diretório que traz o texto fica com ele.
    pool: dict[str, tuple[str, str]] = {}
    for data_dir in DATA_DIRS:
        antes = len(pool)
        lidas = 0
        for chave, texto in _turnos(data_dir):
            lidas += 1
            pool.setdefault(chave, (texto, data_dir))
        print(f"[{SOURCE}] {data_dir}: {lidas} turnos lidos, +{len(pool) - antes} novos")

    print(f"[{SOURCE}] pool com {len(pool)} turnos unicos; cap por hash em {CAP}")
    for chave in sorted(pool)[:CAP]:
        texto, data_dir = pool[chave]
        yield _row(chave, texto, data_dir)


def _row(chave: str, texto: str, data_dir: str) -> dict[str, Any]:
    return make_row(
        source=SOURCE,
        source_id=chave,
        source_split=data_dir,
        text_raw=texto,
        lang_source="en",
    )
