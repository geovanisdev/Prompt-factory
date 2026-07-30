"""Leitura (com cache) de ``config/settings.toml`` e ``config/sources.toml``.

Só stdlib: ``tomllib`` + ``functools.lru_cache``. Os TOML são lidos em modo
binário, como o tomllib exige, então acentuação nunca depende da codepage do
console — problema real no Windows.

Uso típico::

    from prompt_factory.config import settings, sources, get

    near = get("dedup", "near_cosine", default=0.985)
    for name, spec in enabled_sources().items():
        ...
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from .paths import SETTINGS_TOML, SOURCES_TOML

_MISSING = object()


@lru_cache(maxsize=8)
def _read_toml(path_str: str) -> dict[str, Any]:
    path = Path(path_str)
    if not path.is_file():
        raise FileNotFoundError(f"config não encontrado: {path}")
    with path.open("rb") as fh:
        return tomllib.load(fh)


def settings() -> dict[str, Any]:
    """``config/settings.toml`` inteiro, como dict aninhado."""
    return _read_toml(str(SETTINGS_TOML))


def sources() -> dict[str, Any]:
    """``config/sources.toml`` inteiro: ``{nome_da_fonte: spec}``."""
    return _read_toml(str(SOURCES_TOML))


def get(section: str, key: str, default: Any = _MISSING) -> Any:
    """Lê ``settings[section][key]``.

    Sem ``default``, uma chave ausente é erro (falha cedo e alto, em vez de a
    pipeline rodar com um threshold silenciosamente errado).
    """
    try:
        return settings()[section][key]
    except KeyError:
        if default is _MISSING:
            raise KeyError(
                f"chave ausente em {SETTINGS_TOML.name}: [{section}] {key}"
            ) from None
        return default


def hf_home() -> str:
    """Diretório de cache do HuggingFace (``[general] hf_home``)."""
    return str(get("general", "hf_home", default="G:/hf-cache"))


def seed() -> int:
    """Semente global de aleatoriedade (``[general] seed``)."""
    return int(get("general", "seed", default=42))


def source(name: str) -> dict[str, Any]:
    """Spec de uma fonte pelo nome da seção em ``sources.toml``."""
    try:
        return sources()[name]
    except KeyError:
        known = ", ".join(sorted(sources()))
        raise KeyError(f"fonte desconhecida: {name!r} (conhecidas: {known})") from None


def enabled_sources() -> dict[str, Any]:
    """Só as fontes com ``enabled = true``, na ordem do arquivo."""
    return {n: s for n, s in sources().items() if s.get("enabled", False)}


def clear_cache() -> None:
    """Descarta o cache — útil em testes que reescrevem os TOML."""
    _read_toml.cache_clear()


__all__ = [
    "clear_cache",
    "enabled_sources",
    "get",
    "hf_home",
    "seed",
    "settings",
    "source",
    "sources",
]
