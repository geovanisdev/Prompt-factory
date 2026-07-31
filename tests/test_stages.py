"""Estágios s01/s02/s03 e a expansão de ``pf run`` — tudo offline, em tmp_path.

O s02 completo depende dos dois detectores de idioma (fastText + lingua), que
são caros de carregar; aqui só se testa o caminho que não precisa deles. A
qualidade da decisão de idioma se mede no funil do run real, não em unidade.
"""

from __future__ import annotations

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from prompt_factory.cli import expandir_estagios
from prompt_factory.schema import COLUMN_NAMES
from prompt_factory.stages import LANG, NORMALIZED, SCRUBBED, StageConfig
from prompt_factory.stages import s01_normalize as s01
from prompt_factory.stages import s03_pii as s03

from .conftest import escrever

# ---------------------------------------------------------------------------
# s01
# ---------------------------------------------------------------------------


def _rodar_s01(cfg: StageConfig, tabela: pa.Table, fonte: str = "aya") -> pa.Table:
    escrever(tabela, cfg.raw / f"{fonte}.parquet")
    assert s01.run(cfg) == 0
    return pq.read_table(cfg.caminho(NORMALIZED))


def test_s01_produz_as_27_colunas(stage_dirs: StageConfig, make_raw_table) -> None:
    saida = _rodar_s01(stage_dirs, make_raw_table([{"text_raw": " Olá!\r\n\r\n\r\nMundo "}]))
    assert tuple(saida.schema.names) == COLUMN_NAMES
    assert saida.column("text").to_pylist() == ["Olá!\n\nMundo"]
    assert saida.column("text_raw").to_pylist() == [" Olá!\r\n\r\n\r\nMundo "]
    assert saida.column("n_chars").to_pylist() == [11]
    assert saida.column("n_words").to_pylist() == [2]


def test_s01_descarta_texto_que_normaliza_para_vazio(
    stage_dirs: StageConfig, make_raw_table
) -> None:
    tabela = make_raw_table(
        [
            {"source_id": "1", "text_raw": "​​"},  # só invisíveis
            {"source_id": "2", "text_raw": "prompt de verdade"},
        ]
    )
    saida = _rodar_s01(stage_dirs, tabela)
    assert saida.column("text").to_pylist() == ["prompt de verdade"]


def test_s01_dedup_de_uid_mantem_a_primeira(
    stage_dirs: StageConfig, make_raw_table
) -> None:
    # Sem source_id o uid vem do texto: a mesma frase duas vezes colide.
    tabela = make_raw_table(
        [
            {"source_id": "", "text_raw": "mesmo texto", "native_category": "primeira"},
            {"source_id": "", "text_raw": "mesmo texto", "native_category": "segunda"},
        ]
    )
    saida = _rodar_s01(stage_dirs, tabela)
    assert saida.num_rows == 1
    assert saida.column("native_category").to_pylist() == ["primeira"]


def test_s01_lang_so_aceita_codigo_exato(stage_dirs: StageConfig, make_raw_table) -> None:
    tabela = make_raw_table(
        [
            {"source_id": "1", "lang_source": "pt", "text_raw": "um"},
            {"source_id": "2", "lang_source": "en", "text_raw": "dois"},
            {"source_id": "3", "lang_source": "Portuguese", "text_raw": "tres"},
            {"source_id": "4", "lang_source": "pt-BR", "text_raw": "quatro"},
        ]
    )
    saida = _rodar_s01(stage_dirs, tabela)
    assert saida.column("lang").to_pylist() == ["pt", "en", "und", "und"]
    assert saida.column("lang_variant").to_pylist() == [None] * 4


def test_s01_dobra_nsfw_hint_no_meta(stage_dirs: StageConfig, make_raw_table) -> None:
    tabela = make_raw_table(
        [
            {"source_id": "1", "text_raw": "um", "nsfw_hint": True, "meta_json": '{"a":1}'},
            {"source_id": "2", "text_raw": "dois", "nsfw_hint": False},
            {"source_id": "3", "text_raw": "tres", "nsfw_hint": True, "meta_json": "isto não é json"},
        ]
    )
    saida = _rodar_s01(stage_dirs, tabela)
    metas = [json.loads(m) for m in saida.column("meta_json").to_pylist()]
    assert metas[0] == {"a": 1, "nsfw_hint": True}
    assert metas[1] == {}
    assert metas[2] == {"_raw_meta": "isto não é json", "nsfw_hint": True}


def test_s01_vazios_do_raw_viram_nulo(stage_dirs: StageConfig, make_raw_table) -> None:
    tabela = make_raw_table([{"source_id": "", "country": "", "created_ts": ""}])
    saida = _rodar_s01(stage_dirs, tabela)
    assert saida.column("source_id").to_pylist() == [None]
    assert saida.column("country").to_pylist() == [None]
    assert saida.column("created_ts").to_pylist() == [None]


def test_s01_recusa_licenca_divergente(stage_dirs: StageConfig, make_raw_table) -> None:
    tabela = make_raw_table([{"license": "mit"}])  # aya é apache-2.0
    escrever(tabela, stage_dirs.raw / "aya.parquet")
    with pytest.raises(SystemExit, match="licença do raw"):
        s01.run(stage_dirs)


def test_s01_recusa_fonte_desconhecida(stage_dirs: StageConfig, make_raw_table) -> None:
    escrever(make_raw_table([{}]), stage_dirs.raw / "fonte_fantasma.parquet")
    with pytest.raises(SystemExit, match="não existe em"):
        s01.run(stage_dirs)


def test_s01_ignora_o_pool_do_wildchat(stage_dirs: StageConfig, make_raw_table) -> None:
    # wildchat_en_pool.parquet é insumo do downsample, não uma fonte: entrar com
    # ele duplicaria 158 mil linhas no universo.
    escrever(
        make_raw_table([{"source": "wildchat_en", "text_raw": "do pool"}]),
        stage_dirs.raw / "wildchat_en_pool.parquet",
    )
    escrever(
        make_raw_table([{"text_raw": "da fonte"}]),
        stage_dirs.raw / "aya.parquet",
    )
    assert s01.run(stage_dirs) == 0
    saida = pq.read_table(stage_dirs.caminho(NORMALIZED))
    assert saida.column("text").to_pylist() == ["da fonte"]


def test_s01_max_rows_corta_por_fonte(stage_dirs: StageConfig, make_raw_table) -> None:
    cfg = StageConfig(data_dir=stage_dirs.data_dir, max_rows=2)
    tabela = make_raw_table([{"source_id": str(i), "text_raw": f"p{i}"} for i in range(10)])
    escrever(tabela, cfg.raw / "aya.parquet")
    assert s01.run(cfg) == 0
    assert pq.read_table(cfg.caminho(NORMALIZED)).num_rows == 2


def test_s01_sem_raw_sai_2(stage_dirs: StageConfig) -> None:
    assert s01.run(stage_dirs) == 2


# ---------------------------------------------------------------------------
# s03
# ---------------------------------------------------------------------------


def test_s03_recalcula_hash_e_marca_pii(stage_dirs: StageConfig, make_table) -> None:
    tabela = make_table(
        [
            {"uid": "aaa", "text": "fale com joao@x.com sobre o projeto"},
            {"uid": "bbb", "text": "fale com maria@y.org sobre o projeto"},
            {"uid": "ccc", "text": "prompt sem dado pessoal"},
        ]
    )
    escrever(tabela, stage_dirs.caminho(LANG))
    assert s03.run(stage_dirs) == 0
    saida = pq.read_table(stage_dirs.caminho(SCRUBBED))

    assert saida.num_rows == 3  # o s03 nunca descarta linha
    textos = saida.column("text").to_pylist()
    assert textos[0] == textos[1] == "fale com [EMAIL] sobre o projeto"
    assert saida.column("pii_found").to_pylist() == [True, True, False]
    hashes = saida.column("hash_norm").to_pylist()
    # O ponto do estágio: dois spams que só diferiam no e-mail passam a colidir
    # no dedup exato do s04.
    assert hashes[0] == hashes[1] != hashes[2]
    assert saida.column("n_chars").to_pylist()[0] == len(textos[0])


def test_s03_preserva_text_raw(stage_dirs: StageConfig, make_table) -> None:
    tabela = make_table([{"text": "email: a@b.com", "text_raw": "email: a@b.com"}])
    escrever(tabela, stage_dirs.caminho(LANG))
    assert s03.run(stage_dirs) == 0
    saida = pq.read_table(stage_dirs.caminho(SCRUBBED))
    assert saida.column("text_raw").to_pylist() == ["email: a@b.com"]
    assert saida.column("text").to_pylist() == ["email: [EMAIL]"]


def test_s03_exige_o_s02(stage_dirs: StageConfig) -> None:
    with pytest.raises(SystemExit, match="pf run s02"):
        s03.run(stage_dirs)


# ---------------------------------------------------------------------------
# expansão de estágios do `pf run`
# ---------------------------------------------------------------------------

VALIDOS = ["s01", "s02", "s03", "s04", "s05", "s06"]


def test_expandir_intervalos_e_all() -> None:
    assert expandir_estagios(["all"], VALIDOS) == VALIDOS
    assert expandir_estagios(["s01-s03"], VALIDOS) == ["s01", "s02", "s03"]
    assert expandir_estagios(["s02"], VALIDOS) == ["s02"]
    assert expandir_estagios(["s01", "s02", "s03"], VALIDOS) == ["s01", "s02", "s03"]
    assert expandir_estagios(["s01", "s01-s02"], VALIDOS) == ["s01", "s02"]


def test_expandir_recusa_desconhecido_e_desordem() -> None:
    with pytest.raises(ValueError, match="desconhecido"):
        expandir_estagios(["s99"], VALIDOS)
    with pytest.raises(ValueError, match="fora de ordem"):
        expandir_estagios(["s04", "s02"], VALIDOS)
    with pytest.raises(ValueError, match="invertido"):
        expandir_estagios(["s05-s02"], VALIDOS)
