"""Modelos Pydantic do contrato da API.

Duas famílias:

* ``Filtros`` — o recorte do corpus. É o **mesmo objeto** em três lugares: como
  query string em ``GET /api/prompts`` e ``GET /api/facets``, e como campo
  aninhado no corpo de ``/api/collections/{id}/items/from-filter`` e de
  ``POST /api/export``. Um único vocabulário de filtro em toda a app é o que
  permite "adicionar à coleção os 12.483 que estou vendo" sem o cliente
  paginar, e é o que faz a contagem prometida pela faceta ser a contagem
  entregue pelo ``from-filter``.
* ``ConsultaPrompts`` — ``Filtros`` + ordenação e paginação. Só a listagem usa.

``extra="forbid"`` em todos: ``?task_types=codigo`` (plural, um erro de digitação
plausível) devolve **422** em vez de ignorar o parâmetro e devolver o corpus
inteiro achando que filtrou. Falhar alto aqui é barato; devolver 189 mil linhas
como se fossem o recorte pedido é o tipo de erro que passa despercebido e
contamina um export.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..config import get as _cfg
from ..schema import DOMAINS, QUALITY_VALUES, TASK_TYPES, License

#: Tetos vindos do ``config/settings.toml`` (nunca hardcoded). Lidos no import
#: porque viram limites de validação do Pydantic — o ``config`` é cacheado.
MAX_PAGE_SIZE: int = int(_cfg("app", "max_page_size", default=200))
PAGE_SIZE: int = int(_cfg("app", "page_size", default=50))
MAX_UIDS: int = int(_cfg("app", "max_uids_por_requisicao", default=5000))
#: Tetos da busca semântica (M10). Ficam aqui, e não em ``app/semantic.py``, pelo
#: mesmo motivo dos de cima: viram limites de validação do Pydantic, que são
#: lidos no import.
K_SEMANTICO: int = int(_cfg("app", "semantic_k", default=50))
MAX_K_SEMANTICO: int = int(_cfg("app", "semantic_max_k", default=200))
MIN_CHARS_SEMANTICO: int = int(_cfg("app", "semantic_min_chars", default=3))

#: Ordenações aceitas. ``relevance`` só existe com ``q``; sem ele a rota cai
#: para ``newest`` e **devolve o sort efetivo** no envelope, para a interface
#: não mentir sobre o que o usuário está vendo.
Ordenacao = Literal[
    "relevance", "newest", "oldest", "shortest", "longest", "dups", "random"
]

_LICENCAS: frozenset[str] = frozenset(m.value for m in License)


class Filtros(BaseModel):
    """O recorte do corpus. Todo campo é opcional; nada preenchido = tudo."""

    model_config = ConfigDict(extra="forbid")

    #: Busca textual (FTS5). Passa pelo sanitizador de ``queries.sanitize_fts``
    #: antes de chegar ao SQLite — nunca vai crua para o MATCH.
    q: str | None = None

    lang: list[Literal["pt", "en"]] | None = None
    lang_variant: list[Literal["pt-BR", "pt-PT", "pt-indef"]] | None = None
    source: list[str] | None = None
    license: list[str] | None = None
    task_type: list[str] | None = None
    domain: list[str] | None = None

    quality: list[int] | None = None
    #: ``quality_min`` EXCLUI os não rotulados (NULL não é >= 1). É o
    #: comportamento desejado, mas a interface precisa dizer isso na tela.
    quality_min: Annotated[int, Field(ge=1, le=3)] | None = None

    #: ``exclude`` usa ``IS NOT 1`` e portanto **mantém** os não rotulados
    #: (nsfw NULL). Ver ``queries.NSFW_SQL``.
    nsfw: Literal["exclude", "include", "only"] = "exclude"
    pii: Literal["any", "only", "none"] = "any"

    needs_review: bool | None = None
    edited: bool | None = None
    commercial_only: bool = False
    redistributable_only: bool = False

    #: ANTI-ROBÔ. ~34% do português do WildChat é um template automatizado
    #: repetido; ``max_dups=0`` deixa só o que aparece uma vez no corpus.
    max_dups: Annotated[int, Field(ge=0)] | None = None

    n_chars_min: Annotated[int, Field(ge=0)] | None = None
    n_chars_max: Annotated[int, Field(ge=0)] | None = None

    collection_id: int | None = None

    @field_validator("task_type")
    @classmethod
    def _task_valido(cls, v: list[str] | None) -> list[str] | None:
        return _checar_vocabulario(v, TASK_TYPES, "task_type")

    @field_validator("domain")
    @classmethod
    def _domain_valido(cls, v: list[str] | None) -> list[str] | None:
        return _checar_vocabulario(v, DOMAINS, "domain")

    @field_validator("license")
    @classmethod
    def _license_valida(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        desconhecidas = [x for x in v if x not in _LICENCAS]
        if desconhecidas:
            raise ValueError(
                f"licença desconhecida: {desconhecidas} "
                f"(conhecidas: {', '.join(sorted(_LICENCAS))})"
            )
        return v

    @field_validator("quality", mode="before")
    @classmethod
    def _quality_valida(cls, v: Any) -> Any:
        # `mode="before"` é obrigatório: o Pydantic converte bool em int na
        # coerção padrão, então um `quality=[true]` chegaria a um validador
        # normal já virado `[1]` e passaria. Ver o mesmo cuidado em
        # PatchPrompt._quality e em labeling_io.
        if v is None:
            return None
        valores = v if isinstance(v, list) else [v]
        for x in valores:
            if isinstance(x, bool):
                raise ValueError("quality não é booleano (use 1, 2 ou 3)")
        return v

    @field_validator("quality")
    @classmethod
    def _quality_no_intervalo(cls, v: list[int] | None) -> list[int] | None:
        # Depois da coerção: aqui os valores já são int (query string manda
        # "2", não 2), e é onde o intervalo pode ser conferido.
        if v is None:
            return None
        fora = [x for x in v if x not in QUALITY_VALUES]
        if fora:
            raise ValueError(f"quality fora de {list(QUALITY_VALUES)}: {fora}")
        return v

    @model_validator(mode="after")
    def _faixas_coerentes(self) -> Filtros:
        if (
            self.n_chars_min is not None
            and self.n_chars_max is not None
            and self.n_chars_min > self.n_chars_max
        ):
            raise ValueError(
                f"n_chars_min ({self.n_chars_min}) > n_chars_max ({self.n_chars_max})"
            )
        return self

    def filtros(self) -> Filtros:
        """O recorte puro (``ConsultaPrompts`` sobrescreve devolvendo a base)."""
        return self


def _checar_vocabulario(
    valores: list[str] | None, vocabulario: tuple[str, ...], nome: str
) -> list[str] | None:
    """Valida contra a taxonomia **em Python**, não com um Enum gerado.

    A taxonomia evolui (v1.1 já está prevista) e um ``Enum`` congelado no import
    transformaria "classe nova no taxonomy.json" num 500 obscuro. Aqui a
    divergência vira 422 com a lista do que é aceito.
    """
    if valores is None:
        return None
    fora = [v for v in valores if v not in vocabulario]
    if fora:
        raise ValueError(f"{nome} fora da taxonomia: {fora}")
    return valores


class ConsultaPrompts(Filtros):
    """``Filtros`` + ordenação e paginação. Só ``GET /api/prompts`` usa."""

    sort: Ordenacao = "relevance"
    #: Semente do ``sort=random``. Fixa por sessão na interface, senão cada
    #: página traria uma ordem diferente e o usuário reveria os mesmos itens.
    seed: Annotated[int, Field(ge=1, le=2_147_483_647)] = 42
    page: Annotated[int, Field(ge=1)] = 1
    page_size: Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE)] = PAGE_SIZE

    def filtros(self) -> Filtros:
        """A parte de recorte, sem ordenação/paginação — é o que vai para o
        ``filters`` do manifesto de export e para o ``from-filter``."""
        return Filtros.model_validate(
            self.model_dump(include=set(Filtros.model_fields))
        )


class ConsultaSemantica(Filtros):
    """``Filtros`` + ``k``. Só ``GET /api/semantic`` usa (M10).

    **Não herda ``sort``, ``page`` nem ``page_size``, e isso é deliberado.** Numa
    busca por sentido a ordem É o escore — não existe "ordenar por mais novo"
    dentro de um resultado cuja única razão de ser é a proximidade. E "página 2 do
    sentido" não significa nada para quem procura: ou o item está entre os mais
    próximos ou não está. Com ``extra="forbid"``, mandar ``sort=newest`` aqui
    devolve **422** em vez de ser ignorado em silêncio — que é justamente o que
    faria a interface prometer uma ordenação que não aconteceu.

    ``q`` é herdado de ``Filtros`` (onde é opcional) e passa a ser **obrigatório**:
    sem texto não há vetor de consulta.
    """

    #: Quantos itens voltam. Teto em ``[app] semantic_max_k``: o custo não está no
    #: k (o top-k é 1,4 ms para qualquer k pequeno), está em hidratar e serializar
    #: — e ninguém lê 5.000 vizinhos.
    k: Annotated[int, Field(ge=1, le=MAX_K_SEMANTICO)] = K_SEMANTICO

    @model_validator(mode="after")
    def _consulta_com_texto(self) -> ConsultaSemantica:
        texto = (self.q or "").strip()
        if len(texto) < MIN_CHARS_SEMANTICO:
            raise ValueError(
                f"q é obrigatório na busca por sentido e precisa de ao menos "
                f"{MIN_CHARS_SEMANTICO} caracteres (recebi {len(texto)}): abaixo "
                "disso o modelo devolve ruído com forma de vetor"
            )
        return self

    def filtros(self) -> Filtros:
        """O recorte puro, sem o ``k``."""
        return Filtros.model_validate(self.model_dump(include=set(Filtros.model_fields)))


# ---------------------------------------------------------------------------
# edição
# ---------------------------------------------------------------------------


class PatchPrompt(BaseModel):
    """Corpo do ``PATCH /api/prompts/{uid}``.

    ``exclude_unset`` é o que distingue "não mandei o campo" de "mandei null":
    limpar um rótulo (``{"domain": null}``) é uma ação legítima e diferente de
    não mexer nele. Por isso nenhum campo tem default aqui — quem lê usa
    ``model_dump(exclude_unset=True)``.
    """

    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    task_type: str | None = None
    domain: str | None = None
    quality: int | None = None
    nsfw: bool | None = None
    needs_review: bool | None = None

    @field_validator("text")
    @classmethod
    def _texto_nao_vazio(cls, v: str | None) -> str | None:
        # `text` é NOT NULL no DDL e um prompt vazio não é curadoria, é perda.
        if v is None or not v.strip():
            raise ValueError("text não pode ser nulo nem vazio (use o revert para desfazer)")
        return v

    @field_validator("task_type")
    @classmethod
    def _task(cls, v: str | None) -> str | None:
        if v is not None and v not in TASK_TYPES:
            raise ValueError(f"task_type fora da taxonomia: {v!r}")
        return v

    @field_validator("domain")
    @classmethod
    def _domain(cls, v: str | None) -> str | None:
        if v is not None and v not in DOMAINS:
            raise ValueError(f"domain fora da taxonomia: {v!r}")
        return v

    @field_validator("quality", mode="before")
    @classmethod
    def _quality(cls, v: Any) -> Any:
        # `mode="before"`, e não um validador normal: bool é subclasse de int em
        # Python E o Pydantic coage bool→int na entrada, então `"quality": true`
        # chegaria a um validador comum já como `1` e viraria "qualidade ruim"
        # em silêncio. A checagem tem de acontecer ANTES da coerção.
        if isinstance(v, bool):
            raise ValueError("quality não é booleano (use 1, 2 ou 3)")
        if v is not None and v not in QUALITY_VALUES:
            raise ValueError(f"quality fora de {list(QUALITY_VALUES)}: {v}")
        return v

    @model_validator(mode="after")
    def _algo_para_mudar(self) -> PatchPrompt:
        if not self.model_fields_set:
            raise ValueError("corpo vazio: informe ao menos um campo para alterar")
        return self


# ---------------------------------------------------------------------------
# coleções
# ---------------------------------------------------------------------------


class ColecaoIn(BaseModel):
    """Criação de coleção."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=200)]
    description: str | None = None


class ColecaoPatch(BaseModel):
    """Renomear / redescrever uma coleção."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _algo_para_mudar(self) -> ColecaoPatch:
        if not self.model_fields_set:
            raise ValueError("corpo vazio: informe name e/ou description")
        return self


class ItensUids(BaseModel):
    """Lista explícita de uids (adicionar ou remover de uma coleção)."""

    model_config = ConfigDict(extra="forbid")

    uids: Annotated[list[str], Field(min_length=1, max_length=MAX_UIDS)]


class ItensPorFiltro(BaseModel):
    """Adicionar/remover **todos os N que casam** com um filtro.

    É o que torna possível montar uma coleção de 10 mil itens sem 10 mil
    cliques — e, no sentido inverso, tirar de uma vez a família inteira do robô
    que responde por ~34% do português do WildChat.
    """

    model_config = ConfigDict(extra="forbid")

    filter: Filtros
    #: Teto opcional ("os 500 primeiros do filtro atual"). Sem ele, vão todos.
    limit: Annotated[int, Field(ge=1)] | None = None
    #: Ordem usada quando há ``limit`` — sem isso, "os 500 primeiros" seria
    #: uma amostra arbitrária que muda entre chamadas.
    sort: Ordenacao = "newest"
    seed: Annotated[int, Field(ge=1, le=2_147_483_647)] = 42


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


class ExportIn(BaseModel):
    """Corpo do ``POST /api/export``.

    ``profile`` e ``container`` são separados de propósito: ``exports.format``
    tem ``CHECK (format IN ('jsonl','csv'))`` no DDL, então um perfil futuro
    (Label Studio) entra como ``profile="label-studio"`` continuando a gravar
    ``format="jsonl"`` — zero migração de schema. Ver ``export.REGISTRY``.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["filter", "collection", "uids"]
    format: Literal["jsonl", "csv"] = "jsonl"
    profile: str = "flat"

    filter: Filtros | None = None
    collection_id: int | None = None
    uids: list[str] | None = None

    #: ``redistributable = 0`` fica FORA por padrão. Ligar isto é legítimo (uso
    #: local), e o manifesto carimba um WARNING dizendo para não publicar.
    include_nonredistributable: bool = False
    include_text_original: bool = False
    #: Nome do arquivo (sem extensão). Vazio = carimbo de tempo.
    name: str | None = None

    @model_validator(mode="after")
    def _campo_do_modo(self) -> ExportIn:
        exigido = {"filter": "filter", "collection": "collection_id", "uids": "uids"}[self.mode]
        if getattr(self, exigido) is None:
            raise ValueError(f"mode={self.mode!r} exige o campo {exigido!r}")
        sobrando = [
            campo
            for campo in ("filter", "collection_id", "uids")
            if campo != exigido and getattr(self, campo) is not None
        ]
        if sobrando:
            # Mandar filter E collection_id é ambíguo; adivinhar qual vale
            # produziria um export plausível e errado.
            raise ValueError(f"mode={self.mode!r} não aceita {sobrando}")
        if self.mode == "uids" and not self.uids:
            raise ValueError("uids vazio")
        return self

    def chave_formato(self) -> str:
        """A chave do ``export.REGISTRY``: ``"<profile>.<container>"``."""
        return f"{self.profile}.{self.format}"


def dump_filtros(f: Filtros) -> dict[str, Any]:
    """Filtros como dict enxuto (só o que foi realmente pedido).

    Vai para ``exports.filters_json`` e para o manifesto: gravar os 20 campos
    com seus defaults faria o manifesto de um export sem filtro nenhum parecer
    um recorte elaborado.
    """
    return f.model_dump(exclude_defaults=True, exclude_none=True)


__all__ = [
    "K_SEMANTICO",
    "MAX_K_SEMANTICO",
    "MAX_PAGE_SIZE",
    "MAX_UIDS",
    "MIN_CHARS_SEMANTICO",
    "PAGE_SIZE",
    "ColecaoIn",
    "ColecaoPatch",
    "ConsultaPrompts",
    "ConsultaSemantica",
    "ExportIn",
    "Filtros",
    "ItensPorFiltro",
    "ItensUids",
    "Ordenacao",
    "PatchPrompt",
    "dump_filtros",
]
