"""Modelos Pydantic da Bancada (P1: só o que a casca precisa).

``extra="forbid"`` em tudo, pela mesma razão do ``app/models.py``: um campo
digitado errado tem de virar **422 com o nome do campo**, nunca ser ignorado em
silêncio. Numa plataforma de anotação isso é mais grave do que numa listagem —
um payload aceito pela metade grava trabalho humano incompleto e ninguém
descobre até a hora de exportar.

Os vocabulários (``papel``, tipos de tarefa, ...) vêm de ``annotate/db.py``, que
é onde os CHECKs do DDL os declaram. Uma segunda cópia aqui divergiria da
primeira, e a que diverge é sempre a que não está no banco.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import PAPEIS

#: Comprimento máximo de um nome de perfil. Não é estética: o nome é o
#: identificador da pessoa em toda rota e aparece em tabela no painel do admin.
MAX_NOME = 60


class PerfilIn(BaseModel):
    """Corpo do ``POST /api/perfis`` — criar uma persona.

    É o "entrar com outro nome" da tela de entrada: quem abre a demonstração
    pode se cadastrar em vez de vestir uma das personas semeadas. Sem senha,
    sem e-mail, sem confirmação: uma decisão nessa tela e mais nada.
    """

    model_config = ConfigDict(extra="forbid")

    nome: Annotated[str, Field(min_length=1, max_length=MAX_NOME)]
    papel: str

    @field_validator("nome")
    @classmethod
    def _nome_limpo(cls, v: str) -> str:
        # Espaço nas pontas transformaria "Ana" e "Ana " em duas pessoas
        # diferentes, e o UNIQUE do banco não as veria como iguais.
        limpo = " ".join(v.split())
        if not limpo:
            raise ValueError("o nome não pode ser só espaço")
        return limpo

    @field_validator("papel")
    @classmethod
    def _papel_conhecido(cls, v: str) -> str:
        # Validado contra a MESMA tupla que gerou o CHECK do DDL. Sem isto, um
        # papel inventado só apareceria como IntegrityError sem nome de campo.
        if v not in PAPEIS:
            raise ValueError(f"papel fora de {list(PAPEIS)}: {v!r}")
        return v


def perfil(linha: Any) -> dict[str, Any]:
    """Linha de ``anotadores`` -> dict JSON.

    ``ativo`` sai como booleano de verdade (o SQLite guarda 0/1): a interface
    escreve ``if (p.ativo)`` e precisa que isso signifique o que parece.
    """
    return {
        "id": int(linha["id"]),
        "nome": str(linha["nome"]),
        "papel": str(linha["papel"]),
        "ativo": bool(linha["ativo"]),
        "criado_em": linha["criado_em"],
    }


__all__ = ["MAX_NOME", "PerfilIn", "perfil"]
