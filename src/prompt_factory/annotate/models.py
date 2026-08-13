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
    MOTIVOS_ABANDONO,
    PAPEIS,
    PAPEIS_PEDIDO,
    ROTULOS_DUELO,
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
    """``POST /api/atribuicoes/{id}/abandonar`` — devolve a vaga na hora.

    ``motivo`` é o SKIP CATEGORIZADO do brief v2 ("pick the reason from the
    list. One click, no essay"). Opcional no contrato, e isso é decisão: o
    abandono também serve a quem largou uma tarefa do catálogo ou mudou de ideia
    — gestos que não são "não sei julgar isto" e não devem fingir que são. A
    tela da FILA sempre manda um; a API não obriga.

    Quando vem, é validado contra ``db.MOTIVOS_ABANDONO`` — a lista é fechada
    porque o brief a promete fechada, e um campo livre viraria justamente o
    pedágio que a regra existe para não ter.
    """

    motivo: str | None = None

    @field_validator("motivo")
    @classmethod
    def _motivo_do_catalogo(cls, v: str | None) -> str | None:
        if v is not None and v not in MOTIVOS_ABANDONO:
            raise ValueError(
                f"motivo de skip desconhecido: {v!r} — os válidos são "
                f"{', '.join(MOTIVOS_ABANDONO)}"
            )
        return v


#: Teto de um turno escrito pelo anotador. Generoso: um turno de red-team
#: costuma ser uma colagem longa, e o teto existe contra o paste acidental de
#: 1 MB, não contra quem escreve muito.
MAX_TURNO = 20_000


class TurnoIn(_ComAnotador):
    """``POST /api/conversa/{id}/turno`` — mandar um turno ao modelo.

    ``texto`` é **opcional de propósito**: ausente, a rota entende "tente gerar
    de novo o turno que ficou sem resposta". O turno do humano é gravado antes
    da chamada ao Ollama, então um timeout de 180 s deixa a conversa num estado
    nomeado em vez de custar o parágrafo que a pessoa escreveu — e obrigá-la a
    redigitá-lo seria cobrar dela o preço do relógio.
    """

    texto: Annotated[str | None, Field(max_length=MAX_TURNO)] = None

    @field_validator("texto")
    @classmethod
    def _texto_limpo(cls, v: str | None) -> str | None:
        if v is None:
            return None
        limpo = v.strip()
        if not limpo:
            # `""` chegaria aqui de um campo em branco enviado por engano, e
            # viraria um turno vazio mandado ao modelo. `None` significa
            # "regerar", e são coisas diferentes: recusar é a única leitura
            # honesta de um envio em branco.
            raise ValueError("um turno vazio não vai para o modelo — escreva alguma coisa")
        return limpo


class EscolherRodadaIn(_ComAnotador):
    """``POST /api/conversa/{id}/escolher`` — a vencedora de uma rodada do duelo.

    A justificativa é obrigatória e tem o mesmo piso do A/B, pelo mesmo motivo:
    o valor de uma preferência está no motivo, não na preferência. Aqui vale
    ainda mais — é **por rodada**, e "escolhi B pela concisão na rodada 1 e A na
    rodada 3 porque B esqueceu a restrição" são dois sinais que uma justificativa
    única fundiria num só.
    """

    ordem: Annotated[int, Field(ge=0)]
    rotulo: str
    justificativa: Annotated[str, Field(max_length=4_000)]

    @field_validator("ordem", mode="before")
    @classmethod
    def _ordem_nao_e_bool(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("ordem não é booleano")
        return v

    @field_validator("rotulo")
    @classmethod
    def _rotulo_conhecido(cls, v: str) -> str:
        if v not in ROTULOS_DUELO:
            raise ValueError(f"rótulo fora de {list(ROTULOS_DUELO)}: {v!r}")
        return v

    @field_validator("justificativa", mode="before")
    @classmethod
    def _justificativa_limpa(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v

    @field_validator("justificativa")
    @classmethod
    def _justificativa_suficiente(cls, v: str) -> str:
        piso = int(_cfg("annotate", "min_chars_justificativa", default=30))
        if len(v) < piso:
            raise ValueError(
                f"a escolha precisa de ao menos {piso} caracteres de motivo "
                f"(tem {len(v)}) — o valor da preferência está no motivo"
            )
        return v


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


#: Teto de um lote de geração de tarefas. Não é medo do INSERT: é o tamanho do
#: erro que um dedo escorregado produz. Gerar 20 tarefas de menos custa um
#: clique; gerar 5.000 enche a fila de todo mundo com trabalho que ninguém pediu
#: e a única saída é apagar tarefa à mão de um banco que guarda trabalho humano.
MAX_GERACAO = 200

#: Teto de anotações independentes por tarefa. 5 é folgado para a plataforma
#: real (o seed usa 1 e 2): passar disso significa pedir a seis pessoas o mesmo
#: item numa equipe de três anotadores — uma fila que nunca fecha.
MAX_ALVO_GERACAO = 5


class GerarTarefasIn(BaseModel):
    """``/api/admin/tarefas`` (e a prévia) — o admin enche a fila em lote.

    O mesmo corpo serve à PRÉVIA e à criação, e isso é a decisão: um formulário
    que estima com um conjunto de parâmetros e cria com outro é um formulário
    que mente. A prévia é ``GET`` (não escreve nada), a criação é ``POST``, e o
    plano que as duas executam é a mesma função.

    ``lang``/``fonte`` são recorte do POOL, não do corpus: a rota parte sempre
    dos uids que ``tarefas.uids_do_pool`` oferece, na ordem determinística deles.
    Um filtro que alcançasse o corpus inteiro seria a porta lateral que o pool
    existe para fechar (NSFW, robô repetido, prompt de 1 MB, licença fechada).
    """

    model_config = ConfigDict(extra="forbid")

    admin_id: Annotated[int, Field(ge=1)]
    tipo: str
    n: Annotated[int, Field(ge=1, le=MAX_GERACAO)] = 20
    lang: Annotated[str | None, Field(max_length=16)] = None
    fonte: Annotated[str | None, Field(max_length=80)] = None
    n_anotacoes_alvo: Annotated[int, Field(ge=1, le=MAX_ALVO_GERACAO)] = 1
    #: Entre a do pool (40) e a do pacote de demonstração (60 a 90): tarefa
    #: gerada pelo admin passa na frente do fallback do seed e continua atrás
    #: das fixtures escritas à mão, que são o primeiro ato da demonstração.
    prioridade: Annotated[int, Field(ge=0, le=100)] = 50
    projeto_id: Annotated[int | None, Field(ge=1)] = None

    @field_validator(
        "admin_id", "n", "n_anotacoes_alvo", "prioridade", "projeto_id", mode="before"
    )
    @classmethod
    def _nao_e_bool(cls, v: Any) -> Any:
        # Quarta vez neste repositório: `bool` é subclasse de `int`, e o Pydantic
        # coage `true` para `1` ANTES de qualquer validador comum. `{"n": true}`
        # geraria uma tarefa só, em silêncio, com cara de sucesso.
        if isinstance(v, bool):
            raise ValueError("este campo não é booleano")
        return v

    @field_validator("lang", "fonte", mode="before")
    @classmethod
    def _vazio_e_ausencia(cls, v: Any) -> Any:
        # O formulário manda `""` quando o campo não foi preenchido, e `""` num
        # `WHERE lang = ?` casaria zero linhas — uma prévia de "0 tarefas" que
        # parece falta de material e é campo em branco.
        if isinstance(v, str) and not v.strip():
            return None
        return v.strip() if isinstance(v, str) else v

    @field_validator("tipo")
    @classmethod
    def _tipo_conhecido(cls, v: str) -> str:
        if v not in TIPOS_TAREFA:
            raise ValueError(f"tipo fora de {list(TIPOS_TAREFA)}: {v!r}")
        return v


#: Teto do texto de um prompt criado na plataforma. É o mesmo teto que o pool
#: aplica ao que ele OFERECE (``pool_fallback_max_chars``): um prompt que a
#: plataforma não mostraria a ninguém não deveria poder nascer nela.
MAX_TEXTO_CRIACAO = 2_000

#: Teto do brief — a nota de quem escreveu sobre o que o prompt está testando.
#: Curto de propósito: ele orienta quem for anotar, não substitui a rubrica.
MAX_BRIEF_CRIACAO = 1_000


class ProximoPedidoIn(BaseModel):
    """``POST /api/pedidos/proximo`` — reservar o próximo pedido da fila.

    Os dois filtros são OPCIONAIS e estreitos de propósito: disciplina e papel
    são os eixos por que alguém escolhe o que escrever hoje. Filtrar por
    ``task_type`` ou por série faria a fila prometer um recorte específico, e a
    Central existe justamente para tirar de quem escreve a decisão de "sobre o
    quê" — a página em branco é o problema que ela resolve.
    """

    model_config = ConfigDict(extra="forbid")

    anotador_id: Annotated[int, Field(ge=1)]
    disciplina: Annotated[str | None, Field(max_length=60)] = None
    papel: str | None = None

    @field_validator("anotador_id", mode="before")
    @classmethod
    def _nao_e_bool(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("este campo não é booleano")
        return v

    @field_validator("disciplina", mode="before")
    @classmethod
    def _limpa(cls, v: Any) -> Any:
        return (v.strip() or None) if isinstance(v, str) else v

    @field_validator("papel")
    @classmethod
    def _papel_conhecido(cls, v: str | None) -> str | None:
        # A tupla de `db.py`, que é de onde o CHECK do DDL sai. Um papel
        # inventado aqui devolveria fila vazia em silêncio, que é indistinguível
        # de "acabaram os pedidos".
        if v is not None and v not in PAPEIS_PEDIDO:
            raise ValueError(f"papel inválido: {v!r} (aceitos: {', '.join(PAPEIS_PEDIDO)})")
        return v


class DevolverPedidoIn(BaseModel):
    """``POST /api/pedidos/{id}/devolver``."""

    model_config = ConfigDict(extra="forbid")

    anotador_id: Annotated[int, Field(ge=1)]

    @field_validator("anotador_id", mode="before")
    @classmethod
    def _nao_e_bool(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("este campo não é booleano")
        return v


#: O contrato do blob de ``criacoes.material_json``. Viaja DENTRO do objeto,
#: como ``briefs.texto_json`` carrega ``briefs@1`` e os lotes carregam
#: ``geracao@1``: ``criacoes`` não tem coluna de schema, e criar uma agora
#: afirmaria que toda criação tem material — a livre não tem.
SCHEMA_MATERIAL_CRIACAO = "material_criacao@1"


class EscalaCriacaoIn(BaseModel):
    """A escala ancorada de um critério, no formato de ``rubrica@3``.

    É o formato do INSTRUMENTO montado (``escala.min/max/ancoras``), e não o
    ``escrever_rubrica@1`` do formulário do anotador (``escala_min``/
    ``rotulo_min``). Os dois existem e são diferentes de propósito, e gerar no
    errado daria uma rubrica que a tela abre **sem âncora nenhuma** —
    funcionando, e medindo outra coisa. É o defeito do ``routes_revisao:243``,
    que fez uma rubrica de 1 a 5 aceitar nota 9 no servidor.
    """

    model_config = ConfigDict(extra="forbid")

    min: int
    max: int
    ancoras: list[dict[str, Any]]

    @field_validator("min", "max", mode="before")
    @classmethod
    def _nao_e_bool(cls, v: Any) -> Any:
        # `bool` é subclasse de `int`, sétima aparição: `true` viraria escala 1.
        if isinstance(v, bool):
            raise ValueError("este campo não é booleano")
        return v

    @model_validator(mode="after")
    def _cabe_e_tem_pontas(self) -> EscalaCriacaoIn:
        if not (1 <= self.min < self.max <= 9) or self.max - self.min < 2:
            raise ValueError(
                f"escala {self.min}..{self.max} inválida — precisa caber em 1..9 e ter "
                "ao menos 3 posições"
            )
        valores: set[int] = set()
        for a in self.ancoras:
            if not isinstance(a, dict):
                raise ValueError("cada âncora precisa ser um objeto {valor, rotulo}")
            valor, rotulo = a.get("valor"), a.get("rotulo")
            if isinstance(valor, bool) or not isinstance(valor, int):
                raise ValueError("âncora sem 'valor' inteiro")
            if not isinstance(rotulo, str) or len(rotulo.strip()) < 3:
                raise ValueError(f"a âncora {valor} está sem rótulo legível")
            if not self.min <= valor <= self.max:
                raise ValueError(f"âncora {valor} fora da escala {self.min}..{self.max}")
            valores.add(valor)
        # As PONTAS são o que faz uma escala ancorada ser ancorada: sem elas,
        # "clareza de 1 a 5" significa cinco coisas para cinco anotadores.
        if self.min not in valores or self.max not in valores:
            raise ValueError(
                f"as duas PONTAS ({self.min} e {self.max}) precisam estar ancoradas"
            )
        return self


class CriterioCriacaoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nome: Annotated[str, Field(min_length=3, max_length=120)]
    descricao: Annotated[str, Field(min_length=10, max_length=600)]
    escala: EscalaCriacaoIn


class RubricaCriacaoIn(BaseModel):
    """A rubrica que vem junto do prompt criado, na forma de ``rubrica@3``."""

    model_config = ConfigDict(extra="forbid")

    titulo: Annotated[str, Field(min_length=5, max_length=160)]
    criterios: list[CriterioCriacaoIn]

    @model_validator(mode="after")
    def _quantidade_e_nomes(self) -> RubricaCriacaoIn:
        piso = int(_cfg("annotate", "min_criterios_rubrica", default=3))
        teto = int(_cfg("pedidos", "max_criterios_rubrica", default=5))
        if not (piso <= len(self.criterios) <= teto):
            raise ValueError(
                f"a rubrica precisa de {piso} a {teto} critérios (vieram "
                f"{len(self.criterios)})"
            )
        nomes = [c.nome.strip().casefold() for c in self.criterios]
        if len(set(nomes)) != len(nomes):
            raise ValueError("dois critérios com o mesmo nome")
        return self


class GoldCriacaoIn(BaseModel):
    """A resposta-gold DESCRITIVA: o que uma resposta precisaria conter.

    Em listas, e não em prosa corrida, pelo mesmo argumento do checklist do SFT:
    o revisor confere item a item. Prosa livre cabe em ``observacoes``, que é
    opcional — e é opcional para que ela não vire o campo onde tudo acaba.

    NÃO é a resposta ideal redigida. Escrever a resposta seria produzir o dado
    de SFT, que é outro trabalho, com outro contrato e outro custo; aqui o que se
    quer é a RÉGUA, e ela é mais barata de escrever e mais útil de revisar.
    """

    model_config = ConfigDict(extra="forbid")

    deve_conter: list[str]
    nao_pode: list[str]
    armadilhas: list[str] = []
    observacoes: Annotated[str | None, Field(max_length=1200)] = None

    @field_validator("deve_conter", "nao_pode", "armadilhas", mode="before")
    @classmethod
    def _itens_limpos(cls, v: Any) -> Any:
        if isinstance(v, list):
            return [x.strip() if isinstance(x, str) else x for x in v if x is not None]
        return v

    @field_validator("observacoes", mode="before")
    @classmethod
    def _obs_limpa(cls, v: Any) -> Any:
        return (v.strip() or None) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _pisos(self) -> GoldCriacaoIn:
        minimo = int(_cfg("pedidos", "gold_min_chars_item", default=15))
        exigidos = {
            "deve_conter": int(_cfg("pedidos", "gold_min_deve_conter", default=2)),
            "nao_pode": int(_cfg("pedidos", "gold_min_nao_pode", default=1)),
        }
        for campo, piso in exigidos.items():
            itens = getattr(self, campo)
            if len(itens) < piso:
                raise ValueError(f"'{campo}' precisa de ao menos {piso} item(ns)")
        for campo in ("deve_conter", "nao_pode", "armadilhas"):
            for i, item in enumerate(getattr(self, campo)):
                if not isinstance(item, str) or len(item) < minimo:
                    raise ValueError(
                        f"'{campo}[{i}]' precisa de ao menos {minimo} caracteres — um "
                        "item que não diz o que conferir não é conferível"
                    )
        return self


class MaterialCriacaoIn(BaseModel):
    """A TRÍADE que acompanha um prompt escrito a partir de um pedido.

    O prompt é o produto; a rubrica é como avaliá-lo; a gold é a referência de
    revisão. Os três juntos são o que datasets de instruction-following pedem, e
    é a razão de a Central existir — um prompt solto qualquer pessoa escreve.

    ``schema`` é aceito e ignorado se vier: quem carimba é o servidor
    (``SCHEMA_MATERIAL_CRIACAO``), porque um cliente que declarasse o contrato
    poderia declarar um que ele não cumpre.
    """

    model_config = ConfigDict(extra="forbid")

    rubrica: RubricaCriacaoIn
    gold: GoldCriacaoIn


class CriacaoIn(BaseModel):
    """``POST /api/criacoes`` — o modo criar.

    ``lang`` é DECLARADO e não detectado, e vale registrar que ele é um palpite:
    quem decide o idioma de verdade é o **s02**, com dois detectores e um
    árbitro, quando o texto atravessar a pipeline. Detectar aqui daria um
    terceiro veredito, de uma terceira implementação, sobre a mesma pergunta —
    e o da pipeline é o que vai para o corpus.

    ``task_type_sugerido``/``domain_sugerido`` falam a língua da taxonomia do
    corpus (os mesmos ids do escopo do brief e da pergunta de categoria do P9).
    **Sugeridos**, no nome e no efeito: eles viajam como pista para quem revisa,
    e não como rótulo — o rótulo do corpus sai da campanha de rotulagem.
    """

    model_config = ConfigDict(extra="forbid")

    autor_id: Annotated[int, Field(ge=1)]
    texto: Annotated[str, Field(max_length=MAX_TEXTO_CRIACAO)]
    lang: str
    task_type_sugerido: Annotated[str | None, Field(max_length=60)] = None
    domain_sugerido: Annotated[str | None, Field(max_length=60)] = None
    brief: Annotated[str | None, Field(max_length=MAX_BRIEF_CRIACAO)] = None
    #: O pedido que originou este prompt (Central de Briefs). NULL na criação
    #: livre, que continua existindo exatamente como estava — o P4 não sabe que
    #: a v7 existe, e é assim que tem de continuar.
    pedido_id: Annotated[int | None, Field(ge=1)] = None
    material: MaterialCriacaoIn | None = None

    @field_validator("autor_id", "pedido_id", mode="before")
    @classmethod
    def _nao_e_bool(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("este campo não é booleano")
        return v

    @model_validator(mode="after")
    def _pedido_e_material_andam_juntos(self) -> CriacaoIn:
        """A regra que o CHECK não pode expressar: ela depende de outra coluna.

        Os dois sentidos são erros diferentes. Pedido sem material entregaria
        metade do que a Central existe para produzir — e o pedido ficaria
        ``usado`` sem ter rendido a tríade. Material sem pedido é alguém
        mandando rubrica e gold por uma vista que não os coleta: aceitar
        gravaria um blob que nenhuma tela mostra e nenhum export lê.
        """
        if self.pedido_id is not None and self.material is None:
            raise ValueError(
                "uma criação vinda de pedido precisa da tríade: prompt, rubrica e gold"
            )
        if self.material is not None and self.pedido_id is None:
            raise ValueError("'material' só faz sentido junto de um 'pedido_id'")
        return self

    @field_validator("texto", "brief", "task_type_sugerido", "domain_sugerido", mode="before")
    @classmethod
    def _limpo(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    @field_validator("texto")
    @classmethod
    def _texto_tem_piso(cls, v: str | None) -> str:
        # O piso sai do settings (`min_chars_criacao`), como todos os limites que
        # a tela mostra antes do clique — uma cópia aqui divergiria do 422 na
        # primeira vez que alguém ajustasse o arquivo.
        piso = int(_cfg("annotate", "min_chars_criacao", default=15))
        if v is None or len(v) < piso:
            raise ValueError(f"o prompt precisa de pelo menos {piso} caracteres")
        return v

    @field_validator("lang")
    @classmethod
    def _lang_conhecida(cls, v: str) -> str:
        # As duas do CHECK do DDL. O corpus tem outras, mas a plataforma é
        # bilíngue e um `fr` aqui viraria IntegrityError sem nome de campo.
        if v not in ("pt", "en"):
            raise ValueError(f"idioma fora de ['pt', 'en']: {v!r}")
        return v


class RevisarCriacaoIn(BaseModel):
    """``POST /api/criacoes/{id}/revisar`` — aprova ou recusa uma criação.

    ``comentario`` é obrigatório na RECUSA, pela mesma razão da devolução da
    triagem: recusar sem dizer por quê devolve a decisão sem devolver a
    informação, e quem escreveu não tem como fazer diferente da próxima vez.
    Aprovar sem comentário é normal — não há nada a corrigir.
    """

    model_config = ConfigDict(extra="forbid")

    revisor_id: Annotated[int, Field(ge=1)]
    aprovar: bool
    comentario: Annotated[str | None, Field(max_length=MAX_MOTIVO)] = None

    @field_validator("revisor_id", mode="before")
    @classmethod
    def _nao_e_bool(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("este campo não é booleano")
        return v

    @field_validator("comentario", mode="before")
    @classmethod
    def _limpo(cls, v: Any) -> Any:
        return (v.strip() or None) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _recusa_exige_motivo(self) -> RevisarCriacaoIn:
        piso = int(_cfg("annotate", "min_chars_devolucao", default=20))
        if not self.aprovar and len(self.comentario or "") < piso:
            raise ValueError(
                f"recusar exige um comentário de pelo menos {piso} caracteres — "
                "ele é a única informação que quem escreveu recebe"
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
    "MAX_ALVO_GERACAO",
    "MAX_CAMPO",
    "MAX_GERACAO",
    "MAX_MOTIVO",
    "MAX_NOME",
    "MAX_TEMPO_ATIVO_MS",
    "MAX_TURNO",
    "AbandonarIn",
    "AvaliarIn",
    "DecisaoAdminIn",
    "EdicaoIn",
    "EscolherRodadaIn",
    "GerarTarefasIn",
    "LivreIn",
    "PerfilIn",
    "ProximaIn",
    "SubmeterIn",
    "TriarIn",
    "TurnoIn",
    "perfil",
]
