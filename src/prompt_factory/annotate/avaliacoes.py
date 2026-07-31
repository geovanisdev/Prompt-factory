"""PASSAGEM 2 — o Rate and Review: a fila, o diff campo a campo e a escalação.

A triagem (``routes_revisao``) aprova ou devolve. Aqui o revisor faz outra
coisa: ele **mede** o trabalho em duas escalas e **conserta no lugar** o que dá
para consertar. O trabalho não volta ao anotador nesta passagem — quem conserta
é quem avalia, e é isso que a torna cara e informativa ao mesmo tempo.

POR QUE O DIFF É CALCULADO NO SERVIDOR
======================================
O cliente manda o payload corrigido e uma lista de motivos, um por caminho
alterado. Se o servidor confiasse nessa lista, três coisas silenciosas
aconteceriam:

1. uma alteração **sem** motivo entraria — e ``edicoes_avaliacao`` existe
   justamente para que ninguém possa dizer "o revisor mudou as notas" sem saber
   se ele corrigiu um erro ou impôs o gosto dele;
2. um motivo **sobre um campo que não mudou** entraria, e a auditoria passaria
   a descrever uma correção que não houve;
3. os caminhos gravados sairiam da imaginação do JS, e não do payload.

Então o servidor achata os dois payloads (``achatar``), compara e exige
**igualdade exata** entre o conjunto de caminhos alterados e o conjunto de
motivos declarados. Discordou, é 422 — com os dois lados na mensagem.

O CAMINHO É O ENDEREÇO DENTRO DO JSON
=====================================
``notas.2.nota``, ``criterios.0.rotulo_min``, ``justificativa``. É o que o DDL
de ``edicoes_avaliacao`` chama de ``campo``, e não um nome de coluna: o payload
é JSON versionado (``comparar_ab@1``), e o caminho é a única referência estável
dentro dele. Um índice de lista faz parte do endereço — trocar a nota do
terceiro critério e trocar a do primeiro são fatos diferentes.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import db as adb
from . import solo as solomod

#: O separador dos segmentos de um caminho. Um ponto, como em ``notas.2.nota``:
#: é a notação que o próprio plano usa e a que qualquer pessoa lê sem legenda.
SEPARADOR = "."

#: Quanto texto de um valor entra em ``edicoes_avaliacao.valor_antes/depois``.
#: A tabela é o resumo auditável da mudança, não uma segunda cópia do payload —
#: quem guarda o texto inteiro é ``avaliacoes.payload_corrigido_json``. Sem o
#: corte, corrigir uma resposta de SFT de 40.000 caracteres gravaria 80.000 numa
#: linha que o painel do admin mostra numa tabela.
MAX_VALOR = 2_000


def _folha(valor: Any) -> bool:
    """``True`` quando o valor não se decompõe mais (não é dict nem lista)."""
    return not isinstance(valor, (dict, list))


def achatar(valor: Any, prefixo: str = "") -> dict[str, Any]:
    """``{"notas": [{"nota": 3}]}`` → ``{"notas.0.nota": 3}``.

    Só as FOLHAS entram: um dicionário intermediário não é um campo que alguém
    edita, e listá-lo faria "mudei a nota do critério 2" aparecer três vezes
    (``notas``, ``notas.2`` e ``notas.2.nota``) pedindo três motivos.

    Uma lista ou dicionário **vazio** é folha, porque é o valor final ali: sem
    isso, apagar o último item de ``por_criterio`` não apareceria como mudança
    nenhuma.
    """
    if _folha(valor) or (isinstance(valor, (dict, list)) and not valor):
        return {prefixo: valor} if prefixo else {}
    itens = valor.items() if isinstance(valor, dict) else enumerate(valor)
    plano: dict[str, Any] = {}
    for chave, sub in itens:
        caminho = f"{prefixo}{SEPARADOR}{chave}" if prefixo else str(chave)
        plano.update(achatar(sub, caminho))
    return plano


def como_texto(valor: Any) -> str | None:
    """O valor de uma folha como TEXT, para ``edicoes_avaliacao``.

    ``None`` vira NULL (e não a string ``"None"``): a diferença entre "não tem
    justificativa" e "a justificativa é a palavra None" precisa sobreviver ao
    export. Número e booleano viram JSON — ``true``, e não ``True``, porque quem
    lê a trilha lê JSON no resto do banco inteiro.
    """
    if valor is None:
        return None
    texto = valor if isinstance(valor, str) else json.dumps(valor, ensure_ascii=False)
    return texto[:MAX_VALOR]


def diferencas(antes: Any, depois: Any) -> list[dict[str, Any]]:
    """Os caminhos que mudaram, em ordem estável, com os dois valores.

    Ordem estável = a ordem em que os caminhos aparecem no payload ORIGINAL,
    seguida dos que só existem no corrigido. É o que faz o resumo do diff na
    tela e as linhas de ``edicoes_avaliacao`` saírem na mesma sequência do
    formulário — um diff em ordem alfabética misturaria o critério 10 com o 1.
    """
    plano_antes = achatar(antes)
    plano_depois = achatar(depois)
    ordem = list(plano_antes) + [c for c in plano_depois if c not in plano_antes]
    ausente = object()
    saida: list[dict[str, Any]] = []
    for caminho in ordem:
        a = plano_antes.get(caminho, ausente)
        b = plano_depois.get(caminho, ausente)
        if a == b:
            continue
        saida.append(
            {
                "campo": caminho,
                "valor_antes": None if a is ausente else a,
                "valor_depois": None if b is ausente else b,
            }
        )
    return saida


# ---------------------------------------------------------------------------
# a fila da passagem 2
# ---------------------------------------------------------------------------

#: A fila do Rate and Review. É a irmã de ``routes_revisao._SQL_FILA_BASE`` e
#: difere dela em UMA palavra (o status) — mas mora aqui, e não lá, porque a
#: passagem 2 traz a triagem junto: quem avalia precisa saber quem aprovou, e é
#: dessa junção que nasce a métrica "a triagem deixou passar" do P5a.
_SQL_FILA_BASE = (
    "SELECT an.id, an.versao, an.submetida_em, an.payload_schema, an.status, "
    "       a.id AS atribuicao_id, a.anotador_id, au.nome AS anotador, "
    "       t.id AS tarefa_id, t.tipo, t.origem, t.prompt_uid, "
    "       t.projeto_id, pr.nome AS projeto, "
    "       r.revisor_id AS triador_id, rv.nome AS triador, r.criada_em AS triada_em "
    "FROM anotacoes an "
    "JOIN atribuicoes a ON a.id = an.atribuicao_id "
    "JOIN anotadores au ON au.id = a.anotador_id "
    "JOIN tarefas t ON t.id = a.tarefa_id "
    "LEFT JOIN projetos pr ON pr.id = t.projeto_id "
    "LEFT JOIN revisoes r ON r.anotacao_id = an.id "
    "LEFT JOIN anotadores rv ON rv.id = r.revisor_id "
    "WHERE an.status = 'pendente_avaliacao'"
)


def sql_fila(*, com_projeto: bool) -> str:
    """A consulta da fila, com o recorte de projeto e a regra do modo solo."""
    sql = _SQL_FILA_BASE
    if not solomod.ligado():
        # A MESMA regra da triagem, e pelo mesmo motivo: com uma pessoa só
        # avaliando o que ela mesma escreveu, a distância entre as duas escalas
        # deixa de medir qualquer coisa. O modo solo a substitui por uma regra
        # que se declara (faixa na tela + `avaliacoes.autorrevisao`).
        sql += " AND a.anotador_id <> :eu"
    if com_projeto:
        sql += " AND t.projeto_id = :projeto"
    # Cronológica: quem esperou mais é atendido primeiro.
    return sql + " ORDER BY an.id"


def minhas_esperando(conn: sqlite3.Connection, revisor_id: int) -> int:
    """Quantas anotações DESTA pessoa estão na fila da passagem 2.

    Sem este número, quem anotou e vê a fila vazia acha que a plataforma perdeu
    o trabalho dele.
    """
    return int(
        conn.execute(
            "SELECT count(*) AS n FROM anotacoes an "
            "JOIN atribuicoes a ON a.id = an.atribuicao_id "
            "WHERE an.status = 'pendente_avaliacao' AND a.anotador_id = ?",
            (revisor_id,),
        ).fetchone()["n"]
    )


# ---------------------------------------------------------------------------
# a fila do admin (escalação)
# ---------------------------------------------------------------------------

#: Os itens que o revisor marcou ``borderline_admin`` e que **ninguém decidiu
#: ainda**. O ``LEFT JOIN ... IS NULL`` é o que faz a fila esvaziar sozinha: a
#: decisão é ``UNIQUE(avaliacao_id)``, então uma linha em ``decisoes_admin`` é,
#: por construção, o item fora da fila.
SQL_ESCALADOS = (
    "SELECT av.id AS avaliacao_id, av.anotacao_id, av.avaliacao_antes, av.avaliacao_depois, "
    "       av.justificativa, av.autorrevisao, av.criada_em, av.revisor_id, "
    "       rev.nome AS revisor, "
    "       an.versao, an.payload_schema, an.status, an.submetida_em, "
    "       a.id AS atribuicao_id, a.anotador_id, au.nome AS anotador, "
    "       t.id AS tarefa_id, t.tipo, t.prompt_uid, t.projeto_id, pr.nome AS projeto "
    "FROM avaliacoes av "
    "JOIN anotacoes an ON an.id = av.anotacao_id "
    "JOIN atribuicoes a ON a.id = an.atribuicao_id "
    "JOIN anotadores au ON au.id = a.anotador_id "
    "JOIN anotadores rev ON rev.id = av.revisor_id "
    "JOIN tarefas t ON t.id = a.tarefa_id "
    "LEFT JOIN projetos pr ON pr.id = t.projeto_id "
    "LEFT JOIN decisoes_admin d ON d.avaliacao_id = av.id "
    "WHERE av.avaliacao_depois = 'borderline_admin' AND d.id IS NULL "
    "ORDER BY av.id"
)


def edicoes(conn: sqlite3.Connection, avaliacao_id: int) -> list[dict[str, Any]]:
    """O diff gravado de uma avaliação, na ordem em que foi registrado.

    É o que o admin lê para decidir e o que o export de auditoria do P5b junta à
    cadeia do item.
    """
    linhas = conn.execute(
        "SELECT campo, valor_antes, valor_depois, motivo FROM edicoes_avaliacao "
        "WHERE avaliacao_id = ? ORDER BY id",
        (avaliacao_id,),
    ).fetchall()
    return [
        {
            "campo": str(linha["campo"]),
            "valor_antes": linha["valor_antes"],
            "valor_depois": linha["valor_depois"],
            "motivo": str(linha["motivo"]),
        }
        for linha in linhas
    ]


def parecer(conn: sqlite3.Connection, anotacao_id: int) -> dict[str, Any] | None:
    """A avaliação de uma anotação (com o diff), ou ``None`` se não foi avaliada.

    Sai no envelope do admin e, no P5a, no painel. **Não** sai para o anotador:
    a passagem 2 não devolve trabalho, e mandar o parecer para quem não pode
    agir sobre ele seria só ruído.
    """
    linha = conn.execute(
        "SELECT av.id, av.revisor_id, rev.nome AS revisor, av.avaliacao_antes, "
        "       av.avaliacao_depois, av.justificativa, av.payload_corrigido_json, "
        "       av.autorrevisao, av.tempo_ativo_ms, av.criada_em "
        "FROM avaliacoes av JOIN anotadores rev ON rev.id = av.revisor_id "
        "WHERE av.anotacao_id = ?",
        (anotacao_id,),
    ).fetchone()
    if linha is None:
        return None
    bruto = linha["payload_corrigido_json"]
    try:
        corrigido = None if bruto is None else json.loads(str(bruto))
    except (TypeError, ValueError):  # pragma: no cover - gravado por nós, sempre válido
        corrigido = None
    return {
        "id": int(linha["id"]),
        "revisor_id": int(linha["revisor_id"]),
        "revisor": str(linha["revisor"]),
        "avaliacao_antes": str(linha["avaliacao_antes"]),
        "avaliacao_depois": str(linha["avaliacao_depois"]),
        "justificativa": str(linha["justificativa"]),
        "payload_corrigido": corrigido,
        "autorrevisao": bool(linha["autorrevisao"]),
        "tempo_ativo_ms": int(linha["tempo_ativo_ms"]),
        "criada_em": linha["criada_em"],
        "edicoes": edicoes(conn, int(linha["id"])),
    }


def desfecho(avaliacao_depois: str) -> str:
    """A escala 2 → o status da anotação. Ver ``db.DESFECHO_AVALIACAO``."""
    try:
        return adb.DESFECHO_AVALIACAO[avaliacao_depois]
    except KeyError as exc:  # pragma: no cover - o Pydantic já barra
        raise ValueError(f"avaliacao_depois desconhecida: {avaliacao_depois!r}") from exc


__all__ = [
    "MAX_VALOR",
    "SEPARADOR",
    "SQL_ESCALADOS",
    "achatar",
    "como_texto",
    "desfecho",
    "diferencas",
    "edicoes",
    "minhas_esperando",
    "parecer",
    "sql_fila",
]
