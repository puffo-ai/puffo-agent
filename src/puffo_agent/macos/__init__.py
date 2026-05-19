"""macOS-specific helpers for puffo-agent.

The async ``CredentialRefresher`` lifecycle is platform-agnostic and
lives in ``puffo_agent.portal.credential_refresh``; this package owns
the macOS storage primitives (Keychain read/write, PATH shim,
credential cache, refresh oneshot) it delegates to via
``KeychainBackend``.
"""

from .keychain import (
    CACHE_FILENAME,
    KEYCHAIN_POLL_INTERVAL_SECONDS,
    KEYCHAIN_SERVICE,
    SHIM_FILENAME,
    CredentialCache,
    KeychainReadResult,
    bootstrap_from_keychain,
    install_path_shim,
    is_macos,
    read_keychain_blob,
    refresh_via_oneshot,
    shim_dir,
    writeback_to_keychain,
)

__all__ = [
    "CACHE_FILENAME",
    "KEYCHAIN_POLL_INTERVAL_SECONDS",
    "KEYCHAIN_SERVICE",
    "SHIM_FILENAME",
    "CredentialCache",
    "KeychainReadResult",
    "bootstrap_from_keychain",
    "install_path_shim",
    "is_macos",
    "read_keychain_blob",
    "refresh_via_oneshot",
    "shim_dir",
    "writeback_to_keychain",
]
