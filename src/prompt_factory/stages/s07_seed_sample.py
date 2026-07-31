"""s07 — amostra-semente estratificada por fonte x faixa de tamanho.

Entrada ``final/universe.parquet`` → saídas ``labeling/seed/seed.parquet`` (os
12.000 itens sorteados), ``labeling/seed/strata.txt`` (o relatório de estratos,
que é o que se lê para *aceitar* a amostra) e, via ``labeling_io.montar_lotes``,
os lotes em ``labeling/batches/`` mais o ``labeling/manifest.json``.

**Por que estratificar.** Sortear 12.000 uniformemente do universo entregaria
uma semente que é quase só WildChat, quase só prompt longo, e um classificador
treinado nela erraria feio em tudo que é curto ou vem de fonte pequena. As
cotas por fonte moram em ``[seed.allocation.pt|en]`` e as faixas de tamanho em
``[seed] bucket_edges`` — thresholds em config, nunca aqui, senão a amostra
deixa de ser replayável.

**Déficit e redistribuição.** Fonte com menos linhas que a cota (o ``oasst``
tem 286 no universo, contra 600 de cota) trava no que tem, e o que sobra é
redistribuído entre as não-travadas **proporcionalmente às cotas planejadas**,
por maior-resto, com a ordem do ``settings.toml`` como desempate. O processo
repete até nenhuma fonte estar acima do disponível, e cada rodada vira uma
linha no ``strata.txt``: quem cedeu quanto para quem é informação que se olha
uma vez e se guarda para sempre.

**Determinismo.** Um único ``default_rng([seed] rng_seed)`` e uma sequência
fixa de sorteios: calibração primeiro, depois idiomas (pt, en), depois fontes
na ordem do TOML, depois faixas. Dentro de cada estrato os uids entram
**ordenados** antes do ``rng.choice`` — a ordem em que o pyarrow devolveu as
linhas não pode influenciar quem é sorteado, senão a semente muda quando o
parquet é reescrito com o mesmo conteúdo.

**A calibração sai de dentro da semente**, não ao lado: são 100 dos 12.000
itens, que o humano revisa e que viram o gabarito. Os 11.900 restantes é que
viram lotes de 77. Assim todo item da semente termina com rótulo — 100 humanos,
11.900 de agente — e não sobra nenhum item pago e não usado.
"""

from __future__ import annotations

from collections import Counter
from itertools import pairwise
from typing import Any

from ..config import get
from ..labeling_io import LabelingPaths, escrever_texto_atomico, montar_lotes
from ..schema import load_taxonomy
from . import (
    SEED_PARQUET,
    SEED_STRATA,
    UNIVERSE,
    Cronometro,
    StageConfig,
    escrever_tabela,
    exigir,
    imprimir_funil,
    rel,
)

ESTAGIO = "s07"

#: Colunas lidas do universo (pushdown: o universo tem 27 e ``text_raw`` sozinho
#: é a maior delas).
COLUNAS_LIDAS: tuple[str, ...] = ("uid", "text", "lang", "source", "n_chars", "native_category")

#: Colunas da semente. ``bucket`` viaja junto porque é o estrato que o relatório
#: e a auditoria do M7 vão querer cruzar sem recalcular fronteiras.
COLUNAS_SEED: tuple[str, ...] = (*COLUNAS_LIDAS, "bucket")


def faixas(edges: list[int]) -> list[str]:
    """Nomes das faixas a partir das fronteiras: ``[120, 600]`` → 3 nomes."""
    nomes = [f"<{edges[0]}"]
    nomes += [f"{a}-{b}" for a, b in pairwise(edges)]
    nomes.append(f">{edges[-1]}")
    return nomes


def faixa_de(n_chars: int, edges: list[int], nomes: list[str]) -> str:
    for i, corte in enumerate(edges):
        if n_chars < corte:
            return nomes[i]
    return nomes[-1]


def maior_resto(pesos: dict[str, float], total: int) -> dict[str, int]:
    """Reparte ``total`` proporcionalmente a ``pesos``, por maior resto.

    Maior-resto (e não ``round`` por item) porque a soma tem de fechar EXATA:
    arredondar cada cota isoladamente sobra ou falta e a semente sai com 11.998
    ou 12.003 itens, o que estraga o fatiamento em lotes e a contabilidade toda.
    Empate de resto desempata pela ordem do dicionário — que é a ordem do
    ``settings.toml``, o mesmo critério editorial do dedup.
    """
    soma = sum(pesos.values())
    if total <= 0 or soma <= 0:
        return dict.fromkeys(pesos, 0)
    exatos = {k: total * p / soma for k, p in pesos.items()}
    base = {k: int(v) for k, v in exatos.items()}
    sobra = total - sum(base.values())
    if sobra > 0:
        # -resto para ordenar decrescente; o índice preserva a ordem do TOML no
        # empate (sorted é estável, mas a chave precisa ser total).
        ordem = sorted(
            enumerate(pesos), key=lambda par: (-(exatos[par[1]] - base[par[1]]), par[0])
        )
        for _, chave in ordem[:sobra]:
            base[chave] += 1
    return base


def alocar(
    cotas: dict[str, int],
    disponivel: dict[str, int],
    total: int,
) -> tuple[dict[str, int], list[str]]:
    """Cotas por fonte respeitando o que existe, redistribuindo o déficit.

    Devolve ``(final, notas)``. ``notas`` conta a história para o ``strata.txt``.
    """
    planejado = maior_resto({k: float(v) for k, v in cotas.items()}, total)
    final = dict(planejado)
    travadas: set[str] = set()
    notas: list[str] = []

    for rodada in range(1, len(cotas) + 2):
        estouradas = {
            f: final[f] - disponivel.get(f, 0)
            for f in final
            if f not in travadas and final[f] > disponivel.get(f, 0)
        }
        if not estouradas:
            break
        deficit = 0
        for fonte, excesso in estouradas.items():
            deficit += excesso
            final[fonte] = disponivel.get(fonte, 0)
            travadas.add(fonte)
            notas.append(
                f"rodada {rodada}: {fonte} cede {excesso} "
                f"(cota {planejado[fonte]}, disponível {disponivel.get(fonte, 0)})"
            )
        livres = {f: float(planejado[f]) for f in final if f not in travadas}
        if not livres or deficit <= 0:
            if deficit > 0:
                notas.append(
                    f"rodada {rodada}: {deficit} vaga(s) sem destino — "
                    "todas as fontes travadas no disponível"
                )
            break
        extra = maior_resto(livres, deficit)
        for fonte, quanto in extra.items():
            if quanto:
                final[fonte] += quanto
                notas.append(f"rodada {rodada}: {fonte} recebe +{quanto}")
    return final, notas


def repartir_faixas(
    cota: int,
    disponivel: dict[str, int],
    piso_frac: float,
) -> dict[str, int]:
    """Cota de uma fonte repartida entre as faixas de tamanho.

    Piso primeiro (``min(floor(piso_frac * cota), disponível)`` por faixa), o
    resto por maior-resto proporcional ao que existe, e as sobras de faixa
    esgotada redistribuídas dentro da própria fonte. O piso é o que impede a
    faixa rara de sumir: sem ele, a fonte cujos prompts longos são 2% do total
    entraria na semente com zero longos e o classificador nunca veria o formato.
    """
    faixas_ordem = list(disponivel)
    piso = int(piso_frac * cota)
    final = {f: min(piso, disponivel[f]) for f in faixas_ordem}
    resto = cota - sum(final.values())

    for _ in range(len(faixas_ordem) + 1):
        if resto <= 0:
            break
        folga = {f: float(disponivel[f] - final[f]) for f in faixas_ordem}
        if sum(folga.values()) <= 0:
            break
        proposta = maior_resto(folga, min(resto, int(sum(folga.values()))))
        movido = 0
        for f, quanto in proposta.items():
            cabe = min(quanto, disponivel[f] - final[f])
            final[f] += cabe
            movido += cabe
        resto -= movido
        if movido == 0:
            break
    return final


def _recusar_se_ha_campanha(lp: LabelingPaths, force: bool) -> None:
    """Aborta se já existe manifest com trabalho feito e ninguém pediu ``--force``.

    Refazer a semente é refazer os uids, os lotes e o gabarito: os rótulos já
    produzidos passam a apontar para itens que não estão mais em lote nenhum.
    Um manifest ainda 100% ``pending`` é descartável (nada se perdeu); com um
    único lote ``done``, ou com o ouro importado, jogar fora é decisão humana.
    """
    if not lp.manifest.is_file():
        return
    import json

    with lp.manifest.open(encoding="utf-8") as fh:
        anterior = json.load(fh)
    lotes = anterior.get("batches", {})
    feitos = [b for b, r in lotes.items() if r.get("status") != "pending"]
    tem_ouro = bool(anterior.get("gold"))
    if not feitos and not tem_ouro:
        return
    if force:
        print(
            f"[{ESTAGIO}] --force: descartando campanha anterior "
            f"({len(feitos)} lote(s) fora de pending, ouro={'sim' if tem_ouro else 'não'})"
        )
        return
    raise SystemExit(
        f"[{ESTAGIO}] {rel(lp.manifest)} já registra uma campanha em andamento "
        f"({len(feitos)} lote(s) fora de pending, ouro={'sim' if tem_ouro else 'não'}). "
        "Refazer a semente invalida todo rótulo já produzido — use `pf make-seed --force` "
        "se é isso mesmo que você quer."
    )


def _sortear(rng: Any, uids: list[str], k: int) -> list[str]:
    """``k`` uids de uma lista JÁ ORDENADA, sem reposição, sem alocar cópias."""
    if k >= len(uids):
        return list(uids)
    if k <= 0:
        return []
    escolhidos = rng.choice(len(uids), size=k, replace=False)
    return [uids[int(i)] for i in sorted(escolhidos)]


def run(cfg: StageConfig) -> int:
    """Monta a semente, o relatório de estratos e os lotes. 0 = sucesso."""
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    relogio = Cronometro(ESTAGIO)
    load_taxonomy()  # a semente carimba a versão no manifest: valida antes
    origem = cfg.caminho(UNIVERSE)
    exigir(origem, "pf run s01-s06")

    lp = LabelingPaths(cfg.labeling_dir)
    lp.preparar()
    _recusar_se_ha_campanha(lp, cfg.force)
    destino = cfg.caminho_labeling(SEED_PARQUET)
    destino_strata = cfg.caminho_labeling(SEED_STRATA)

    total = int(get("seed", "size", default=12000))
    pt_share = float(get("seed", "pt_share", default=0.5))
    edges = [int(x) for x in get("seed", "bucket_edges", default=[120, 600])]
    piso_frac = float(get("seed", "bucket_floor", default=0.10))
    rng_seed = int(get("seed", "rng_seed", default=42))
    alocacao = dict(get("seed", "allocation"))
    nomes_faixa = faixas(edges)

    tabela = pq.read_table(origem, columns=list(COLUNAS_LIDAS))
    linhas = tabela.to_pylist()

    # --- inventário: (idioma, fonte, faixa) -> uids ORDENADOS ---------------
    pool: dict[tuple[str, str, str], list[str]] = {}
    por_uid: dict[str, dict[str, Any]] = {}
    for linha in linhas:
        idioma = str(linha["lang"] or "")
        if idioma not in alocacao:
            continue
        fonte = str(linha["source"] or "")
        faixa = faixa_de(int(linha["n_chars"] or 0), edges, nomes_faixa)
        linha["bucket"] = faixa
        uid = str(linha["uid"])
        por_uid[uid] = linha
        pool.setdefault((idioma, fonte, faixa), []).append(uid)
    for chave in pool:
        pool[chave].sort()

    metas = {"pt": round(total * pt_share), "en": total - round(total * pt_share)}
    rng = np.random.default_rng(rng_seed)

    relatorio: list[str] = [
        "# s07 — amostra-semente estratificada (fonte x faixa de tamanho)",
        f"# universo: {rel(origem)} ({tabela.num_rows} linhas)",
        f"# rng_seed={rng_seed}  size={total}  pt_share={pt_share}  "
        f"bucket_edges={edges}  bucket_floor={piso_frac}",
        "",
    ]
    escolhidos: list[str] = []
    resumo_alocacao: dict[str, Any] = {}
    linhas_funil: list[list[Any]] = []

    for idioma, cotas_toml in alocacao.items():
        cotas = {str(f): int(q) for f, q in dict(cotas_toml).items()}
        disponivel = {
            f: sum(len(pool.get((idioma, f, b), [])) for b in nomes_faixa) for f in cotas
        }
        final, notas = alocar(cotas, disponivel, metas.get(idioma, 0))
        resumo_alocacao[idioma] = {
            "planejado": maior_resto({k: float(v) for k, v in cotas.items()}, metas.get(idioma, 0)),
            "disponivel": disponivel,
            "final": final,
            "notas": notas,
        }

        relatorio.append(f"## {idioma} — alvo {metas.get(idioma, 0)}")
        relatorio.append(f"{'fonte':<16}{'cota':>8}{'disponível':>12}{'final':>8}   faixas")
        for fonte in cotas:
            disponivel_faixa = {
                b: len(pool.get((idioma, fonte, b), [])) for b in nomes_faixa
            }
            por_faixa = repartir_faixas(final[fonte], disponivel_faixa, piso_frac)
            detalhe = " ".join(
                f"{b}={por_faixa[b]}/{disponivel_faixa[b]}" for b in nomes_faixa
            )
            relatorio.append(
                f"{fonte:<16}{cotas[fonte]:>8}{disponivel[fonte]:>12}{final[fonte]:>8}   {detalhe}"
            )
            linhas_funil.append(
                [idioma, fonte, cotas[fonte], disponivel[fonte], final[fonte]]
            )
            for b in nomes_faixa:
                escolhidos.extend(_sortear(rng, pool.get((idioma, fonte, b), []), por_faixa[b]))
        if notas:
            relatorio.append("redistribuição de déficit:")
            relatorio.extend(f"  - {n}" for n in notas)
        relatorio.append("")

    if not escolhidos:
        print(f"[{ESTAGIO}] nenhuma linha elegível no universo — nada a fazer")
        return 1

    # --- calibração: 100 itens tirados de DENTRO da semente ------------------
    tamanho_calibracao = int(get("labeling", "calibration_size", default=100))
    batch_size = int(get("labeling", "batch_size", default=80))
    gold_per_batch = int(get("labeling", "gold_per_batch", default=3))

    # Proporcional ao que cada (idioma, fonte) trouxe, para o gabarito refletir a
    # semente e não só a fonte maior.
    por_grupo: dict[tuple[str, str], list[str]] = {}
    for uid in escolhidos:
        linha = por_uid[uid]
        por_grupo.setdefault((str(linha["lang"]), str(linha["source"])), []).append(uid)
    cotas_cal = maior_resto(
        {f"{i}|{f}": float(len(v)) for (i, f), v in por_grupo.items()},
        min(tamanho_calibracao, len(escolhidos)),
    )
    calibracao: list[str] = []
    for chave, quanto in cotas_cal.items():
        idioma, _, fonte = chave.partition("|")
        calibracao.extend(_sortear(rng, sorted(por_grupo[(idioma, fonte)]), quanto))

    # --- semente em parquet --------------------------------------------------
    dados = [por_uid[u] for u in escolhidos]
    escrever_tabela(
        pa.table(
            {col: [linha.get(col) for linha in dados] for col in COLUNAS_SEED},
            schema=pa.schema(
                [
                    pa.field("uid", pa.string(), nullable=False),
                    pa.field("text", pa.string(), nullable=False),
                    pa.field("lang", pa.string(), nullable=False),
                    pa.field("source", pa.string(), nullable=False),
                    pa.field("n_chars", pa.int32(), nullable=False),
                    pa.field("native_category", pa.string(), nullable=True),
                    pa.field("bucket", pa.string(), nullable=False),
                ]
            ),
        ),
        destino,
    )

    manifest = montar_lotes(
        dados,
        tamanho=batch_size,
        n_calibracao=gold_per_batch,
        calibracao_uids=calibracao,
        tamanho_calibracao=tamanho_calibracao,
        rng=rng,
        lp=lp,
        extra_manifest={
            "seed_rng": rng_seed,
            "bucket_edges": edges,
            "allocation": resumo_alocacao,
        },
    )
    n_lotes = len(manifest["batches"])

    contagem_faixa = Counter(por_uid[u]["bucket"] for u in escolhidos)
    relatorio.append("## faixas de tamanho na semente")
    for b in nomes_faixa:
        relatorio.append(f"{b:<12}{contagem_faixa.get(b, 0):>8}")
    relatorio.append("")
    relatorio.append(
        f"## lotes: {n_lotes} de {batch_size} itens "
        f"({batch_size - gold_per_batch} novos + {gold_per_batch} de calibração ocultos)"
    )
    relatorio.append(
        f"calibração: batch_0000 com {len(calibracao)} itens (gold_pending — "
        "revisar à mão e importar com `pf labels gold`)"
    )
    escrever_texto_atomico(destino_strata, "\n".join(relatorio) + "\n")

    imprimir_funil(
        ESTAGIO,
        ("idioma", "fonte", "cota", "disponível", "final"),
        linhas_funil,
        ["TOTAL", "", total, tabela.num_rows, len(escolhidos)],
    )
    print(f"[{ESTAGIO}] {len(escolhidos)} itens -> {rel(destino)}")
    print(f"[{ESTAGIO}] estratos -> {rel(destino_strata)}")
    print(
        f"[{ESTAGIO}] {n_lotes} lotes + calibração de {len(calibracao)} "
        f"-> {rel(lp.batches)} (manifest: {rel(lp.manifest)})"
    )
    print(
        f"[{ESTAGIO}] PRÓXIMO PASSO: rotular batch_0000, revisar à mão e "
        "importar com `pf labels gold --file <jsonl>`"
    )
    relogio.fim()
    return 0


__all__ = [
    "COLUNAS_LIDAS",
    "COLUNAS_SEED",
    "ESTAGIO",
    "alocar",
    "faixa_de",
    "faixas",
    "maior_resto",
    "repartir_faixas",
    "run",
]
