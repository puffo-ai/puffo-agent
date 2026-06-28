"""api-puffo runtime: cloud-hosted agents.

Bearer session_token auth + cloud-mediated RPC. Decrypt-inbound is
local (KEM secret stays on the daemon), encrypt-outbound and LLM
inference are cloud-side. No CLI subprocess, no MCP subprocess —
the worker runs an in-process LLM loop calling cloud endpoints.
"""
