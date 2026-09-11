"""Run the Puffo CLI without the generated console-script launcher.

``python -m puffo_agent`` is also a useful recovery entry point on managed
Windows machines whose App Control policy blocks the unsigned
``puffo-agent.exe`` shim created by pip or uv.
"""

from __future__ import annotations

from .portal.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
