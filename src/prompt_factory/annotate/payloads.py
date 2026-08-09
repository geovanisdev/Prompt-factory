"""Os seis payloads versionados — o contrato do que o anotador produz.

``avaliar_rubrica@1`` · ``escrever_rubrica@1`` · ``sft_resposta@1`` ·
``comparar_ab@1`` · ``conversa_modelo@1`` · ``duelo_modelos@1``.
O nome do schema é gravado numa COLUNA de ``anotacoes``
(``payload_schema``), e não deduzido do tipo da tarefa na hora de ler: a versão
viaja com o dado, para que uma anotação de hoje continue legível quando o
formato evoluir.

A REGRA QUE VALE MAIS QUE TODAS AS OUTRAS AQUI
==============================================
**O tipo vem do SERVIDOR.** ``escolher(tipo)`` é sempre chamado com
``tarefas.tipo`` lido do banco, nunca com o que o cliente afirmou estar
enviando. Um cliente que dissesse "isto é um comparar_ab" enquanto a tarefa é
``sft_resposta`` gravaria um payload perfeitamente válido no schema errado — e
o defeito só apareceria no export, meses depois.

``bool`` É SUBCLASSE DE ``int`` EM PYTHON
=========================================
E o Pydantic ainda coage ``true`` para ``1`` **antes** de qualquer validador
comum. Uma nota ``true`` viraria "nota 1" em silêncio, que é uma nota válida e
errada. A checagem tem de ser ``mode="before"``, e é o mesmo cuidado que já
mordeu ``labeling_io`` e ``app/models``. Aqui é pior que nos dois: uma nota é o
dado inteiro desta plataforma.

LIMITES LIDOS NA HORA
=====================
``min_chars_justificativa``, ``max_criterios_rubrica`` e companhia moram em
``config/settings.toml``. Os validadores os leem **durante a validação**, não no
import: um limiar hardcoded no corpo da classe congelaria a configuração no
momento em que o módulo foi importado, e o arquivo é justamente o que o dono
mexe entre duas subidas.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..config import get as _cfg
from .db import ESCALA_TURNO, PAPEIS_TURNO, ROTULOS_DUELO, ROTULOS_MODELO, TIPOS_TAREFA

#: Versão PADRÃO, para um tipo que não esteja em ``VERSOES``. Continua existindo
#: porque cinco dos seis contratos estão nela e escrevê-la seis vezes na tabela
#: abaixo só criaria seis lugares para divergir.
VERSAO_PAYLOAD = 1

#: Versão POR TIPO. Era uma constante global (`VERSAO_PAYLOAD`) lida por
#: ``nome_schema`` para os seis contratos — e essa era a armadilha: subir o
#: ``avaliar_rubrica`` para 2 renomearia os outros cinco, que não mudaram. O
#: ``entrega.py`` grava ``payload_schema`` dentro do JSONL entregue, então cinco
#: contratos passariam a **declarar publicamente uma versão que nunca existiu**.
# `avaliar_rubrica`: @2 = P8 (N/A, tipos_issue, trecho); @3 = P9 (respostas
# às perguntas do instrumento — a caixa de user goal).
VERSOES: dict[str, int] = {"avaliar_rubrica": 3}

#: Separador entre o contrato e a identidade do INSTRUMENTO em ``payload_schema``.
#:
#: POR QUE ISTO EXISTE ANTES DE O CONSTRUTOR EXISTIR
#: =================================================
#: O P5a dá ao admin um arsenal para montar a tarefa: escolher blocos, reusar
#: perguntas de um banco, escrever novas. A partir daí **duas tarefas do mesmo
#: tipo têm instrumentos diferentes**, e o ``assert set(MODELOS) == set(TIPOS_TAREFA)``
#: lá embaixo deixa de descrever o mundo.
#:
#: ``payload_schema`` é gravado em TODA linha de ``anotacoes``, copiado coluna a
#: coluna pela migração e usado pelo ``entrega.py`` para chavear o glossário —
#: ou seja, **é imutável no que já está escrito**. O formato se decide agora ou
#: nunca: ``avaliar_rubrica@2+severidade@1``. Sem instrumento declarado, o sufixo
#: simplesmente não aparece e tudo o que existe hoje continua legível.
SEPARADOR_INSTRUMENTO = "+"

#: Teto de caracteres de um campo de texto livre do anotador. Não é estética:
#: sem teto, um paste acidental de 1 MB entra no ``payload_json`` e a fila do
#: revisor passa a carregar isso a cada listagem.
MAX_TEXTO_LIVRE = 20_000

#: Teto da resposta de referência (SFT). Maior que o de justificativa porque
#: aqui o texto É o produto, não o comentário sobre ele.
MAX_RESPOSTA_SFT = 40_000

#: As escolhas possíveis num A/B. ``empate`` é uma resposta legítima e precisa
#: existir no vocabulário: forçar preferência onde não há produz ruído com cara
#: de sinal.
ESCOLHAS_AB: tuple[str, ...] = (*ROTULOS_MODELO, "empate")


def _sem_bool(v: Any, campo: str) -> Any:
    """Levanta se ``v`` (ou algum item de ``v``) for booleano. Use em ``mode="before"``."""
    valores = v if isinstance(v, list) else [v]
    for x in valores:
        if isinstance(x, bool):
            raise ValueError(f"{campo} não é booleano — mande o número")
    return v


def _limpo(v: str) -> str:
    """Colapsa espaço em branco das pontas. ``" "`` vira ``""`` e reprova no min_length."""
    return v.strip()


class _Base(BaseModel):
    """``extra="forbid"`` em tudo, pela razão do ``app/models.py`` e por uma a mais.

    Numa listagem, um parâmetro ignorado devolve o corpus inteiro no lugar do
    recorte pedido. Aqui é pior: um campo digitado errado grava **trabalho
    humano pela metade**, e ninguém descobre até a hora de exportar — quando a
    pessoa que anotou já foi embora e não dá para perguntar o que ela quis dizer.
    """

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# avaliar_rubrica@1
# ---------------------------------------------------------------------------


class NotaCriterio(_Base):
    """Uma nota por critério — mais o que a torna auditável (``@2``).

    OS QUATRO ESTADOS DE UM CRITÉRIO, E POR QUE ELES PRECISAM DE NOMES DIFERENTES
    ============================================================================
    ``não avaliado`` · ``limpo`` · ``com issue`` · ``não se aplica``. Numa matriz,
    o valor bom da escala e a não-decisão são a MESMA célula preenchida se a
    distinção não for gravada — e depois, no export, ninguém consegue separar
    "olhei e está tudo certo" de "não olhei".

    Aqui a separação é dura: ``nao_aplicavel`` é campo próprio (não um valor de
    escala, não ``0``, não uma sentinela numérica — sentinela entra em toda média
    e é o defeito que ninguém encontra), e **``não avaliado`` simplesmente não
    atravessa o fio**: sem ``nao_aplicavel``, a nota é obrigatória. É estado de
    formulário, e quem o barra é o botão de enviar antes do clique.

    ``tipos_issue`` É UM OBJETO, NUNCA UMA LISTA
    ============================================
    ``{id: bool}`` com **todos** os ids do catálogo presentes, inclusive os
    ``false``. Não é gosto: ``avaliacoes.achatar`` trata lista vazia como FOLHA,
    então ``[] -> ["x"]`` produziria dois caminhos de diff (``…tipos_issue``
    sumindo e ``…tipos_issue.0`` nascendo) e o Rate and Review cobraria **dois
    motivos por uma marcação**. Com o objeto completo, todo toggle é exatamente
    um caminho, ``true``↔``false``.
    """

    criterio: Annotated[str, Field(min_length=1, max_length=200)]
    nota: int | None = None
    justificativa: Annotated[str | None, Field(max_length=MAX_TEXTO_LIVRE)] = None
    #: O critério não se aplica a esta resposta. Exige ``motivo_na`` e proíbe nota.
    nao_aplicavel: bool = False
    motivo_na: Annotated[str | None, Field(max_length=MAX_TEXTO_LIVRE)] = None
    #: ``{id_do_catalogo: marcado}``. Ver o docstring da classe.
    tipos_issue: Annotated[dict[str, bool], Field(max_length=64)] = {}
    #: Trecho verbatim da resposta que sustenta o achado. **Opcional de propósito**:
    #: exigir um span em toda severidade alta faria quem enfrenta um defeito difuso
    #: ("a resposta inteira erra o registro") colar um trecho arbitrário só para
    #: destravar o botão — evidência fabricada dentro do artefato entregue.
    trecho: Annotated[str | None, Field(max_length=MAX_TEXTO_LIVRE)] = None
    #: Ato explícito de dizer "não há um trecho: o defeito é difuso".
    trecho_difuso: bool = False

    @field_validator("nota", mode="before")
    @classmethod
    def _nota_nao_e_bool(cls, v: Any) -> Any:
        # A pegadinha central deste módulo: `"nota": true` chegaria a um
        # validador comum já convertido em 1 — uma nota válida, e errada.
        return None if v is None else _sem_bool(v, "nota")

    @field_validator("nota")
    @classmethod
    def _nota_no_intervalo(cls, v: int | None) -> int | None:
        # 1..9 é o teto ABSOLUTO da plataforma (é o que o teclado gradua). A
        # escala REAL de cada critério é a da rubrica, e quem a confere é a rota
        # de submissão, que tem a rubrica em mãos — o modelo não a tem.
        if v is not None and not 1 <= v <= 9:
            raise ValueError(f"nota fora de 1..9: {v}")
        return v

    @field_validator("justificativa", "motivo_na", "trecho")
    @classmethod
    def _texto_limpo(cls, v: str | None) -> str | None:
        if v is None:
            return None
        limpo = _limpo(v)
        # Campo aberto e não preenchido chega como "" — guardar string vazia
        # faria a fila do revisor mostrar uma justificativa que ninguém escreveu.
        return limpo or None

    @model_validator(mode="after")
    def _na_e_nota_se_excluem(self) -> NotaCriterio:
        if self.nao_aplicavel:
            # XOR duro. `nota` AUSENTE, e não `null`: um leitor que encontre a
            # chave com valor nulo não sabe dizer se ela foi apagada ou nunca
            # existiu, e é justamente essa a distinção que este campo carrega.
            if self.nota is not None:
                raise ValueError("critério marcado N/A não pode ter nota")
            if not self.motivo_na:
                raise ValueError("N/A exige o motivo — sem ele o critério some sem explicação")
            if any(self.tipos_issue.values()):
                raise ValueError("critério marcado N/A não pode ter tipo de issue")
            if self.trecho:
                raise ValueError("critério marcado N/A não pode ter trecho")
        elif self.nota is None:
            raise ValueError("critério sem nota e sem N/A — decida um dos dois")
        if self.trecho and self.trecho_difuso:
            raise ValueError("ou o trecho existe, ou o defeito é difuso — não os dois")
        return self


class AvaliarRubrica(_Base):
    """``avaliar_rubrica@3`` — uma nota por critério da rubrica, mais o geral.

    ``@1`` e ``@2`` continuam legíveis e nada os reescreve: a versão viaja na
    coluna ``payload_schema``, que é o motivo de a coluna existir. Uma linha
    gravada em ``@1`` não tem `tipos_issue` nem `nao_aplicavel`, e é isso mesmo
    — inventar as chaves na leitura afirmaria que alguém decidiu algo que
    ninguém decidiu.

    ``@3`` (P9) acrescenta ``respostas`` — as PERGUNTAS ABERTAS que o
    instrumento declara (``rubrica.perguntas``), sendo a primeira delas a caixa
    de user goal que o brief v2 manda preencher antes de dar nota. É um **mapa
    ``{id: texto}`` com todos os ids sempre presentes**, nunca lista, pela mesma
    razão do ``tipos_issue``: ``avaliacoes.achatar`` trata lista como folha, e
    com o mapa cada resposta editada no Rate and Review é exatamente UM caminho
    de diff com UM motivo.

    O Pydantic só barra a forma (string, teto de tamanho, bool disfarçado).
    Quem confere obrigatoriedade, mínimo e pergunta desconhecida é
    ``tarefas.erro_contra_a_rubrica`` — a rubrica está lá, não aqui.
    """

    notas: Annotated[list[NotaCriterio], Field(min_length=1, max_length=32)]
    comentario_geral: Annotated[str | None, Field(max_length=MAX_TEXTO_LIVRE)] = None
    respostas: dict[str, str] = Field(default_factory=dict)

    @field_validator("comentario_geral")
    @classmethod
    def _geral_limpo(cls, v: str | None) -> str | None:
        return (_limpo(v) or None) if v is not None else None

    @field_validator("respostas", mode="before")
    @classmethod
    def _respostas_sao_texto(cls, v: Any) -> Any:
        if isinstance(v, dict):
            for chave, valor in v.items():
                # A pegadinha de sempre, quarta aparição: `True` passaria por
                # `str(valor)` como "True" — uma resposta válida e inventada.
                if not isinstance(valor, str):
                    raise ValueError(f"a resposta de {chave!r} não é texto")
                if len(valor) > MAX_TEXTO_LIVRE:
                    raise ValueError(f"a resposta de {chave!r} passa do teto da plataforma")
        return v

    @model_validator(mode="after")
    def _sem_criterio_repetido(self) -> AvaliarRubrica:
        nomes = [nota.criterio for nota in self.notas]
        if len(set(nomes)) != len(nomes):
            raise ValueError("o mesmo critério aparece duas vezes nas notas")
        return self


# ---------------------------------------------------------------------------
# escrever_rubrica@1
# ---------------------------------------------------------------------------


class CriterioProposto(_Base):
    """Um critério escrito pelo anotador, com a escala e as PONTAS descritas.

    ``rotulo_min``/``rotulo_max`` são obrigatórios porque uma escala sem âncora
    não mede nada: "clareza de 1 a 5" significa cinco coisas diferentes para
    cinco anotadores, e o agreement despenca sem que ninguém saiba por quê.
    """

    nome: Annotated[str, Field(min_length=3, max_length=120)]
    descricao: Annotated[str, Field(min_length=10, max_length=2_000)]
    escala_min: int = 1
    escala_max: int = 5
    rotulo_min: Annotated[str, Field(min_length=3, max_length=200)]
    rotulo_max: Annotated[str, Field(min_length=3, max_length=200)]

    @field_validator("escala_min", "escala_max", mode="before")
    @classmethod
    def _escala_nao_e_bool(cls, v: Any) -> Any:
        return _sem_bool(v, "escala")

    @field_validator("nome", "descricao", "rotulo_min", "rotulo_max", mode="before")
    @classmethod
    def _texto_limpo(cls, v: Any) -> Any:
        return _limpo(v) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _escala_coerente(self) -> CriterioProposto:
        if self.escala_min < 1 or self.escala_max > 9:
            raise ValueError("a escala tem de caber em 1..9")
        if self.escala_max - self.escala_min < 2:
            # Escala de duas posições é um checkbox com nome de escala: quem
            # avalia não consegue registrar "quase lá", e a rubrica perde a
            # única coisa que uma rubrica faz melhor que um sim/não.
            raise ValueError("a escala precisa de ao menos 3 posições (ex.: 1 a 3)")
        return self


class EscreverRubrica(_Base):
    """``escrever_rubrica@1`` — o instrumento de medida que outros vão aplicar."""

    titulo: Annotated[str, Field(min_length=5, max_length=200)]
    criterios: list[CriterioProposto]
    notas_do_autor: Annotated[str | None, Field(max_length=MAX_TEXTO_LIVRE)] = None

    @field_validator("titulo", mode="before")
    @classmethod
    def _titulo_limpo(cls, v: Any) -> Any:
        return _limpo(v) if isinstance(v, str) else v

    @field_validator("notas_do_autor")
    @classmethod
    def _notas_limpas(cls, v: str | None) -> str | None:
        return (_limpo(v) or None) if v is not None else None

    @field_validator("criterios")
    @classmethod
    def _quantidade(cls, v: list[CriterioProposto]) -> list[CriterioProposto]:
        # Lido AGORA, e não no import: o teto mora no settings.toml e é o tipo
        # de número que o dono ajusta entre duas subidas para ver o efeito.
        teto = int(_cfg("annotate", "max_criterios_rubrica", default=8))
        piso = int(_cfg("annotate", "min_criterios_rubrica", default=3))
        if len(v) < piso:
            raise ValueError(f"a rubrica precisa de ao menos {piso} critérios")
        if len(v) > teto:
            raise ValueError(
                f"máximo de {teto} critérios — rubrica com 20 não é mais rigorosa, "
                "é impossível de aplicar duas vezes igual"
            )
        nomes = [c.nome.casefold() for c in v]
        if len(set(nomes)) != len(nomes):
            raise ValueError("dois critérios com o mesmo nome")
        return v


# ---------------------------------------------------------------------------
# sft_resposta@1
# ---------------------------------------------------------------------------


class ItemChecklist(_Base):
    """Um critério da rubrica-guia marcado (ou não) pelo autor da resposta."""

    criterio: Annotated[str, Field(min_length=1, max_length=200)]
    atendido: bool


class SftResposta(_Base):
    """``sft_resposta@1`` — a resposta de referência, o texto que vira treino."""

    resposta: Annotated[str, Field(min_length=1, max_length=MAX_RESPOSTA_SFT)]
    checklist: Annotated[list[ItemChecklist] | None, Field(max_length=32)] = None
    notas: Annotated[str | None, Field(max_length=MAX_TEXTO_LIVRE)] = None

    @field_validator("resposta", mode="before")
    @classmethod
    def _resposta_limpa(cls, v: Any) -> Any:
        # `.strip()` ANTES do min_length: uma resposta só de espaços passaria
        # pelo min_length=1 e gravaria um exemplo de treino em branco.
        return _limpo(v) if isinstance(v, str) else v

    @field_validator("notas")
    @classmethod
    def _notas_limpas(cls, v: str | None) -> str | None:
        return (_limpo(v) or None) if v is not None else None

    @field_validator("resposta")
    @classmethod
    def _tamanho_minimo(cls, v: str) -> str:
        piso = int(_cfg("annotate", "min_chars_sft", default=40))
        if len(v) < piso:
            raise ValueError(f"a resposta precisa de ao menos {piso} caracteres")
        return v


# ---------------------------------------------------------------------------
# comparar_ab@1
# ---------------------------------------------------------------------------


class ComparacaoCriterio(_Base):
    """A comparação por critério — opcional, e fechada na tela até alguém pedir."""

    criterio: Annotated[str, Field(min_length=1, max_length=200)]
    vencedor: str

    @field_validator("vencedor")
    @classmethod
    def _vencedor_conhecido(cls, v: str) -> str:
        if v not in ESCOLHAS_AB:
            raise ValueError(f"vencedor fora de {list(ESCOLHAS_AB)}: {v!r}")
        return v


class CompararAB(_Base):
    """``comparar_ab@1`` — preferência **com motivo**.

    A justificativa é obrigatória e tem piso de caracteres porque o valor de uma
    comparação está no motivo, não na preferência: "melhor" não distingue nada e
    não treina nada.
    """

    preferencia: str
    justificativa: Annotated[str, Field(max_length=MAX_TEXTO_LIVRE)]
    por_criterio: Annotated[list[ComparacaoCriterio] | None, Field(max_length=32)] = None

    @field_validator("preferencia")
    @classmethod
    def _preferencia_conhecida(cls, v: str) -> str:
        if v not in ESCOLHAS_AB:
            raise ValueError(f"preferência fora de {list(ESCOLHAS_AB)}: {v!r}")
        return v

    @field_validator("justificativa", mode="before")
    @classmethod
    def _justificativa_limpa(cls, v: Any) -> Any:
        return _limpo(v) if isinstance(v, str) else v

    @field_validator("justificativa")
    @classmethod
    def _justificativa_suficiente(cls, v: str) -> str:
        piso = int(_cfg("annotate", "min_chars_justificativa", default=30))
        if len(v) < piso:
            raise ValueError(
                f"a justificativa precisa de ao menos {piso} caracteres "
                f"(tem {len(v)}) — o valor da comparação está no motivo"
            )
        return v


# ---------------------------------------------------------------------------
# conversa_modelo@1 e duelo_modelos@1  (P4d)
# ---------------------------------------------------------------------------
#
# DUAS CHAVES DESTES DOIS CONTRATOS NÃO VÊM DO CLIENTE
# =====================================================
# ``turnos`` (e ``rodadas``, no duelo) são SOBRESCRITAS pela rota de submissão
# com o que ``conversa.para_payload`` lê de ``turnos_conversa``. Não é
# desconfiança genérica: no duelo o cliente **não sabe** qual modelo respondeu —
# esse é o ponto inteiro do rótulo cego —, e um "quem" vindo de quem não sabia
# seria fabricação. Elas continuam declaradas aqui porque o contrato do dado
# gravado é este, e é ele que o export vai ler.


class TurnoConversa(_Base):
    """Um turno da conversa. ``modelo``/``digest`` vazios no turno do humano."""

    ordem: int
    papel: str
    texto: Annotated[str, Field(min_length=1, max_length=MAX_RESPOSTA_SFT)]
    #: O ``message.thinking`` do Ollama, quando houve. Guardado e NÃO filtrado —
    #: ver ``modelos.py``: descartá-lo em silêncio seria decidir pelo anotador
    #: que o raciocínio não conta.
    raciocinio: Annotated[str, Field(max_length=MAX_RESPOSTA_SFT)] = ""
    #: O NOSSO teto de tokens cortou este turno? É informação sobre a coleta, não
    #: sobre o modelo, e sem ela um turno cortado no meio parece decisão dele.
    truncado: bool = False
    #: A TAG (``qwen3:4b``) e o CONTEÚDO (o digest). A tag é reescrita quando o
    #: autor republica o modelo; sem o digest, "o B era melhor" não significa
    #: nada daqui a seis meses.
    modelo: Annotated[str, Field(max_length=200)] = ""
    digest: Annotated[str, Field(max_length=200)] = ""
    duracao_ms: int = 0
    criado_em: Annotated[str | None, Field(max_length=64)] = None

    @field_validator("ordem", "duracao_ms", mode="before")
    @classmethod
    def _numeros_nao_sao_bool(cls, v: Any) -> Any:
        return _sem_bool(v, "ordem/duracao_ms")

    @field_validator("papel")
    @classmethod
    def _papel_conhecido(cls, v: str) -> str:
        if v not in PAPEIS_TURNO:
            raise ValueError(f"papel fora de {list(PAPEIS_TURNO)}: {v!r}")
        return v


class TurnoProblematico(_Base):
    """Um turno marcado: qual, quão grave e por quê.

    É a metade da avaliação que a rubrica não alcança. A rubrica mede a conversa
    INTEIRA; esta lista aponta o turno exato em que ela descarrilou — e é a
    diferença entre "coerência 2" e "no turno 5 ele inverteu a restrição do
    turno 2". Sem o índice, quem lê a anotação depois teria de reler a conversa
    para descobrir do que ela fala.
    """

    ordem: int
    nota: int
    motivo: Annotated[str, Field(min_length=1, max_length=MAX_TEXTO_LIVRE)]

    @field_validator("ordem", "nota", mode="before")
    @classmethod
    def _nao_e_bool(cls, v: Any) -> Any:
        # A mesma pegadinha de `NotaCriterio.nota`, e pela quinta vez neste repo:
        # `{"nota": true}` chegaria a um validador comum já convertido em 1.
        return _sem_bool(v, "ordem/nota")

    @field_validator("ordem")
    @classmethod
    def _ordem_valida(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"ordem de turno negativa: {v}")
        return v

    @field_validator("nota")
    @classmethod
    def _nota_na_escala(cls, v: int) -> int:
        piso, teto = ESCALA_TURNO
        if not piso <= v <= teto:
            raise ValueError(f"a nota de um turno vai de {piso} a {teto}; veio {v}")
        return v

    @field_validator("motivo", mode="before")
    @classmethod
    def _motivo_limpo(cls, v: Any) -> Any:
        return _limpo(v) if isinstance(v, str) else v


class _AvaliacaoDeConversa(_Base):
    """O que os dois contratos têm em comum: a rubrica, as notas e o porquê."""

    #: A IDENTIDADE do instrumento aplicado (``multiturno@1``). O ``payload_schema``
    #: versiona o FORMATO e o ``versao_diretriz`` versiona a INSTRUÇÃO; esta
    #: coluna versiona a RÉGUA. São três coisas e as três mudam sozinhas.
    rubrica: Annotated[str, Field(min_length=1, max_length=120)]
    turnos: Annotated[list[TurnoConversa], Field(min_length=1, max_length=200)] = []
    notas: Annotated[list[NotaCriterio], Field(min_length=1, max_length=32)]
    turnos_problematicos: Annotated[list[TurnoProblematico], Field(max_length=64)] = []
    justificativa: Annotated[str, Field(max_length=MAX_TEXTO_LIVRE)]

    @field_validator("justificativa", mode="before")
    @classmethod
    def _justificativa_limpa(cls, v: Any) -> Any:
        return _limpo(v) if isinstance(v, str) else v

    @field_validator("justificativa")
    @classmethod
    def _justificativa_suficiente(cls, v: str) -> str:
        piso = int(_cfg("annotate", "min_chars_justificativa", default=30))
        if len(v) < piso:
            raise ValueError(
                f"a justificativa precisa de ao menos {piso} caracteres (tem {len(v)}) — "
                "uma conversa avaliada sem motivo é um número que ninguém contesta"
            )
        return v

    @model_validator(mode="after")
    def _coerente(self) -> _AvaliacaoDeConversa:
        nomes = [nota.criterio for nota in self.notas]
        if len(set(nomes)) != len(nomes):
            raise ValueError("o mesmo critério aparece duas vezes nas notas")
        # Um turno marcado que não existe na conversa é uma nota órfã: ela
        # apontaria para um índice que ninguém consegue reencontrar depois.
        existentes = {turno.ordem for turno in self.turnos}
        if existentes:
            fora = sorted({p.ordem for p in self.turnos_problematicos} - existentes)
            if fora:
                raise ValueError(f"turno(s) marcado(s) que não existem na conversa: {fora}")
        marcados = [p.ordem for p in self.turnos_problematicos]
        if len(set(marcados)) != len(marcados):
            raise ValueError("o mesmo turno foi marcado duas vezes")
        return self


class ConversaModelo(_AvaliacaoDeConversa):
    """``conversa_modelo@1`` — a conversa com UM modelo, avaliada por inteiro.

    Aqui o nome do modelo **não** é segredo (a tela o mostra durante a conversa):
    sem duas respostas não há preferência a enviesar, e saber com quem se fala é
    o que permite ao anotador dizer "esta família faz isto".
    """


class RespostaDeRodada(_Base):
    """Um dos dois lados de uma rodada de duelo, com QUEM o escreveu."""

    rotulo: str
    texto: Annotated[str, Field(min_length=1, max_length=MAX_RESPOSTA_SFT)]
    raciocinio: Annotated[str, Field(max_length=MAX_RESPOSTA_SFT)] = ""
    truncado: bool = False
    modelo: Annotated[str, Field(max_length=200)] = ""
    digest: Annotated[str, Field(max_length=200)] = ""
    duracao_ms: int = 0

    @field_validator("duracao_ms", mode="before")
    @classmethod
    def _nao_e_bool(cls, v: Any) -> Any:
        return _sem_bool(v, "duracao_ms")

    @field_validator("rotulo")
    @classmethod
    def _rotulo_conhecido(cls, v: str) -> str:
        if v not in ROTULOS_DUELO:
            raise ValueError(f"rótulo fora de {list(ROTULOS_DUELO)}: {v!r}")
        return v


class RodadaDuelo(_Base):
    """Uma rodada: as duas respostas, qual venceu e **por quê**.

    A justificativa é por RODADA, e não uma só no fim, porque é isso que torna o
    dado multi-turno mais valioso que o de turno único: "escolhi B na rodada 1
    pela concisão e A na rodada 3 porque B esqueceu a restrição" são dois sinais
    diferentes, e uma justificativa única os fundiria num só.
    """

    ordem: int
    respostas: Annotated[list[RespostaDeRodada], Field(min_length=2, max_length=2)]
    vencedora: str
    justificativa: Annotated[str, Field(max_length=MAX_TEXTO_LIVRE)] = ""

    @field_validator("ordem", mode="before")
    @classmethod
    def _nao_e_bool(cls, v: Any) -> Any:
        return _sem_bool(v, "ordem")

    @field_validator("vencedora")
    @classmethod
    def _vencedora_conhecida(cls, v: str) -> str:
        if v not in ROTULOS_DUELO:
            raise ValueError(f"vencedora fora de {list(ROTULOS_DUELO)}: {v!r}")
        return v

    @model_validator(mode="after")
    def _vencedora_existe(self) -> RodadaDuelo:
        rotulos = [r.rotulo for r in self.respostas]
        if len(set(rotulos)) != 2:
            raise ValueError("uma rodada tem de ter os dois lados, A e B")
        if self.vencedora not in rotulos:
            raise ValueError(f"a vencedora {self.vencedora!r} não é um dos lados desta rodada")
        return self


class DueloModelos(_AvaliacaoDeConversa):
    """``duelo_modelos@1`` — preferência MULTI-TURNO, com a conversa que ela gerou.

    ``turnos`` é a conversa como ela aconteceu (o humano + a resposta vencedora
    de cada rodada); ``rodadas`` é o dado de preferência (os dois lados, quem
    venceu, por quê). São duas chaves porque são dois produtos: quem treina um
    modelo lê a primeira, quem treina um juiz lê a segunda.
    """

    rodadas: Annotated[list[RodadaDuelo], Field(max_length=100)] = []

    @model_validator(mode="after")
    def _rodadas_batem_com_os_turnos(self) -> DueloModelos:
        ordens = {turno.ordem for turno in self.turnos}
        if ordens:
            fora = sorted({r.ordem for r in self.rodadas} - ordens)
            if fora:
                raise ValueError(f"rodada(s) sem turno correspondente: {fora}")
        return self


# ---------------------------------------------------------------------------
# despacho
# ---------------------------------------------------------------------------

#: tipo de tarefa → modelo. As chaves são EXATAMENTE ``db.TIPOS_TAREFA``, e o
#: assert abaixo garante que acrescentar um tipo sem o payload correspondente
#: quebre no import, não numa submissão em produção.
MODELOS: dict[str, type[_Base]] = {
    "avaliar_rubrica": AvaliarRubrica,
    "escrever_rubrica": EscreverRubrica,
    "sft_resposta": SftResposta,
    "comparar_ab": CompararAB,
    "conversa_modelo": ConversaModelo,
    "duelo_modelos": DueloModelos,
}
assert set(MODELOS) == set(TIPOS_TAREFA), "MODELOS e TIPOS_TAREFA divergiram"


def nome_schema(tipo: str, instrumento: str | None = None) -> str:
    """O que vai para ``payload_schema``.

    ``"comparar_ab"`` → ``"comparar_ab@1"`` ·
    ``"avaliar_rubrica"`` → ``"avaliar_rubrica@2"`` ·
    ``("avaliar_rubrica", "severidade@1")`` → ``"avaliar_rubrica@2+severidade@1"``.

    O terceiro caso é o que o construtor do admin (P5a) vai usar: com um arsenal
    de blocos, duas tarefas do MESMO tipo têm instrumentos diferentes, e o tipo
    deixa de descrever o que foi preenchido. Reservar o formato agora é barato;
    depois não é — esta string é gravada em toda linha de ``anotacoes`` e nada a
    reescreve.
    """
    base = f"{tipo}@{VERSOES.get(tipo, VERSAO_PAYLOAD)}"
    return base if not instrumento else f"{base}{SEPARADOR_INSTRUMENTO}{instrumento}"


def partes_do_schema(schema: str) -> tuple[str, str, str | None]:
    """``"avaliar_rubrica@2+severidade@1"`` → ``("avaliar_rubrica", "2", "severidade@1")``.

    O leitor do formato acima, em UM lugar. Quem precisar decidir por tipo (o
    ``entrega.py`` chaveia o glossário assim) usa o primeiro elemento e ignora o
    resto — que é o que faz uma linha com instrumento continuar legível por
    código escrito antes de o instrumento existir.
    """
    corpo, _, instrumento = str(schema).partition(SEPARADOR_INSTRUMENTO)
    tipo, _, versao = corpo.partition("@")
    return tipo, versao, instrumento or None


def escolher(tipo: str) -> type[_Base]:
    """O modelo do tipo. **Chame sempre com ``tarefas.tipo`` lido do banco.**"""
    try:
        return MODELOS[tipo]
    except KeyError as exc:  # pragma: no cover - CHECK do DDL já barra
        raise ValueError(f"tipo de tarefa sem payload: {tipo!r}") from exc


__all__ = [
    "ESCOLHAS_AB",
    "MAX_RESPOSTA_SFT",
    "MAX_TEXTO_LIVRE",
    "MODELOS",
    "SEPARADOR_INSTRUMENTO",
    "VERSAO_PAYLOAD",
    "VERSOES",
    "AvaliarRubrica",
    "ComparacaoCriterio",
    "CompararAB",
    "ConversaModelo",
    "CriterioProposto",
    "DueloModelos",
    "EscreverRubrica",
    "ItemChecklist",
    "NotaCriterio",
    "RespostaDeRodada",
    "RodadaDuelo",
    "SftResposta",
    "TurnoConversa",
    "TurnoProblematico",
    "escolher",
    "nome_schema",
]
