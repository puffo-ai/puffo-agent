"""Runtime adapters: translation layers between the portal shell
and an external CLI runtime.

Adapters configure the runtime, forward its output, and manage its
lifecycle; they do not implement tools or run the agentic loop
themselves. See ``base.py`` for the interface.
"""

from .base import Adapter, TurnContext, TurnResult

__all__ = ["Adapter", "TurnContext", "TurnResult"]
