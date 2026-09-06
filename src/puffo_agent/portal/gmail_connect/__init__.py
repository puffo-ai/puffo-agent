"""Gmail token-axis connect: daemon-side initiation, status, disconnect.

The daemon never holds OAuth token material. It initiates the flow only
after a device-local native confirmation, drives the gateway OAuth
executor as a subprocess (which owns the token file end to end), and
persists/publishes nothing beyond a sanitized status projection.
"""
