"""Escopo de projeto/campanha — e a separação entre demonstração e trabalho real.

Duas razões para isto existir, e a segunda foi a que o tornou urgente:

1. **Multi-cliente.** Uma plataforma que serve um cliente não precisa de escopo;
   uma que serve dois, sim — e acrescentá-lo depois significa migrar um banco
   cheio de trabalho humano, decidindo a que projeto pertence cada tarefa já
   anotada sem ninguém para perguntar.
2. **O autor vai trabalhar aqui de verdade.** As anotações que um avaliador vai
   ler — as justificativas, as rubricas, as respostas de referência — são as que
   o dono escreveu, não as das fixtures. Misturar as duas coisas na mesma lista
   apagaria justamente a distinção que dá valor a uma delas.

Daí os DOIS projetos padrão:

* **``Demonstração``** — o pacote de fixtures. Prompts escritos à mão, rubricas
  de exemplo e respostas com defeito plantado. Serve para mostrar a mecânica.
* **``Portfólio``** — o trabalho real sobre o corpus. É o default de toda tarefa
  nova, porque é onde o trabalho de verdade acontece.

Os nomes moram em ``[annotate] projeto_padrao`` e ``projeto_demonstracao``.
A tela para criar OUTROS projetos vem depois; o seletor para trocar entre os
que existem já está na barra, porque sem ele "trabalhar só num projeto" seria
uma promessa do schema que a interface não cumpre.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..config import get as _cfg


def nome_padrao() -> str:
    """``[annotate] projeto_padrao`` — onde nasce toda tarefa nova."""
    return str(_cfg("annotate", "projeto_padrao", default="Portfólio"))


def nome_demonstracao() -> str:
    """``[annotate] projeto_demonstracao`` — onde vive o pacote de fixtures."""
    return str(_cfg("annotate", "projeto_demonstracao", default="Demonstração"))


#: O que cada projeto padrão é, em uma linha — vai para a coluna ``descricao`` e
#: aparece no painel do admin. Um projeto sem descrição, numa lista de dois,
#: obriga quem olha a adivinhar qual é qual.
DESCRICOES: dict[str, tuple[str, str]] = {
    "padrao": (
        "autoral",
        "Trabalho real sobre o corpus. É daqui que sai o material do portfólio: "
        "as rubricas, as justificativas e as respostas de referência escritas de "
        "verdade. Toda tarefa nova nasce neste projeto.",
    ),
    "demonstracao": (
        "interno",
        "Pacote de demonstração: prompts escritos à mão, rubricas de exemplo e "
        "respostas com defeito plantado. Serve para mostrar a mecânica da "
        "plataforma, não para compor o portfólio.",
    ),
}


def _garantir(conn: sqlite3.Connection, nome: str, papel: str) -> int:
    """``ON CONFLICT DO NOTHING`` + ``SELECT``, e não ``SELECT`` antes do INSERT.

    Com duas abas abertas na demonstração, duas requisições podem chegar aqui
    juntas — e o UNIQUE do DDL é quem resolve, não uma checagem prévia que
    perderia a corrida.
    """
    cliente, descricao = DESCRICOES[papel]
    conn.execute(
        "INSERT INTO projetos (nome, cliente, descricao, status) VALUES (?, ?, ?, 'ativo') "
        "ON CONFLICT(nome) DO NOTHING",
        (nome, cliente, descricao),
    )
    return int(
        conn.execute("SELECT id FROM projetos WHERE nome = ?", (nome,)).fetchone()["id"]
    )


def garantir(conn: sqlite3.Connection, nome: str | None = None) -> int:
    """O id do projeto padrão (``Portfólio``), criando-o se faltar."""
    if nome is None or nome == nome_padrao():
        return _garantir(conn, nome_padrao(), "padrao")
    if nome == nome_demonstracao():
        return _garantir(conn, nome_demonstracao(), "demonstracao")
    # Um projeto de nome livre (um cliente novo) nasce com a descrição do
    # padrão: é melhor uma descrição genérica que uma coluna vazia.
    return _garantir(conn, nome, "padrao")


def garantir_demonstracao(conn: sqlite3.Connection) -> int:
    """O id do projeto do pacote de fixtures."""
    return _garantir(conn, nome_demonstracao(), "demonstracao")


def garantir_padroes(conn: sqlite3.Connection) -> dict[str, int]:
    """Os dois de uma vez: ``{"padrao": id, "demonstracao": id}``."""
    return {
        "padrao": garantir(conn),
        "demonstracao": garantir_demonstracao(conn),
    }


def listar(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Os projetos com a contagem de tarefas — é o que o seletor da barra mostra.

    A contagem entra aqui (e não numa segunda rota) porque um seletor que não
    diz quanto trabalho tem cada projeto obriga a entrar em todos para
    descobrir onde está o que se procura.
    """
    linhas = conn.execute(
        "SELECT p.id, p.nome, p.cliente, p.descricao, p.status, p.criado_em, "
        "       (SELECT count(*) FROM tarefas t WHERE t.projeto_id = p.id) AS n_tarefas, "
        "       (SELECT count(*) FROM tarefas t WHERE t.projeto_id = p.id "
        "        AND t.status = 'aberta') AS n_abertas "
        "FROM projetos p ORDER BY p.id"
    ).fetchall()
    return [
        {
            "id": int(linha["id"]),
            "nome": str(linha["nome"]),
            "cliente": str(linha["cliente"]),
            "descricao": str(linha["descricao"]),
            "status": str(linha["status"]),
            "criado_em": linha["criado_em"],
            "n_tarefas": int(linha["n_tarefas"]),
            "n_abertas": int(linha["n_abertas"]),
        }
        for linha in linhas
    ]


def existe(conn: sqlite3.Connection, projeto_id: int) -> bool:
    return (
        conn.execute("SELECT 1 FROM projetos WHERE id = ?", (projeto_id,)).fetchone()
        is not None
    )


__all__ = [
    "DESCRICOES",
    "existe",
    "garantir",
    "garantir_demonstracao",
    "garantir_padroes",
    "listar",
    "nome_demonstracao",
    "nome_padrao",
]
