"""De onde saem os prompts que a Bancada oferece — e por que isso é resolvido
**a cada request**.

Duas origens, nesta ordem:

1. **A coleção curada.** A ferramenta de curadoria (``:8765``) monta uma coleção
   chamada ``[annotate] pool_collection`` e a Bancada a lê do corpus read-only.
   É a origem boa: um humano escolheu o que vai para a anotação.
2. **O fallback**, obrigatório. ``pf load-db`` reconstrói o banco do zero, e o
   s11 **apaga as coleções** junto — o corpus que está no disco hoje tem zero
   coleções, então esta é a origem ativa na prática. Sem fallback, a plataforma
   ficaria sem pool depois de toda recarga do corpus, o que numa demonstração é
   uma tela morta sem explicação.

POR REQUEST, NUNCA NO LIFESPAN
==============================
O corpus é trocado por swap de arquivo embaixo desta app. Resolver o pool uma
vez na subida daria uma lista de uids de um banco que pode não existir mais — e
o pior desfecho não é o erro, é o acerto aparente: uids que ainda casam, textos
que já são de outras linhas. Resolver por request custa 0,2 ms na contagem (o
que o ``/api/health`` faz) e é sempre verdade.

A ORIGEM ATIVA APARECE NA TELA
==============================
``/api/health`` e o painel do admin dizem qual das duas está valendo e por quê.
Um pool que troca de origem em silêncio é exatamente o tipo de mentira que este
projeto não conta: o avaliador veria 500 prompts nos dois casos e não teria como
saber que a curadoria evaporou.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..config import get as _cfg

#: Valor de ``origem`` quando a coleção curada não pôde ser usada.
FALLBACK = "fallback"


def cfg_pool() -> dict[str, object]:
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


#: FILTRO DO FALLBACK. ``nsfw IS NOT 1`` (e não ``= 0``) mantém os NULL, que hoje
#: são o corpus inteiro — a campanha de rotulagem ainda não rodou. Esta cláusula
#: **não** é configurável de propósito: não é um limiar a calibrar, é a regra de
#: que uma demonstração aberta na frente de um avaliador nunca abre com conteúdo
#: explícito na tela.
_FILTRO_FALLBACK = (
    "FROM prompts "
    "WHERE lang = :lang AND n_chars >= :min_chars AND n_chars <= :max_chars "
    "  AND n_exact_dups <= :max_dups AND nsfw IS NOT 1"
)


def _params_fallback(cfg: dict[str, object]) -> dict[str, object]:
    return {
        "lang": cfg["lang"],
        "min_chars": cfg["min_chars"],
        "max_chars": cfg["max_chars"],
        "max_dups": cfg["max_dups"],
    }


def _uids_fallback(conn: sqlite3.Connection, cfg: dict[str, object]) -> list[str]:
    """``pool_max`` uids do corpus, **amostrados com passo constante**.

    Por que não ``ORDER BY id LIMIT 500``: o universo é gravado agrupado por
    fonte, então os 500 primeiros que casam o filtro saem TODOS de ``arena140k``
    (medido). Um pool monofonte faz a plataforma mentir sobre o corpus que ela
    representa. O passo constante sobre o conjunto inteiro devolve a proporção
    real — medido no corpus de hoje: 393 wildchat_pt, 83 aya, 19 arena140k,
    4 oasst, 1 wildchat_en.

    Por que não ``ORDER BY uid``: ordenar por uid obriga a buscar o uid de cada
    uma das 23 mil linhas que casam, e ``text`` chega a 1 MB por linha — são
    ~100 ms de páginas lidas para jogar 22.700 fora. O passo lê só rowids e
    hidrata 500.

    Determinístico: mesmo corpus e mesmas chaves, mesmos 500 uids, sempre.
    """
    teto = int(cfg["max"])
    ids = [
        int(linha["id"])
        for linha in conn.execute(
            f"SELECT id {_FILTRO_FALLBACK} ORDER BY id", _params_fallback(cfg)
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


def _contar_fallback(conn: sqlite3.Connection, cfg: dict[str, object]) -> int:
    """Quantas linhas o fallback entregaria, já com o teto.

    O ``LIMIT`` DENTRO da subconsulta é o que torna isto barato (0,2 ms medidos,
    contra ~95 ms de um ``count(*)`` do conjunto todo): o SQLite para na
    quingentésima linha do índice de cobertura e nunca toca a tabela. E o número
    que sai é exatamente ``len(uids)`` da função acima — a contagem que o health
    mostra é a mesma que o catálogo vai entregar, não uma estimativa.
    """
    sql = f"SELECT count(*) AS n FROM (SELECT 1 {_FILTRO_FALLBACK} LIMIT {int(cfg['max'])})"
    return int(conn.execute(sql, _params_fallback(cfg)).fetchone()["n"])


def _colecao_id(conn: sqlite3.Connection, nome: str) -> int | None:
    linha = conn.execute("SELECT id FROM collections WHERE name = ?", (nome,)).fetchone()
    return None if linha is None else int(linha["id"])


def resolver(conn_corpus: sqlite3.Connection, *, com_uids: bool = False) -> Pool:
    """O pool de agora, a partir da conexão **read-only** do corpus.

    ``com_uids=False`` (o padrão) faz só as contagens — é o que o
    ``/api/health`` chama a cada abertura de tela. Peça ``com_uids=True`` só
    quando a lista for de fato usada: no fallback ela custa ~100 ms.
    """
    cfg = cfg_pool()
    nome = str(cfg["colecao"])
    teto = int(cfg["max"])

    colecao_id = _colecao_id(conn_corpus, nome)
    if colecao_id is not None:
        n = int(
            conn_corpus.execute(
                "SELECT count(*) AS n FROM (SELECT 1 FROM collection_items "
                f"WHERE collection_id = ? LIMIT {teto})",
                (colecao_id,),
            ).fetchone()["n"]
        )
        if n:
            uids: list[str] = []
            if com_uids:
                uids = [
                    str(linha["uid"])
                    for linha in conn_corpus.execute(
                        "SELECT p.uid FROM collection_items ci "
                        "JOIN prompts p ON p.id = ci.prompt_id "
                        "WHERE ci.collection_id = ? ORDER BY p.uid LIMIT ?",
                        (colecao_id, teto),
                    )
                ]
            return Pool(
                origem=f"colecao:{nome}",
                n_pool=n,
                motivo=f"coleção {nome!r} curada na interface de curadoria",
                colecao=nome,
                uids=uids,
            )
        # Coleção existe mas está vazia: tratada como ausente. Um pool de zero
        # itens não é uma origem, é uma tela morta — e o motivo abaixo diz
        # exatamente o que aconteceu, em vez de a plataforma fingir que
        # a curadoria está valendo.
        motivo = (
            f"a coleção {nome!r} existe no corpus mas está vazia — "
            "filtro determinístico enquanto ninguém a preenche"
        )
    else:
        motivo = (
            f"não existe coleção {nome!r} no corpus (toda `pf load-db` reconstrói "
            "o banco e apaga as coleções) — filtro determinístico no lugar"
        )

    if com_uids:
        lista = _uids_fallback(conn_corpus, cfg)
        return Pool(FALLBACK, len(lista), motivo, nome, lista)
    return Pool(FALLBACK, _contar_fallback(conn_corpus, cfg), motivo, nome)


def descrever_fallback() -> str:
    """O filtro do fallback em uma linha, para a tela explicar o que está vendo."""
    cfg = cfg_pool()
    return (
        f"idioma {cfg['lang']}, de {cfg['min_chars']} a {cfg['max_chars']} caracteres, "
        f"até {cfg['max_dups']} duplicata(s) exata(s), sem NSFW, "
        f"amostra de passo constante até {cfg['max']}"
    )


__all__ = ["FALLBACK", "Pool", "cfg_pool", "descrever_fallback", "resolver"]
