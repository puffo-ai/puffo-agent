"""Agent Portal control plane (machine side).

A machine generates its own control keypair locally (``machine.json``), enrolls
with one or more operators via ``puffo-agent link``, and pins each operator's
root pubkey (``pairings.json``). The daemon then polls the server for E2E
command envelopes, verifies them against the pinned operator root, executes
them, and reports encrypted agent state back. The private control key never
leaves the machine.
"""
