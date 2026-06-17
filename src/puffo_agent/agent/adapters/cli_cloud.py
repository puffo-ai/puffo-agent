"""cli-cloud adapter: the cli-local harness, keyless, in an E2B sandbox.

Same claude-code/codex subprocess as cli-local — only the trust seam
differs. Model calls route through the LiteLLM gateway with a scoped
virtual key, and all puffo-server I/O goes through the Bridge, so the
sandbox holds no identity keys. The harness machinery is inherited
wholesale from ``LocalCLIAdapter``; this class exists to give the
dispatch a distinct target and a home for future cloud-only divergence.
"""

from __future__ import annotations

from .local_cli import LocalCLIAdapter


class CliCloudAdapter(LocalCLIAdapter):
    runtime_kind = "cli-cloud"
