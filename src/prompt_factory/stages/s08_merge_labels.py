"""s08 — funde os rótulos da campanha com os rótulos nativos das fontes.

Entradas: ``labeling/labels/*.jsonl`` (o que os agentes produziram, um arquivo
por lote ``done``), o gabarito revisado à mão no ``manifest.gold`` e os rótulos
nativos de ``no_robots``/``dolly`` já presentes no universo como
``native_category``. Saída: ``final/seed_labels.parquet``.

**Três origens, uma precedência.** ``manual`` (o humano revisou) > ``agent``
(rotulou os 4 eixos, peso 1.0) > ``native`` (categoria da própria fonte mapeada
por ``labeling/mappings/``, peso 0.5, **só ``task_type``**). Conflito entre
agente e nativo o agente ganha — ele viu o texto e decidiu pelos quatro eixos,
enquanto o nativo é um mapeamento de categoria genérica feito uma vez. O peso
não desempata sozinho: quem desempata é a precedência, e o peso viaja para o
M7 pesar cada linha na regressão.

**Rótulo de agente sobre item de calibração é descartado.** Os 3 ouros
escondidos em cada lote existem para MEDIR o agente, não para rotular: cada um
reaparece em ~4,7 lotes, e mantê-los seria contar 465 rótulos redundantes que,
por cima, competiriam com o gabarito humano do mesmo uid.

**Peso 0.5 do nativo não é palpite**: é o desconto de um rótulo que ninguém leu
o texto para dar. Ele mora em ``labeling/mappings/*.json`` (chave ``weight``),
não aqui.

O relatório final cruza as duas origens nos uids que têm as duas: se uma
categoria nativa discorda do agente em mais de ``[labeling] mapping_warn``, o
mapeamento é suspeito e a linha sai marcada. **Nada é removido automaticamente**
— corrigir um mapeamento é editar o JSON e re-rodar, com um humano no meio.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from ..config import get
from ..labeling_io import (
    BATCH_CALIBRACAO,
    DONE,
    LabelingPaths,
    carregar_manifest,
    painel,
)
from ..schema import DOMAINS, TASK_TYPES, load_taxonomy
from . import (
    SEED_LABELS,
    UNIVERSE,
    Cronometro,
    StageConfig,
    escrever_tabela,
    exigir,
    imprimir_funil,
    rel,
)

ESTAGIO = "s08"

#: Colunas da saída, na ordem.
COLUNAS: tuple[str, ...] = (
    "uid",
    "task_type",
    "domain",
    "quality",
    "nsfw",
    "label_method",
    "label_weight",
    "batch_id",
)

#: Precedência das origens: menor ganha o uid.
PRECEDENCIA: dict[str, int] = {"manual": 0, "agent": 1, "native": 2}

#: Peso de um rótulo produzido lendo o texto (agente ou humano).
PESO_CHEIO = 1.0


def limite_discordancia() -> float:
    """Acima disto a discordância nativo x agente vira aviso de mapeamento
    suspeito (``[labeling] mapping_warn``)."""
    return float(get("labeling", "mapping_warn", default=0.30))


def carregar_mapeamentos(diretorio: Path) -> dict[str, dict[str, Any]]:
    """Lê ``labeling/mappings/*.json`` e valida os alvos contra a taxonomia.

    Alvo fora de ``TASK_TYPES`` é erro fatal: é sempre erro de digitação no
    JSON, e deixar passar produziria milhares de linhas com um ``task_type``
    que nenhum outro estágio conhece.
    """
    mapas: dict[str, dict[str, Any]] = {}
    for arquivo in sorted(diretorio.glob("*.json")):
        with arquivo.open(encoding="utf-8") as fh:
            spec = json.load(fh)
        fonte = str(spec.get("source") or arquivo.stem)
        invalidos = [
            f"{k!r}->{v!r}"
            for k, v in dict(spec.get("map", {})).items()
            if v is not None and v not in TASK_TYPES
        ]
        if invalidos:
            raise SystemExit(
                f"[{ESTAGIO}] {rel(arquivo)}: alvo fora da taxonomia: {', '.join(invalidos)}"
            )
        if spec.get("axis", "task_type") != "task_type":
            raise SystemExit(
                f"[{ESTAGIO}] {rel(arquivo)}: só o eixo task_type é suportado no M5"
            )
        mapas[fonte] = spec
    return mapas


def _ler_rotulos_do_lote(arquivo: Path) -> list[dict[str, Any]]:
    linhas = []
    for bruta in arquivo.read_text(encoding="utf-8").splitlines():
        if bruta.strip():
            linhas.append(json.loads(bruta))
    return linhas


def coletar_agentes(
    lp: LabelingPaths, manifest: dict[str, Any], estrito: bool
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Rótulos dos lotes ``done``, já sem os itens de calibração."""
    stats: Counter[str] = Counter()
    calibracao = set(manifest.get("calibration", {}).get("uids", []))
    saida: list[dict[str, Any]] = []

    for batch_id, registro in sorted(manifest.get("batches", {}).items()):
        if registro.get("status") != DONE:
            stats[f"lote {registro.get('status', '?')}"] += 1
            continue
        arquivo = lp.rotulos(batch_id)
        if not arquivo.is_file():
            stats["lote done sem arquivo de rótulos"] += 1
            if estrito:
                raise SystemExit(
                    f"[{ESTAGIO}] {batch_id} está done mas {rel(arquivo)} não existe"
                )
            continue
        stats["lotes lidos"] += 1
        for linha in _ler_rotulos_do_lote(arquivo):
            uid = str(linha["uid"])
            if uid in calibracao:
                stats["descartados (item de calibração)"] += 1
                continue
            saida.append(
                {
                    "uid": uid,
                    "task_type": str(linha["task_type"]),
                    "domain": str(linha["domain"]),
                    "quality": int(linha["quality"]),
                    "nsfw": bool(linha["nsfw"]),
                    "label_method": "agent",
                    "label_weight": PESO_CHEIO,
                    "batch_id": batch_id,
                }
            )
    return saida, stats


def coletar_ouro(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Os 100 itens revisados à mão — a única origem que ninguém questiona."""
    return [
        {
            "uid": uid,
            "task_type": str(rotulo["task_type"]),
            "domain": str(rotulo["domain"]),
            "quality": int(rotulo["quality"]),
            "nsfw": bool(rotulo["nsfw"]),
            "label_method": "manual",
            "label_weight": PESO_CHEIO,
            "batch_id": BATCH_CALIBRACAO,
        }
        for uid, rotulo in sorted(dict(manifest.get("gold") or {}).items())
    ]


def coletar_nativos(
    universo: Path, mapas: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Categoria nativa da fonte → ``task_type``, com o peso do mapeamento."""
    import pyarrow.parquet as pq

    stats: Counter[str] = Counter()
    saida: list[dict[str, Any]] = []
    if not mapas:
        return saida, stats

    tabela = pq.read_table(universo, columns=["uid", "source", "native_category"])
    for uid, fonte, categoria in zip(
        tabela.column("uid").to_pylist(),
        tabela.column("source").to_pylist(),
        tabela.column("native_category").to_pylist(),
        strict=True,
    ):
        spec = mapas.get(str(fonte or ""))
        if spec is None or not categoria:
            continue
        mapa = dict(spec.get("map", {}))
        if str(categoria) not in mapa:
            stats[f"{fonte}: categoria fora do mapeamento ({categoria})"] += 1
            continue
        alvo = mapa[str(categoria)]
        if alvo is None:
            stats[f"{fonte}: categoria sem correspondência ({categoria})"] += 1
            continue
        saida.append(
            {
                "uid": str(uid),
                "task_type": str(alvo),
                "domain": None,
                "quality": None,
                "nsfw": None,
                "label_method": "native",
                "label_weight": float(spec.get("weight", 0.5)),
                "batch_id": None,
            }
        )
    return saida, stats


def resolver(
    candidatos: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Um rótulo por uid: precedência primeiro, primeira ocorrência no empate.

    ``candidatos`` chega na ordem de coleta (manual, agente, nativo) e a ordem
    dentro de cada origem é estável — logo o resultado é determinístico sem
    depender de nenhum critério escondido.
    """
    stats: Counter[str] = Counter()
    melhor: dict[str, dict[str, Any]] = {}
    for linha in candidatos:
        uid = linha["uid"]
        atual = melhor.get(uid)
        if atual is None:
            melhor[uid] = linha
            continue
        rank_novo = PRECEDENCIA[linha["label_method"]]
        rank_atual = PRECEDENCIA[atual["label_method"]]
        if rank_novo < rank_atual:
            stats[f"conflito {atual['label_method']} x {linha['label_method']}"] += 1
            melhor[uid] = linha
        elif rank_novo > rank_atual:
            stats[f"conflito {atual['label_method']} x {linha['label_method']}"] += 1
        else:
            stats[f"duplicata em {linha['label_method']} (fica a 1ª)"] += 1
    return [melhor[uid] for uid in sorted(melhor)], stats


def cruzar(
    agentes: list[dict[str, Any]],
    universo: Path,
    mapas: dict[str, dict[str, Any]],
) -> list[list[Any]]:
    """Matriz categoria nativa x task_type do agente, nos uids sobrepostos."""
    import pyarrow.parquet as pq

    if not mapas or not agentes:
        return []
    por_uid = {linha["uid"]: linha["task_type"] for linha in agentes}
    tabela = pq.read_table(universo, columns=["uid", "source", "native_category"])
    contagem: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for uid, fonte, categoria in zip(
        tabela.column("uid").to_pylist(),
        tabela.column("source").to_pylist(),
        tabela.column("native_category").to_pylist(),
        strict=True,
    ):
        if str(fonte or "") not in mapas or not categoria:
            continue
        do_agente = por_uid.get(str(uid))
        if do_agente is None:
            continue
        contagem[(str(fonte), str(categoria))][do_agente] += 1

    limite = limite_discordancia()
    linhas: list[list[Any]] = []
    for (fonte, categoria), quem in sorted(contagem.items()):
        alvo = dict(mapas[fonte].get("map", {})).get(categoria)
        total = sum(quem.values())
        acertos = quem.get(alvo, 0) if alvo else 0
        taxa = acertos / total if total else 0.0
        aviso = ""
        if alvo is None:
            aviso = "sem mapeamento (só medição)"
        elif 1 - taxa > limite:
            aviso = "MAPEAMENTO SUSPEITO"
        campeao, n_campeao = quem.most_common(1)[0]
        linhas.append(
            [
                fonte,
                categoria,
                alvo or "-",
                total,
                f"{taxa:.0%}",
                f"{campeao} ({n_campeao})",
                aviso,
            ]
        )
    return linhas


def run(cfg: StageConfig) -> int:
    """Funde as origens e grava ``final/seed_labels.parquet``. 0 = sucesso."""
    import pyarrow as pa

    relogio = Cronometro(ESTAGIO)
    load_taxonomy()
    cfg.preparar_dirs()
    universo = cfg.caminho(UNIVERSE)
    exigir(universo, "pf run s01-s06")
    lp = LabelingPaths(cfg.labeling_dir)
    manifest = carregar_manifest(lp)
    destino = cfg.caminho(SEED_LABELS)
    estrito = bool(cfg.strict)

    mapas = carregar_mapeamentos(cfg.labeling_dir / "mappings")
    ouro = coletar_ouro(manifest)
    agentes, stats_agente = coletar_agentes(lp, manifest, estrito)
    nativos, stats_nativo = coletar_nativos(universo, mapas)

    final, stats_conflito = resolver([*ouro, *agentes, *nativos])
    if not final:
        print(
            f"[{ESTAGIO}] nenhum rótulo para consolidar — rode a campanha "
            "(`pf labels next` / `pf labels submit`) antes"
        )
        return 1

    schema = pa.schema(
        [
            pa.field("uid", pa.string(), nullable=False),
            pa.field("task_type", pa.string(), nullable=False),
            pa.field("domain", pa.string(), nullable=True),
            # int8 nullable como no universo: passar por pandas viraria float64
            # e o 3 sairia 3.0 no export.
            pa.field("quality", pa.int8(), nullable=True),
            pa.field("nsfw", pa.bool_(), nullable=True),
            pa.field("label_method", pa.string(), nullable=False),
            pa.field("label_weight", pa.float32(), nullable=False),
            pa.field("batch_id", pa.string(), nullable=True),
        ]
    )
    escrever_tabela(
        pa.table(
            {col: [linha[col] for linha in final] for col in COLUNAS}, schema=schema
        ),
        destino,
    )

    # --- relatório ----------------------------------------------------------
    por_metodo = Counter(linha["label_method"] for linha in final)
    imprimir_funil(
        ESTAGIO,
        ("origem", "rótulos", "peso"),
        [
            ["manual (ouro revisado)", por_metodo.get("manual", 0), PESO_CHEIO],
            ["agent (campanha)", por_metodo.get("agent", 0), PESO_CHEIO],
            [
                "native (mapeado)",
                por_metodo.get("native", 0),
                ", ".join(sorted({str(m.get("weight", 0.5)) for m in mapas.values()})) or "-",
            ],
        ],
        ["TOTAL", len(final), ""],
    )
    for rotulo, contador in (
        ("lotes", stats_agente),
        ("nativos", stats_nativo),
        ("conflitos", stats_conflito),
    ):
        if contador:
            imprimir_funil(
                ESTAGIO,
                (rotulo, "n"),
                sorted(contador.items(), key=lambda kv: (-kv[1], kv[0]))[:12],
            )

    tarefas = Counter(linha["task_type"] for linha in final)
    imprimir_funil(
        ESTAGIO,
        ("task_type", "n"),
        [[t, tarefas.get(t, 0)] for t in TASK_TYPES if tarefas.get(t)],
    )
    dominios = Counter(linha["domain"] for linha in final if linha["domain"])
    if dominios:
        imprimir_funil(
            ESTAGIO,
            ("domain", "n"),
            [[d, dominios.get(d, 0)] for d in DOMAINS if dominios.get(d)],
        )
    qualidades = Counter(linha["quality"] for linha in final if linha["quality"] is not None)
    nsfw = Counter(linha["nsfw"] for linha in final if linha["nsfw"] is not None)
    if qualidades:
        imprimir_funil(
            ESTAGIO,
            ("quality", "n"),
            [[q, qualidades[q]] for q in sorted(qualidades)],
        )
        imprimir_funil(ESTAGIO, ("nsfw", "n"), [[k, nsfw[k]] for k in sorted(nsfw, key=str)])

    cruzada = cruzar(agentes, universo, mapas)
    if cruzada:
        imprimir_funil(
            ESTAGIO,
            ("fonte", "categoria nativa", "alvo", "n", "acerto", "mais votado", "aviso"),
            cruzada,
        )

    p = painel(lp, manifest)
    medio = p["agreement_medio"]
    print(
        f"[{ESTAGIO}] campanha: {p['contagem'].get(DONE, 0)}/{p['n_lotes']} lotes done, "
        f"agreement médio "
        + (f"{medio:.2f}" if medio is not None else "não medido (sem ouro)")
        + f", {len(p['lotes_baixos'])} lote(s) abaixo de {p['agreement_min']:.2f}"
    )
    print(f"[{ESTAGIO}] {len(final)} rótulos -> {rel(destino)}")
    relogio.fim()
    return 0


__all__ = [
    "COLUNAS",
    "ESTAGIO",
    "PESO_CHEIO",
    "PRECEDENCIA",
    "carregar_mapeamentos",
    "coletar_agentes",
    "coletar_nativos",
    "coletar_ouro",
    "cruzar",
    "limite_discordancia",
    "resolver",
    "run",
]
