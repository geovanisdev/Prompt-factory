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
  é **NULL enquanto ninguém editou** e só recebe conteúdo quando a interface
  guarda o texto de antes de uma edição manual, para o "reverter". Copiar
  ``text`` nele na carga duplicaria ~800 MB de string para dizer "nada mudou",
  que o ``edited`` já diz. Quem lê usa ``COALESCE(text_original, text)``.
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
import math
import sqlite3
import tempfile
import time
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
  text_original      TEXT,
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
-- Índice de COBERTURA das facetas. O SQLite não combina dois índices de
-- igualdade: com só os simples acima, `WHERE lang=? AND task_type=? AND
-- domain=?` escolhe UM deles e varre o resto. Medido em 180k linhas sintéticas:
-- contagem por faceta 89,7 -> 13,2 ms (6,8x), filtro triplo 11,6 -> 0,16 ms
-- (70x), lang+task+needs_review 9,6 -> 0,46 ms (21x); os planos passam a dizer
-- "USING COVERING INDEX" (nem toca na tabela). A ordem das colunas é a ordem em
-- que a interface filtra: idioma sempre, depois tarefa, domínio e a fila de
-- revisão.
CREATE INDEX IF NOT EXISTS idx_prompts_facets  ON prompts(lang, task_type, domain, needs_review);

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

#: Índices de ``prompts``, na ordem do DDL. A carga bulk do s11 derruba os onze
#: antes de inserir (onze B-trees crescendo a cada linha custam mais do que
#: construí-los de uma vez no fim) e recria tudo com ``executescript(DDL)``.
INDEXES: tuple[str, ...] = (
    "idx_prompts_lang",
    "idx_prompts_variant",
    "idx_prompts_task",
    "idx_prompts_domain",
    "idx_prompts_source",
    "idx_prompts_license",
    "idx_prompts_quality",
    "idx_prompts_nsfw",
    "idx_prompts_review",
    "idx_prompts_hash",
    "idx_prompts_facets",
)


def connect(
    db_path: str | Path,
    *,
    readonly: bool = False,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Abre o banco com os PRAGMAs do projeto e ``sqlite3.Row``.

    ``foreign_keys`` é **por conexão**: abrir com ``sqlite3.connect`` direto (em
    teste, por exemplo) deixa o ON DELETE CASCADE inerte e o teste passa em
    falso. Use sempre esta função.

    ``check_same_thread=False`` só é legítimo quando a conexão pertence a UM
    fluxo de cada vez e o que atravessa a fronteira de thread é só o bastão
    entre etapas desse mesmo fluxo. São dois casos no projeto, ambos
    documentados na origem: o gerador do download de export e a dependência
    ``app.deps.get_conn`` (uma conexão por request, criada e fechada em
    chamadas distintas do threadpool do AnyIO). Numa conexão de verdade
    **compartilhada** entre usuários concorrentes, desligar a checagem troca um
    erro alto por corrupção silenciosa — aí não.
    """
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        alvo = ".".join(map(str, MIN_SQLITE))
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} é antigo demais (mínimo {alvo})"
        )
    if readonly:
        # file:///G:/.../prompts.sqlite no Windows, file:///a/b no POSIX.
        uri = f"file:///{Path(db_path).resolve().as_posix().lstrip('/')}?mode=ro"
        conn = sqlite3.connect(
            uri, uri=True, timeout=5.0, check_same_thread=check_same_thread
        )
    else:
        conn = sqlite3.connect(
            db_path,
            timeout=5.0,
            isolation_level=None,  # autocommit
            check_same_thread=check_same_thread,
        )
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
    # ``text_original`` fica de fora de propósito: NULL é o estado "nunca
    # editado", o mesmo que a carga do s11 grava em 100% das linhas.
    cur = conn.execute(
        "INSERT INTO prompts "
        "(uid, text, lang, source, license, hash_norm, n_chars, n_words) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uid,
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


#: Consultas do ``--bench``, na ordem. ``{f}`` = termo frequente (pior caso da
#: posting list), ``{s}`` = termo seletivo tirado do próprio corpus.
_BENCH_META_MS = 100.0


def _termos_do_corpus(conn: sqlite3.Connection) -> tuple[str, str]:
    """``(termo frequente, termo seletivo)`` extraídos do banco.

    Chutar termos fixos mediria o corpus errado: "the" não existe num banco só
    de português. O frequente sai do idioma majoritário, o seletivo sai de uma
    linha de verdade — assim os dois sempre casam com alguma coisa.
    """
    linha = conn.execute(
        "SELECT lang, count(*) AS n FROM prompts GROUP BY lang ORDER BY n DESC LIMIT 1"
    ).fetchone()
    frequente = "the" if (linha and str(linha["lang"]) == "en") else "de"
    seletivo = frequente
    amostra = conn.execute("SELECT text FROM prompts WHERE n_chars > 200 LIMIT 1").fetchone()
    if amostra is not None:
        palavras = [p for p in str(amostra["text"]).split() if p.isalpha() and len(p) >= 8]
        if palavras:
            seletivo = palavras[0]
    return frequente, seletivo


def _medir(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...], repeticoes: int) -> tuple[float, float, int]:
    """``(mediana ms, p95 ms, linhas)`` — 1 aquecimento fora do cronômetro.

    Mediana e p95, nunca média: neste Windows o antivírus injeta picos de
    dezenas de ms que puxam a média e não representam nada.
    """
    import statistics

    linhas = len(conn.execute(sql, params).fetchall())
    tempos: list[float] = []
    for _ in range(max(repeticoes, 1)):
        inicio = time.perf_counter()
        conn.execute(sql, params).fetchall()
        tempos.append((time.perf_counter() - inicio) * 1000)
    tempos.sort()
    indice = min(len(tempos) - 1, math.ceil(0.95 * len(tempos)) - 1)
    return statistics.median(tempos), tempos[max(indice, 0)], linhas


def run_bench(db_path: str | Path | None = None, repeticoes: int | None = None) -> int:
    """Mede as consultas que a interface do M9 vai fazer. 0 = dentro da meta.

    Sem banco carregado não há o que medir — avisa e devolve 0, para que
    ``pf db-check --bench`` continue sendo um comando seguro de rodar sempre.
    """
    from .config import get as _get
    from .paths import DB_FILE

    caminho = Path(db_path) if db_path else DB_FILE
    if not caminho.is_file():
        print(f"db-check: --bench sem banco em {caminho} — rode `pf load-db` antes")
        return 0
    if repeticoes is None:
        repeticoes = int(_get("loaddb", "bench_repeats", default=5))

    conn = connect(caminho, readonly=True)
    try:
        n = int(conn.execute("SELECT count(*) AS n FROM prompts").fetchone()["n"])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        print(
            f"db-check: {caminho} — {n} linhas, {caminho.stat().st_size / 1e6:.1f} MB "
            f"({page_count} páginas x {page_size} B = {page_count * page_size / 1e6:.1f} MB)"
        )
        for chave, valor in conn.execute("SELECT key, value FROM app_meta ORDER BY key"):
            texto = str(valor)
            print(f"db-check: app_meta[{chave}] = {texto[:110]}{'…' if len(texto) > 110 else ''}")
        if n == 0:
            print("db-check: banco vazio, nada a medir")
            return 0

        frequente, seletivo = _termos_do_corpus(conn)
        junta = (
            "SELECT p.id, p.uid FROM prompts_fts JOIN prompts p ON p.id = prompts_fts.rowid "
            "WHERE prompts_fts MATCH ? LIMIT 50"
        )
        consultas: list[tuple[str, str, tuple[Any, ...], bool]] = [
            (f"Q1 FTS seletiva ({seletivo!r}) LIMIT 50", junta, (fts_query(seletivo),), True),
            (f"Q2 FTS frequente ({frequente!r}) LIMIT 50", junta, (fts_query(frequente),), True),
            (
                f"Q3 COUNT(*) do MATCH {frequente!r}",
                "SELECT count(*) FROM prompts_fts WHERE prompts_fts MATCH ?",
                (fts_query(frequente),),
                False,  # varre a posting list inteira: não se cobra meta aqui
            ),
            (
                "Q4 filtro facetado (lang+task+domain)",
                "SELECT id FROM prompts WHERE lang = ? AND task_type IS NOT NULL "
                "AND domain IS NOT NULL LIMIT 50",
                ("pt",),
                True,
            ),
            (
                "Q5 GROUP BY task_type WHERE lang=?",
                "SELECT task_type, count(*) FROM prompts WHERE lang = ? GROUP BY task_type",
                ("pt",),
                True,
            ),
            (
                "Q6 paginação OFFSET 1000",
                "SELECT id, uid FROM prompts ORDER BY id LIMIT 50 OFFSET 1000",
                (),
                True,
            ),
        ]

        linhas: list[list[Any]] = []
        estourou = 0
        for rotulo, sql, params, cobra in consultas:
            mediana, p95, achou = _medir(conn, sql, params, repeticoes)
            estado = "-" if not cobra else ("ok" if mediana < _BENCH_META_MS else "ACIMA DA META")
            if cobra and mediana >= _BENCH_META_MS:
                estourou += 1
            linhas.append([rotulo, f"{mediana:.1f}", f"{p95:.1f}", achou, estado])
            plano = " | ".join(str(r[3]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params))
            linhas.append(["  plano: " + plano[:96], "", "", "", ""])

        larguras = [max(len(str(linha[i])) for linha in linhas) for i in range(5)]
        print(f"db-check: {'consulta'.ljust(larguras[0])}  mediana  p95      linhas  meta {_BENCH_META_MS:.0f} ms")
        for linha in linhas:
            print(
                f"db-check: {str(linha[0]).ljust(larguras[0])}  "
                f"{str(linha[1]).rjust(7)}  {str(linha[2]).rjust(7)}  "
                f"{str(linha[3]).rjust(6)}  {linha[4]}"
            )
        if estourou:
            print(f"db-check: {estourou} consulta(s) acima de {_BENCH_META_MS:.0f} ms")
        return 0
    finally:
        conn.close()


def run_db_check(
    bench: bool = False, query: str | None = None, db_path: str | Path | None = None
) -> int:
    """Autoteste do SQLite: DDL, WAL, FTS5 e busca sem acento. 0 = PASS.

    O autoteste roda inteiro num diretório temporário — nunca toca ``data/db/``.
    O ``--bench``, esse sim, mede o banco REAL (só leitura).
    """
    print(f"db-check: SQLite {sqlite3.sqlite_version} (mínimo exigido {'.'.join(map(str, MIN_SQLITE))})")

    falhas: list[str] = []
    # ignore_cleanup_errors: no Windows os arquivos -wal/-shm às vezes seguem
    # com handle aberto por um instante depois do close().
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        # NÃO reaproveitar o nome ``db_path``: ele é o parâmetro que diz qual
        # banco REAL o --bench mede, e sombreá-lo aqui faria o bench medir um
        # temporário já apagado (e reportar "sem banco" sempre).
        temporario = Path(tmp) / "db-check.sqlite"
        conn: sqlite3.Connection | None = None
        try:
            conn = connect(temporario)
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
    # O bench vem depois do PASS de propósito: medir um banco real não faz
    # sentido se a própria build do SQLite estiver quebrada.
    if bench:
        return run_bench(db_path)
    return 0


__all__ = [
    "DDL",
    "INDEXES",
    "MIN_SQLITE",
    "SCHEMA_VERSION",
    "TABLES",
    "TRIGGERS",
    "connect",
    "fts_query",
    "fts_search",
    "get_meta",
    "init_db",
    "run_bench",
    "run_db_check",
    "set_meta",
]
