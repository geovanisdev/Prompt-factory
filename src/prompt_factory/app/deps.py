"""Dependências das rotas: a conexão por request e as guardas compartilhadas.

REGRA DURA, MEDIDA, NÃO NEGOCIÁVEL
==================================
**Nenhuma rota que toca o banco pode ser ``async def``.**

O ``sqlite3`` amarra a conexão à thread que a criou. Uma rota ``def``
(síncrona) e a dependência ``get_conn`` rodam na MESMA thread do threadpool —
verificado, 4 de 4 execuções — e portanto funcionam com o
``check_same_thread=True`` que ``db.connect`` usa por padrão. Trocar a rota para
``async def`` põe a rota no event loop e a dependência síncrona no threadpool:
threads diferentes, e a primeira query morre com

    sqlite3.ProgrammingError: SQLite objects created in a thread can only be
    used in that same thread

Este é um bug que só aparece quando alguém "otimiza" a rota para async, e ele
não aparece em revisão de código porque o diff parece uma melhoria. Se você veio
parar aqui atrás desse erro: **tire o ``async``, não desligue o
``check_same_thread``.** Desligar troca uma exceção alta por corrupção
silenciosa sob concorrência.

(O único lugar do projeto que legitimamente usa ``check_same_thread=False`` é o
download de export, onde o Starlette itera o gerador num threadpool que pode
não ser o da rota. Lá a conexão nasce e morre dentro do próprio gerador.)
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
    """
    conn = dbmod.connect(request.app.state.db_file)
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
