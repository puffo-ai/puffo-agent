"""Process birth and home ownership for durable daemon lifecycle markers.

PID files stay numeric for older CLIs. New daemons publish an identity
sidecar; readers never fall back to PID-only checks when that sidecar exists.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import math
import ntpath
import os
from pathlib import Path
import secrets
import time

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


def _windows_user_home(environment: dict[str, str], user: str) -> str:
    home = environment.get("USERPROFILE")
    if home is None:
        tail = environment.get("HOMEPATH")
        home = environment.get("HOMEDRIVE", "") + tail if tail is not None else None
    if home is None:
        raise RuntimeError("cannot resolve daemon user home")
    if user and user != environment.get("USERNAME"):
        if environment.get("USERNAME") != ntpath.basename(home):
            raise RuntimeError("cannot resolve daemon user home")
        home = ntpath.join(ntpath.dirname(home), user)
    return home


def _user_home(
    proc: psutil.Process, environment: dict[str, str], user: str = ""
) -> str:
    if os.name == "nt":
        return _windows_user_home(environment, user)
    if not user and "HOME" in environment:
        return environment["HOME"]
    import pwd

    try:
        return (
            pwd.getpwnam(user).pw_dir if user else pwd.getpwuid(proc.uids().real).pw_dir
        )
    except KeyError as exc:
        raise RuntimeError("cannot resolve daemon user home") from exc


def _process_home(proc: psutil.Process, pid: int) -> str:
    environment = dict(os.environ) if pid == os.getpid() else proc.environ()
    override = environment.get("PUFFO_AGENT_HOME")
    if override:
        if override.startswith("~"):
            suffix = (
                override[1:].replace("\\", "/") if os.name == "nt" else override[1:]
            )
            user, _, tail = suffix.partition("/")
            override = str(Path(_user_home(proc, environment, user)) / tail)
        home = Path(override)
    else:
        home = Path(_user_home(proc, environment)) / ".puffo-agent"
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


@contextmanager
def marker_lock(home: Path):
    """Serialize short marker transactions; never delete the lock inode."""
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(home / "daemon.markers.lock", os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
        else:
            import fcntl
        deadline = time.monotonic() + 5
        while True:
            try:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (BlockingIOError, PermissionError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("daemon marker lock timed out")
                time.sleep(0.01)
        yield
    finally:
        if acquired:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
