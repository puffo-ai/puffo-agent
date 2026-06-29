# puffo-agent-cloud

Slim cloud-hosted agent runtime for Puffo.ai. Installs into an E2B
sandbox with only thin deps (`aiohttp` + `pyyaml` + `puffo-agent-core`).
Speaks the puffo-server bridge (plaintext WS) and a cloud-hosted LLM
HTTP endpoint; holds no key material. Entry point: `puffo-agent-cloud`.
