"""s04 — dedup exato por ``hash_norm``.

Entrada ``interim/scrubbed.parquet`` → saídas ``interim/dedup1.parquet`` (o
universo sem duplicatas exatas) e ``interim/dedup_exact_map.parquet`` (uma linha
por descartado, para auditoria e replay).

``hash_norm`` é o sha256 de ``norm_for_hash(text)``, que dobra acento, caixa,
espaço e pontuação terminal: "Olá!!" e "ola" caem no mesmo grupo de propósito.

**Chave vazia não agrupa.** ``norm_for_hash("!!!") == ""``, então todo prompt
feito só de pontuação ou de emoji com ZWJ tem o mesmo hash (o sha256 da string
vazia). Colapsar isso num só seria destruir centenas de linhas distintas por um
detalhe de normalização — elas passam inteiras e são contadas à parte no funil.

**Qual cópia sobrevive**: ``dedup.choose_canonical`` — licença mais permissiva
primeiro, depois a ordem editorial do ``sources.toml``, depois ``source_id``/
``uid``. O canônico recebe ``n_exact_dups`` = quantas cópias absorveu e, no
``meta_json``, a chave ``dup_sources`` com as **outras** fontes que traziam o
mesmo texto — é assim que se descobre depois que um prompt do WildChat também
está no Aya.

**Pegadinha do pandas 3**: a tabela canônica NUNCA passa por pandas. O DataFrame
daqui tem só colunas de texto e ranks inteiros, e serve exclusivamente para
ordenar; a filtragem acontece em pyarrow, com máscara posicional. Se ``quality``
(int8 *nullable*) desse uma volta pelo pandas, viraria float64 e 3 viraria 3.0
no export.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from .. import dedup
from ..schema import arrow_schema
from . import (
    BATCH_LEITURA,
    DEDUP1,
    MAPA_EXATO,
    SCRUBBED,
    Cronometro,
    EscritorParquet,
    StageConfig,
    checar_colunas,
    escrever_tabela,
    exigir,
    imprimir_funil,
    rel,
)

ESTAGIO = "s04"

#: sha256 da string vazia — o ``hash_norm`` de todo prompt que vira "" na
#: normalização de comparação. Nunca agrupa (ver docstring do módulo).
HASH_VAZIO = hashlib.sha256(b"").hexdigest()

#: Colunas do mapa de duplicatas exatas.
COLUNAS_MAPA: tuple[str, ...] = (
    "uid",
    "canonical_uid",
    "hash_norm",
    "source",
    "canonical_source",
)


def _agrupar(origem: Any) -> tuple[Any, Any, dict[int, list[str]], list[dict[str, str]], dict]:
    """Passe 1: lê só as colunas-chave e decide quem sobrevive.

    Devolve ``(remover, n_dups, dup_sources, linhas_do_mapa, estatísticas)``.
    """
    import numpy as np
    import pandas as pd
    import pyarrow.parquet as pq

    chaves = pq.read_table(
        origem, columns=["uid", "hash_norm", "license", "source", "source_id"]
    )
    n = chaves.num_rows
    uid = chaves.column("uid").to_pylist()
    hash_norm = chaves.column("hash_norm").to_pylist()
    licenca = chaves.column("license").to_pylist()
    fonte = chaves.column("source").to_pylist()
    sid = chaves.column("source_id").to_pylist()

    ranks = dedup.source_rank()
    df = pd.DataFrame(
        {
            "hash": hash_norm,
            "rank_lic": [
                dedup.LICENSE_RANK.get(str(x or ""), dedup.RANK_LICENCA_DESCONHECIDA)
                for x in licenca
            ],
            "rank_src": [ranks.get(str(x or ""), dedup.RANK_FONTE_DESCONHECIDA) for x in fonte],
            "sid_nulo": [0 if s else 1 for s in sid],
            "sid": [str(s or "") for s in sid],
            "uid": uid,
            "pos": np.arange(n, dtype=np.int64),
        }
    )
    ordenado = df.sort_values(
        ["hash", "rank_lic", "rank_src", "sid_nulo", "sid", "uid"], kind="stable"
    )
    ord_hash = ordenado["hash"].to_numpy()
    ord_pos = ordenado["pos"].to_numpy()

    remover = np.zeros(n, dtype=bool)
    n_dups = np.zeros(n, dtype=np.int32)
    dup_sources: dict[int, list[str]] = {}
    mapa: list[dict[str, str]] = []
    tamanhos: Counter[int] = Counter()
    removidas_por_fonte: Counter[str] = Counter()
    maiores: list[tuple[int, int]] = []  # (tamanho, posição do canônico)
    grupos = vazias = 0

    i = 0
    while i < n:
        j = i + 1
        while j < n and ord_hash[j] == ord_hash[i]:
            j += 1
        tamanho = j - i
        if ord_hash[i] == HASH_VAZIO or not ord_hash[i]:
            vazias += tamanho
            i = j
            continue
        if tamanho > 1:
            grupos += 1
            tamanhos[tamanho] += 1
            canonico = int(ord_pos[i])
            n_dups[canonico] = tamanho - 1
            outras: set[str] = set()
            maiores.append((tamanho, canonico))
            for k in range(i + 1, j):
                p = int(ord_pos[k])
                remover[p] = True
                removidas_por_fonte[fonte[p]] += 1
                if fonte[p] != fonte[canonico]:
                    outras.add(str(fonte[p]))
                mapa.append(
                    {
                        "uid": uid[p],
                        "canonical_uid": uid[canonico],
                        "hash_norm": hash_norm[p],
                        "source": fonte[p],
                        "canonical_source": fonte[canonico],
                    }
                )
            if outras:
                dup_sources[canonico] = sorted(outras)
        i = j

    maiores.sort(key=lambda t: (-t[0], t[1]))
    estat = {
        "linhas": n,
        "grupos": grupos,
        "vazias": vazias,
        "removidas": int(remover.sum()),
        "removidas_por_fonte": removidas_por_fonte,
        "tamanhos": tamanhos,
        "maiores": [(t, uid[p], fonte[p]) for t, p in maiores[:5]],
    }
    return remover, n_dups, dup_sources, mapa, estat


def run(cfg: StageConfig) -> int:
    """Colapsa duplicatas exatas. 0 = sucesso."""
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    relogio = Cronometro(ESTAGIO)
    cfg.preparar_dirs()
    origem = cfg.caminho(SCRUBBED)
    exigir(origem, "pf run s03")
    destino = cfg.caminho(DEDUP1)
    destino_mapa = cfg.caminho(MAPA_EXATO)

    remover, n_dups, dup_sources, mapa, estat = _agrupar(origem)

    schema = arrow_schema()
    i_dups = schema.get_field_index("n_exact_dups")
    i_meta = schema.get_field_index("meta_json")
    arquivo = pq.ParquetFile(origem)
    checar_colunas(arquivo.schema_arrow, rel(origem))

    offset = 0
    with EscritorParquet(destino, schema=schema) as escritor:
        for lote in arquivo.iter_batches(batch_size=BATCH_LEITURA):
            n = lote.num_rows
            fatia_remover = remover[offset : offset + n]
            manter = np.flatnonzero(~fatia_remover)
            if manter.size:
                tabela = pa.Table.from_batches([lote]).take(pa.array(manter, type=pa.int64()))
                dups = n_dups[offset : offset + n][manter]
                tabela = tabela.set_column(
                    i_dups, schema.field(i_dups), pa.array(dups, type=pa.int32())
                )
                if dup_sources:
                    metas = tabela.column("meta_json").to_pylist()
                    mudou = False
                    for local, global_pos in enumerate(manter):
                        fontes = dup_sources.get(offset + int(global_pos))
                        if not fontes:
                            continue
                        try:
                            dados = json.loads(metas[local]) if metas[local] else {}
                        except (json.JSONDecodeError, TypeError):
                            dados = {"_raw_meta": metas[local]}
                        if not isinstance(dados, dict):
                            dados = {"_raw_meta": metas[local]}
                        dados["dup_sources"] = fontes
                        metas[local] = json.dumps(
                            dados, ensure_ascii=False, separators=(",", ":")
                        )
                        mudou = True
                    if mudou:
                        tabela = tabela.set_column(
                            i_meta, schema.field(i_meta), pa.array(metas, type=pa.string())
                        )
                escritor.escrever(tabela.cast(schema))
            offset += n

    escrever_tabela(
        pa.table(
            {coluna: [linha[coluna] for linha in mapa] for coluna in COLUNAS_MAPA},
            schema=pa.schema([pa.field(c, pa.string(), nullable=False) for c in COLUNAS_MAPA]),
        ),
        destino_mapa,
    )

    saida = estat["linhas"] - estat["removidas"]
    imprimir_funil(
        ESTAGIO,
        ("etapa", "linhas"),
        [
            ["lidas", estat["linhas"]],
            ["grupos com duplicata", estat["grupos"]],
            ["removidas", estat["removidas"]],
            ["chaves vazias (nunca agrupam)", estat["vazias"]],
        ],
        ["SAÍDA", saida],
    )
    if estat["removidas_por_fonte"]:
        imprimir_funil(
            ESTAGIO,
            ("fonte", "removidas"),
            sorted(estat["removidas_por_fonte"].items(), key=lambda kv: -kv[1]),
        )
    if estat["maiores"]:
        imprimir_funil(
            ESTAGIO,
            ("tam", "uid canônico", "fonte"),
            [[t, u, f] for t, u, f in estat["maiores"]],
        )
    print(f"[{ESTAGIO}] {saida} linhas -> {rel(destino)}")
    print(f"[{ESTAGIO}] {len(mapa)} descartes -> {rel(destino_mapa)}")
    relogio.fim()
    return 0


__all__ = ["COLUNAS_MAPA", "ESTAGIO", "HASH_VAZIO", "run"]
