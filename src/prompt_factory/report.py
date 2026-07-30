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


__all__ = ["LinhaRelatorio", "report_raw"]
