"""`write_raw` + `pf report raw` — testes OFFLINE, com `data/raw` desviado para tmp_path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from prompt_factory import paths, report
from prompt_factory.ingest import REGISTRY, resolve_names
from prompt_factory.ingest.base import RAW_COLUMNS, dump_meta, make_row, to_iso, write_raw


@pytest.fixture
def raw_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Desvia `data/raw` para o tmp_path — nenhum teste escreve no repo."""
    destino = tmp_path / "raw"
    destino.mkdir()
    monkeypatch.setattr(paths, "RAW", destino)
    return destino


def _linhas() -> list[dict[str, Any]]:
    return [
        make_row(
            source="dolly",
            source_id="train-00000",
            source_split="train",
            text_raw="Explique a fotossíntese",
            lang_source="en",
            native_category="open_qa",
            meta={"has_context": 0},
        ),
        make_row(
            source="dolly",
            source_id="train-00001",
            source_split="train",
            text_raw="   \n  ",  # vazio depois do strip: descartado
            lang_source="en",
        ),
        make_row(
            source="dolly",
            source_id="train-00002",
            source_split="train",
            text_raw="Resuma o texto\n\nera uma vez",
            lang_source="en",
            created_ts="2023-04-12T00:00:00+00:00",
        ),
    ]


def test_write_raw_grava_12_colunas_na_ordem(raw_dir: Path) -> None:
    import pyarrow.parquet as pq

    destino = write_raw("dolly", iter(_linhas()))
    assert destino == raw_dir / "dolly.parquet"
    tabela = pq.read_table(destino)
    assert tuple(tabela.schema.names) == RAW_COLUMNS
    assert len(RAW_COLUMNS) == 12
    # A linha só-espaço não entra; as outras duas sim.
    assert tabela.num_rows == 2
    assert tabela.column("text_raw").to_pylist()[0] == "Explique a fotossíntese"


def test_write_raw_sobrescreve_a_licenca_com_o_sources_toml(raw_dir: Path) -> None:
    import pyarrow.parquet as pq

    linhas = _linhas()
    linhas[0]["license"] = "mentira"  # o ingester não decide licença
    tabela = pq.read_table(write_raw("dolly", iter(linhas)))
    assert set(tabela.column("license").to_pylist()) == {"cc-by-sa-3.0"}


def test_write_raw_escreve_metadata_de_procedencia(raw_dir: Path) -> None:
    import pyarrow.parquet as pq

    meta = pq.read_table(write_raw("dolly", iter(_linhas()))).schema.metadata
    assert meta[b"pf_source"] == b"dolly"
    assert meta[b"pf_hf_id"] == b"databricks/databricks-dolly-15k"
    assert meta[b"pf_license"] == b"cc-by-sa-3.0"
    assert meta[b"pf_n_rows"] == b"2"
    assert meta[b"pf_attribution"].startswith(b"Conover")
    assert meta[b"pf_ingested_at"].startswith(b"20")


def test_write_raw_respeita_max_rows(raw_dir: Path) -> None:
    import pyarrow.parquet as pq

    tabela = pq.read_table(write_raw("dolly", iter(_linhas()), max_rows=1))
    assert tabela.num_rows == 1


def test_write_raw_nao_deixa_tmp_para_tras(raw_dir: Path) -> None:
    write_raw("dolly", iter(_linhas()))
    assert list(raw_dir.glob("*.tmp")) == []


def test_write_raw_recusa_linha_fora_do_schema(raw_dir: Path) -> None:
    ruim = _linhas()[0]
    del ruim["meta_json"]
    with pytest.raises(ValueError, match="fora do RAW_SCHEMA"):
        write_raw("dolly", iter([ruim]))


def test_write_raw_reescreve_o_arquivo(raw_dir: Path) -> None:
    import pyarrow.parquet as pq

    write_raw("dolly", iter(_linhas()))
    write_raw("dolly", iter(_linhas()[:1]))  # raw é regenerável: a 2a rodada manda
    assert pq.read_table(raw_dir / "dolly.parquet").num_rows == 1


def test_make_row_tem_exatamente_as_chaves_do_schema() -> None:
    linha = make_row(source="x", source_id=None, source_split="train", text_raw=None, lang_source="pt")
    assert tuple(linha) == RAW_COLUMNS
    assert linha["source_id"] == "" and linha["text_raw"] == ""
    assert linha["meta_json"] == "{}"
    assert linha["nsfw_hint"] is False


def test_dump_meta_compacto_e_sem_escape_de_acento() -> None:
    assert dump_meta({"a": 1, "b": "ç"}) == '{"a":1,"b":"ç"}'
    assert dump_meta(None) == "{}"


def test_to_iso_aceita_datetime_str_e_none() -> None:
    from datetime import UTC, datetime

    assert to_iso(datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)) == "2024-01-02T03:04:05+00:00"
    assert to_iso("2024-01-02") == "2024-01-02"
    assert to_iso(None) == ""


# ---------------------------------------------------------------------------
# resolução de nomes de fonte
# ---------------------------------------------------------------------------


def test_resolve_names_vazio_expande_para_as_default_on() -> None:
    padrao = resolve_names([])
    # lmsys está no REGISTRY mas é enabled=false: nunca entra no padrão.
    assert set(padrao) == set(REGISTRY) - {"lmsys"}
    assert padrao[-2:] == ["wildchat_pt", "wildchat_en"]  # os caros por último
    assert resolve_names(None) == padrao
    assert resolve_names(["all"]) == padrao


def test_resolve_names_preserva_ordem_e_remove_repetidas() -> None:
    assert resolve_names(["dolly", "aya", "dolly"]) == ["dolly", "aya"]


def test_resolve_names_aceita_hifen_no_lugar_do_underscore() -> None:
    # A linha de comando escreve `wildchat-pt`; o TOML e o parquet usam `_`.
    assert resolve_names(["wildchat-pt", "wildchat-en"]) == ["wildchat_pt", "wildchat_en"]


def test_resolve_names_recusa_fonte_desconhecida() -> None:
    with pytest.raises(ValueError, match="fonte desconhecida"):
        resolve_names(["wildchat"])  # sem o sufixo de modo não existe fonte


# ---------------------------------------------------------------------------
# pf report raw
# ---------------------------------------------------------------------------


def test_report_raw_marca_warn_fora_da_faixa(
    raw_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_raw("dolly", iter(_linhas()))
    assert report.report_raw(sources=["dolly"], head=2) == 0  # informativo: sempre 0
    saida = capsys.readouterr().out
    assert "WARN [15011-15011]" in saida
    assert "Explique a fotossíntese" in saida  # amostra da linha 0


def test_report_raw_acusa_parquet_faltando(
    raw_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert report.report_raw(sources=["prism"]) == 0
    assert "MISSING" in capsys.readouterr().out
