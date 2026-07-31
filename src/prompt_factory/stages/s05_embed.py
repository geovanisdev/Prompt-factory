"""s05 — embeddings do universo com o e5-small multilíngue.

Entrada ``interim/dedup1.parquet`` → saídas ``emb/embeddings.f16.npy`` (matriz
``(n, 384)`` em float16) e ``emb/uids.txt`` (um uid por linha, **na mesma ordem
posicional**).

**O invariante que sustenta o resto do projeto**: a linha *i* do `.npy` é a
linha *i* do parquet. Não há índice, não há join — há posição. O s06 confere
isso com assert antes de encostar nos vetores, e o s06 grava um par próprio
(``universe.f16.npy``/``universe_uids.txt``) alinhado ao universo final, porque
depois do dedup próximo as posições mudam.

**Retomada.** Esta é a etapa longa (20-60 min para ~190 mil linhas em CPU, mais
o download de ~450 MB do modelo na primeira vez). Perder tudo por um Ctrl+C
seria inaceitável, então a gravação é um **memmap pré-alocado** em ``.tmp`` mais
um *sidecar* ``emb/progress.json`` com ``{rows_done, n, model, dim}``:

* a cada bloco de ``[embed] chunk_rows`` linhas, escreve no memmap, dá ``flush``
  e só então atualiza o sidecar (nessa ordem — invertida, um crash entre as duas
  escritas alegaria progresso que não existe no disco);
* ao retomar, se o ``.tmp`` e o sidecar existem e concordam com ``n``, modelo e
  dimensão, o passe reabre em ``mode="r+"`` e continua de ``rows_done``;
  qualquer divergência (mudou o corpus, mudou o modelo) zera e recomeça;
* o arquivo final só aparece com ``os.replace``, no fim, com o sidecar apagado.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .. import config, embedder
from . import (
    DEDUP1,
    EMB_SIDECAR,
    EMB_UIDS,
    EMBEDDINGS,
    Cronometro,
    StageConfig,
    exigir,
    imprimir_funil,
    rel,
    substituir,
)

ESTAGIO = "s05"


def _ler_sidecar(caminho: Path) -> dict[str, Any] | None:
    try:
        with caminho.open(encoding="utf-8") as fh:
            dados = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return dados if isinstance(dados, dict) else None


def _gravar_sidecar(caminho: Path, dados: dict[str, Any]) -> None:
    tmp = caminho.parent / f"{caminho.name}.tmp"
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(dados, fh, ensure_ascii=False)
    substituir(tmp, caminho)


def run(cfg: StageConfig) -> int:
    """Gera os embeddings, retomando de onde parou. 0 = sucesso."""
    import numpy as np
    import pyarrow.parquet as pq
    from numpy.lib.format import open_memmap

    relogio = Cronometro(ESTAGIO)
    cfg.preparar_dirs()
    origem = cfg.caminho(DEDUP1)
    exigir(origem, "pf run s04")
    destino = cfg.caminho(EMBEDDINGS)
    destino_uids = cfg.caminho(EMB_UIDS)
    sidecar = cfg.caminho(EMB_SIDECAR)
    tmp = destino.parent / f"{destino.name}.tmp"

    uids = pq.read_table(origem, columns=["uid"]).column("uid").to_pylist()
    n = len(uids)
    modelo = embedder.model_name()
    dim = embedder.dim()
    chunk = int(config.get("embed", "chunk_rows", default=2048))
    threads = int(config.get("embed", "threads", default=0))
    print(
        f"[{ESTAGIO}] {n} linhas x {dim} dims, modelo {modelo!r}, "
        f"blocos de {chunk} linhas, threads={threads or 'auto'}, "
        f"corte de {embedder.truncate_chars()} chars por texto"
    )

    if n == 0:
        # numpy não faz mmap de arquivo vazio: o caso degenerado sai por aqui.
        np.save(destino, np.zeros((0, dim), dtype=np.float16))
        destino_uids.write_text("", encoding="utf-8", newline="\n")
        sidecar.unlink(missing_ok=True)
        print(f"[{ESTAGIO}] corpus vazio — {rel(destino)} com 0 linhas")
        relogio.fim()
        return 0

    esperado = {"n": n, "model": modelo, "dim": dim}
    feitas = 0
    mm: Any = None
    estado = _ler_sidecar(sidecar)
    if tmp.is_file() and estado and all(estado.get(k) == v for k, v in esperado.items()):
        candidato = int(estado.get("rows_done", 0))
        if 0 <= candidato <= n:
            mm = open_memmap(tmp, mode="r+")
            if mm.shape == (n, dim) and mm.dtype == np.float16:
                feitas = candidato
                print(f"[{ESTAGIO}] retomando de {feitas}/{n} linhas (sidecar válido)")
            else:  # pragma: no cover - .tmp de outra geração
                del mm
                mm = None
    if mm is None:
        if tmp.is_file():
            print(f"[{ESTAGIO}] .tmp incompatível com o corpus atual — recomeçando do zero")
        sidecar.unlink(missing_ok=True)
        mm = open_memmap(tmp, mode="w+", dtype=np.float16, shape=(n, dim))
        feitas = 0

    inicio = time.perf_counter()
    if feitas < n:
        pulo = feitas
        buffer: list[str] = []
        indice = feitas

        def _descarregar(alvo: Any, linhas: list[str], comeco: int) -> int:
            """Embeda o bloco, grava, dá flush e SÓ ENTÃO avança o sidecar."""
            emb = embedder.embed_texts(linhas)
            alvo[comeco : comeco + len(linhas)] = emb.astype(np.float16, copy=False)
            alvo.flush()
            _gravar_sidecar(sidecar, {**esperado, "rows_done": comeco + len(linhas)})
            return comeco + len(linhas)

        arquivo = pq.ParquetFile(origem)
        for lote in arquivo.iter_batches(batch_size=chunk, columns=["text"]):
            textos = lote.column("text").to_pylist()
            if pulo:
                if pulo >= len(textos):
                    pulo -= len(textos)
                    continue
                textos = textos[pulo:]
                pulo = 0
            buffer.extend(textos)
            while len(buffer) >= chunk:
                indice = _descarregar(mm, buffer[:chunk], indice)
                del buffer[:chunk]
                decorrido = time.perf_counter() - inicio
                taxa = (indice - feitas) / decorrido if decorrido > 0 else 0.0
                restante = (n - indice) / taxa if taxa > 0 else 0.0
                print(
                    f"[{ESTAGIO}] {indice}/{n} ({100.0 * indice / n:.1f}%) "
                    f"{taxa:.0f} textos/s, faltam ~{restante / 60:.1f} min",
                    flush=True,
                )
        if buffer:
            indice = _descarregar(mm, buffer, indice)
        if indice != n:
            raise SystemExit(
                f"[{ESTAGIO}] inconsistência: gravadas {indice} linhas para {n} do parquet"
            )
    else:
        print(f"[{ESTAGIO}] nada a fazer: as {n} linhas já estavam no .tmp")

    forma = mm.shape
    del mm  # fecha o memmap ANTES do replace (no Windows não se move arquivo aberto)
    substituir(tmp, destino)
    tmp_uids = destino_uids.parent / f"{destino_uids.name}.tmp"
    with tmp_uids.open("w", encoding="utf-8", newline="\n") as fh:
        for uid in uids:
            fh.write(f"{uid}\n")
    substituir(tmp_uids, destino_uids)
    sidecar.unlink(missing_ok=True)

    if n != forma[0] or n != len(uids):  # pragma: no cover - defensivo
        raise SystemExit(
            f"[{ESTAGIO}] forma {forma} não bate com {n} linhas do parquet / {len(uids)} uids"
        )
    segundos = time.perf_counter() - inicio
    imprimir_funil(
        ESTAGIO,
        ("etapa", "valor"),
        [
            ["linhas no parquet", n],
            ["linhas no .npy", forma[0]],
            ["uids gravados", len(uids)],
            ["dimensão", dim],
            ["textos/s", f"{(n - feitas) / segundos:.0f}" if segundos > 0 else "-"],
            ["tamanho (MB)", f"{os.path.getsize(destino) / 2**20:.1f}"],
        ],
    )
    print(f"[{ESTAGIO}] {rel(destino)} + {rel(destino_uids)}")
    relogio.fim()
    return 0


__all__ = ["ESTAGIO", "run"]
