"""``pf annotate seed`` — o mínimo para a plataforma ser navegável.

**No P1 isto semeia só as seis personas.** O pacote de demonstração (prompts à
mão, rubricas, respostas de modelo com defeito plantado, tarefas de avaliar e
comparar) é o P2, e vai morar em ``annotate/fixtures/demo_pack.json`` — dentro
do pacote, porque ``data/`` é gitignorado e uma fixture versionada é o que faz a
demonstração abrir igual em qualquer clone.

Idempotente por NOME: rodar duas vezes não duplica ninguém, e o
``ON CONFLICT DO NOTHING`` também não sobrescreve um papel que alguém tenha
mudado na tela. Semear é começar, não reinicializar.
"""

from __future__ import annotations

import sqlite3

#: As seis personas do plano: 3 anotadores, 2 revisores, 1 admin.
#:
#: Dois revisores existem por um motivo de produto, não de simetria: o revisor
#: **não pode revisar a própria anotação** (P3), então um revisor sozinho
#: travaria a fila assim que ele mesmo anotasse alguma coisa. Três anotadores
#: são o mínimo para uma tarefa com ``n_anotacoes_alvo = 2`` ainda deixar alguém
#: de fora e o agreement ter de quem discordar.
PERSONAS: tuple[tuple[str, str], ...] = (
    ("Ana Ribeiro", "anotador"),
    ("Bruno Tavares", "anotador"),
    ("Carla Nunes", "anotador"),
    ("Diego Prado", "revisor"),
    ("Elisa Marques", "revisor"),
    ("Marina Alves", "admin"),
)


def semear_personas(conn: sqlite3.Connection) -> int:
    """Insere as personas que faltam. Devolve quantas ENTRARAM de fato.

    A conta é a diferença do total, e não ``changes()``: depois de um
    ``executemany`` o ``changes()`` reporta só a última instrução.
    """
    antes = int(conn.execute("SELECT count(*) AS n FROM anotadores").fetchone()["n"])
    conn.executemany(
        "INSERT INTO anotadores (nome, papel) VALUES (?, ?) ON CONFLICT(nome) DO NOTHING",
        PERSONAS,
    )
    depois = int(conn.execute("SELECT count(*) AS n FROM anotadores").fetchone()["n"])
    return depois - antes


def tem_trabalho(conn: sqlite3.Connection) -> bool:
    """``True`` se já existe anotação submetida neste banco.

    O ``--force`` do seed (P2, que vai APAGAR e refazer as fixtures) consulta
    isto antes de destruir qualquer coisa: refazer o pacote de demonstração por
    cima de trabalho humano é a única operação verdadeiramente irreversível
    desta app.
    """
    linha = conn.execute("SELECT EXISTS(SELECT 1 FROM anotacoes) AS tem").fetchone()
    return bool(linha["tem"])


__all__ = ["PERSONAS", "semear_personas", "tem_trabalho"]
