"""Construção do SQL da interface. **Todo** o SQL de leitura da app mora aqui.

Concentrar isto num módulo só não é organização: é a única forma de garantir
que o filtro que a listagem aplica, o que a faceta conta e o que o export
escreve sejam **o mesmo filtro**. Quando essas três consultas são escritas em
lugares diferentes, elas divergem — e a divergência aparece como "a faceta
prometia 8.412 e o export trouxe 8.409", que é o tipo de erro que destrói a
confiança na ferramenta sem nunca levantar uma exceção.

Três armadilhas medidas que a estrutura daqui evita:

1. **``nsfw = 0`` perde as linhas NULL em silêncio.** Não rotulado é o estado
   normal deste corpus (a campanha do M6/M7 ainda não rodou), e ``= 0`` devolve
   só quem foi explicitamente marcado como não-NSFW. Todo booleano anulável usa
   ``IS 1`` / ``IS NOT 1``. Ver ``NSFW_SQL``.
2. **``MATERIALIZED`` no CTE do FTS é 100x mais lento.** Medido em 200k linhas:
   9,5 ms sem, 956 ms com — a CTE materializada vira tabela temporária sem
   índice e o planner passa a varrer ``prompts`` como laço externo. O SQLite
   inlina sozinho; a palavra não pode aparecer neste arquivo.
3. **``snippet()`` sobre todos os hits custa o corpus; sobre a página, 2,3 ms.**
   Medido em 120k linhas com um termo de 97.541 hits: dentro do CTE, 384 ms;
   restrito aos 50 rowids da página, **2,3 ms**. Por isso o snippet sai numa
   SEGUNDA consulta (``sql_snippets``), depois de a página já estar decidida.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, NamedTuple

from ..config import get as _cfg
from ..schema import DOMAINS, TASK_TYPES
from .models import ConsultaPrompts, Filtros

# ---------------------------------------------------------------------------
# sanitizador do FTS5
# ---------------------------------------------------------------------------

_TOKENS = re.compile(r'"([^"]*)"|(\S+)')
#: ``\w`` é unicode-aware: japonês/cirílico passam, emoji sozinho não (o
#: tokenizador ``unicode61`` também não o indexa, então descartar é honesto).
_SO_PONTUACAO = re.compile(r"^[\W_]+$", re.UNICODE)


def sanitize_fts(q: str | None, *, max_tokens: int | None = None) -> str:
    """Texto do usuário → expressão FTS5 **segura**.

    Cada token vira um literal entre aspas, o que neutraliza a sintaxe inteira
    do FTS5 (``-``, ``:``, ``(``, ``)``, ``*``, ``^``, ``NEAR``, ``OR``, ``AND``,
    ``NOT``). Sem isso, um prompt tão banal quanto ``e-mail`` derruba a busca com
    ``OperationalError: no such column: mail``, e ``a" OR b`` derruba com erro de
    sintaxe. Preserva duas coisas que o usuário usa de propósito: frases entre
    aspas e o ``*`` de prefixo no fim do token.

    Devolve ``""`` quando não sobra nada. O chamador **não pode** emitir um
    MATCH nesse caso: ``MATCH ''`` casa zero linhas, que é diferente de "sem
    filtro de texto" — a diferença entre uma busca por ``***`` devolver o corpus
    inteiro (errado) e devolver nada (também errado, mas silencioso).
    """
    if max_tokens is None:
        max_tokens = int(_cfg("app", "fts_max_tokens", default=24))
    partes: list[str] = []
    for m in _TOKENS.finditer(q or ""):
        frase, solto = m.group(1), m.group(2)
        if frase is not None:
            texto, prefixo = frase.strip(), False
        else:
            texto = solto
            prefixo = texto.endswith("*")
            if prefixo:
                texto = texto[:-1]
        if not texto or _SO_PONTUACAO.match(texto):
            continue
        partes.append('"' + texto.replace('"', '""') + '"' + ("*" if prefixo else ""))
        if len(partes) >= max_tokens:
            break
    return " ".join(partes)


def fts_literal(q: str | None) -> str:
    """A query inteira como UMA frase literal — o paraquedas do sanitizador."""
    return '"' + (q or "").replace('"', '""') + '"'


def executar_fts(
    conn: sqlite3.Connection, sql: str, params: dict[str, Any], q: str | None
) -> list[sqlite3.Row]:
    """Executa uma consulta com MATCH, com fallback para a frase literal.

    O sanitizador sobreviveu a 36 entradas hostis sem precisar do fallback; ele
    existe porque a alternativa a um fallback é um 500 na cara do usuário
    quando aparecer a 37ª. Erro de sintaxe do FTS5 vira uma busca mais burra,
    nunca uma página quebrada.
    """
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        aviso = str(exc).lower()
        if "fts5" not in aviso and "no such column" not in aviso and "syntax" not in aviso:
            raise
        if "fts" not in params:
            raise
        return conn.execute(sql, {**params, "fts": fts_literal(q)}).fetchall()


# ---------------------------------------------------------------------------
# WHERE
# ---------------------------------------------------------------------------

#: ``exclude`` mantém quem não foi rotulado (nsfw NULL). É a diferença entre
#: "não é NSFW" e "não sabemos", e num corpus sem campanha de rotulagem o
#: segundo é 100% das linhas.
NSFW_SQL: dict[str, str | None] = {
    "exclude": "p.nsfw IS NOT 1",
    "include": None,
    "only": "p.nsfw IS 1",
}
PII_SQL: dict[str, str | None] = {
    "any": None,
    "only": "p.pii_found IS 1",
    "none": "p.pii_found IS NOT 1",
}

#: Filtros de lista: campo do modelo → coluna. Os nomes de coluna vêm DAQUI,
#: nunca da entrada; o que o usuário manda vira sempre parâmetro ligado.
_EM: dict[str, str] = {
    "lang": "p.lang",
    "lang_variant": "p.lang_variant",
    "source": "p.source",
    "license": "p.license",
    "task_type": "p.task_type",
    "domain": "p.domain",
    "quality": "p.quality",
}


def build_where(
    f: Filtros, *, ignorar: frozenset[str] = frozenset()
) -> tuple[list[str], dict[str, Any]]:
    """``(condições, params)`` a partir dos filtros.

    ``ignorar`` recebe **nomes de campo do modelo** e serve às facetas: cada
    dimensão é contada com o filtro dela mesma removido, senão marcar "pt" zera
    as contagens de "en" e o usuário fica preso no primeiro clique que deu.
    """
    cond: list[str] = []
    params: dict[str, Any] = {}

    for campo, coluna in _EM.items():
        valores = getattr(f, campo)
        if campo in ignorar or not valores:
            continue
        chaves = []
        for i, valor in enumerate(valores):
            chave = f"f_{campo}_{i}"  # o nome deriva de um literal do código
            params[chave] = valor
            chaves.append(f":{chave}")
        cond.append(f"{coluna} IN ({', '.join(chaves)})")

    if f.quality_min is not None and "quality_min" not in ignorar:
        # NULL >= 1 é NULL, logo falso: filtrar por qualidade ESCONDE os não
        # rotulados. É o desejado; a interface é que precisa dizer isso.
        cond.append("p.quality >= :f_quality_min")
        params["f_quality_min"] = f.quality_min

    if "nsfw" not in ignorar and (sql := NSFW_SQL[f.nsfw]) is not None:
        cond.append(sql)
    if "pii" not in ignorar and (sql := PII_SQL[f.pii]) is not None:
        cond.append(sql)

    for campo in ("needs_review", "edited"):
        valor = getattr(f, campo)
        if campo in ignorar or valor is None:
            continue
        cond.append(f"p.{campo} IS {'1' if valor else 'NOT 1'}")

    if f.commercial_only and "commercial_only" not in ignorar:
        cond.append("p.commercial_ok IS 1")
    if f.redistributable_only and "redistributable_only" not in ignorar:
        cond.append("p.redistributable IS 1")

    if f.max_dups is not None and "max_dups" not in ignorar:
        cond.append("p.n_exact_dups <= :f_max_dups")
        params["f_max_dups"] = f.max_dups

    if f.n_chars_min is not None and "n_chars_min" not in ignorar:
        cond.append("p.n_chars >= :f_n_chars_min")
        params["f_n_chars_min"] = f.n_chars_min
    if f.n_chars_max is not None and "n_chars_max" not in ignorar:
        cond.append("p.n_chars <= :f_n_chars_max")
        params["f_n_chars_max"] = f.n_chars_max

    if f.collection_id is not None and "collection_id" not in ignorar:
        cond.append(
            "EXISTS (SELECT 1 FROM collection_items ci "
            "WHERE ci.prompt_id = p.id AND ci.collection_id = :f_collection_id)"
        )
        params["f_collection_id"] = f.collection_id

    return cond, params


def _clausula(cond: list[str]) -> str:
    return (" WHERE " + " AND ".join(cond)) if cond else ""


# ---------------------------------------------------------------------------
# FROM (com e sem busca textual)
# ---------------------------------------------------------------------------

#: O CTE que traz os hits do FTS. ``bm25()`` **só** pode ser chamada na consulta
#: que contém o MATCH, então o score nasce aqui dentro e é ordenado lá fora.
#: NÃO escreva ``AS MATERIALIZED``: medido, 100x mais lento (ver docstring).
_CTE_HITS = (
    "WITH hits AS (SELECT rowid AS id, bm25(prompts_fts) AS score "
    "FROM prompts_fts WHERE prompts_fts MATCH :fts) "
)
_FROM_FTS = "FROM prompts p JOIN hits h ON h.id = p.id"
_FROM_SIMPLES = "FROM prompts p"

#: Tabela TEMPORÁRIA com os acertos da busca, materializada uma vez por
#: requisição. Vive na conexão (que é uma por request) e morre com ela.
TABELA_HITS = "pf_hits"
_FROM_HITS = f"FROM prompts p JOIN temp.{TABELA_HITS} h ON h.id = p.id"


def preparar_busca(
    f: Filtros, *, hits_prontos: bool = False
) -> tuple[str, str, str, dict[str, Any]]:
    """``(prefixo_cte, from, expressão_de_score, params)``.

    ``expressão_de_score`` sai vazia quando não há busca — a listagem então não
    devolve ``score``, e não devolver é melhor que devolver zero: um score 0
    para todo mundo faz a interface desenhar uma barra de relevância que não
    significa nada.

    ``hits_prontos=True`` troca o CTE pela tabela temporária de
    ``materializar_hits``. Ver lá por que os dois caminhos existem.
    """
    fts = sanitize_fts(f.q)
    if not fts:
        return "", _FROM_SIMPLES, "", {}
    if hits_prontos:
        return "", _FROM_HITS, "h.score", {}
    return _CTE_HITS, _FROM_FTS, "h.score", {"fts": fts}


def materializar_hits(conn: sqlite3.Connection, f: Filtros) -> bool:
    """Guarda os acertos da busca numa tabela temporária. ``False`` se não há busca.

    SIM, ISTO CONTRADIZ A ARMADILHA 2 DO CABEÇALHO — E DE PROPÓSITO.
    =============================================================
    Materializar os acertos DENTRO de uma consulta (``AS MATERIALIZED``) é 100x
    mais lento na LISTAGEM, e continua sendo. Materializá-los numa tabela
    temporária INDEXADA, reaproveitada por várias consultas, é 7x mais rápido
    nas FACETAS. As duas coisas são verdade porque as duas consultas são
    diferentes:

    * A listagem tem ``LIMIT 50``: o CTE inline deixa o SQLite usar os acertos
      como laço externo e parar cedo. Materializar apaga essa saída antecipada.
    * Uma faceta consome o conjunto INTEIRO de acertos e não devolve texto
      nenhum. Com o CTE, o plano é ``SCAN prompts_fts`` + ``SEARCH p USING
      INTEGER PRIMARY KEY`` — 14 mil linhas COMPLETAS carregadas de uma tabela de
      687 MB, com o ``text`` junto, só para contar. Com a tabela temporária, o
      plano vira ``SEARCH p USING COVERING INDEX idx_prompts_app`` sondando
      ``pf_hits`` pelo rowid: **a tabela não é tocada**.

    Medido no banco real, busca ``texto`` (14.020 acertos), sob ``lang=pt``:

    ========================================  ========
    por consulta de faceta, com o CTE          77 ms
    por consulta de faceta, com a temporária   11 ms
    montar a temporária (uma vez)             8,5 ms
    ========================================  ========

    Cinco consultas por resposta de faceta: 385 ms viram 63 ms.

    A tabela é ``TEMP``: vive na conexão, que é uma por requisição, e some com
    ela. Nada disso escreve no banco.
    """
    fts = sanitize_fts(f.q)
    if not fts:
        return False
    conn.execute(f"DROP TABLE IF EXISTS temp.{TABELA_HITS}")
    conn.execute(f"CREATE TEMP TABLE {TABELA_HITS} (id INTEGER PRIMARY KEY, score REAL)")
    sql = (
        f"INSERT INTO temp.{TABELA_HITS} (id, score) "
        "SELECT rowid, bm25(prompts_fts) FROM prompts_fts WHERE prompts_fts MATCH :fts"
    )
    # O mesmo paraquedas do `executar_fts`: erro de sintaxe do FTS5 vira uma
    # busca mais burra, nunca um 500. Agora ele acontece UMA vez, aqui, em vez de
    # uma vez por consulta de faceta.
    try:
        conn.execute(sql, {"fts": fts})
    except sqlite3.OperationalError as exc:
        aviso = str(exc).lower()
        if "fts5" not in aviso and "no such column" not in aviso and "syntax" not in aviso:
            raise
        conn.execute(sql, {"fts": fts_literal(f.q)})
    return True


# ---------------------------------------------------------------------------
# ORDER BY
# ---------------------------------------------------------------------------

#: Toda ordenação termina em ``p.id``. Sem o desempate, ``n_chars DESC`` com
#: empates (e o corpus é cheio deles) devolve a MESMA linha em duas páginas e
#: some com outra — o usuário vê duplicata e perde item sem nenhum aviso.
#:
#: ``random``: dois módulos, não um. A fórmula ``(id * seed) % 1000003``, de um
#: módulo só, **não embaralha** — é linear em ``id`` e, medida em 200k ids com
#: seed=7, devolveu ``[142858, 1, 142859, 2, ...]``: duas sequências crescentes
#: intercaladas, que é ordem por id disfarçada. Com o segundo módulo a
#: linearidade quebra e os 1.000 primeiros se distribuem pelos decis.
ORDER_BY: dict[str, str] = {
    "relevance": "score ASC, p.id ASC",  # bm25 é NEGATIVO: menor = mais relevante
    "newest": "p.created_ts DESC, p.id DESC",
    "oldest": "p.created_ts ASC, p.id ASC",
    "shortest": "p.n_chars ASC, p.id ASC",
    "longest": "p.n_chars DESC, p.id ASC",
    "dups": "p.n_exact_dups DESC, p.id ASC",
    "random": (
        "(((p.id * 2654435761 + :seed * 40503) % 4294967291) "
        "* 279470273 % 1000003) ASC, p.id ASC"
    ),
}


def sort_efetivo(sort: str, tem_busca: bool) -> str:
    """``relevance`` sem busca não existe: vira ``newest``.

    Quem chama **devolve isto no envelope**, para a interface mostrar o que
    realmente ordenou a página em vez de um "relevância" que é mentira.
    """
    return "newest" if (sort == "relevance" and not tem_busca) else sort


def _order_by(sort: str, params: dict[str, Any], seed: int) -> str:
    if sort == "random":
        params["seed"] = seed
    return ORDER_BY[sort]


# ---------------------------------------------------------------------------
# colunas
# ---------------------------------------------------------------------------

#: Colunas da LISTAGEM. ``text`` sai cortado no SQL: há prompts de ~1 milhão de
#: caracteres e uma página de 50 devolveria dezenas de MB de JSON.
_COLS_LISTA = """
  p.id, p.uid,
  substr(p.text, 1, :corte) AS text,
  p.n_chars, p.n_words,
  p.lang, p.lang_variant, p.variant_confidence,
  p.source, p.source_id, p.license, p.commercial_ok, p.redistributable,
  p.task_type, p.domain, p.quality, p.nsfw, p.pii_found,
  p.label_method, p.label_confidence, p.needs_review, p.edited,
  p.n_exact_dups, p.n_near_dups, p.created_ts, p.updated_at,
  (substr(p.text, 1, :marc) LIKE '%User:%'
   OR substr(p.text, 1, :marc) LIKE '%Assistant:%') AS has_chat_markers
"""

#: Colunas do DETALHE: tudo, com o texto inteiro e o original ao lado.
_COLS_DETALHE = """
  p.id, p.uid, p.text, p.text_original, p.edited,
  p.n_chars, p.n_words,
  p.lang, p.lang_variant, p.variant_confidence,
  p.source, p.source_id, p.source_split, p.license,
  p.commercial_ok, p.redistributable,
  p.task_type, p.domain, p.quality, p.nsfw, p.pii_found,
  p.label_method, p.label_confidence, p.needs_review,
  p.hash_norm, p.n_exact_dups, p.n_near_dups,
  p.country, p.model_family, p.created_ts, p.native_category, p.meta_json,
  p.ingested_at, p.updated_at,
  (p.text LIKE '%User:%' OR p.text LIKE '%Assistant:%') AS has_chat_markers
"""

SQL_DETALHE = f"SELECT {_COLS_DETALHE} FROM prompts p WHERE p.uid = :uid"


def limites() -> tuple[int, int, int]:
    """``(corte do texto, janela do marcador de chat, teto de offset)``."""
    return (
        int(_cfg("app", "list_text_chars", default=1200)),
        int(_cfg("app", "chat_markers_scan_chars", default=20000)),
        int(_cfg("app", "max_offset", default=100_000)),
    )


def expr_marcadores_chat(coluna: str = "text") -> str:
    """A expressão "este texto parece uma conversa colada?", com o corte LITERAL.

    **Ponto único da verdade, e não por elegância.** ``app/main.py`` cria um
    índice de EXPRESSÃO com exatamente esta string; o ``/api/stats`` a soma sobre
    o corpus inteiro. O SQLite só usa um índice de expressão quando a expressão
    da consulta é textualmente a mesma da do índice (a menos de espaços) — duas
    cópias divergentes não dariam erro, dariam 1.462 ms em vez de 5,5 ms, em
    silêncio. Por isso o corte entra como **literal** e não como parâmetro
    ligado: ``:marc`` nunca casaria com o índice.

    (A listagem continua usando ``:marc`` parametrizado em ``_COLS_LISTA``: lá a
    expressão é avaliada sobre as 50 linhas da página, onde o índice não faz
    diferença nenhuma.)
    """
    _, marc, _ = limites()
    return (
        f"(substr({coluna}, 1, {marc}) LIKE '%User:%' "
        f"OR substr({coluna}, 1, {marc}) LIKE '%Assistant:%')"
    )


# ---------------------------------------------------------------------------
# consultas de listagem
# ---------------------------------------------------------------------------


#: Ordenações que ``idx_prompts_created`` (created_ts DESC, id DESC, nsfw, lang)
#: serve inteiras — a mesma B-tree, lida para frente ou para trás.
_ORDENS_DO_INDICE_CRIACAO = frozenset({"newest", "oldest"})


def _limiar_offset_profundo() -> int:
    return int(_cfg("app", "deep_offset_hint", default=10_000))


def sql_lista(c: ConsultaPrompts) -> tuple[str, dict[str, Any], str]:
    """``(sql, params, sort_efetivo)`` da página de prompts."""
    cte, origem, score, params = preparar_busca(c)
    cond, p_where = build_where(c.filtros())
    params.update(p_where)

    corte, marc, _ = limites()
    params["corte"] = corte
    params["marc"] = marc

    efetivo = sort_efetivo(c.sort, bool(score))
    ordem = _order_by(efetivo, params, c.seed)
    colunas = _COLS_LISTA + (f", {score} AS score" if score else "")

    params["limit"] = c.page_size
    params["offset"] = (c.page - 1) * c.page_size

    # OFFSET PROFUNDO: o único lugar da app que passa por cima do planner.
    #
    # Com um filtro de igualdade indexado (`lang=pt`, `source=aya`), o SQLite
    # prefere BUSCAR por esse índice e ordenar o resultado num TEMP B-TREE. Para
    # a página 1 essa escolha é CERTA (medido: `source=aya` página 1 = 19 ms).
    # Para a página 2000 é uma catástrofe, porque o TEMP B-TREE passa a ordenar
    # dezenas de milhares de linhas — medido no banco real, `lang=pt` na página
    # 2000: 2.256 ms contra 12 ms percorrendo `idx_prompts_created`.
    #
    # O limiar é a justificativa: um OFFSET de N só é alcançável se o filtro
    # casar mais de N linhas, e ordenar N linhas num B-tree temporário nunca sai
    # mais barato que caminhar N entradas de um índice já ordenado. Abaixo do
    # limiar o planner decide sozinho, que é o certo.
    #
    # Só sem busca textual: com o CTE do FTS o laço externo são os hits, e forçar
    # o índice aqui inverteria a junção e varreria o corpus para cada acerto.
    if (
        not score
        and efetivo in _ORDENS_DO_INDICE_CRIACAO
        and params["offset"] >= _limiar_offset_profundo()
    ):
        origem = f"{_FROM_SIMPLES} INDEXED BY idx_prompts_created"

    sql = (
        f"{cte}SELECT {colunas} {origem}{_clausula(cond)} "
        f"ORDER BY {ordem} LIMIT :limit OFFSET :offset"
    )
    return sql, params, efetivo


def sql_contagem(f: Filtros, *, hits_prontos: bool = False) -> tuple[str, dict[str, Any]]:
    """``count(*)`` sob o filtro — o número grande do topo da interface."""
    cte, origem, _, params = preparar_busca(f, hits_prontos=hits_prontos)
    cond, p_where = build_where(f)
    params.update(p_where)
    return f"{cte}SELECT count(*) AS n {origem}{_clausula(cond)}", params


def sql_ids(f: Filtros, *, sort: str = "newest", seed: int = 42, limit: int | None = None) -> tuple[str, dict[str, Any]]:
    """Só os ``id`` que casam — insumo do ``from-filter`` e do export.

    Devolve ``id`` (o rowid interno), não ``uid``: é ele que as coleções e o
    ``IN`` da hidratação usam, e converter de novo custaria uma consulta extra.
    """
    cte, origem, score, params = preparar_busca(f)
    cond, p_where = build_where(f)
    params.update(p_where)
    efetivo = sort_efetivo(sort, bool(score))
    ordem = _order_by(efetivo, params, seed)
    sql = f"{cte}SELECT p.id {origem}{_clausula(cond)} ORDER BY {ordem}"
    if limit is not None:
        sql += " LIMIT :limit"
        params["limit"] = limit
    return sql, params


def sql_export(f: Filtros) -> tuple[str, dict[str, Any]]:
    """As linhas de um export por filtro, com o texto INTEIRO."""
    cte, origem, _, params = preparar_busca(f)
    cond, p_where = build_where(f)
    params.update(p_where)
    return (
        f"{cte}SELECT {_COLS_DETALHE} {origem}{_clausula(cond)} ORDER BY p.id",
        params,
    )


def sql_export_ids(quantos: int) -> str:
    """As linhas de um export por lista de ids (parâmetros POSICIONAIS).

    Usada em lotes pelo modo ``uids``: o SQLite tem teto de variáveis ligadas
    por consulta, e uma lista de 100 mil ids num único ``IN`` não passa.
    """
    marcas = ", ".join("?" * quantos)
    return f"SELECT {_COLS_DETALHE} FROM prompts p WHERE p.id IN ({marcas}) ORDER BY p.id"


def sql_snippets(ids: list[int]) -> str:
    """Snippet destacado do FTS **só para os ids da página**.

    Medido em 120k linhas, termo com 97.541 hits: snippet dentro do CTE custa
    384 ms; restrito aos 50 rowids da página, **2,3 ms**. A diferença é a
    definição do contrato — é isto que torna viável devolver o trecho com o
    termo em vez do começo do texto (que, num prompt de 1 MB, não mostra nada
    do que o usuário procurou).
    """
    marcas = ", ".join(f":id{i}" for i in range(len(ids)))
    return (
        "SELECT rowid AS id, snippet(prompts_fts, 0, :abre, :fecha, :elipse, :janela) AS snippet "
        f"FROM prompts_fts WHERE prompts_fts MATCH :fts AND rowid IN ({marcas})"
    )


def buscar_snippets(
    conn: sqlite3.Connection, ids: list[int], q: str | None
) -> dict[int, str]:
    """``{id: snippet}`` com ``<mark>`` em volta do termo. Vazio se não há busca.

    O ``<mark>`` viaja como texto no JSON; **a interface tem de escapar o resto
    do conteúdo** antes de injetar isso como HTML, senão um prompt com ``<script>``
    (e o corpus tem) vira XSS na própria máquina do usuário.
    """
    fts = sanitize_fts(q)
    if not fts or not ids:
        return {}
    params: dict[str, Any] = {
        "fts": fts,
        "abre": "<mark>",
        "fecha": "</mark>",
        "elipse": "…",
        "janela": int(_cfg("app", "snippet_tokens", default=18)),
    }
    for i, ident in enumerate(ids):
        params[f"id{i}"] = ident
    linhas = executar_fts(conn, sql_snippets(ids), params, q)
    return {int(r["id"]): str(r["snippet"]) for r in linhas}


# ---------------------------------------------------------------------------
# facetas
# ---------------------------------------------------------------------------


class Faceta(NamedTuple):
    """Uma dimensão da barra lateral."""

    nome: str
    #: Expressão SQL agrupada (sempre literal do código).
    expr: str
    #: Campos de ``Filtros`` que esta dimensão controla — removidos do WHERE ao
    #: contá-la.
    ignora: frozenset[str]
    #: Vocabulário fixo, quando existe: define a ORDEM e força a aparição dos
    #: zeros. Sem ele, a dimensão sai por contagem decrescente.
    vocabulario: tuple[Any, ...] = ()


FACETAS: tuple[Faceta, ...] = (
    Faceta("lang", "p.lang", frozenset({"lang"}), ("pt", "en")),
    Faceta(
        "lang_variant",
        "p.lang_variant",
        frozenset({"lang_variant"}),
        ("pt-BR", "pt-PT", "pt-indef"),
    ),
    Faceta("source", "p.source", frozenset({"source"})),
    Faceta("license", "p.license", frozenset({"license"})),
    # task_type/domain saem na ORDEM DA TAXONOMIA, não por contagem: uma barra
    # lateral que se reordena a cada clique é inutilizável para quem varre
    # milhares de itens. A ordem de schema.TASK_TYPES é contrato.
    Faceta("task_type", "p.task_type", frozenset({"task_type"}), TASK_TYPES),
    Faceta("domain", "p.domain", frozenset({"domain"}), DOMAINS),
    Faceta("quality", "p.quality", frozenset({"quality", "quality_min"}), (1, 2, 3)),
    Faceta("nsfw", "p.nsfw", frozenset({"nsfw"}), (1, 0)),
    Faceta("needs_review", "p.needs_review", frozenset({"needs_review"}), (1, 0)),
    Faceta("edited", "p.edited", frozenset({"edited"}), (1, 0)),
    Faceta("pii_found", "p.pii_found", frozenset({"pii"}), (1, 0)),
    Faceta(
        "n_chars_bucket",
        "CASE WHEN p.n_chars < :bk0 THEN 'curto' "
        "WHEN p.n_chars < :bk1 THEN 'medio' ELSE 'longo' END",
        frozenset({"n_chars_min", "n_chars_max"}),
        ("curto", "medio", "longo"),
    ),
)


def bordas_tamanho() -> tuple[int, int]:
    """Fronteiras de ``n_chars`` da faceta de tamanho (``[app] size_buckets``)."""
    bordas = _cfg("app", "size_buckets", default=[120, 600])
    return int(bordas[0]), int(bordas[1])


def sql_faceta(
    f: Filtros, faceta: Faceta, *, hits_prontos: bool = False
) -> tuple[str, dict[str, Any]]:
    """``GROUP BY`` de uma dimensão, com o filtro dela mesma removido."""
    cte, origem, _, params = preparar_busca(f, hits_prontos=hits_prontos)
    cond, p_where = build_where(f, ignorar=faceta.ignora)
    params.update(p_where)
    if ":bk0" in faceta.expr:
        params["bk0"], params["bk1"] = bordas_tamanho()
    sql = (
        f"{cte}SELECT {faceta.expr} AS v, count(*) AS n {origem}"
        f"{_clausula(cond)} GROUP BY v"
    )
    return sql, params


# ---------------------------------------------------------------------------
# facetas em UMA passada (M9-C)
# ---------------------------------------------------------------------------

#: A ORDEM DESTA TUPLA É A ORDEM DAS COLUNAS DE ``idx_prompts_app``, E ISSO É
#: LOAD-BEARING.
#:
#: Um ``GROUP BY`` cujas colunas são o **prefixo contíguo** de um índice é
#: resolvido varrendo o índice já ordenado: o SQLite agrupa na passagem e não
#: monta B-tree nenhuma. Qualquer outra ordem — ou a mesma ordem com um BURACO —
#: cai num ``USE TEMP B-TREE FOR GROUP BY`` sobre 144 mil linhas.
#:
#: Medido no banco real, as mesmas 11 dimensões, a mesma resposta:
#:
#: =========================================  ========
#: formulação                                 tempo
#: =========================================  ========
#: ordem arbitrária (12 dims)                 202 ms
#: ordem do índice, com buraco                162 ms
#: ordem do índice, prefixo contíguo           37 ms
#: =========================================  ========
#:
#: ``commercial_ok`` e ``redistributable`` NÃO são facetas: estão aqui só para
#: tapar o buraco entre ``edited`` e ``pii_found`` e manter o prefixo contíguo.
#: Tirá-las "porque não são usadas" multiplica o custo por 4,4. Se um dia
#: ``idx_prompts_app`` mudar de ordem, esta tupla muda junto — ou o ganho some
#: sem nenhum sintoma além da lentidão.
#:
#: ``n_chars_bucket`` fica de fora: agrupar por ``n_chars`` cru seriam 14.571
#: grupos, e a coluna vem depois de ``pii_found`` no índice. Ela sai numa
#: segunda passada (47 ms), que ainda é uma varredura de cobertura.
PREFIXO_FACETAS: tuple[str, ...] = (
    "lang",
    "quality",
    "nsfw",
    "lang_variant",
    "task_type",
    "domain",
    "source",
    "license",
    "needs_review",
    "edited",
    "commercial_ok",
    "redistributable",
    "pii_found",
)

#: ``{nome da faceta: coluna}`` para as facetas que são uma coluna crua do
#: prefixo. Só essas podem entrar na passada única — as demais (hoje só o
#: ``n_chars_bucket``, que é um ``CASE``) continuam com consulta própria.
_COLUNA_DA_FACETA: dict[str, str] = {
    faceta.nome: faceta.expr[2:]
    for faceta in FACETAS
    if faceta.expr.startswith("p.") and faceta.expr[2:] in PREFIXO_FACETAS
}

#: Todo campo de ``Filtros`` que alguma faceta pede para ignorar. É o universo
#: que ``campos_ativos`` precisa examinar.
_CAMPOS_FACETADOS: frozenset[str] = frozenset().union(*(f.ignora for f in FACETAS))


def campos_ativos(f: Filtros) -> frozenset[str]:
    """Campos de ``Filtros`` que REALMENTE produzem condição no WHERE.

    Derivado do próprio ``build_where`` (por diferença), nunca de uma segunda
    lista de "campos preenchidos": uma segunda lista fatalmente diverge, e
    divergir aqui significa juntar numa passada só duas facetas que deveriam ter
    WHERE diferentes — ou seja, contagem errada, não lentidão.
    """
    base, _ = build_where(f)
    return frozenset(
        campo
        for campo in _CAMPOS_FACETADOS
        if build_where(f, ignorar=frozenset({campo}))[0] != base
    )


def particionar_facetas(f: Filtros) -> tuple[tuple[Faceta, ...], tuple[Faceta, ...]]:
    """``(juntas, sozinhas)`` — quem pode dividir uma passada e quem não pode.

    Uma faceta é contada com o filtro DELA MESMA removido (ver ``sql_faceta``).
    Duas facetas só podem sair da mesma varredura quando o WHERE das duas é o
    mesmo — o que acontece exatamente quando nenhuma delas ignora um campo que
    está ativo. Com a tela sem filtro (o primeiro paint), isso vale para TODAS.
    """
    ativos = campos_ativos(f)
    juntas: list[Faceta] = []
    sozinhas: list[Faceta] = []
    for faceta in FACETAS:
        pode = faceta.nome in _COLUNA_DA_FACETA and not (faceta.ignora & ativos)
        (juntas if pode else sozinhas).append(faceta)
    return tuple(juntas), tuple(sozinhas)


def sql_facetas_prefixo(
    f: Filtros, *, hits_prontos: bool = False
) -> tuple[str, dict[str, Any]]:
    """UMA varredura que devolve a contagem cruzada do prefixo inteiro.

    Sempre o prefixo COMPLETO, mesmo que só três facetas venham a ser
    marginalizadas dele: um subconjunto viraria um prefixo com buracos e pagaria
    o TEMP B-TREE (162 ms em vez de 37 ms). Colunas a mais numa varredura de
    cobertura são de graça; um agrupamento fora da ordem do índice, não.

    O resultado é a distribuição conjunta (82 grupos no corpus real). Quem chama
    **marginaliza** somando ``n`` por valor de cada coluna — o que é exato, e não
    uma aproximação, porque os grupos são uma partição das linhas filtradas.
    """
    cte, origem, _, params = preparar_busca(f, hits_prontos=hits_prontos)
    cond, p_where = build_where(f)
    params.update(p_where)
    colunas = ", ".join(f"p.{c}" for c in PREFIXO_FACETAS)
    sql = (
        f"{cte}SELECT {colunas}, count(*) AS n {origem}"
        f"{_clausula(cond)} GROUP BY {colunas}"
    )
    return sql, params


def marginalizar(
    linhas: list[sqlite3.Row], facetas: tuple[Faceta, ...]
) -> dict[str, dict[Any, int]]:
    """``{faceta: {valor: contagem}}`` a partir da distribuição conjunta."""
    saida: dict[str, dict[Any, int]] = {faceta.nome: {} for faceta in facetas}
    colunas = [(faceta.nome, _COLUNA_DA_FACETA[faceta.nome]) for faceta in facetas]
    for linha in linhas:
        n = int(linha["n"])
        for nome, coluna in colunas:
            alvo = saida[nome]
            valor = linha[coluna]
            alvo[valor] = alvo.get(valor, 0) + n
    return saida


__all__ = [
    "FACETAS",
    "NSFW_SQL",
    "ORDER_BY",
    "PII_SQL",
    "PREFIXO_FACETAS",
    "SQL_DETALHE",
    "TABELA_HITS",
    "Faceta",
    "bordas_tamanho",
    "build_where",
    "buscar_snippets",
    "campos_ativos",
    "executar_fts",
    "expr_marcadores_chat",
    "fts_literal",
    "limites",
    "marginalizar",
    "materializar_hits",
    "particionar_facetas",
    "preparar_busca",
    "sanitize_fts",
    "sort_efetivo",
    "sql_contagem",
    "sql_export",
    "sql_export_ids",
    "sql_faceta",
    "sql_facetas_prefixo",
    "sql_ids",
    "sql_lista",
    "sql_snippets",
]
