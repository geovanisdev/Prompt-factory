"""`pf report raw` — resumo legível dos parquets de `data/raw/`.

Existe porque parquet é binário: abrir com `cat`/`Get-Content` enche o terminal
de lixo e não responde nada. Este relatório responde as perguntas que importam
depois de uma ingestão: quantas linhas vieram, quantas são distintas, se o texto
não veio truncado, e se a contagem bate com o `expected_min/max` declarado em
`config/sources.toml`.

**Sai sempre com 0**, mesmo com WARN: é diagnóstico, não portão de qualidade.
Uma rodada de smoke (`--max-rows 200`) fica abaixo do esperado por construção, e
`distintas < linhas` é normal no arena140k (o mesmo prompt de abertura se repete
entre `evaluation_order` da mesma sessão; quem deduplica é o s04).
"""

from __future__ import annotations

import hashlib
import os
import statistics
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NamedTuple

from . import config, paths

#: Abaixo disto o texto é curto demais para ser um prompt de verdade.
CURTO = 10
#: Quantos caracteres de cada amostra aparecem no relatório.
AMOSTRA_CHARS = 100


class LinhaRelatorio(NamedTuple):
    """Uma linha da tabela (uma fonte)."""

    fonte: str
    linhas: int
    distintas: int
    pct_curto: float
    len_mediano: int
    status: str
    amostras: tuple[tuple[int, str], ...]

    @property
    def ok(self) -> bool:
        return self.status == "PASS"


def _esperadas() -> dict[str, Any]:
    """Fontes que o `pf ingest` produz por padrão (para acusar arquivo faltando)."""
    return {
        nome: spec
        for nome, spec in config.sources().items()
        if spec.get("enabled", False) and spec.get("default_on", False)
    }


def _status(linhas: int, spec: dict[str, Any] | None) -> str:
    if spec is None:
        return "PASS"
    minimo = spec.get("expected_min")
    maximo = spec.get("expected_max")
    if minimo is None or maximo is None:
        return "PASS"
    if int(minimo) <= linhas <= int(maximo):
        return "PASS"
    return f"WARN [{minimo}-{maximo}]"


def _amostras(textos: Sequence[str], quantas: int) -> tuple[tuple[int, str], ...]:
    """Posições igualmente espaçadas: com `quantas=2` dá exatamente 0 e n//2."""
    n = len(textos)
    if n == 0 or quantas <= 0:
        return ()
    posicoes = sorted({(i * n) // quantas for i in range(quantas)})
    return tuple((p, textos[p]) for p in posicoes if p < n)


def _analisa(fonte: str, arquivo: Path, spec: dict[str, Any] | None, head: int) -> LinhaRelatorio:
    import pyarrow.parquet as pq

    textos: list[str] = pq.read_table(arquivo, columns=["text_raw"]).column("text_raw").to_pylist()
    textos = [t or "" for t in textos]
    n = len(textos)
    if n == 0:
        return LinhaRelatorio(fonte, 0, 0, 0.0, 0, _status(0, spec), ())
    distintas = len({hashlib.md5(t.encode("utf-8"), usedforsecurity=False).digest() for t in textos})
    curtos = sum(1 for t in textos if len(t.strip()) < CURTO)
    return LinhaRelatorio(
        fonte=fonte,
        linhas=n,
        distintas=distintas,
        pct_curto=100.0 * curtos / n,
        len_mediano=int(statistics.median(len(t) for t in textos)),
        status=_status(n, spec),
        amostras=_amostras(textos, head),
    )


def report_raw(sources: Sequence[str] | None = None, head: int = 2) -> int:
    """Imprime a tabela de `data/raw/`. Retorna 0 SEMPRE (ver docstring do módulo)."""
    arquivos = {p.stem: p for p in sorted(paths.RAW.glob("*.parquet"))}
    esperadas = _esperadas()
    if sources:
        alvos = list(dict.fromkeys(sources))
    else:
        alvos = sorted(set(arquivos) | set(esperadas))

    if not alvos:
        print(f"[pf] nenhum parquet em {paths.RAW} - rode `pf ingest` primeiro")
        return 0

    linhas: list[LinhaRelatorio] = []
    faltando: list[str] = []
    for fonte in alvos:
        arquivo = arquivos.get(fonte)
        spec = esperadas.get(fonte) or config.sources().get(fonte)
        if arquivo is None:
            faltando.append(fonte)
            linhas.append(LinhaRelatorio(fonte, 0, 0, 0.0, 0, "MISSING", ()))
            continue
        linhas.append(_analisa(fonte, arquivo, spec, head))

    largura = max(len("fonte"), *(len(x.fonte) for x in linhas))
    cabecalho = (
        f"{'fonte':<{largura}}  {'linhas':>7}  {'distintas':>9}  "
        f"{'%curto':>6}  {'len_med':>7}  status"
    )
    print(cabecalho)
    print("-" * len(cabecalho))
    for x in linhas:
        if x.status == "MISSING":
            print(f"{x.fonte:<{largura}}  {'-':>7}  {'-':>9}  {'-':>6}  {'-':>7}  MISSING (sem parquet)")
            continue
        print(
            f"{x.fonte:<{largura}}  {x.linhas:>7}  {x.distintas:>9}  "
            f"{x.pct_curto:>6.1f}  {x.len_mediano:>7}  {x.status}"
        )

    print()
    for x in linhas:
        for pos, texto in x.amostras:
            print(f"[{x.fonte}] linha {pos}: {texto!r:.{AMOSTRA_CHARS}}")

    avisos = [x.fonte for x in linhas if not x.ok]
    print()
    if avisos:
        print(f"{len(avisos)} WARN de {len(linhas)} fontes: {', '.join(avisos)}")
    else:
        print(f"ALL PASS ({len(linhas)} fontes)")
    if faltando:
        print(f"sem parquet ainda: {', '.join(faltando)}")
    return 0


# ---------------------------------------------------------------------------
# `pf report universe` (M4)
# ---------------------------------------------------------------------------

#: Quantos pares o `pf report dedup-sample` mostra (o gate humano do M4).
AMOSTRA_PARES = 50
#: Quantos caracteres de cada lado do par aparecem.
PAR_CHARS = 120


def _tabela_contagem(titulo: str, contagens: Sequence[tuple[str, int]], total: int) -> None:
    if not contagens:
        return
    largura = max(len(titulo), *(len(str(k)) for k, _ in contagens))
    print(f"{titulo:<{largura}}  {'linhas':>9}  {'%':>6}")
    print("-" * (largura + 20))
    for chave, n in contagens:
        pct = (100.0 * n / total) if total else 0.0
        print(f"{chave!s:<{largura}}  {n:>9}  {pct:>5.1f}%")
    print()


def _contar(tabela: Any, coluna: str) -> list[tuple[str, int]]:
    """Contagem por valor de uma coluna string, ordenada da maior para a menor.

    Feito com ``pyarrow.compute.value_counts`` — a tabela canônica NUNCA passa
    por pandas (``quality`` é int8 nullable e viraria float64 no caminho).
    """
    import pyarrow.compute as pc

    contagens = pc.value_counts(tabela.column(coluna))
    pares = [
        (str(item["values"]) if item["values"] is not None else "(nulo)", int(item["counts"]))
        for item in contagens.to_pylist()
    ]
    return sorted(pares, key=lambda kv: (-kv[1], kv[0]))


def report_universe(caminho: Path | None = None) -> int:
    """Retrato do ``data/final/universe.parquet``: distribuições e somas."""
    import numpy as np
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    alvo = Path(caminho) if caminho else paths.FINAL / "universe.parquet"
    if not alvo.is_file():
        print(f"[pf] {alvo} não existe — rode `pf run s01-s06` antes")
        return 2

    tabela = pq.read_table(
        alvo,
        columns=[
            "lang",
            "lang_variant",
            "source",
            "license",
            "pii_found",
            "n_exact_dups",
            "n_near_dups",
            "n_chars",
            "n_words",
            "commercial_ok",
            "redistributable",
        ],
    )
    total = tabela.num_rows
    print(f"universo: {total} linhas em {alvo}")
    print()
    for coluna, titulo in (
        ("lang", "idioma"),
        ("lang_variant", "variante"),
        ("source", "fonte"),
        ("license", "licença"),
    ):
        _tabela_contagem(titulo, _contar(tabela, coluna), total)

    pii = int(pc.sum(pc.cast(tabela.column("pii_found"), "int32")).as_py() or 0)
    comercial = int(pc.sum(pc.cast(tabela.column("commercial_ok"), "int32")).as_py() or 0)
    redist = int(pc.sum(pc.cast(tabela.column("redistributable"), "int32")).as_py() or 0)
    exatas = int(pc.sum(tabela.column("n_exact_dups")).as_py() or 0)
    proximas = int(pc.sum(tabela.column("n_near_dups")).as_py() or 0)
    print(
        f"pii_found: {pii} ({100.0 * pii / total if total else 0:.2f}%)   "
        f"commercial_ok: {comercial}   redistributable: {redist}"
    )
    print(
        f"duplicatas absorvidas: {exatas} exatas + {proximas} próximas "
        f"= {exatas + proximas} linhas colapsadas em {total}"
    )
    print()

    largura = len("n_words")
    print(f"{'métrica':<{largura}}  {'mediana':>9}  {'p90':>9}  {'máx':>9}")
    print("-" * (largura + 33))
    for coluna in ("n_chars", "n_words"):
        valores = np.asarray(tabela.column(coluna).to_numpy(zero_copy_only=False))
        if valores.size == 0:
            continue
        mediana, p90 = np.percentile(valores, [50, 90])
        print(
            f"{coluna:<{largura}}  {int(mediana):>9}  {int(p90):>9}  {int(valores.max()):>9}"
        )
    return 0


def _textos_por_uid(caminho: Path, alvos: set[str]) -> dict[str, tuple[str, str]]:
    """``{uid: (texto, fonte)}`` só para os uids pedidos, sem carregar o resto."""
    import pyarrow.parquet as pq

    achados: dict[str, tuple[str, str]] = {}
    arquivo = pq.ParquetFile(caminho)
    for lote in arquivo.iter_batches(batch_size=16_384, columns=["uid", "text", "source"]):
        uids = lote.column("uid").to_pylist()
        if not any(u in alvos for u in uids):
            continue
        textos = lote.column("text").to_pylist()
        fontes = lote.column("source").to_pylist()
        for i, uid in enumerate(uids):
            if uid in alvos:
                achados[uid] = (textos[i] or "", str(fontes[i] or ""))
        if len(achados) == len(alvos):
            break
    return achados


def report_dedup_sample(
    quantos: int = AMOSTRA_PARES,
    seed: int | None = None,
    data_dir: Path | None = None,
) -> int:
    """Amostra de pares near-duplicados lado a lado — o gate humano do M4.

    Imprime **e grava** ``final/dedup_sample_50.txt``: aceitar os thresholds do
    s06 é decisão de pessoa, e pessoa não revisa rolando terminal.
    """
    import numpy as np
    import pyarrow.parquet as pq

    from . import config

    raiz = Path(data_dir) if data_dir else paths.DATA
    mapa = raiz / "final" / "dedup_near_map.parquet"
    fonte_textos = raiz / "interim" / "dedup1.parquet"
    if not mapa.is_file():
        print(f"[pf] {mapa} não existe — rode `pf run s06` antes")
        return 2
    if not fonte_textos.is_file():
        print(f"[pf] {fonte_textos} não existe — rode `pf run s04` antes")
        return 2

    tabela = pq.read_table(mapa)
    n = tabela.num_rows
    if n == 0:
        print("[pf] nenhum par near-duplicado foi encontrado — nada a revisar")
        return 0
    rng = np.random.default_rng(config.seed() if seed is None else seed)
    escolhidos = sorted(rng.choice(n, size=min(quantos, n), replace=False).tolist())
    amostra = tabela.take(escolhidos).to_pylist()

    alvos = {linha["uid"] for linha in amostra} | {linha["canonical_uid"] for linha in amostra}
    textos = _textos_por_uid(fonte_textos, alvos)

    linhas: list[str] = [
        f"amostra de {len(amostra)} pares near-duplicados de {n} (seed "
        f"{config.seed() if seed is None else seed})",
        "GATE HUMANO: confirme que cada par é MESMO a mesma coisa. Se houver par "
        "diferente demais, suba [dedup] near_cosine/near_jaccard e rode `pf run s06` de novo.",
        "",
    ]
    for i, linha in enumerate(amostra, start=1):
        removido, fonte_r = textos.get(linha["uid"], ("(texto não encontrado)", "?"))
        canonico, fonte_c = textos.get(linha["canonical_uid"], ("(texto não encontrado)", "?"))
        linhas.append(
            f"[{i:02d}] lang={linha['lang']} cos={linha['cosine']:.4f} "
            f"jaccard={linha['jaccard']:.4f}"
        )
        linhas.append(f"  canônico  ({fonte_c}/{linha['canonical_uid']}): "
                      f"{canonico[:PAR_CHARS]!r}")
        linhas.append(f"  removido  ({fonte_r}/{linha['uid']}): {removido[:PAR_CHARS]!r}")
        linhas.append("")

    texto = "\n".join(linhas)
    print(texto)
    destino = raiz / "final" / "dedup_sample_50.txt"
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.parent / f"{destino.name}.tmp"
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(texto)
    os.replace(tmp, destino)
    print(f"[pf] amostra gravada em {destino}")
    return 0


__all__ = [
    "LinhaRelatorio",
    "report_dedup_sample",
    "report_raw",
    "report_universe",
]
