"""macOS-specific helpers for puffo-agent.

The async ``CredentialRefresher`` lifecycle is platform-agnostic and
lives in ``puffo_agent.portal.credential_refresh``; this package owns
the macOS storage primitives (Keychain read/write, credential cache)
it delegates to via ``KeychainBackend``.
"""

from .keychain import (
    CACHE_FILENAME,
    KEYCHAIN_POLL_INTERVAL_SECONDS,
    KEYCHAIN_SERVICE,
    CredentialCache,
    KeychainReadResult,
    bootstrap_from_keychain,
    is_macos,
    read_keychain_blob,
    writeback_to_keychain,
)

__all__ = [
    "CACHE_FILENAME",
    "KEYCHAIN_POLL_INTERVAL_SECONDS",
    "KEYCHAIN_SERVICE",
    "CredentialCache",
    "KeychainReadResult",
    "bootstrap_from_keychain",
    "is_macos",
    "read_keychain_blob",
    "writeback_to_keychain",
]
