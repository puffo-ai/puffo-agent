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

COVERAGE NOTE — what this oracle stopped verifying, and why
-----------------------------------------------------------
It once asserted that the provider call ran under *the same audit id* as the
adjudication that admitted it, by reading ``current_provider_call_audit_id()``
inside the provider.  LingTai removed that accessor: the audit id is no longer
propagated to the provider call site, and ``ProviderCallDecision.audit_id`` is
now documented as "correlation material only ... never a grant".

So this oracle now checks COUNTS on both sides -- exactly one adjudication and
exactly one provider call -- which still reddens ``replay`` (two adjudications)
and ``fanout`` (two provider calls).  It does NOT check request-to-response
CORRELATION: a provider call executing under a DIFFERENT adjudication's id
would pass every assertion here.  Do not read a green run as covering that axis.

An earlier version of this note claimed the correlation check "cannot" be
restored because nothing observable binds a call to an adjudication.  That
claim was wrong -- asserted without enumerating what is actually reachable.
At least two candidate observables exist:

* ``lingtai.kernel.provider_admission.current_provider_admission()`` is
  exported and readable at the provider call site; ``RootProviderAdmission``
  carries ``correlation_id``, documented as audit/routing material.  This is
  ROOT-TURN scoped, not per-call.
* ``AuthorityAuditRecord.call_id`` on the Puffo side is the client-supplied
  per-call id echoed back into the audit record.

Whether those two join into a genuine per-call correlation is UNVERIFIED.
Note the granularity trap: the accessor LingTai removed
(``current_provider_call_audit_id``) was the PER-CALL one, while the surviving
``correlation_id`` hangs off ``RootProviderAdmission`` and is ROOT-TURN scoped.
So restoring per-call correlation forks into either (a) a LingTai-side per-call
observable, which first needs to establish why the old accessor was deliberately
removed -- a design decision, not an oracle question -- or (b) threading the
Puffo-side ``call_id`` end to end.  Neither branch has been shown to work.

The honest status is therefore "not currently checked, closability unverified",
not "impossible".
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

    from lingtai.adapters.acp.driver_authority import DriverAuthorityClient
    from lingtai.kernel.provider_admission import (
        ProviderAdmittedLLMService,
        RootProviderAdmission,
        bind_provider_admission,
        clear_provider_admission,
    )
    from puffo_agent.agent.harness.driver_authority_server import DriverAuthorityServer

    return (
        DriverAuthorityClient,
        ProviderAdmittedLLMService,
        RootProviderAdmission,
        bind_provider_admission,
        clear_provider_admission,
        DriverAuthorityServer,
    )


def _require(condition: bool, message: str) -> None:
    """Fail the delivery oracle even when Python assertions are disabled."""
    if not condition:
        raise SystemExit(f"Driver authority verification failed: {message}")


def _validate_oracle(
    adjudications: list[str],
    provider_calls: list[str | None],
) -> str:
    """Validate the one-adjudication/one-provider-call delivery contract.

    LingTai removed the per-call audit id, so a provider call can no longer be
    matched to its adjudication BY IDENTITY: fan-out is caught by the call
    COUNT alone, and the post-call leak check is gone entirely. See the
    coverage note in the module docstring for what that costs.
    """
    _require(
        len(adjudications) == 1,
        f"expected one adjudication, observed {len(adjudications)}",
    )
    audit_id = adjudications[0]
    _require(isinstance(audit_id, str), "adjudication audit ID is not a string")
    _require(
        len(provider_calls) == 1,
        f"expected one provider call, observed {len(provider_calls)}",
    )
    return audit_id


def main(*, mode: str, lingtai_src: Path) -> None:
    (
        driver_authority_adapter,
        provider_admitted_llm_service,
        root_provider_admission,
        bind_provider_admission,
        clear_provider_admission,
        driver_authority_server,
    ) = _load_components(lingtai_src)

    class RecordingProvider:
        def __init__(self, *, fanout: bool) -> None:
            self.provider_calls: list[str | None] = []
            self.fanout = fanout

        def generate(self, prompt: str) -> str:
            # The admitting audit id is no longer observable here — LingTai
            # deliberately stopped propagating it to the provider call site
            # (``ProviderCallDecision.audit_id`` is documented as correlation
            # material only).  Record the prompt so fan-out is still counted.
            self.provider_calls.append(prompt)
            if self.fanout:
                self.provider_calls.append(prompt)
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

        _validate_oracle(
            adjudications,
            inner.provider_calls,
        )
    finally:
        if adapter is not None:
            adapter.close()
        server.close()


if __name__ == "__main__":
    arguments = _parse_args()
    main(mode=arguments.mode, lingtai_src=arguments.lingtai_src)
