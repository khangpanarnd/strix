"""TLS trust for LLM endpoints behind a private certificate authority.

The regression these tests pin: a gateway whose certificate is signed by a
private root CA *without a Key Usage extension* failed the handshake from Python
3.13 (``CA cert does not include key usage extension``) even when that CA was
trusted, and the viewer then reported it as an unreachable base URL.

``test_*_handshake`` run a real TLS handshake against a loopback server using a
generated CA, so they fail without the fix on any Python version (the strict
flag is set explicitly rather than relying on the interpreter's default).
"""

from __future__ import annotations

import datetime as dt
import socket
import ssl
import threading
from typing import TYPE_CHECKING, Any

import pytest
from agents.models import openai_provider
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from litellm.llms.custom_httpx import http_handler

from strix.llm import tls


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_HOST = "localhost"


class _Pki:
    def __init__(self, ca_pem: Path, cert_pem: Path, key_pem: Path) -> None:
        self.ca_pem = ca_pem
        self.cert_pem = cert_pem
        self.key_pem = key_pem


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _write_pki(directory: Path) -> _Pki:
    """A root CA with NO KeyUsage extension and a ``localhost`` leaf it signed."""
    now = dt.datetime.now(dt.UTC)
    window = {
        "not_valid_before": now - dt.timedelta(days=1),
        "not_valid_after": now + dt.timedelta(days=1),
    }

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("Test Private Root"))
        .issuer_name(_name("Test Private Root"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(window["not_valid_before"])
        .not_valid_after(window["not_valid_after"])
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(ca_ski, critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(_name(_HOST))
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(window["not_valid_before"])
        .not_valid_after(window["not_valid_after"])
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(_HOST)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski), critical=False
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    ca_pem = directory / "ca.pem"
    cert_pem = directory / "leaf.pem"
    key_pem = directory / "leaf.key"
    ca_pem.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_pem.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return _Pki(ca_pem, cert_pem, key_pem)


@pytest.fixture
def tls_server(tmp_path: Path) -> Iterator[tuple[int, _Pki]]:
    """A loopback TLS server presenting the generated leaf; yields (port, pki)."""
    pki = _write_pki(tmp_path)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(str(pki.cert_pem), str(pki.key_pem))

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.2)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except (TimeoutError, OSError):
                continue
            try:
                with server_ctx.wrap_socket(conn, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                continue  # a client that rejected our certificate aborts the handshake

    thread = threading.Thread(target=serve, name="tls-test-server", daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1], pki
    finally:
        stop.set()
        thread.join(timeout=2)
        listener.close()


def _handshake(context: ssl.SSLContext, port: int, *, server_hostname: str = _HOST) -> None:
    with (
        socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
        context.wrap_socket(raw, server_hostname=server_hostname),
    ):
        pass


def _strict_context(cafile: Path) -> ssl.SSLContext:
    """Python 3.13's default behaviour, forced on so the test is version-independent."""
    context = ssl.create_default_context(cafile=str(cafile))
    context.verify_flags |= ssl.VERIFY_X509_STRICT
    return context


def test_strict_context_rejects_a_trusted_ca_without_key_usage(
    tls_server: tuple[int, _Pki],
) -> None:
    """The failure being fixed: the CA is trusted, yet strict verification refuses it."""
    port, pki = tls_server
    with pytest.raises(ssl.SSLCertVerificationError, match="key usage"):
        _handshake(_strict_context(pki.ca_pem), port)


def test_build_ssl_context_accepts_a_private_ca_from_ssl_cert_file(
    tls_server: tuple[int, _Pki], monkeypatch: pytest.MonkeyPatch
) -> None:
    port, pki = tls_server
    monkeypatch.setenv("SSL_CERT_FILE", str(pki.ca_pem))

    _handshake(tls.build_ssl_context(), port)


def test_build_ssl_context_still_verifies_the_chain(
    tls_server: tuple[int, _Pki], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without the CA in the trust set the handshake must still fail: not 'verify off'."""
    port, _ = tls_server
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.setenv("SSL_CERT_FILE", str(_write_pki(unrelated).ca_pem))  # a different CA

    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(tls.build_ssl_context(), port)


def test_build_ssl_context_still_verifies_the_host_name(
    tls_server: tuple[int, _Pki], monkeypatch: pytest.MonkeyPatch
) -> None:
    port, pki = tls_server
    monkeypatch.setenv("SSL_CERT_FILE", str(pki.ca_pem))

    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(tls.build_ssl_context(), port, server_hostname="wrong.example.com")


def test_build_ssl_context_keeps_verification_on_and_clears_only_the_strict_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)

    context = tls.build_ssl_context()

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert not context.verify_flags & ssl.VERIFY_X509_STRICT


def test_build_ssl_context_ignores_a_missing_ssl_cert_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stale path must fall back to certifi rather than crash every LLM call."""
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "does-not-exist.pem"))
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)

    assert tls.build_ssl_context().verify_mode == ssl.CERT_REQUIRED


# -- the OpenAI Agents SDK client --------------------------------------------


def test_install_sdk_client_replaces_the_shared_client_and_keeps_its_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def hook(_response: Any) -> None:
        return None

    stock = openai_provider.DefaultAsyncHttpxClient(event_hooks={"response": [hook]})
    monkeypatch.setattr(openai_provider, "_http_client", stock)

    tls.install_sdk_client()

    replaced = openai_provider.shared_http_client()
    assert replaced is not stock
    assert type(replaced).__name__ == "StrixAsyncHttpxClient"
    assert hook in replaced.event_hooks["response"]


def test_install_sdk_client_creates_the_client_when_none_exists_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openai_provider, "_http_client", None)

    tls.install_sdk_client()
    first = openai_provider.shared_http_client()
    tls.install_sdk_client()

    assert type(first).__name__ == "StrixAsyncHttpxClient"
    assert openai_provider.shared_http_client() is first


# -- LiteLLM ------------------------------------------------------------------


def _strict_litellm_factory(
    cafile: str | None, ssl_security_level: str | None, ssl_ecdh_curve: str | None
) -> ssl.SSLContext:
    """Stands in for LiteLLM's own factory: certifi-only and strict, as 3.13 builds it."""
    del ssl_security_level, ssl_ecdh_curve
    context = ssl.create_default_context(cafile=cafile)
    context.verify_flags |= ssl.VERIFY_X509_STRICT
    return context


@pytest.fixture
def litellm_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unwrapped factory and an empty cache, independent of test order."""
    monkeypatch.setattr(http_handler, "_create_ssl_context", _strict_litellm_factory)
    monkeypatch.setattr(http_handler, "_ssl_context_cache", {})
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)


@pytest.mark.usefixtures("litellm_factory")
def test_install_litellm_relaxes_strict_in_the_contexts_litellm_builds() -> None:
    tls.install_litellm()
    context = http_handler.get_ssl_configuration()

    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert not context.verify_flags & ssl.VERIFY_X509_STRICT


@pytest.mark.usefixtures("litellm_factory")
def test_install_litellm_wraps_once_and_drops_contexts_built_before_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale: dict[Any, Any] = {("stale", None, None): object()}
    monkeypatch.setattr(http_handler, "_ssl_context_cache", stale)

    tls.install_litellm()
    wrapped = http_handler._create_ssl_context
    tls.install_litellm()

    assert wrapped is not _strict_litellm_factory
    assert http_handler._create_ssl_context is wrapped
    assert not stale


def test_install_litellm_survives_a_litellm_without_the_context_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(http_handler, "_create_ssl_context")

    tls.install_litellm()  # must log and return, never fail a scan

    assert not hasattr(http_handler, "_create_ssl_context")
