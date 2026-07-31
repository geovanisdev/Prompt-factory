"""Caminhos canônicos do repositório.

Tudo é derivado de ``__file__`` — nada depende do diretório de trabalho atual,
porque a CLI e os testes rodam de lugares diferentes.

Layout::

    <ROOT>/
      config/            settings.toml, sources.toml
      data/              (gitignorado, só .gitkeep versionado)
        raw/ interim/ final/ emb/ models/ db/ exports/
      labeling/          taxonomy.json, manifest.json, batches/, mappings/
      src/prompt_factory/
"""

from __future__ import annotations

from pathlib import Path

# src/prompt_factory/paths.py -> src/prompt_factory -> src -> <ROOT>
ROOT: Path = Path(__file__).resolve().parents[2]

CONFIG: Path = ROOT / "config"
SETTINGS_TOML: Path = CONFIG / "settings.toml"
SOURCES_TOML: Path = CONFIG / "sources.toml"

DATA: Path = ROOT / "data"
RAW: Path = DATA / "raw"          # um parquet por fonte, cru-normalizado
INTERIM: Path = DATA / "interim"  # estágios s01..s06
FINAL: Path = DATA / "final"      # universe.parquet e derivados
EMB: Path = DATA / "emb"          # embeddings .npy (float16, mmap)
MODELS: Path = DATA / "models"    # classificadores joblib
DB: Path = DATA / "db"            # prompts.sqlite (+ build/swap)
EXPORTS: Path = DATA / "exports"  # JSONL/CSV + manifests

LABELING: Path = ROOT / "labeling"
TAXONOMY_JSON: Path = LABELING / "taxonomy.json"
LABEL_MANIFEST: Path = LABELING / "manifest.json"
BATCHES: Path = LABELING / "batches"      # lotes entregues aos agentes (gitignorado)
LABELS: Path = LABELING / "labels"        # respostas validadas, 1 jsonl por lote
SEED: Path = LABELING / "seed"            # s07: seed.parquet + strata.txt
MAPPINGS: Path = LABELING / "mappings"

SCRIPTS: Path = ROOT / "scripts"
TESTS: Path = ROOT / "tests"

#: Diretórios que ``ensure_dirs()`` garante existirem.
DATA_DIRS: tuple[Path, ...] = (RAW, INTERIM, FINAL, EMB, MODELS, DB, EXPORTS)
LABELING_DIRS: tuple[Path, ...] = (BATCHES, LABELS, SEED, MAPPINGS)


def ensure_dirs(*extra: Path) -> None:
    """Cria (idempotente) toda a árvore de trabalho, mais quaisquer ``extra``."""
    for directory in (*DATA_DIRS, *LABELING_DIRS, *extra):
        directory.mkdir(parents=True, exist_ok=True)


__all__ = [
    "BATCHES",
    "CONFIG",
    "DATA",
    "DATA_DIRS",
    "DB",
    "EMB",
    "EXPORTS",
    "FINAL",
    "INTERIM",
    "LABELING",
    "LABELING_DIRS",
    "LABELS",
    "LABEL_MANIFEST",
    "MAPPINGS",
    "MODELS",
    "RAW",
    "ROOT",
    "SCRIPTS",
    "SEED",
    "SETTINGS_TOML",
    "SOURCES_TOML",
    "TAXONOMY_JSON",
    "TESTS",
    "ensure_dirs",
]
