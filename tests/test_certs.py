"""Ponte com o armazenamento de certificados do Windows (`certs.py`).

Nada aqui toca a rede: o que se testa é a montagem do bundle e a regra de não
atropelar um bundle que o operador já tenha configurado.
"""

from __future__ import annotations

import ssl
import sys
from pathlib import Path

import pytest

from prompt_factory import certs


@pytest.mark.skipif(sys.platform != "win32", reason="ssl.enum_certificates só existe no Windows")
def test_windows_pems_traz_raizes_de_servidor() -> None:
    pems = certs.windows_pems()
    assert pems, "o store do Windows deveria ter pelo menos uma raiz de servidor"
    assert all(p.startswith("-----BEGIN CERTIFICATE-----") for p in pems)


def test_build_bundle_escreve_pem_e_nao_deixa_tmp(tmp_path: Path) -> None:
    destino = certs.build_bundle(tmp_path / "ca.pem")
    conteudo = destino.read_text(encoding="utf-8")
    assert "-----BEGIN CERTIFICATE-----" in conteudo
    assert list(tmp_path.glob("*.tmp")) == []


def test_ensure_ca_bundle_respeita_bundle_do_operador(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "C:/meu/bundle.pem")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    assert certs.ensure_ca_bundle(tmp_path / "ca.pem") is None
    assert not (tmp_path / "ca.pem").exists()


def test_ensure_ca_bundle_exporta_as_duas_variaveis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    destino = tmp_path / "ca.pem"
    resultado = certs.ensure_ca_bundle(destino)
    if not hasattr(ssl, "enum_certificates"):  # pragma: no cover - fora do Windows
        assert resultado is None
        return
    assert resultado == str(destino)
    assert destino.is_file()
    # requests lê REQUESTS_CA_BUNDLE; urllib/httpx leem SSL_CERT_FILE.
    import os

    assert os.environ["REQUESTS_CA_BUNDLE"] == str(destino)
    assert os.environ["SSL_CERT_FILE"] == str(destino)
