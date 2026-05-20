"""puffo-agent top-level package.

The only thing the package-init does is the Python-version precheck
— it runs *before* any submodule of ``puffo_agent`` is parsed, which
means a user on Python 3.9 / 3.10 gets a clear, actionable message
even if a downstream module happens to use 3.11-only syntax. The
deliberate constraint on this file: keep it parseable on Python 3.6+
(f-strings only, no PEP 604 unions in expression position, no
``match/case``) so the precheck itself never trips before it runs.
"""

import sys


def _require_python_311() -> None:
    if sys.version_info < (3, 11):
        v = sys.version_info
        sys.stderr.write(
            f"puffo-agent requires Python >= 3.11; you have "
            f"{v.major}.{v.minor}.{v.micro}.\n"
            f"Upgrade via pyenv (`pyenv install 3.12`), Homebrew "
            f"(`brew install python@3.12`), or python.org/downloads, "
            f"then re-run.\n"
        )
        raise SystemExit(1)


_require_python_311()
