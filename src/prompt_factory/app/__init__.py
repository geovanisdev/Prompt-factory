"""Interface local do Prompt Factory (M9): FastAPI + SQLite, sem build step.

A app toca **apenas** o banco (``data/db/prompts.sqlite``) e serve um
``static/index.html`` único. Nada aqui lê Parquet: quem transforma dado é a
pipeline (``s01``-``s11``), quem cura é esta app.

Mapa dos módulos:

* ``models``  — os modelos Pydantic do contrato (filtros, corpos de PATCH/export)
* ``queries`` — TODO o SQL num lugar só (sanitizador do FTS, WHERE, ORDER BY)
* ``deps``    — a conexão por request (e a regra dura sobre ``async def``)
* ``main``    — ``criar_app()``, lifespan, montagem do ``static/``
* ``routes_*``— as rotas, finas: validam, chamam ``queries``, formatam

O ponto de entrada é ``criar_app(db_file=...)``, uma **fábrica**: não existe
``app`` global lendo ``paths.DB_FILE`` no import, porque isso tornaria
impossível apontar os testes para um banco temporário.
"""

from __future__ import annotations

__all__ = ["criar_app"]


def __getattr__(nome: str) -> object:
    # Import preguiçoso: `from prompt_factory.app import criar_app` não deve
    # arrastar fastapi/pydantic para dentro de um `pf --help`.
    if nome == "criar_app":
        from .main import criar_app

        return criar_app
    raise AttributeError(f"module {__name__!r} has no attribute {nome!r}")
