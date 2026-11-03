"""TLS trust for LLM endpoints that sit behind a private certificate authority.

Corporate gateways commonly present a certificate signed by an internal root CA
that the operating system already trusts but that is absent from the ``certifi``
bundle httpx uses. Python 3.13 also turns on ``VERIFY_X509_STRICT`` in
``ssl.create_default_context()``, which rejects many such roots for a missing
Key Usage extension (``CA cert does not include key usage extension``) even when
they are trusted. Either way the handshake fails and the request never reaches
the gateway.

:func:`build_ssl_context` returns a context that trusts, together:

* the file named by ``SSL_CERT_FILE`` (or the ``SSL_CERT_DIR`` directory), else
  ``certifi`` - exactly what httpx trusts by default;
* the operating system store, so a CA that IT already deployed just works.

It clears only ``VERIFY_X509_STRICT``. The chain, the expiry dates and the host
name are still verified; verification is never disabled.

:func:`install_sdk_client` and :func:`install_litellm` apply that context to the
two HTTP stacks Strix sends model requests through. Both are idempotent.
"""

from __future__ import annotations

import logging
import os
import ssl
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

# ``ssl.VERIFY_X509_STRICT`` exists on every supported Python; the fallback only
# keeps the module importable on an exotic build.
_VERIFY_X509_STRICT: int = getattr(ssl, "VERIFY_X509_STRICT", 0)

# Marks LiteLLM's context factory once wrapped, so a second install is a no-op.
_WRAPPED_MARKER = "_strix_tls_wrapped"


def _relax_strict_x509(context: ssl.SSLContext) -> None:
    """Drop only ``VERIFY_X509_STRICT``; chain, expiry and host name stay verified."""
    context.verify_flags &= ~_VERIFY_X509_STRICT


def _add_system_trust(context: ssl.SSLContext) -> None:
    """Trust the operating system store as well (Windows ROOT/CA, OpenSSL paths elsewhere)."""
    try:
        context.load_default_certs()
    except (OSError, ssl.SSLError):
        logger.warning("could not load the operating system certificate store", exc_info=True)


def _env_path(name: str, *, is_dir: bool) -> str | None:
    value = os.environ.get(name)
    if not value:
        return None
    exists = os.path.isdir(value) if is_dir else os.path.isfile(value)  # noqa: PTH112, PTH113
    return value if exists else None


def build_ssl_context() -> ssl.SSLContext:
    """A verifying context trusting the env/certifi bundle plus the OS store, non-strict."""
    cafile = _env_path("SSL_CERT_FILE", is_dir=False)
    capath = _env_path("SSL_CERT_DIR", is_dir=True)
    if cafile is None and capath is None:
        import certifi

        cafile = certifi.where()
    context = ssl.create_default_context(cafile=cafile, capath=capath)
    _add_system_trust(context)
    _relax_strict_x509(context)
    return context


def _strix_client_class() -> type[httpx.AsyncClient]:
    from openai import DefaultAsyncHttpxClient

    class StrixAsyncHttpxClient(DefaultAsyncHttpxClient):
        """The SDK's default client, built on :func:`build_ssl_context`."""

    return StrixAsyncHttpxClient


def install_sdk_client() -> None:
    """Make the OpenAI Agents SDK's shared httpx client use :func:`build_ssl_context`.

    The shared client is created lazily with httpx's defaults, so this must run
    before anything touches it. If it already exists with the stock context it is
    replaced, keeping its event hooks so the request log stays attached.
    """
    from agents.models import openai_provider

    current: httpx.AsyncClient | None = getattr(openai_provider, "_http_client", None)
    if current is not None and type(current).__name__ == "StrixAsyncHttpxClient":
        return
    replacement = _strix_client_class()(verify=build_ssl_context())
    if current is not None:
        replacement.event_hooks = current.event_hooks
    openai_provider._http_client = replacement  # no public hook for the shared client


def install_litellm() -> None:
    """Make LiteLLM build its HTTP clients on the same trust as :func:`build_ssl_context`.

    LiteLLM ignores an ``ssl.SSLContext`` assigned to ``litellm.ssl_verify``: its
    clients call ``get_ssl_configuration()`` without arguments, which builds a
    strict, certifi-only context. The one seam that every such client shares is
    its context factory, so wrap that. If a LiteLLM release moves it, log and
    leave LiteLLM on its own defaults rather than fail the scan.
    """
    try:
        from litellm.llms.custom_httpx import http_handler
    except ImportError:
        logger.warning(
            "LiteLLM HTTP handler not importable; leaving its TLS defaults", exc_info=True
        )
        return

    original: Any = getattr(http_handler, "_create_ssl_context", None)
    if not callable(original):
        logger.warning("LiteLLM no longer exposes _create_ssl_context; leaving its TLS defaults")
        return
    if getattr(original, _WRAPPED_MARKER, False):
        return

    def _create_ssl_context(*args: Any, **kwargs: Any) -> ssl.SSLContext:
        context: ssl.SSLContext = original(*args, **kwargs)
        _add_system_trust(context)
        _relax_strict_x509(context)
        return context

    setattr(_create_ssl_context, _WRAPPED_MARKER, True)
    http_handler._create_ssl_context = _create_ssl_context
    # Contexts built before the wrap (strict, certifi-only) must not be reused.
    cache = getattr(http_handler, "_ssl_context_cache", None)
    if isinstance(cache, dict):
        cache.clear()
