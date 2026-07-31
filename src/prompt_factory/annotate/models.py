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

from .db import PAPEIS, TIPOS_TAREFA

#: Comprimento máximo de um nome de perfil. Não é estética: o nome é o
#: identificador da pessoa em toda rota e aparece em tabela no painel do admin.
MAX_NOME = 60

#: Teto do tempo ativo que o cliente pode declarar numa submissão: 24 h em ms.
#: O número vem do relógio do NAVEGADOR, então é dado de cliente e precisa de
#: limite — uma aba esquecida aberta no fim de semana mandaria 200 horas e
#: envenenaria a mediana de tempo por tipo do painel do admin.
MAX_TEMPO_ATIVO_MS = 24 * 60 * 60 * 1000


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


class _ComAnotador(BaseModel):
    """Base das rotas de trabalho: quem está pedindo.

    ``anotador_id`` é a IDENTIDADE, e ela vem do cliente porque não há sessão —
    é uma demonstração sem login, e isso está escrito na tela. O **papel**, ao
    contrário, nunca vem daqui: quem o lê é ``deps.exigir_papel``, na linha de
    ``anotadores``. Identidade escolhida na tela, autorização no servidor.
    """

    model_config = ConfigDict(extra="forbid")

    anotador_id: Annotated[int, Field(ge=1)]

    @field_validator("anotador_id", mode="before")
    @classmethod
    def _id_nao_e_bool(cls, v: Any) -> Any:
        # `bool` é subclasse de `int`: `{"anotador_id": true}` viraria o perfil
        # de id 1 em silêncio — que, num banco recém-semeado, é uma pessoa real.
        if isinstance(v, bool):
            raise ValueError("anotador_id não é booleano")
        return v


class _ComTipo(_ComAnotador):
    """Mais o estilo de tarefa, validado contra a MESMA tupla que gerou o CHECK."""

    tipo: str

    @field_validator("tipo")
    @classmethod
    def _tipo_conhecido(cls, v: str) -> str:
        if v not in TIPOS_TAREFA:
            raise ValueError(f"tipo fora de {list(TIPOS_TAREFA)}: {v!r}")
        return v


class ProximaIn(_ComTipo):
    """``POST /api/tarefas/proxima`` — me dá a próxima deste tipo."""


class LivreIn(_ComTipo):
    """``POST /api/tarefas/livre`` — quero anotar ESTE prompt (modo livre).

    ``origem`` distingue "escolhi no catálogo" de "aceitei a oferta de
    continuação depois de escrever a rubrica". As duas criam a mesma tarefa; o
    que muda é a proveniência, e ela é o que o painel do admin lê para dizer
    quanto do trabalho nasceu de escolha própria.
    """

    prompt_uid: Annotated[str, Field(min_length=4, max_length=200)]
    origem: str = "livre"

    @field_validator("origem")
    @classmethod
    def _origem_permitida(cls, v: str) -> str:
        # `semente` e `admin` NÃO entram: elas são criadas pelo seed e pelo
        # painel, e deixar o cliente carimbar uma tarefa como "da semente"
        # falsificaria a origem do trabalho no relatório.
        if v not in ("livre", "continuacao"):
            raise ValueError("origem só pode ser 'livre' ou 'continuacao' por esta rota")
        return v


class SubmeterIn(_ComAnotador):
    """``POST /api/atribuicoes/{id}/submeter``.

    ``payload`` chega como dicionário CRU de propósito: quem o valida é o modelo
    escolhido pelo **tipo da tarefa lido do banco**, e não um campo do corpo. Um
    cliente que declarasse o próprio tipo poderia gravar um payload válido no
    schema errado, e o defeito só apareceria no export.
    """

    payload: dict[str, Any]
    tempo_ativo_ms: Annotated[int, Field(ge=0, le=MAX_TEMPO_ATIVO_MS)] = 0

    @field_validator("tempo_ativo_ms", mode="before")
    @classmethod
    def _tempo_nao_e_bool(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("tempo_ativo_ms não é booleano")
        return v


class AbandonarIn(_ComAnotador):
    """``POST /api/atribuicoes/{id}/abandonar`` — devolve a vaga na hora."""


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


__all__ = [
    "MAX_NOME",
    "MAX_TEMPO_ATIVO_MS",
    "AbandonarIn",
    "LivreIn",
    "PerfilIn",
    "ProximaIn",
    "SubmeterIn",
    "perfil",
]
