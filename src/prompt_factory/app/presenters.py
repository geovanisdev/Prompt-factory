"""Linha do SQLite → dict JSON. O formato que a interface enxerga nasce aqui.

Três conversões que não são cosméticas:

* **Booleano vira booleano.** O SQLite guarda 0/1; devolver 0/1 no JSON faz o
  cliente escrever ``if (item.nsfw)`` e acertar por acidente até o dia em que o
  valor é ``null``. E ``null`` acontece o tempo todo: ``nsfw``, ``quality``,
  ``task_type`` e ``domain`` são NULL enquanto a campanha de rotulagem não
  rodar. ``null`` é "não sabemos", ``false`` é "sabemos que não" — a interface
  precisa dos dois.
* **``license`` vira também uma CLASSE.** O usuário monta datasets para
  terceiros; se ele exportar CC-BY-NC achando que é livre, o estrago é dele.
  A licença é sinal de primeira classe na tela, não rodapé de card.
* **``text`` da listagem já vem cortado do SQL** e carrega ``n_chars`` ao lado,
  para o card poder dizer "+12.847 caracteres" sem ter buscado 1 MB.
"""

from __future__ import annotations

import json
import sqlite3
from functools import lru_cache
from typing import Any

from ..config import sources as _sources

#: Classes de licença, na ordem em que o usuário decide. A classificação
#: combina as três colunas porque nenhuma delas sozinha responde: ``cc-by-nc``
#: é redistribuível mas não comercial, ``lmsys-1m`` não é nem uma coisa nem
#: outra, e ``unknown`` é tratada como bloqueada até alguém verificar.
LICENCA_LIVRE = "livre"
LICENCA_VIRAL = "viral"
LICENCA_NAO_COMERCIAL = "nao-comercial"
LICENCA_BLOQUEADA = "bloqueada"

#: ShareAlike contamina o derivado: quem exporta precisa saber ANTES.
_VIRAIS: frozenset[str] = frozenset({"cc-by-sa-3.0"})


def classe_da_licenca(
    licenca: str, comercial: object, redistribuivel: object
) -> str:
    """A classe visual de uma licença (``livre``/``viral``/``nao-comercial``/``bloqueada``)."""
    if not redistribuivel:
        return LICENCA_BLOQUEADA
    if not comercial:
        return LICENCA_NAO_COMERCIAL
    if str(licenca) in _VIRAIS:
        return LICENCA_VIRAL
    return LICENCA_LIVRE


@lru_cache(maxsize=1)
def atribuicoes() -> dict[str, str]:
    """``{fonte: crédito por extenso}``, de ``config/sources.toml``.

    **``attribution`` não é coluna do banco.** Ela viaja no TOML e é resolvida
    aqui — e sem ela o export não cumpre a licença: ODC-BY, CC-BY e CC-BY-SA
    exigem atribuição a cada uso.
    """
    return {nome: str(spec.get("attribution", "")) for nome, spec in _sources().items()}


def atribuicao(fonte: str) -> str:
    """O crédito de uma fonte (string vazia se a fonte não está no TOML)."""
    return atribuicoes().get(str(fonte), "")


def _b(valor: object) -> bool | None:
    """0/1/NULL do SQLite → ``false``/``true``/``null``."""
    return None if valor is None else bool(valor)


def _meta(bruto: object) -> dict[str, Any]:
    """``meta_json`` desserializado; conteúdo torto vira ``{}`` sem derrubar a rota."""
    if not bruto:
        return {}
    try:
        dados = json.loads(str(bruto))
    except (TypeError, ValueError):
        return {}
    return dados if isinstance(dados, dict) else {}


def _comuns(linha: sqlite3.Row) -> dict[str, Any]:
    """Os campos que listagem e detalhe compartilham."""
    licenca = str(linha["license"])
    comercial = _b(linha["commercial_ok"])
    redistribuivel = _b(linha["redistributable"])
    return {
        "id": int(linha["id"]),
        "uid": str(linha["uid"]),
        "n_chars": int(linha["n_chars"]),
        "n_words": int(linha["n_words"]),
        "lang": linha["lang"],
        "lang_variant": linha["lang_variant"],
        "variant_confidence": linha["variant_confidence"],
        "source": linha["source"],
        "source_id": linha["source_id"],
        "license": licenca,
        "license_class": classe_da_licenca(licenca, comercial, redistribuivel),
        "commercial_ok": comercial,
        "redistributable": redistribuivel,
        "task_type": linha["task_type"],
        "domain": linha["domain"],
        "quality": None if linha["quality"] is None else int(linha["quality"]),
        "nsfw": _b(linha["nsfw"]),
        "pii_found": _b(linha["pii_found"]),
        "label_method": linha["label_method"],
        "label_confidence": linha["label_confidence"],
        "needs_review": _b(linha["needs_review"]),
        "n_exact_dups": int(linha["n_exact_dups"]),
        "n_near_dups": int(linha["n_near_dups"]),
        "created_ts": linha["created_ts"],
        "updated_at": linha["updated_at"],
        "has_chat_markers": bool(linha["has_chat_markers"]),
    }


def item_lista(
    linha: sqlite3.Row, *, corte: int, snippet: str | None = None
) -> dict[str, Any]:
    """Um item da LISTAGEM: texto cortado + snippet do FTS quando há busca."""
    dados = _comuns(linha)
    texto = str(linha["text"])
    dados.update(
        {
            "text": texto,
            "text_truncated": dados["n_chars"] > corte,
            "edited": bool(linha["edited"]),
            # `snippet` é o trecho COM O TERMO destacado por <mark>; `text` é o
            # começo do prompt. Num prompt de 1 MB os dois não têm relação
            # nenhuma, e é o snippet que responde "por que este item apareceu".
            "snippet": snippet,
            "score": linha["score"] if "score" in linha.keys() else None,
        }
    )
    return dados


def item_detalhe(linha: sqlite3.Row, *, colecoes: list[dict[str, Any]]) -> dict[str, Any]:
    """O item COMPLETO: texto inteiro, original ao lado, procedência e coleções."""
    dados = _comuns(linha)
    dados.update(
        {
            "text": str(linha["text"]),
            "text_truncated": False,
            "text_original": linha["text_original"],
            "edited": bool(linha["edited"]),
            "source_split": linha["source_split"],
            "hash_norm": linha["hash_norm"],
            "country": linha["country"],
            "model_family": linha["model_family"],
            "native_category": linha["native_category"],
            "meta": _meta(linha["meta_json"]),
            "attribution": atribuicao(str(linha["source"])),
            "ingested_at": linha["ingested_at"],
            "collections": colecoes,
            "snippet": None,
            "score": None,
        }
    )
    return dados


def colecao(linha: sqlite3.Row, n_items: int | None = None) -> dict[str, Any]:
    """Uma coleção como JSON."""
    dados = {
        "id": int(linha["id"]),
        "name": str(linha["name"]),
        "description": linha["description"],
        "created_at": linha["created_at"],
        "updated_at": linha["updated_at"],
    }
    if n_items is not None:
        dados["n_items"] = n_items
    return dados


__all__ = [
    "LICENCA_BLOQUEADA",
    "LICENCA_LIVRE",
    "LICENCA_NAO_COMERCIAL",
    "LICENCA_VIRAL",
    "atribuicao",
    "atribuicoes",
    "classe_da_licenca",
    "colecao",
    "item_detalhe",
    "item_lista",
]
