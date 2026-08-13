"""A fila de PEDIDOS: reservar, devolver, e o que a vista criar mostra.

O PEDIDO É INSUMO, NÃO TAREFA — E ISSO DECIDE TUDO AQUI
=======================================================
Um pedido não entra em ``tarefas``. Ele é para a vista criar o que o brief do P9
é para as telas de anotação: o enquadramento no topo, não a unidade de trabalho.
A consequência prática é que este módulo **não** tem atribuição, versão,
triagem nem Rate and Review — tem uma reserva, que é bem menos que isso.

Por que existe reserva, então? Porque escrever um prompt bom leva tempo e duas
pessoas trabalhando o mesmo recorte produzem trabalho duplicado que ninguém
pediu. A reserva é a única coordenação necessária, e ela é frouxa de propósito:
expira sozinha, devolve-se com um clique e não guarda histórico.

EXPIRAÇÃO PREGUIÇOSA, COMO EM ``atribuicoes``
=============================================
Um UPDATE antes de cada listagem e de cada claim, sem thread de fundo. O carimbo
sai de ``db.SQL_AGORA`` — a comparação é entre STRINGS, e só funciona porque o
formato é lexicograficamente ordenável. Um segundo formato de data aqui quebraria
a comparação em silêncio, com os dois lados parecendo datas.

O RECORTE VIAJA PARA A TELA, E SÓ PARA ELA
==========================================
``apresentar`` devolve o recorte porque quem escreve o prompt precisa LER o
trecho — é a razão de o pedido existir. O que ele nunca faz é sair da máquina:
a tabela ``pedidos`` não entra em perfil de entrega nenhum, e o ``meta_json`` do
``pf ingest plataforma`` não carrega texto livre.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..config import get as _cfg
from . import db as adb
from . import eventos as evmod

#: Colunas que a API devolve. Uma constante e não ``SELECT *`` pela razão de
#: ``criacoes.COLUNAS``: a estrela traria ``chave`` (ruído) e, no dia em que
#: alguém acrescentasse uma coluna interna, ela vazaria sozinha para a tela.
COLUNAS = (
    "p.id, p.arquivo_fonte, p.offset_inicio, p.recorte, p.task_type, p.tema, "
    "p.meta_pedagogica, p.habilidades_json, p.papel, p.serie, p.dificuldade, "
    "p.disciplina, p.colecao, p.status, p.reservado_por, p.reservado_em, "
    "p.avisos_json, p.lote_id, p.criado_em"
)


def reserva_ttl_min() -> int:
    """Minutos de uma reserva sem submissão. Ver ``expirar``.

    Mais longo que o claim de uma tarefa de anotação (``[annotate]
    claim_ttl_min``, 120) não seria absurdo — escrever um prompt bom, uma rubrica
    e uma gold é trabalho de uma hora. Mas a reserva aqui é frouxa: ninguém está
    esperando na fila por ESTE recorte, e o custo de uma expiração cedo demais é
    um clique para reservar de novo.
    """
    return int(_cfg("pedidos", "reserva_ttl_min", default=180))


def expirar(conn: sqlite3.Connection) -> int:
    """Devolve à fila as reservas vencidas. Devolve quantas foram.

    Preguiçoso, como ``tarefas.expirar_vencidas``: um UPDATE antes de cada
    listagem e de cada claim. Um relógio de fundo numa app local é complexidade
    que só aparece quando quebra.
    """
    cur = conn.execute(
        "UPDATE pedidos SET status = 'disponivel', reservado_por = NULL, "
        "                   reservado_em = NULL "
        "WHERE status = 'reservado' "
        f"  AND (reservado_em IS NULL OR reservado_em < "
        f"       strftime('%Y-%m-%dT%H:%M:%fZ','now', '-{reserva_ttl_min()} minutes'))"
    )
    return int(cur.rowcount or 0)


def apresentar(linha: sqlite3.Row, *, reservado_por_nome: str | None = None) -> dict[str, Any]:
    """Linha de ``pedidos`` → JSON da tela.

    ``habilidades`` e ``avisos`` saem como LISTA, e não como a string JSON que
    está na coluna: o front escreve ``pedido.avisos.length`` e precisa que isso
    signifique o que parece. É a mesma regra do ``ativo`` booleano em
    ``models.perfil``.
    """
    def _lista(bruto: Any) -> list[str]:
        try:
            valor = json.loads(str(bruto or "[]"))
        except (TypeError, ValueError):  # pragma: no cover - escrito por nós
            return []
        return [str(x) for x in valor] if isinstance(valor, list) else []

    return {
        "id": int(linha["id"]),
        "arquivo_fonte": str(linha["arquivo_fonte"]),
        "offset_inicio": int(linha["offset_inicio"]),
        "recorte": str(linha["recorte"]),
        "task_type": str(linha["task_type"]),
        "tema": str(linha["tema"]),
        "meta_pedagogica": str(linha["meta_pedagogica"]),
        "habilidades": _lista(linha["habilidades_json"]),
        "papel": str(linha["papel"]),
        "serie": str(linha["serie"]),
        "dificuldade": str(linha["dificuldade"]),
        "disciplina": str(linha["disciplina"]),
        "colecao": str(linha["colecao"]),
        "status": str(linha["status"]),
        "reservado_por": linha["reservado_por"],
        "reservado_por_nome": reservado_por_nome,
        "reservado_em": linha["reservado_em"],
        "avisos": _lista(linha["avisos_json"]),
        "lote_id": str(linha["lote_id"]),
        "criado_em": linha["criado_em"],
    }


def _por_id(conn: sqlite3.Connection, pedido_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {COLUNAS} FROM pedidos p WHERE p.id = ?", (pedido_id,)
    ).fetchone()


def meu(conn: sqlite3.Connection, anotador_id: int) -> dict[str, Any] | None:
    """O pedido que esta pessoa tem reservado agora, ou ``None``.

    Expira antes de olhar: uma reserva vencida que a tela continuasse mostrando
    faria alguém escrever um prompt para um pedido que já voltou para a fila.
    """
    expirar(conn)
    linha = conn.execute(
        f"SELECT {COLUNAS} FROM pedidos p "
        "WHERE p.status = 'reservado' AND p.reservado_por = ? ORDER BY p.id LIMIT 1",
        (anotador_id,),
    ).fetchone()
    return None if linha is None else apresentar(linha)


def proximo(
    conn: sqlite3.Connection,
    *,
    anotador_id: int,
    disciplina: str | None = None,
    papel: str | None = None,
) -> dict[str, Any]:
    """Reserva o próximo pedido disponível. ``{pedido, motivo, motivo_chave}``.

    FILA VAZIA É 200 COM ``pedido: null``, não erro — a mesma decisão do
    ``POST /api/tarefas/proxima``: fila vazia é o estado mais comum de uma
    plataforma bem servida, e um 404 faria o cliente tratar o caminho normal
    como falha.

    UMA RESERVA POR PESSOA. Quem já tem um pedido recebe o MESMO de volta em vez
    de um segundo: dois recortes abertos ao mesmo tempo produziriam dois
    rascunhos concorrentes na mesma vista, que só tem um formulário.

    ``BEGIN IMMEDIATE`` porque escolher e marcar são duas operações e o intervalo
    entre elas é onde duas sessões pegariam o mesmo pedido.
    """
    expirar(conn)
    ja = meu(conn, anotador_id)
    if ja is not None:
        return {"pedido": ja, "motivo": None, "motivo_chave": "ja_reservado"}

    cond = ["p.status = 'disponivel'"]
    params: dict[str, Any] = {}
    if disciplina:
        cond.append("p.disciplina = :disciplina")
        params["disciplina"] = disciplina
    if papel:
        cond.append("p.papel = :papel")
        params["papel"] = papel

    conn.execute("BEGIN IMMEDIATE")
    try:
        linha = conn.execute(
            f"SELECT {COLUNAS} FROM pedidos p WHERE {' AND '.join(cond)} "
            "ORDER BY p.id LIMIT 1",
            params,
        ).fetchone()
        if linha is None:
            conn.execute("ROLLBACK")
            # A frase em português é para o /docs e para a CLI; a TELA usa a
            # chave. Frase de tela mora na tela — a lição do P3i.
            filtrado = bool(disciplina or papel)
            return {
                "pedido": None,
                "motivo": (
                    "nenhum pedido disponível com esses filtros"
                    if filtrado
                    else "nenhum pedido disponível — destile mais com "
                    "`pf annotate pedidos preparar`"
                ),
                "motivo_chave": "vazio_filtrado" if filtrado else "vazio",
            }
        pedido_id = int(linha["id"])
        conn.execute(
            "UPDATE pedidos SET status = 'reservado', reservado_por = ?, "
            f"                  reservado_em = {adb.SQL_AGORA} WHERE id = ?",
            (anotador_id, pedido_id),
        )
        evmod.registrar(
            conn,
            acao="pedido_reservado",
            entidade="pedido",
            entidade_id=pedido_id,
            ator_id=anotador_id,
            disciplina=str(linha["disciplina"]),
            papel=str(linha["papel"]),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return {"pedido": apresentar(_por_id(conn, pedido_id)), "motivo": None, "motivo_chave": None}


def devolver(conn: sqlite3.Connection, pedido_id: int, *, anotador_id: int) -> dict[str, Any]:
    """Desfaz a reserva. Só quem reservou devolve.

    Devolver **não** arquiva e não penaliza ninguém: o pedido volta para o fim
    de nada — ele volta para a fila na mesma posição, porque a ordem é por id.
    Quem devolveu simplesmente não era a pessoa certa para aquele recorte.
    """
    linha = _por_id(conn, pedido_id)
    if linha is None:
        raise LookupError(f"pedido {pedido_id} não existe")
    if str(linha["status"]) != "reservado":
        raise ValueError(
            f"este pedido está em {linha['status']!r}; só um 'reservado' pode ser devolvido"
        )
    if int(linha["reservado_por"] or 0) != anotador_id:
        raise PermissionError("este pedido está reservado por outra pessoa")

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE pedidos SET status = 'disponivel', reservado_por = NULL, "
            "                   reservado_em = NULL WHERE id = ?",
            (pedido_id,),
        )
        evmod.registrar(
            conn,
            acao="pedido_devolvido",
            entidade="pedido",
            entidade_id=pedido_id,
            ator_id=anotador_id,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return apresentar(_por_id(conn, pedido_id))


def marcar_usado(conn: sqlite3.Connection, pedido_id: int, *, criacao_id: int) -> None:
    """O pedido virou criação. **Sem transação própria** — ver o porquê.

    Chamada de DENTRO da transação de ``criacoes.criar``: se a criação e a marca
    do pedido não commitassem juntas, existiria um instante (e, num crash, um
    estado permanente) em que a criação aponta para um pedido que a fila ainda
    oferece a outra pessoa. É a mesma disciplina do evento gravado na mesma
    transação da mudança que ele registra.
    """
    conn.execute(
        "UPDATE pedidos SET status = 'usado', reservado_por = NULL, reservado_em = NULL "
        "WHERE id = ?",
        (pedido_id,),
    )
    evmod.registrar(
        conn,
        acao="pedido_usado",
        entidade="pedido",
        entidade_id=pedido_id,
        criacao_id=criacao_id,
    )


def conferir_para_criacao(
    conn: sqlite3.Connection, pedido_id: int, *, anotador_id: int
) -> sqlite3.Row:
    """A linha do pedido, se esta pessoa pode escrever a criação dele.

    Ponto ÚNICO da regra, chamado pela rota antes de gravar. Três recusas, e as
    três são erros diferentes para quem lê: o pedido não existe, ele não está
    reservado (voltou para a fila enquanto a pessoa escrevia — o TTL expirou), ou
    está reservado por outra pessoa.

    A do meio é a que mais vai acontecer, e é por isso que a mensagem diz o que
    fazer em vez de só recusar.
    """
    linha = _por_id(conn, pedido_id)
    if linha is None:
        raise LookupError(f"pedido {pedido_id} não existe")
    dono = int(linha["reservado_por"] or 0)
    if str(linha["status"]) != "reservado":
        raise ValueError(
            f"este pedido está em {linha['status']!r} e não reservado — a reserva pode "
            f"ter expirado ({reserva_ttl_min()} min). Puxe-o de novo antes de enviar."
        )
    if dono != anotador_id:
        raise PermissionError("este pedido está reservado por outra pessoa")
    return linha


def facetas(conn: sqlite3.Connection) -> dict[str, Any]:
    """As opções do filtro da fila, **contadas sobre o que está DISPONÍVEL**.

    Contar sobre a tabela inteira ofereceria "Filosofia (5)" numa fila em que os
    cinco já foram usados — um filtro que devolve vazio depois de prometer
    cinco. A contagem e a fila respondem à mesma pergunta ou não servem.
    """
    def _conta(coluna: str) -> list[dict[str, Any]]:
        return [
            {"valor": str(linha[coluna]), "n": int(linha["n"])}
            for linha in conn.execute(
                f"SELECT {coluna}, count(*) AS n FROM pedidos WHERE status = 'disponivel' "
                f"GROUP BY {coluna} ORDER BY {coluna}"
            )
        ]

    expirar(conn)
    return {
        "disponiveis": int(
            conn.execute(
                "SELECT count(*) AS n FROM pedidos WHERE status = 'disponivel'"
            ).fetchone()["n"]
        ),
        "disciplina": _conta("disciplina"),
        "papel": _conta("papel"),
    }


__all__ = [
    "COLUNAS",
    "apresentar",
    "conferir_para_criacao",
    "devolver",
    "expirar",
    "facetas",
    "marcar_usado",
    "meu",
    "proximo",
    "reserva_ttl_min",
]
