"""Contrato canônico do universo de prompts: colunas, enums e taxonomia.

Este módulo é a **fonte única** do formato das linhas que trafegam entre os
estágios ``s01`` a ``s12`` em Parquet. Quem cria coluna nova mexe aqui primeiro;
``db.py`` espelha o subconjunto que vai para o SQLite.

Import leve de propósito (só stdlib): ``pyarrow`` entra *lazy*, dentro de
``arrow_schema()``, para que ``pf --help`` não pague por ele.

Regras que valem para todos os estágios:

* ``text`` é sempre ``textnorm.norm_display(text_raw)``; ``text_raw`` guarda o
  original exatamente como veio da fonte e não é carregado no SQLite.
* ``hash_norm`` é ``sha256(textnorm.norm_for_hash(text))`` — ver a cadeia
  canônica documentada em ``textnorm``.
* ``license`` / ``commercial_ok`` / ``redistributable`` viajam **por linha**,
  vindos de ``config/sources.toml``; ``LICENSE_POLICY`` existe só para conferir
  coerência, nunca para substituir o que a fonte declarou.
* ``task_type`` e ``domain`` não têm CHECK no banco (a taxonomia evolui): a
  validação contra ``TASK_TYPES``/``DOMAINS`` acontece em Python.

Pegadinha do pandas: ``quality`` é ``int8`` *nullable*. Ao converter para
pandas com valores nulos, use ``dtype="Int8"`` (com maiúscula) ou fique em
pyarrow — senão a coluna vira ``float64`` e 3 vira 3.0 no export.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:  # pragma: no cover - só para type checkers
    import pyarrow as pa

#: Versão da taxonomia em ``labeling/taxonomy.json``. Muda junto com as tuplas
#: abaixo; ``load_taxonomy()`` recusa arquivo com versão diferente.
TAXONOMY_VERSION = "1.0"

#: 16 classes de tarefa, NA ORDEM do taxonomy.json (a ordem é contrato: relatórios
#: e a UI iteram por ela).
TASK_TYPES: tuple[str, ...] = (
    "geracao-criativa",
    "redacao-pratica",
    "reescrita-edicao",
    "resumo",
    "traducao",
    "qa-aberta",
    "qa-contexto",
    "brainstorm",
    "classificacao-extracao",
    "codigo",
    "matematica-raciocinio",
    "conselho-opiniao",
    "planejamento",
    "roleplay-persona",
    "conversa-social",
    "outro",
)

#: 16 domínios, também na ordem do taxonomy.json.
DOMAINS: tuple[str, ...] = (
    "tecnologia",
    "educacao",
    "trabalho-negocios",
    "saude",
    "direito",
    "ciencia",
    "artes-entretenimento",
    "financas",
    "viagens",
    "culinaria",
    "esportes",
    "politica-sociedade",
    "relacionamentos-pessoal",
    "jogos",
    "linguagem-idiomas",
    "geral",
)

#: Valores aceitos em ``quality`` (1 ruim, 2 usável, 3 bom).
QUALITY_VALUES: tuple[int, ...] = (1, 2, 3)


class Lang(StrEnum):
    """Idiomas do corpus."""

    PT = "pt"
    EN = "en"


class Variant(StrEnum):
    """Variante do português (s02). ``PT_INDEF`` = detector sem confiança."""

    PT_BR = "pt-BR"
    PT_PT = "pt-PT"
    PT_INDEF = "pt-indef"


class LabelMethod(StrEnum):
    """Origem do rótulo: agente de rotulagem, classificador treinado, rótulo
    nativo da fonte mapeado, ou correção humana na interface."""

    AGENT = "agent"
    CLASSIFIER = "classifier"
    NATIVE = "native"
    MANUAL = "manual"


class License(StrEnum):
    """Licenças em uso. As strings são EXATAMENTE as de ``config/sources.toml``,
    que é a fonte de verdade — mudar uma aqui sem mudar lá quebra o export."""

    ODC_BY_1_0 = "odc-by-1.0"
    APACHE_2_0 = "apache-2.0"
    CC_BY_4_0 = "cc-by-4.0"
    CC_BY_SA_3_0 = "cc-by-sa-3.0"
    CC_BY_NC_4_0 = "cc-by-nc-4.0"
    MIT = "mit"
    LMSYS_1M = "lmsys-1m"
    UNKNOWN = "unknown"


class LicensePolicy(NamedTuple):
    """O que a licença permite fazer com o texto do prompt."""

    commercial_ok: bool
    redistributable: bool


#: Política por licença. Chaveado por ``str`` (``.value``), não pelo membro do
#: enum: ``Enum.__hash__`` usa o NOME do membro, então um dict com membros como
#: chave não seria encontrado por ``LICENSE_POLICY["mit"]``.
LICENSE_POLICY: dict[str, LicensePolicy] = {
    License.ODC_BY_1_0.value: LicensePolicy(True, True),
    License.APACHE_2_0.value: LicensePolicy(True, True),
    License.CC_BY_4_0.value: LicensePolicy(True, True),
    License.CC_BY_SA_3_0.value: LicensePolicy(True, True),
    # NonCommercial: redistribuir pode, vender o derivado não.
    License.CC_BY_NC_4_0.value: LicensePolicy(False, True),
    License.MIT.value: LicensePolicy(True, True),
    # LMSYS-Chat-1M: uso local e não comercial, redistribuição proibida.
    License.LMSYS_1M.value: LicensePolicy(False, False),
    # Sem licença identificada: nada é permitido até alguém verificar.
    License.UNKNOWN.value: LicensePolicy(False, False),
}


def license_policy(license_id: str) -> LicensePolicy:
    """Política de uma licença; licença desconhecida fecha as duas portas."""
    return LICENSE_POLICY.get(str(license_id), LicensePolicy(False, False))


class Column(NamedTuple):
    """Uma coluna canônica: nome, tipo lógico e se aceita nulo."""

    name: str
    dtype: str
    nullable: bool


#: As colunas do universo, na ordem em que aparecem no Parquet.
COLUMNS: tuple[Column, ...] = (
    # --- identidade e texto -------------------------------------------------
    Column("uid", "string", False),
    Column("text", "string", False),          # display-normalizado
    Column("text_raw", "string", False),      # cru da fonte; não vai para o SQLite
    # --- idioma (s02) -------------------------------------------------------
    Column("lang", "string", False),
    Column("lang_variant", "string", True),
    Column("variant_confidence", "float32", True),
    # --- procedência e licença ---------------------------------------------
    Column("source", "string", False),
    Column("source_id", "string", True),
    Column("source_split", "string", True),
    Column("license", "string", False),
    Column("commercial_ok", "bool", False),
    Column("redistributable", "bool", False),
    # --- rótulos (s08/s10) --------------------------------------------------
    Column("task_type", "string", True),
    Column("domain", "string", True),
    Column("quality", "int8", True),
    Column("nsfw", "bool", True),
    # --- PII (s03) ----------------------------------------------------------
    Column("pii_found", "bool", False),
    # --- dedup (s04/s06) e métricas de tamanho ------------------------------
    Column("hash_norm", "string", False),
    Column("n_exact_dups", "int32", False),
    Column("n_near_dups", "int32", False),
    Column("n_chars", "int32", False),
    Column("n_words", "int32", False),
    # --- metadados opcionais da fonte --------------------------------------
    Column("country", "string", True),
    Column("model_family", "string", True),
    Column("created_ts", "string", True),     # ISO-8601 em texto, sem fuso implícito
    Column("native_category", "string", True),
    Column("meta_json", "string", True),      # sobras da fonte, serializadas
)

#: Só os nomes, na ordem — atalho para validar DataFrames/tabelas.
COLUMN_NAMES: tuple[str, ...] = tuple(c.name for c in COLUMNS)


def arrow_schema() -> pa.Schema:
    """Schema pyarrow correspondente a ``COLUMNS`` (import lazy do pyarrow)."""
    import pyarrow as pa

    tipos = {
        "string": pa.string(),
        "bool": pa.bool_(),
        "int8": pa.int8(),
        "int32": pa.int32(),
        "float32": pa.float32(),
    }
    return pa.schema(
        [pa.field(c.name, tipos[c.dtype], nullable=c.nullable) for c in COLUMNS]
    )


def make_uid(source: str, source_id: str | None, text_raw: str) -> str:
    """Identificador estável de uma linha: 16 hex (64 bits) de sha256.

    Quando a fonte tem id próprio, ele manda — assim reingerir a mesma fonte
    reproduz os mesmos uids. Sem id, o texto cru vira a base, o que faz o mesmo
    prompt repetido dentro da fonte colapsar num uid só (de propósito).

    64 bits dá ~1e-9 de chance de colisão em 200k linhas; o UNIQUE do SQLite
    pega o caso patológico.
    """
    basis = f"{source}:{source_id}" if source_id else f"{source}:text:{text_raw}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def text_stats(text: str) -> tuple[int, int]:
    """``(n_chars, n_words)`` do texto já display-normalizado."""
    return len(text), len(text.split())


def taxonomy_path() -> Path:
    """Caminho de ``labeling/taxonomy.json``."""
    try:
        from .paths import TAXONOMY_JSON

        return TAXONOMY_JSON
    except ImportError:  # pragma: no cover - módulo copiado para fora do pacote
        return Path(__file__).resolve().parents[2] / "labeling" / "taxonomy.json"


def load_taxonomy(path: str | Path | None = None) -> dict[str, Any]:
    """Carrega e **valida** o taxonomy.json contra as constantes deste módulo.

    Confere versão, presença das seções e — o que mais importa — que as chaves
    de ``task_type`` e ``domain`` sejam exatamente ``TASK_TYPES``/``DOMAINS``,
    *na mesma ordem*. Divergência levanta ``ValueError``: é sempre erro de
    edição do JSON ou de constante esquecida, nunca algo para tolerar em
    silêncio no meio de uma campanha de rotulagem.
    """
    arquivo = Path(path) if path is not None else taxonomy_path()
    with arquivo.open(encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)

    versao = data.get("version")
    if versao != TAXONOMY_VERSION:
        raise ValueError(
            f"{arquivo}: versão da taxonomia {versao!r} != {TAXONOMY_VERSION!r} "
            "(atualize schema.TAXONOMY_VERSION junto com o JSON)"
        )
    for secao in ("task_type", "domain", "flags"):
        if secao not in data:
            raise ValueError(f"{arquivo}: seção ausente: {secao!r}")

    for secao, esperado in (("task_type", TASK_TYPES), ("domain", DOMAINS)):
        obtido = tuple(data[secao])
        if obtido != esperado:
            faltando = [k for k in esperado if k not in obtido]
            sobrando = [k for k in obtido if k not in esperado]
            detalhe = (
                f"faltando={faltando} sobrando={sobrando}"
                if (faltando or sobrando)
                else "mesmas chaves, ORDEM diferente"
            )
            raise ValueError(f"{arquivo}: {secao} diverge de schema.py ({detalhe})")
    return data


__all__ = [
    "COLUMNS",
    "COLUMN_NAMES",
    "DOMAINS",
    "LICENSE_POLICY",
    "QUALITY_VALUES",
    "TASK_TYPES",
    "TAXONOMY_VERSION",
    "Column",
    "LabelMethod",
    "Lang",
    "License",
    "LicensePolicy",
    "Variant",
    "arrow_schema",
    "license_policy",
    "load_taxonomy",
    "make_uid",
    "taxonomy_path",
    "text_stats",
]
