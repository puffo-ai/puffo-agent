"""Verify Puffo Driver authority against an exact LingTai source checkout.

This is the delivery-bound D6 acceptance oracle.  It deliberately supports
two negative modes whose assertions must fail:

* ``replay`` performs a second admitted operation, making the adjudication
  count greater than one.
* ``fanout`` makes one admitted operation reach the provider twice under the
  same audit ID.

Run it from the Puffo repository root, naming the LingTai checkout explicitly:

    uv run python scripts/verify_lingtai_driver_authority.py \
        --lingtai-src /path/to/lingtai-kernel/src baseline

Repeat with ``replay`` and ``fanout``.  Baseline must exit 0; both negative
modes must exit non-zero at their respective count assertions.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Any


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lingtai-src",
        required=True,
        type=Path,
        help="path to the exact LingTai checkout's src directory",
    )
    parser.add_argument("mode", choices=("baseline", "replay", "fanout"))
    return parser.parse_args()


def _load_components(lingtai_src: Path) -> tuple[Any, ...]:
    puffo_src = Path(__file__).resolve().parents[1] / "src"
    lingtai_src = lingtai_src.resolve(strict=True)
    if not (lingtai_src / "lingtai").is_dir():
        raise SystemExit(f"--lingtai-src does not contain lingtai/: {lingtai_src}")

    # Assemble the import path inside the delivered oracle so reviewers do not
    # depend on the author's shell state.  The candidate Puffo tree wins over
    # any installed copy; the explicitly named LingTai checkout is second.
    sys.path[:0] = [str(puffo_src), str(lingtai_src)]

    from lingtai.adapters.acp.driver_authority import DriverAuthorityAdapter
    from lingtai.kernel.provider_admission import (
        ProviderAdmittedLLMService,
        RootProviderAdmission,
        bind_provider_admission,
        clear_provider_admission,
        current_provider_call_audit_id,
    )
    from puffo_agent.agent.harness.driver_authority_server import DriverAuthorityServer

    return (
        DriverAuthorityAdapter,
        ProviderAdmittedLLMService,
        RootProviderAdmission,
        bind_provider_admission,
        clear_provider_admission,
        current_provider_call_audit_id,
        DriverAuthorityServer,
    )


def main(*, mode: str, lingtai_src: Path) -> None:
    (
        driver_authority_adapter,
        provider_admitted_llm_service,
        root_provider_admission,
        bind_provider_admission,
        clear_provider_admission,
        current_provider_call_audit_id,
        driver_authority_server,
    ) = _load_components(lingtai_src)

    class RecordingProvider:
        def __init__(self, *, fanout: bool) -> None:
            self.provider_calls: list[str | None] = []
            self.fanout = fanout

        def generate(self, _prompt: str) -> str:
            audit_id = current_provider_call_audit_id()
            self.provider_calls.append(audit_id)
            if self.fanout:
                self.provider_calls.append(audit_id)
            return "generated"

    server = driver_authority_server()
    adapter = None
    try:
        endpoint = server.issue_root(launch_id=f"root-d6-{mode}")
        inherited_fd = os.dup(endpoint.fileno())
        endpoint.close()
        adapter = driver_authority_adapter.from_inherited_fd(inherited_fd)
        inner = RecordingProvider(fanout=mode == "fanout")
        service = provider_admitted_llm_service(inner, adapter)
        token = bind_provider_admission(
            root_provider_admission(f"turn-{mode}", "puffo-v0.e2e")
        )
        try:
            service.generate("legal-operation")
            if mode == "replay":
                service.generate("replayed-legal-operation")
        finally:
            clear_provider_admission(token)

        records = [
            record
            for record in server.audit_records()
            if record.operation == "authorize_provider_call"
        ]
        adjudications = [record.audit_id for record in records]
        print(f"mode={mode}")
        print(f"adjudications={len(adjudications)} ids={adjudications}")
        print(f"provider_calls={len(inner.provider_calls)} ids={inner.provider_calls}")
        print(f"trace_after_call={current_provider_call_audit_id()!r}")

        assert len(adjudications) == 1
        audit_id = adjudications[0]
        assert isinstance(audit_id, str)
        assert inner.provider_calls == [audit_id]
        assert current_provider_call_audit_id() is None
    finally:
        if adapter is not None:
            adapter.close()
        server.close()


if __name__ == "__main__":
    arguments = _parse_args()
    main(mode=arguments.mode, lingtai_src=arguments.lingtai_src)
