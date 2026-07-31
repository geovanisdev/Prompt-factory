"""s06 — dedup próximo (cosseno + Jaccard) e nascimento do ``universe.parquet``.

Entradas: ``interim/dedup1.parquet`` + ``emb/embeddings.f16.npy`` +
``emb/uids.txt``. Saídas: ``final/universe.parquet``,
``final/dedup_near_map.parquet`` e o par de embeddings alinhado ao universo
(``emb/universe.f16.npy`` + ``emb/universe_uids.txt``).

**Dois critérios, não um.** Cosseno alto sozinho junta prompts que apenas
*falam do mesmo assunto* — e este corpus tem um robô que gerou dezenas de
milhares de linhas com o mesmo cabeçalho e conteúdos diferentes. Só vira
duplicata quem passa nos três testes:

1. cosseno ≥ ``[dedup] near_cosine``;
2. Jaccard dos tokens (mesma normalização do hash) ≥ ``[dedup] near_jaccard``;
3. razão de ``n_chars`` ≤ ``[dedup] near_len_ratio`` — um resumo não é o texto.

**Por que blocos duplos.** A conta é ``X @ X.T`` dentro de cada idioma; com 145
mil linhas isso seria uma matriz de 84 GB. O passe percorre ``block_rows`` linhas
x ``col_chunk`` colunas por vez (2048 x 32768 f32 = 256 MB) e só olha o triângulo
superior. A multiplicação é **sempre em float32**: o `.npy` é f16 para caber no
disco, mas numpy não tem BLAS para f16 e a conta cairia para um laço em Python.
Os vetores são renormalizados em f32 depois do round-trip.

**Invariante posicional.** ``embeddings.f16.npy`` está alinhado ao
``dedup1.parquet``; ``universe.f16.npy`` ao ``universe.parquet``. O estágio
confere o primeiro par com assert (forma e uids, posição a posição) antes de
qualquer conta, e grava o segundo par junto do universo — sem isso a busca
semântica do app devolveria uids que não existem mais.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .. import config, dedup
from ..schema import arrow_schema
from . import (
    BATCH_LEITURA,
    DEDUP1,
    EMB_UIDS,
    EMBEDDINGS,
    MAPA_PROXIMO,
    UNIVERSE,
    UNIVERSE_EMB,
    UNIVERSE_UIDS,
    Cronometro,
    EscritorParquet,
    StageConfig,
    checar_colunas,
    escrever_tabela,
    exigir,
    imprimir_funil,
    rel,
    substituir,
)

ESTAGIO = "s06"

#: Colunas do mapa de near-duplicatas.
COLUNAS_MAPA: tuple[str, ...] = ("uid", "canonical_uid", "cosine", "jaccard", "lang")


def _confirmar_pares(
    pares: Any,
    idx: Any,
    textos: Any,
    n_chars: Any,
    uf: dedup.UnionFind,
    limiar_jaccard: float,
    limiar_razao: float,
    cache: dict[int, frozenset[str]],
) -> int:
    """Aplica Jaccard nos candidatos e une o que passar. Devolve quantos passaram."""
    confirmados = 0
    for local_i, local_j in pares:
        if uf.find(local_i) == uf.find(local_j):
            # Já estão no mesmo componente: confirmar o par não mudaria nada e
            # o Jaccard é a parte cara do estágio.
            continue
        gi, gj = int(idx[local_i]), int(idx[local_j])
        ti = cache.get(gi)
        if ti is None:
            ti = cache[gi] = dedup.tokens(textos[gi].as_py() or "")
        tj = cache.get(gj)
        if tj is None:
            tj = cache[gj] = dedup.tokens(textos[gj].as_py() or "")
        if dedup.jaccard(ti, tj) < limiar_jaccard:
            continue
        if dedup.razao_tamanho(int(n_chars[gi]), int(n_chars[gj])) > limiar_razao:
            continue
        uf.union(local_i, local_j)
        confirmados += 1
    return confirmados


def run(cfg: StageConfig) -> int:
    """Colapsa near-duplicatas e grava o universo. 0 = sucesso."""
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    relogio = Cronometro(ESTAGIO)
    cfg.preparar_dirs()
    origem = cfg.caminho(DEDUP1)
    exigir(origem, "pf run s04")
    origem_emb = cfg.caminho(EMBEDDINGS)
    exigir(origem_emb, "pf run s05")
    origem_uids = cfg.caminho(EMB_UIDS)
    exigir(origem_uids, "pf run s05")

    near_cos = float(config.get("dedup", "near_cosine", default=0.985))
    near_jac = float(config.get("dedup", "near_jaccard", default=0.65))
    near_ratio = float(config.get("dedup", "near_len_ratio", default=2.0))
    block_rows = int(config.get("dedup", "block_rows", default=2048))
    col_chunk = int(config.get("dedup", "col_chunk", default=32768))

    tabela_chaves = pq.read_table(
        origem, columns=["uid", "lang", "n_chars", "license", "source", "source_id", "text"]
    )
    n = tabela_chaves.num_rows
    uids = tabela_chaves.column("uid").to_pylist()
    langs = np.array(tabela_chaves.column("lang").to_pylist(), dtype=object)
    n_chars = np.array(tabela_chaves.column("n_chars").to_pylist(), dtype=np.int64)
    licencas = tabela_chaves.column("license").to_pylist()
    fontes = tabela_chaves.column("source").to_pylist()
    sids = tabela_chaves.column("source_id").to_pylist()
    textos = tabela_chaves.column("text").combine_chunks()

    emb = np.load(origem_emb, mmap_mode="r")
    if emb.shape[0] != n:
        raise SystemExit(
            f"[{ESTAGIO}] invariante quebrado: {rel(origem_emb)} tem {emb.shape[0]} linhas "
            f"e {rel(origem)} tem {n} — rode `pf run s05` de novo"
        )
    uids_npy = origem_uids.read_text(encoding="utf-8").splitlines()
    if uids_npy != uids:
        divergencia = next(
            (i for i, (a, b) in enumerate(zip(uids_npy, uids, strict=False)) if a != b),
            min(len(uids_npy), len(uids)),
        )
        raise SystemExit(
            f"[{ESTAGIO}] invariante quebrado: {rel(origem_uids)} diverge do parquet na "
            f"posição {divergencia} — rode `pf run s05` de novo"
        )
    print(f"[{ESTAGIO}] invariante posicional OK: {n} linhas x {emb.shape[1]} dims")
    print(
        f"[{ESTAGIO}] limiares: cosseno>={near_cos} jaccard>={near_jac} "
        f"razão<={near_ratio}; blocos {block_rows}x{col_chunk}"
    )

    remover = np.zeros(n, dtype=bool)
    n_near = np.zeros(n, dtype=np.int32)
    mapa: list[dict[str, Any]] = []
    tamanhos: Counter[int] = Counter()
    removidas_por_fonte: Counter[str] = Counter()
    maiores: list[tuple[int, str, str]] = []
    resumo_particoes: list[list[Any]] = []
    cache_tokens: dict[int, frozenset[str]] = {}

    for lang in sorted({str(x) for x in langs}):
        idx = np.flatnonzero(langs == lang)
        m = int(idx.size)
        if m < 2:
            resumo_particoes.append([lang, m, 0, 0, 0, 0])
            continue
        X = np.asarray(emb[idx], dtype=np.float32)
        normas = np.linalg.norm(X, axis=1, keepdims=True)
        np.maximum(normas, 1e-12, out=normas)
        X /= normas

        uf = dedup.UnionFind(m)
        candidatos = confirmados = 0
        for i0 in range(0, m, block_rows):
            i1 = min(i0 + block_rows, m)
            A = X[i0:i1]
            for j0 in range(i0, m, col_chunk):
                j1 = min(j0 + col_chunk, m)
                S = A @ X[j0:j1].T
                linhas, colunas = np.nonzero(S >= near_cos)
                if linhas.size == 0:
                    continue
                gi = linhas + i0
                gj = colunas + j0
                acima = gj > gi
                gi, gj = gi[acima], gj[acima]
                if gi.size == 0:
                    continue
                # Filtro de tamanho vetorizado ANTES do Jaccard (que é Python).
                ci = n_chars[idx[gi]]
                cj = n_chars[idx[gj]]
                maior = np.maximum(ci, cj).astype(np.float64)
                menor = np.maximum(np.minimum(ci, cj), 1).astype(np.float64)
                passa = (maior / menor) <= near_ratio
                gi, gj = gi[passa], gj[passa]
                candidatos += int(gi.size)
                if gi.size:
                    confirmados += _confirmar_pares(
                        zip(gi.tolist(), gj.tolist(), strict=True),
                        idx,
                        textos,
                        n_chars,
                        uf,
                        near_jac,
                        near_ratio,
                        cache_tokens,
                    )
            print(
                f"[{ESTAGIO}] {lang}: {min(i1, m)}/{m} linhas varridas, "
                f"{candidatos} candidatos, {confirmados} confirmados",
                flush=True,
            )

        grupos = uf.groups()
        for membros in grupos.values():
            globais = [int(idx[k]) for k in membros]
            canonico = int(
                dedup.choose_canonical(
                    [
                        {
                            "uid": uids[g],
                            "license": licencas[g],
                            "source": fontes[g],
                            "source_id": sids[g],
                            "_pos": g,
                        }
                        for g in globais
                    ]
                )["_pos"]
            )
            n_near[canonico] = len(globais) - 1
            tamanhos[len(globais)] += 1
            maiores.append((len(globais), uids[canonico], str(fontes[canonico])))
            vetor_c = X[np.searchsorted(idx, canonico)]
            tok_c = cache_tokens.get(canonico)
            if tok_c is None:
                tok_c = cache_tokens[canonico] = dedup.tokens(textos[canonico].as_py() or "")
            for g in globais:
                if g == canonico:
                    continue
                remover[g] = True
                removidas_por_fonte[str(fontes[g])] += 1
                tok_g = cache_tokens.get(g)
                if tok_g is None:
                    tok_g = cache_tokens[g] = dedup.tokens(textos[g].as_py() or "")
                mapa.append(
                    {
                        "uid": uids[g],
                        "canonical_uid": uids[canonico],
                        "cosine": float(np.dot(vetor_c, X[np.searchsorted(idx, g)])),
                        "jaccard": float(dedup.jaccard(tok_c, tok_g)),
                        "lang": lang,
                    }
                )
        resumo_particoes.append(
            [lang, m, candidatos, confirmados, len(grupos), int(np.count_nonzero(remover[idx]))]
        )
        del X

    # --- gravação --------------------------------------------------------
    destino = cfg.caminho(UNIVERSE)
    schema = arrow_schema()
    i_near = schema.get_field_index("n_near_dups")
    arquivo = pq.ParquetFile(origem)
    checar_colunas(arquivo.schema_arrow, rel(origem))
    offset = 0
    with EscritorParquet(destino, schema=schema) as escritor:
        for lote in arquivo.iter_batches(batch_size=BATCH_LEITURA):
            b = lote.num_rows
            manter = np.flatnonzero(~remover[offset : offset + b])
            if manter.size:
                tabela = pa.Table.from_batches([lote]).take(pa.array(manter, type=pa.int64()))
                perto = n_near[offset : offset + b][manter]
                tabela = tabela.set_column(
                    i_near, schema.field(i_near), pa.array(perto, type=pa.int32())
                )
                escritor.escrever(tabela.cast(schema))
            offset += b

    mantidas = np.flatnonzero(~remover)
    destino_emb = cfg.caminho(UNIVERSE_EMB)
    tmp_emb = destino_emb.parent / f"{destino_emb.name}.tmp"
    # np.save gruda ".npy" no NOME que recebe; com file handle ele respeita o .tmp.
    with tmp_emb.open("wb") as fh:
        np.save(fh, np.asarray(emb[mantidas], dtype=np.float16))
    substituir(tmp_emb, destino_emb)
    destino_uids = cfg.caminho(UNIVERSE_UIDS)
    tmp_uids = destino_uids.parent / f"{destino_uids.name}.tmp"
    with tmp_uids.open("w", encoding="utf-8", newline="\n") as fh:
        for p in mantidas.tolist():
            fh.write(f"{uids[p]}\n")
    substituir(tmp_uids, destino_uids)

    destino_mapa = cfg.caminho(MAPA_PROXIMO)
    escrever_tabela(
        pa.table(
            {
                "uid": [linha["uid"] for linha in mapa],
                "canonical_uid": [linha["canonical_uid"] for linha in mapa],
                "cosine": pa.array([linha["cosine"] for linha in mapa], type=pa.float32()),
                "jaccard": pa.array([linha["jaccard"] for linha in mapa], type=pa.float32()),
                "lang": [linha["lang"] for linha in mapa],
            },
            schema=pa.schema(
                [
                    pa.field("uid", pa.string(), nullable=False),
                    pa.field("canonical_uid", pa.string(), nullable=False),
                    pa.field("cosine", pa.float32(), nullable=False),
                    pa.field("jaccard", pa.float32(), nullable=False),
                    pa.field("lang", pa.string(), nullable=False),
                ]
            ),
        ),
        destino_mapa,
    )

    saida = int(mantidas.size)
    if pq.ParquetFile(destino).metadata.num_rows != saida:  # pragma: no cover - defensivo
        raise SystemExit(f"[{ESTAGIO}] universo gravado com contagem inesperada")
    conferir = np.load(destino_emb, mmap_mode="r")
    if conferir.shape[0] != saida:  # pragma: no cover - defensivo
        raise SystemExit(f"[{ESTAGIO}] universe.f16.npy com {conferir.shape[0]} != {saida}")
    del conferir

    imprimir_funil(
        ESTAGIO,
        ("lang", "linhas", "candidatos", "confirmados", "clusters", "removidas"),
        resumo_particoes,
    )
    imprimir_funil(
        ESTAGIO,
        ("etapa", "linhas"),
        [["lidas", n], ["removidas", n - saida], ["clusters", sum(tamanhos.values())]],
        ["SAÍDA", saida],
    )
    if removidas_por_fonte:
        imprimir_funil(
            ESTAGIO,
            ("fonte", "removidas"),
            sorted(removidas_por_fonte.items(), key=lambda kv: -kv[1]),
        )
    if tamanhos:
        imprimir_funil(
            ESTAGIO,
            ("tamanho do cluster", "quantos"),
            sorted(tamanhos.items()),
        )
        maiores.sort(key=lambda t: -t[0])
        imprimir_funil(
            ESTAGIO,
            ("tam", "uid canônico", "fonte"),
            [list(x) for x in maiores[:5]],
        )
    print(f"[{ESTAGIO}] {saida} linhas -> {rel(destino)}")
    print(f"[{ESTAGIO}] {len(mapa)} descartes -> {rel(destino_mapa)}")
    print(f"[{ESTAGIO}] embeddings do universo -> {rel(destino_emb)} + {rel(destino_uids)}")
    relogio.fim()
    return 0


__all__ = ["COLUNAS_MAPA", "ESTAGIO", "run"]
