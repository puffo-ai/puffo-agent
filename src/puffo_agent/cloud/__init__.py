"""Cloud-hosting support for puffo-agent.

Holds the pieces specific to running an agent in a remote sandbox (E2B)
behind a keyless trust boundary — chiefly the ``bridge`` client, which
is distinct from the ws-local ``WsLocalBridge`` (a local relay for an
externally-attached tool).
"""
