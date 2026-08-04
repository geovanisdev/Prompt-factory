"""A trilha de auditoria: quem fez o quê, quando, sobre qual coisa.

**Por que uma trilha numa demonstração.** Porque o artefato de portfólio mais
forte que esta plataforma pode produzir é a auditoria completa de UM item —
prompt → anotação v1 → triagem → correções do Rate and Review com o motivo de
cada uma → decisão final. Sem os eventos, essa cadeia teria de ser remontada
por inferência a partir de carimbos de tempo espalhados por cinco tabelas, e
"remontada por inferência" é exatamente o que uma auditoria não pode ser.

REGRA DE ESCRITA
================
Um evento é gravado **na mesma transação** da mudança que ele registra. Um
``INSERT`` de evento depois do ``COMMIT`` é um evento que some quando o commit
falha — e a trilha passa a mentir justamente nos casos interessantes.

O ``acao`` não tem CHECK no DDL (única exceção do schema, ver ``db.py``): o
vocabulário cresce a cada marco, e migrar um banco com trabalho humano dentro só
para registrar um NOME de evento novo seria absurdo. ``ACOES`` abaixo é o
inventário do que existe hoje, e serve de referência para quem lê a tabela.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

#: As ações conhecidas HOJE. Documentação, não CHECK — ver o cabeçalho.
ACOES: tuple[str, ...] = (
    "tarefa_reivindicada",   # claim do modo locked
    "tarefa_escolhida",      # modo livre / continuação
    "tarefa_abandonada",
    "anotacao_submetida",    # inclui o re-trabalho (versao > 1)
    "triagem_aprovada",
    "triagem_devolvida",
    "avaliacao_registrada",  # P3b
    "decisao_admin",         # P3b
    "banco_migrado",
    "pool_materializado",
    # P4c — a campanha de geração. O evento da sintética NÃO carrega o gabarito:
    # `eventos` é a trilha de auditoria e um alvo escondido que vaza por aqui
    # vaza por uma porta que ninguém está olhando.
    "material_importado",
    "anotacao_sintetica_importada",
    # P4d — a conversa com o modelo local. As três já eram gravadas pelas rotas
    # de `routes_conversa` e faltavam neste inventário: uma ação que existe no
    # banco e não existe aqui faz a lista mentir justamente para quem a lê para
    # saber o que procurar.
    "turno_gerado",
    "rodada_gerada",
    "rodada_decidida",
    # P5a — o admin enchendo a fila em lote, pelo painel.
    "tarefas_geradas",
)

#: Os nomes de entidade. ``entidade`` + ``entidade_id`` é a referência lógica:
#: não há FK de propósito, porque um evento sobre algo apagado continua sendo um
#: fato que aconteceu — e uma FK com CASCADE apagaria o registro de que existiu.
ENTIDADES: tuple[str, ...] = (
    "tarefa", "atribuicao", "anotacao", "avaliacao", "banco", "lote"
)


def registrar(
    conn: sqlite3.Connection,
    *,
    acao: str,
    entidade: str,
    entidade_id: int | None = None,
    ator_id: int | None = None,
    **detalhe: Any,
) -> int:
    """Grava um evento. Devolve o id.

    ``detalhe`` vira ``detalhe_json`` com ``ensure_ascii=False``: a trilha é
    lida por gente, e ``justificativa`` com ``\\u00e7`` no lugar de ``ç`` seria
    ilegível justamente no campo que importa.
    """
    cur = conn.execute(
        "INSERT INTO eventos (ator_id, acao, entidade, entidade_id, detalhe_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            ator_id,
            acao,
            entidade,
            entidade_id,
            json.dumps(detalhe, ensure_ascii=False, default=str),
        ),
    )
    return int(cur.lastrowid or 0)


def da_entidade(
    conn: sqlite3.Connection, entidade: str, entidade_id: int
) -> list[dict[str, Any]]:
    """A trilha de UMA coisa, em ordem cronológica.

    É o que o export de auditoria do P5b vai ler, e o que o painel do admin
    mostra ao abrir um item escalado.
    """
    linhas = conn.execute(
        "SELECT e.id, e.ator_id, a.nome AS ator, e.acao, e.entidade, e.entidade_id, "
        "       e.detalhe_json, e.criado_em "
        "FROM eventos e LEFT JOIN anotadores a ON a.id = e.ator_id "
        "WHERE e.entidade = ? AND e.entidade_id = ? ORDER BY e.id",
        (entidade, entidade_id),
    ).fetchall()
    saida: list[dict[str, Any]] = []
    for linha in linhas:
        try:
            detalhe = json.loads(str(linha["detalhe_json"]))
        except (TypeError, ValueError):  # pragma: no cover
            detalhe = {}
        saida.append(
            {
                "id": int(linha["id"]),
                "ator_id": None if linha["ator_id"] is None else int(linha["ator_id"]),
                "ator": linha["ator"],
                "acao": str(linha["acao"]),
                "entidade": str(linha["entidade"]),
                "entidade_id": None
                if linha["entidade_id"] is None
                else int(linha["entidade_id"]),
                "detalhe": detalhe,
                "criado_em": linha["criado_em"],
            }
        )
    return saida


__all__ = ["ACOES", "ENTIDADES", "da_entidade", "registrar"]
