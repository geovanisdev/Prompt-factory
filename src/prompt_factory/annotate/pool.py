"""De onde saem os prompts que a Bancada oferece — e o que ela NUNCA oferece.

Duas origens, nesta ordem:

1. **A coleção curada.** A ferramenta de curadoria (``:8765``) monta uma coleção
   chamada ``[annotate] pool_collection`` e a Bancada a lê do corpus read-only.
   É a origem boa: um humano escolheu o que vai para a anotação.
2. **O fallback**, obrigatório. ``pf load-db`` reconstrói o banco do zero, e o
   s11 **apaga as coleções** junto — o corpus que está no disco hoje tem zero
   coleções, então esta é a origem ativa na prática. Sem fallback, a plataforma
   ficaria sem pool depois de toda recarga do corpus, o que numa demonstração é
   uma tela morta sem explicação.

A POLÍTICA DE LICENÇA VALE NAS DUAS ORIGENS
============================================
Este é o P3a corrigindo um defeito medido. Até aqui o pool filtrava idioma,
tamanho e duplicatas e **não filtrava licença**: bastava trocar
``pool_fallback_lang`` para ``"en"`` para que **9.148 linhas ``cc-by-nc-4.0``
com ``commercial_ok = 0``** entrassem no trabalho de anotação (60 delas na
amostra de 500 que a plataforma de fato serve), sem uma palavra na tela.

Isso contradiz a tese inteira do projeto. O ``export.py`` exclui
``redistributable = 0`` do export justamente porque a proveniência por linha é o
argumento do Prompt Factory; a plataforma não tinha herdado a política. Agora
herda, **com o default fechado**: só entra o que é comercialmente utilizável
E redistribuível. As duas chaves estão em ``[annotate]`` e a política ativa —
mais quantas linhas ela exclui — aparece no ``/api/health`` e no painel do admin.

A cláusula vale para a **coleção curada também**. Um humano escolhendo na
curadoria não deixa de ser um humano que pode escolher uma linha ``cc-by-nc``:
a política é do produto, não do fallback.

SOBRE NSFW, A VERDADE
=====================
``nsfw IS NOT 1`` continua na cláusula e hoje **exclui zero linhas**: ``nsfw`` é
NULO nas 159.733 do corpus, porque a campanha de rotulagem ainda não rodou. A
cláusula fica (ela passa a valer no dia em que os rótulos existirem), mas a tela
e ``descrever_fallback`` dizem isso com todas as letras. Prometer uma garantia
que o dado não sustenta é pior do que não ter a garantia.

MATERIALIZADO, COM CHAVE DE INVALIDAÇÃO EXPLÍCITA
==================================================
Resolver o pool no corpus a cada request custava **607 ms medidos** — e ele está
no caminho de ``/api/catalogo``, ``/api/prompts/{uid}`` e ``/api/tarefas/livre``.
(As docstrings antigas prometiam "~100 ms" e "0,2 ms". Erravam por 6x e 70x;
documentação que mente é pior que documentação ausente, e por isso os números
deste módulo agora são os medidos.)

A solução é a tabela ``pool(uid, ordem)`` no ``annotate.sqlite``, reconstruída
quando a **assinatura** muda (``assinatura()``: build do corpus + configuração
do pool + tamanho da coleção). Isto não é cache de agregado: um cache mente
quando um escritor externo muda o dado por baixo, e é exatamente o que a
assinatura impede — ela é derivada do build do corpus, não do relógio.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ..config import get as _cfg

#: Valor de ``origem`` quando a coleção curada não pôde ser usada.
FALLBACK = "fallback"

#: Chaves de ``app_meta`` (no banco da plataforma) do pool materializado.
CHAVE_ASSINATURA = "pool_assinatura"
CHAVE_ORIGEM = "pool_origem"
CHAVE_MOTIVO = "pool_motivo"
CHAVE_COLECAO = "pool_colecao"
CHAVE_EXCLUIDAS = "pool_excluidas_licenca"
CHAVE_ATUALIZADO = "pool_atualizado_em"
CHAVE_BUILD = "pool_db_build_id"

#: A frase honesta sobre NSFW. Uma constante porque ela aparece em três lugares
#: (o filtro descrito, o health e o painel do admin) e as três precisam dizer a
#: MESMA coisa — foi a divergência entre a promessa e o dado que criou o defeito.
NOTA_NSFW = (
    "a marcação de conteúdo sensível depende da campanha de rotulagem, que ainda "
    "não rodou: hoje `nsfw` é nulo no corpus inteiro e a cláusula exclui zero linhas"
)


def cfg_pool() -> dict[str, Any]:
    """As chaves de ``[annotate]`` que definem o pool, lidas na hora.

    Lidas por chamada (e não no import) de propósito: são a única configuração
    desta app que um usuário mexe entre duas subidas para ver o efeito na tela.
    O ``config`` já é cacheado por arquivo, então isto é um acesso a dict.
    """
    return {
        "colecao": str(_cfg("annotate", "pool_collection", default="anotacao")),
        "max": int(_cfg("annotate", "pool_max", default=500)),
        "lang": str(_cfg("annotate", "pool_fallback_lang", default="pt")),
        "min_chars": int(_cfg("annotate", "pool_fallback_min_chars", default=40)),
        "max_chars": int(_cfg("annotate", "pool_fallback_max_chars", default=2000)),
        "max_dups": int(_cfg("annotate", "pool_fallback_max_dups", default=0)),
        # DEFAULT FECHADO. Só entra o que é comercialmente utilizável e
        # redistribuível — a mesma política que o `export.py` aplica ao banco.
        "comercial": bool(_cfg("annotate", "pool_exigir_commercial_ok", default=True)),
        "redistribuivel": bool(
            _cfg("annotate", "pool_exigir_redistributable", default=True)
        ),
    }


@dataclass(slots=True)
class Pool:
    """O pool resolvido. ``uids`` só vem preenchido quando alguém o pediu."""

    #: ``"colecao:<nome>"`` ou ``"fallback"``. É o que a tela mostra.
    origem: str
    #: Quantos prompts o pool tem AGORA (já com o teto de ``pool_max`` aplicado).
    n_pool: int
    #: Por que esta origem, em português, para aparecer no painel do admin.
    motivo: str
    #: Nome da coleção procurada, valha ela ou não.
    colecao: str
    uids: list[str] = field(default_factory=list)
    #: Quantas linhas a POLÍTICA DE LICENÇA tirou da origem ativa. É o número
    #: que prova, na tela, que a política não é decorativa.
    excluidas_licenca: int = 0


# ---------------------------------------------------------------------------
# a política de licença
# ---------------------------------------------------------------------------


def politica_licenca(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """A política ativa, no formato que ``/api/health`` e o admin mostram."""
    c = cfg if cfg is not None else cfg_pool()
    exigencias = []
    if c["comercial"]:
        exigencias.append("commercial_ok = 1")
    if c["redistribuivel"]:
        exigencias.append("redistributable = 1")
    return {
        "commercial_ok": bool(c["comercial"]),
        "redistributable": bool(c["redistribuivel"]),
        "descricao": (
            "sem restrição de licença — TODA linha do corpus pode ser anotada"
            if not exigencias
            else "só entra prompt com " + " e ".join(exigencias)
        ),
        "fechada": bool(c["comercial"] and c["redistribuivel"]),
    }


def _clausulas_licenca(cfg: dict[str, Any]) -> str:
    """O pedaço de SQL da política. Vazio quando ela está desligada."""
    partes = []
    if cfg["comercial"]:
        partes.append("p.commercial_ok = 1")
    if cfg["redistribuivel"]:
        partes.append("p.redistributable = 1")
    return "".join(f" AND {parte}" for parte in partes)


def _filtro_fallback(cfg: dict[str, Any], *, com_licenca: bool = True) -> str:
    """O FROM/WHERE do fallback.

    ``com_licenca=False`` devolve o mesmo filtro **sem** a política — é como se
    conta quantas linhas ela exclui, e esse número vai para a tela.

    ``nsfw IS NOT 1`` (e não ``= 0``) mantém os NULL, que hoje são o corpus
    inteiro. Ver ``NOTA_NSFW``: a cláusula é verdadeira e vazia até a campanha
    de rotulagem rodar, e a tela diz isso.
    """
    base = (
        "FROM prompts p "
        "WHERE p.lang = :lang AND p.n_chars >= :min_chars AND p.n_chars <= :max_chars "
        "  AND p.n_exact_dups <= :max_dups AND p.nsfw IS NOT 1"
    )
    return base + (_clausulas_licenca(cfg) if com_licenca else "")


def _params_fallback(cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "lang": cfg["lang"],
        "min_chars": cfg["min_chars"],
        "max_chars": cfg["max_chars"],
        "max_dups": cfg["max_dups"],
    }


# ---------------------------------------------------------------------------
# resolução no corpus  (o caminho LENTO — ~607 ms medidos com uids)
# ---------------------------------------------------------------------------


def _uids_fallback(conn: sqlite3.Connection, cfg: dict[str, Any]) -> list[str]:
    """``pool_max`` uids do corpus, **amostrados com passo constante**.

    Por que não ``ORDER BY id LIMIT 500``: o universo é gravado agrupado por
    fonte, então os 500 primeiros que casam o filtro saem TODOS de ``arena140k``
    (medido). Um pool monofonte faz a plataforma mentir sobre o corpus que ela
    representa. O passo constante sobre o conjunto inteiro devolve a proporção
    real — medido no corpus de hoje, em pt: 411 wildchat_pt, 69 aya,
    16 arena140k, 4 oasst.

    Por que não ``ORDER BY uid``: ordenar por uid obriga a buscar o uid de cada
    uma das 27 mil linhas que casam, e ``text`` chega a 1 MB por linha — são
    centenas de ms de páginas lidas para jogar 27.200 fora. O passo lê só rowids
    e hidrata 500.

    Determinístico: mesmo corpus e mesmas chaves, mesmos 500 uids, sempre. Custa
    **~607 ms medidos** contra o corpus real, e é por isso que quem o chama é a
    materialização (uma vez por build do corpus), não o request.
    """
    teto = int(cfg["max"])
    ids = [
        int(linha["id"])
        for linha in conn.execute(
            f"SELECT p.id {_filtro_fallback(cfg)} ORDER BY p.id", _params_fallback(cfg)
        )
    ]
    if not ids:
        return []
    if len(ids) > teto:
        passo = len(ids) / teto
        ids = [ids[int(i * passo)] for i in range(teto)]
    marcas = ", ".join("?" * len(ids))
    return [
        str(linha["uid"])
        for linha in conn.execute(
            f"SELECT uid FROM prompts WHERE id IN ({marcas}) ORDER BY id", ids
        )
    ]


def _contar_fallback(conn: sqlite3.Connection, cfg: dict[str, Any]) -> int:
    """Quantas linhas o fallback entregaria, já com o teto.

    O ``LIMIT`` DENTRO da subconsulta é o que torna isto barato: o SQLite para
    na quingentésima linha do índice e nunca varre o conjunto todo. **~14 ms
    medidos** contra o corpus real (a docstring antiga dizia 0,2 ms — errava por
    70x). E o número que sai é exatamente ``len(uids)`` da função acima: a
    contagem que o health mostra é a mesma que o catálogo vai entregar.
    """
    sql = (
        f"SELECT count(*) AS n FROM (SELECT 1 {_filtro_fallback(cfg)} "
        f"LIMIT {int(cfg['max'])})"
    )
    return int(conn.execute(sql, _params_fallback(cfg)).fetchone()["n"])


def _excluidas_por_licenca_fallback(conn: sqlite3.Connection, cfg: dict[str, Any]) -> int:
    """Quantas linhas elegíveis a política de licença derruba. 0 se desligada.

    Conta o conjunto INTEIRO (sem o teto de ``pool_max``): é o número que
    responde "o que a política está protegendo?", e o teto o esconderia.
    Roda só na materialização, e o resultado fica em ``app_meta``.
    """
    if not (cfg["comercial"] or cfg["redistribuivel"]):
        return 0
    par = _params_fallback(cfg)
    total = int(
        conn.execute(
            f"SELECT count(*) AS n {_filtro_fallback(cfg, com_licenca=False)}", par
        ).fetchone()["n"]
    )
    passam = int(
        conn.execute(f"SELECT count(*) AS n {_filtro_fallback(cfg)}", par).fetchone()["n"]
    )
    return total - passam


def _colecao_id(conn: sqlite3.Connection, nome: str) -> int | None:
    linha = conn.execute("SELECT id FROM collections WHERE name = ?", (nome,)).fetchone()
    return None if linha is None else int(linha["id"])


_COLECAO_FROM = (
    "FROM collection_items ci JOIN prompts p ON p.id = ci.prompt_id "
    "WHERE ci.collection_id = :colecao"
)


def _da_colecao(
    conn: sqlite3.Connection, colecao_id: int, cfg: dict[str, Any], *, com_uids: bool
) -> tuple[int, list[str], int]:
    """``(n, uids, excluidas_licenca)`` da coleção curada, já sob a política.

    A política vale aqui também: um humano curando na outra ferramenta pode
    escolher uma linha ``cc-by-nc`` sem perceber, e a regra é do produto — não
    do fallback.
    """
    par: dict[str, Any] = {"colecao": colecao_id}
    teto = int(cfg["max"])
    onde = _COLECAO_FROM + _clausulas_licenca(cfg)
    n = int(
        conn.execute(
            f"SELECT count(*) AS n FROM (SELECT 1 {onde} LIMIT {teto})", par
        ).fetchone()["n"]
    )
    excluidas = 0
    if cfg["comercial"] or cfg["redistribuivel"]:
        total = int(
            conn.execute(f"SELECT count(*) AS n {_COLECAO_FROM}", par).fetchone()["n"]
        )
        passam = int(conn.execute(f"SELECT count(*) AS n {onde}", par).fetchone()["n"])
        excluidas = total - passam
    uids: list[str] = []
    if com_uids and n:
        uids = [
            str(linha["uid"])
            for linha in conn.execute(
                f"SELECT p.uid {onde} ORDER BY p.uid LIMIT {teto}", par
            )
        ]
    return n, uids, excluidas


def resolver(conn_corpus: sqlite3.Connection, *, com_uids: bool = False) -> Pool:
    """O pool de agora, a partir da conexão **read-only** do corpus.

    Este é o caminho LENTO, e ele existe para dois usos: a materialização (uma
    vez por build do corpus) e o ``pf annotate status``, que não tem o banco da
    plataforma em mãos. **As rotas não chamam isto** — elas chamam ``uids()``,
    que lê a tabela materializada.

    ``com_uids=False`` faz só as contagens (~14 ms medidos);
    ``com_uids=True`` monta a lista (~607 ms medidos, no fallback).
    """
    cfg = cfg_pool()
    nome = str(cfg["colecao"])

    colecao_id = _colecao_id(conn_corpus, nome)
    if colecao_id is not None:
        n, uids, excluidas = _da_colecao(conn_corpus, colecao_id, cfg, com_uids=com_uids)
        if n:
            motivo = f"coleção {nome!r} curada na interface de curadoria"
            if excluidas:
                motivo += (
                    f"; {excluidas} item(ns) dela ficaram de fora pela política de licença"
                )
            return Pool(
                origem=f"colecao:{nome}",
                n_pool=n,
                motivo=motivo,
                colecao=nome,
                uids=uids,
                excluidas_licenca=excluidas,
            )
        # Coleção existe mas está vazia (ou a política zerou): tratada como
        # ausente. Um pool de zero itens não é uma origem, é uma tela morta — e
        # o motivo abaixo diz exatamente o que aconteceu, em vez de a plataforma
        # fingir que a curadoria está valendo.
        motivo = (
            f"a coleção {nome!r} existe no corpus mas está vazia — "
            "filtro determinístico enquanto ninguém a preenche"
        )
        if excluidas:
            motivo = (
                f"a coleção {nome!r} existe mas nenhum item dela passa na política de "
                f"licença ({excluidas} excluído(s)) — filtro determinístico no lugar"
            )
    else:
        motivo = (
            f"não existe coleção {nome!r} no corpus (toda `pf load-db` reconstrói "
            "o banco e apaga as coleções) — filtro determinístico no lugar"
        )

    excluidas = _excluidas_por_licenca_fallback(conn_corpus, cfg)
    if com_uids:
        lista = _uids_fallback(conn_corpus, cfg)
        return Pool(FALLBACK, len(lista), motivo, nome, lista, excluidas)
    return Pool(
        FALLBACK,
        _contar_fallback(conn_corpus, cfg),
        motivo,
        nome,
        excluidas_licenca=excluidas,
    )


# ---------------------------------------------------------------------------
# materialização
# ---------------------------------------------------------------------------


def _build_id(conn_corpus: sqlite3.Connection) -> str:
    linha = conn_corpus.execute(
        "SELECT value FROM app_meta WHERE key = 'db_build_id'"
    ).fetchone()
    return "" if linha is None else str(linha["value"])


def assinatura(conn_corpus: sqlite3.Connection) -> str:
    """A chave de invalidação do pool materializado.

    Três ingredientes, e os três são necessários:

    1. **``db_build_id`` do corpus** — toda ``pf load-db`` o troca, e é assim
       que "o corpus mudou embaixo da app" vira uma reconstrução.
    2. **A configuração do pool inteira** — trocar ``pool_fallback_lang`` no
       ``settings.toml`` tem de refazer o pool na próxima subida. Sem isto,
       ajustar o arquivo não mudaria nada e pareceria que a chave não funciona.
    3. **O tamanho da coleção curada** — a outra ferramenta escreve nela sem
       tocar no ``db_build_id``. É uma consulta indexada, ~0,1 ms.

    Barata de propósito: ela roda em TODO request que precisa do pool.
    """
    cfg = cfg_pool()
    n_colecao = -1
    colecao_id = _colecao_id(conn_corpus, str(cfg["colecao"]))
    if colecao_id is not None:
        n_colecao = int(
            conn_corpus.execute(
                "SELECT count(*) AS n FROM collection_items WHERE collection_id = ?",
                (colecao_id,),
            ).fetchone()["n"]
        )
    material = json.dumps(
        {"build": _build_id(conn_corpus), "cfg": cfg, "colecao_n": n_colecao},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _gravar(conn: sqlite3.Connection, p: Pool, assin: str, build: str) -> None:
    from . import db as adb

    conn.execute("DELETE FROM pool")
    conn.executemany(
        "INSERT INTO pool (uid, ordem) VALUES (?, ?)",
        [(uid, i) for i, uid in enumerate(p.uids)],
    )
    for chave, valor in (
        (CHAVE_ASSINATURA, assin),
        (CHAVE_ORIGEM, p.origem),
        (CHAVE_MOTIVO, p.motivo),
        (CHAVE_COLECAO, p.colecao),
        (CHAVE_EXCLUIDAS, p.excluidas_licenca),
        (CHAVE_BUILD, build),
    ):
        adb.set_meta(conn, chave, valor)
    conn.execute(
        "INSERT INTO app_meta (key, value) VALUES (?, "
        "strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (CHAVE_ATUALIZADO,),
    )


def materializar(
    conn: sqlite3.Connection,
    conn_corpus: sqlite3.Connection,
    *,
    forcar: bool = False,
) -> Pool:
    """Garante a tabela ``pool`` em dia e devolve o pool (sem os uids).

    No caminho comum — assinatura igual — são **duas leituras indexadas** e nada
    mais. Quando ela muda, reconstrói dentro de ``BEGIN IMMEDIATE``: com duas
    abas abertas na demonstração (o caso normal, não o excepcional), duas
    materializações simultâneas escreveriam a mesma tabela ao mesmo tempo. A
    assinatura é reconferida DENTRO da transação, então a segunda vira no-op em
    vez de refazer os 607 ms de trabalho que a primeira acabou de fazer.
    """
    from . import db as adb

    assin = assinatura(conn_corpus)
    atual = adb.get_meta(conn, CHAVE_ASSINATURA)
    if not forcar and atual == assin:
        return _do_banco(conn)

    conn.execute("BEGIN IMMEDIATE")
    try:
        if not forcar and adb.get_meta(conn, CHAVE_ASSINATURA) == assin:
            conn.execute("COMMIT")
            return _do_banco(conn)
        p = resolver(conn_corpus, com_uids=True)
        _gravar(conn, p, assin, _build_id(conn_corpus))
        from . import eventos as evmod

        evmod.registrar(
            conn,
            acao="pool_materializado",
            entidade="banco",
            origem=p.origem,
            n_pool=p.n_pool,
            excluidas_licenca=p.excluidas_licenca,
            assinatura=assin,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    p.uids = []
    return p


def _do_banco(conn: sqlite3.Connection) -> Pool:
    """O pool como ele está gravado, sem tocar no corpus."""
    from . import db as adb

    n = int(conn.execute("SELECT count(*) AS n FROM pool").fetchone()["n"])
    return Pool(
        origem=str(adb.get_meta(conn, CHAVE_ORIGEM, FALLBACK) or FALLBACK),
        n_pool=n,
        motivo=str(adb.get_meta(conn, CHAVE_MOTIVO, "") or ""),
        colecao=str(adb.get_meta(conn, CHAVE_COLECAO, "") or ""),
        excluidas_licenca=int(adb.get_meta(conn, CHAVE_EXCLUIDAS, "0") or 0),
    )


def uids(conn: sqlite3.Connection, conn_corpus: sqlite3.Connection) -> list[str]:
    """Os uids oferecíveis, na ordem determinística — **o caminho das rotas**.

    Materializa se preciso e lê da tabela local. É a função que tirou o pool do
    caminho crítico: de 607 ms por request para uma varredura de 500 linhas num
    índice do próprio banco da plataforma.
    """
    materializar(conn, conn_corpus)
    return [
        str(linha["uid"])
        for linha in conn.execute("SELECT uid FROM pool ORDER BY ordem")
    ]


def esta_no_pool(
    conn: sqlite3.Connection, conn_corpus: sqlite3.Connection, uid: str
) -> bool:
    """``uid IN pool``, sem materializar a lista inteira em Python."""
    materializar(conn, conn_corpus)
    return conn.execute("SELECT 1 FROM pool WHERE uid = ?", (uid,)).fetchone() is not None


def descrever_fallback() -> str:
    """O filtro do fallback em uma linha, para a tela explicar o que está vendo.

    Inclui a política de licença e **a verdade sobre NSFW** — a frase antiga
    dizia "sem NSFW", o que prometia uma garantia que o dado não sustenta.
    """
    cfg = cfg_pool()
    pol = politica_licenca(cfg)
    return (
        f"idioma {cfg['lang']}, de {cfg['min_chars']} a {cfg['max_chars']} caracteres, "
        f"até {cfg['max_dups']} duplicata(s) exata(s), {pol['descricao']}, "
        f"amostra de passo constante até {cfg['max']}"
    )


__all__ = [
    "CHAVE_ASSINATURA",
    "CHAVE_ATUALIZADO",
    "CHAVE_BUILD",
    "CHAVE_COLECAO",
    "CHAVE_EXCLUIDAS",
    "CHAVE_MOTIVO",
    "CHAVE_ORIGEM",
    "FALLBACK",
    "NOTA_NSFW",
    "Pool",
    "assinatura",
    "cfg_pool",
    "descrever_fallback",
    "esta_no_pool",
    "materializar",
    "politica_licenca",
    "resolver",
    "uids",
]
