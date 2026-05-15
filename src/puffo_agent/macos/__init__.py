"""macOS-specific helpers for transparent Claude Code credential
management. Everything in this package is no-op on Linux/Windows; call
sites must still gate on ``platform.system() == "Darwin"`` to avoid
shipping behaviour they didn't mean to ship.
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
    "refresh_via_oneshot",
    "shim_dir",
    "writeback_to_keychain",
]
