"""Testes do SQLite (M1): DDL, PRAGMAs, FTS5 sem acento, CHECKs e taxonomia.

Toda conexão sai de ``db.connect``: é ela que liga ``foreign_keys``, que é um
PRAGMA POR CONEXÃO. Abrir com ``sqlite3.connect`` direto faria os testes de
CASCADE passarem em falso.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from prompt_factory import db, schema

_DEFAULTS: dict[str, object] = {
    "uid": "0123456789abcdef",
    "text": "Meu coração está partido",
    "text_original": "Meu coração está partido",
    "lang": "pt",
    "source": "teste",
    "license": "unknown",
    "hash_norm": "deadbeef",
    "n_chars": 24,
    "n_words": 4,
}


def _insert(conn: sqlite3.Connection, **overrides: object) -> int:
    """Insere uma linha válida em ``prompts``; devolve o id."""
    linha = {**_DEFAULTS, **overrides}
    colunas = ", ".join(linha)
    marcas = ", ".join("?" for _ in linha)
    cur = conn.execute(
        f"INSERT INTO prompts ({colunas}) VALUES ({marcas})", tuple(linha.values())
    )
    return int(cur.lastrowid or 0)


@pytest.fixture
def conn(tmp_path: Path):
    """Banco novo por teste, já com o DDL aplicado."""
    conexao = db.connect(tmp_path / "prompts.sqlite")
    db.init_db(conexao)
    yield conexao
    conexao.close()


def test_ddl_idempotente_e_objetos_criados(conn: sqlite3.Connection) -> None:
    db.init_db(conn)  # segunda vez não pode explodir (tudo é IF NOT EXISTS)

    tabelas = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    # As shadow tables do FTS5 (prompts_fts_data etc.) também aparecem aqui.
    assert set(db.TABLES) <= tabelas, f"faltando: {set(db.TABLES) - tabelas}"

    triggers = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }
    assert triggers == set(db.TRIGGERS)


def test_pragmas_da_conexao(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_conexao_readonly(tmp_path: Path) -> None:
    # A URI de readonly é montada à mão e a forma muda entre Windows e POSIX;
    # este teste é a guarda contra "unable to open database file".
    caminho = tmp_path / "prompts.sqlite"
    escrita = db.connect(caminho)
    db.init_db(escrita)
    db.set_meta(escrita, "k", "v")
    escrita.close()

    leitura = db.connect(caminho, readonly=True)
    try:
        assert db.get_meta(leitura, "k") == "v"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            leitura.execute("INSERT INTO app_meta (key, value) VALUES ('x','y')")
    finally:
        leitura.close()


def test_fts_acha_com_e_sem_acento(conn: sqlite3.Connection) -> None:
    rowid = _insert(conn)
    assert [r["id"] for r in db.fts_search(conn, "coracao")] == [rowid]
    assert [r["id"] for r in db.fts_search(conn, "coração")] == [rowid]
    assert [r["id"] for r in db.fts_search(conn, "CORACAO")] == [rowid]
    assert db.fts_search(conn, "alegria") == []


def test_fts_ressincroniza_no_update(conn: sqlite3.Connection) -> None:
    rowid = _insert(conn)
    conn.execute("UPDATE prompts SET text = ? WHERE id = ?", ("Agora feliz", rowid))
    assert db.fts_search(conn, "coracao") == []
    assert [r["id"] for r in db.fts_search(conn, "feliz")] == [rowid]


def test_fts_ressincroniza_no_delete(conn: sqlite3.Connection) -> None:
    rowid = _insert(conn)
    conn.execute("DELETE FROM prompts WHERE id = ?", (rowid,))
    assert db.fts_search(conn, "coracao") == []


def test_check_de_lang(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, uid="lang-invalido", lang="es")
    _insert(conn, uid="lang-en", lang="en", lang_variant=None)


def test_checks_de_rotulo(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, uid="q5", quality=5)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, uid="metodo-torto", label_method="vibes")
    # Não rotulado ainda é o estado normal de quase todo o banco.
    rowid = _insert(conn, uid="sem-rotulo", quality=None, task_type=None, domain=None)
    assert conn.execute(
        "SELECT quality FROM prompts WHERE id = ?", (rowid,)
    ).fetchone()["quality"] is None


def test_uid_unico(conn: sqlite3.Connection) -> None:
    _insert(conn, uid="repetido")
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, uid="repetido")


def test_collections_cascade_dos_dois_lados(conn: sqlite3.Connection) -> None:
    p1 = _insert(conn, uid="p1")
    p2 = _insert(conn, uid="p2")
    conn.execute("INSERT INTO collections (name) VALUES ('favoritos')")
    col = int(conn.execute("SELECT id FROM collections").fetchone()["id"])
    conn.executemany(
        "INSERT INTO collection_items (collection_id, prompt_id) VALUES (?, ?)",
        [(col, p1), (col, p2)],
    )

    # FK inválida é barrada (só com foreign_keys=ON — ver docstring do módulo).
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO collection_items (collection_id, prompt_id) VALUES (?, ?)",
            (col, 999999),
        )

    # Apagar o prompt tira o item da coleção...
    conn.execute("DELETE FROM prompts WHERE id = ?", (p1,))
    assert conn.execute("SELECT count(*) AS n FROM collection_items").fetchone()["n"] == 1
    # ...e apagar a coleção leva o resto junto.
    conn.execute("DELETE FROM collections WHERE id = ?", (col,))
    assert conn.execute("SELECT count(*) AS n FROM collection_items").fetchone()["n"] == 0


def test_app_meta_roundtrip(conn: sqlite3.Connection) -> None:
    assert db.get_meta(conn, "schema_version") == str(db.SCHEMA_VERSION)
    assert db.get_meta(conn, "nao-existe") is None
    db.set_meta(conn, "taxonomy_version", schema.TAXONOMY_VERSION)
    db.set_meta(conn, "taxonomy_version", "1.1")  # upsert, não duplica
    assert db.get_meta(conn, "taxonomy_version") == "1.1"
    assert conn.execute(
        "SELECT count(*) AS n FROM app_meta WHERE key = 'taxonomy_version'"
    ).fetchone()["n"] == 1


def test_taxonomia_bate_com_o_schema() -> None:
    tax = schema.load_taxonomy()  # já valida versão, chaves e ORDEM
    assert tax["version"] == schema.TAXONOMY_VERSION == "1.0"
    assert tuple(tax["task_type"]) == schema.TASK_TYPES
    assert tuple(tax["domain"]) == schema.DOMAINS
    assert len(schema.TASK_TYPES) == len(schema.DOMAINS) == 16

    for secao in ("task_type", "domain"):
        for chave, classe in tax[secao].items():
            assert classe["nome"].strip(), f"{secao}.{chave} sem nome"
            assert len(classe["definicao"]) > 40, f"{secao}.{chave} com definição rasa"
            assert len(classe["exemplos"]) >= 2, f"{secao}.{chave} com poucos exemplos"

    assert set(tax["flags"]["quality"]["valores"]) == {"1", "2", "3"}
    assert set(map(int, tax["flags"]["quality"]["valores"])) == set(schema.QUALITY_VALUES)


def test_load_taxonomy_recusa_divergencia(tmp_path: Path) -> None:
    tax = schema.load_taxonomy()
    tax["task_type"] = dict(reversed(list(tax["task_type"].items())))
    fora_de_ordem = tmp_path / "taxonomy.json"
    fora_de_ordem.write_text(json.dumps(tax, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="ORDEM"):
        schema.load_taxonomy(fora_de_ordem)


def test_db_check_passa() -> None:
    assert db.run_db_check() == 0
