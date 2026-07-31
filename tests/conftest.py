"""Fixtures compartilhadas dos testes dos estágios (M4).

Duas ferramentas, nada mais:

* ``make_table`` monta uma tabela pyarrow **já no schema canônico das 27
  colunas**, preenchendo defaults válidos e derivando ``hash_norm``/``n_chars``/
  ``n_words`` do texto quando o teste não os informa. Escrever essas 27 colunas
  na mão em cada teste seria ruído puro — e o ruído esconde o que o teste quer
  provar.
* ``stage_dirs`` devolve um ``StageConfig`` apontado para ``tmp_path``, com a
  árvore criada. **Nenhum teste toca em ``data/``.**
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from prompt_factory.ingest.base import RAW_SCHEMA
from prompt_factory.schema import COLUMN_NAMES, arrow_schema, text_stats
from prompt_factory.stages import StageConfig
from prompt_factory.textnorm import norm_for_hash

#: Valores default de cada coluna canônica (linha mínima válida).
DEFAULTS: dict[str, Any] = {
    "uid": "",
    "text": "prompt de teste",
    "text_raw": "",
    "lang": "pt",
    "lang_variant": None,
    "variant_confidence": None,
    "source": "aya",
    "source_id": "1",
    "source_split": "train",
    "license": "apache-2.0",
    "commercial_ok": True,
    "redistributable": True,
    "task_type": None,
    "domain": None,
    "quality": None,
    "nsfw": None,
    "pii_found": False,
    "hash_norm": "",
    "n_exact_dups": 0,
    "n_near_dups": 0,
    "n_chars": 0,
    "n_words": 0,
    "country": None,
    "model_family": None,
    "created_ts": None,
    "native_category": None,
    "meta_json": "{}",
}


def _completar(linha: Mapping[str, Any], indice: int) -> dict[str, Any]:
    dados = {**DEFAULTS, **linha}
    texto = str(dados["text"])
    if not dados["text_raw"]:
        dados["text_raw"] = texto
    if not dados["uid"]:
        dados["uid"] = f"{indice:016x}"
    if not dados["hash_norm"]:
        dados["hash_norm"] = hashlib.sha256(
            norm_for_hash(texto).encode("utf-8")
        ).hexdigest()
    if not dados["n_chars"] and not dados["n_words"]:
        dados["n_chars"], dados["n_words"] = text_stats(texto)
    return dados


@pytest.fixture
def make_table() -> Callable[[Sequence[Mapping[str, Any]]], pa.Table]:
    """``make_table([{...}, {...}])`` → tabela no schema canônico."""

    def _make(linhas: Sequence[Mapping[str, Any]]) -> pa.Table:
        completas = [_completar(linha, i + 1) for i, linha in enumerate(linhas)]
        return pa.table(
            {col: [linha[col] for linha in completas] for col in COLUMN_NAMES},
            schema=arrow_schema(),
        )

    return _make


@pytest.fixture
def stage_dirs(tmp_path: Path) -> StageConfig:
    """``StageConfig`` isolado em ``tmp_path`` com a árvore de dados criada."""
    cfg = StageConfig(data_dir=tmp_path / "data")
    cfg.preparar_dirs()
    return cfg


@pytest.fixture
def make_raw_table() -> Callable[[Sequence[Mapping[str, Any]]], pa.Table]:
    """Tabela nas 12 colunas cruas do ``ingest/base.RAW_SCHEMA`` (entrada do s01)."""

    padrao: dict[str, Any] = {
        "source": "aya",
        "source_id": "1",
        "source_split": "train",
        "text_raw": "prompt cru",
        "lang_source": "pt",
        "license": "apache-2.0",
        "country": "",
        "model_family": "",
        "created_ts": "",
        "nsfw_hint": False,
        "native_category": "",
        "meta_json": "{}",
    }

    def _make(linhas: Sequence[Mapping[str, Any]]) -> pa.Table:
        completas = [{**padrao, **linha} for linha in linhas]
        return pa.table(
            {col: [linha[col] for linha in completas] for col in RAW_SCHEMA.names},
            schema=RAW_SCHEMA,
        )

    return _make


def escrever(tabela: pa.Table, destino: Path) -> Path:
    """Grava um parquet de teste (zstd, como a pipeline)."""
    import pyarrow.parquet as pq

    destino.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(tabela, destino, compression="zstd")
    return destino


__all__ = ["DEFAULTS", "escrever"]
