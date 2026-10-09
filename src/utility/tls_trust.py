"""Enterprise TLS trust configuration for OS-native certificate stores.

Why: on machines behind corporate TLS-inspecting proxies (Netskope, Zscaler,
Palo Alto, …), the intercepting root CA is installed in the OS keychain by IT
but is absent from the `certifi` bundle that httpx/requests use by default —
so every HTTPS call fails with CERTIFICATE_VERIFY_FAILED.

This module activates Python's OS-native trust store (macOS Keychain, Windows
certificate store, or the system OpenSSL paths on Linux) via the `truststore`
package, so those enterprise roots are honored with zero per-machine
configuration and no SSL_CERT_FILE environment variables.

Call `configure_enterprise_tls_trust()` once, as early as possible at process
start — before any module opens an HTTPS connection. It is idempotent and
never raises: if `truststore` is unavailable or activation fails, TLS falls
back to the default (certifi) behavior and a warning is logged instead.
"""

import logging

logger = logging.getLogger(__name__)

_configured = False


def configure_enterprise_tls_trust() -> bool:
    """Enable the OS-native certificate trust store for all TLS connections.

    Returns True when the OS trust store is active, False when the process
    remains on the default certifi-based verification.
    """
    global _configured
    if _configured:
        return True
    try:
        import truststore
    except ImportError:
        logger.warning(
            "truststore is not installed; TLS will use the default certifi "
            "bundle. Install it (`pip install truststore`) if this machine is "
            "behind a corporate TLS-inspecting proxy (Netskope, Zscaler, …)."
        )
        return False
    try:
        truststore.inject_into_ssl()
    except Exception as exc:  # noqa: BLE001 — never break startup over TLS setup
        logger.warning(
            "OS trust store activation failed, using default TLS verification: %s",
            exc,
        )
        return False
    _configured = True
    logger.debug("OS-native certificate trust store activated")
    return True
