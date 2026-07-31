"""Monta um ``data/raw`` de brinquedo para o smoke test, sem tocar na rede.

Copia as primeiras N linhas de cada ``data/raw/<fonte>.parquet`` real para um
diretorio descartavel, preservando as 12 colunas e o metadata. Existe porque o
smoke tem de rodar a pipeline inteira em segundos e sem baixar 10 datasets --
e porque toda escrita de dados sai de dentro do Python (o PowerShell corrompe
encoding em redirecionamento).

Uso::

    python scripts/make_smoke_raw.py <destino> [--rows 200] [--source data/raw]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))


def main(argv: list[str] | None = None) -> int:
    import pyarrow as pa
    import pyarrow.parquet as pq

    from prompt_factory import paths
    from prompt_factory.stages.s01_normalize import SUFIXOS_IGNORADOS

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destino", help="diretorio de dados descartavel (recebe raw/)")
    parser.add_argument("--rows", type=int, default=200, help="linhas por fonte")
    parser.add_argument("--source", default=None, help="raw de origem (padrao: data/raw)")
    args = parser.parse_args(argv)

    origem = Path(args.source) if args.source else paths.RAW
    destino = Path(args.destino) / "raw"
    destino.mkdir(parents=True, exist_ok=True)

    arquivos = [
        p
        for p in sorted(origem.glob("*.parquet"))
        if not p.stem.endswith(SUFIXOS_IGNORADOS)
    ]
    if not arquivos:
        print(f"[smoke-raw] nenhum parquet em {origem} - rode `pf ingest` antes")
        return 2

    total = 0
    for caminho in arquivos:
        arquivo = pq.ParquetFile(caminho)
        lotes = list(arquivo.iter_batches(batch_size=args.rows))
        fatia = pa.Table.from_batches(
            lotes[:1], schema=arquivo.schema_arrow
        ).replace_schema_metadata(arquivo.schema_arrow.metadata)
        pq.write_table(fatia, destino / caminho.name, compression="zstd")
        total += fatia.num_rows
        print(f"[smoke-raw] {caminho.stem}: {fatia.num_rows} linhas")
    print(f"[smoke-raw] {total} linhas em {destino}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
