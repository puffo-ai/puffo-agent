#!/usr/bin/env python3
"""Walk the Gmail connect flow on this machine, from the branch, no merge.

  python demo_gmail_connect.py           # real macOS confirm dialog
  python demo_gmail_connect.py --auto    # skip the dialog (CI / headless)

What is REAL here: the confirm gate, the subprocess spawn and its
stdin/stdout contract, the loopback check, the reason clamp, the state
file, and the Layer-B projection the UI would read.

What is a STAND-IN: the executor itself. Bob's gateway/connect_executor.py
does not exist yet, so this drives a stub that speaks the same schema
v1.1 lines. Nothing here contacts Google and no credential is involved —
a real connect additionally needs Bob's module and a GCP client bundle
placed by a human, never through an agent.
"""
import asyncio, json, os, sys, tempfile, pathlib

sys.path.insert(0, "src")
os.environ.setdefault("PUFFO_AGENT_HOME", tempfile.mkdtemp(prefix="gmail-demo-"))

from puffo_agent.portal.gmail_connect import ops                      # noqa: E402
from puffo_agent.portal.gmail_connect.status_store import (           # noqa: E402
    load_status, status_path,
)
from puffo_agent.portal.state import DaemonConfig, GmailConnectConfig  # noqa: E402

AUTO = "--auto" in sys.argv
STUB = '''#!/usr/bin/env python3
import json, sys, time
req = json.loads(sys.stdin.readline())
print(json.dumps({"event": "ready",
                  "redirect_uri": "http://127.0.0.1:49713/oauth2/callback"}), flush=True)
time.sleep(1.0)                      # stands in for the consent page
print(json.dumps({"event": "result", "status": "connected",
                  "summary": {"scope": "https://www.googleapis.com/auth/gmail.send",
                              "expires_in": 3599, "has_refresh_token": True,
                              "token_db": "/Users/you/.puffo-agent/gmail/tokens.db"}}),
      flush=True)
'''

def show(label):
    st = load_status()
    print(f"  {label:<22} state={st.state!r:<16} reason={st.reason!r}")

async def main():
    d = pathlib.Path(tempfile.mkdtemp())
    stub = d / "stub_executor.py"
    stub.write_text(STUB); stub.chmod(0o755)

    cfg = DaemonConfig()
    cfg.gmail_connect = GmailConnectConfig(
        enabled=True, executor_path=str(stub), data_root=str(d),
        client_bundle_sha256="a" * 64, flow_timeout_seconds=30.0,
    )
    ops._config = lambda: cfg
    if AUTO:
        async def yes(prompt, timeout_s=0.0):
            print(f"  [--auto] confirm prompt: {prompt}")
            return True
        ops.request_native_confirm = yes

    print("\n=== Gmail connect, on this machine ===")
    print(f"state file: {status_path()}\n")
    show("before")
    print("\n-> gmail.connect_initiate  (a dialog opens unless --auto)")
    res = await ops.gmail_connect_initiate({})
    print(f"  control-plane reply: {json.dumps(res)}")
    show("after connect")
    print(f"  ON DISK: {status_path().read_text()}")
    print(f"  WHAT THE UI SEES: {json.dumps(load_status().projection())}")
    print("  ^ note: no token_db, no scope, no paths — Layer A stopped at the daemon\n")

    print("-> gmail.disconnect (token-only)")
    res = await ops.gmail_disconnect_token({})
    print(f"  control-plane reply: {json.dumps(res)}")
    show("after disconnect")
    print("  ^ 'revoke_unconfirmed', not 'revoked': the grant axis does not exist yet,\n"
          "    so we do not claim the token died everywhere.\n")

asyncio.run(main())
