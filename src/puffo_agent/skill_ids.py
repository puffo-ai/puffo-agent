"""Shared skill-id rule — single definition, imported everywhere.

Also a ``CHECK`` on ``skill_templates`` in puffo-server migration 038, which
can't import this and can't be edited (sqlx checksums applied migrations).
"""

import re

# ``\Z`` not ``$``: Python's ``$`` also matches before a trailing newline, so
# ``"ok-id\n"`` would pass here and be rejected by the SQL copy.
SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")
