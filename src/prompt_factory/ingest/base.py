"""Contrato da camada `raw`: schema de 12 colunas e escrita atômica do parquet.

`data/raw/<fonte>.parquet` é **camada de auditoria**: guarda o texto EXATAMENTE
como veio da fonte, sem normalização nenhuma. A cadeia canônica
(`norm_display` -> `hash_norm` -> `uid` -> as 27 colunas de `schema.py`) começa
só no s01 (M4), lendo daqui. A única exceção a "texto exato" é o strip
construtivo do parser do `hh_rlhf`, que precisa recortar um turno de dentro de
um transcript.

Consequências práticas:

* raw é **regenerável**: re-rodar `pf ingest <fonte>` sobrescreve o arquivo;
* raw é **imutável na prática**: nenhum estágio escreve de volta nele;
* nenhum valor nulo — string ausente vira ``""`` e bool ausente vira ``False``,
  para que o s01 nunca precise de `fillna`.

`pyarrow` é importado no topo (este módulo só é carregado pelo handler do
`pf ingest`, nunca por `pf --help`), mas `datasets` entra *lazy* dentro do
`iter_rows()` de cada ingester — ver `ingest/aya.py`.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .. import config, paths

#: As 12 colunas do parquet cru, NA ORDEM (a ordem é contrato: o s01 lê por
#: nome, mas os testes e o `pf report raw` conferem a ordem).
RAW_SCHEMA: pa.Schema = pa.schema(
    [
        pa.field("source", pa.string(), nullable=False),
        pa.field("source_id", pa.string(), nullable=False),
        pa.field("source_split", pa.string(), nullable=False),
        pa.field("text_raw", pa.string(), nullable=False),
        pa.field("lang_source", pa.string(), nullable=False),
        pa.field("license", pa.string(), nullable=False),
        pa.field("country", pa.string(), nullable=False),
        pa.field("model_family", pa.string(), nullable=False),
        pa.field("created_ts", pa.string(), nullable=False),
        pa.field("nsfw_hint", pa.bool_(), nullable=False),
        pa.field("native_category", pa.string(), nullable=False),
        pa.field("meta_json", pa.string(), nullable=False),
    ]
)

#: Só os nomes, na ordem.
RAW_COLUMNS: tuple[str, ...] = tuple(RAW_SCHEMA.names)
_RAW_KEYS: frozenset[str] = frozenset(RAW_COLUMNS)


# ---------------------------------------------------------------------------
# helpers usados pelos 7 ingesters
# ---------------------------------------------------------------------------


def get_source_cfg(name: str) -> dict[str, Any]:
    """Spec da fonte em `config/sources.toml`, ou erro claro listando as conhecidas."""
    try:
        return config.source(name)
    except KeyError as exc:
        raise SystemExit(f"[pf] {exc.args[0]}") from None


def require_columns(features: Any, needed: Iterable[str], source: str) -> None:
    """Falha alto se a fonte mudou de schema no Hub (drift silencioso é pior)."""
    disponiveis = set(features)
    faltando = [c for c in needed if c not in disponiveis]
    if faltando:
        raise SystemExit(
            f"[{source}] schema-drift: colunas ausentes {faltando}\n"
            f"[{source}] features da fonte: {features}"
        )


def to_iso(value: Any) -> str:
    """Timestamp da fonte -> ISO-8601 em texto. `None` vira ``""``, nunca nulo."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return str(isoformat())
        except (TypeError, ValueError):  # pragma: no cover - tipo exótico
            return str(value)
    return str(value)


def sha256_hex(text: str) -> str:
    """sha256 hexdigest completo do texto em UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def dump_meta(meta: Mapping[str, Any] | None) -> str:
    """Sobras da fonte -> JSON compacto. Vazio vira ``"{}"``, nunca nulo.

    `default=str` porque datas e numpy scalars aparecem em metadados de várias
    fontes e não podem derrubar uma ingestão de 8h.
    """
    if not meta:
        return "{}"
    return json.dumps(dict(meta), ensure_ascii=False, default=str, separators=(",", ":"))


def column_values(ds: Any, column: str, batch_size: int = 20_000) -> list[Any]:
    """Lê UMA coluna inteira sem materializar as demais.

    Existe por causa do arena140k: converter as 135k linhas inteiras para dict
    Python custa minutos (`full_conversation` é lista aninhada), enquanto ler só
    `language` para descobrir quais índices interessam custa segundos.
    """
    values: list[Any] = []
    for batch in ds.select_columns([column]).iter(batch_size=batch_size):
        values.extend(batch[column])
    return values


def make_row(
    *,
    source: str,
    source_id: Any,
    source_split: str,
    text_raw: Any,
    lang_source: str,
    created_ts: str = "",
    native_category: Any = "",
    meta: Mapping[str, Any] | None = None,
    country: str = "",
    model_family: str = "",
    nsfw_hint: bool = False,
) -> dict[str, Any]:
    """Monta uma linha com as 12 chaves do `RAW_SCHEMA`, sem nulos.

    `license` sai vazia de propósito: quem preenche é o `write_raw`, a partir do
    `config/sources.toml`, para que a licença gravada nunca dependa do ingester.
    """
    return {
        "source": source,
        "source_id": _s(source_id),
        "source_split": source_split,
        "text_raw": _s(text_raw),
        "lang_source": lang_source,
        "license": "",  # sobrescrito por write_raw
        "country": country,
        "model_family": model_family,
        "created_ts": created_ts,
        "nsfw_hint": bool(nsfw_hint),
        "native_category": _s(native_category),
        "meta_json": dump_meta(meta),
    }


def _s(value: Any) -> str:
    """`None` -> ``""``; string passa intocada (raw é raw)."""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


# ---------------------------------------------------------------------------
# escrita
# ---------------------------------------------------------------------------


def _check_keys(row: Any, source_name: str) -> None:
    if not isinstance(row, dict):
        raise TypeError(f"[{source_name}] iter_rows deveria render dict, veio {type(row).__name__}")
    chaves = set(row)
    if chaves != _RAW_KEYS:
        faltando = sorted(_RAW_KEYS - chaves)
        sobrando = sorted(chaves - _RAW_KEYS)
        raise ValueError(
            f"[{source_name}] linha fora do RAW_SCHEMA: faltando={faltando} sobrando={sobrando}"
        )


def _rel(path: Path) -> str:
    """Caminho relativo à raiz do repo, para o resumo caber numa linha."""
    try:
        return path.relative_to(paths.ROOT).as_posix()
    except ValueError:  # pragma: no cover - tmp_path nos testes
        return str(path)


def write_raw(
    source_name: str,
    rows_iter: Iterable[dict[str, Any]],
    max_rows: int | None = None,
    raw_dir: Path | None = None,
) -> Path:
    """Materializa `rows_iter` em `data/raw/<fonte>.parquet` e devolve o caminho.

    Materializa em RAM (lista) de propósito: as 7 fontes do M2 cabem em ~16k
    linhas cada. O WildChat do M3 é grande demais para isto e vai precisar de
    part-files próprios — não reuse `write_raw` para ele sem revisar este ponto.

    Escreve em `.tmp` + `os.replace` (atômico até no Windows): um Ctrl+C no meio
    do download nunca deixa um parquet truncado no lugar do bom.

    `raw_dir` redireciona a escrita (é o `--data-dir` do `pf ingest`, P6). Existe
    porque o smoke da fonte `plataforma` precisa rodar a cadeia inteira num
    diretório descartável: ela é a única fonte cujo insumo é LOCAL, então a
    ingestão dela é testável de ponta a ponta — e um teste que escrevesse em
    `data/raw/` estaria mexendo no corpus de produção para provar um ponto.
    """
    cfg = get_source_cfg(source_name)
    inicio = time.perf_counter()
    licenca = str(cfg.get("license", "unknown"))

    rows: list[dict[str, Any]] = []
    n_vazias = 0
    for row in itertools.islice(rows_iter, max_rows):
        _check_keys(row, source_name)
        texto = row["text_raw"]
        if not isinstance(texto, str) or not texto.strip():
            n_vazias += 1
            continue
        row["license"] = licenca
        rows.append(row)

    tabela = pa.Table.from_pylist(rows, schema=RAW_SCHEMA)
    tabela = tabela.replace_schema_metadata(
        {
            "pf_source": source_name,
            "pf_hf_id": str(cfg.get("hf_id", "")),
            "pf_license": licenca,
            "pf_attribution": str(cfg.get("attribution", "")),
            "pf_ingested_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "pf_n_rows": str(len(rows)),
        }
    )

    destino_dir = paths.RAW if raw_dir is None else Path(raw_dir)
    destino_dir.mkdir(parents=True, exist_ok=True)
    destino = destino_dir / f"{source_name}.parquet"
    tmp = destino.parent / f"{destino.name}.tmp"
    pq.write_table(tabela, tmp, compression="zstd")
    os.replace(tmp, destino)

    tamanho = destino.stat().st_size
    segundos = time.perf_counter() - inicio
    print(
        f"[{source_name}] {len(rows)} linhas ({n_vazias} vazias puladas) -> "
        f"{_rel(destino)} ({tamanho} bytes, {segundos:.1f} s)"
    )
    if not rows:
        print(f"[{source_name}] ATENCAO: parquet vazio - confira o filtro do ingester")
    for pos in dict.fromkeys((0, len(rows) // 2)):
        if pos < len(rows):
            print(f"[{source_name}]   amostra[{pos}] {rows[pos]['text_raw']!r:.120}")
    return destino


__all__ = [
    "RAW_COLUMNS",
    "RAW_SCHEMA",
    "column_values",
    "dump_meta",
    "get_source_cfg",
    "make_row",
    "require_columns",
    "sha256_hex",
    "to_iso",
    "write_raw",
]
