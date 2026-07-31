"""Rotas de export: gerar o arquivo, listar o histórico e baixar.

**O arquivo é escrito em disco e SÓ ENTÃO servido.** O briefing propunha
streamar a resposta HTTP e gravar em disco na mesma passada; o desenho aqui é
outro, de propósito, por três razões medidas:

1. O ``row_count``, o ``sha256`` e as contagens do manifesto só existem depois
   da última linha. Streamar obriga a mandar o corpo antes de saber o que ele
   contém, e um manifesto que descreve um arquivo que o cliente abortou no meio
   é pior que nenhum manifesto.
2. O Starlette itera geradores síncronos com ``iterate_in_threadpool``, numa
   thread que **não é garantidamente** a da rota. Uma conexão ``sqlite3`` que
   atravessa essa fronteira levanta ``ProgrammingError`` sob concorrência (a
   mesma armadilha de ``deps.py``). Escrevendo dentro da rota ``def``, a
   conexão nunca sai da thread dela.
3. "O que baixou == o que ficou salvo" fica trivialmente verdadeiro: o download
   é um ``FileResponse`` do mesmo arquivo cujo sha256 está no manifesto.

O custo é o cliente esperar a escrita terminar antes de o download começar.
Numa ferramenta local, com teto de ``[app] export_max_rows``, isso é segundos.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .. import export as exportmod
from .. import paths
from ..config import get as _cfg
from . import presenters, queries
from .deps import Conexao, Estado, exigir_colecao
from .models import ExportIn, Filtros, dump_filtros

router = APIRouter(tags=["export"])


def _destino(request: Request) -> Path:
    """Onde os arquivos são gravados. Vem de ``app.state`` (posto por
    ``criar_app``), não de uma constante: é o que permite o teste gerar exports
    de verdade num ``tmp_path`` sem tocar em ``data/``."""
    return Path(getattr(request.app.state, "exports_dir", paths.EXPORTS))


def _linhas_por_ids(conn: sqlite3.Connection, ids: list[int]) -> Any:
    return exportmod.linhas_em_lotes(conn, queries.sql_export_ids, sorted(ids))


def _fonte_das_linhas(
    conn: sqlite3.Connection, corpo: ExportIn
) -> tuple[Any, Filtros | None, dict[str, Any]]:
    """``(iterável de linhas, filtros efetivos, filtros serializados)``.

    Os três modos convergem para o mesmo iterador de linhas — é o que faz o
    formato de saída e o manifesto serem idênticos venha o recorte de onde vier.
    """
    if corpo.mode == "filter":
        assert corpo.filter is not None
        sql, params = queries.sql_export(corpo.filter)
        return (
            queries.executar_fts(conn, sql, params, corpo.filter.q),
            corpo.filter,
            dump_filtros(corpo.filter),
        )

    if corpo.mode == "collection":
        assert corpo.collection_id is not None
        colecao = exigir_colecao(conn, corpo.collection_id)
        # Uma coleção é só um filtro: reaproveitar o caminho de cima garante que
        # exportar uma coleção e exportar `?collection_id=N` deem o mesmo arquivo.
        filtro = Filtros(collection_id=corpo.collection_id, nsfw="include")
        sql, params = queries.sql_export(filtro)
        return (
            queries.executar_fts(conn, sql, params, None),
            filtro,
            {"collection_id": corpo.collection_id, "collection_name": str(colecao["name"])},
        )

    assert corpo.uids is not None
    unicos = list(dict.fromkeys(corpo.uids))
    achados: dict[str, int] = {}
    for i in range(0, len(unicos), 5000):
        fatia = unicos[i : i + 5000]
        marcas = ", ".join("?" * len(fatia))
        for linha in conn.execute(
            f"SELECT id, uid FROM prompts WHERE uid IN ({marcas})", fatia
        ):
            achados[str(linha["uid"])] = int(linha["id"])
    faltando = [u for u in unicos if u not in achados]
    if faltando:
        # Exportar em silêncio um subconjunto do que foi pedido produziria um
        # arquivo com menos linhas do que o usuário acha que pediu.
        raise HTTPException(
            status_code=400,
            detail=f"{len(faltando)} uid(s) não existem: {faltando[:10]}",
        )
    return (
        _linhas_por_ids(conn, list(achados.values())),
        None,
        {"n_uids": len(unicos)},
    )


@router.post("/api/export", summary="Gera um JSONL/CSV + manifesto")
def exportar(
    corpo: ExportIn, conn: Conexao, meta: Estado, request: Request
) -> dict[str, Any]:
    """Gera o arquivo em ``data/exports/`` e devolve o manifesto.

    ``redistributable = 0`` sai fora por padrão e o manifesto diz quantas linhas
    foram excluídas. ``commercial_ok = 0`` **entra** e é contabilizado à parte:
    a licença não comercial não impede o uso, ela impede *um certo* uso, e quem
    sabe qual é o destino é o usuário.
    """
    chave = corpo.chave_formato()
    if chave not in exportmod.REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=(
                f"perfil/formato {chave!r} não existe "
                f"(disponíveis: {', '.join(sorted(exportmod.REGISTRY))})"
            ),
        )

    linhas, _, filtros_serializados = _fonte_das_linhas(conn, corpo)
    try:
        resultado = exportmod.executar(
            linhas,
            destino_dir=_destino(request),
            chave_formato=chave,
            atribuicoes=presenters.atribuicoes(),
            nome=corpo.name,
            mode=corpo.mode,
            filtros=filtros_serializados,
            include_nonredistributable=corpo.include_nonredistributable,
            include_text_original=corpo.include_text_original,
            max_rows=int(_cfg("app", "export_max_rows", default=250_000)),
            meta_banco={
                "build_id": meta.get("db_build_id"),
                "schema_version": meta.get("schema_version"),
                "taxonomy_version": meta.get("taxonomy_version"),
            },
        )
    except ValueError as exc:  # teto de linhas estourado
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    export_id = exportmod.registrar(conn, resultado, filtros=filtros_serializados)
    return {
        "id": export_id,
        "file": resultado.arquivo.name,
        "path": str(resultado.arquivo),
        "manifest_file": resultado.manifesto.name,
        "row_count": resultado.row_count,
        "download_url": f"/api/exports/{export_id}/download",
        "manifest": resultado.manifest,
    }


@router.get("/api/exports", summary="Histórico de exports")
def historico(conn: Conexao) -> dict[str, Any]:
    """Lista o que já foi exportado, com o manifesto desserializado.

    ``exists`` diz se o arquivo ainda está lá (o usuário pode ter movido ou
    apagado), e ``filters`` volta pronto para a interface remontar a URL do
    recorte — é o que fecha o ciclo: voltar na segunda e reconstruir o recorte
    de sexta.
    """
    itens = []
    for linha in conn.execute(
        "SELECT id, format, path, filters_json, row_count, manifest_json, created_at "
        "FROM exports ORDER BY id DESC"
    ):
        caminho = Path(str(linha["path"]))
        itens.append(
            {
                "id": int(linha["id"]),
                "format": linha["format"],
                "file": caminho.name,
                "path": str(caminho),
                "exists": caminho.is_file(),
                "row_count": int(linha["row_count"]),
                "created_at": linha["created_at"],
                "filters": _json(linha["filters_json"]),
                "manifest": _json(linha["manifest_json"]),
                "download_url": f"/api/exports/{int(linha['id'])}/download",
            }
        )
    return {"items": itens}


@router.get("/api/exports/{export_id}/download", summary="Baixa um export gerado")
def baixar(export_id: int, conn: Conexao, request: Request) -> FileResponse:
    """O MESMO arquivo cujo sha256 está no manifesto."""
    linha = conn.execute(
        "SELECT path, format FROM exports WHERE id = ?", (export_id,)
    ).fetchone()
    if linha is None:
        raise HTTPException(status_code=404, detail=f"export {export_id} não existe")
    caminho = Path(str(linha["path"]))
    if not caminho.is_file():
        raise HTTPException(
            status_code=410, detail=f"o arquivo {caminho.name} não está mais em disco"
        )
    del request  # a assinatura documenta que a rota é síncrona (ver deps.py)
    return FileResponse(
        caminho,
        media_type="application/octet-stream",
        filename=caminho.name,
    )


def _json(bruto: object) -> dict[str, Any]:
    try:
        dados = json.loads(str(bruto or "{}"))
    except ValueError:
        return {}
    return dados if isinstance(dados, dict) else {}


__all__ = ["router"]
