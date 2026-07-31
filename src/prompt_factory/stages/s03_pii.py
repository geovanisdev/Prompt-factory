"""s03 — remove dados pessoais do texto.

Entrada ``interim/lang.parquet`` → saída ``interim/scrubbed.parquet``. Nenhuma
linha é descartada aqui: o funil é 1:1 e o que muda é o conteúdo de ``text``.

Três efeitos, todos obrigatórios:

1. ``text`` passa por ``pii.scrub`` (ordem e critérios documentados lá);
2. ``pii_found`` vira ``True`` na linha que teve pelo menos uma troca;
3. a linha alterada **recalcula** ``hash_norm``, ``n_chars`` e ``n_words``. Isto
   é o ponto sutil do estágio: dois spams idênticos que só diferem no e-mail
   passam a ter o mesmo ``hash_norm`` e colapsam no dedup exato do s04 — que é
   exatamente o que queremos.

``text_raw`` fica **intacto**: é a camada de auditoria, e é dele que sai o
``uid``. Reprocessar o s03 com outro conjunto de padrões não muda nenhum uid.
"""

from __future__ import annotations

import hashlib

from .. import pii
from ..schema import arrow_schema, text_stats
from ..textnorm import norm_for_hash
from . import (
    BATCH_LEITURA,
    LANG,
    SCRUBBED,
    Cronometro,
    EscritorParquet,
    StageConfig,
    checar_colunas,
    exigir,
    imprimir_funil,
    rel,
)

ESTAGIO = "s03"


def run(cfg: StageConfig) -> int:
    """Aplica o scrub de PII linha a linha. 0 = sucesso."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    relogio = Cronometro(ESTAGIO)
    cfg.preparar_dirs()
    origem = cfg.caminho(LANG)
    exigir(origem, "pf run s02")
    destino = cfg.caminho(SCRUBBED)

    schema = arrow_schema()
    idx = {nome: schema.get_field_index(nome) for nome in
           ("text", "pii_found", "hash_norm", "n_chars", "n_words")}

    arquivo = pq.ParquetFile(origem)
    checar_colunas(arquivo.schema_arrow, rel(origem))

    lidas = alteradas = 0
    por_tipo: dict[str, int] = dict.fromkeys(pii.PII_TIPOS, 0)

    with EscritorParquet(destino, schema=schema) as escritor:
        for lote in arquivo.iter_batches(batch_size=BATCH_LEITURA):
            lidas += lote.num_rows
            textos = lote.column("text").to_pylist()
            achados = lote.column("pii_found").to_pylist()
            hashes = lote.column("hash_norm").to_pylist()
            n_chars = lote.column("n_chars").to_pylist()
            n_words = lote.column("n_words").to_pylist()

            for i, texto in enumerate(textos):
                limpo, contagens = pii.scrub(texto or "")
                if not contagens:
                    continue
                alteradas += 1
                for tipo, n in contagens.items():
                    por_tipo[tipo] += n
                textos[i] = limpo
                achados[i] = True
                hashes[i] = hashlib.sha256(
                    norm_for_hash(limpo).encode("utf-8")
                ).hexdigest()
                n_chars[i], n_words[i] = text_stats(limpo)

            tabela = pa.Table.from_batches([lote])
            for nome, valores, tipo in (
                ("text", textos, pa.string()),
                ("pii_found", achados, pa.bool_()),
                ("hash_norm", hashes, pa.string()),
                ("n_chars", n_chars, pa.int32()),
                ("n_words", n_words, pa.int32()),
            ):
                tabela = tabela.set_column(
                    idx[nome], schema.field(idx[nome]), pa.array(valores, type=tipo)
                )
            escritor.escrever(tabela.cast(schema))

    total_trocas = sum(por_tipo.values())
    imprimir_funil(
        ESTAGIO,
        ("tipo", "trocas"),
        [[tipo, por_tipo[tipo]] for tipo in pii.PII_TIPOS],
        ["TOTAL", total_trocas],
    )
    pct = (100.0 * alteradas / lidas) if lidas else 0.0
    print(
        f"[{ESTAGIO}] lidas {lidas} -> saída {lidas} (nenhuma descartada); "
        f"{alteradas} linhas alteradas ({pct:.2f}%)"
    )
    print(f"[{ESTAGIO}] {lidas} linhas -> {rel(destino)}")
    relogio.fim()
    return 0


__all__ = ["ESTAGIO", "run"]
