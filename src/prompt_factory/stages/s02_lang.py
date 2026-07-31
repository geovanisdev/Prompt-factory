"""s02 — decide o idioma em duas camadas e classifica a variante do português.

Entrada ``interim/normalized.parquet`` → saída ``interim/lang.parquet``, com
``lang`` definitivo (só ``pt`` ou ``en`` sobrevivem), ``lang_variant`` e
``variant_confidence`` preenchidos para o português.

**Por que duas camadas.** O rótulo da fonte erra muito: no WildChat, o mesmo
template automatizado em francês aparece 7.553x como ``English`` e 2.049x como
``Portuguese``. Um detector só também erra, principalmente em texto curto e em
prompt com código no meio. Então:

* **camada 1** (fastText, ``langdetect.detect1``) roda em todas as linhas e é
  barata;
* **camada 2** (lingua, ``langdetect.arbitrar``) é o **árbitro**, cara, e só
  entra quando há motivo: confiança baixa, texto curto, ou desacordo com o que a
  fonte declarou.

**Tabela-verdade.** Com ``b1`` = balde da camada 1 (``pt``/``en``/``other``),
``s1`` = confiança dela e ``lang_src`` = o que veio do s01 (``pt``, ``en`` ou
``und``)::

    gatilho = s1 < arbiter_conf  ou  len(text) < min_chars
              ou  (lang_src ∈ {pt,en} e b1 ≠ lang_src)

    sem gatilho          → final = b1
    com gatilho          → final = b1 se b1 == balde(árbitro), senão NADA

    mantém  ⇔  final ∈ {pt, en}

Consequências que valem registrar: as duas camadas **concordando** podem migrar
uma linha de pt para en (e vice-versa) contra o rótulo da fonte — é o caso mais
comum de correção real e é contado à parte no funil. Qualquer discordância entre
camada 1 e árbitro descarta a linha: preferimos perder 3% de corpus a plantar
espanhol dentro do português. E ``other`` com confiança alta cai direto, sem
gastar árbitro.

A variante (``variant_ptbr.classify_variant``) só roda no português, sobre os
primeiros ``VARIANT_MAX_CHARS`` caracteres: as pistas de variante aparecem cedo
e o corpus tem prompts de até 1 milhão de caracteres.
"""

from __future__ import annotations

from typing import Any

from .. import config, langdetect
from ..schema import arrow_schema
from ..variant_ptbr import classify_variant
from . import (
    BATCH_LEITURA,
    LANG,
    NORMALIZED,
    Cronometro,
    EscritorParquet,
    StageConfig,
    checar_colunas,
    exigir,
    imprimir_funil,
    rel,
)

ESTAGIO = "s02"

#: Baldes de decisão: tudo que não é pt/en é "other".
OUTRO = "other"

#: Janela do classificador de variante (ver docstring do módulo).
VARIANT_MAX_CHARS = 20_000


def _balde(lang: str) -> str:
    return lang if lang in ("pt", "en") else OUTRO


def run(cfg: StageConfig) -> int:
    """Resolve idioma e variante. 0 = sucesso."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    relogio = Cronometro(ESTAGIO)
    cfg.preparar_dirs()
    origem = cfg.caminho(NORMALIZED)
    exigir(origem, "pf run s01")
    destino = cfg.caminho(LANG)

    arbiter_conf = float(config.get("langid", "arbiter_conf", default=0.65))
    min_chars = int(config.get("langid", "min_chars", default=25))
    schema = arrow_schema()
    i_lang = schema.get_field_index("lang")
    i_variant = schema.get_field_index("lang_variant")
    i_conf = schema.get_field_index("variant_confidence")

    arquivo = pq.ParquetFile(origem)
    checar_colunas(arquivo.schema_arrow, rel(origem))

    lidas = arbitragens = 0
    mantidas = {"pt": 0, "en": 0}
    migradas = {"pt->en": 0, "en->pt": 0}
    resolvidas = 0  # lang_src era "und" e virou pt/en
    drop_discordancia = drop_outro = 0
    variantes: dict[str, int] = {"pt-BR": 0, "pt-PT": 0, "pt-indef": 0}

    print(f"[{ESTAGIO}] camada 1: fast-langdetect (modelo {langdetect.modelo_ativo()!r})")
    print(f"[{ESTAGIO}] árbitro:  lingua {'/'.join(langdetect.IDIOMAS_ARBITRO)}")

    with EscritorParquet(destino, schema=schema) as escritor:
        for lote in arquivo.iter_batches(batch_size=BATCH_LEITURA):
            lidas += lote.num_rows
            textos = lote.column("text").to_pylist()
            langs_src = lote.column("lang").to_pylist()
            paises = lote.column("country").to_pylist()

            deteccoes = [langdetect.detect1(t or "") for t in textos]
            precisa: list[int] = []
            for i, (rotulo, score) in enumerate(deteccoes):
                b1 = _balde(rotulo)
                origem_i = langs_src[i] or ""
                if (
                    score < arbiter_conf
                    or len(textos[i] or "") < min_chars
                    or (origem_i in ("pt", "en") and b1 != origem_i)
                ):
                    precisa.append(i)
            arbitragens += len(precisa)
            veredito: dict[int, str] = {}
            if precisa:
                for i, (rotulo, _score) in zip(
                    precisa, langdetect.arbitrar([textos[i] or "" for i in precisa]), strict=True
                ):
                    veredito[i] = _balde(rotulo)

            manter: list[int] = []
            langs_out: list[str] = []
            variantes_out: list[str | None] = []
            confs_out: list[float | None] = []
            for i, (rotulo, _score) in enumerate(deteccoes):
                b1 = _balde(rotulo)
                if i in veredito:
                    final = b1 if b1 == veredito[i] else None
                else:
                    final = b1
                if final is None:
                    drop_discordancia += 1
                    continue
                if final == OUTRO:
                    drop_outro += 1
                    continue
                origem_i = langs_src[i] or ""
                if origem_i in ("pt", "en") and origem_i != final:
                    migradas[f"{origem_i}->{final}"] += 1
                elif origem_i not in ("pt", "en"):
                    resolvidas += 1
                mantidas[final] += 1
                manter.append(i)
                langs_out.append(final)
                if final == "pt":
                    variante, conf = classify_variant(
                        (textos[i] or "")[:VARIANT_MAX_CHARS], paises[i]
                    )
                    variantes[variante] += 1
                    variantes_out.append(variante)
                    confs_out.append(conf)
                else:
                    variantes_out.append(None)
                    confs_out.append(None)

            if not manter:
                continue
            tabela = pa.Table.from_batches([lote]).take(pa.array(manter, type=pa.int32()))
            tabela = tabela.set_column(
                i_lang, schema.field(i_lang), pa.array(langs_out, type=pa.string())
            )
            tabela = tabela.set_column(
                i_variant, schema.field(i_variant), pa.array(variantes_out, type=pa.string())
            )
            tabela = tabela.set_column(
                i_conf, schema.field(i_conf), pa.array(confs_out, type=pa.float32())
            )
            escritor.escrever(tabela.cast(schema))

    saida = mantidas["pt"] + mantidas["en"]
    linhas: list[list[Any]] = [
        ["lidas", lidas],
        ["arbitragens", arbitragens],
        ["mantidas pt", mantidas["pt"]],
        ["mantidas en", mantidas["en"]],
        ["migradas pt->en", migradas["pt->en"]],
        ["migradas en->pt", migradas["en->pt"]],
        ["resolvidas (und -> pt/en)", resolvidas],
        ["drop discordância", drop_discordancia],
        ["drop outro idioma", drop_outro],
    ]
    imprimir_funil(ESTAGIO, ("etapa", "linhas"), linhas, ["SAÍDA", saida])
    imprimir_funil(
        ESTAGIO,
        ("variante", "linhas", "% do pt"),
        [
            [k, v, f"{(100.0 * v / mantidas['pt']) if mantidas['pt'] else 0.0:.1f}%"]
            for k, v in variantes.items()
        ],
    )
    print(f"[{ESTAGIO}] {saida} linhas -> {rel(destino)}")
    relogio.fim()
    return 0


__all__ = ["ESTAGIO", "OUTRO", "VARIANT_MAX_CHARS", "run"]
