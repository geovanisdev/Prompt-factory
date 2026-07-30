"""Smoke test do M0: o pacote importa, a CLI responde e os configs parseiam."""

from __future__ import annotations

import pytest

import prompt_factory
from prompt_factory import cli, config, paths


def test_package_importa_com_versao() -> None:
    assert prompt_factory.__version__ == "0.1.0"


def test_help_sai_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    assert "ingest" in capsys.readouterr().out


@pytest.mark.parametrize("cmd", [c.name for c in cli.COMMANDS])
def test_subcomando_tem_help_proprio(cmd: str) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([cmd, "--help"])
    assert exc.value.code == 0


@pytest.mark.parametrize("cmd", [c.name for c in cli.stub_commands()])
def test_subcomando_ainda_nao_implementado(
    cmd: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # Parametrizado só sobre os stubs: ao implementar um comando basta marcar
    # implemented=True em cli.COMMANDS e ele sai desta lista sozinho.
    assert cli.main([cmd]) == 2
    assert "ainda não implementado" in capsys.readouterr().out


def test_sem_comando_mostra_help_e_falha() -> None:
    assert cli.main([]) == 2


def test_settings_tem_chaves_do_contrato() -> None:
    assert config.seed() == 42
    assert config.get("embed", "dim") == 384
    assert config.get("app", "port") == 8765
    assert 0.0 < config.get("classifier", "conf_review") < config.get(
        "classifier", "conf_accept"
    ) < 1.0


def test_sources_tem_licenca_e_atribuicao_em_toda_fonte() -> None:
    srcs = config.sources()
    assert "wildchat_pt" in srcs and "wildchat_en" in srcs
    obrigatorias = (
        "enabled",
        "hf_id",
        "license",
        "commercial_ok",
        "redistributable",
        "attribution",
        "expected_min",
        "expected_max",
    )
    for nome, spec in srcs.items():
        faltando = [k for k in obrigatorias if k not in spec]
        assert not faltando, f"fonte {nome} sem {faltando}"
        assert spec["attribution"].strip(), f"fonte {nome} sem atribuição"
    # lmsys fica desligada: gated, sem redistribuição.
    assert srcs["lmsys"]["enabled"] is False
    assert srcs["lmsys"]["redistributable"] is False
    assert "lmsys" not in config.enabled_sources()


def test_paths_apontam_para_dentro_do_repo() -> None:
    assert (paths.ROOT / "pyproject.toml").is_file()
    for d in paths.DATA_DIRS:
        assert paths.DATA in d.parents
