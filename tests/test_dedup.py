"""Dedup exato (s04) e próximo (s06): escolha do canônico, union-find e limiares.

Tudo offline e sintético: o s06 recebe um `.npy` **crafteado**, com vetores que
eu escolhi, para que o teste prove a lógica de decisão e não a qualidade do
modelo de embeddings (isso é outro assunto, e não é testável assim).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from prompt_factory import dedup
from prompt_factory.schema import License
from prompt_factory.stages import DEDUP1, MAPA_EXATO, SCRUBBED, StageConfig
from prompt_factory.stages import s04_dedup_exact as s04
from prompt_factory.stages import s06_dedup_near as s06

from .conftest import escrever

# ---------------------------------------------------------------------------
# ranks e canônico
# ---------------------------------------------------------------------------


def test_license_rank_cobre_todo_o_enum() -> None:
    # Licença nova em schema.License sem rank aqui faria o dedup preferir a
    # cópia errada em silêncio. O módulo já falha no import; isto documenta.
    assert set(dedup.LICENSE_RANK) == {lic.value for lic in License}
    assert len(set(dedup.LICENSE_RANK.values())) == len(dedup.LICENSE_RANK)


def test_licenca_mais_permissiva_vence() -> None:
    grupo = [
        {"uid": "a", "license": "cc-by-nc-4.0", "source": "no_robots", "source_id": "1"},
        {"uid": "b", "license": "apache-2.0", "source": "aya", "source_id": "2"},
    ]
    assert dedup.choose_canonical(grupo)["uid"] == "b"
    assert dedup.choose_canonical(list(reversed(grupo)))["uid"] == "b"


def test_desempate_pela_ordem_do_sources_toml() -> None:
    ranks = dedup.source_rank()
    assert ranks["wildchat_pt"] < ranks["aya"] < ranks["prism"]
    grupo = [
        {"uid": "a", "license": "apache-2.0", "source": "aya", "source_id": "9"},
        {"uid": "b", "license": "apache-2.0", "source": "oasst", "source_id": "1"},
    ]
    # oasst vem depois de aya no TOML: aya ganha mesmo com source_id "maior".
    assert dedup.choose_canonical(grupo)["uid"] == "a"


def test_desempate_por_source_id_e_uid() -> None:
    base = {"license": "mit", "source": "hh_rlhf"}
    com_id = {**base, "uid": "z", "source_id": "007"}
    sem_id = {**base, "uid": "a", "source_id": None}
    assert dedup.choose_canonical([sem_id, com_id])["uid"] == "z"
    dois = [
        {**base, "uid": "b", "source_id": "1"},
        {**base, "uid": "a", "source_id": "1"},
    ]
    assert dedup.choose_canonical(dois)["uid"] == "a"


def test_fonte_desconhecida_fica_por_ultimo() -> None:
    grupo = [
        {"uid": "a", "license": "mit", "source": "fonte_inexistente", "source_id": "1"},
        {"uid": "b", "license": "mit", "source": "dolly", "source_id": "2"},
    ]
    assert dedup.choose_canonical(grupo)["uid"] == "b"


def test_grupo_vazio_e_erro() -> None:
    with pytest.raises(ValueError, match="grupo vazio"):
        dedup.choose_canonical([])


# ---------------------------------------------------------------------------
# union-find e jaccard
# ---------------------------------------------------------------------------


def test_union_find_e_transitivo() -> None:
    uf = dedup.UnionFind(6)
    assert uf.union(0, 1)
    assert uf.union(1, 2)
    assert not uf.union(0, 2)  # já estavam juntos
    uf.union(4, 5)
    grupos = uf.groups()
    assert sorted(sorted(m) for m in grupos.values()) == [[0, 1, 2], [4, 5]]
    assert uf.size(2) == 3
    assert uf.find(3) == 3  # elemento solto não vira grupo


def test_jaccard_bordas() -> None:
    assert dedup.jaccard(frozenset(), frozenset()) == 1.0
    assert dedup.jaccard(frozenset(), {"a"}) == 0.0
    assert dedup.jaccard({"a"}, frozenset()) == 0.0
    assert dedup.jaccard({"a", "b"}, {"c", "d"}) == 0.0
    assert dedup.jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert dedup.jaccard({"a", "b", "c"}, {"a", "b"}) == pytest.approx(2 / 3)


def test_tokens_usam_a_normalizacao_do_hash() -> None:
    assert dedup.tokens("Olá,  MUNDO!!") == dedup.tokens("ola, mundo")
    assert dedup.tokens("!!!") == frozenset()


def test_razao_de_tamanho() -> None:
    assert dedup.razao_tamanho(100, 50) == 2.0
    assert dedup.razao_tamanho(50, 100) == 2.0
    assert dedup.razao_tamanho(0, 0) == 1.0
    assert dedup.razao_tamanho(10, 0) == float("inf")


# ---------------------------------------------------------------------------
# s04 fim a fim
# ---------------------------------------------------------------------------


def _rodar_s04(cfg: StageConfig, tabela: pa.Table) -> tuple[pa.Table, pa.Table]:
    escrever(tabela, cfg.caminho(SCRUBBED))
    assert s04.run(cfg) == 0
    return (
        pq.read_table(cfg.caminho(DEDUP1)),
        pq.read_table(cfg.caminho(MAPA_EXATO)),
    )


def test_s04_colapsa_duplicatas_e_conta(stage_dirs: StageConfig, make_table) -> None:
    tabela = make_table(
        [
            {"uid": "aaa", "text": "Olá mundo!!", "source": "wildchat_pt",
             "license": "odc-by-1.0", "source_id": "1"},
            {"uid": "bbb", "text": "ola mundo", "source": "aya",
             "license": "apache-2.0", "source_id": "2"},
            {"uid": "ccc", "text": "OLÁ MUNDO", "source": "arena140k",
             "license": "cc-by-4.0", "source_id": "3"},
            {"uid": "ddd", "text": "outro prompt qualquer", "source": "aya",
             "license": "apache-2.0", "source_id": "4"},
        ]
    )
    universo, mapa = _rodar_s04(stage_dirs, tabela)

    assert universo.num_rows == 2
    uids = universo.column("uid").to_pylist()
    # apache-2.0 é a licença mais permissiva do grupo: o aya vence.
    assert "bbb" in uids and "ddd" in uids
    dups = dict(zip(uids, universo.column("n_exact_dups").to_pylist(), strict=True))
    assert dups == {"bbb": 2, "ddd": 0}

    assert mapa.num_rows == 2
    assert set(mapa.column("uid").to_pylist()) == {"aaa", "ccc"}
    assert set(mapa.column("canonical_uid").to_pylist()) == {"bbb"}
    assert set(mapa.column("canonical_source").to_pylist()) == {"aya"}


def test_s04_grava_dup_sources_no_meta(stage_dirs: StageConfig, make_table) -> None:
    import json

    tabela = make_table(
        [
            {"uid": "aaa", "text": "mesmo texto", "source": "aya",
             "license": "apache-2.0", "source_id": "1", "meta_json": '{"x":1}'},
            {"uid": "bbb", "text": "mesmo texto", "source": "wildchat_pt",
             "license": "odc-by-1.0", "source_id": "2"},
        ]
    )
    universo, _mapa = _rodar_s04(stage_dirs, tabela)
    meta = json.loads(universo.column("meta_json").to_pylist()[0])
    assert meta["x"] == 1
    assert meta["dup_sources"] == ["wildchat_pt"]


def test_s04_nao_agrupa_chave_vazia(stage_dirs: StageConfig, make_table) -> None:
    # norm_for_hash("!!!") == "": todo prompt só de pontuação tem o MESMO
    # hash_norm. Colapsar isso apagaria linhas distintas.
    tabela = make_table(
        [
            {"uid": "aaa", "text": "!!!", "source_id": "1"},
            {"uid": "bbb", "text": "???", "source_id": "2"},
            {"uid": "ccc", "text": "...", "source_id": "3"},
        ]
    )
    universo, mapa = _rodar_s04(stage_dirs, tabela)
    assert universo.num_rows == 3
    assert mapa.num_rows == 0
    assert set(universo.column("hash_norm").to_pylist()) == {s04.HASH_VAZIO}


def test_s04_preserva_quality_int8_com_nulos(stage_dirs: StageConfig, make_table) -> None:
    # A regressão do pandas 3: se a tabela canônica desse uma volta por
    # DataFrame, quality viraria float64 e 3 viraria 3.0 no export.
    tabela = make_table(
        [
            {"uid": "aaa", "text": "prompt um", "quality": 1, "source_id": "1"},
            {"uid": "bbb", "text": "prompt dois", "quality": None, "source_id": "2"},
            {"uid": "ccc", "text": "prompt tres", "quality": 3, "source_id": "3"},
        ]
    )
    universo, _mapa = _rodar_s04(stage_dirs, tabela)
    assert universo.schema.field("quality").type == pa.int8()
    assert universo.column("quality").to_pylist() == [1, None, 3]
    assert universo.schema.field("n_exact_dups").type == pa.int32()


def test_s04_e_idempotente(stage_dirs: StageConfig, make_table) -> None:
    tabela = make_table(
        [
            {"uid": "aaa", "text": "texto repetido", "source_id": "1"},
            {"uid": "bbb", "text": "texto repetido", "source_id": "2"},
        ]
    )
    primeira, _ = _rodar_s04(stage_dirs, tabela)
    segunda, _ = _rodar_s04(stage_dirs, tabela)
    assert primeira.to_pylist() == segunda.to_pylist()


# ---------------------------------------------------------------------------
# s06 fim a fim
# ---------------------------------------------------------------------------

TEXTO_A = "o gato subiu no telhado da casa azul"
TEXTO_B = "o gato subiu no telhado da casa azul ontem"
TEXTO_C = "receita de bolo de cenoura com cobertura de chocolate"
TEXTO_D = "o gato"


def _preparar_s06(cfg: StageConfig, make_table, vetores: np.ndarray) -> None:
    tabela = make_table(
        [
            {"uid": "aaa", "text": TEXTO_A, "source": "aya", "source_id": "1"},
            {"uid": "bbb", "text": TEXTO_B, "source": "aya", "source_id": "2"},
            {"uid": "ccc", "text": TEXTO_C, "source": "aya", "source_id": "3"},
            {"uid": "ddd", "text": TEXTO_D, "source": "aya", "source_id": "4"},
        ]
    )
    escrever(tabela, cfg.caminho(DEDUP1))
    np.save(cfg.caminho("emb/embeddings.f16.npy"), vetores.astype(np.float16))
    cfg.caminho("emb/uids.txt").write_text(
        "aaa\nbbb\nccc\nddd\n", encoding="utf-8", newline="\n"
    )


#: A, B e D no mesmo ponto do espaço; C ortogonal. Assim o filtro de cosseno
#: aprova três pares e sobra para o Jaccard/tamanho decidirem.
VETORES = np.array(
    [[1.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]],
    dtype=np.float32,
)


def test_s06_confirma_por_jaccard_e_tamanho(
    stage_dirs: StageConfig, make_table
) -> None:
    _preparar_s06(stage_dirs, make_table, VETORES)
    assert s06.run(stage_dirs) == 0

    universo = pq.read_table(stage_dirs.caminho("final/universe.parquet"))
    uids = universo.column("uid").to_pylist()
    # A e B colapsam (jaccard 8/9); D tem cosseno 1.0 com A mas Jaccard 0.25 e
    # razão de tamanho 6:1 — sobrevive. C é ortogonal.
    assert uids == ["aaa", "ccc", "ddd"]
    dups = dict(zip(uids, universo.column("n_near_dups").to_pylist(), strict=True))
    assert dups == {"aaa": 1, "ccc": 0, "ddd": 0}

    mapa = pq.read_table(stage_dirs.caminho("final/dedup_near_map.parquet"))
    assert mapa.column("uid").to_pylist() == ["bbb"]
    assert mapa.column("canonical_uid").to_pylist() == ["aaa"]
    assert mapa.column("cosine").to_pylist()[0] == pytest.approx(1.0, abs=1e-3)
    assert mapa.column("jaccard").to_pylist()[0] == pytest.approx(8 / 9, abs=1e-3)


def test_s06_grava_par_de_embeddings_do_universo(
    stage_dirs: StageConfig, make_table
) -> None:
    _preparar_s06(stage_dirs, make_table, VETORES)
    assert s06.run(stage_dirs) == 0
    emb = np.load(stage_dirs.caminho("emb/universe.f16.npy"))
    uids = stage_dirs.caminho("emb/universe_uids.txt").read_text(
        encoding="utf-8"
    ).splitlines()
    assert emb.shape == (3, 4)
    assert uids == ["aaa", "ccc", "ddd"]
    assert emb.dtype == np.float16


def test_s06_recusa_npy_desalinhado(stage_dirs: StageConfig, make_table) -> None:
    _preparar_s06(stage_dirs, make_table, VETORES)
    np.save(stage_dirs.caminho("emb/embeddings.f16.npy"), VETORES[:3].astype(np.float16))
    with pytest.raises(SystemExit, match="invariante quebrado"):
        s06.run(stage_dirs)


def test_s06_recusa_uids_desalinhados(stage_dirs: StageConfig, make_table) -> None:
    _preparar_s06(stage_dirs, make_table, VETORES)
    stage_dirs.caminho("emb/uids.txt").write_text(
        "aaa\nXXX\nccc\nddd\n", encoding="utf-8", newline="\n"
    )
    with pytest.raises(SystemExit, match="diverge do parquet"):
        s06.run(stage_dirs)


def test_s06_sem_candidatos_mantem_tudo(stage_dirs: StageConfig, make_table) -> None:
    ortogonais = np.eye(4, dtype=np.float32)
    _preparar_s06(stage_dirs, make_table, ortogonais)
    assert s06.run(stage_dirs) == 0
    universo = pq.read_table(stage_dirs.caminho("final/universe.parquet"))
    assert universo.num_rows == 4
    assert set(universo.column("n_near_dups").to_pylist()) == {0}


def test_s06_exige_o_s05(stage_dirs: StageConfig, make_table, tmp_path: Path) -> None:
    tabela = make_table([{"uid": "aaa", "text": TEXTO_A}])
    escrever(tabela, stage_dirs.caminho(DEDUP1))
    with pytest.raises(SystemExit, match="pf run s05"):
        s06.run(stage_dirs)
