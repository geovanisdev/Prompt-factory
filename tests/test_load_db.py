"""Testes do s11 (M8): carga bulk, rebuild do FTS, guardas e swap de arquivo.

Nada aqui toca ``data/``: o ``stage_dirs`` do conftest aponta o
``StageConfig.data_dir`` para ``tmp_path``, e ``DB_SQLITE`` é relativo a ele.

Três testes existem para impedir uma "simplificação" futura que quebraria a
carga em silêncio:

* ``test_integrity_check_detecta_indice_vazio`` — ``count(*) FROM prompts_fts``
  **não** detecta índice vazio (numa tabela de conteúdo externo o count lê a
  tabela de conteúdo);
* ``test_nenhum_valor_virou_blob`` — escalar numpy liga como BLOB sem reclamar
  nas colunas sem CHECK;
* ``test_triggers_voltam_ativos_depois_da_carga`` — a carga derruba os triggers
  e depende do ``executescript(DDL)`` para trazê-los de volta.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from prompt_factory import db as dbmod
from prompt_factory.stages import DB_SQLITE, SEED_LABELS, UNIVERSE, StageConfig
from prompt_factory.stages import s11_load_db as s11

from .conftest import escrever

#: Textos com acento (prova o remove_diacritics 2), cerca de código e tamanho
#: variado — o suficiente para o bench e para o MATCH de amostra.
_TEXTOS: tuple[str, ...] = (
    "Meu coração está partido e eu preciso de um conselho sincero sobre isso",
    "Explique como funciona um decorador em Python com exemplo de código completo",
    "Traduza para o inglês o parágrafo a seguir mantendo o tom formal do original",
    "```python\nprint('ola mundo')\n```\nO que este trecho imprime exatamente?",
    "Resuma em tres frases curtas o enredo principal do livro que eu descrevi antes",
)


def _universo(n: int = 50) -> pa.Table:
    """Universo sintético nas 27 colunas canônicas."""
    from prompt_factory.schema import COLUMN_NAMES, arrow_schema, text_stats

    from .conftest import DEFAULTS

    linhas: list[dict[str, Any]] = []
    for i in range(n):
        texto = f"{_TEXTOS[i % len(_TEXTOS)]} (item {i})"
        n_chars, n_words = text_stats(texto)
        linhas.append(
            {
                **DEFAULTS,
                "uid": f"{i:016x}",
                "text": texto,
                "text_raw": texto,
                "lang": "pt" if i % 2 == 0 else "en",
                "source": "aya" if i % 3 else "no_robots",
                "license": "apache-2.0" if i % 3 else "cc-by-nc-4.0",
                "commercial_ok": bool(i % 3),
                "redistributable": True,
                "source_id": str(i),
                "hash_norm": f"{i:064x}",
                "n_chars": n_chars,
                "n_words": n_words,
                "n_exact_dups": i % 4,
                "n_near_dups": 0,
                "variant_confidence": 0.75 if i % 2 == 0 else None,
                "lang_variant": "pt-BR" if i % 2 == 0 else None,
                "native_category": "Generation" if i % 3 == 0 else None,
            }
        )
    return pa.table(
        {col: [linha[col] for linha in linhas] for col in COLUMN_NAMES},
        schema=arrow_schema(),
    )


def _rotulos(uids: Sequence[str], **override: Any) -> pa.Table:
    """Parquet de rótulos no formato do s10 (``labeled.parquet``)."""
    n = len(uids)
    dados: dict[str, Any] = {
        "uid": list(uids),
        "task_type": ["codigo" if i % 2 else "qa-aberta" for i in range(n)],
        "domain": ["tecnologia" if i % 2 else "geral" for i in range(n)],
        "quality": [(i % 3) + 1 for i in range(n)],
        "nsfw": [False] * n,
        "label_method": ["classifier"] * n,
        "label_confidence": [0.9 if i % 5 else 0.4 for i in range(n)],
        "needs_review": [0 if i % 5 else 1 for i in range(n)],
    }
    dados.update(override)
    return pa.table(
        dados,
        schema=pa.schema(
            [
                pa.field("uid", pa.string(), nullable=False),
                pa.field("task_type", pa.string(), nullable=True),
                pa.field("domain", pa.string(), nullable=True),
                pa.field("quality", pa.int8(), nullable=True),
                pa.field("nsfw", pa.bool_(), nullable=True),
                pa.field("label_method", pa.string(), nullable=True),
                pa.field("label_confidence", pa.float32(), nullable=True),
                pa.field("needs_review", pa.int8(), nullable=True),
            ]
        ),
    )


@pytest.fixture
def universo(stage_dirs: StageConfig) -> pa.Table:
    """Grava ``final/universe.parquet`` com 50 linhas e devolve a tabela."""
    tabela = _universo()
    escrever(tabela, stage_dirs.caminho(UNIVERSE))
    return tabela


@pytest.fixture
def carregado(stage_dirs: StageConfig, universo: pa.Table) -> StageConfig:
    """Universo + rótulos para 49 das 50 linhas (uma fica sem rótulo)."""
    uids = universo.column("uid").to_pylist()
    escrever(_rotulos(uids[:-1]), stage_dirs.caminho("final/labeled.parquet"))
    return stage_dirs


def _cfg(base: StageConfig, **extra: Any) -> StageConfig:
    from dataclasses import replace

    return replace(base, **extra)


def _abrir(cfg: StageConfig) -> sqlite3.Connection:
    return dbmod.connect(cfg.caminho(DB_SQLITE))


# ---------------------------------------------------------------------------
# carga
# ---------------------------------------------------------------------------


def test_carga_completa_e_uid_sem_rotulo(carregado: StageConfig) -> None:
    # 1 de 50 sem rótulo = 2% > 1% do default, então o teto precisa subir.
    assert s11.run(_cfg(carregado, allow_unlabeled_pct=5.0)) == 0

    conn = _abrir(carregado)
    try:
        assert conn.execute("SELECT count(*) AS n FROM prompts").fetchone()["n"] == 50
        assert conn.execute("SELECT count(DISTINCT uid) AS n FROM prompts").fetchone()["n"] == 50

        orfa = conn.execute(
            "SELECT task_type, domain, needs_review, label_method, label_confidence "
            "FROM prompts WHERE uid = ?",
            (f"{49:016x}",),
        ).fetchone()
        assert orfa["task_type"] is None
        assert orfa["domain"] is None
        assert orfa["needs_review"] == 1
        assert orfa["label_method"] is None
        assert orfa["label_confidence"] is None

        # text_original NULL = nunca editado (é o que economiza ~800 MB).
        assert conn.execute(
            "SELECT count(*) AS n FROM prompts WHERE text_original IS NOT NULL"
        ).fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM prompts WHERE edited = 1").fetchone()["n"] == 0
    finally:
        conn.close()


def test_carga_sem_rotulo_nenhum_falha_no_default_e_passa_com_teto(
    stage_dirs: StageConfig, universo: pa.Table, capsys: pytest.CaptureFixture[str]
) -> None:
    # Sem labeled.parquet nem seed_labels.parquet: é o estado real de hoje.
    assert s11.run(_cfg(stage_dirs)) == 1
    saida = capsys.readouterr().out
    assert "RECUSADO" in saida and "100.00%" in saida
    assert not stage_dirs.caminho(DB_SQLITE).exists()

    assert s11.run(_cfg(stage_dirs, allow_unlabeled_pct=100.0)) == 0
    conn = _abrir(stage_dirs)
    try:
        assert conn.execute("SELECT count(*) AS n FROM prompts").fetchone()["n"] == 50
        assert conn.execute(
            "SELECT count(*) AS n FROM prompts WHERE needs_review = 1"
        ).fetchone()["n"] == 50
        assert conn.execute(
            "SELECT count(*) AS n FROM prompts WHERE task_type IS NULL"
        ).fetchone()["n"] == 50
        # O FTS tem de funcionar mesmo num banco sem um rótulo sequer.
        assert dbmod.fts_search(conn, "coracao")
    finally:
        conn.close()


def test_fts_acha_sem_acento_depois_do_rebuild(carregado: StageConfig) -> None:
    assert s11.run(_cfg(carregado, allow_unlabeled_pct=5.0)) == 0
    conn = _abrir(carregado)
    try:
        com = [r["id"] for r in dbmod.fts_search(conn, "coração")]
        sem = [r["id"] for r in dbmod.fts_search(conn, "coracao")]
        assert com and com == sem
        assert len(dbmod.fts_search(conn, "decorador")) == 10
    finally:
        conn.close()


def test_triggers_voltam_ativos_depois_da_carga(carregado: StageConfig) -> None:
    """Prova o passo do ``executescript(DDL)``: a carga derruba os triggers."""
    assert s11.run(_cfg(carregado, allow_unlabeled_pct=5.0)) == 0
    conn = _abrir(carregado)
    try:
        triggers = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        assert triggers == set(dbmod.TRIGGERS)

        alvo = dbmod.fts_search(conn, "coracao")[0]["id"]
        conn.execute("UPDATE prompts SET text = ? WHERE id = ?", ("Agora tudo alegria", alvo))
        assert alvo not in [r["id"] for r in dbmod.fts_search(conn, "coracao")]
        assert [r["id"] for r in dbmod.fts_search(conn, "alegria")] == [alvo]

        conn.execute("DELETE FROM prompts WHERE id = ?", (alvo,))
        assert dbmod.fts_search(conn, "alegria") == []
    finally:
        conn.close()


def test_indices_recriados(carregado: StageConfig) -> None:
    assert s11.run(_cfg(carregado, allow_unlabeled_pct=5.0)) == 0
    conn = _abrir(carregado)
    try:
        indices = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_prompts_%'"
            )
        }
        assert indices == set(dbmod.INDEXES)
        # O composto tem de ser COBERTURA (o plano nem toca na tabela).
        plano = " ".join(
            str(r[3])
            for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT count(*) FROM prompts "
                "WHERE lang = 'pt' AND task_type = 'codigo' AND domain = 'tecnologia'"
            )
        )
        assert "COVERING INDEX idx_prompts_facets" in plano
    finally:
        conn.close()


def test_nenhum_valor_virou_blob(carregado: StageConfig) -> None:
    """Escalar numpy liga como BLOB **em silêncio** nas colunas sem CHECK."""
    assert s11.run(_cfg(carregado, allow_unlabeled_pct=5.0)) == 0
    conn = _abrir(carregado)
    try:
        assert conn.execute(s11.SQL_TIPOS).fetchone()["n"] == 0
        # sum() ignora BLOB: se algum n_chars tivesse virado BLOB, a soma abaixo
        # ficaria menor que a contagem de linhas x 1 caractere.
        tipos = {
            r["t"]
            for r in conn.execute("SELECT DISTINCT typeof(n_chars) AS t FROM prompts")
        }
        assert tipos == {"integer"}
        assert conn.execute(
            "SELECT count(*) AS n FROM prompts WHERE n_chars > 10"
        ).fetchone()["n"] == 50
    finally:
        conn.close()


def test_licenca_e_flags_preservadas_por_linha(carregado: StageConfig) -> None:
    """Protege o export do M9: no_robots é cc-by-nc (commercial_ok = 0)."""
    assert s11.run(_cfg(carregado, allow_unlabeled_pct=5.0)) == 0
    conn = _abrir(carregado)
    try:
        linhas = {
            (r["license"], r["commercial_ok"], r["redistributable"]): r["n"]
            for r in conn.execute(
                "SELECT license, commercial_ok, redistributable, count(*) AS n "
                "FROM prompts GROUP BY 1, 2, 3"
            )
        }
        assert linhas == {("apache-2.0", 1, 1): 33, ("cc-by-nc-4.0", 0, 1): 17}
    finally:
        conn.close()


def test_app_meta_preenchido(carregado: StageConfig) -> None:
    from prompt_factory import __version__
    from prompt_factory.schema import TAXONOMY_VERSION

    assert s11.run(_cfg(carregado, allow_unlabeled_pct=5.0)) == 0
    conn = _abrir(carregado)
    try:
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM app_meta")}
        assert meta["taxonomy_version"] == TAXONOMY_VERSION
        assert meta["schema_version"] == str(dbmod.SCHEMA_VERSION)
        assert meta["n_rows"] == "50"
        assert meta["pf_version"] == __version__
        assert len(meta["db_build_id"]) == 16
        assert meta["built_at"].endswith("Z")
        assert len(meta["universe_sha256"]) == 64
        assert meta["unlabeled_pct"] == "0.0200"
        assert "conf_accept" in meta["thresholds_json"]
        assert meta["pipeline_git_dirty"] in {"0", "1"}
    finally:
        conn.close()


def test_build_id_e_deterministico(carregado: StageConfig) -> None:
    """Mesmas entradas ⇒ mesmo id. É por isso que não é um uuid4."""
    ids = []
    for _ in range(2):
        assert s11.run(_cfg(carregado, allow_unlabeled_pct=5.0)) == 0
        conn = _abrir(carregado)
        try:
            ids.append(dbmod.get_meta(conn, "db_build_id"))
            assert conn.execute("SELECT count(*) AS n FROM prompts").fetchone()["n"] == 50
        finally:
            conn.close()
    assert ids[0] == ids[1]


# ---------------------------------------------------------------------------
# precedência dos rótulos
# ---------------------------------------------------------------------------


def test_seed_humano_vence_a_predicao_e_native_nao(
    stage_dirs: StageConfig, universo: pa.Table
) -> None:
    uids = universo.column("uid").to_pylist()
    escrever(_rotulos(uids), stage_dirs.caminho("final/labeled.parquet"))
    # uid[0]: rótulo de agente (observado) → vence. uid[1]: nativo → perde.
    # uid[49] não está no labeled... está; então o seed só testa a precedência.
    seed = pa.table(
        {
            "uid": [uids[0], uids[1]],
            "task_type": ["traducao", "resumo"],
            "domain": ["linguagem-idiomas", "geral"],
            "quality": pa.array([3, 2], type=pa.int8()),
            "nsfw": [False, False],
            "label_method": ["agent", "native"],
            "label_weight": pa.array([1.0, 0.5], type=pa.float32()),
            "batch_id": ["batch_0001", None],
        }
    )
    escrever(seed, stage_dirs.caminho(SEED_LABELS))

    assert s11.run(_cfg(stage_dirs)) == 0
    conn = _abrir(stage_dirs)
    try:
        primeira = conn.execute(
            "SELECT task_type, label_method, label_confidence, needs_review "
            "FROM prompts WHERE uid = ?",
            (uids[0],),
        ).fetchone()
        assert primeira["task_type"] == "traducao"
        assert primeira["label_method"] == "agent"
        # Confiança de humano NÃO é probabilidade: NULL é o valor honesto.
        assert primeira["label_confidence"] is None
        assert primeira["needs_review"] == 0

        segunda = conn.execute(
            "SELECT task_type, label_method FROM prompts WHERE uid = ?", (uids[1],)
        ).fetchone()
        assert segunda["task_type"] != "resumo"
        assert segunda["label_method"] == "classifier"
    finally:
        conn.close()


def test_seed_sozinho_rotula_o_banco(stage_dirs: StageConfig, universo: pa.Table) -> None:
    """Sem o s10, o seed (inclusive o nativo) é a única origem — e vale."""
    uids = universo.column("uid").to_pylist()
    seed = pa.table(
        {
            "uid": uids,
            "task_type": ["codigo"] * len(uids),
            "domain": [None] * len(uids),  # nativo só cobre task_type
            "quality": pa.array([None] * len(uids), type=pa.int8()),
            "nsfw": [None] * len(uids),
            "label_method": ["native"] * len(uids),
            "label_weight": pa.array([0.5] * len(uids), type=pa.float32()),
            "batch_id": [None] * len(uids),
        }
    )
    escrever(seed, stage_dirs.caminho(SEED_LABELS))

    assert s11.run(_cfg(stage_dirs)) == 0
    conn = _abrir(stage_dirs)
    try:
        # task_type preenchido, mas domain faltando ⇒ fila de revisão.
        assert conn.execute(
            "SELECT count(*) AS n FROM prompts WHERE task_type = 'codigo' AND domain IS NULL "
            "AND needs_review = 1 AND label_method = 'native'"
        ).fetchone()["n"] == 50
    finally:
        conn.close()


def test_labels_all_e_aceito_como_alias(stage_dirs: StageConfig, universo: pa.Table) -> None:
    uids = universo.column("uid").to_pylist()
    escrever(_rotulos(uids), stage_dirs.caminho("final/labels_all.parquet"))
    assert s11.run(_cfg(stage_dirs)) == 0
    conn = _abrir(stage_dirs)
    try:
        assert conn.execute(
            "SELECT count(*) AS n FROM prompts WHERE task_type IS NOT NULL"
        ).fetchone()["n"] == 50
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# guardas
# ---------------------------------------------------------------------------


def test_integrity_check_detecta_indice_vazio(carregado: StageConfig) -> None:
    """A guarda óbvia (``count(*) FROM prompts_fts``) NÃO detecta índice vazio.

    Este teste é a trava contra alguém "simplificar" ``verificar()`` de volta
    para o count.
    """
    assert s11.run(_cfg(carregado, allow_unlabeled_pct=5.0)) == 0
    caminho = carregado.caminho(DB_SQLITE)

    assert s11.verificar(caminho, n_esperado=50, n_revisao=11) == []

    conn = dbmod.connect(caminho)
    try:
        for trigger in dbmod.TRIGGERS:  # senão o delete-all dispararia o sync
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute("INSERT INTO prompts_fts(prompts_fts) VALUES('delete-all')")
        # O count continua mentindo 50 (lê a tabela de conteúdo externa).
        assert conn.execute("SELECT count(*) AS n FROM prompts_fts").fetchone()["n"] == 50
    finally:
        conn.close()

    falhas = s11.verificar(caminho, n_esperado=50, n_revisao=11)
    assert any("integrity-check" in f for f in falhas), falhas


def test_verificar_pega_contagem_errada(carregado: StageConfig) -> None:
    assert s11.run(_cfg(carregado, allow_unlabeled_pct=5.0)) == 0
    falhas = s11.verificar(carregado.caminho(DB_SQLITE), n_esperado=51, n_revisao=11)
    assert any("count(prompts)" in f for f in falhas), falhas


# ---------------------------------------------------------------------------
# swap
# ---------------------------------------------------------------------------


def test_no_swap_deixa_o_build_e_nao_troca(carregado: StageConfig) -> None:
    assert s11.run(_cfg(carregado, allow_unlabeled_pct=5.0, swap=False)) == 0
    assert carregado.caminho("db/prompts.build.sqlite").is_file()
    assert not carregado.caminho(DB_SQLITE).exists()

    # --swap-only completa o trabalho sem reconstruir nada.
    assert s11.run(_cfg(carregado, swap_only=True)) == 0
    assert carregado.caminho(DB_SQLITE).is_file()
    assert not carregado.caminho("db/prompts.build.sqlite").exists()


def test_swap_limpa_wal_shm_orfaos(carregado: StageConfig) -> None:
    destino = carregado.caminho(DB_SQLITE)
    destino.parent.mkdir(parents=True, exist_ok=True)
    # Banco antigo + sidecars órfãos: aplicar o WAL de OUTRO arquivo corromperia.
    antigo = dbmod.connect(destino)
    dbmod.init_db(antigo)
    antigo.close()
    for sufixo in ("-wal", "-shm"):
        Path(f"{destino}{sufixo}").write_bytes(b"lixo de um crash antigo")

    assert s11.run(_cfg(carregado, allow_unlabeled_pct=5.0, backup=True)) == 0
    assert not Path(f"{destino}-wal").exists()
    assert not Path(f"{destino}-shm").exists()
    assert Path(f"{destino}.bak").is_file()


@pytest.mark.skipif(sys.platform != "win32", reason="o lock de arquivo é do Windows")
def test_swap_recusado_com_o_banco_aberto(carregado: StageConfig) -> None:
    """Com o banco aberto por outro processo, o swap recusa e NÃO apaga o build."""
    assert s11.run(_cfg(carregado, allow_unlabeled_pct=5.0)) == 0
    destino = carregado.caminho(DB_SQLITE)
    build = carregado.caminho("db/prompts.build.sqlite")

    # Reconstrói para ter um build pendente e segura o destino aberto.
    assert s11.run(_cfg(carregado, allow_unlabeled_pct=5.0, swap=False)) == 0
    # O handle aberto É o objeto do teste: fechá-lo com `with` tiraria o lock.
    aberto = open(destino, "rb")
    try:
        assert s11.trocar(build, destino, backup=False) == s11.EXIT_SWAP_BLOQUEADO
        assert build.is_file(), "o build é o produto pronto — nunca apagar no erro"
    finally:
        aberto.close()

    assert s11.trocar(build, destino, backup=False) == 0


def test_destino_ocupado_detecta_lock(carregado: StageConfig) -> None:
    assert s11.run(_cfg(carregado, allow_unlabeled_pct=5.0)) == 0
    destino = carregado.caminho(DB_SQLITE)
    assert s11.destino_ocupado(destino) is None
    assert s11.destino_ocupado(destino.parent / "nao-existe.sqlite") is None

    trava = dbmod.connect(destino)
    try:
        trava.execute("BEGIN EXCLUSIVE")
        motivo = s11.destino_ocupado(destino)
        assert motivo is not None and "travado" in motivo
    finally:
        trava.execute("ROLLBACK")
        trava.close()


# ---------------------------------------------------------------------------
# bench
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_load_db_mapeia_as_flags(carregado: StageConfig) -> None:
    """O mapeamento ``--no-swap`` -> ``swap=False`` é o tipo de coisa que
    inverte em silêncio; só um teste pelo caminho da CLI pega."""
    from prompt_factory import cli

    raiz = str(carregado.data_dir)
    assert cli.main(["load-db", "--data-dir", raiz, "--allow-unlabeled-pct", "5", "--no-swap"]) == 0
    assert carregado.caminho("db/prompts.build.sqlite").is_file()
    assert not carregado.caminho(DB_SQLITE).exists()

    assert cli.main(["load-db", "--data-dir", raiz, "--swap-only"]) == 0
    assert carregado.caminho(DB_SQLITE).is_file()

    # Sem o teto explícito, 2% de linhas sem rótulo derruba a carga.
    assert cli.main(["load-db", "--data-dir", raiz]) == 1


def test_bench_sem_banco_avisa_e_sai_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert dbmod.run_bench(tmp_path / "nao-existe.sqlite") == 0
    assert "sem banco" in capsys.readouterr().out


def test_bench_mede_o_banco_carregado(
    carregado: StageConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert s11.run(_cfg(carregado, allow_unlabeled_pct=5.0)) == 0
    assert dbmod.run_bench(carregado.caminho(DB_SQLITE), repeticoes=2) == 0
    saida = capsys.readouterr().out
    for marca in ("Q1 FTS seletiva", "Q3 COUNT(*)", "Q6 paginação", "app_meta[db_build_id]"):
        assert marca in saida, saida
