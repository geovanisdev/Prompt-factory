"""s01 — normaliza ``data/raw/*.parquet`` para as 27 colunas canônicas.

Entrada: um parquet por fonte, 12 colunas cruas (``ingest/base.RAW_SCHEMA``).
Saída: ``interim/normalized.parquet``, schema de ``schema.arrow_schema()``.

O que este estágio faz — e, tão importante quanto, o que **não** faz:

* aplica a cadeia canônica (``norm_display`` → ``hash_norm`` → ``uid`` →
  ``text_stats``) e descarta a linha que ficar sem texto;
* **deduplica uids dentro da execução**: fontes diferentes podem produzir o
  mesmo ``uid`` (o mesmo texto sem ``source_id``), e o UNIQUE do SQLite lá no
  M8 não pode ser a primeira vez que alguém descobre isso. Fica a primeira
  ocorrência, na ordem de leitura;
* **não** decide idioma. ``lang`` sai provisório: só ``"pt"``/``"en"`` **exatos**
  vindos da fonte são copiados; ``"Portuguese"``, ``"English"`` e ``"pt-BR"``
  viram ``"und"`` e vão inteiros para o s02 decidir pelos detectores. Isso é
  deliberado: são justamente as fontes que rotulam por extenso (WildChat) que
  erram feio — o mesmo template em francês aparece 7.553x como ``English`` e
  2.049x como ``Portuguese``. ``"und"`` nunca chega ao SQLite; o s02 resolve ou
  descarta.

Ordem de saída: fontes em ordem alfabética de arquivo, linhas na ordem original
de cada fonte. Rodar de novo produz exatamente a mesma sequência.

Licença: a licença viaja na linha desde o ``write_raw``. Aqui ela é **conferida**
contra ``config/sources.toml`` (divergência é erro, não aviso: significa raw
gerado com outra configuração) e ``commercial_ok``/``redistributable`` são lidos
do TOML. Se a política declarada divergir de ``schema.LICENSE_POLICY``, sai um
aviso — o TOML continua mandando, porque é ele que o jurídico revisa.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .. import config
from ..ingest.base import RAW_COLUMNS
from ..schema import COLUMN_NAMES, arrow_schema, license_policy, make_uid, text_stats
from ..textnorm import norm_display, norm_for_hash
from . import (
    BATCH_LEITURA,
    NORMALIZED,
    Cronometro,
    EscritorParquet,
    StageConfig,
    imprimir_funil,
    rel,
)

ESTAGIO = "s01"

#: Idiomas que a fonte pode declarar e nós aceitamos como definitivos.
LANGS_ACEITOS: frozenset[str] = frozenset({"pt", "en"})

#: Marcador de "idioma ainda não decidido" (resolvido no s02).
LANG_INDEFINIDO = "und"

#: Parquets de ``data/raw/`` que NÃO são fontes: o ``wildchat_en_pool`` é o pool
#: intermediário de onde sai o ``wildchat_en`` de 100k (ingerir os dois
#: duplicaria 158 mil linhas). Sufixo, não nome fixo, porque o M3 pode gerar
#: outros pools.
SUFIXOS_IGNORADOS: tuple[str, ...] = ("_pool",)


def _limpar_nulo(valor: Any) -> str | None:
    """Coluna opcional: string vazia do raw vira NULL no canônico."""
    if valor is None:
        return None
    texto = str(valor)
    return texto if texto else None


def _meta(meta_json: str, nsfw_hint: bool) -> str:
    """Re-serializa o ``meta_json`` da fonte, dobrando o ``nsfw_hint`` para dentro.

    O raw guarda ``nsfw_hint`` como coluna própria; o canônico não tem essa
    coluna (tem ``nsfw``, que é rótulo do M7/M10, coisa diferente). Perder a
    dica da fonte seria perder informação, então ela entra no meta.
    """
    try:
        dados = json.loads(meta_json) if meta_json else {}
    except (json.JSONDecodeError, TypeError):
        dados = {"_raw_meta": meta_json}
    if not isinstance(dados, dict):
        dados = {"_raw_meta": meta_json}
    if nsfw_hint:
        dados["nsfw_hint"] = True
    if not dados:
        return "{}"
    return json.dumps(dados, ensure_ascii=False, separators=(",", ":"))


def _fontes(cfg: StageConfig) -> list[Path]:
    """Parquets de fonte em ``raw/``, em ordem alfabética, sem os intermediários."""
    arquivos = []
    for caminho in sorted(cfg.raw.glob("*.parquet")):
        if caminho.stem.endswith(SUFIXOS_IGNORADOS):
            continue
        arquivos.append(caminho)
    return arquivos


def _conferir_licenca(fonte: str, spec: dict[str, Any], licencas: set[str]) -> None:
    esperada = str(spec.get("license", "unknown"))
    diferentes = {lic for lic in licencas if lic != esperada}
    if diferentes:
        raise SystemExit(
            f"[{ESTAGIO}] {fonte}: licença do raw {sorted(diferentes)} != "
            f"{esperada!r} do sources.toml — regenere o raw (`pf ingest {fonte}`)"
        )
    politica = license_policy(esperada)
    declarada = (bool(spec.get("commercial_ok", False)), bool(spec.get("redistributable", False)))
    if declarada != (politica.commercial_ok, politica.redistributable):
        print(
            f"[{ESTAGIO}] AVISO {fonte}: sources.toml declara "
            f"commercial_ok={declarada[0]}/redistributable={declarada[1]} mas "
            f"LICENSE_POLICY[{esperada!r}] diz {politica.commercial_ok}/"
            f"{politica.redistributable} — vale o TOML, confira quem está errado"
        )


def run(cfg: StageConfig) -> int:
    """Normaliza todas as fontes de ``raw/``. 0 = sucesso."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    relogio = Cronometro(ESTAGIO)
    cfg.preparar_dirs()
    arquivos = _fontes(cfg)
    if not arquivos:
        print(f"[{ESTAGIO}] nenhum parquet em {rel(cfg.raw)} — rode `pf ingest` antes")
        return 2

    conhecidas = config.sources()
    destino = cfg.caminho(NORMALIZED)
    schema = arrow_schema()
    vistos: set[str] = set()
    funil: list[list[Any]] = []
    totais = [0, 0, 0, 0]

    with EscritorParquet(destino, schema=schema) as escritor:
        for caminho in arquivos:
            fonte = caminho.stem
            if fonte not in conhecidas:
                raise SystemExit(
                    f"[{ESTAGIO}] {rel(caminho)}: fonte {fonte!r} não existe em "
                    f"config/sources.toml (conhecidas: {', '.join(sorted(conhecidas))})"
                )
            spec = conhecidas[fonte]
            arquivo = pq.ParquetFile(caminho)
            if tuple(arquivo.schema_arrow.names) != RAW_COLUMNS:
                raise SystemExit(
                    f"[{ESTAGIO}] {rel(caminho)}: esperava as 12 colunas do RAW_SCHEMA, "
                    f"veio {tuple(arquivo.schema_arrow.names)}"
                )
            comercial = bool(spec.get("commercial_ok", False))
            redistribuivel = bool(spec.get("redistributable", False))

            lidas = vazias = uid_dup = saida = 0
            licencas_vistas: set[str] = set()
            for lote in arquivo.iter_batches(batch_size=BATCH_LEITURA):
                if cfg.max_rows is not None:
                    restante = cfg.max_rows - lidas
                    if restante <= 0:
                        break
                    if lote.num_rows > restante:
                        lote = lote.slice(0, restante)
                lidas += lote.num_rows
                licencas_vistas.update(
                    v for v in pc.unique(lote.column("license")).to_pylist() if v is not None
                )

                col = {nome: lote.column(nome).to_pylist() for nome in RAW_COLUMNS}
                dados: dict[str, list[Any]] = {nome: [] for nome in COLUMN_NAMES}
                for i in range(lote.num_rows):
                    bruto = col["text_raw"][i] or ""
                    texto = norm_display(bruto)
                    if not texto:
                        vazias += 1
                        continue
                    source_id = _limpar_nulo(col["source_id"][i])
                    uid = make_uid(fonte, source_id, bruto)
                    if uid in vistos:
                        uid_dup += 1
                        continue
                    vistos.add(uid)
                    n_chars, n_words = text_stats(texto)
                    lang_fonte = str(col["lang_source"][i] or "")
                    dados["uid"].append(uid)
                    dados["text"].append(texto)
                    dados["text_raw"].append(bruto)
                    dados["lang"].append(
                        lang_fonte if lang_fonte in LANGS_ACEITOS else LANG_INDEFINIDO
                    )
                    dados["lang_variant"].append(None)
                    dados["variant_confidence"].append(None)
                    dados["source"].append(fonte)
                    dados["source_id"].append(source_id)
                    dados["source_split"].append(_limpar_nulo(col["source_split"][i]))
                    dados["license"].append(str(col["license"][i] or ""))
                    dados["commercial_ok"].append(comercial)
                    dados["redistributable"].append(redistribuivel)
                    dados["task_type"].append(None)
                    dados["domain"].append(None)
                    dados["quality"].append(None)
                    dados["nsfw"].append(None)
                    dados["pii_found"].append(False)
                    dados["hash_norm"].append(
                        hashlib.sha256(norm_for_hash(texto).encode("utf-8")).hexdigest()
                    )
                    dados["n_exact_dups"].append(0)
                    dados["n_near_dups"].append(0)
                    dados["n_chars"].append(n_chars)
                    dados["n_words"].append(n_words)
                    dados["country"].append(_limpar_nulo(col["country"][i]))
                    dados["model_family"].append(_limpar_nulo(col["model_family"][i]))
                    dados["created_ts"].append(_limpar_nulo(col["created_ts"][i]))
                    dados["native_category"].append(_limpar_nulo(col["native_category"][i]))
                    dados["meta_json"].append(
                        _meta(col["meta_json"][i] or "", bool(col["nsfw_hint"][i]))
                    )
                    saida += 1
                escritor.escrever(pa.table(dados, schema=schema))

            _conferir_licenca(fonte, spec, licencas_vistas)
            funil.append([fonte, lidas, vazias, uid_dup, saida])
            for j, v in enumerate((lidas, vazias, uid_dup, saida)):
                totais[j] += v

    imprimir_funil(
        ESTAGIO,
        ("fonte", "lidas", "vazias", "uid_dup", "saída"),
        funil,
        ["TOTAL", *totais],
    )
    print(f"[{ESTAGIO}] {totais[3]} linhas -> {rel(destino)}")
    relogio.fim()
    return 0


__all__ = ["ESTAGIO", "LANGS_ACEITOS", "LANG_INDEFINIDO", "run"]
