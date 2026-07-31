"""``criar_app()`` — a app FastAPI da Bancada (plataforma de anotação).

Fábrica, não ``app`` global: um ``app = FastAPI()`` de módulo leria os caminhos
dos bancos no import e tornaria impossível apontar os testes para outro lugar.
``criar_app(db_anotacao=..., db_corpus=...)`` guarda os dois em ``app.state`` e
as dependências abrem de lá.

O lifespan desta app faz quatro coisas, e **três delas são o oposto** da app de
curadoria — por bons motivos:

1. **Recusa subir sem o corpus**, com a mesma mensagem instrutiva do
   ``pf serve``. Um servidor que sobe e devolve 500 em toda rota é pior que um
   que não sobe: a linha de conserto fica enterrada num traceback por request em
   vez de aparecer uma vez no terminal. (Igual à curadoria.)
2. **CRIA o ``annotate.sqlite`` se ele não existir.** A curadoria se recusa a
   criar o ``prompts.sqlite`` porque quem o constrói é a pipeline, e um banco
   vazio criado por acidente parece um corpus que ficou sem dados. Aqui é o
   contrário: **esta app é a dona deste banco**, ninguém mais o produz, e exigir
   um comando prévio para começar a usar a plataforma seria fricção sem
   contrapartida.
3. **Não congela nada do corpus.** Nem contagem, nem build_id, nem pool. O
   corpus é trocado por swap de arquivo embaixo da app (``pf load-db``), e
   número lido na subida é mentira com data de validade. Tudo ao vivo, por
   request — os volumes desta app permitem, e o problema "cache mente sob
   escritor externo" deixa de existir por construção.
4. **Na saída, limpa os dois rastros**: ``wal_checkpoint(TRUNCATE)`` no banco da
   plataforma e a remoção do ``-shm`` órfão do corpus. Ver ``_encerrar``.

**CORS não é configurado**, pelo mesmo motivo da curadoria: mesma origem, e
abrir CORS numa app de ``127.0.0.1`` seria superfície de ataque de graça.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .. import __version__, paths
from .. import db as dbmod
from . import db as adb

#: Diretório do ``index.html``. Fica DENTRO do pacote (e não na raiz do repo)
#: para que ``pf annotate serve`` funcione de qualquer diretório de trabalho.
STATIC = Path(__file__).resolve().parent / "static"

#: A mensagem de conserto quando falta o corpus. É a MESMA de ``cli._serve``, e
#: mora aqui para que a CLI (que falha antes de subir o uvicorn) e o lifespan
#: (que falha se o arquivo sumir entre uma coisa e outra) digam a mesma frase.
SEM_CORPUS = (
    "banco de prompts não encontrado em {caminho}.\n"
    "A Bancada só LÊ o corpus — quem o constrói é a pipeline.\n"
    "Rode:  pf load-db            (ou, antes do M7, "
    "pf load-db --allow-unlabeled-pct 100)"
)


def _preparar(db_anotacao: Path, db_corpus: Path) -> dict[str, Any]:
    """Guardas de partida. Levanta ``RuntimeError`` com a instrução se não dá."""
    if not db_corpus.is_file():
        raise RuntimeError(SEM_CORPUS.format(caminho=db_corpus))

    # O corpus é só CONFERIDO aqui (abre? tem a tabela?) e nada dele é guardado.
    # Falhar agora, na subida, transforma "500 no meio de uma anotação" em uma
    # linha no terminal antes de o servidor existir.
    conn = dbmod.connect(db_corpus, readonly=True)
    try:
        conn.execute("SELECT 1 FROM prompts LIMIT 1").fetchone()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(
            f"{db_corpus} abriu mas não parece o corpus do Prompt Factory ({exc}).\n"
            "Rode:  pf load-db"
        ) from exc
    finally:
        conn.close()

    # O banco da plataforma, esse sim, nasce aqui se precisar.
    db_anotacao.parent.mkdir(parents=True, exist_ok=True)
    novo = not db_anotacao.is_file()
    conn = dbmod.connect(db_anotacao)
    try:
        # `init_db` recusa ANTES de tocar no DDL quando a versão diverge (este
        # banco guarda trabalho humano e não é regenerável pela pipeline).
        # Recusar é a única resposta honesta — e desde o P3a ela vem com um
        # caminho: `pf annotate migrate` migra preservando o que já está lá.
        adb.init_db(conn)
    except adb.SchemaDivergente as exc:
        raise RuntimeError(f"{db_anotacao}: {exc}") from exc
    finally:
        conn.close()
    if novo:
        print(f"[bancada] banco da plataforma criado em {db_anotacao}")
        print("[bancada] sem personas ainda: rode `pf annotate seed`")

    return {
        "db_anotacao": str(db_anotacao),
        "db_corpus": str(db_corpus),
        "schema_version_anotacao": adb.SCHEMA_VERSION_ANOTACAO,
        "pf_version": __version__,
    }


def _encerrar(db_anotacao: Path, db_corpus: Path) -> None:
    """Devolve os dois arquivos ao estado em que a próxima pipeline os espera.

    **No banco da plataforma**: ``wal_checkpoint(TRUNCATE)``, para que um Ctrl+C
    não deixe ``-wal``/``-shm`` ao lado.

    **No corpus**: apaga só o ``-shm``, e só ele. Uma conexão READ-ONLY em WAL
    cria o ``-shm`` e **não consegue removê-lo ao fechar** (removê-lo é uma
    escrita). O arquivo fica órfão ao lado do corpus — e é exatamente o sinal que
    o pré-voo do swap do ``pf load-db`` lê como "alguém está com isto aberto".
    Resultado: a próxima recarga do corpus seria recusada por causa de uma app
    que já morreu.

    ``-wal`` NUNCA é tocado. Um ``-wal`` pode conter transações commitadas que
    ainda não foram para o arquivo principal; apagá-lo é perder dados do corpus.
    O ``-shm`` é só memória compartilhada de índice do wal, reconstruída sozinha
    na próxima abertura.

    Tudo dentro de ``try/except OSError``: no Windows o arquivo pode estar com
    handle aberto por outro processo (a app de curadoria, por exemplo), e nesse
    caso não há nada a limpar — quem tem o handle é quem manda.
    """
    try:
        conn = dbmod.connect(db_anotacao)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    except sqlite3.Error:  # pragma: no cover - banco sumiu no meio
        pass
    try:
        shm = db_corpus.with_name(db_corpus.name + "-shm")
        if shm.is_file():
            shm.unlink()
    except OSError:
        # Em uso por outro processo (ou permissão): não é problema nosso.
        pass


def criar_app(
    db_anotacao: str | Path | None = None,
    db_corpus: str | Path | None = None,
) -> FastAPI:
    """Monta a app da Bancada.

    ``db_anotacao`` padrão: ``data/db/annotate.sqlite`` (criado se faltar).
    ``db_corpus`` padrão: ``data/db/prompts.sqlite`` (exigido, só leitura).
    São parâmetros, e não constantes, para que os testes montem os dois bancos
    em ``tmp_path`` sem escrever uma linha em ``data/``.
    """
    alvo_anotacao = Path(db_anotacao) if db_anotacao is not None else paths.ANNOTATE_DB_FILE
    alvo_corpus = Path(db_corpus) if db_corpus is not None else paths.DB_FILE

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db_anotacao = alvo_anotacao
        app.state.db_corpus = alvo_corpus
        app.state.meta = _preparar(alvo_anotacao, alvo_corpus)
        try:
            yield
        finally:
            _encerrar(alvo_anotacao, alvo_corpus)

    app = FastAPI(
        title="Bancada",
        version=__version__,
        description=(
            "Plataforma de anotação (demonstração). Estado próprio em "
            "annotate.sqlite; o corpus do Prompt Factory é lido em modo "
            "somente leitura e nunca escrito por esta app."
        ),
        lifespan=lifespan,
    )

    # Import aqui dentro (e não no topo) para manter o grafo de import acíclico:
    # as rotas importam `deps`, que importa daqui.
    from . import (
        routes_avaliacao,
        routes_conversa,
        routes_meta,
        routes_revisao,
        routes_trabalho,
    )

    app.include_router(routes_meta.router)
    app.include_router(routes_trabalho.router)
    app.include_router(routes_conversa.router)
    app.include_router(routes_revisao.router)
    app.include_router(routes_avaliacao.router)

    # POR ÚLTIMO. Um mount em "/" registrado antes dos routers engoliria
    # /api/* — o Starlette casa as rotas na ordem em que foram adicionadas.
    STATIC.mkdir(parents=True, exist_ok=True)
    app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")
    return app


__all__ = ["SEM_CORPUS", "STATIC", "criar_app"]
