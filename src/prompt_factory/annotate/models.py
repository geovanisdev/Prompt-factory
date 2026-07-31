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
from .db import (
    AVALIACOES_ANTES,
    AVALIACOES_DEPOIS,
    DECISOES_ADMIN,
    PAPEIS,
    TIPOS_TAREFA,
    VEREDITOS,
)

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


#: Teto do caminho de um campo dentro do payload (``notas.12.justificativa``).
#: Curto de propósito: um caminho de 200 caracteres não vem de um formulário,
#: vem de alguém montando payload à mão.
MAX_CAMPO = 200

#: Teto do motivo de UMA mudança. Ele é curto por desenho — a justificativa
#: longa é a geral, e um motivo de mudança que precisa de 2.000 caracteres é um
#: sinal de que a correção deveria ter sido uma devolução na triagem.
MAX_MOTIVO = 600


class EdicaoIn(BaseModel):
    """Uma linha do diff: o caminho, os dois valores e **o motivo daquela** mudança.

    ``motivo`` é obrigatório e tem piso de caracteres. Sem ele, o Rate and
    Review vira "o revisor mudou as notas" e ninguém consegue dizer se ele
    corrigiu um erro ou impôs o gosto dele — que é exatamente a diferença entre
    QC e opinião.

    ``valor_antes``/``valor_depois`` chegam do cliente e são **conferidos**
    contra o diff que o servidor calcula (``avaliacoes.diferencas``): eles
    existem no corpo para que a tela possa mandar o que mostrou, não para que o
    servidor acredite.
    """

    model_config = ConfigDict(extra="forbid")

    campo: Annotated[str, Field(min_length=1, max_length=MAX_CAMPO)]
    valor_antes: Annotated[str | None, Field(max_length=20_000)] = None
    valor_depois: Annotated[str | None, Field(max_length=20_000)] = None
    motivo: Annotated[str, Field(max_length=MAX_MOTIVO)]

    @field_validator("campo", "motivo", mode="before")
    @classmethod
    def _texto_limpo(cls, v: Any) -> Any:
        # `.strip()` ANTES do min_length: um motivo só de espaços passaria pelo
        # comprimento e gravaria uma justificativa em branco no CHECK do DDL —
        # que a recusaria com um IntegrityError sem nome de campo.
        return v.strip() if isinstance(v, str) else v

    @field_validator("motivo")
    @classmethod
    def _motivo_suficiente(cls, v: str) -> str:
        piso = int(_cfg("annotate", "min_chars_motivo_edicao", default=10))
        if len(v) < piso:
            raise ValueError(
                f"cada mudança precisa do próprio motivo, com ao menos {piso} caracteres "
                f"(tem {len(v)})"
            )
        return v


class AvaliarIn(BaseModel):
    """``POST /api/avaliacao/{anotacao_id}`` — a passagem 2 inteira, num corpo só.

    As duas escalas, o payload corrigido, o diff com um motivo por mudança e a
    justificativa geral. Tudo junto porque é **uma** decisão: gravar a escala
    antes e a correção depois deixaria um item medido e não corrigido no banco
    se a segunda chamada falhasse.

    ``payload_corrigido`` chega como dicionário CRU, pela mesma razão do
    ``SubmeterIn``: quem o valida é o modelo escolhido pelo **tipo da tarefa
    lido do banco**.
    """

    model_config = ConfigDict(extra="forbid")

    revisor_id: Annotated[int, Field(ge=1)]
    avaliacao_antes: str
    avaliacao_depois: str
    justificativa: Annotated[str, Field(max_length=4_000)]
    payload_corrigido: dict[str, Any] | None = None
    edicoes: Annotated[list[EdicaoIn], Field(max_length=200)] = []
    tempo_ativo_ms: Annotated[int, Field(ge=0, le=MAX_TEMPO_ATIVO_MS)] = 0

    @field_validator("revisor_id", "tempo_ativo_ms", mode="before")
    @classmethod
    def _nao_e_bool(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("este campo não é booleano")
        return v

    @field_validator("avaliacao_antes")
    @classmethod
    def _antes_conhecida(cls, v: str) -> str:
        # Validado contra a MESMA tupla que gerou o CHECK do DDL. `inutilizavel`
        # está lá de propósito, mesmo tendo passado na triagem: é o caso "a
        # triagem deixou passar", e registrá-lo é o ponto.
        if v not in AVALIACOES_ANTES:
            raise ValueError(f"avaliacao_antes fora de {list(AVALIACOES_ANTES)}: {v!r}")
        return v

    @field_validator("avaliacao_depois")
    @classmethod
    def _depois_conhecida(cls, v: str) -> str:
        if v not in AVALIACOES_DEPOIS:
            raise ValueError(f"avaliacao_depois fora de {list(AVALIACOES_DEPOIS)}: {v!r}")
        return v

    @field_validator("justificativa", mode="before")
    @classmethod
    def _justificativa_limpa(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v

    @field_validator("justificativa")
    @classmethod
    def _justificativa_suficiente(cls, v: str) -> str:
        # SEMPRE obrigatória — inclusive quando o revisor não mudou nada e achou
        # tudo excepcional. Uma avaliação sem motivo é um número que ninguém
        # consegue contestar nem aprender com.
        piso = int(_cfg("annotate", "min_chars_avaliacao", default=30))
        if len(v) < piso:
            raise ValueError(
                f"a avaliação precisa de uma justificativa de ao menos {piso} caracteres "
                f"(tem {len(v)}) — ela é o que o painel e o export mostram ao lado do escore"
            )
        return v

    @model_validator(mode="after")
    def _um_motivo_por_campo(self) -> AvaliarIn:
        campos = [e.campo for e in self.edicoes]
        if len(set(campos)) != len(campos):
            # Dois motivos para o mesmo caminho: a tela mandaria um só, e duas
            # linhas em `edicoes_avaliacao` para o mesmo campo fariam o diff da
            # auditoria contar a mesma mudança duas vezes.
            raise ValueError("o mesmo campo aparece duas vezes em `edicoes`")
        if self.edicoes and self.payload_corrigido is None:
            raise ValueError(
                "vieram motivos de mudança sem o payload corrigido — o servidor calcula "
                "o diff a partir dele, e sem ele não há mudança nenhuma a justificar"
            )
        return self


class DecisaoAdminIn(BaseModel):
    """``POST /api/escalacao/{avaliacao_id}`` — o admin encerra um item escalado.

    ``devolvida`` **é** permitida aqui, e não contradiz "só a triagem devolve":
    a regra existe para que a passagem 2 não jogue de volta, dias depois, um
    trabalho que ela mesma aprovou. O admin decidindo sobre um item que o
    revisor marcou como duvidoso é o caminho de escalação.
    """

    model_config = ConfigDict(extra="forbid")

    admin_id: Annotated[int, Field(ge=1)]
    decisao: str
    comentario: Annotated[str | None, Field(max_length=4_000)] = None

    @field_validator("admin_id", mode="before")
    @classmethod
    def _id_nao_e_bool(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("admin_id não é booleano")
        return v

    @field_validator("decisao")
    @classmethod
    def _decisao_conhecida(cls, v: str) -> str:
        if v not in DECISOES_ADMIN:
            raise ValueError(f"decisão fora de {list(DECISOES_ADMIN)}: {v!r}")
        return v

    @field_validator("comentario")
    @classmethod
    def _comentario_limpo(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.strip() or None

    @model_validator(mode="after")
    def _devolver_exige_motivo(self) -> DecisaoAdminIn:
        # Mesma regra da triagem, e pelo mesmo motivo: devolver sem dizer o que
        # corrigir devolve trabalho sem devolver informação. Aqui o comentário
        # atravessa dois saltos (admin → anotador) e é a única coisa que chega.
        if self.decisao != "devolvida":
            return self
        piso = int(_cfg("annotate", "min_chars_devolucao", default=20))
        if not self.comentario or len(self.comentario) < piso:
            raise ValueError(
                f"devolver pede um comentário de ao menos {piso} caracteres — ele volta "
                "para o anotador e é a única instrução que ele recebe"
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
    "MAX_CAMPO",
    "MAX_MOTIVO",
    "MAX_NOME",
    "MAX_TEMPO_ATIVO_MS",
    "AbandonarIn",
    "AvaliarIn",
    "DecisaoAdminIn",
    "EdicaoIn",
    "LivreIn",
    "PerfilIn",
    "ProximaIn",
    "SubmeterIn",
    "TriarIn",
    "perfil",
]
