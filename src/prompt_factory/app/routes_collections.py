"""Rotas das coleções — a mesa onde o usuário empilha o que vai exportar.

A operação que define este módulo é **``from-filter``**: adicionar (ou remover)
de uma vez todos os N itens que casam com um filtro, sem o cliente paginar.
Sem ela, montar uma coleção de 10 mil itens seriam 10 mil cliques, e tirar a
família do robô que responde por ~34% do português do WildChat seria
impossível na prática. Ela recebe **o mesmo objeto ``Filtros``** da listagem e
das facetas, e é isso que garante que a contagem prometida pela barra lateral
seja a contagem que entra na coleção.

Para LISTAR os itens de uma coleção não há rota própria: é
``GET /api/prompts?collection_id=N``. Assim a coleção herda de graça a busca, a
ordenação, as facetas e a paginação — e nunca diverge delas.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException, Response

from . import presenters, queries
from .deps import Conexao, exigir_colecao, marcar_atualizado
from .models import ColecaoIn, ColecaoPatch, ItensPorFiltro, ItensUids

router = APIRouter(tags=["colecoes"])

#: Linhas por lote nos INSERT/DELETE em massa. Sem lote, um ``from-filter`` de
#: 189 mil itens estouraria o teto de variáveis ligadas do SQLite.
LOTE = 5000


def _n_itens(conn: sqlite3.Connection, colecao_id: int) -> int:
    return int(
        conn.execute(
            "SELECT count(*) AS n FROM collection_items WHERE collection_id = ?",
            (colecao_id,),
        ).fetchone()["n"]
    )


def _tocar(conn: sqlite3.Connection, colecao_id: int) -> None:
    conn.execute(
        f"UPDATE collections SET updated_at = {marcar_atualizado()} WHERE id = ?",
        (colecao_id,),
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.get("/api/collections", summary="Todas as coleções, com o tamanho de cada uma")
def listar(conn: Conexao) -> dict[str, Any]:
    linhas = conn.execute(
        "SELECT c.id, c.name, c.description, c.created_at, c.updated_at, "
        "       count(ci.prompt_id) AS n_items "
        "FROM collections c "
        "LEFT JOIN collection_items ci ON ci.collection_id = c.id "
        "GROUP BY c.id ORDER BY c.name"
    ).fetchall()
    return {"items": [presenters.colecao(r, int(r["n_items"])) for r in linhas]}


@router.post("/api/collections", status_code=201, summary="Cria uma coleção")
def criar(corpo: ColecaoIn, conn: Conexao) -> dict[str, Any]:
    """409 em nome repetido — o ``UNIQUE`` do DDL é quem decide.

    Conferir com um SELECT antes seria uma corrida; deixar o banco recusar e
    traduzir o erro é o único jeito correto (e ainda vale para dois processos).
    """
    try:
        cur = conn.execute(
            "INSERT INTO collections (name, description) VALUES (?, ?)",
            (corpo.name, corpo.description),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail=f"já existe uma coleção chamada {corpo.name!r}"
        ) from exc
    return presenters.colecao(exigir_colecao(conn, int(cur.lastrowid or 0)), 0)


@router.get("/api/collections/{colecao_id}", summary="Uma coleção")
def obter(colecao_id: int, conn: Conexao) -> dict[str, Any]:
    return presenters.colecao(
        exigir_colecao(conn, colecao_id), _n_itens(conn, colecao_id)
    )


@router.patch("/api/collections/{colecao_id}", summary="Renomeia ou redescreve")
def editar(colecao_id: int, corpo: ColecaoPatch, conn: Conexao) -> dict[str, Any]:
    exigir_colecao(conn, colecao_id)
    campos = corpo.model_dump(exclude_unset=True)
    sets = [f"{nome} = :{nome}" for nome in campos]
    sets.append(f"updated_at = {marcar_atualizado()}")
    try:
        conn.execute(
            f"UPDATE collections SET {', '.join(sets)} WHERE id = :id",
            {**campos, "id": colecao_id},
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail=f"já existe uma coleção chamada {campos.get('name')!r}"
        ) from exc
    return presenters.colecao(
        exigir_colecao(conn, colecao_id), _n_itens(conn, colecao_id)
    )


@router.delete("/api/collections/{colecao_id}", status_code=204, summary="Apaga a coleção")
def apagar(colecao_id: int, conn: Conexao) -> Response:
    """Os itens vão junto pelo ``ON DELETE CASCADE`` — que só funciona porque
    ``db.connect`` liga ``foreign_keys``, um PRAGMA **por conexão**. Os prompts
    em si não são tocados: coleção é um recorte, não uma cópia."""
    exigir_colecao(conn, colecao_id)
    conn.execute("DELETE FROM collections WHERE id = ?", (colecao_id,))
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# itens
# ---------------------------------------------------------------------------


def _inserir(conn: sqlite3.Connection, colecao_id: int, ids: list[int]) -> int:
    """Insere ignorando repetidos; devolve quantos ENTRARAM de fato.

    ``ON CONFLICT DO NOTHING`` torna a operação idempotente (mandar a mesma
    lista duas vezes não é erro nem duplica). O ``changes()`` não serve para
    contar aqui — depois de um ``executemany`` ele reporta só a última
    instrução —, então a conta é a diferença do total.
    """
    antes = _n_itens(conn, colecao_id)
    # BEGIN explícito: a conexão do projeto é autocommit, e sem transação um
    # from-filter de 100 mil itens viraria 100 mil commits com fsync.
    conn.execute("BEGIN")
    try:
        for i in range(0, len(ids), LOTE):
            conn.executemany(
                "INSERT INTO collection_items (collection_id, prompt_id) VALUES (?, ?) "
                "ON CONFLICT DO NOTHING",
                [(colecao_id, ident) for ident in ids[i : i + LOTE]],
            )
        _tocar(conn, colecao_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _n_itens(conn, colecao_id) - antes


def _remover(conn: sqlite3.Connection, colecao_id: int, ids: list[int]) -> int:
    antes = _n_itens(conn, colecao_id)
    conn.execute("BEGIN")
    try:
        for i in range(0, len(ids), LOTE):
            fatia = ids[i : i + LOTE]
            marcas = ", ".join("?" * len(fatia))
            conn.execute(
                f"DELETE FROM collection_items WHERE collection_id = ? AND prompt_id IN ({marcas})",
                (colecao_id, *fatia),
            )
        _tocar(conn, colecao_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return antes - _n_itens(conn, colecao_id)


def _ids_dos_uids(conn: sqlite3.Connection, uids: list[str]) -> tuple[list[int], list[str]]:
    """``(ids encontrados, uids inexistentes)``.

    Resolver aqui em vez de deixar a FK explodir é o que permite responder
    "estes 3 uids não existem" em vez de um 400 genérico — o cliente trabalha
    com uids e nunca viu o rowid interno.
    """
    achados: dict[str, int] = {}
    unicos = list(dict.fromkeys(uids))  # preserva a ordem, mata repetição
    for i in range(0, len(unicos), LOTE):
        fatia = unicos[i : i + LOTE]
        marcas = ", ".join("?" * len(fatia))
        for linha in conn.execute(
            f"SELECT id, uid FROM prompts WHERE uid IN ({marcas})", fatia
        ):
            achados[str(linha["uid"])] = int(linha["id"])
    faltando = [u for u in unicos if u not in achados]
    return [achados[u] for u in unicos if u in achados], faltando


def _ids_do_filtro(conn: sqlite3.Connection, corpo: ItensPorFiltro) -> list[int]:
    sql, params = queries.sql_ids(
        corpo.filter, sort=corpo.sort, seed=corpo.seed, limit=corpo.limit
    )
    return [int(r["id"]) for r in queries.executar_fts(conn, sql, params, corpo.filter.q)]


@router.post("/api/collections/{colecao_id}/items", summary="Adiciona uids à coleção")
def adicionar(colecao_id: int, corpo: ItensUids, conn: Conexao) -> dict[str, Any]:
    exigir_colecao(conn, colecao_id)
    ids, faltando = _ids_dos_uids(conn, corpo.uids)
    adicionados = _inserir(conn, colecao_id, ids)
    return {
        "collection_id": colecao_id,
        "added": adicionados,
        "already_present": len(ids) - adicionados,
        "not_found": faltando,
        "n_items": _n_itens(conn, colecao_id),
    }


@router.post("/api/collections/{colecao_id}/items/remove", summary="Remove uids da coleção")
def remover(colecao_id: int, corpo: ItensUids, conn: Conexao) -> dict[str, Any]:
    exigir_colecao(conn, colecao_id)
    ids, faltando = _ids_dos_uids(conn, corpo.uids)
    return {
        "collection_id": colecao_id,
        "removed": _remover(conn, colecao_id, ids),
        "not_found": faltando,
        "n_items": _n_itens(conn, colecao_id),
    }


@router.post(
    "/api/collections/{colecao_id}/items/from-filter",
    summary="Adiciona TODOS os itens que casam com um filtro",
)
def adicionar_por_filtro(
    colecao_id: int, corpo: ItensPorFiltro, conn: Conexao
) -> dict[str, Any]:
    """"Adicionar os 12.483 que estou vendo", numa requisição.

    ``matched`` é quantos o filtro achou e ``added`` quantos entraram — a
    diferença são os que já estavam lá. Os dois são devolvidos porque a
    interface precisa dizer "1.204 adicionados (280 já estavam)" em vez de um
    número que o usuário não consegue conferir.
    """
    exigir_colecao(conn, colecao_id)
    ids = _ids_do_filtro(conn, corpo)
    adicionados = _inserir(conn, colecao_id, ids)
    return {
        "collection_id": colecao_id,
        "matched": len(ids),
        "added": adicionados,
        "already_present": len(ids) - adicionados,
        "n_items": _n_itens(conn, colecao_id),
    }


@router.post(
    "/api/collections/{colecao_id}/items/remove-from-filter",
    summary="Remove da coleção TODOS os itens que casam com um filtro",
)
def remover_por_filtro(
    colecao_id: int, corpo: ItensPorFiltro, conn: Conexao
) -> dict[str, Any]:
    """O simétrico do ``from-filter`` — é assim que a família inteira do robô
    sai da coleção de uma vez (``max_dups`` invertido, ou uma busca pelo
    prefixo do template)."""
    exigir_colecao(conn, colecao_id)
    ids = _ids_do_filtro(conn, corpo)
    return {
        "collection_id": colecao_id,
        "matched": len(ids),
        "removed": _remover(conn, colecao_id, ids),
        "n_items": _n_itens(conn, colecao_id),
    }


__all__ = ["router"]
