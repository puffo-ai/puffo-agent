"""Daemon-level Claude Code OAuth credential management. Some pieces
here (Keychain bridge, PATH shim) are macOS-only; ``CredentialManager``
runs on every platform — refresh strategy dispatched internally.

Path-name historical: this package was bootstrapped as macOS-only.
The non-macOS daemon-level refresh got bundled in when we generalised
to fix the "refresh ran but expiry didn't advance" reports on
Linux/Windows.
"""

from .credential_manager import (
    CACHE_FILENAME,
    KEYCHAIN_SERVICE,
    SHIM_FILENAME,
    CredentialCache,
    CredentialManager,
    bootstrap_from_keychain,
    install_path_shim,
    is_macos,
    read_keychain_blob,
    refresh_via_host_oneshot,
    refresh_via_oneshot,
    shim_dir,
    writeback_to_keychain,
)

__all__ = [
    "CACHE_FILENAME",
    "KEYCHAIN_SERVICE",
    "SHIM_FILENAME",
    "CredentialCache",
    "CredentialManager",
    "bootstrap_from_keychain",
    "install_path_shim",
    "is_macos",
    "read_keychain_blob",
    "refresh_via_host_oneshot",
    "refresh_via_oneshot",
    "shim_dir",
    "writeback_to_keychain",
]
