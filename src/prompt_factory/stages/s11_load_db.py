"""s11 — carrega o universo rotulado num SQLite novo e troca por swap de arquivo.

Entradas: ``final/universe.parquet`` (obrigatório, as 27 colunas canônicas) mais
os dois parquets de rótulo, ambos opcionais — ``final/labeled.parquet`` (s10, a
predição do classificador) e ``final/seed_labels.parquet`` (s08, a campanha).
Saída: ``db/prompts.sqlite``, construído inteiro em ``db/prompts.build.sqlite`` e
trocado no fim com um ``os.replace``.

**Por que construir ao lado.** O banco é o único artefato que alguém abre
enquanto a pipeline roda (``pf serve``). Carregar 200 mil linhas por cima do
banco vivo deixaria a interface lendo um estado meio velho meio novo por
minutos; ``os.replace`` é atômico, então quem estiver lendo continua no arquivo
antigo até fechar e o próximo a abrir já pega o novo, inteiro.

**Precedência dos rótulos.** ``labeled`` cobre todo mundo; por cima dele,
``seed_labels`` com ``label_method`` ``manual``/``agent`` **vence**, porque
rótulo observado (alguém leu o texto) ganha de rótulo inferido — e porque o
classificador foi TREINADO nessas linhas: deixá-lo sobrescrevê-las fecharia um
laço em que o modelo audita a si mesmo. Rótulo ``native`` do seed não vence a
predição (é um mapeamento grosseiro de categoria da fonte, feito sem ler o
texto), mas entra quando não há predição nenhuma — melhor que NULL. Rótulo
humano grava ``label_confidence`` NULL: confiança de humano não é probabilidade,
e um 1.0 inventado poluiria a fila de revisão ordenada por essa coluna.

Ordem da carga, que não é arbitrária:

1. índices e triggers **derrubados** antes do primeiro INSERT;
2. ``BEGIN`` explícito (a conexão do projeto é autocommit: sem ele cada lote de
   10 mil linhas viraria uma transação com fsync);
3. FTS populado de uma vez com ``'rebuild'`` — indexar linha a linha pelo
   trigger é ordens de grandeza mais lento, e o comando ``'delete'`` do FTS
   externo exige que ``old.text`` seja EXATAMENTE o texto indexado: qualquer
   divergência corrompe o índice **em silêncio**;
4. ``executescript(DDL)`` recria os onze índices e os três triggers de uma vez
   (todo o DDL é ``IF NOT EXISTS``: tabelas viram no-op, dados permanecem).

**Nunca passe escalar numpy para o sqlite3.** Ele aceita o buffer protocol e
grava **BLOB** sem reclamar; as colunas com CHECK explodem na hora, mas
``n_chars``, ``n_words``, ``variant_confidence``, ``label_confidence`` e os
contadores de duplicata não têm CHECK e passam podres — e um BLOB compara maior
que qualquer INTEGER, então até ``n_chars > 100`` continua devolvendo TRUE.
Daí toda linha sair de ``RecordBatch.to_pydict()``, que devolve ``int``/
``float``/``None`` nativos.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import db as dbmod
from .. import paths
from ..config import get, settings
from ..schema import TAXONOMY_VERSION, license_policy
from . import (
    DB_BUILD_SQLITE,
    DB_SQLITE,
    LABELED,
    LABELED_ALIASES,
    SEED_LABELS,
    UNIVERSE,
    Cronometro,
    StageConfig,
    exigir,
    imprimir_funil,
    rel,
)

ESTAGIO = "s11"

#: Código de saída quando o swap é recusado — distinto de 1 (erro de carga) para
#: que um script saiba que o banco NOVO está pronto e só falta trocar.
EXIT_SWAP_BLOQUEADO = 3

#: Colunas do universo que vão para o SQLite. ``text_raw`` é a única que fica de
#: fora: ela é a camada de auditoria e mora no Parquet.
COLUNAS_UNIVERSO: tuple[str, ...] = (
    "uid",
    "text",
    "lang",
    "lang_variant",
    "variant_confidence",
    "source",
    "source_id",
    "source_split",
    "license",
    "commercial_ok",
    "redistributable",
    "task_type",
    "domain",
    "quality",
    "nsfw",
    "pii_found",
    "hash_norm",
    "n_exact_dups",
    "n_near_dups",
    "n_chars",
    "n_words",
    "country",
    "model_family",
    "created_ts",
    "native_category",
    "meta_json",
)

#: Colunas do INSERT, na ordem. ``id``/``ingested_at``/``updated_at`` ficam de
#: fora para os DEFAULT do DDL responderem.
COLUNAS_INSERT: tuple[str, ...] = (
    "uid",
    "text",
    "text_original",
    "edited",
    "lang",
    "lang_variant",
    "variant_confidence",
    "source",
    "source_id",
    "source_split",
    "license",
    "commercial_ok",
    "redistributable",
    "task_type",
    "domain",
    "quality",
    "nsfw",
    "pii_found",
    "label_method",
    "label_confidence",
    "needs_review",
    "hash_norm",
    "n_exact_dups",
    "n_near_dups",
    "n_chars",
    "n_words",
    "country",
    "model_family",
    "created_ts",
    "native_category",
    "meta_json",
)

#: Métodos de rotulagem que vencem a predição do modelo: alguém leu o texto.
METODOS_OBSERVADOS: frozenset[str] = frozenset({"manual", "agent"})


# ---------------------------------------------------------------------------
# configuração
# ---------------------------------------------------------------------------


def teto_sem_rotulo(override: float | None = None) -> float:
    """Percentual máximo de linhas sem ``task_type`` (``[loaddb]``)."""
    if override is not None:
        return float(override)
    return float(get("loaddb", "allow_unlabeled_pct", default=1.0))


def _inteiro(secao: str, chave: str, padrao: int) -> int:
    return int(get(secao, chave, default=padrao))


# ---------------------------------------------------------------------------
# rótulos
# ---------------------------------------------------------------------------


class Rotulo:
    """Um rótulo resolvido, pronto para virar colunas do INSERT."""

    __slots__ = ("confidence", "domain", "method", "needs_review", "nsfw", "quality", "task_type")

    def __init__(
        self,
        task_type: str | None,
        domain: str | None,
        quality: int | None,
        nsfw: bool | None,
        method: str | None,
        confidence: float | None,
        needs_review: bool,
    ) -> None:
        self.task_type = task_type
        self.domain = domain
        self.quality = quality
        self.nsfw = nsfw
        self.method = method
        self.confidence = confidence
        self.needs_review = needs_review


def _achar_labeled(cfg: StageConfig) -> Path | None:
    """``final/labeled.parquet`` ou um dos nomes alternativos do s10.

    O M7 ainda não existe; o nome do arquivo dele foi discutido como
    ``labeled.parquet`` e como ``labels_all.parquet``. Aceitar os dois evita o
    pior desfecho possível — uma carga que roda inteira, não acha rótulo nenhum
    e só denuncia o erro no teto de ``allow_unlabeled_pct``.
    """
    for relativo in (LABELED, *LABELED_ALIASES):
        caminho = cfg.caminho(relativo)
        if caminho.is_file():
            return caminho
    return None


def _coluna(tabela: Any, nome: str, padrao: Any = None) -> list[Any]:
    """Coluna como lista Python, ou uma lista de ``padrao`` se ela não existe."""
    if nome in tabela.column_names:
        return list(tabela.column(nome).to_pylist())
    return [padrao] * tabela.num_rows


def carregar_rotulos(
    labeled: Path | None, seed: Path | None
) -> tuple[dict[str, Rotulo], Counter[str]]:
    """Funde os dois parquets de rótulo num ``dict`` ``uid -> Rotulo``.

    Cabe em RAM de sobra (~200 mil entradas). O universo, esse sim, é lido em
    streaming: iterar a fonte da verdade e buscar o rótulo aqui é o que garante
    que nenhuma linha do universo suma por falta de rótulo.
    """
    import pyarrow.parquet as pq

    stats: Counter[str] = Counter()
    rotulos: dict[str, Rotulo] = {}

    if labeled is not None:
        tabela = pq.read_table(labeled)
        if "uid" not in tabela.column_names:
            raise SystemExit(f"[{ESTAGIO}] {rel(labeled)}: sem coluna 'uid'")
        uids = tabela.column("uid").to_pylist()
        colunas = {
            nome: _coluna(tabela, nome)
            for nome in ("task_type", "domain", "quality", "nsfw", "label_method",
                         "label_confidence", "needs_review")
        }
        for i, uid in enumerate(uids):
            metodo = colunas["label_method"][i] or "classifier"
            revisar = colunas["needs_review"][i]
            rotulos[str(uid)] = Rotulo(
                task_type=colunas["task_type"][i],
                domain=colunas["domain"][i],
                quality=None if colunas["quality"][i] is None else int(colunas["quality"][i]),
                nsfw=None if colunas["nsfw"][i] is None else bool(colunas["nsfw"][i]),
                method=str(metodo),
                confidence=(
                    None
                    if colunas["label_confidence"][i] is None
                    else float(colunas["label_confidence"][i])
                ),
                # Sem a coluna, a política do M7 é reconstruída aqui: rótulo sem
                # task_type OU sem domain não é rótulo completo.
                needs_review=(
                    bool(revisar)
                    if revisar is not None
                    else not (colunas["task_type"][i] and colunas["domain"][i])
                ),
            )
        stats["de labeled (classificador)"] = len(rotulos)

    if seed is not None:
        tabela = pq.read_table(seed)
        uids = tabela.column("uid").to_pylist()
        colunas = {
            nome: _coluna(tabela, nome)
            for nome in ("task_type", "domain", "quality", "nsfw", "label_method")
        }
        for i, uid in enumerate(uids):
            chave = str(uid)
            metodo = str(colunas["label_method"][i] or "agent")
            observado = metodo in METODOS_OBSERVADOS
            if chave in rotulos and not observado:
                # Nativo não derruba a predição: o classificador treinou COM ele
                # e acerta mais onde o mapeamento é grosseiro.
                stats["seed native preterido (já tinha predição)"] += 1
                continue
            if chave in rotulos:
                stats["seed observado venceu a predição"] += 1
            else:
                stats[f"só do seed ({metodo})"] += 1
            rotulos[chave] = Rotulo(
                task_type=colunas["task_type"][i],
                domain=colunas["domain"][i],
                quality=None if colunas["quality"][i] is None else int(colunas["quality"][i]),
                nsfw=None if colunas["nsfw"][i] is None else bool(colunas["nsfw"][i]),
                method=metodo,
                # Rótulo humano/agente não tem probabilidade; NULL é honesto e
                # mantém a fila de revisão (ORDER BY label_confidence) limpa.
                confidence=None,
                needs_review=not (colunas["task_type"][i] and colunas["domain"][i]),
            )
    return rotulos, stats


# ---------------------------------------------------------------------------
# leitura do universo
# ---------------------------------------------------------------------------


def _linhas_do_lote(
    lote: Any, rotulos: dict[str, Rotulo], stats: Counter[str], divergencia: Counter[str]
) -> list[tuple[Any, ...]]:
    """Um ``RecordBatch`` do universo vira tuplas prontas para o executemany.

    ``to_pydict()`` é obrigatório: iterar sobre a coluna do pyarrow (ou sobre um
    ``ndarray``) entregaria escalares numpy ao sqlite3, que os grava como BLOB
    sem reclamar. Ver a docstring do módulo.
    """
    dados = lote.to_pydict()
    saida: list[tuple[Any, ...]] = []
    for i in range(lote.num_rows):
        uid = str(dados["uid"][i])
        rotulo = rotulos.get(uid)
        if rotulo is None:
            stats["sem rótulo (task_type NULL, needs_review=1)"] += 1
            task_type = domain = quality = nsfw = metodo = confianca = None
            revisar = 1
        else:
            task_type = rotulo.task_type
            domain = rotulo.domain
            quality = rotulo.quality
            nsfw = None if rotulo.nsfw is None else int(rotulo.nsfw)
            metodo = rotulo.method
            confianca = rotulo.confidence
            revisar = int(rotulo.needs_review)
            if task_type is None:
                stats["sem rótulo (task_type NULL, needs_review=1)"] += 1
                revisar = 1
            elif domain is None:
                stats["rótulo parcial (sem domain, needs_review=1)"] += 1
                revisar = 1

        licenca = dados["license"][i]
        politica = license_policy(str(licenca))
        comercial = bool(dados["commercial_ok"][i])
        redistribuivel = bool(dados["redistributable"][i])
        if (comercial, redistribuivel) != (politica.commercial_ok, politica.redistributable):
            # A licença viaja POR LINHA desde a ingestão; LICENSE_POLICY é
            # conferência, nunca substituição. Divergir é sinal de sources.toml
            # editado sem re-ingerir — avisa e grava o que a linha diz.
            divergencia[str(licenca)] += 1

        saida.append(
            (
                uid,
                dados["text"][i],
                None,          # text_original: NULL = nunca editado
                0,             # edited
                dados["lang"][i],
                dados["lang_variant"][i],
                dados["variant_confidence"][i],
                dados["source"][i],
                dados["source_id"][i],
                dados["source_split"][i],
                licenca,
                int(comercial),
                int(redistribuivel),
                task_type,
                domain,
                quality,
                nsfw,
                int(bool(dados["pii_found"][i])),
                metodo,
                confianca,
                revisar,
                dados["hash_norm"][i],
                dados["n_exact_dups"][i],
                dados["n_near_dups"][i],
                dados["n_chars"][i],
                dados["n_words"][i],
                dados["country"][i],
                dados["model_family"][i],
                dados["created_ts"][i],
                dados["native_category"][i],
                dados["meta_json"][i],
            )
        )
    return saida


def cobertura(universo: Path, rotulos: dict[str, Rotulo]) -> tuple[int, int]:
    """``(linhas do universo, linhas sem task_type)`` — só a coluna ``uid``.

    Pré-voo: ler uma coluna de string custa frações de segundo e permite recusar
    a carga ANTES dos minutos de INSERT e de rebuild do FTS.
    """
    import pyarrow.parquet as pq

    tabela = pq.read_table(universo, columns=["uid"])
    total = tabela.num_rows
    sem = 0
    for uid in tabela.column("uid").to_pylist():
        rotulo = rotulos.get(str(uid))
        if rotulo is None or rotulo.task_type is None:
            sem += 1
    return total, sem


# ---------------------------------------------------------------------------
# metadados
# ---------------------------------------------------------------------------


def sha256_arquivo(caminho: Path, bloco: int = 1 << 20) -> str:
    """sha256 em streaming (o universo passa de 700 MB)."""
    h = hashlib.sha256()
    with caminho.open("rb") as fh:
        for pedaco in iter(lambda: fh.read(bloco), b""):
            h.update(pedaco)
    return h.hexdigest()


def _git(*args: str) -> str:
    """Saída de um comando git, ou string vazia. **Nunca levanta**: o repo pode
    não ser um checkout git (tarball, cópia) e isso não é motivo para não
    carregar um banco."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=paths.ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def gravar_app_meta(
    conn: sqlite3.Connection,
    *,
    n_rows: int,
    universe_sha: str,
    labels_sha: str,
    needs_review_pct: float,
    unlabeled_pct: float,
) -> str:
    """Preenche ``app_meta`` e devolve o ``db_build_id``.

    ``db_build_id`` é derivado dos sha256 das entradas, **não** de um uuid4: o
    projeto inteiro se apoia em "mesmas entradas ⇒ mesmo resultado", e um id
    aleatório faria dois bancos idênticos parecerem diferentes.
    """
    from .. import __version__

    build_id = hashlib.sha256(f"{universe_sha}:{labels_sha}".encode()).hexdigest()[:16]
    cfg = settings()
    limiares = {
        secao: cfg.get(secao, {}) for secao in ("classifier", "dedup", "loaddb", "embed")
    }
    valores = {
        "schema_version": dbmod.SCHEMA_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "db_build_id": build_id,
        "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_rows": n_rows,
        "needs_review_pct": f"{needs_review_pct:.4f}",
        "unlabeled_pct": f"{unlabeled_pct:.4f}",
        "universe_sha256": universe_sha,
        "labels_sha256": labels_sha,
        "embed_model": get("embed", "model", default=""),
        "thresholds_json": json.dumps(limiares, ensure_ascii=False, sort_keys=True),
        "pipeline_git_sha": _git("rev-parse", "HEAD"),
        "pipeline_git_dirty": "1" if _git("status", "--porcelain") else "0",
        "pf_version": __version__,
        "sqlite_version": sqlite3.sqlite_version,
    }
    for chave, valor in valores.items():
        dbmod.set_meta(conn, chave, valor)
    return build_id


# ---------------------------------------------------------------------------
# construção
# ---------------------------------------------------------------------------


def _apagar_banco(caminho: Path) -> None:
    """Remove o arquivo e os sidecars ``-wal``/``-shm``."""
    for sufixo in ("", "-wal", "-shm"):
        Path(f"{caminho}{sufixo}").unlink(missing_ok=True)


def _limpar_sidecars(caminho: Path) -> None:
    """Apaga os sidecars que uma conexão SÓ-LEITURA deixa para trás.

    O SQLite remove ``-wal``/``-shm`` sozinho quando a última conexão fecha —
    mas só se ela puder escrever. Uma conexão ``mode=ro`` (a que mede o FTS, a
    que confere o banco depois do swap) recria o ``-shm`` e não consegue
    limpá-lo, e um ``-shm`` órfão ao lado do banco é exatamente o sinal que o
    pré-voo do swap usa para dizer "alguém está com isto aberto". O ``-wal`` só
    é removido se estiver VAZIO: com frames dentro ele é dado, não sujeira.
    """
    wal = Path(f"{caminho}-wal")
    try:
        if wal.is_file() and wal.stat().st_size == 0:
            wal.unlink()
        Path(f"{caminho}-shm").unlink(missing_ok=True)
    except OSError:  # pragma: no cover - handle ainda aberto por outro processo
        pass


def _primeiro_termo(conn: sqlite3.Connection) -> str | None:
    """Uma palavra que **sabidamente** está no corpus, para provar o índice.

    Um ``integrity-check`` prova coerência interna; ele não prova que o índice
    aponta para as linhas certas. Um MATCH de verdade, com um termo tirado do
    próprio texto carregado, prova.
    """
    linha = conn.execute("SELECT text FROM prompts WHERE n_chars > 20 LIMIT 1").fetchone()
    if linha is None:
        return None
    candidatos = [p for p in str(linha["text"]).split() if p.isalnum() and len(p) >= 4]
    return candidatos[0] if candidatos else None


def construir(
    build: Path,
    universo: Path,
    rotulos: dict[str, Rotulo],
    *,
    n_universo: int,
) -> tuple[Counter[str], Counter[str], Counter[str], dict[str, Counter[str]]]:
    """Constrói o banco do zero em ``build``. Devolve os contadores do relatório."""
    import pyarrow.parquet as pq

    stats: Counter[str] = Counter()
    divergencia: Counter[str] = Counter()
    revisao: Counter[str] = Counter()
    facetas: dict[str, Counter[str]] = {
        "source": Counter(),
        "lang": Counter(),
        "license": Counter(),
        "label_method": Counter(),
    }

    lote_leitura = _inteiro("loaddb", "read_batch_rows", 10_000)
    lote_insert = _inteiro("loaddb", "insert_chunk_rows", 10_000)
    cache_mb = _inteiro("loaddb", "cache_size_mb", 256)

    _apagar_banco(build)
    build.parent.mkdir(parents=True, exist_ok=True)
    conn = dbmod.connect(build)
    try:
        dbmod.init_db(conn)
        # PRAGMAs de CARGA. Só valem aqui: o banco é descartável até o swap, e
        # perder a build num crash custa re-rodar o comando, não dados.
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA journal_mode=MEMORY")
        conn.execute(f"PRAGMA cache_size=-{cache_mb * 1024}")
        conn.execute("PRAGMA temp_store=MEMORY")

        for trigger in dbmod.TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        for indice in dbmod.INDEXES:
            conn.execute(f"DROP INDEX IF EXISTS {indice}")

        sql = (
            f"INSERT INTO prompts ({', '.join(COLUNAS_INSERT)}) "
            f"VALUES ({', '.join('?' * len(COLUNAS_INSERT))})"
        )
        relogio = time.perf_counter()
        conn.execute("BEGIN")
        pendentes: list[tuple[Any, ...]] = []
        arquivo = pq.ParquetFile(universo)
        for lote in arquivo.iter_batches(
            batch_size=lote_leitura, columns=list(COLUNAS_UNIVERSO)
        ):
            pendentes.extend(_linhas_do_lote(lote, rotulos, stats, divergencia))
            for coluna, contador in facetas.items():
                if coluna == "label_method":
                    continue
                contador.update(str(v) for v in lote.column(coluna).to_pylist())
            while len(pendentes) >= lote_insert:
                conn.executemany(sql, pendentes[:lote_insert])
                del pendentes[:lote_insert]
        if pendentes:
            conn.executemany(sql, pendentes)
        conn.execute("COMMIT")
        inseridas = int(conn.execute("SELECT count(*) AS n FROM prompts").fetchone()["n"])
        print(
            f"[{ESTAGIO}] {inseridas} linhas inseridas em {time.perf_counter() - relogio:.1f} s "
            f"(lotes de {lote_insert}, sem índice e sem trigger)"
        )

        relogio = time.perf_counter()
        conn.execute("INSERT INTO prompts_fts(prompts_fts) VALUES('rebuild')")
        print(f"[{ESTAGIO}] índice FTS5 reconstruído em {time.perf_counter() - relogio:.1f} s")

        # Todo o DDL é IF NOT EXISTS: isto recria os 11 índices e os 3 triggers
        # sem tocar em tabela nem em dado.
        relogio = time.perf_counter()
        conn.executescript(dbmod.DDL)
        print(
            f"[{ESTAGIO}] {len(dbmod.INDEXES)} índices + {len(dbmod.TRIGGERS)} triggers "
            f"recriados em {time.perf_counter() - relogio:.1f} s"
        )

        for chave, valor in conn.execute(
            "SELECT label_method AS k, count(*) AS n FROM prompts GROUP BY label_method"
        ):
            facetas["label_method"][str(chave) if chave else "(sem rótulo)"] = int(valor)
        revisao["needs_review=1"] = int(
            conn.execute(
                "SELECT count(*) AS n FROM prompts WHERE needs_review = 1"
            ).fetchone()["n"]
        )
        revisao["needs_review=0"] = inseridas - revisao["needs_review=1"]

        if inseridas != n_universo:
            raise SystemExit(
                f"[{ESTAGIO}] carga incompleta: {inseridas} linhas no banco != "
                f"{n_universo} no {rel(universo)}"
            )
    finally:
        conn.close()
    return stats, divergencia, revisao, facetas


def finalizar(build: Path) -> None:
    """PRAGMAs de leitura, ANALYZE e checkpoint — o banco sai pronto para servir."""
    conn = dbmod.connect(build)
    try:
        # Sem sqlite_stat1 o planner escolhe pior; ANALYZE é barato aqui e
        # fica gravado no arquivo, então vale para toda conexão futura.
        conn.execute("ANALYZE")
        conn.execute("PRAGMA optimize")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# verificação de integridade
# ---------------------------------------------------------------------------

#: BLOB silencioso: as colunas SEM CHECK são as que passam podres. Esta consulta
#: é a única guarda que as cobre.
SQL_TIPOS = """
SELECT count(*) AS n FROM prompts
WHERE typeof(uid) != 'text'
   OR typeof(text) != 'text'
   OR typeof(n_chars) != 'integer'
   OR typeof(n_words) != 'integer'
   OR typeof(n_exact_dups) != 'integer'
   OR typeof(n_near_dups) != 'integer'
   OR typeof(commercial_ok) != 'integer'
   OR typeof(redistributable) != 'integer'
   OR typeof(needs_review) != 'integer'
   OR (quality IS NOT NULL AND typeof(quality) != 'integer')
   OR (nsfw IS NOT NULL AND typeof(nsfw) != 'integer')
   OR (label_confidence IS NOT NULL AND typeof(label_confidence) != 'real')
   OR (variant_confidence IS NOT NULL AND typeof(variant_confidence) != 'real')
   OR (text_original IS NOT NULL AND typeof(text_original) != 'text')
"""


def verificar(
    caminho: Path, *, n_esperado: int, n_revisao: int, profundo: bool = False
) -> list[str]:
    """Guardas pós-carga. Lista vazia = banco íntegro.

    A guarda óbvia do FTS **não funciona**: numa tabela de conteúdo externo,
    ``SELECT count(*) FROM prompts_fts`` lê a tabela de conteúdo e devolve o
    número certo mesmo com o índice completamente vazio (medido). Quem detecta é
    ``integrity-check`` **com o argumento 1** — com índice vazio ele levanta
    ``DatabaseError: database disk image is malformed``; sem o argumento, passa.

    A conexão é de ESCRITA por causa dele: os comandos do FTS5 viajam como
    ``INSERT`` na tabela virtual e um ``mode=ro`` devolve "attempt to write a
    readonly database" antes de verificar coisa nenhuma. O ``integrity-check``
    não altera dado — mas exige o direito de escrever.
    """
    falhas: list[str] = []
    conn = dbmod.connect(caminho)
    try:
        n = int(conn.execute("SELECT count(*) AS n FROM prompts").fetchone()["n"])
        if n != n_esperado:
            falhas.append(f"count(prompts)={n} != {n_esperado} esperadas")
        distintos = int(
            conn.execute("SELECT count(DISTINCT uid) AS n FROM prompts").fetchone()["n"]
        )
        if distintos != n:
            falhas.append(f"uid repetido: {n - distintos} linha(s)")

        podres = int(conn.execute(SQL_TIPOS).fetchone()["n"])
        if podres:
            falhas.append(
                f"{podres} linha(s) com coluna de tipo errado (escalar numpy virou BLOB?)"
            )

        try:
            conn.execute("INSERT INTO prompts_fts(prompts_fts, rank) VALUES('integrity-check', 1)")
        except sqlite3.DatabaseError as exc:
            falhas.append(f"integrity-check do FTS5 falhou: {exc}")

        termo = _primeiro_termo(conn)
        if termo is not None and not dbmod.fts_search(conn, termo):
            falhas.append(f"MATCH {termo!r} (tirado do próprio corpus) não achou nada")

        if list(conn.execute("PRAGMA foreign_key_check")):
            falhas.append("foreign_key_check apontou violação")

        revisao = int(
            conn.execute(
                "SELECT count(*) AS n FROM prompts WHERE needs_review = 1"
            ).fetchone()["n"]
        )
        if revisao != n_revisao:
            falhas.append(f"needs_review no banco ({revisao}) != o contado na carga ({n_revisao})")

        pragma = "integrity_check" if profundo else "quick_check"
        resultado = conn.execute(f"PRAGMA {pragma}").fetchone()[0]
        if str(resultado).lower() != "ok":
            falhas.append(f"PRAGMA {pragma}: {resultado}")
    finally:
        # Conexão de escrita em WAL deixa -wal/-shm; o checkpoint devolve o
        # banco a um arquivo único, que é o que o swap troca.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    return falhas


# ---------------------------------------------------------------------------
# swap
# ---------------------------------------------------------------------------


def destino_ocupado(destino: Path) -> str | None:
    """Motivo pelo qual o destino NÃO pode ser trocado, ou ``None``.

    Duas provas, porque nenhuma sozinha basta. (1) Um lock exclusivo: se outra
    conexão tem o banco aberto, o ``BEGIN EXCLUSIVE`` volta ``database is
    locked``. (2) No Windows, o próprio ``os.replace`` recusa — o SQLite abre o
    arquivo sem ``FILE_SHARE_DELETE``. Quando nem uma nem outra consegue
    responder, a função devolve o motivo em vez de assumir que está livre: o
    preço de errar aqui é um banco corrompido debaixo de um servidor rodando.
    """
    if not destino.is_file():
        return None
    conn: sqlite3.Connection | None = None
    try:
        uri = f"file:///{destino.resolve().as_posix().lstrip('/')}?mode=rw"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=1000")
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        return f"o banco está travado por outro processo ({exc})"
    except sqlite3.DatabaseError as exc:
        # Arquivo corrompido ou não-SQLite: trocar por cima é o certo, mas quem
        # decide é o humano.
        return f"o arquivo atual não abre como SQLite ({exc})"
    finally:
        if conn is not None:
            conn.close()
    return None


def trocar(build: Path, destino: Path, *, backup: bool) -> int:
    """Swap atômico do banco. 0 = trocado, ``EXIT_SWAP_BLOQUEADO`` = recusado."""
    if not build.is_file():
        print(f"[{ESTAGIO}] nada para trocar: {rel(build)} não existe", flush=True)
        return EXIT_SWAP_BLOQUEADO

    for sufixo in ("-wal", "-shm"):
        sidecar = Path(f"{build}{sufixo}")
        if sidecar.is_file() and sidecar.stat().st_size > 0:
            print(
                f"[{ESTAGIO}] AVISO: {rel(sidecar)} não está vazio — o checkpoint "
                "não fechou; o banco novo pode estar incompleto"
            )
    for sufixo in ("-wal", "-shm"):
        if Path(f"{destino}{sufixo}").is_file():
            print(
                f"[{ESTAGIO}] AVISO: {rel(destino)}{sufixo} existe (banco aberto agora, "
                "ou sobra de um crash antigo)"
            )

    motivo = destino_ocupado(destino)
    if motivo is not None:
        print(f"[{ESTAGIO}] swap RECUSADO: {motivo}", flush=True)
        print(
            f"[{ESTAGIO}] pare o `pf serve` e rode `pf load-db --swap-only` — "
            f"o banco novo já está pronto em {rel(build)}"
        )
        return EXIT_SWAP_BLOQUEADO

    if backup and destino.is_file():
        # copy2 só LÊ o destino: funciona mesmo com a app aberta.
        bak = Path(f"{destino}.bak")
        shutil.copy2(destino, bak)
        print(f"[{ESTAGIO}] backup do banco anterior -> {rel(bak)} ({bak.stat().st_size / 1e6:.1f} MB)")

    try:
        os.replace(build, destino)
    except PermissionError as exc:
        # NÃO apagar o build: ele é o produto pronto e o retry é `--swap-only`.
        print(
            f"[{ESTAGIO}] swap RECUSADO: {rel(destino)} está aberto por outro processo "
            f"(o `pf serve`?) — {exc}",
            flush=True,
        )
        print(
            f"[{ESTAGIO}] pare o servidor e rode `pf load-db --swap-only` — "
            f"o banco novo já está em {rel(build)}"
        )
        return EXIT_SWAP_BLOQUEADO

    # Os sidecars que sobraram pertenciam ao banco ANTIGO: mantê-los faria o
    # SQLite tentar aplicar um WAL de outro arquivo.
    for sufixo in ("-wal", "-shm"):
        orfao = Path(f"{destino}{sufixo}")
        try:
            orfao.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - depende de lock do SO
            print(f"[{ESTAGIO}] AVISO: não consegui apagar {rel(orfao)} ({exc})")
    print(f"[{ESTAGIO}] swap concluído -> {rel(destino)}")
    return 0


# ---------------------------------------------------------------------------
# relatório
# ---------------------------------------------------------------------------


def _tempo_fts(caminho: Path) -> str:
    """Tempo de uma consulta FTS de amostra, no banco recém-trocado."""
    conn = dbmod.connect(caminho, readonly=True)
    try:
        termo = _primeiro_termo(conn)
        if termo is None:
            return "sem linhas para medir"
        dbmod.fts_search(conn, termo)  # aquecimento fora do cronômetro
        inicio = time.perf_counter()
        achou = len(dbmod.fts_search(conn, termo))
        return f"MATCH {termo!r} -> {achou} linha(s) em {(time.perf_counter() - inicio) * 1000:.1f} ms"
    finally:
        conn.close()
        _limpar_sidecars(caminho)


def _top(contador: Counter[str], n: int = 12) -> list[list[Any]]:
    total = sum(contador.values()) or 1
    return [
        [chave, valor, f"{valor / total:.1%}"]
        for chave, valor in contador.most_common(n)
    ]


# ---------------------------------------------------------------------------
# estágio
# ---------------------------------------------------------------------------


def run(cfg: StageConfig) -> int:
    """Constrói o SQLite e troca pelo banco vivo. 0 = sucesso."""
    relogio = Cronometro(ESTAGIO)
    cfg.preparar_dirs()
    destino = cfg.caminho(DB_SQLITE)
    build = cfg.caminho(DB_BUILD_SQLITE)

    if cfg.swap_only:
        print(f"[{ESTAGIO}] --swap-only: nada é construído, só a troca")
        codigo = trocar(build, destino, backup=cfg.backup)
        if codigo == 0:
            _pos_swap(destino, profundo=cfg.deep)
        return codigo

    universo = cfg.caminho(UNIVERSE)
    exigir(universo, "pf run s01-s06")
    labeled = _achar_labeled(cfg)
    seed = cfg.caminho(SEED_LABELS)
    seed = seed if seed.is_file() else None

    print(f"[{ESTAGIO}] universo: {rel(universo)}")
    print(f"[{ESTAGIO}] rótulos do classificador: {rel(labeled) if labeled else 'AUSENTE (s10 não rodou)'}")
    print(f"[{ESTAGIO}] rótulos da campanha: {rel(seed) if seed else 'AUSENTE (s08 não rodou)'}")

    rotulos, stats_rotulo = carregar_rotulos(labeled, seed)
    n_universo, sem_rotulo = cobertura(universo, rotulos)
    pct_sem = 100.0 * sem_rotulo / n_universo if n_universo else 0.0
    teto = teto_sem_rotulo(cfg.allow_unlabeled_pct)
    print(
        f"[{ESTAGIO}] cobertura: {n_universo - sem_rotulo}/{n_universo} linhas com task_type "
        f"({pct_sem:.2f}% sem rótulo, teto {teto:.2f}%)"
    )
    if pct_sem > teto:
        print(
            f"[{ESTAGIO}] RECUSADO: {pct_sem:.2f}% do universo está sem task_type, acima do "
            f"teto de {teto:.2f}% — rode `pf merge-labels` e `pf apply` antes, ou "
            f"passe `--allow-unlabeled-pct {min(100.0, pct_sem + 1):.0f}` se a falta "
            "de rótulo for intencional",
            flush=True,
        )
        return 1

    print(f"[{ESTAGIO}] construindo {rel(build)} do zero…", flush=True)
    stats, divergencia, revisao, facetas = construir(
        build, universo, rotulos, n_universo=n_universo
    )
    finalizar(build)

    n_revisao = revisao["needs_review=1"]
    universe_sha = sha256_arquivo(universo)
    labels_sha = sha256_arquivo(labeled) if labeled else ""
    conn = dbmod.connect(build)
    try:
        build_id = gravar_app_meta(
            conn,
            n_rows=n_universo,
            universe_sha=universe_sha,
            labels_sha=labels_sha,
            needs_review_pct=n_revisao / n_universo if n_universo else 0.0,
            unlabeled_pct=pct_sem / 100.0,
        )
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    falhas = verificar(
        build, n_esperado=n_universo, n_revisao=n_revisao, profundo=cfg.deep
    )
    if falhas:
        for falha in falhas:
            print(f"[{ESTAGIO}] INTEGRIDADE: {falha}", file=sys.stderr)
        print(f"[{ESTAGIO}] banco RECUSADO — {rel(build)} fica para inspeção, nada foi trocado")
        return 1

    # --- relatório ----------------------------------------------------------
    tamanho = build.stat().st_size
    imprimir_funil(
        ESTAGIO,
        ("etapa", "linhas"),
        [
            ["universo (parquet)", n_universo],
            ["carregadas no SQLite", n_universo],
            ["needs_review = 1", f"{n_revisao} ({n_revisao / max(n_universo, 1):.1%})"],
            ["sem task_type", f"{sem_rotulo} ({pct_sem:.2f}%)"],
        ],
    )
    if stats_rotulo:
        imprimir_funil(ESTAGIO, ("origem do rótulo", "n"), sorted(stats_rotulo.items()))
    if stats:
        imprimir_funil(ESTAGIO, ("observação", "n"), sorted(stats.items()))
    for titulo, contador in (
        ("fonte", facetas["source"]),
        ("idioma", facetas["lang"]),
        ("licença", facetas["license"]),
        ("label_method", facetas["label_method"]),
    ):
        if contador:
            imprimir_funil(ESTAGIO, (titulo, "n", "%"), _top(contador))
    if divergencia:
        imprimir_funil(
            ESTAGIO,
            ("licença com política divergente do sources.toml", "linhas"),
            sorted(divergencia.items()),
        )
        print(
            f"[{ESTAGIO}] AVISO: a licença viaja por linha e foi PRESERVADA; "
            "conferir config/sources.toml contra schema.LICENSE_POLICY"
        )

    print(f"[{ESTAGIO}] build_id={build_id} tamanho={tamanho / 1e6:.1f} MB")
    print(f"[{ESTAGIO}] FTS: {_tempo_fts(build)}")

    if not cfg.swap:
        print(f"[{ESTAGIO}] --no-swap: banco pronto em {rel(build)}, nada foi trocado")
        print(f"[{ESTAGIO}] para trocar depois: `pf load-db --swap-only`")
        relogio.fim()
        return 0

    codigo = trocar(build, destino, backup=cfg.backup)
    if codigo == 0:
        _pos_swap(destino, profundo=cfg.deep)
    relogio.fim()
    return codigo


def _pos_swap(destino: Path, *, profundo: bool) -> None:
    """Reabre o banco trocado em readonly e imprime o que ele diz de si mesmo."""
    conn = dbmod.connect(destino, readonly=True)
    try:
        pragma = "integrity_check" if profundo else "quick_check"
        estado = conn.execute(f"PRAGMA {pragma}").fetchone()[0]
        n = int(conn.execute("SELECT count(*) AS n FROM prompts").fetchone()["n"])
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM app_meta")}
        print(
            f"[{ESTAGIO}] pós-swap: {pragma}={estado}, {n} linhas, "
            f"build_id={meta.get('db_build_id', '?')}, "
            f"taxonomia {meta.get('taxonomy_version', '?')}, "
            f"construído em {meta.get('built_at', '?')}"
        )
    finally:
        conn.close()
        _limpar_sidecars(destino)


__all__ = [
    "COLUNAS_INSERT",
    "COLUNAS_UNIVERSO",
    "ESTAGIO",
    "EXIT_SWAP_BLOQUEADO",
    "METODOS_OBSERVADOS",
    "Rotulo",
    "carregar_rotulos",
    "cobertura",
    "construir",
    "destino_ocupado",
    "finalizar",
    "gravar_app_meta",
    "run",
    "sha256_arquivo",
    "teto_sem_rotulo",
    "trocar",
    "verificar",
]
