"""SQLite do Prompt Factory: DDL, conexão e o autoteste ``pf db-check``.

Um arquivo só (``data/db/prompts.sqlite``), em WAL, com busca textual por FTS5
configurado com ``remove_diacritics 2`` — é isso que faz "coracao" achar
"coração" sem gambiarra de coluna sem acento.

Só stdlib: o módulo ``sqlite3`` embutido no CPython 3.12 já traz FTS5 nas
builds oficiais (Windows e manylinux).

Notas de projeto que valem a leitura antes de mexer no DDL:

* ``id`` é ``INTEGER PRIMARY KEY``, ou seja, alias do rowid — requisito do
  ``content_rowid`` da tabela FTS externa.
* ``text_raw`` **não** entra no SQLite (ele mora no Parquet). ``text_original``
  recebe o texto normalizado no momento da carga e existe só para o "reverter"
  da interface depois de uma edição manual.
* Os triggers do FTS espelham apenas ``text``; o de UPDATE é
  ``AFTER UPDATE OF text`` para que corrigir um rótulo não reindexe nada.
* Não há trigger de ``updated_at``: quem faz PATCH na API seta a coluna. Trigger
  aqui dispararia em toda carga bulk e mentiria sobre curadoria humana.
* ``task_type``/``domain`` propositalmente **sem CHECK**: a taxonomia evolui
  (v1.1) e migrar CHECK em SQLite exige recriar a tabela. A validação vive em
  ``schema.load_taxonomy``.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from . import schema, textnorm

#: Versão do schema físico, gravada em ``app_meta``. Incrementar exige migração.
SCHEMA_VERSION = 1

#: Mínimo para os recursos usados aqui (upsert com ON CONFLICT, STRICT-adjacent,
#: FTS5 estável). Python 3.12 embute 3.4x.
MIN_SQLITE = (3, 35, 0)

DDL = """
CREATE TABLE IF NOT EXISTS prompts (
  id                 INTEGER PRIMARY KEY,
  uid                TEXT NOT NULL UNIQUE,
  text               TEXT NOT NULL,
  text_original      TEXT NOT NULL,
  edited             INTEGER NOT NULL DEFAULT 0 CHECK (edited IN (0,1)),
  lang               TEXT NOT NULL CHECK (lang IN ('pt','en')),
  lang_variant       TEXT CHECK (lang_variant IS NULL OR lang_variant IN ('pt-BR','pt-PT','pt-indef')),
  variant_confidence REAL,
  source             TEXT NOT NULL,
  source_id          TEXT,
  source_split       TEXT,
  license            TEXT NOT NULL,
  commercial_ok      INTEGER NOT NULL DEFAULT 0 CHECK (commercial_ok IN (0,1)),
  redistributable    INTEGER NOT NULL DEFAULT 0 CHECK (redistributable IN (0,1)),
  task_type          TEXT,
  domain             TEXT,
  quality            INTEGER CHECK (quality IS NULL OR quality IN (1,2,3)),
  nsfw               INTEGER CHECK (nsfw IS NULL OR nsfw IN (0,1)),
  pii_found          INTEGER NOT NULL DEFAULT 0 CHECK (pii_found IN (0,1)),
  label_method       TEXT CHECK (label_method IS NULL OR label_method IN ('agent','classifier','native','manual')),
  label_confidence   REAL,
  needs_review       INTEGER NOT NULL DEFAULT 0 CHECK (needs_review IN (0,1)),
  hash_norm          TEXT NOT NULL,
  n_exact_dups       INTEGER NOT NULL DEFAULT 0,
  n_near_dups        INTEGER NOT NULL DEFAULT 0,
  n_chars            INTEGER NOT NULL,
  n_words            INTEGER NOT NULL,
  country            TEXT,
  model_family       TEXT,
  created_ts         TEXT,
  native_category    TEXT,
  meta_json          TEXT,
  ingested_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_prompts_lang    ON prompts(lang);
CREATE INDEX IF NOT EXISTS idx_prompts_variant ON prompts(lang_variant);
CREATE INDEX IF NOT EXISTS idx_prompts_task    ON prompts(task_type);
CREATE INDEX IF NOT EXISTS idx_prompts_domain  ON prompts(domain);
CREATE INDEX IF NOT EXISTS idx_prompts_source  ON prompts(source);
CREATE INDEX IF NOT EXISTS idx_prompts_license ON prompts(license);
CREATE INDEX IF NOT EXISTS idx_prompts_quality ON prompts(quality);
CREATE INDEX IF NOT EXISTS idx_prompts_nsfw    ON prompts(nsfw);
CREATE INDEX IF NOT EXISTS idx_prompts_review  ON prompts(needs_review);
CREATE INDEX IF NOT EXISTS idx_prompts_hash    ON prompts(hash_norm);

-- Índice externo (content='prompts'): o texto não é duplicado, o FTS guarda só
-- o índice invertido. remove_diacritics 2 dobra acento em TODO o Unicode.
CREATE VIRTUAL TABLE IF NOT EXISTS prompts_fts USING fts5(
  text,
  content='prompts',
  content_rowid='id',
  tokenize="unicode61 remove_diacritics 2"
);
-- Os três triggers mantêm o índice em dia em INSERT/DELETE/UPDATE de text.
-- ATENÇÃO (M8): o 'delete' de um FTS externo exige que old.text seja EXATAMENTE
-- o texto indexado; se divergir, o índice corrompe silenciosamente. Por isso a
-- carga bulk derruba os triggers, insere tudo e refaz o índice com
--   INSERT INTO prompts_fts(prompts_fts) VALUES('rebuild');
CREATE TRIGGER IF NOT EXISTS prompts_fts_ai AFTER INSERT ON prompts BEGIN
  INSERT INTO prompts_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS prompts_fts_ad AFTER DELETE ON prompts BEGIN
  INSERT INTO prompts_fts(prompts_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS prompts_fts_au AFTER UPDATE OF text ON prompts BEGIN
  INSERT INTO prompts_fts(prompts_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO prompts_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS collections (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  description TEXT,
  created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS collection_items (
  collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
  prompt_id     INTEGER NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
  added_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (collection_id, prompt_id)
);
CREATE INDEX IF NOT EXISTS idx_collection_items_prompt ON collection_items(prompt_id);

CREATE TABLE IF NOT EXISTS seed_labels (
  id               INTEGER PRIMARY KEY,
  uid              TEXT NOT NULL,
  batch_id         TEXT NOT NULL,
  task_type        TEXT,
  domain           TEXT,
  quality          INTEGER CHECK (quality IS NULL OR quality IN (1,2,3)),
  nsfw             INTEGER CHECK (nsfw IS NULL OR nsfw IN (0,1)),
  labeler          TEXT NOT NULL,
  is_calibration   INTEGER NOT NULL DEFAULT 0 CHECK (is_calibration IN (0,1)),
  taxonomy_version TEXT NOT NULL,
  created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (uid, batch_id, labeler)
);
CREATE TABLE IF NOT EXISTS exports (
  id            INTEGER PRIMARY KEY,
  format        TEXT NOT NULL CHECK (format IN ('jsonl','csv')),
  path          TEXT NOT NULL,
  filters_json  TEXT NOT NULL DEFAULT '{}',
  row_count     INTEGER NOT NULL,
  manifest_json TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS app_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

#: Tabelas próprias (as shadow tables do FTS5 não entram nesta lista).
TABLES: tuple[str, ...] = (
    "prompts",
    "prompts_fts",
    "collections",
    "collection_items",
    "seed_labels",
    "exports",
    "app_meta",
)

#: Triggers de sincronia do índice FTS.
TRIGGERS: tuple[str, ...] = ("prompts_fts_ai", "prompts_fts_ad", "prompts_fts_au")


def connect(db_path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Abre o banco com os PRAGMAs do projeto e ``sqlite3.Row``.

    ``foreign_keys`` é **por conexão**: abrir com ``sqlite3.connect`` direto (em
    teste, por exemplo) deixa o ON DELETE CASCADE inerte e o teste passa em
    falso. Use sempre esta função.
    """
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        alvo = ".".join(map(str, MIN_SQLITE))
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} é antigo demais (mínimo {alvo})"
        )
    if readonly:
        # file:///G:/.../prompts.sqlite no Windows, file:///a/b no POSIX.
        uri = f"file:///{Path(db_path).resolve().as_posix().lstrip('/')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    else:
        conn = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    if not readonly:
        # WAL é persistente (fica no arquivo), mas repetir é barato e idempotente.
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Aplica o DDL (idempotente) e semeia ``schema_version`` em ``app_meta``."""
    conn.executescript(DDL)
    conn.execute(
        "INSERT INTO app_meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO NOTHING",
        (str(SCHEMA_VERSION),),
    )
    return conn


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """Grava (upsert) uma chave em ``app_meta``. O valor é serializado como texto."""
    conn.execute(
        "INSERT INTO app_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    """Lê uma chave de ``app_meta`` (``default`` se ausente)."""
    row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    return default if row is None else str(row["value"])


def fts_query(term: str) -> str:
    """Envelopa um termo como string literal do FTS5.

    Sem as aspas duplas, `-`, `*`, `:` e `OR` viram sintaxe da query e um termo
    vindo do usuário derruba a busca com ``fts5: syntax error``. Dentro do
    literal, a aspa dupla se escapa dobrando.
    """
    return '"' + term.replace('"', '""') + '"'


def fts_search(conn: sqlite3.Connection, term: str) -> list[sqlite3.Row]:
    """Linhas de ``prompts`` cujo ``text`` casa com ``term`` (uma palavra/frase).

    O JOIN por ``rowid`` é justamente o que prova que o índice externo está
    apontando para as linhas certas.
    """
    return conn.execute(
        "SELECT p.id, p.uid, p.text FROM prompts_fts "
        "JOIN prompts p ON p.id = prompts_fts.rowid "
        "WHERE prompts_fts MATCH ? ORDER BY p.id",
        (fts_query(term),),
    ).fetchall()


# ---------------------------------------------------------------------------
# pf db-check
# ---------------------------------------------------------------------------

_CHECK_TEXT = "Meu coração está partido"
_CHECK_TEXT_UPDATED = "Agora estou feliz de novo"


def _insert_check_row(conn: sqlite3.Connection, text_raw: str) -> int:
    """Insere uma linha seguindo a cadeia canônica do projeto."""
    text = textnorm.norm_display(text_raw)
    hash_norm = hashlib.sha256(textnorm.norm_for_hash(text).encode("utf-8")).hexdigest()
    n_chars, n_words = schema.text_stats(text)
    uid = schema.make_uid("db-check", "1", text_raw)
    cur = conn.execute(
        "INSERT INTO prompts "
        "(uid, text, text_original, lang, source, license, hash_norm, n_chars, n_words) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uid,
            text,
            text,
            schema.Lang.PT.value,
            "db-check",
            schema.License.UNKNOWN.value,
            hash_norm,
            n_chars,
            n_words,
        ),
    )
    return int(cur.lastrowid or 0)


def run_db_check(bench: bool = False, query: str | None = None) -> int:
    """Autoteste do SQLite: DDL, WAL, FTS5 e busca sem acento. 0 = PASS.

    Roda inteiro num diretório temporário — nunca toca ``data/db/``.
    """
    if bench:
        print("db-check: --bench adiado para o M8 (precisa do banco carregado); segue o check")

    print(f"db-check: SQLite {sqlite3.sqlite_version} (mínimo exigido {'.'.join(map(str, MIN_SQLITE))})")

    falhas: list[str] = []
    # ignore_cleanup_errors: no Windows os arquivos -wal/-shm às vezes seguem
    # com handle aberto por um instante depois do close().
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "db-check.sqlite"
        conn: sqlite3.Connection | None = None
        try:
            conn = connect(db_path)
            try:
                init_db(conn)
            except sqlite3.OperationalError as exc:
                # "no such module: fts5" — build do SQLite sem o módulo.
                print(f"db-check: FTS5 indisponível nesta build do SQLite ({exc})")
                print("db-check: FAIL")
                return 1
            modo = conn.execute("PRAGMA journal_mode").fetchone()[0]
            print(f"db-check: DDL aplicado, journal_mode={modo}")

            rowid = _insert_check_row(conn, _CHECK_TEXT)
            print(f"db-check: linha inserida (id={rowid}) {_CHECK_TEXT!r}")

            # O par que justifica o remove_diacritics 2: sem acento e com acento
            # têm de achar a mesma linha.
            for termo, esperado in (("coracao", 1), ("coração", 1)):
                achou = len(fts_search(conn, termo))
                estado = "ok" if achou == esperado else "FALHOU"
                print(f"db-check: busca {termo!r} -> {achou} linha(s) [{estado}]")
                if achou != esperado:
                    falhas.append(f"busca {termo!r} devolveu {achou}, esperado {esperado}")

            if query:
                # Antes do UPDATE: o termo é testado contra a frase acima.
                achou = len(fts_search(conn, query))
                print(f"db-check: --query {query!r} -> {achou} linha(s)")

            conn.execute(
                "UPDATE prompts SET text = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ?",
                (_CHECK_TEXT_UPDATED, rowid),
            )
            sumiu = len(fts_search(conn, "coracao"))
            apareceu = len(fts_search(conn, "feliz"))
            estado = "ok" if (sumiu, apareceu) == (0, 1) else "FALHOU"
            print(
                f"db-check: após UPDATE, 'coracao' -> {sumiu}, 'feliz' -> {apareceu} [{estado}]"
            )
            if (sumiu, apareceu) != (0, 1):
                falhas.append("trigger de UPDATE não ressincronizou o FTS")
        finally:
            # Fechar ANTES de sair do contexto: o TemporaryDirectory limpa aqui
            # e o Windows não apaga arquivo com handle aberto.
            if conn is not None:
                conn.close()

    if falhas:
        for f in falhas:
            print(f"db-check: {f}")
        print("db-check: FAIL")
        return 1
    print("db-check: PASS")
    return 0


__all__ = [
    "DDL",
    "MIN_SQLITE",
    "SCHEMA_VERSION",
    "TABLES",
    "TRIGGERS",
    "connect",
    "fts_query",
    "fts_search",
    "get_meta",
    "init_db",
    "run_db_check",
    "set_meta",
]
