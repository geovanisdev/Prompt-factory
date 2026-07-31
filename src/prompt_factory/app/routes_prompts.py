"""Rotas dos prompts: listar, ver, editar, reverter, facetar e o panorama.

Todas as rotas daqui são ``def``, nunca ``async def`` — ver a regra dura em
``deps.py``. Não é estilo: ``async def`` + dependência ``sqlite3`` levanta
``ProgrammingError`` na primeira query, porque a conexão nasce numa thread e é
usada em outra.
"""

from __future__ import annotations

import math
import sqlite3
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from .. import schema
from . import presenters, queries
from .deps import Conexao, id_por_uid, linha_por_uid, marcar_atualizado, rotulagem_pendente
from .models import ConsultaPrompts, Filtros, PatchPrompt

router = APIRouter(tags=["prompts"])


# ---------------------------------------------------------------------------
# rótulos legíveis da taxonomia
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _nomes_taxonomia() -> dict[str, dict[str, str]]:
    """``{"task_type": {chave: nome legível}, "domain": {...}}``.

    Vem do ``labeling/taxonomy.json``, que é a fonte única. A interface não
    deveria ter uma segunda cópia dos 32 nomes: duas cópias divergem, e a que
    diverge é sempre a da tela. Taxonomia ilegível não derruba a rota — cai
    para as próprias chaves.
    """
    try:
        tax = schema.load_taxonomy()
    except (OSError, ValueError):
        return {"task_type": {}, "domain": {}}
    return {
        secao: {k: str(v.get("nome", k)) for k, v in tax.get(secao, {}).items()}
        for secao in ("task_type", "domain")
    }


#: Dimensões cujo valor é booleano no banco (0/1) e sai como bool no JSON.
_FACETAS_BOOL = frozenset({"nsfw", "needs_review", "edited", "pii_found"})

#: Rótulos legíveis das dimensões booleanas. ``str(True)`` daria "True" na
#: barra lateral de uma interface em português — e, pior, "None" onde o que o
#: usuário precisa ler é "não rotulado".
_ROTULOS_BOOL: dict[bool | None, str] = {True: "sim", False: "não", None: "não rotulado"}


def _rotulo(dimensao: str, valor: Any) -> str:
    if isinstance(valor, bool) or (valor is None and dimensao in _FACETAS_BOOL):
        return _ROTULOS_BOOL[valor]
    if valor is None:
        return "(sem rótulo)" if dimensao in ("task_type", "domain") else "(vazio)"
    nomes = _nomes_taxonomia().get(dimensao, {})
    return nomes.get(str(valor), str(valor))


# ---------------------------------------------------------------------------
# GET /api/prompts
# ---------------------------------------------------------------------------


@router.get("/api/prompts", summary="Página de prompts sob o filtro atual")
def listar(conn: Conexao, consulta: Annotated[ConsultaPrompts, Query()]) -> dict[str, Any]:
    """A listagem paginada.

    Duas coisas que o cliente **não** pode assumir:

    * ``text`` vem CORTADO (``[app] list_text_chars``); ``n_chars`` diz o
      tamanho real e ``text_truncated`` diz se cortou. O texto inteiro sai só
      em ``GET /api/prompts/{uid}``.
    * ``sort`` nem sempre é o que foi pedido: ``relevance`` sem ``q`` não
      existe e vira ``newest``. O que valeu está em ``sort_efetivo``.
    """
    _, _, teto = queries.limites()
    if consulta.page * consulta.page_size > teto:
        # Um OFFSET gigante custa caro e ninguém pagina até a linha 100.000 com
        # a mão. Quem precisa de tudo usa o export ou o `from-filter`.
        raise HTTPException(
            status_code=400,
            detail=(
                f"page * page_size = {consulta.page * consulta.page_size} passa do teto "
                f"de {teto} ([app] max_offset). Refine o filtro, ou use "
                "POST /api/export / from-filter para levar o conjunto inteiro."
            ),
        )

    corte, _, _ = queries.limites()
    sql, params, efetivo = queries.sql_lista(consulta)
    linhas = queries.executar_fts(conn, sql, params, consulta.q)

    ids = [int(r["id"]) for r in linhas]
    trechos = queries.buscar_snippets(conn, ids, consulta.q)

    sql_c, params_c = queries.sql_contagem(consulta.filtros())
    total = int(queries.executar_fts(conn, sql_c, params_c, consulta.q)[0]["n"])

    return {
        "total": total,
        # Sempre False aqui (o count é exato). O campo existe para a busca
        # semântica do M10, que devolve top-k e portanto um total truncado —
        # assim o cliente não precisa de dois formatos de envelope.
        "truncated_total": False,
        "page": consulta.page,
        "page_size": consulta.page_size,
        "pages": max(1, math.ceil(total / consulta.page_size)) if total else 0,
        "sort": consulta.sort,
        "sort_efetivo": efetivo,
        "seed": consulta.seed,
        "q": consulta.q,
        # A expressão que REALMENTE foi ao FTS5, depois do sanitizador. É o que
        # explica "busquei `***` e não veio nada": q_fts sai vazia.
        "q_fts": queries.sanitize_fts(consulta.q) or None,
        "items": [
            presenters.item_lista(linha, corte=corte, snippet=trechos.get(int(linha["id"])))
            for linha in linhas
        ],
    }


# ---------------------------------------------------------------------------
# GET /api/prompts/{uid}
# ---------------------------------------------------------------------------


def _colecoes_do_item(conn: sqlite3.Connection, prompt_id: int) -> list[dict[str, Any]]:
    return [
        {"id": int(r["id"]), "name": str(r["name"])}
        for r in conn.execute(
            "SELECT c.id, c.name FROM collections c "
            "JOIN collection_items ci ON ci.collection_id = c.id "
            "WHERE ci.prompt_id = ? ORDER BY c.name",
            (prompt_id,),
        )
    ]


def _detalhe(conn: sqlite3.Connection, uid: str) -> dict[str, Any]:
    linha = linha_por_uid(conn, uid, queries.SQL_DETALHE)
    return presenters.item_detalhe(
        linha, colecoes=_colecoes_do_item(conn, int(linha["id"]))
    )


@router.get("/api/prompts/{uid}", summary="O item completo, com o texto inteiro")
def detalhe(uid: str, conn: Conexao) -> dict[str, Any]:
    """Texto sem corte, ``text_original``, procedência, ``attribution`` e coleções."""
    return _detalhe(conn, uid)


# ---------------------------------------------------------------------------
# PATCH /api/prompts/{uid}
# ---------------------------------------------------------------------------

#: Campos de rótulo. Mexer em qualquer um deles carimba ``label_method='manual'``
#: e tira o item da fila de revisão.
_CAMPOS_ROTULO = ("task_type", "domain", "quality", "nsfw")


@router.patch("/api/prompts/{uid}", summary="Edita o texto e/ou os rótulos de um item")
def editar(uid: str, corpo: PatchPrompt, conn: Conexao) -> dict[str, Any]:
    """Edição manual. Devolve a linha inteira, para o cliente reconciliar.

    **A preservação do original é feita pelo próprio UPDATE**, não por um SELECT
    antes:

    .. code-block:: sql

        text_original = CASE WHEN edited = 0 THEN text ELSE text_original END

    O SQLite avalia o SET inteiro contra os valores ANTIGOS da linha, então o
    ``text`` desse CASE é o texto de antes desta edição. Primeira edição copia o
    original; a segunda encontra ``edited = 1`` e preserva o que já estava lá.
    Ler-depois-escrever daria o mesmo resultado em duas viagens e com uma
    janela de corrida no meio.

    ``label_confidence`` vira **NULL** num rótulo manual, nunca 1.0: confiança
    de humano não é probabilidade, e um 1.0 inventado envenenaria a fila de
    revisão, que é ordenada justamente por essa coluna.
    """
    prompt_id = id_por_uid(conn, uid)
    campos = corpo.model_dump(exclude_unset=True)

    sets: list[str] = []
    params: dict[str, Any] = {"id": prompt_id}

    if "text" in campos:
        sets.append("text = :text")
        sets.append("text_original = CASE WHEN edited = 0 THEN text ELSE text_original END")
        sets.append("edited = 1")
        params["text"] = campos["text"]

    mexeu_rotulo = any(campo in campos for campo in _CAMPOS_ROTULO)
    for campo in _CAMPOS_ROTULO:
        if campo not in campos:
            continue
        valor = campos[campo]
        sets.append(f"{campo} = :{campo}")
        params[campo] = None if valor is None else int(valor) if campo in ("quality", "nsfw") else valor
    if mexeu_rotulo:
        sets.append("label_method = 'manual'")
        sets.append("label_confidence = NULL")

    if "needs_review" in campos:
        sets.append("needs_review = :needs_review")
        params["needs_review"] = int(bool(campos["needs_review"]))
    elif mexeu_rotulo:
        # Quem acabou de rotular à mão já revisou: o item sai da fila.
        sets.append("needs_review = 0")

    sets.append(f"updated_at = {marcar_atualizado()}")

    # Um único UPDATE: texto e rótulo mudam juntos ou não mudam. Uma instrução
    # já é atômica no SQLite, então não há BEGIN aqui — dois UPDATEs, sim,
    # precisariam de transação (e é por isso que não são dois).
    cur = conn.execute(
        f"UPDATE prompts SET {', '.join(sets)} WHERE id = :id", params
    )
    if cur.rowcount == 0:  # pragma: no cover - a linha existia dois passos atrás
        raise HTTPException(status_code=404, detail=f"prompt {uid!r} sumiu durante a edição")
    # O trigger prompts_fts_au reindexa sozinho — mas SÓ quando `text` aparece
    # no SET (ele é AFTER UPDATE OF text). Corrigir um rótulo não mexe no FTS,
    # que é exatamente o que se quer num índice de 189 mil textos.
    return _detalhe(conn, uid)


# ---------------------------------------------------------------------------
# POST /api/prompts/{uid}/revert
# ---------------------------------------------------------------------------


@router.post("/api/prompts/{uid}/revert", summary="Desfaz a edição de texto")
def reverter(uid: str, conn: Conexao) -> dict[str, Any]:
    """Volta ao texto original e zera ``text_original``.

    O ``AND edited = 1`` no WHERE é a guarda: reverter um item nunca editado
    não é um no-op simpático, é sinal de que o cliente está com estado velho —
    e o silêncio faria a interface mostrar "revertido" sobre nada.
    """
    prompt_id = id_por_uid(conn, uid)
    cur = conn.execute(
        "UPDATE prompts SET text = text_original, text_original = NULL, edited = 0, "
        f"updated_at = {marcar_atualizado()} "
        "WHERE id = :id AND edited = 1 AND text_original IS NOT NULL",
        {"id": prompt_id},
    )
    if cur.rowcount == 0:
        raise HTTPException(
            status_code=400, detail=f"prompt {uid!r} não foi editado — não há o que reverter"
        )
    return _detalhe(conn, uid)


# ---------------------------------------------------------------------------
# GET /api/facets
# ---------------------------------------------------------------------------

def _selecionado(f: Filtros, faceta: queries.Faceta, valor: Any) -> bool:
    """A opção está marcada no filtro atual?"""
    if faceta.nome in ("needs_review", "edited"):
        return getattr(f, faceta.nome) is valor
    if faceta.nome == "nsfw":
        return (f.nsfw == "only" and valor is True) or (f.nsfw == "include" and valor is None)
    if faceta.nome == "pii_found":
        return (f.pii == "only" and valor is True) or (f.pii == "none" and valor is False)
    if faceta.nome == "n_chars_bucket":
        return False  # a seleção de tamanho é uma faixa, não um valor
    escolhidos = getattr(f, faceta.nome, None)
    return bool(escolhidos) and valor in escolhidos


def _faixa_do_bucket(rotulo: str) -> dict[str, int]:
    """O filtro que a opção de tamanho aplica (``n_chars_min``/``n_chars_max``)."""
    curto, medio = queries.bordas_tamanho()
    return {
        "curto": {"n_chars_max": curto - 1},
        "medio": {"n_chars_min": curto, "n_chars_max": medio - 1},
        "longo": {"n_chars_min": medio},
    }[rotulo]


@router.get("/api/facets", summary="Contagens por dimensão, sob o filtro atual")
def facetas(conn: Conexao, filtros: Annotated[Filtros, Query()]) -> dict[str, Any]:
    """As contagens da barra lateral.

    Duas regras que o cliente precisa conhecer para não se confundir com os
    números:

    1. **Cada dimensão é contada com o filtro DELA MESMA removido.** Se não
       fosse assim, marcar "pt" zeraria a contagem de "en" e o usuário ficaria
       preso no primeiro clique. Consequência direta: uma dimensão **não
       filtrada** soma exatamente ``total``; uma dimensão **filtrada** soma
       mais que ``total``, e isso é o comportamento correto.
    2. **``task_type`` e ``domain`` saem na ordem da taxonomia, com os zeros.**
       Uma classe com 0 itens precisa aparecer desabilitada; sumir da lista faz
       parecer que a classe não existe. Enquanto ``rotulagem_pendente`` for
       ``true``, TODAS elas são zero e a interface deve desabilitar o grupo com
       a explicação, não escondê-lo.
    """
    sql_c, params_c = queries.sql_contagem(filtros)
    total = int(queries.executar_fts(conn, sql_c, params_c, filtros.q)[0]["n"])

    saida: dict[str, list[dict[str, Any]]] = {}
    for faceta in queries.FACETAS:
        sql, params = queries.sql_faceta(filtros, faceta)
        bruto: dict[Any, int] = {}
        for linha in queries.executar_fts(conn, sql, params, filtros.q):
            valor = linha["v"]
            if valor is not None and faceta.nome in _FACETAS_BOOL:
                valor = bool(valor)
            elif valor is not None and faceta.nome == "quality":
                valor = int(valor)
            bruto[valor] = int(linha["n"])

        opcoes: list[dict[str, Any]] = []
        vistos: set[Any] = set()
        for valor in faceta.vocabulario:
            if faceta.nome in _FACETAS_BOOL:
                valor = bool(valor)
            vistos.add(valor)
            opcoes.append(_opcao(filtros, faceta, valor, bruto.get(valor, 0)))
        # Valores que existem no banco mas não estão no vocabulário fixo (fonte
        # nova, licença nova, classe de uma taxonomia mais recente que o código).
        extras = [
            (v, n) for v, n in bruto.items() if v is not None and v not in vistos
        ]
        for valor, n in sorted(extras, key=lambda par: (-par[1], str(par[0]))):
            opcoes.append(_opcao(filtros, faceta, valor, n))
        if None in bruto and faceta.nome != "n_chars_bucket":
            opcoes.append(_opcao(filtros, faceta, None, bruto[None]))
        saida[faceta.nome] = opcoes

    return {
        "total": total,
        "rotulagem_pendente": rotulagem_pendente(conn),
        "facets": saida,
    }


def _opcao(f: Filtros, faceta: queries.Faceta, valor: Any, n: int) -> dict[str, Any]:
    opcao: dict[str, Any] = {
        "value": valor,
        "label": _rotulo(faceta.nome, valor),
        "count": n,
        "selected": _selecionado(f, faceta, valor),
    }
    if faceta.nome == "n_chars_bucket" and valor is not None:
        # Esta é a única faceta cujo `value` não vai direto num campo de filtro:
        # ela aplica uma FAIXA. `filter` já vem pronto para ser espalhado nos
        # parâmetros da consulta.
        opcao["filter"] = _faixa_do_bucket(str(valor))
    return opcao


# ---------------------------------------------------------------------------
# GET /api/stats
# ---------------------------------------------------------------------------


def _grupo(conn: sqlite3.Connection, coluna: str, dimensao: str | None = None) -> list[dict[str, Any]]:
    """``[{value, label, count}]`` de um GROUP BY simples (coluna é literal)."""
    return [
        {
            "value": r["v"],
            "label": _rotulo(dimensao or coluna, r["v"]),
            "count": int(r["n"]),
        }
        for r in conn.execute(
            f"SELECT {coluna} AS v, count(*) AS n FROM prompts GROUP BY v ORDER BY n DESC"
        )
    ]


@router.get("/api/stats", summary="Panorama do banco (sem filtro)")
def stats(conn: Conexao) -> dict[str, Any]:
    """O tamanho e a forma do corpus — a tela de abertura, antes de filtrar."""
    total = int(conn.execute("SELECT count(*) AS n FROM prompts").fetchone()["n"])
    _, marc, _ = queries.limites()

    contagens = conn.execute(
        f"""
        SELECT
          sum(pii_found IS 1)                   AS pii,
          sum(edited IS 1)                      AS editados,
          sum(needs_review IS 1)                AS needs_review,
          sum(nsfw IS 1)                        AS nsfw_sim,
          sum(nsfw IS 0)                        AS nsfw_nao,
          sum(nsfw IS NULL)                     AS nsfw_nulo,
          sum(task_type IS NOT NULL)            AS rotulados,
          sum(n_exact_dups > 0)                 AS com_duplicata,
          sum(substr(text, 1, {marc}) LIKE '%User:%'
              OR substr(text, 1, {marc}) LIKE '%Assistant:%') AS com_marcadores_chat
        FROM prompts
        """
    ).fetchone()

    # O robô: os textos que mais se repetem. É o sinal que separa corpus de
    # automação, e é o que justifica o filtro `max_dups`.
    top_dups = [
        {
            "uid": str(r["uid"]),
            "n_exact_dups": int(r["n_exact_dups"]),
            "source": r["source"],
            "lang": r["lang"],
            "preview": str(r["preview"]),
        }
        for r in conn.execute(
            "SELECT uid, n_exact_dups, source, lang, substr(text, 1, 160) AS preview "
            "FROM prompts WHERE n_exact_dups > 0 ORDER BY n_exact_dups DESC, id ASC LIMIT 10"
        )
    ]

    return {
        "total": total,
        "rotulagem_pendente": rotulagem_pendente(conn),
        "por_lang": _grupo(conn, "lang"),
        "por_lang_variant": _grupo(conn, "lang_variant"),
        "por_source": _grupo(conn, "source"),
        "por_license": _grupo(conn, "license"),
        "por_task_type": _grupo(conn, "task_type"),
        "por_domain": _grupo(conn, "domain"),
        "por_quality": _grupo(conn, "quality"),
        "por_label_method": _grupo(conn, "label_method"),
        "nsfw": {
            "sim": int(contagens["nsfw_sim"] or 0),
            "nao": int(contagens["nsfw_nao"] or 0),
            "nulo": int(contagens["nsfw_nulo"] or 0),
        },
        "pii_found": int(contagens["pii"] or 0),
        "editados": int(contagens["editados"] or 0),
        "needs_review": int(contagens["needs_review"] or 0),
        "rotulados": int(contagens["rotulados"] or 0),
        "sem_rotulo": total - int(contagens["rotulados"] or 0),
        "com_duplicata_exata": int(contagens["com_duplicata"] or 0),
        "com_marcadores_chat": int(contagens["com_marcadores_chat"] or 0),
        "top_duplicados": top_dups,
        # A taxonomia inteira, na ordem canônica: é ela que a interface usa para
        # desenhar os filtros mesmo quando NENHUMA classe tem item ainda.
        "taxonomia": {
            "version": schema.TAXONOMY_VERSION,
            "task_type": [
                {"value": k, "label": _rotulo("task_type", k)} for k in schema.TASK_TYPES
            ],
            "domain": [
                {"value": k, "label": _rotulo("domain", k)} for k in schema.DOMAINS
            ],
            "quality": list(schema.QUALITY_VALUES),
        },
        "size_buckets": {
            "edges": list(queries.bordas_tamanho()),
            "labels": ["curto", "medio", "longo"],
        },
    }


__all__ = ["router"]
