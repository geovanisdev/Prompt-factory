"""Ponte entre o armazenamento de certificados do Windows e o `requests`.

Esta máquina tem o TLS interceptado por um middlebox (antivírus/proxy) que
reassina o tráfego. A raiz dele existe **só** no armazenamento de certificados
do Windows. O `uv` já foi ensinado a usar esse armazenamento (`system-certs` no
pyproject), mas o Python tem o mesmo problema por outro caminho:

* `ssl.create_default_context()` (urllib, httpx) já lê o store do Windows — funciona;
* `requests`/`huggingface_hub` ignoram o store e apontam para o `certifi`, que
  não conhece a raiz do middlebox — e todo download do Hub morre em
  ``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate``.

A solução é gerar um bundle PEM = `certifi` + raízes de servidor do Windows e
apontar `REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE` para ele. **A verificação continua
ligada** — nunca troque isto por `verify=False` ou
`HF_HUB_DISABLE_SSL_VERIFICATION`, que desligam a checagem de verdade.

O bundle é um artefato de máquina: fica em `data/` (gitignorado), é regenerado
sozinho a cada 7 dias e pode ser apagado à vontade. Em Linux/macOS o módulo é
inerte (`ssl.enum_certificates` só existe no Windows).
"""

from __future__ import annotations

import os
import ssl
import time
from pathlib import Path

from .paths import DATA

#: OID de "TLS Web Server Authentication" — só essas raízes entram no bundle.
_SERVER_AUTH_OID = "1.3.6.1.5.5.7.3.1"

#: Regenera o bundle depois disso (raiz nova do middlebox aparece sozinha).
MAX_IDADE_S = 7 * 24 * 3600

#: Caminho padrão do bundle gerado.
CA_BUNDLE: Path = DATA / "system-ca.pem"


def windows_pems() -> list[str]:
    """Raízes de autenticação de servidor do store do Windows, em PEM."""
    enum_certificates = getattr(ssl, "enum_certificates", None)
    if enum_certificates is None:  # não é Windows: certifi + store do SO já bastam
        return []
    pems: list[str] = []
    for store in ("ROOT", "CA"):
        try:
            certificados = enum_certificates(store)
        except OSError:  # pragma: no cover - store inacessível
            continue
        for cert, encoding, trust in certificados:
            if encoding != "x509_asn":
                continue
            if trust is not True and _SERVER_AUTH_OID not in (trust or ()):
                continue
            pems.append(ssl.DER_cert_to_PEM_cert(cert))
    return pems


def build_bundle(destino: Path = CA_BUNDLE) -> Path:
    """Escreve `certifi` + store do Windows num PEM único (atômico)."""
    partes: list[str] = []
    try:
        import certifi

        partes.append(Path(certifi.where()).read_text(encoding="utf-8"))
    except (ImportError, OSError):  # pragma: no cover - certifi sempre vem com requests
        pass
    partes.extend(windows_pems())

    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.parent / f"{destino.name}.tmp"
    tmp.write_text("\n".join(partes), encoding="utf-8")
    os.replace(tmp, destino)
    return destino


def ensure_ca_bundle(destino: Path = CA_BUNDLE) -> str | None:
    """Garante o bundle e exporta as variáveis de ambiente. Devolve o caminho.

    Não faz nada (e devolve `None`) se o operador já apontou um bundle próprio,
    se o sistema não é Windows, ou se o arquivo não pôde ser escrito — nenhum
    desses casos pode derrubar um `pf --help`.
    """
    if os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE"):
        return None
    if not hasattr(ssl, "enum_certificates"):
        return None
    try:
        idade = time.time() - destino.stat().st_mtime if destino.is_file() else None
        if idade is None or idade > MAX_IDADE_S:
            build_bundle(destino)
    except OSError:  # pragma: no cover - disco cheio / permissão
        return None
    os.environ["REQUESTS_CA_BUNDLE"] = str(destino)
    os.environ["SSL_CERT_FILE"] = str(destino)
    return str(destino)


__all__ = ["CA_BUNDLE", "MAX_IDADE_S", "build_bundle", "ensure_ca_bundle", "windows_pems"]
