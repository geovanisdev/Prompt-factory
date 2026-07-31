"""Rotas da busca por sentido (M10).

Três rotas, e as duas menores existem por causa da maior:

* ``GET /api/semantic`` — a busca.
* ``GET /api/semantic/status`` — está disponível? já aqueceu? quanto ocupa?
* ``POST /api/semantic/warmup`` — comece a aquecer agora, numa thread, e me
  responda na hora.

O modelo e5 tem ~450 MB e a matriz ~212 MiB: a PRIMEIRA busca paga alguns
segundos. Sem as duas rotas de apoio, a interface só teria duas opções ruins —
aquecer no ``pf serve`` (que passaria a levar segundos mesmo para quem nunca vai
usar a semântica) ou deixar o usuário olhando uma tela parada sem explicação. Com
elas, o clique que liga o modo "sentido" dispara o aquecimento e a tela diz o que
está acontecendo.

Todas são ``def``, nunca ``async def`` — a regra dura de ``deps.py``. Vale em
dobro aqui: além do SQL síncrono, o produto de matrizes segura o GIL por ~11 ms e
o carregamento por segundos. No event loop, isso seria o servidor inteiro parado.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from . import presenters, queries, semantic
from .deps import Conexao, Semantica
from .models import ConsultaSemantica

router = APIRouter(tags=["semântica"])


def _hidratar(
    conn: sqlite3.Connection, ids: list[int], escores: list[float]
) -> list[dict[str, Any]]:
    """Ids + escores → itens no mesmo formato de ``GET /api/prompts``.

    O ``IN`` do SQLite **não preserva a ordem da lista**, e a ordem aqui é o
    resultado inteiro: reordenar em Python, pelo índice, é obrigatório. Com 50
    ids isto custa 0,1 ms (medido no banco real) — o texto já sai cortado no SQL,
    como na listagem.
    """
    if not ids:
        return []
    corte, marc, _ = queries.limites()
    params: dict[str, Any] = {"corte": corte, "marc": marc}
    for i, ident in enumerate(ids):
        params[f"id{i}"] = ident
    linhas = {
        int(linha["id"]): linha
        for linha in conn.execute(queries.sql_lista_por_ids(len(ids)), params)
    }

    itens: list[dict[str, Any]] = []
    for ident, escore in zip(ids, escores, strict=True):
        linha = linhas.get(ident)
        if linha is None:  # pragma: no cover - a linha sumiu entre as duas consultas
            continue
        item = presenters.item_lista(linha, corte=corte, snippet=None)
        # `score` é o campo genérico de relevância que a listagem já devolve (lá,
        # o bm25 — NEGATIVO, menor é melhor). Aqui ele é o cosseno, onde MAIOR é
        # melhor. Como a inversão de sinal é invisível para quem só lê o número,
        # a similaridade também sai com nome próprio: quem desenha a barrinha lê
        # `similaridade` e não precisa saber em que modo está.
        item["score"] = escore
        item["similaridade"] = escore
        itens.append(item)
    return itens


@router.get("/api/semantic", summary="Busca por sentido (embeddings), top-k exato")
def buscar(
    conn: Conexao,
    indice: Semantica,
    consulta: Annotated[ConsultaSemantica, Query()],
) -> dict[str, Any]:
    """Os ``k`` prompts mais próximos da consulta, **dentro do filtro atual**.

    O envelope é o mesmo de ``GET /api/prompts`` (mais alguns campos), para a
    interface desenhar a lista com o mesmo código. As diferenças que o cliente
    precisa conhecer:

    * ``sort_efetivo`` é ``"semantic"``: relevância por cosseno é a única ordem
      que existe aqui, e por isso não há paginação (``pages`` é sempre 1).
    * ``total`` é quantos itens VOLTARAM (no máximo ``k``), com
      ``truncated_total: true``. Quantos casaram o filtro está em
      ``n_candidatos`` — é esse o número grande que a tela deve mostrar.
    * cada item ganha ``similaridade`` (0..1, maior é melhor) e o mesmo valor em
      ``score``. Não há ``snippet``: sem termo de busca não há o que destacar.

    O filtro é **exato**. Pontua-se o corpus inteiro e apaga-se o que o SQL não
    deixou passar, em vez de pegar os N melhores e filtrar depois — que é rápido,
    é o que quase todo mundo faz, e devolve um top-k que parece certo e não é
    quando o recorte é estreito.

    **A primeira chamada é lenta** (carrega o e5 e converte a matriz) e diz quanto
    custou em ``aquecimento_ms``. Quem quiser evitar isso na cara do usuário chama
    ``POST /api/semantic/warmup`` antes.
    """
    filtros = consulta.filtros()

    # ANTES de qualquer coisa cara: o arquivo do banco pode ter sido trocado por
    # um `pf load-db` embaixo do servidor, e aí o mapa uid -> id que está na RAM é
    # de outro corpus. Custa uma busca por chave primária.
    indice.conferir_build(conn)

    inicio = time.perf_counter()
    # `aquecido` (matriz E modelo), não `carregado`: a matriz fica pronta em
    # 417 ms e o e5 leva ~16 s. Medir só a matriz faria esta resposta dizer
    # "aquecimento: 0,4 s" numa requisição que levou 16 — e a interface prometer
    # que a próxima seria rápida quando o modelo ainda nem tinha começado.
    frio = not indice.aquecido
    ms_carga = 0.0
    if not indice.carregado:
        marco = time.perf_counter()
        try:
            indice.carregar()
        except semantic.SemanticaIndisponivel as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except semantic.DesalinhamentoEmbeddings as exc:
            # 500, e não 503: "ainda não gerei os embeddings" é falta de insumo;
            # "gerei de outro universo" é o banco e a matriz discordando sobre o
            # que é cada linha. A segunda não melhora sozinha e não pode ser
            # confundida com indisponibilidade temporária.
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        ms_carga = (time.perf_counter() - marco) * 1000

    sql, params = queries.sql_ids_filtrados(filtros)
    marco_sql = time.perf_counter()
    permitidos = queries.executar_ids(conn, sql, params)
    ms_sql = (time.perf_counter() - marco_sql) * 1000

    marco_emb = time.perf_counter()
    vetor = indice.vetor_da_consulta(str(consulta.q))
    ms_embed = (time.perf_counter() - marco_emb) * 1000
    # O modelo só carrega de fato no primeiro `embed_texts` — 10,6 s de import do
    # torch mais 4,7 s do e5 —, então o embedding da consulta fria É o
    # aquecimento. Somar os dois é o que faz este número corresponder ao que o
    # usuário esperou.
    aquecimento_ms = (ms_carga + ms_embed) if frio else None

    marco_topk = time.perf_counter()
    ids, escores, n_candidatos = indice.pontuar(vetor, permitidos, consulta.k)
    ms_topk = (time.perf_counter() - marco_topk) * 1000

    itens = _hidratar(conn, ids, escores)
    return {
        "total": len(itens),
        # Sempre True: top-k não é uma página de um total, é um recorte por
        # proximidade. O cliente usa `n_candidatos` para o número grande.
        "truncated_total": True,
        "page": 1,
        "page_size": consulta.k,
        "pages": 1 if itens else 0,
        "sort": "semantic",
        "sort_efetivo": "semantic",
        "seed": None,
        "q": consulta.q,
        # A busca por sentido não passa pelo FTS5: não há expressão sanitizada
        # para mostrar, e devolver uma faria a interface explicar o índice errado.
        "q_fts": None,
        "k": consulta.k,
        #: Quantos itens casaram o filtro E têm vetor — o universo real desta busca.
        "n_candidatos": n_candidatos,
        #: Quantos casaram o filtro mas NÃO estão no `.npy` (linha carregada no
        #: banco depois do s06). Zero num par bem-formado; se subir, a busca está
        #: enxergando menos corpus do que a listagem, e a tela precisa dizer.
        "n_sem_vetor": max(0, int(permitidos.size) - n_candidatos),
        "aquecimento_ms": None if aquecimento_ms is None else round(aquecimento_ms, 1),
        "ms": round((time.perf_counter() - inicio) * 1000, 1),
        "ms_detalhe": {
            "sql": round(ms_sql, 1),
            "embed": round(ms_embed, 1),
            "topk": round(ms_topk, 1),
        },
        "items": itens,
    }


@router.get("/api/semantic/status", summary="A busca por sentido está pronta?")
def status(conn: Conexao, indice: Semantica) -> dict[str, Any]:
    """Disponibilidade, aquecimento e tamanho do índice.

    A interface consulta isto ao ligar o modo "sentido" e enquanto o aquecimento
    corre. Nunca levanta: um índice torto precisa **aparecer**, e uma rota de
    diagnóstico que devolve 500 é justamente a que não conta o que houve.
    """
    indice.conferir_build(conn)
    dados = indice.status()
    if dados["sonda"] is None:
        dados["sonda"] = indice.sondar(conn)
    return dados


@router.post("/api/semantic/warmup", summary="Começa a carregar o modelo e a matriz")
def warmup(indice: Semantica) -> dict[str, Any]:
    """Dispara o aquecimento numa thread e responde **na hora**.

    Não é ``async``, mas também não bloqueia: o trabalho vai para uma thread
    daemon e esta rota só devolve o estado. É o que permite à interface mostrar
    "carregando o modelo (~3 s)" sem travar a tela nem segurar uma requisição
    aberta por segundos.

    Idempotente: chamar duas vezes durante o aquecimento não dispara dois.
    """
    iniciou = indice.aquecer_em_thread()
    dados = indice.status()
    dados["iniciou_agora"] = iniciou
    return dados


__all__ = ["router"]
