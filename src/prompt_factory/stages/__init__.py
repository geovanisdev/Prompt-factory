"""Estágios da pipeline (s01-s06) e o contrato que a CLI enxerga.

``cli.py`` importa **só** ``STAGES`` e ``StageConfig`` daqui. Nenhum estágio é
importado no topo deste módulo: cada entrada de ``STAGES`` é um despachante que
faz o import na hora de rodar, senão um ``pf --help`` pagaria por torch,
sentence-transformers e lingua.

Contrato de um estágio::

    def run(cfg: StageConfig) -> int   # 0 = sucesso

Regras que todos seguem:

* leem e escrevem **Parquet imutável** dentro de ``cfg.data_dir``, nunca fora;
* escrita atômica (``.tmp`` + ``os.replace``), compressão zstd, schema EXATO de
  ``schema.arrow_schema()`` conferido na entrada e na saída;
* a cadeia canônica é sempre ``text = norm_display(text_raw)`` →
  ``hash_norm = sha256(norm_for_hash(text))`` → ``uid = make_uid(...)`` →
  ``n_chars, n_words = text_stats(text)``, a mesma de ``db._insert_check_row``;
* imprimem funil (lidas → saída, com o motivo de cada queda) e duração;
* são **idempotentes**: rodar de novo com a mesma entrada dá o mesmo arquivo.

``StageConfig.data_dir`` existe para o smoke test: apontando para um diretório
descartável, a pipeline inteira roda em miniatura sem encostar em ``data/``.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import paths
from ..schema import COLUMN_NAMES, arrow_schema

if TYPE_CHECKING:  # pragma: no cover
    import pyarrow as pa

#: Nomes dos arquivos de cada etapa, relativos a ``cfg.data_dir``.
NORMALIZED = "interim/normalized.parquet"
LANG = "interim/lang.parquet"
SCRUBBED = "interim/scrubbed.parquet"
DEDUP1 = "interim/dedup1.parquet"
MAPA_EXATO = "interim/dedup_exact_map.parquet"
EMBEDDINGS = "emb/embeddings.f16.npy"
EMB_UIDS = "emb/uids.txt"
EMB_SIDECAR = "emb/progress.json"
UNIVERSE = "final/universe.parquet"
MAPA_PROXIMO = "final/dedup_near_map.parquet"
UNIVERSE_EMB = "emb/universe.f16.npy"
UNIVERSE_UIDS = "emb/universe_uids.txt"
SEED_LABELS = "final/seed_labels.parquet"
LABELED = "final/labeled.parquet"
DB_SQLITE = "db/prompts.sqlite"
DB_BUILD_SQLITE = "db/prompts.build.sqlite"

#: Outros nomes que a saída do s10 já teve em projeto. O s11 aceita os dois: o
#: desfecho de não aceitar é uma carga que roda inteira sem rótulo nenhum e só
#: denuncia o engano no teto de ``allow_unlabeled_pct``.
LABELED_ALIASES: tuple[str, ...] = ("final/labels_all.parquet",)

#: Saídas do s07 — relativas a ``cfg.labeling_dir``, não a ``cfg.data_dir``: a
#: semente é insumo da campanha de rotulagem (versionável em espírito, revisável
#: à mão), não mais um estágio intermediário do funil de dados.
SEED_PARQUET = "seed/seed.parquet"
SEED_STRATA = "seed/strata.txt"

#: Linhas por bloco na leitura dos parquets (compromisso memória x chamadas).
BATCH_LEITURA = 16_384


@dataclass(frozen=True)
class StageConfig:
    """Parâmetros que a CLI passa para qualquer estágio.

    ``max_rows`` é aplicado **só pelo s01**, e por fonte: os estágios seguintes
    processam tudo o que receberem, senão o funil deixaria de fechar.

    ``labeling_dir`` é o par de ``data_dir`` para o s07/s08: redirecionado, a
    campanha inteira (semente, lotes, manifest, rótulos) roda dentro de um
    ``tmp_path`` sem encostar em ``labeling/`` de verdade.
    """

    data_dir: Path = paths.DATA
    max_rows: int | None = None
    labeling_dir: Path = paths.LABELING
    #: Só o s07 lê: reescrever a semente APAGA uma campanha em andamento, e isso
    #: nunca pode acontecer por descuido de quem repetiu um comando.
    force: bool = False
    #: Só o s08 lê (``pf merge-labels --strict``): falha em vez de avisar quando
    #: um lote está done sem arquivo de rótulos.
    strict: bool = False
    #: --- só o s11 lê (``pf load-db``) -------------------------------------
    #: Teto de linhas sem ``task_type`` (em %). ``None`` = o de
    #: ``[loaddb] allow_unlabeled_pct``.
    allow_unlabeled_pct: float | None = None
    #: Trocar o banco vivo pelo recém-construído ao final (``--no-swap`` desliga).
    swap: bool = True
    #: Só a troca, sem construir nada: é o retry de um swap recusado por lock.
    swap_only: bool = False
    #: ``.bak`` do banco anterior antes de trocar.
    backup: bool = False
    #: ``PRAGMA integrity_check`` (varre tudo) no lugar do ``quick_check``.
    deep: bool = False

    def caminho(self, relativo: str) -> Path:
        return self.data_dir / relativo

    def caminho_labeling(self, relativo: str) -> Path:
        return self.labeling_dir / relativo

    @property
    def raw(self) -> Path:
        return self.data_dir / "raw"

    def preparar_dirs(self) -> None:
        """Cria a árvore de trabalho dentro de ``data_dir`` (idempotente)."""
        for sub in ("raw", "interim", "final", "emb", "models", "db", "exports"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# utilidades compartilhadas pelos estágios
# ---------------------------------------------------------------------------


def rel(caminho: Path) -> str:
    """Caminho relativo à raiz do repo, para caber numa linha de log."""
    try:
        return caminho.relative_to(paths.ROOT).as_posix()
    except ValueError:
        return str(caminho)


def substituir(tmp: Path, destino: Path) -> None:
    """``os.replace`` com uma tentativa extra — no Windows o destino pode estar
    com handle aberto por um instante (antivírus, explorer, um `pf report` que
    acabou de fechar)."""
    try:
        os.replace(tmp, destino)
    except PermissionError:  # pragma: no cover - depende do SO e do timing
        time.sleep(0.5)
        os.replace(tmp, destino)


def exigir(caminho: Path, comando: str) -> None:
    """Falha com instrução de qual estágio rodar antes."""
    if not caminho.is_file():
        raise SystemExit(f"[pf] insumo ausente: {rel(caminho)} — rode `{comando}` antes")


def checar_colunas(tabela: pa.Table | pa.RecordBatch | pa.Schema, origem: str) -> None:
    """Confere que as colunas são EXATAMENTE as canônicas, na ordem.

    Aceita tabela, record batch ou o próprio ``Schema`` (é o que
    ``ParquetFile.schema_arrow`` devolve, e é o caminho mais barato: confere
    antes de ler qualquer linha).
    """
    nomes = tuple(getattr(tabela, "schema", tabela).names)
    if nomes != COLUMN_NAMES:
        faltando = [c for c in COLUMN_NAMES if c not in nomes]
        sobrando = [c for c in nomes if c not in COLUMN_NAMES]
        detalhe = (
            f"faltando={faltando} sobrando={sobrando}"
            if (faltando or sobrando)
            else "mesmas colunas, ORDEM diferente"
        )
        raise ValueError(f"{origem}: schema fora do contrato ({detalhe})")


def cast_canonico(tabela: pa.Table, origem: str = "tabela") -> pa.Table:
    """Confere nomes/ordem e converte os tipos para ``schema.arrow_schema()``."""
    checar_colunas(tabela, origem)
    return tabela.cast(arrow_schema())


class EscritorParquet:
    """``ParquetWriter`` em ``.tmp`` que só vira o arquivo final no ``close()``.

    Ctrl+C no meio nunca deixa um parquet truncado no lugar do bom.
    """

    def __init__(self, destino: Path, schema: pa.Schema | None = None) -> None:
        self.destino = destino
        self._schema = schema
        self._tmp = destino.parent / f"{destino.name}.tmp"
        self._writer: Any = None
        self.linhas = 0

    def __enter__(self) -> EscritorParquet:
        import pyarrow.parquet as pq

        self.destino.parent.mkdir(parents=True, exist_ok=True)
        self._writer = pq.ParquetWriter(
            self._tmp, self._schema or arrow_schema(), compression="zstd"
        )
        return self

    def escrever(self, tabela: pa.Table) -> None:
        if tabela.num_rows == 0:
            return
        self._writer.write_table(tabela)
        self.linhas += tabela.num_rows

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        if exc_type is not None:
            self._tmp.unlink(missing_ok=True)
            return
        substituir(self._tmp, self.destino)


def escrever_tabela(tabela: pa.Table, destino: Path, schema: pa.Schema | None = None) -> Path:
    """Grava uma tabela inteira, atômica e comprimida."""
    with EscritorParquet(destino, schema=schema or tabela.schema) as w:
        w.escrever(tabela)
    return destino


def imprimir_funil(
    estagio: str,
    colunas: Sequence[str],
    linhas: Iterable[Sequence[Any]],
    total: Sequence[Any] | None = None,
) -> None:
    """Tabela de funil alinhada, com TOTAL opcional na última linha."""
    corpo = [[str(c) for c in linha] for linha in linhas]
    if total is not None:
        corpo.append([str(c) for c in total])
    larguras = [len(c) for c in colunas]
    for linha in corpo:
        for i, celula in enumerate(linha):
            larguras[i] = max(larguras[i], len(celula))
    cabecalho = "  ".join(c.ljust(larguras[i]) for i, c in enumerate(colunas))
    print(f"[{estagio}] {cabecalho}")
    print(f"[{estagio}] {'  '.join('-' * w for w in larguras)}")
    for i, linha in enumerate(corpo):
        if total is not None and i == len(corpo) - 1:
            print(f"[{estagio}] {'  '.join('-' * w for w in larguras)}")
        print(f"[{estagio}] {'  '.join(c.ljust(larguras[j]) for j, c in enumerate(linha))}")


class Cronometro:
    """Marca o tempo do estágio e imprime na saída."""

    def __init__(self, estagio: str) -> None:
        self.estagio = estagio
        self.inicio = time.perf_counter()

    @property
    def segundos(self) -> float:
        return time.perf_counter() - self.inicio

    def fim(self, mensagem: str = "") -> None:
        s = self.segundos
        tempo = f"{s:.1f} s" if s < 90 else f"{s / 60:.1f} min"
        extra = f" {mensagem}" if mensagem else ""
        print(f"[{self.estagio}] concluído em {tempo}{extra}")


# ---------------------------------------------------------------------------
# despacho
# ---------------------------------------------------------------------------


def _despacho(modulo: str) -> Callable[[StageConfig], int]:
    """Fabrica o wrapper que importa o estágio só na hora de rodar."""

    def executar(cfg: StageConfig) -> int:
        from importlib import import_module

        return int(import_module(f".{modulo}", __name__).run(cfg))

    executar.__name__ = f"run_{modulo}"
    return executar


#: Nome curto → função do estágio. Único ponto de contato do ``cli.py`` com a
#: pipeline: todos seguem ``run(cfg) -> int``.
STAGES: dict[str, Callable[[StageConfig], int]] = {
    "s01": _despacho("s01_normalize"),
    "s02": _despacho("s02_lang"),
    "s03": _despacho("s03_pii"),
    "s04": _despacho("s04_dedup_exact"),
    "s05": _despacho("s05_embed"),
    "s06": _despacho("s06_dedup_near"),
    "s07": _despacho("s07_seed_sample"),
    "s08": _despacho("s08_merge_labels"),
    "s11": _despacho("s11_load_db"),
}

#: O que ``pf run`` (e ``pf run all``) encadeia.
#:
#: O s07 e o s08 estão registrados em ``STAGES``, mas **fora da cadeia**: entre
#: os dois existe uma campanha de rotulagem de horas, com revisão humana da
#: calibração no meio. Se ``all`` os incluísse, um ``pf run`` de rotina
#: regeraria a semente e apagaria o manifest de uma campanha em andamento — sem
#: perguntar nada. Cada um tem comando próprio: `pf make-seed` e `pf merge-labels`.
#:
#: O s11 fica fora pelo mesmo motivo, agravado: ele é o único estágio cuja saída
#: alguém está LENDO enquanto a pipeline roda (``pf serve``), e a última coisa
#: que faz é trocar esse arquivo. Um ``pf run`` de rotina — o comando que se
#: repete depois de mexer num threshold do s02 — publicaria um banco carregado
#: com os rótulos que estivessem por ali no momento, inclusive nenhum. Comando
#: próprio: `pf load-db`.
CADEIA: tuple[str, ...] = ("s01", "s02", "s03", "s04", "s05", "s06")

#: Descrição de uma linha para o `pf run --help` e mensagens de erro.
DESCRICOES: dict[str, str] = {
    "s01": "normaliza raw/*.parquet -> interim/normalized.parquet (27 colunas)",
    "s02": "idioma (2 camadas) + variante pt-BR/pt-PT -> interim/lang.parquet",
    "s03": "remove PII -> interim/scrubbed.parquet",
    "s04": "dedup exato por hash_norm -> interim/dedup1.parquet",
    "s05": "embeddings e5-small -> emb/embeddings.f16.npy",
    "s06": "dedup próximo par a par + recheck de idioma -> final/universe.parquet",
    "s07": "amostra-semente estratificada + lotes de rotulagem (fora da cadeia)",
    "s08": "funde rótulos de agente + nativos -> final/seed_labels.parquet (fora da cadeia)",
    "s11": "carga bulk no SQLite + rebuild do FTS + swap de arquivo (fora da cadeia)",
}


__all__ = [
    "BATCH_LEITURA",
    "CADEIA",
    "DB_BUILD_SQLITE",
    "DB_SQLITE",
    "DEDUP1",
    "DESCRICOES",
    "EMBEDDINGS",
    "EMB_SIDECAR",
    "EMB_UIDS",
    "LABELED",
    "LABELED_ALIASES",
    "LANG",
    "MAPA_EXATO",
    "MAPA_PROXIMO",
    "NORMALIZED",
    "SCRUBBED",
    "SEED_LABELS",
    "SEED_PARQUET",
    "SEED_STRATA",
    "STAGES",
    "UNIVERSE",
    "UNIVERSE_EMB",
    "UNIVERSE_UIDS",
    "Cronometro",
    "EscritorParquet",
    "StageConfig",
    "cast_canonico",
    "checar_colunas",
    "escrever_tabela",
    "exigir",
    "imprimir_funil",
    "rel",
    "substituir",
]
