"""Shared SSL context for aiohttp sessions.

Provides a certifi-backed SSL context so aiohttp works on Windows systems
that have a missing or incomplete OS cert store, including PyInstaller
frozen builds on clean Windows installs.

Usage:
    import aiohttp
    from core.ssl_context import make_connector

    async with aiohttp.ClientSession(connector=make_connector()) as session:
        ...

Why this exists:
    aiohttp on Windows relies on the OS trust store by default. Fresh
    Windows installs and some enterprise/AV environments do not have the
    needed intermediate CA certs, producing:
        [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate
    Using certifi's bundled CA file avoids this entirely.

Notes:
    - SSLContext is cached and safe to share across sessions.
    - TCPConnector is NOT shareable (it closes with its session), so
      make_connector() returns a fresh one each call.
    - certifi is already in the PyInstaller spec hiddenimports.
"""

import ssl
import certifi
import aiohttp

_ssl_context = None


def get_ssl_context() -> ssl.SSLContext:
    """Return a cached SSLContext using certifi's CA bundle."""
    global _ssl_context
    if _ssl_context is None:
        _ssl_context = ssl.create_default_context(cafile=certifi.where())
    return _ssl_context


def make_connector() -> aiohttp.TCPConnector:
    """Create a fresh TCPConnector configured with certifi CA bundle.

    Returns a new connector each call since connectors close with their
    parent ClientSession and cannot be reused.
    """
    return aiohttp.TCPConnector(ssl=get_ssl_context())
