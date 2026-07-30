"""Registro dos ingesters: uma fonte do HuggingFace -> `data/raw/<fonte>.parquet`.

Há **dois contratos**, e o `cli._ingest` escolhe pelo que o módulo expõe:

* `iter_rows(cfg, max_rows=None) -> Iterator[dict]` — as 7 fontes pequenas do
  M2. Cada dict tem exatamente as 12 chaves do `base.RAW_SCHEMA`; quem grava (e
  carimba a licença do `sources.toml`) é o `base.write_raw`, que materializa
  tudo em RAM — o que é aceitável até ~16k linhas por fonte.
* `run_ingest(nome, cfg, args) -> int` — fontes que precisam controlar a própria
  escrita. O `wildchat` (M3) usa isto porque 3,2M de linhas em streaming não
  cabem no modelo do `write_raw`: ele escreve part-files, mantém checkpoint e
  consolida no fim. Devolve o exit code (0 ok, 1 erro, 2 desabilitada).

A ordem do `REGISTRY` é a ordem de execução de `pf ingest` sem argumentos, e foi
escolhida por custo de download crescente: as baratas terminam primeiro, os dois
passes do WildChat (1-3 h cada) ficam por último.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import ModuleType

from . import arena140k, aya, dolly, hh_rlhf, lmsys_chat_1m, no_robots, oasst, prism, wildchat

#: nome da fonte (seção do sources.toml) -> módulo com `iter_rows` ou `run_ingest`.
REGISTRY: dict[str, ModuleType] = {
    "aya": aya,
    "oasst": oasst,
    "arena140k": arena140k,
    "no_robots": no_robots,
    "dolly": dolly,
    "hh_rlhf": hh_rlhf,
    "prism": prism,
    "wildchat_pt": wildchat,
    "wildchat_en": wildchat,
    "lmsys": lmsys_chat_1m,
}

#: Ordem de execução do `all`: barato -> caro. `lmsys` não entra (enabled=false;
#: só roda se pedida pelo nome, e ainda assim sai 2).
ORDEM_PADRAO: tuple[str, ...] = (
    "no_robots",
    "dolly",
    "prism",
    "aya",
    "oasst",
    "hh_rlhf",
    "arena140k",
    "wildchat_pt",
    "wildchat_en",
)


def default_sources() -> list[str]:
    """Fontes de `default_on = true` que já têm ingester, na ordem de custo."""
    from .. import config

    srcs = config.sources()

    def ligada(nome: str) -> bool:
        spec = srcs.get(nome, {})
        return bool(spec.get("enabled", False)) and bool(spec.get("default_on", False))

    return [n for n in ORDEM_PADRAO if n in REGISTRY and ligada(n)]


def resolve_names(nomes: Sequence[str] | None) -> list[str]:
    """Argumentos de `pf ingest` -> lista de fontes, sem repetir, na ordem pedida.

    Vazio (ou `all`) expande para `default_sources()`. Nome desconhecido é
    `ValueError` — errar o nome da fonte não pode virar um download de 8 GB da
    fonte errada.

    Hífen e underscore são equivalentes: o nome canônico é a seção do TOML
    (`wildchat_pt`, que também é o nome do parquet), mas a linha de comando
    aceita `wildchat-pt`, que é como se digita.
    """
    if not nomes:
        return default_sources()

    resolvidos: list[str] = []
    for nome in nomes:
        candidatos = default_sources() if nome == "all" else [nome.replace("-", "_")]
        for candidato in candidatos:
            if candidato not in REGISTRY:
                conhecidas = ", ".join(REGISTRY)
                raise ValueError(f"fonte desconhecida: {candidato!r} (disponiveis: {conhecidas}, all)")
            if candidato not in resolvidos:
                resolvidos.append(candidato)
    return resolvidos


__all__ = ["ORDEM_PADRAO", "REGISTRY", "default_sources", "resolve_names"]
