"""Per-agent logger that prefixes each record with ``agent <id>:``
so log lines from a multi-agent daemon stay attributable."""

from __future__ import annotations

import logging


class _AgentLogAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        agent_id = self.extra.get("agent_id") if self.extra else ""
        if agent_id:
            return f"agent {agent_id}: {msg}", kwargs
        return msg, kwargs


def agent_logger(name: str, agent_id: str) -> logging.LoggerAdapter:
    return _AgentLogAdapter(logging.getLogger(name), {"agent_id": agent_id})
