"""O catálogo do modo LIVRE: buscar no corpus, dentro do pool, com três filtros.

O SQL de busca **não é reimplementado aqui**. ``app/queries.py`` é o módulo que
já resolveu o problema difícil — o sanitizador do FTS5, que transforma texto de
gente (``e-mail``, ``a" OR b``, ``***``) em expressão MATCH segura, e o fallback
para frase literal quando o FTS5 recusa mesmo assim. Copiar aquilo para cá
criaria uma segunda versão que erra diferente da primeira.

O que importa: importamos o **MÓDULO**, não a app. Instanciar a app de curadoria
aqui traria o cache de agregados dela, o lifespan dela e a suposição dela de ser
a única escritora do corpus. Precisamos de três funções puras, e é só isso que
atravessa.

O RECORTE É SEMPRE O POOL
=========================
Toda consulta daqui carrega ``p.uid IN (...)`` com os uids do pool. Sem isso o
modo livre viraria uma porta lateral para os 159 mil prompts do corpus,
inclusive os que o filtro do pool exclui de propósito: NSFW, o template de robô
com 120 duplicatas e os prompts de 1 MB. O pool é curadoria, não paginação.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..app import queries as q
from ..config import get as _cfg
from . import tarefas as tmod

#: As três faixas de tamanho do filtro. Três, e não um par de campos numéricos,
#: porque a decisão de quem escolhe uma tarefa é "quero uma curta agora", não
#: "quero entre 240 e 700 caracteres". ``None`` no topo = sem limite.
FAIXAS: dict[str, tuple[int, int | None]] = {
    "curto": (0, 300),
    "medio": (300, 900),
    "longo": (900, None),
}


def page_size() -> int:
    return int(_cfg("annotate", "page_size", default=20))


def texto_da_lista() -> int:
    return int(_cfg("annotate", "list_text_chars", default=600))


def _condicoes(
    lang: str | None, fonte: str | None, faixa: str | None
) -> tuple[list[str], dict[str, Any]]:
    cond: list[str] = []
    params: dict[str, Any] = {}
    # Os nomes de coluna saem DAQUI, de literais do código; o que o usuário
    # manda vira sempre parâmetro ligado. Mesma disciplina de `build_where`.
    if lang:
        cond.append("p.lang = :lang")
        params["lang"] = lang
    if fonte:
        cond.append("p.source = :fonte")
        params["fonte"] = fonte
    if faixa and faixa in FAIXAS:
        piso, teto = FAIXAS[faixa]
        cond.append("p.n_chars >= :piso")
        params["piso"] = piso
        if teto is not None:
            cond.append("p.n_chars < :teto")
            params["teto"] = teto
    return cond, params


def _marcas(prefixo: str, valores: list[str]) -> tuple[str, dict[str, Any]]:
    """``(:u0, :u1, ...)`` + os parâmetros. Nunca interpole o valor no SQL."""
    chaves = [f"{prefixo}{i}" for i in range(len(valores))]
    return ", ".join(f":{c}" for c in chaves), dict(zip(chaves, valores, strict=True))


def buscar(
    conn_corpus: sqlite3.Connection,
    uids_pool: list[str],
    *,
    busca: str | None = None,
    lang: str | None = None,
    fonte: str | None = None,
    faixa: str | None = None,
    pagina: int = 1,
) -> dict[str, Any]:
    """Uma página do catálogo. Só leitura, só o pool.

    Devolve ``{items, total, page, page_size, pages, busca_aplicada}``.
    ``busca_aplicada`` é a expressão FTS que de fato rodou — é ela que a tela
    mostra quando o sanitizador descartou tudo (``"***"``), em vez de deixar o
    usuário achar que a busca não achou nada quando ela nem aconteceu.
    """
    if not uids_pool:
        return {
            "items": [],
            "total": 0,
            "page": 1,
            "page_size": page_size(),
            "pages": 0,
            "busca_aplicada": "",
        }

    marcas_uid, params_uid = _marcas("u", uids_pool)
    cond, params = _condicoes(lang, fonte, faixa)
    cond.insert(0, f"p.uid IN ({marcas_uid})")
    params.update(params_uid)

    fts = q.sanitize_fts(busca) if busca else ""
    if fts:
        # `MATCH ''` casa ZERO linhas, o que é diferente de "sem filtro de
        # texto": por isso o CTE só entra quando sobrou expressão do
        # sanitizador. Ver a docstring de `sanitize_fts`.
        cte = (
            "WITH hits AS (SELECT rowid AS id, bm25(prompts_fts) AS score "
            "FROM prompts_fts WHERE prompts_fts MATCH :fts) "
        )
        origem = "FROM prompts p JOIN hits h ON h.id = p.id"
        ordem = "ORDER BY h.score, p.id"
        params["fts"] = fts
    else:
        cte, origem, ordem = "", "FROM prompts p", "ORDER BY p.id"

    onde = " WHERE " + " AND ".join(cond)
    tam = page_size()
    pagina = max(1, int(pagina))

    # UMA passagem, não duas. `count(*) OVER ()` devolve o total do conjunto
    # inteiro em cada linha da página — e o total já teria de ser calculado de
    # qualquer jeito, então a janela sai de graça e a segunda ida ao banco
    # desaparece. Medido no corpus real, buscando "como" (23.561 acertos, o pior
    # caso possível: um termo quase-vazio): 186 ms com duas consultas, 96 ms com
    # esta. Sem busca, onde não há conjunto grande a percorrer, são 24 ms.
    #
    # A janela é avaliada ANTES do LIMIT (é a definição de função de janela),
    # então o número é o total de verdade, não o da página.
    linhas = q.executar_fts(
        conn_corpus,
        f"{cte}SELECT p.uid, substr(COALESCE(p.text_original, p.text), 1, :corte) AS trecho, "
        f"       p.n_chars, p.lang, p.source, p.license, count(*) OVER () AS total "
        f"{origem}{onde} {ordem} LIMIT :limite OFFSET :salto",
        {**params, "corte": texto_da_lista(), "limite": tam, "salto": (pagina - 1) * tam},
        busca,
    )
    if linhas:
        total = int(linhas[0]["total"])
    else:
        # Página vazia: ou não há nada, ou o salto passou do fim. O segundo caso
        # é raro (a tela não oferece página que não existe) e é o único em que a
        # janela não tem linha nenhuma para carimbar — aí o count sai à parte.
        total = int(
            q.executar_fts(
                conn_corpus, f"{cte}SELECT count(*) AS n {origem}{onde}", params, busca
            )[0]["n"]
        )

    itens = [
        {
            "uid": str(linha["uid"]),
            "trecho": str(linha["trecho"]),
            "truncado": int(linha["n_chars"]) > texto_da_lista(),
            "n_chars": int(linha["n_chars"]),
            "lang": str(linha["lang"]),
            "fonte": str(linha["source"]),
            "licenca": str(linha["license"]),
        }
        for linha in linhas
    ]
    return {
        "items": itens,
        "total": total,
        "page": pagina,
        "page_size": tam,
        "pages": (total + tam - 1) // tam,
        "busca_aplicada": fts,
    }


def marcar_ja_anotei(
    conn: sqlite3.Connection, itens: list[dict[str, Any]], tipo: str, anotador_id: int
) -> None:
    """Carimba ``ja_anotei`` em cada item da página, **por (tipo, anotador)**.

    Por tipo, e não por prompt: avaliar com rubrica e comparar A/B sobre o mesmo
    texto são trabalhos diferentes, e esconder o segundo porque o primeiro foi
    feito tiraria metade do catálogo da vista sem motivo.

    Uma consulta para a página inteira (20 uids), não uma por item — e no banco
    da plataforma, que é onde o histórico mora.
    """
    for item in itens:
        item["ja_anotei"] = False
    if not itens:
        return
    uids = [item["uid"] for item in itens]
    marcas, params = _marcas("k", uids)
    linhas = conn.execute(
        f"SELECT DISTINCT t.prompt_uid AS uid FROM tarefas t "
        "JOIN atribuicoes a ON a.tarefa_id = t.id "
        f"WHERE t.tipo = :tipo AND a.anotador_id = :eu AND t.prompt_uid IN ({marcas}) "
        "  AND a.status IN ('em_andamento','submetida','aprovada')",
        {**params, "tipo": tipo, "eu": anotador_id},
    ).fetchall()
    feitos = {str(linha["uid"]) for linha in linhas}
    for item in itens:
        item["ja_anotei"] = item["uid"] in feitos


def marcar_material(conn: sqlite3.Connection, itens: list[dict[str, Any]]) -> None:
    """Carimba ``pode``: quais tipos de tarefa este prompt consegue sustentar.

    **É o "o erro se anuncia antes de acontecer" aplicado ao catálogo.** Escrever
    uma rubrica ou uma resposta SFT só precisa do prompt; avaliar com rubrica
    precisa de uma rubrica ativa E de uma resposta de modelo; comparar A/B
    precisa de DUAS respostas. Um prompt cru do corpus não tem nada disso — e um
    botão "Anotar este" que leva a uma tela sem material é exatamente o beco que
    esta plataforma não pode ter.

    Sem isto, a alternativa seria descobrir no clique (um 409) ou, pior, abrir
    um workspace vazio de duas colunas que nunca vão existir.
    """
    if not itens:
        return
    uids = [item["uid"] for item in itens]
    marcas, params = _marcas("m", uids)
    com_rubrica = {
        str(linha["prompt_uid"])
        for linha in conn.execute(
            f"SELECT DISTINCT prompt_uid FROM rubricas "
            f"WHERE status = 'ativa' AND prompt_uid IN ({marcas})",
            params,
        )
    }
    respostas: dict[str, int] = {}
    for linha in conn.execute(
        f"SELECT prompt_uid, count(DISTINCT rotulo_modelo) AS n FROM respostas_modelo "
        f"WHERE prompt_uid IN ({marcas}) GROUP BY prompt_uid",
        params,
    ):
        respostas[str(linha["prompt_uid"])] = int(linha["n"])

    for item in itens:
        uid = item["uid"]
        n_respostas = respostas.get(uid, 0)
        # As duas de CONVERSA (P4d) não precisam de material nenhum: a resposta
        # do modelo é gerada na hora, e a rubrica delas é da plataforma. Um
        # prompt cru do corpus sustenta as quatro daqui.
        pode = ["escrever_rubrica", "sft_resposta", "conversa_modelo", "duelo_modelos"]
        if uid in com_rubrica and n_respostas >= 1:
            pode.append("avaliar_rubrica")
        if n_respostas >= 2:
            pode.append("comparar_ab")
        item["pode"] = pode
        item["n_respostas"] = n_respostas
        item["tem_rubrica"] = uid in com_rubrica


def material_faltando(conn: sqlite3.Connection, uid: str, tipo: str) -> str | None:
    """O que falta para este prompt sustentar ``tipo`` — ou ``None`` se dá.

    A MESMA regra de ``marcar_material``, do lado de quem decide: o catálogo
    desabilita o botão, esta função é quem recusa de verdade. As duas discordarem
    significaria um botão habilitado que devolve erro.
    """
    item = {"uid": uid}
    marcar_material(conn, [item])
    if tipo in item["pode"]:
        return None
    if tipo == "comparar_ab":
        return (
            f"este prompt tem {item['n_respostas']} resposta(s) de modelo e a comparação A/B "
            "precisa de duas. Você pode escrever a rubrica ou a resposta de referência dele."
        )
    return (
        "este prompt ainda não tem rubrica ativa e resposta de modelo para avaliar. "
        "Você pode escrever a rubrica dele primeiro."
    )


def fontes_do_pool(conn_corpus: sqlite3.Connection, uids_pool: list[str]) -> list[str]:
    """As fontes que existem DENTRO do pool, para o terceiro filtro.

    Lidas do pool e não do corpus inteiro: um seletor com 7 fontes das quais 5
    não têm nenhum prompt oferecível é um filtro que devolve vazio e parece bug.
    """
    if not uids_pool:
        return []
    marcas, params = _marcas("u", uids_pool)
    linhas = conn_corpus.execute(
        f"SELECT DISTINCT source FROM prompts WHERE uid IN ({marcas}) ORDER BY source",
        params,
    ).fetchall()
    return [str(linha["source"]) for linha in linhas]


def uma_pagina(
    conn: sqlite3.Connection,
    conn_corpus: sqlite3.Connection,
    *,
    anotador_id: int,
    tipo: str,
    busca: str | None,
    lang: str | None,
    fonte: str | None,
    faixa: str | None,
    pagina: int,
) -> dict[str, Any]:
    """O catálogo inteiro de uma tacada: página + ``ja_anotei`` + as fontes."""
    uids = tmod.uids_do_pool(conn, conn_corpus)
    resultado = buscar(
        conn_corpus,
        uids,
        busca=busca,
        lang=lang,
        fonte=fonte,
        faixa=faixa,
        pagina=pagina,
    )
    marcar_ja_anotei(conn, resultado["items"], tipo, anotador_id)
    marcar_material(conn, resultado["items"])
    resultado["fontes"] = fontes_do_pool(conn_corpus, uids)
    resultado["n_pool"] = len(uids)
    resultado["faixas"] = list(FAIXAS)
    return resultado


__all__ = [
    "FAIXAS",
    "buscar",
    "fontes_do_pool",
    "marcar_ja_anotei",
    "marcar_material",
    "material_faltando",
    "page_size",
    "texto_da_lista",
    "uma_pagina",
]
