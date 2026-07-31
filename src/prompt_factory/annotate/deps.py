"""Dependências das rotas da Bancada: **duas** conexões por request.

REGRA DURA, HERDADA DE ``app/deps.py`` E VÁLIDA AQUI IGUAL
==========================================================
**Nenhuma rota que toca banco pode ser ``async def``.** Uma rota ``async def``
roda no event loop, o ``sqlite3`` é síncrono e bloqueante, e uma consulta ali
dentro trava o servidor inteiro. Rota que toca banco é ``def``, sempre, e o
Starlette a joga no threadpool.

O QUE É NOVO AQUI: SÃO DOIS BANCOS, COM PERMISSÕES DIFERENTES
==============================================================
* ``ConAnotacao`` — ``data/db/annotate.sqlite``, **leitura e escrita**. Esta app
  é a dona dele.
* ``ConCorpus`` — ``data/db/prompts.sqlite``, **somente leitura**, sempre. A app
  de curadoria assume ser a única escritora do corpus (é o que autoriza o cache
  de agregados dela, ver ``app/cache.py``), e essa suposição não pode ser
  quebrada por um segundo processo escrevendo por baixo. O ``mode=ro`` do
  ``db.connect`` faz o próprio SQLite recusar qualquer escrita — a garantia é do
  banco, não da nossa disciplina. Há um teste que prova isso.

Não há ATTACH dos dois arquivos numa conexão só. Um ATTACH em modo escrita
tornaria a app escritora do corpus por acidente; e um JOIN entre bancos
ganharia performance que estes volumes não precisam, em troca da única
propriedade que importa aqui. Cruzamento é feito em Python, por ``uid``, com
``IN (...)`` — nunca por ``id``/rowid, que muda a cada ``pf load-db``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request

from .. import db as dbmod


def get_conn_anotacao(request: Request) -> Iterator[sqlite3.Connection]:
    """Conexão de ESCRITA no ``annotate.sqlite``. Uma por request, sempre em ``def``.

    ``db.connect`` já aplica WAL, ``busy_timeout``, ``foreign_keys=ON`` (PRAGMA
    **por conexão**: sem ele os ``ON DELETE CASCADE`` deste schema ficam
    inertes) e ``row_factory``. Não reimplemente nada disso.

    POR QUE ``check_same_thread=False``
    ===================================
    Idêntico ao caso já medido em ``app/deps.get_conn``, e a medição está lá:
    o FastAPI resolve uma dependência que é gerador síncrono com
    ``contextmanager_in_threadpool``, e o ``__enter__`` (que abre), o corpo da
    rota e o ``__exit__`` (que fecha) são **três chamadas distintas** de
    ``anyio.to_thread.run_sync`` — o AnyIO não promete a mesma worker thread
    entre elas, e o ``__exit__`` ainda usa um ``CapacityLimiter`` próprio. Com
    uma requisição em voo o pool reaproveita a thread ociosa e parece funcionar;
    com as duas ou três que a interface dispara no primeiro paint, não: o
    ``conn.close()`` levanta ``ProgrammingError: SQLite objects created in a
    thread can only be used in that same thread`` e a tela não abre.

    Isto **não** é o caso proibido do ``db.connect``: a conexão nasce, é usada e
    morre dentro de UMA requisição, uma operação de cada vez. O que atravessa a
    fronteira de thread é só o bastão entre as etapas dessa mesma requisição, e
    o módulo ``sqlite3`` do CPython é compilado em modo serializado.
    """
    conn = dbmod.connect(request.app.state.db_anotacao, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


def get_conn_corpus(request: Request) -> Iterator[sqlite3.Connection]:
    """Conexão SOMENTE LEITURA no ``prompts.sqlite``.

    ``readonly=True`` abre por URI com ``mode=ro``: qualquer INSERT/UPDATE por
    esta conexão levanta ``sqlite3.OperationalError: attempt to write a readonly
    database``. É garantia do SQLite, não convenção.

    **Aberta por request, nunca cacheada.** O corpus é trocado por swap de
    arquivo embaixo desta app a cada ``pf load-db`` (o s11 constrói ao lado e faz
    ``os.replace``). Uma conexão de longa duração continuaria lendo o INODE
    velho — respondendo com um corpus que não existe mais no disco, sem erro
    nenhum. Abrir e fechar custa ~0,1 ms; ler o banco errado custa a confiança na
    ferramenta inteira.

    ``check_same_thread=False`` pelo mesmo motivo do de cima.
    """
    conn = dbmod.connect(
        request.app.state.db_corpus, readonly=True, check_same_thread=False
    )
    try:
        yield conn
    finally:
        conn.close()


#: Açúcar para as assinaturas. Ler ``def rota(anot: ConAnotacao, corpus:
#: ConCorpus)`` diz, na própria assinatura, o que a rota pode escrever.
ConAnotacao = Annotated[sqlite3.Connection, Depends(get_conn_anotacao)]
ConCorpus = Annotated[sqlite3.Connection, Depends(get_conn_corpus)]


def estado(request: Request) -> dict[str, Any]:
    """Metadados fixos da app (caminhos dos dois bancos, versões).

    **Nada do CONTEÚDO do corpus entra aqui.** Contagem de linhas, build_id e
    origem do pool são lidos ao vivo, por request: o corpus troca por baixo, e
    um número congelado no lifespan seria uma mentira com data de validade.
    """
    return dict(request.app.state.meta)


Estado = Annotated[dict[str, Any], Depends(estado)]


def exigir_anotador(conn: sqlite3.Connection, anotador_id: int) -> sqlite3.Row:
    """O perfil, ou 404. A guarda que toda rota mutante do P2 vai usar."""
    linha = conn.execute(
        "SELECT id, nome, papel, ativo, criado_em FROM anotadores WHERE id = ?",
        (anotador_id,),
    ).fetchone()
    if linha is None:
        raise HTTPException(status_code=404, detail=f"perfil {anotador_id} não existe")
    return linha


def exigir_papel(linha: sqlite3.Row, *papeis: str) -> sqlite3.Row:
    """403 quando o perfil não tem um dos papéis pedidos.

    O papel vem do servidor (da linha em ``anotadores``), **nunca** do que o
    cliente afirma ser. Sem senha, a identidade é escolhida na tela; a
    autorização, não. É teatro de demonstração — mas teatro coerente, senão
    vira defeito de verdade no dia em que isto sair do ``127.0.0.1``.
    """
    if str(linha["papel"]) not in papeis:
        raise HTTPException(
            status_code=403,
            detail=(
                f"{linha['nome']} é {linha['papel']}; esta ação é de "
                f"{' ou '.join(papeis)}"
            ),
        )
    return linha


__all__ = [
    "ConAnotacao",
    "ConCorpus",
    "Estado",
    "estado",
    "exigir_anotador",
    "exigir_papel",
    "get_conn_anotacao",
    "get_conn_corpus",
]
