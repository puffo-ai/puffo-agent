"""Process birth and home ownership for durable daemon lifecycle markers.

PID files stay numeric for older CLIs. New daemons publish an identity
sidecar; readers never fall back to PID-only checks when that sidecar exists.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import secrets

import psutil


@dataclass(frozen=True)
class DaemonIdentity:
    pid: int
    create_time: float
    home: str
    instance: str


def identity_path(home: Path) -> Path:
    return home / "daemon.identity.json"


def normalized_home(home: Path) -> str:
    return os.path.normcase(str(home.resolve()))


def parse_identity(raw: object) -> DaemonIdentity | None:
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return None
    pid, birth = raw.get("pid"), raw.get("create_time")
    home, instance = raw.get("home"), raw.get("instance")
    if (
        type(pid) is not int
        or pid <= 0
        or type(birth) not in (int, float)
        or not math.isfinite(birth)
        or birth <= 0
        or not isinstance(home, str)
        or not Path(home).is_absolute()
        or not isinstance(instance, str)
        or not instance
    ):
        return None
    return DaemonIdentity(pid, float(birth), home, instance)


def identity_payload(identity: DaemonIdentity) -> dict:
    return {"version": 1, **asdict(identity)}


def read_identity(home: Path) -> DaemonIdentity | None:
    try:
        return parse_identity(
            json.loads(identity_path(home).read_text(encoding="utf-8"))
        )
    except (OSError, ValueError):
        return None


def publish_identity(home: Path, pid: int) -> DaemonIdentity:
    identity = DaemonIdentity(
        pid,
        psutil.Process(pid).create_time(),
        normalized_home(home),
        secrets.token_hex(16),
    )
    path = identity_path(home)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(identity_payload(identity)), encoding="utf-8")
    temporary.replace(path)
    return identity


def _process_home(proc: psutil.Process, pid: int) -> str | None:
    environment = dict(os.environ) if pid == os.getpid() else proc.environ()
    override = environment.get("PUFFO_AGENT_HOME")
    user_home = (
        environment.get("USERPROFILE") if os.name == "nt" else environment.get("HOME")
    )
    if override:
        if override == "~" or override.startswith("~/"):
            if not user_home:
                return None
            override = str(Path(user_home) / override[2:])
        home = Path(override)
    elif user_home:
        home = Path(user_home) / ".puffo-agent"
    else:
        return None
    if not home.is_absolute():
        home = Path(proc.cwd()) / home
    return normalized_home(home)


def inspect_process(home: Path, pid: int) -> float | None:
    """Return birth time only for a daemon in this home; deny unknown ownership."""
    try:
        proc = psutil.Process(pid)
        tokens = [t or "" for t in proc.cmdline()]
        has_exe = any(
            Path(t).name.lower().startswith(("puffo-agent", "puffo_agent"))
            for t in tokens
        )
        if not has_exe or "start" not in [t.lower() for t in tokens]:
            return None
        if _process_home(proc, pid) != normalized_home(home):
            return None
        return proc.create_time()
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None
    except psutil.AccessDenied as exc:
        # Unknown is not proof of absence: don't launch a second daemon.
        raise RuntimeError(f"cannot verify daemon ownership for pid={pid}") from exc


def resolve_identity(home: Path, pid: int) -> DaemonIdentity | None:
    birth = inspect_process(home, pid)
    if birth is None:
        return None
    path = identity_path(home)
    if path.exists():
        identity = read_identity(home)
        if (
            identity is not None
            and identity.pid == pid
            and identity.create_time == birth
            and identity.home == normalized_home(home)
        ):
            return identity
        return None
    # Legacy upgrade: only a same-home process already alive when the marker
    # was written can own it. Never promote an unverified stale PID to v1.
    try:
        if birth > (home / "daemon.pid").stat().st_mtime:
            return None
    except OSError:
        return None
    return DaemonIdentity(pid, birth, normalized_home(home), "legacy")


def identity_is_alive(home: Path, identity: DaemonIdentity) -> bool:
    current = read_identity(home)
    if current is not None and current.pid == identity.pid and current != identity:
        return False
    return (
        identity.home == normalized_home(home)
        and inspect_process(home, identity.pid) == identity.create_time
    )
