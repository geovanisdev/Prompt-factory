"""wildchat — `allenai/WildChat-4.8M` em streaming, com checkpoint e retomada.

3.199.860 conversas reais com ChatGPT (2023-04-09 a 2025-07-31), ODC-BY-1.0.
É a única fonte do corpus grande demais para o `base.write_raw`, que materializa
tudo em RAM: aqui o passe escreve **part-files** de 50k linhas varridas e
consolida no fim. Dois modos, dois passes independentes:

* ``wildchat_pt`` — ``language == "Portuguese"``, tudo que aparecer (~38k);
* ``wildchat_en`` — ``language == "English"`` + `hash_gate` determinístico de
  11% (pool ~200k), depois ``--downsample`` corta para 100.000 exatos, sem rede.

Decisões que não são óbvias (e o motivo):

* **`columns=` sim, `filters=` não.** `columns=` é pushdown real de banda
  (~9-11,5 GB por passe em vez de ~15,3 GB). `filters=` NÃO poupa banda aqui
  (a poda por min/max de row group é inócua para `language`, que está espalhada
  por todos os grupos) e ainda estragaria o checkpoint: o contador de progresso
  é de linhas **varridas**, e com `filters=` o cliente só enxergaria as ~38k
  mantidas — o checkpoint de 50k quase nunca dispararia. Filtrar com um `if` no
  loop dá contador natural, cadência uniforme, ETA sobre 3.199.860 e um único
  code path. A sintaxe do pushdown fica registrada em `_FILTERS_TODO`.
* **`language` vem por extenso** ("Portuguese", "English"), como no aya e ao
  contrário do arena140k ("pt"). Comparação exata, sem `lower()`.
* **`toxic` é sempre False neste release** (as 1.543.476 linhas tóxicas foram
  removidas antes da publicação). `nsfw_hint` constante False é o valor CORRETO,
  não um bug do extractor. Sinal melhor viria de `openai_moderation`, que custa
  banda e fica fora do M3.
* **Ordem part-antes-de-state é obrigatória.** Se o state fosse salvo primeiro e
  o processo morresse antes do part, a retomada pularia 50k linhas sem nunca
  tê-las gravado — perda silenciosa. Na ordem correta, o pior caso é reescrever
  um part idêntico (a extração é determinística) e a consolidação deduplica por
  `source_id` de qualquer jeito. Por isso o índice do part vem do STATE, nunca
  de `len(listdir())`.

Layout em disco (tudo sob `data/`, mesmo volume — `os.replace` só é atômico
dentro do volume, e no Windows isso importa)::

    data/raw/_checkpoints/wildchat_pt.state.json
    data/raw/_parts/wildchat_pt/part_000.parquet
    data/raw/wildchat_pt.parquet          <- consolidado
    data/raw/wildchat_en_pool.parquet     <- pool de ~200k (insumo do downsample)
    data/raw/wildchat_en.parquet          <- 100.000 exatos
    data/raw/wildchat_en.strata.txt       <- relatório do downsample
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .. import paths
from .base import RAW_SCHEMA, make_row, require_columns, to_iso

SPLIT = "train"

#: Colunas pedidas ao Hub. As excluídas de propósito (openai_moderation,
#: detoxify_moderation, hashed_ip, header) são ~25-40% dos bytes do dataset.
COLUMNS: tuple[str, ...] = (
    "conversation_hash",
    "model",
    "timestamp",
    "conversation",
    "turn",
    "language",
    "toxic",
    "redacted",
    "state",
    "country",
)

#: Sintaxe verificada do pushdown de linhas (datasets>=4.8, repo parquet-nativo).
#: Não usamos: ver docstring do módulo. Fica aqui para quem for tentar de novo.
_FILTERS_TODO = 'load_dataset(..., filters=[("language", "==", "Portuguese")])'

#: Fração do pool inglês. 0,11 x ~1,788M linhas EN ≈ 197k, folga suficiente para
#: os floors do downsample estratificado chegarem a 100.000. Com 0,08 sobrariam
#: ~143k e vários estratos ficariam sem margem.
EN_POOL_FRACTION = 0.11

#: Linhas VARRIDAS entre checkpoints (não linhas mantidas).
CHECKPOINT_EVERY = 50_000
#: Linha de progresso (sem tocar no disco).
PROGRESS_EVERY = 10_000
#: Total do split train, para ETA. Se a fonte crescer, o ETA erra — nada quebra.
TOTAL_ROWS = 3_199_860

#: Faixa de sanidade do POOL en (o `expected_min/max` do toml vale para o
#: arquivo final de 100.000, não para o pool). Fora disso: WARN, nunca erro.
POOL_MIN, POOL_MAX = 130_000, 300_000

#: Teto de bytes de UMA leitura HTTP do parquet.
#:
#: MEDIDO, não estimado. Por padrão o pyarrow coalesce column chunks vizinhos em
#: ranges de até 32 MiB, e o `train-00066-of-00086.parquet` pedia 6.516.029 bytes
#: de uma vez. O middlebox de TLS desta máquina (ver CLAUDE.md) corta a resposta
#: em ~4 MB: `IncompleteRead(3.970.004..4.011.148 bytes read)`, SEMPRE o mesmo
#: total esperado. Como o retry do huggingface_hub refaz a MESMA requisição
#: gigante, as 5 tentativas dele falham igual e o passe morre em 76% — foi o que
#: aconteceu três vezes seguidas antes deste teto existir.
#: Custo de limitar: mais requisições, todas na mesma conexão keep-alive.
HTTP_RANGE_LIMIT = 2 * 1024 * 1024

#: Quantas vezes reabrir o stream depois de um erro de rede SEM avançar nenhum
#: checkpoint. Zerado a cada checkpoint novo: uma rede ruim a noite inteira ainda
#: termina o passe, mas uma falha determinística desiste em vez de girar.
MAX_TENTATIVAS_REDE = 6

#: Alvo do downsample e piso por estrato.
DOWNSAMPLE_TARGET = 100_000
DOWNSAMPLE_FLOOR = 500
DOWNSAMPLE_SEED = 42

#: Versão do formato do state.json (muda => `--restart` obrigatório).
STATE_VERSION = 1


@dataclass(frozen=True)
class Modo:
    """Um dos dois passes. `destino` é o stem do parquet consolidado."""

    nome: str
    language: str
    fraction: float
    destino: str


MODOS: dict[str, Modo] = {
    "wildchat_pt": Modo("wildchat_pt", "Portuguese", 1.0, "wildchat_pt"),
    "wildchat_en": Modo("wildchat_en", "English", EN_POOL_FRACTION, "wildchat_en_pool"),
}


# ---------------------------------------------------------------------------
# funções puras (testadas offline)
# ---------------------------------------------------------------------------


def hash_gate(conversation_hash: str, fraction: float = EN_POOL_FRACTION) -> bool:
    """Amostragem determinística e sem estado: `True` para ~`fraction` das chaves.

    Fatia o **hexdigest** (8 hex = 32 bits), não o digest binário — fatiar bytes
    daria um universo de 2**64 e o corte cairia em ~0 se comparado com 2**32.

    Independe da ordem de varredura: a mesma conversa entra ou não entra no pool
    em qualquer execução, o que torna o passe retomável sem guardar quem já foi
    sorteado.
    """
    if fraction <= 0.0:
        return False
    if fraction >= 1.0:
        return True
    h = int(hashlib.sha256(conversation_hash.encode("utf-8")).hexdigest()[:8], 16)
    return h < int(fraction * 2**32)


def first_user_text(conversation: Any) -> str | None:
    """Texto do PRIMEIRO turno `role == "user"`, ou `None` para descartar a conversa.

    Extração deliberadamente míope: das 18 chaves do struct de `conversation`
    neste release, só `role` e `content` são lidas. O struct JÁ mudou entre o
    WildChat-1M e o 4.8M; depender de mais campos é contratar retrabalho.

    `None` (em vez de `""`) distingue "não achei usuário" de "achei e o texto
    veio vazio" — as duas viram descarte, mas só a segunda é contada como
    `dropped_empty_user`.
    """
    if not isinstance(conversation, (list, tuple)):
        return None
    for msg in conversation:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content
            return None
    return None


#: Prefixo -> família, NA ORDEM. A ordem é o contrato: "gpt-4.1-mini" tem de ser
#: testado antes de "gpt-4.1", e "gpt-4-turbo" antes de "gpt-4", senão os
#: estratos do downsample colapsam na família errada.
FAMILIAS: tuple[tuple[str, str], ...] = (
    ("gpt-4.1-mini", "gpt-4.1-mini"),
    ("gpt-4.1", "gpt-4.1"),
    ("gpt-4o", "gpt-4o"),
    ("gpt-4-turbo", "gpt-4-turbo"),
    ("gpt-4", "gpt-4"),
    ("gpt-3.5", "gpt-3.5-turbo"),
    ("o1-mini", "o1-mini"),
    ("o1", "o1-preview"),
)


def family_of(model: str) -> str:
    """String versionada de `model` -> família do card. Sem match: a própria string.

    Fail-open de propósito: um modelo novo vira o seu próprio estrato em vez de
    derrubar o downsample.
    """
    nome = model or ""
    for prefixo, familia in FAMILIAS:
        if nome.startswith(prefixo):
            return familia
    return nome


#: Faixas de tamanho do texto. Prefixo numérico para a ordem lexicográfica das
#: chaves de estrato coincidir com a ordem natural (relatório e determinismo).
def len_bucket(texto: str) -> str:
    n = len(texto)
    if n < 120:
        return "1_lt120"
    if n <= 600:
        return "2_120a600"
    return "3_gt600"


def year_of(created_ts: str) -> str:
    """Ano do ISO-8601, ou `"????"` — nunca levanta (raw pode ter lixo)."""
    prefixo = (created_ts or "")[:4]
    return prefixo if prefixo.isdigit() else "????"


def strata_key(model: str, texto: str, created_ts: str) -> str:
    """Chave de estrato: família | faixa de tamanho | ano. Máx. 63 combinações."""
    return f"{family_of(model)}|{len_bucket(texto)}|{year_of(created_ts)}"


def _largest_remainder(pesos: Mapping[str, int], total: int) -> dict[str, int]:
    """Reparte `total` proporcionalmente a `pesos`, inteirizando por maior resto.

    Desempate pela chave ordenada: nenhum rng, nenhuma dependência da ordem de
    entrada do dicionário.
    """
    chaves = sorted(pesos)
    soma = sum(pesos[k] for k in chaves)
    if total <= 0 or soma <= 0:
        return dict.fromkeys(chaves, 0)
    exatos = {k: total * pesos[k] / soma for k in chaves}
    out = {k: int(exatos[k]) for k in chaves}
    faltam = total - sum(out.values())
    ordem = sorted(chaves, key=lambda k: (-(exatos[k] - int(exatos[k])), k))
    for k in ordem[:faltam]:
        out[k] += 1
    return out


def allocate(
    counts: Mapping[str, int],
    target: int = DOWNSAMPLE_TARGET,
    floor_cap: int = DOWNSAMPLE_FLOOR,
) -> dict[str, int]:
    """Quantas linhas tirar de cada estrato para somar EXATAMENTE `target`.

    Piso de `floor_cap` por estrato (ou tudo, se o estrato for menor) para que
    combinações raras — família nova, textos gigantes de 2023 — não sumam da
    amostra; o resto vai proporcional à sobra de cada estrato, com o cap em
    `counts[k]` redistribuído iterativamente.

    Determinístico e sem rng: quem sorteia *quais* linhas é o `downsample`.
    """
    chaves = sorted(counts)
    n = {k: int(counts[k]) for k in chaves}
    total = sum(n.values())
    if target < 0:
        raise ValueError(f"target negativo: {target}")
    if total < target:
        raise ValueError(
            f"pool insuficiente: {total} linhas para target={target}. "
            f"Suba EN_POOL_FRACTION (hoje {EN_POOL_FRACTION}) e re-rode só o modo en."
        )
    if target == 0:
        return dict.fromkeys(chaves, 0)

    alloc = {k: min(n[k], floor_cap) for k in chaves}
    if sum(alloc.values()) > target:
        # Estratos demais: nem os pisos cabem. Reparte os próprios pisos.
        return _largest_remainder(alloc, target)

    restante = target - sum(alloc.values())
    while restante > 0:
        livres = [k for k in chaves if alloc[k] < n[k]]
        if not livres:  # pragma: no cover - impossível com total >= target
            break
        sobra = {k: n[k] - alloc[k] for k in livres}
        add = _largest_remainder(sobra, min(restante, sum(sobra.values())))
        dado = 0
        for k in livres:
            quanto = min(add[k], n[k] - alloc[k])
            alloc[k] += quanto
            dado += quanto
        if dado == 0:  # trava numérica: distribui de um em um, em ordem de chave
            for k in chaves:
                if restante == 0:
                    break
                if alloc[k] < n[k]:
                    alloc[k] += 1
                    restante -= 1
                    dado += 1
            break
        restante -= dado

    if sum(alloc.values()) != target:  # pragma: no cover - invariante
        raise AssertionError(f"allocate somou {sum(alloc.values())}, esperado {target}")
    for k in chaves:
        if not 0 <= alloc[k] <= n[k]:  # pragma: no cover - invariante
            raise AssertionError(f"allocate estourou o estrato {k}: {alloc[k]} > {n[k]}")
    return alloc


def validate_state(
    estado: Mapping[str, Any],
    *,
    source: str,
    hf_id: str,
    language: str,
    fraction: float,
    max_rows: int | None,
) -> str | None:
    """`None` se o checkpoint casa com a configuração atual; senão, o motivo.

    Retomar com outra fração, outras colunas ou outro `--max-rows` produziria um
    parquet meio de cada — pior que recomeçar.
    """
    esperado: list[tuple[str, Any]] = [
        ("version", STATE_VERSION),
        ("source", source),
        ("hf_id", hf_id),
        ("split", SPLIT),
        ("language", language),
        ("fraction", fraction),
        ("max_rows", max_rows),
        ("columns", list(COLUMNS)),
    ]
    for chave, valor in esperado:
        atual = estado.get(chave)
        if chave == "columns":
            atual = list(atual or [])
        if atual != valor:
            return f"{chave}: checkpoint tem {atual!r}, execução atual quer {valor!r}"
    return None


# ---------------------------------------------------------------------------
# disco: state, parts, consolidação
# ---------------------------------------------------------------------------


def _state_path(nome: str) -> Path:
    return paths.RAW / "_checkpoints" / f"{nome}.state.json"


def _parts_dir(nome: str) -> Path:
    return paths.RAW / "_parts" / nome


def _part_path(nome: str, indice: int) -> Path:
    return _parts_dir(nome) / f"part_{indice:03d}.parquet"


def _rel(path: Path) -> str:
    try:
        return path.relative_to(paths.ROOT).as_posix()
    except ValueError:  # pragma: no cover - tmp_path nos testes
        return str(path)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def load_state(nome: str) -> dict[str, Any] | None:
    """Checkpoint do modo, ou `None`. JSON corrompido conta como ausente."""
    arquivo = _state_path(nome)
    if not arquivo.is_file():
        return None
    try:
        with arquivo.open(encoding="utf-8") as fh:
            estado = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"[{nome}] checkpoint ilegível ({exc}) - recomeçando do zero", flush=True)
        return None
    return estado if isinstance(estado, dict) else None


def save_state(nome: str, estado: Mapping[str, Any]) -> None:
    """Grava o state em JSON (nunca pickle), com `.tmp` + `os.replace`."""
    arquivo = _state_path(nome)
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    texto = json.dumps(dict(estado), ensure_ascii=False, indent=1, sort_keys=True)
    tmp = arquivo.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(texto)
    os.replace(tmp, arquivo)


def write_part(nome: str, indice: int, linhas: Sequence[dict[str, Any]]) -> Path:
    """Escreve um part-file com as 12 colunas do `RAW_SCHEMA`, atomicamente."""
    destino = _part_path(nome, indice)
    destino.parent.mkdir(parents=True, exist_ok=True)
    tabela = pa.Table.from_pylist(list(linhas), schema=RAW_SCHEMA)
    tmp = destino.with_suffix(".parquet.tmp")
    pq.write_table(tabela, tmp, compression="zstd")
    os.replace(tmp, destino)
    return destino


def _limpar(nome: str) -> None:
    """`--restart`: apaga state e parts do modo (listando), sem tocar nos finais."""
    alvos = [_state_path(nome), *sorted(_parts_dir(nome).glob("part_*.parquet"))]
    apagados = 0
    for alvo in alvos:
        if alvo.exists():
            print(f"[{nome}] --restart apagando {_rel(alvo)}", flush=True)
            alvo.unlink()
            apagados += 1
    if not apagados:
        print(f"[{nome}] --restart: nada a apagar", flush=True)


def _write_final(
    tabela: pa.Table,
    destino: Path,
    *,
    source: str,
    cfg: Mapping[str, Any],
    licenca: str,
) -> Path:
    """Grava um parquet final de `data/raw/`, atômico e **byte-determinístico**.

    Sem `pf_ingested_at` (o único metadado não determinístico do `write_raw`):
    re-rodar o downsample com o mesmo pool tem de produzir o MESMO arquivo, byte
    a byte, e um relógio no footer arruinaria isso.
    """
    tabela = tabela.replace_schema_metadata(
        {
            "pf_source": source,
            "pf_hf_id": str(cfg.get("hf_id", "")),
            "pf_license": licenca,
            "pf_attribution": str(cfg.get("attribution", "")),
            "pf_n_rows": str(tabela.num_rows),
        }
    )
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(".parquet.tmp")
    pq.write_table(tabela, tmp, compression="zstd")
    os.replace(tmp, destino)
    return destino


def consolidate(
    nome: str,
    destino_stem: str,
    n_parts: int,
    *,
    cfg: Mapping[str, Any],
) -> tuple[Path, int, int]:
    """Parts -> parquet final. Devolve (caminho, linhas, duplicatas removidas).

    Dedup por `source_id` (o `conversation_hash` é único por conversa) antes da
    ordenação: é a rede de segurança contra o part reescrito por uma retomada.
    Depois ordena por `source_id`, o que dá ordem total determinística —
    consolidar duas vezes produz o mesmo arquivo.
    """
    tabelas: list[pa.Table] = []
    for i in range(n_parts):
        arquivo = _part_path(nome, i)
        if not arquivo.is_file():
            raise SystemExit(
                f"[{nome}] part faltando: {_rel(arquivo)} (o state diz que existem "
                f"{n_parts}). Rode com --restart para refazer o passe."
            )
        tabelas.append(pq.read_table(arquivo, schema=RAW_SCHEMA))

    if tabelas:
        tabela = pa.concat_tables(tabelas)
    else:
        tabela = pa.Table.from_pylist([], schema=RAW_SCHEMA)

    brutas = tabela.num_rows
    if brutas:
        vistos: set[str] = set()
        manter: list[int] = []
        for i, sid in enumerate(tabela.column("source_id").to_pylist()):
            if sid in vistos:
                continue
            vistos.add(sid)
            manter.append(i)
        if len(manter) != brutas:
            tabela = tabela.take(pa.array(manter, type=pa.int64()))
        tabela = tabela.sort_by([("source_id", "ascending")])
    duplicatas = brutas - tabela.num_rows

    licenca = str(cfg.get("license", "unknown"))
    tabela = tabela.set_column(
        RAW_SCHEMA.get_field_index("license"),
        "license",
        pa.array([licenca] * tabela.num_rows, type=pa.string()),
    )
    destino = _write_final(
        tabela,
        paths.RAW / f"{destino_stem}.parquet",
        source=nome,
        cfg=cfg,
        licenca=licenca,
    )
    return destino, tabela.num_rows, duplicatas


# ---------------------------------------------------------------------------
# passe de streaming
# ---------------------------------------------------------------------------


def _row(modo: Modo, rec: Mapping[str, Any], texto: str) -> dict[str, Any]:
    """Registro do Hub -> linha do `RAW_SCHEMA`. Sem normalizar o texto (é s01)."""
    return make_row(
        source=modo.nome,
        source_id=rec["conversation_hash"],
        source_split=SPLIT,
        text_raw=texto,
        lang_source=rec["language"] or "",
        created_ts=to_iso(rec["timestamp"]),
        country=rec["country"] or "",
        # A string CRUA e versionada do modelo; quem agrega nas 7 famílias do
        # card é o `family_of`, no downsample.
        model_family=rec["model"] or "",
        # Sempre False neste release: as linhas tóxicas foram removidas antes da
        # publicação. Não é bug — ver docstring do módulo.
        nsfw_hint=bool(rec["toxic"]),
        native_category="",
        # Chaves já em ordem alfabética: o `dump_meta` não ordena, então esta é
        # a garantia de que o JSON sai byte-idêntico entre execuções.
        meta={
            "redacted": bool(rec["redacted"]),
            "state": rec["state"] or "",
            "turn": int(rec["turn"] or 0),
        },
    )


def _abrir(hf_id: str, nome: str, *, quieto: bool = False) -> Any:
    """`load_dataset` em streaming com pushdown de colunas + fail-fast de schema.

    `fragment_scan_options` existe só para segurar o tamanho de cada GET — ver
    `HTTP_RANGE_LIMIT`. Não muda a ORDEM nem o agrupamento dos exemplos, então um
    `state_dict` gravado sem ele continua válido (e vice-versa).
    """
    import pyarrow.dataset as pads
    from datasets import load_dataset

    ds = load_dataset(
        hf_id,
        split=SPLIT,
        streaming=True,
        columns=list(COLUMNS),
        fragment_scan_options=pads.ParquetFragmentScanOptions(
            cache_options=pa.CacheOptions(range_size_limit=HTTP_RANGE_LIMIT)
        ),
    )
    if quieto:
        return ds
    if not ds.features:
        # Features não resolvidas (repo sem metadados): segue, o extractor é
        # defensivo o bastante. Melhor um passe caro que um passe recusado.
        print(f"[{nome}] ATENCAO: ds.features veio vazio - schema não verificado", flush=True)
        return ds
    require_columns(ds.features, COLUMNS, nome)
    extras = sorted(set(ds.features) - set(COLUMNS))
    if extras:
        print(
            f"[{nome}] ATENCAO: columns= não foi aplicado (vieram também {extras}). "
            f"O passe funciona, mas custa ~40% mais banda.",
            flush=True,
        )
    print(f"[{nome}] features: {sorted(ds.features)}", flush=True)
    print(f"[{nome}] conversation: {ds.features.get('conversation')}", flush=True)
    return ds


def _eta(scanned: int, decorrido: float, max_rows: int | None) -> str:
    alvo = min(TOTAL_ROWS, max_rows) if max_rows else TOTAL_ROWS
    if scanned <= 0 or decorrido <= 0 or scanned >= alvo:
        return "-"
    restante = (alvo - scanned) * decorrido / scanned
    return f"{restante / 60:.0f} min"


def _passe(nome: str, modo: Modo, cfg: Mapping[str, Any], args: argparse.Namespace) -> int:
    """O passe de rede: varre o split inteiro, escreve parts, consolida."""
    max_rows: int | None = getattr(args, "max_rows", None)
    hf_id = str(cfg["hf_id"])

    if getattr(args, "restart", False):
        _limpar(nome)

    estado = load_state(nome)
    if estado is not None:
        motivo = validate_state(
            estado,
            source=nome,
            hf_id=hf_id,
            language=modo.language,
            fraction=modo.fraction,
            max_rows=max_rows,
        )
        if motivo is not None:
            print(f"[{nome}] checkpoint incompatível -> {motivo}", flush=True)
            print(f"[{nome}] rode `pf ingest {nome} --restart` para refazer o passe.", flush=True)
            return 1

    if estado is None:
        estado = {
            "version": STATE_VERSION,
            "source": nome,
            "hf_id": hf_id,
            "split": SPLIT,
            "columns": list(COLUMNS),
            "language": modo.language,
            "fraction": modo.fraction,
            "max_rows": max_rows,
            "scanned": 0,
            "kept": 0,
            "dropped_empty_user": 0,
            "next_part": 0,
            "elapsed_s": 0.0,
            "complete": False,
            "interrupted": False,
            "hf_state": None,
            "laps": [],
            "updated_at": _now(),
        }

    if estado.get("complete"):
        print(
            f"[{nome}] passe já completo ({estado['scanned']} varridas, "
            f"{estado['kept']} mantidas) - só re-consolidando os parts.",
            flush=True,
        )
        return _finalizar(nome, modo, cfg, estado, max_rows)

    print(
        f"[{nome}] filtro: language == {modo.language!r}"
        + (f" + hash_gate({modo.fraction})" if modo.fraction < 1.0 else "")
        + (f" | --max-rows {max_rows} (linhas VARRIDAS)" if max_rows else ""),
        flush=True,
    )

    # Cada tentativa recomeça do ÚLTIMO CHECKPOINT, nunca do zero: o buffer em
    # memória é descartado (aquelas linhas serão re-varridas) e o índice do part
    # continua vindo do state. O contador só zera quando um checkpoint novo
    # entra no disco, para que uma falha determinística desista em vez de girar.
    tentativa = 0
    while True:
        marca = int(estado["scanned"])
        try:
            status = _varrer(nome, modo, hf_id, estado, max_rows, quieto=tentativa > 0)
        except _erros_de_rede() as exc:
            tentativa = tentativa + 1 if int(estado["scanned"]) == marca else 1
            if tentativa > MAX_TENTATIVAS_REDE:
                print(
                    f"[{nome}] rede falhou {MAX_TENTATIVAS_REDE}x sem avançar nenhum "
                    f"checkpoint - desistindo em scanned={estado['scanned']}. "
                    f"Os parts estão salvos: retome com o MESMO comando.",
                    flush=True,
                )
                raise
            espera = min(60, 5 * 2 ** (tentativa - 1))
            print(
                f"[{nome}] rede caiu ({type(exc).__name__}: {str(exc)[:160]}). "
                f"Tentativa {tentativa}/{MAX_TENTATIVAS_REDE} em {espera}s, "
                f"reabrindo o stream em scanned={estado['scanned']}.",
                flush=True,
            )
            time.sleep(espera)
            continue
        if status == "interrompido":
            return 1
        break

    return _finalizar(nome, modo, cfg, estado, max_rows)


def _erros_de_rede() -> tuple[type[BaseException], ...]:
    """Exceções que valem reabrir o stream. Montada tarde: `requests` é do passe."""
    erros: list[type[BaseException]] = [OSError]  # ConnectionError/TimeoutError entram aqui
    try:
        import requests

        erros.append(requests.exceptions.RequestException)
    except ImportError:  # pragma: no cover - requests vem com huggingface_hub
        pass
    try:
        import urllib3

        erros.append(urllib3.exceptions.HTTPError)
    except ImportError:  # pragma: no cover
        pass
    return tuple(erros)


def _varrer(
    nome: str,
    modo: Modo,
    hf_id: str,
    estado: dict[str, Any],
    max_rows: int | None,
    *,
    quieto: bool = False,
) -> str:
    """UMA tentativa de varredura a partir do state. `"ok"` ou `"interrompido"`.

    Mutar `estado` é o contrato: quem chama usa `estado["scanned"]` para saber se
    a tentativa avançou algum checkpoint antes de morrer.
    """
    ds = _abrir(hf_id, nome, quieto=quieto)
    hf_state = estado.get("hf_state")
    if hf_state:
        ds.load_state_dict(hf_state)
        print(
            f"[{nome}] RETOMANDO do checkpoint: {estado['scanned']} varridas, "
            f"{estado['kept']} mantidas, próximo part {estado['next_part']:03d}",
            flush=True,
        )
    elif not quieto:
        print(f"[{nome}] começando do zero (nenhum checkpoint válido)", flush=True)

    scanned = int(estado["scanned"])
    kept = int(estado["kept"])
    vazias = int(estado["dropped_empty_user"])
    proximo_part = int(estado["next_part"])
    buffer: list[dict[str, Any]] = []
    inicio = time.perf_counter()
    base_elapsed = float(estado.get("elapsed_s", 0.0))
    t_lap = inicio
    validado = False

    def flush(final: bool) -> None:
        nonlocal proximo_part, t_lap
        if buffer:
            caminho = write_part(nome, proximo_part, buffer)  # PART PRIMEIRO
            proximo_part += 1
            buffer.clear()
        else:
            caminho = None
        agora = time.perf_counter()
        estado.update(
            scanned=scanned,
            kept=kept,
            dropped_empty_user=vazias,
            next_part=proximo_part,
            hf_state=ds.state_dict(),  # depois do part no disco: ordem obrigatória
            elapsed_s=base_elapsed + (agora - inicio),
            complete=final,
            interrupted=False,
            updated_at=_now(),
        )
        laps = list(estado.get("laps") or [])
        laps.append({"scanned": scanned, "kept": kept, "s": round(agora - t_lap, 1)})
        estado["laps"] = laps[-256:]
        save_state(nome, estado)  # STATE DEPOIS
        decorrido = base_elapsed + (agora - inicio)
        print(
            f"[{nome}] ckpt scanned={scanned} kept={kept} part={caminho.name if caminho else '-'}"
            f" lap={agora - t_lap:.0f}s total={decorrido / 60:.1f}min"
            f" eta={_eta(scanned, decorrido, max_rows)}",
            flush=True,
        )
        t_lap = agora

    try:
        for rec in ds:
            scanned += 1
            if not validado:
                # Prova, na primeira linha, que o state serializa em JSON — em vez
                # de descobrir isso 50k linhas (e alguns minutos) adiante.
                json.dumps(ds.state_dict())
                validado = True
            if rec["language"] == modo.language:
                ch = rec["conversation_hash"] or ""
                if modo.fraction >= 1.0 or hash_gate(ch, modo.fraction):
                    texto = first_user_text(rec["conversation"])
                    if texto is None:
                        vazias += 1
                    else:
                        buffer.append(_row(modo, rec, texto))
                        kept += 1
            if scanned % PROGRESS_EVERY == 0 and scanned % CHECKPOINT_EVERY != 0:
                decorrido = base_elapsed + (time.perf_counter() - inicio)
                print(
                    f"[{nome}] .. scanned={scanned} kept={kept} "
                    f"({100.0 * scanned / TOTAL_ROWS:.1f}%) eta={_eta(scanned, decorrido, max_rows)}",
                    flush=True,
                )
            if scanned % CHECKPOINT_EVERY == 0:
                flush(final=False)
            if max_rows is not None and scanned >= max_rows:
                break
    except KeyboardInterrupt:
        # Kill -9 perde, no máximo, a janela de 50k linhas desde o último part.
        # Ctrl+C não perde nada: o buffer vira part antes de sair.
        print(f"\n[{nome}] Ctrl+C - salvando checkpoint antes de sair...", flush=True)
        flush(final=False)
        estado["interrupted"] = True
        save_state(nome, estado)
        print(
            f"[{nome}] interrompido em scanned={scanned} kept={kept}. "
            f"Retome com o MESMO comando (sem --restart).",
            flush=True,
        )
        return "interrompido"

    flush(final=True)
    return "ok"


def _finalizar(
    nome: str,
    modo: Modo,
    cfg: Mapping[str, Any],
    estado: Mapping[str, Any],
    max_rows: int | None,
) -> int:
    """Consolida os parts e confere as faixas de sanidade (WARN, nunca erro)."""
    destino, linhas, duplicatas = consolidate(
        nome, modo.destino, int(estado["next_part"]), cfg=cfg
    )
    tamanho = destino.stat().st_size
    print(
        f"[{nome}] consolidado: {linhas} linhas ({duplicatas} duplicatas removidas, "
        f"{estado['dropped_empty_user']} sem texto de usuário) -> {_rel(destino)} "
        f"({tamanho} bytes)",
        flush=True,
    )
    print(
        f"[{nome}] varridas={estado['scanned']} mantidas={estado['kept']} "
        f"wall-clock={float(estado.get('elapsed_s', 0.0)) / 60:.1f} min",
        flush=True,
    )

    # Guards: sempre depois de gravar. Descartar horas de download por causa de
    # uma faixa estimada seria o pior negócio possível — ver Riscos do briefing.
    if max_rows is not None:
        print(f"[{nome}] guard de contagem pulado (--max-rows {max_rows})", flush=True)
    elif modo.nome == "wildchat_pt":
        lo, hi = int(cfg.get("expected_min", 0)), int(cfg.get("expected_max", 10**9))
        if not lo <= linhas <= hi:
            print(
                f"[{nome}] WARN: {linhas} linhas fora da faixa esperada [{lo}-{hi}]. "
                f"A faixa veio de extrapolação de janela parcial - inspecione uma "
                f"amostra com `pf report raw --sources {nome}` ANTES de mexer no toml. "
                f"Os parts continuam em disco: não é preciso re-baixar.",
                flush=True,
            )
    elif not POOL_MIN <= linhas <= POOL_MAX:
        print(
            f"[{nome}] WARN: pool de {linhas} linhas fora de [{POOL_MIN}-{POOL_MAX}]. "
            f"Abaixo do piso, suba EN_POOL_FRACTION (hoje {EN_POOL_FRACTION}) e "
            f"re-rode só este modo com --restart.",
            flush=True,
        )
    if modo.nome == "wildchat_en":
        print(
            f"[{nome}] próximo passo (sem rede): `pf ingest wildchat-en --downsample`",
            flush=True,
        )
    return 0


# ---------------------------------------------------------------------------
# downsample estratificado (offline)
# ---------------------------------------------------------------------------


def _strata_report(
    counts: Mapping[str, int], alloc: Mapping[str, int], total_pool: int
) -> str:
    linhas = [
        "# wildchat_en - downsample estratificado (familia | tamanho | ano)",
        f"# pool={total_pool}  target={DOWNSAMPLE_TARGET}  floor={DOWNSAMPLE_FLOOR}  seed={DOWNSAMPLE_SEED}",
        f"# fracao efetiva = {DOWNSAMPLE_TARGET / total_pool:.4f} do pool"
        if total_pool
        else "# pool vazio",
        "",
        f"{'estrato':<44}{'disponiveis':>12}{'alocados':>10}{'%':>8}",
        "-" * 74,
    ]
    for chave in sorted(counts):
        n, a = counts[chave], alloc.get(chave, 0)
        linhas.append(f"{chave:<44}{n:>12}{a:>10}{100.0 * a / n:>7.1f}%")
    linhas += [
        "-" * 74,
        f"{'TOTAL':<44}{sum(counts.values()):>12}{sum(alloc.values()):>10}",
        f"# estratos: {len(counts)} ({sum(1 for k in counts if alloc.get(k, 0) == counts[k])} levados inteiros)",
    ]
    return "\n".join(linhas) + "\n"


def downsample(nome: str, cfg: Mapping[str, Any]) -> int:
    """Pool -> 100.000 linhas estratificadas. Zero rede, byte-determinístico.

    Ordem que garante o determinismo: pool ordenado por `source_id` ANTES de
    montar os estratos (a seleção deixa de depender da ordem física do parquet),
    estratos percorridos em ordem lexicográfica, um único `default_rng(42)` para
    a rodada inteira.
    """
    import numpy as np

    origem = paths.RAW / f"{MODOS[nome].destino}.parquet"
    if not origem.is_file():
        raise SystemExit(
            f"[{nome}] pool ausente: {_rel(origem)}. Rode `pf ingest wildchat-en` primeiro."
        )

    inicio = time.perf_counter()
    tabela = pq.read_table(origem, schema=RAW_SCHEMA).sort_by([("source_id", "ascending")])
    total = tabela.num_rows
    print(f"[{nome}] pool: {total} linhas em {_rel(origem)}", flush=True)

    modelos = tabela.column("model_family").to_pylist()
    textos = tabela.column("text_raw").to_pylist()
    criados = tabela.column("created_ts").to_pylist()
    estratos: dict[str, list[int]] = {}
    for i in range(total):
        estratos.setdefault(strata_key(modelos[i], textos[i] or "", criados[i] or ""), []).append(i)
    counts = {k: len(v) for k, v in estratos.items()}
    print(f"[{nome}] {len(counts)} estratos (familia x tamanho x ano)", flush=True)

    alloc = allocate(counts, DOWNSAMPLE_TARGET, DOWNSAMPLE_FLOOR)

    rng = np.random.default_rng(DOWNSAMPLE_SEED)
    escolhidos: list[int] = []
    for chave in sorted(estratos):  # ordem lexicográfica: o rng é um só
        idxs = estratos[chave]  # já em ordem de source_id (a tabela foi ordenada)
        k = alloc[chave]
        if k <= 0:
            continue
        posicoes = rng.choice(len(idxs), size=k, replace=False)
        escolhidos.extend(idxs[p] for p in sorted(posicoes.tolist()))

    escolhidos.sort()  # índices na tabela já ordenada => saída ordenada por source_id
    amostra = tabela.take(pa.array(escolhidos, type=pa.int64()))
    if amostra.num_rows != DOWNSAMPLE_TARGET:  # pragma: no cover - invariante
        raise SystemExit(f"[{nome}] downsample deu {amostra.num_rows}, esperado {DOWNSAMPLE_TARGET}")

    destino = _write_final(
        amostra,
        paths.RAW / f"{nome}.parquet",
        source=nome,
        cfg=cfg,
        licenca=str(cfg.get("license", "unknown")),
    )
    relatorio = _strata_report(counts, alloc, total)
    alvo_txt = paths.RAW / f"{nome}.strata.txt"
    tmp = alvo_txt.with_suffix(".txt.tmp")
    with tmp.open("w", encoding="utf-8") as fh:  # nunca `>` do PowerShell (UTF-16)
        fh.write(relatorio)
    os.replace(tmp, alvo_txt)

    print(relatorio, flush=True)
    print(
        f"[{nome}] {amostra.num_rows} linhas -> {_rel(destino)} "
        f"({destino.stat().st_size} bytes, {time.perf_counter() - inicio:.1f} s)",
        flush=True,
    )
    print(f"[{nome}] relatório de estratos -> {_rel(alvo_txt)}", flush=True)
    return 0


# ---------------------------------------------------------------------------
# entrypoint chamado pelo `pf ingest`
# ---------------------------------------------------------------------------


def run_ingest(source_name: str, cfg: Mapping[str, Any], args: argparse.Namespace) -> int:
    """`pf ingest wildchat-pt|wildchat-en [--max-rows N] [--restart] [--downsample]`.

    Retomar é o DEFAULT: existindo um checkpoint compatível, o passe continua de
    onde parou sem nenhuma flag. `--resume` é só o alias explícito disso.
    """
    modo = MODOS[source_name]
    if not cfg.get("enabled", False):
        print(f"[{source_name}] fonte desabilitada em config/sources.toml (enabled = false)")
        return 2

    if getattr(args, "downsample", False):
        if source_name != "wildchat_en":
            print(f"[{source_name}] --downsample só existe para wildchat-en", flush=True)
            return 2
        if getattr(args, "max_rows", None) is not None or getattr(args, "restart", False):
            print(
                "[pf] --downsample não combina com --max-rows/--restart "
                "(ele só relê o pool que já está em disco)",
                flush=True,
            )
            return 2
        return downsample(source_name, cfg)

    return _passe(source_name, modo, cfg, args)


__all__ = [
    "CHECKPOINT_EVERY",
    "COLUMNS",
    "DOWNSAMPLE_TARGET",
    "EN_POOL_FRACTION",
    "MODOS",
    "TOTAL_ROWS",
    "allocate",
    "consolidate",
    "downsample",
    "family_of",
    "first_user_text",
    "hash_gate",
    "len_bucket",
    "load_state",
    "run_ingest",
    "save_state",
    "strata_key",
    "validate_state",
    "write_part",
    "year_of",
]
