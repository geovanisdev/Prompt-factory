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

import json
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..config import get as _cfg
from .db import PAPEIS, TIPOS_TAREFA, VEREDITOS

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
    """``POST /api/tarefas/proxima`` — me dá a próxima deste tipo.

    ``projeto_id`` recorta a fila a um projeto. É o que torna "trabalhar só no
    Portfólio" possível sem esbarrar nas tarefas do pacote de demonstração —
    e é opcional porque a fila sem recorte continua sendo o caminho de quem
    não se importa com a separação.
    """

    projeto_id: Annotated[int | None, Field(ge=1)] = None

    @field_validator("projeto_id", mode="before")
    @classmethod
    def _projeto_nao_e_bool(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("projeto_id não é booleano")
        return v


class LivreIn(_ComTipo):
    """``POST /api/tarefas/livre`` — quero anotar ESTE prompt (modo livre).

    ``origem`` distingue "escolhi no catálogo" de "aceitei a oferta de
    continuação depois de escrever a rubrica". As duas criam a mesma tarefa; o
    que muda é a proveniência, e ela é o que o painel do admin lê para dizer
    quanto do trabalho nasceu de escolha própria.
    """

    prompt_uid: Annotated[str, Field(min_length=4, max_length=200)]
    origem: str = "livre"
    #: Em qual projeto a tarefa nasce. Sem isso, escolher um prompt no catálogo
    #: enquanto se trabalha no Portfólio criaria a tarefa no projeto padrão de
    #: qualquer jeito — e o recorte que a barra promete não valeria para as
    #: tarefas criadas dentro dele.
    projeto_id: Annotated[int | None, Field(ge=1)] = None

    @field_validator("projeto_id", mode="before")
    @classmethod
    def _projeto_nao_e_bool(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("projeto_id não é booleano")
        return v

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


class TriarIn(BaseModel):
    """``POST /api/revisao/{id}`` — a passagem 1: aprovar ou devolver.

    ``comentario`` é **obrigatório na devolução** em três camadas: aqui (para o
    422 dizer o campo), na rota (que não deixa passar vazio depois do strip) e
    no CHECK do DDL (que é quem de fato garante). Três não é exagero: o
    comentário é a única informação que o anotador recebe sobre o que corrigir,
    e sem ele o re-trabalho sai igual ao primeiro.
    """

    model_config = ConfigDict(extra="forbid")

    revisor_id: Annotated[int, Field(ge=1)]
    veredito: str
    comentario: Annotated[str | None, Field(max_length=4_000)] = None

    @field_validator("revisor_id", mode="before")
    @classmethod
    def _id_nao_e_bool(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("revisor_id não é booleano")
        return v

    @field_validator("veredito")
    @classmethod
    def _veredito_conhecido(cls, v: str) -> str:
        if v not in VEREDITOS:
            raise ValueError(f"veredito fora de {list(VEREDITOS)}: {v!r}")
        return v

    @field_validator("comentario")
    @classmethod
    def _comentario_limpo(cls, v: str | None) -> str | None:
        if v is None:
            return None
        limpo = v.strip()
        return limpo or None

    @model_validator(mode="after")
    def _devolver_exige_motivo(self) -> TriarIn:
        if self.veredito != "devolvida":
            return self
        piso = int(_cfg("annotate", "min_chars_devolucao", default=20))
        if not self.comentario or len(self.comentario) < piso:
            raise ValueError(
                f"a devolução precisa de um comentário de ao menos {piso} caracteres — "
                "ele volta para o anotador e é a única instrução que ele recebe"
            )
        return self


def perfil(linha: Any) -> dict[str, Any]:
    """Linha de ``anotadores`` -> dict JSON.

    ``ativo`` sai como booleano de verdade (o SQLite guarda 0/1): a interface
    escreve ``if (p.ativo)`` e precisa que isso signifique o que parece.

    ``qualificacoes`` sai como dicionário. A coluna existe e é escrita desde o
    P3a (o seed a preenche); **barrar** quem não é qualificado fica para quando
    a tela existir — mas o dado começa a ser gravado hoje, que é o ponto de
    fechar o schema antes de mais trabalho humano entrar.
    """
    try:
        quali = json.loads(str(linha["qualificacoes_json"] or "{}"))
    except (IndexError, KeyError, TypeError, ValueError):  # pragma: no cover
        quali = {}
    return {
        "id": int(linha["id"]),
        "nome": str(linha["nome"]),
        "papel": str(linha["papel"]),
        "ativo": bool(linha["ativo"]),
        "qualificacoes": quali if isinstance(quali, dict) else {},
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
    "TriarIn",
    "perfil",
]
