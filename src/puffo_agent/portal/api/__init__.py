"""Local HTTP API exposed by the puffo-agent daemon.

Loopback-only (default ``127.0.0.1:63387``). Auth uses the same
ed25519 request-signing scheme as puffo-server, but the device root
key signs directly (no subkey rotation) and a daemon is bound to one
``(slug, device_id)`` at a time — see ``pairing.py``.
"""

from .server import start_api_server, stop_api_server

__all__ = ["start_api_server", "stop_api_server"]
