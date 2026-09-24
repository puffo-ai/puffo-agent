"""The real store on macOS: the connection lives in the login Keychain.

``security`` is stood in for rather than run, so these are about the contract
with it — the command shape, and what each answer is allowed to mean. The fake
reproduces what was measured on this computer: exit 44 for a missing item, one
newline appended to a password on the way out, and ``-U`` replacing an item
rather than adding a second.

What is deliberately NOT claimed here: that a real ``security`` round trip
works. An agent environment has no login Keychain to write to and reaching the
operator's would mean changing HOME, so the leg from this module to a real
Keychain is unverified and has to be run somewhere with a daemon's HOME.
"""

from __future__ import annotations

import subprocess

import pytest

from puffo_agent.portal.connector import keychain_store
from puffo_agent.portal.connector.claim import STAGE_READ_LOCAL, claim_connection
from puffo_agent.portal.connector.keychain_store import (
    KeychainConnectionStore,
    KeychainUnavailable,
)

# Distinctive enough that finding it anywhere is unambiguous.
SENTINEL = "SENTINELe07b2c4arefreshtoken"

_ITEM_NOT_FOUND = 44


class FakeSecurity:
    """``/usr/bin/security``, as measured on this computer.

    Records every invocation so a test can ask what the daemon actually asked
    for — including what it put on the command line, which is the whole point
    of one of them.
    """

    def __init__(self, *, default_keychain: bytes = b"    \"/Users/x/login.keychain-db\"\n"):
        self.items: dict[tuple[str, str], bytes] = {}
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.default_keychain = default_keychain

    def verbs(self) -> list[str]:
        return [argv[1] for argv, _ in self.calls]

    # ``**_kwargs`` swallows whatever spawn policy the caller adds — on
    # Windows ``no_window_kwargs()`` contributes a creationflags entry, and a
    # stand-in that rejected it would make these tests macOS-only by accident.
    def __call__(self, argv, *, input=None, **_kwargs):
        self.calls.append((list(argv), input))
        verb = argv[1]
        if verb == "default-keychain":
            code = 0 if self.default_keychain else 1
            return self._done(argv, code, self.default_keychain, b"" if code == 0 else b"security: SecKeychainCopyDefault: A default keychain could not be found.\n")
        key = (_option(argv, "-s"), _option(argv, "-a"))
        if verb == "find-generic-password":
            if key not in self.items:
                return self._missing(argv)
            # security appends exactly one newline to the password it prints.
            return self._done(argv, 0, self.items[key] + b"\n", b"")
        if verb == "add-generic-password":
            # Both forms of -w are accepted on purpose: a trailing -w takes the
            # password from stdin, -w VALUE takes it from the command line.
            # Refusing the second here would make the argv test pass by
            # construction instead of by what the store does.
            self.items[key] = input if argv[-1] == "-w" else _option(argv, "-w").encode()
            return self._done(argv, 0, b"", b"")
        if verb == "delete-generic-password":
            if key not in self.items:
                return self._missing(argv)
            del self.items[key]
            return self._done(argv, 0, b"", b"")
        raise AssertionError(f"unexpected security verb {verb!r}")

    def _missing(self, argv):
        return self._done(
            argv,
            _ITEM_NOT_FOUND,
            b"",
            b"security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.\n",
        )

    @staticmethod
    def _done(argv, code, out, err):
        return subprocess.CompletedProcess(argv, code, out, err)


def _option(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def keychain(monkeypatch, fake: FakeSecurity) -> KeychainConnectionStore:
    monkeypatch.setattr(keychain_store.subprocess, "run", fake)
    return KeychainConnectionStore("mac_testmachine")


def test_the_credential_never_reaches_a_command_line(monkeypatch):
    """Another process running as this user can read a full command line out
    of ``ps``; ``security``'s own usage calls ``-w <password>`` insecure."""
    fake = FakeSecurity()
    store = keychain(monkeypatch, fake)

    store.save(request_ref="req-1", provider="fake", credential={"refresh_token": SENTINEL})

    for argv, _ in fake.calls:
        assert SENTINEL not in " ".join(argv)
    # Positive control: the scan above can see the sentinel when it is there,
    # so an empty result means "not in argv", not "looked in the wrong place".
    written = [stdin for _, stdin in fake.calls if stdin]
    assert written and all(SENTINEL.encode() in body for body in written)


def test_a_connection_survives_a_round_trip_through_the_keychain(monkeypatch):
    fake = FakeSecurity()
    store = keychain(monkeypatch, fake)

    saved = store.save(
        request_ref="req-1", provider="fake", credential={"refresh_token": SENTINEL}
    )

    reloaded = store.load()
    assert reloaded.reference == saved.reference
    assert reloaded.request_ref == "req-1"
    # Byte-exact despite the newline security adds on the way out.
    assert reloaded.credential == {"refresh_token": SENTINEL}


def test_a_refresh_replaces_the_item_rather_than_adding_a_second(monkeypatch):
    fake = FakeSecurity()
    store = keychain(monkeypatch, fake)
    saved = store.save(request_ref="req-1", provider="fake", credential={"token": "first"})

    store.update(reference=saved.reference, credential={"token": "second"})

    assert len(fake.items) == 1
    assert store.load().credential == {"token": "second"}


def test_a_disconnect_removes_the_item(monkeypatch):
    fake = FakeSecurity()
    store = keychain(monkeypatch, fake)
    store.save(request_ref="req-1", provider="fake", credential={"refresh_token": SENTINEL})

    store.clear()

    assert fake.items == {}
    assert store.load() is None


def test_a_reachable_keychain_with_no_item_means_not_connected(monkeypatch):
    """Positive control for the refusal below: 44 can still mean "none"."""
    store = keychain(monkeypatch, FakeSecurity())

    assert store.load() is None


def test_an_unreachable_keychain_is_not_reported_as_not_connected(monkeypatch):
    """44 is also what a search over the wrong keychains returns.

    Reading it as "no connection" would let the claim write over a credential
    that is sitting in a Keychain this process merely could not see.
    """
    store = keychain(monkeypatch, FakeSecurity(default_keychain=b""))

    with pytest.raises(KeychainUnavailable):
        store.load()


@pytest.mark.asyncio
async def test_a_claim_against_an_unreachable_keychain_writes_nothing(monkeypatch):
    """The refusal has to survive the trip up to the claim, or it buys nothing."""
    fake = FakeSecurity(default_keychain=b"")
    store = keychain(monkeypatch, fake)

    async def hands_over(_request_ref):
        return "fake", {"refresh_token": SENTINEL}

    result = await claim_connection("req-1", fetch=hands_over, store=store)

    assert result["ok"] is False
    assert result["stage"] == STAGE_READ_LOCAL
    assert "add-generic-password" not in fake.verbs()


def test_a_write_the_keychain_did_not_take_is_not_reported_as_saved(monkeypatch):
    """``security`` exiting 0 is not evidence the data is in the Keychain.

    The stdin form of ``-w`` is documented but cannot be exercised from an
    agent environment, so the write checks itself; without that a daemon could
    report connected with nothing stored.
    """
    fake = FakeSecurity()
    store = keychain(monkeypatch, fake)
    store.save(request_ref="req-1", provider="fake", credential={"token": "the good one"})
    kept = dict(fake.items)

    def swallow_the_write(argv, *, input=None, **kwargs):
        if argv[1] == "add-generic-password":
            fake.calls.append((list(argv), input))
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return fake(argv, input=input, **kwargs)

    monkeypatch.setattr(keychain_store.subprocess, "run", swallow_the_write)

    with pytest.raises(KeychainUnavailable):
        store.save(request_ref="req-2", provider="fake", credential={"token": "lost"})

    # The connection this computer was using is still there: a write that did
    # not land must not also destroy what it was replacing.
    assert fake.items == kept
    assert "delete-generic-password" not in fake.verbs()


def test_a_missing_security_binary_does_not_fall_back_to_anything(monkeypatch):
    def not_installed(*_args, **_kwargs):
        raise FileNotFoundError("/usr/bin/security")

    monkeypatch.setattr(keychain_store.subprocess, "run", not_installed)
    store = KeychainConnectionStore("mac_testmachine")

    with pytest.raises(KeychainUnavailable):
        store.load()


def test_a_keychain_that_never_answers_is_not_read_as_empty(monkeypatch):
    """A timeout is where an authorization prompt would show up. Treating it
    as "no connection" would overwrite a credential behind that prompt."""

    def hangs(argv, **_kwargs):
        raise subprocess.TimeoutExpired(argv, 60)

    monkeypatch.setattr(keychain_store.subprocess, "run", hangs)
    store = KeychainConnectionStore("mac_testmachine")

    with pytest.raises(KeychainUnavailable):
        store.load()


def test_a_disconnect_that_could_not_clear_does_not_report_success(monkeypatch):
    """The promise is that the local credential goes first (Jeremy 217298);
    a swallowed failure here says disconnected while it is still there."""
    fake = FakeSecurity()
    store = keychain(monkeypatch, fake)
    store.save(request_ref="req-1", provider="fake", credential={"refresh_token": SENTINEL})

    def refuses_to_delete(argv, *, input=None, **kwargs):
        if argv[1] == "delete-generic-password":
            return subprocess.CompletedProcess(argv, 1, b"", b"security: keychain is locked")
        return fake(argv, input=input, **kwargs)

    monkeypatch.setattr(keychain_store.subprocess, "run", refuses_to_delete)

    with pytest.raises(KeychainUnavailable):
        store.clear()


def test_clearing_a_connection_that_is_already_gone_is_not_a_failure(monkeypatch):
    """44 on delete is the same "no such item"; a second disconnect is fine."""
    store = keychain(monkeypatch, FakeSecurity())

    store.clear()


# ── which backend a computer gets ────────────────────────────────────────────

def a_machine():
    """Only ``machine_id`` is read by the selection, so the keys can be junk."""
    from puffo_agent.portal.control.store import MachineControlIdentity

    return MachineControlIdentity(
        machine_id="mac_testmachine", signing_secret="x", kem_secret="y"
    )


def test_a_macos_computer_keeps_its_connection_in_the_keychain(monkeypatch, tmp_path):
    from puffo_agent.portal.connector import command

    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    monkeypatch.setattr(command, "_KEYCHAIN_VERIFIED", True)
    monkeypatch.setattr(command.platform, "system", lambda: "Darwin")

    store = command.connection_store(a_machine())

    assert isinstance(store, KeychainConnectionStore)
    # Keyed on the identity that owns the connection, not on the home: one
    # Keychain serves every puffo home on the computer.
    assert store.account == "mac_testmachine"


def test_a_computer_without_a_keychain_keeps_its_connection_in_a_file(monkeypatch, tmp_path):
    from puffo_agent.portal.connector import command
    from puffo_agent.portal.connector.store import SkeletonConnectionStore

    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    monkeypatch.setattr(command, "_KEYCHAIN_VERIFIED", True)
    monkeypatch.setattr(command.platform, "system", lambda: "Linux")

    store = command.connection_store(a_machine())

    assert isinstance(store, SkeletonConnectionStore)
    assert store.path == tmp_path / "connector" / "connection.json"


def test_macos_still_gets_the_file_store_while_the_keychain_leg_is_unverified(
    monkeypatch, tmp_path
):
    """A tripwire on the staging, not a property worth keeping.

    The Keychain backend is complete but its leg to a real login Keychain has
    never been run, so macOS is deliberately still on the file store. Whoever
    produces that run deletes this test in the same change that flips the
    constant — if it goes red on its own, the switch happened without the
    evidence that was supposed to come with it.
    """
    from puffo_agent.portal.connector import command
    from puffo_agent.portal.connector.store import SkeletonConnectionStore

    monkeypatch.setenv("PUFFO_AGENT_HOME", str(tmp_path))
    monkeypatch.setattr(command.platform, "system", lambda: "Darwin")

    assert isinstance(command.connection_store(a_machine()), SkeletonConnectionStore)
