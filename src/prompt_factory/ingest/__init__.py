"""Registro dos ingesters: uma fonte do HuggingFace -> `data/raw/<fonte>.parquet`.

Cada módulo expõe `iter_rows(cfg, max_rows=None) -> Iterator[dict]`, onde `cfg` é
a seção da fonte em `config/sources.toml` e cada dict tem exatamente as 12
chaves do `base.RAW_SCHEMA`. Quem grava é o `base.write_raw`, que sobrescreve a
licença e cuida da atomicidade — o ingester só sabe extrair texto.

A ordem do `REGISTRY` é a ordem de execução de `pf ingest` sem argumentos, e foi
escolhida por custo de download crescente: as fontes baratas terminam primeiro,
o arena140k (maior) fica por último.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import ModuleType

from . import arena140k, aya, dolly, hh_rlhf, no_robots, oasst, prism

#: nome da fonte (seção do sources.toml) -> módulo com `iter_rows`.
REGISTRY: dict[str, ModuleType] = {
    "aya": aya,
    "oasst": oasst,
    "arena140k": arena140k,
    "no_robots": no_robots,
    "dolly": dolly,
    "hh_rlhf": hh_rlhf,
    "prism": prism,
}

#: Ordem de execução do `all`: barato -> caro (arena140k é o maior download).
ORDEM_PADRAO: tuple[str, ...] = (
    "no_robots",
    "dolly",
    "prism",
    "aya",
    "oasst",
    "hh_rlhf",
    "arena140k",
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
    """
    if not nomes:
        return default_sources()

    resolvidos: list[str] = []
    for nome in nomes:
        candidatos = default_sources() if nome == "all" else [nome]
        for candidato in candidatos:
            if candidato not in REGISTRY:
                conhecidas = ", ".join(REGISTRY)
                raise ValueError(f"fonte desconhecida: {candidato!r} (disponiveis: {conhecidas}, all)")
            if candidato not in resolvidos:
                resolvidos.append(candidato)
    return resolvidos


__all__ = ["ORDEM_PADRAO", "REGISTRY", "default_sources", "resolve_names"]
