"""Make pytest import the in-tree source instead of any installed
``puffo-agent``. Lets the test suite run against source without
requiring ``pip install -e .``.
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Allow test files to import sibling helpers like ``_bridge_support``.
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))
