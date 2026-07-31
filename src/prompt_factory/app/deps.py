"""Dependências das rotas: a conexão por request e as guardas compartilhadas.

REGRA DURA, NÃO NEGOCIÁVEL
==========================
**Nenhuma rota que toca o banco pode ser ``async def``.**

Uma rota ``async def`` roda no event loop; o ``sqlite3`` é síncrono e bloqueante,
e uma consulta de 200 ms ali dentro trava o servidor inteiro (inclusive as outras
abas e o download de export). Rota que toca o banco é ``def``, sempre, e o
Starlette a joga no threadpool.

O corolário sobre THREADS está em ``get_conn``, abaixo, e é onde mora a medição:
a dependência-gerador tem o ``__enter__``, o corpo da rota e o ``__exit__``
espalhados por chamadas independentes de ``anyio.to_thread.run_sync``, sem
garantia de afinidade de thread entre elas. Por isso a conexão por request nasce
com ``check_same_thread=False`` — leia o porquê lá antes de "consertar" isso.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request

from .. import db as dbmod


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """Uma conexão por request. SEMPRE consumida por rota ``def`` — ver acima.

    Abrir e fechar custa ~0,1 ms num banco em WAL, então não há pool: um pool
    traria de volta exatamente o problema de thread que a regra acima evita, em
    troca de um ganho que não existe.

    ``db.connect`` já aplica WAL, ``busy_timeout``, ``foreign_keys=ON`` (que é
    PRAGMA **por conexão** — sem ele o ON DELETE CASCADE das coleções fica
    inerte) e ``row_factory``. Não reimplemente nada disso aqui.

    POR QUE ``check_same_thread=False`` AQUI (e por que isto NÃO é o caso
    proibido do ``db.connect``)
    ==========================================================================
    O FastAPI resolve uma dependência que é **gerador síncrono** com
    ``contextmanager_in_threadpool``: o ``__enter__`` (que cria a conexão), o
    corpo da rota e o ``__exit__`` (que a fecha) são **três chamadas separadas**
    de ``anyio.to_thread.run_sync`` — e o AnyIO não promete a mesma worker
    thread entre chamadas distintas. Pior: o ``__exit__`` usa um
    ``CapacityLimiter`` próprio, então nem compartilha a fila das outras duas.

    Com **uma** requisição em voo o pool reaproveita a única thread ociosa e tudo
    parece funcionar — foi assim que a medição original ("4 de 4 na mesma
    thread") passou. Com duas ou mais em voo, não. E duas ou mais é o caso
    NORMAL: a interface dispara ``/api/health`` + ``/api/stats`` +
    ``/api/collections`` juntas no primeiro paint, e ``/api/prompts`` +
    ``/api/facets`` juntas a cada filtro.

    Medido contra esta app (24 requisições, mesmas rotas):

    ========================  ==========
    requisições em paralelo   respostas 500
    ========================  ==========
    1                          0
    4                         15
    12                        24
    ========================  ==========

    sempre com ``sqlite3.ProgrammingError: SQLite objects created in a thread
    can only be used in that same thread`` vindo do ``conn.close()`` daqui de
    baixo. Ou seja: a interface não abria de jeito nenhum.

    O que o ``db.connect`` proíbe é desligar a checagem numa conexão
    **realmente compartilhada** entre requisições concorrentes — ali a checagem
    é o que separa um erro alto de corrupção silenciosa. Aqui não há
    compartilhamento nenhum: a conexão nasce, é usada e morre dentro de UMA
    requisição, uma operação de cada vez. O que atravessa a fronteira de thread
    é só o bastão entre as etapas dessa mesma requisição, e o módulo ``sqlite3``
    do CPython é compilado em modo serializado — esse repasse é seguro.

    A regra de "nenhuma rota do banco pode ser ``async def``" **continua
    valendo**, agora por outro motivo: uma rota ``async def`` rodaria SQL
    síncrono direto no event loop e travaria o servidor inteiro a cada consulta.
    """
    conn = dbmod.connect(request.app.state.db_file, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


#: Açúcar para as assinaturas: ``def rota(conn: Conexao)``.
Conexao = Annotated[sqlite3.Connection, Depends(get_conn)]


def estado(request: Request) -> dict[str, Any]:
    """Metadados lidos uma vez no lifespan (``app_meta`` + caminho do banco)."""
    return dict(request.app.state.meta)


Estado = Annotated[dict[str, Any], Depends(estado)]


# ---------------------------------------------------------------------------
# guardas
# ---------------------------------------------------------------------------


def linha_por_uid(conn: sqlite3.Connection, uid: str, sql: str) -> sqlite3.Row:
    """Executa ``sql`` (parâmetro ``:uid``) e devolve a linha, ou levanta 404."""
    linha = conn.execute(sql, {"uid": uid}).fetchone()
    if linha is None:
        raise HTTPException(status_code=404, detail=f"prompt {uid!r} não existe")
    return linha


def id_por_uid(conn: sqlite3.Connection, uid: str) -> int:
    """O rowid interno a partir do uid público, ou 404."""
    linha = conn.execute("SELECT id FROM prompts WHERE uid = ?", (uid,)).fetchone()
    if linha is None:
        raise HTTPException(status_code=404, detail=f"prompt {uid!r} não existe")
    return int(linha["id"])


def exigir_colecao(conn: sqlite3.Connection, colecao_id: int) -> sqlite3.Row:
    """A coleção, ou 404."""
    linha = conn.execute(
        "SELECT id, name, description, created_at, updated_at FROM collections WHERE id = ?",
        (colecao_id,),
    ).fetchone()
    if linha is None:
        raise HTTPException(status_code=404, detail=f"coleção {colecao_id} não existe")
    return linha


def rotulagem_pendente(conn: sqlite3.Connection) -> bool:
    """``True`` quando NENHUMA linha tem ``task_type``.

    Enquanto a campanha do M6/M7 não roda, isto é ``True`` no banco inteiro. A
    API **não pode** simplesmente devolver facetas vazias nesse caso: some da
    tela e parece bug. Ela devolve as classes da taxonomia com contagem 0 e
    levanta esta bandeira, para a interface poder desabilitar o grupo com uma
    explicação ("rotulagem pendente") em vez de escondê-lo.

    ``EXISTS`` com ``idx_prompts_task`` para no primeiro acerto: é O(1), não uma
    contagem do corpus.
    """
    linha = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM prompts WHERE task_type IS NOT NULL) AS tem"
    ).fetchone()
    return not bool(linha["tem"])


def marcar_atualizado() -> str:
    """A expressão SQL de ``updated_at``. Não há trigger para isto de propósito:
    um trigger dispararia na carga bulk do s11 e mentiria dizendo que 189 mil
    linhas foram curadas por um humano."""
    return "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


__all__ = [
    "Conexao",
    "Estado",
    "estado",
    "exigir_colecao",
    "get_conn",
    "id_por_uid",
    "linha_por_uid",
    "marcar_atualizado",
    "rotulagem_pendente",
]
