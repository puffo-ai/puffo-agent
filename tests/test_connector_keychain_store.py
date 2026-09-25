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

import asyncio
import json
import logging
import shlex
import subprocess
from pathlib import Path

import pytest

from puffo_agent.portal.connector import keychain_store
from puffo_agent.portal.connector.claim import (
    STAGE_READ_LOCAL,
    claim_connection,
    disconnect,
)
from puffo_agent.portal.connector import store as store_module
from puffo_agent.portal.connector.store import SkeletonConnectionStore
from puffo_agent.portal.connector.keychain_store import (
    ConflictingConnections,
    ConflictingCredentials,
    KeychainConnectionStore,
    KeychainUnavailable,
    SupersededCopyRemains,
)

# Distinctive enough that finding it anywhere is unambiguous.
SENTINEL = "SENTINELe07b2c4arefreshtoken"

_ITEM_NOT_FOUND = 44


@pytest.fixture(autouse=True)
def a_fresh_process():
    """The once-per-process move guard is module state, so each cell needs its
    own process as far as that guard is concerned. Without this the first cell
    to run consumes the single attempt and every later one sees a store that
    has already given up."""
    keychain_store._MOVE_ATTEMPTED.clear()
    yield
    keychain_store._MOVE_ATTEMPTED.clear()


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
        if argv[1] == "-i":
            return self._interactive(argv, input)
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
            # Reached only when the store writes through argv, which is the
            # shape this file exists to rule out. Accepted rather than refused
            # so the argv test fails on its own assertion instead of on a
            # stand-in that made the bad shape impossible.
            self.items[key] = input if argv[-1] == "-w" else _option(argv, "-w").encode()
            return self._done(argv, 0, b"", b"")
        if verb == "delete-generic-password":
            if key not in self.items:
                return self._missing(argv)
            del self.items[key]
            return self._done(argv, 0, b"", b"")
        raise AssertionError(f"unexpected security verb {verb!r}")

    def _interactive(self, argv, script):
        """``security -i`` reading one command off stdin.

        Parsed rather than pattern-matched so a malformed command shows up as
        a failure here instead of quietly looking like a write that worked.
        """
        parts = shlex.split(script.decode())
        if not parts or parts[0] != "add-generic-password":
            return self._done(argv, 1, b"", b"unsupported interactive command")
        key = (_option(parts, "-s"), _option(parts, "-a"))
        self.items[key] = bytes.fromhex(_option(parts, "-X"))
        # security -i does not carry an inner failure out in its exit code, so
        # this returns 0 for anything it accepted at all — the same unhelpful
        # success the real one gives (Boris 220754).
        return self._done(argv, 0, b"", b"")

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
    # And argv is only ever the two words: no name, no value, nothing to read
    # out of `ps` at all beyond the fact that a Keychain call happened.
    assert [argv[1:] for argv, stdin in fake.calls if stdin] == [["-i"]]
    # Positive control: the sentinel IS in what went to stdin, as hex — so the
    # empty result above means "not in argv", not "looked in the wrong place".
    written = [stdin for _, stdin in fake.calls if stdin]
    assert written and all(SENTINEL.encode().hex() in body.decode() for body in written)


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
        # Exactly what `security -i` does on an inner failure: exit 0 and
        # store nothing (Boris 220754), which is why a zero cannot be the gate.
        if argv[1] == "-i":
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
    """44 on delete is the same "no such item"; a second disconnect is fine.

    Also the positive control for the unreachable-Keychain refusal below: with
    a Keychain that resolves, 44 still means there was nothing to remove.
    """
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


def test_a_disconnect_against_an_unreachable_keychain_is_not_reported_as_cleared(
    monkeypatch,
):
    """Delete answers 44 for "already gone" and for "never reached the right
    Keychain" alike — the same ambiguity ``_read`` has, and it was missed here
    (Jeff 220726). Reading it as done would say disconnected while the
    credential sat there."""
    store = keychain(monkeypatch, FakeSecurity(default_keychain=b""))

    with pytest.raises(KeychainUnavailable):
        store.clear()


def test_a_stored_record_is_always_printable_ascii():
    """The constraint the Keychain transport turned out to need, for free.

    Jeff 220731/220737 measured that ``security`` hands a value back as hex
    rather than raw bytes once it contains non-ASCII, a trailing newline, an
    inner newline or a tab — so a writer has to keep everything inside
    0x20–0x7e or the read side becomes ambiguous. Boris's diagnostic has to
    *refuse* values that miss it, because the Claude blob's shape is not ours.

    This store never has to refuse: ``_encode`` is ``json.dumps`` with the
    default ``ensure_ascii``, which escapes every code point outside that
    range — including DEL, which JSON does not require escaping and which
    CPython escapes anyway (verified 0x00–0x2000 exhaustively and sampled
    above, on 3.12). The property is what the transport depends on, so it is
    pinned here rather than left as a fact about a library default.
    """
    from puffo_agent.portal.connector.store import Connection, _encode

    awkward = {
        "non_ascii": "令牌",
        "emoji": "\U0001f600",
        "inner_newline": "a\nb",
        "inner_tab": "a\tb",
        "trailing_newline": "abc\n",
        "del_character": "a\x7fb",
        "nul": "a\x00b",
        "quotes": 'a"\\b',
        "nested": [1, {"deep\x7f": None}],
    }
    body = _encode(
        Connection(reference="r", request_ref="q", provider="p", credential=awkward)
    )

    outside = sorted({byte for byte in body if not 0x20 <= byte <= 0x7E})
    assert outside == [], [hex(b) for b in outside]
    # Positive control: the same scan does flag a byte that is out of range,
    # so the empty result above is a measurement and not a vacuous one.
    assert [b for b in "a\nb".encode() if not 0x20 <= b <= 0x7E] == [0x0A]


def test_a_value_that_is_not_our_record_is_refused_and_never_decoded(monkeypatch):
    """`security` hands a value back as hex once it holds bytes outside
    0x20-0x7e, and a hex string is printable ASCII just like our records are —
    so "looks like hex" cannot be the test (Jeff 220731, who ruled that rule
    out). Whatever will not parse is not ours and fails closed."""
    fake = FakeSecurity()
    store = keychain(monkeypatch, fake)
    ours = b'{"reference":"r","request_ref":"q","provider":"p","credential":"v"}'
    # Exactly the trap: the stored bytes are a valid hex encoding OF our record.
    fake.items[("Puffo Agent-connector", "mac_testmachine")] = ours.hex().encode()

    with pytest.raises(KeychainUnavailable) as refused:
        store.load()

    assert "not decoded on a guess" in str(refused.value)


def test_a_value_that_will_not_even_decode_does_not_name_its_own_bytes(monkeypatch):
    """The refusal above must not become a way to read the value one byte at a
    time. A ``UnicodeDecodeError`` names the byte it choked on, and this message
    travels out through the claim's save-stage reason."""
    fake = FakeSecurity()
    store = keychain(monkeypatch, fake)
    fake.items[("Puffo Agent-connector", "mac_testmachine")] = b"\xffabc"

    with pytest.raises(KeychainUnavailable) as refused:
        store.load()

    assert "not decoded on a guess" in str(refused.value)
    assert "0xff" not in str(refused.value)
    assert "UnicodeDecodeError" in str(refused.value)


def test_a_name_that_would_need_quoting_rules_nobody_measured_is_refused(monkeypatch):
    """A space is measured working (Jeff 220737); an embedded quote is not, and
    guessing could write the item somewhere else entirely."""
    monkeypatch.setattr(keychain_store.subprocess, "run", FakeSecurity())

    with_a_quote = KeychainConnectionStore("mac_test", service="what's this")
    with pytest.raises(KeychainUnavailable):
        with_a_quote.save(request_ref="r", provider="p", credential={"t": SENTINEL})

    not_printable = KeychainConnectionStore("mac_test", service="café")
    with pytest.raises(KeychainUnavailable):
        not_printable.save(request_ref="r", provider="p", credential={"t": SENTINEL})


def test_a_service_name_with_a_space_is_not_refused(monkeypatch):
    """Positive control for the refusal above — measured working, so the guard
    must not be a blanket ban on anything unusual."""
    fake = FakeSecurity()
    monkeypatch.setattr(keychain_store.subprocess, "run", fake)
    spaced = KeychainConnectionStore("mac_test", service="Puffo Agent connector")

    saved = spaced.save(request_ref="r", provider="p", credential={"t": SENTINEL})

    assert spaced.load().reference == saved.reference


@pytest.mark.parametrize(
    "echo",
    [
        pytest.param(
            ('{"credential":"' + SENTINEL + '"}').encode().hex().encode(), id="hex"
        ),
        pytest.param(SENTINEL.encode(), id="plaintext"),
        pytest.param(SENTINEL.encode()[:12], id="plaintext-fragment"),
        # Jeff 220856: a credential can hold a number that looks like an
        # OSStatus, which is what killed the previous, narrower version.
        pytest.param(b'{"token":"SYNTHETIC -123456"}', id="negative-number"),
    ],
)
def test_a_failure_message_does_not_carry_the_value_back_out(monkeypatch, echo):
    """A pipe does not leak; what you do with what you read out of it can
    (Jeff 220786).

    Three shapes rather than one, because the earlier version of this checked
    only the hex — and a pattern that masks hex says nothing about plaintext.
    Jeff 220838 put an ordinary sentinel through it and it came out whole. The
    answer is not a better pattern: stderr's free text is not forwarded at all.
    """

    def echoes_the_value(argv, *, input=None, **_kwargs):
        return subprocess.CompletedProcess(
            argv, 1, b"", b"security: bad argument -X " + echo + b" (-25299)\n"
        )

    monkeypatch.setattr(keychain_store.subprocess, "run", echoes_the_value)
    store = KeychainConnectionStore("mac_testmachine")

    with pytest.raises(KeychainUnavailable) as failed:
        store.save(request_ref="r", provider="p", credential={"t": SENTINEL})

    # Equality, not absence. "the sentinel is not in there" is a test of this
    # sentinel; "the text is exactly the exit code" is a test of the rule, and
    # it is the only form that cannot be passed by a cleverer filter.
    assert str(failed.value).endswith("exit_code=1")
    assert echo.decode() not in str(failed.value)
    assert SENTINEL not in str(failed.value)


# ---------------------------------------------------------------------------
# The computer that connected before the switch.
#
# Its credential is in the file store, and a Keychain store that only ever
# looked in the Keychain would call it "not connected" and clear nothing on a
# disconnect — the credential reported gone while it stayed readable on disk.
# 測試姬 220874 listed this as the last gate on default-enable.


def files_holding(directory: Path, needle: str) -> list[Path]:
    """Every readable file under ``directory`` whose bytes contain ``needle``.

    Reads bytes directly rather than shelling out: this machine's ``grep`` is a
    wrapper that injects ``-I`` and can drop a file while reporting "no match".
    """
    return [
        path
        for path in sorted(directory.rglob("*"))
        if path.is_file() and needle.encode() in path.read_bytes()
    ]


def a_computer_that_connected_before_the_switch(tmp_path: Path) -> Path:
    """The file store's record, written by the store that used to be here."""
    legacy = tmp_path / "connection.json"
    SkeletonConnectionStore(legacy).save(
        request_ref="req-old", provider="google", credential={"refresh_token": SENTINEL}
    )
    return legacy


def keychain_over(monkeypatch, fake: FakeSecurity, legacy: Path) -> KeychainConnectionStore:
    monkeypatch.setattr(keychain_store.subprocess, "run", fake)
    return KeychainConnectionStore("mac_testmachine", superseded=legacy)


def test_the_scan_can_see_a_credential_that_is_there(tmp_path):
    """Positive control: a green scan below has to mean something."""
    (tmp_path / "left-behind").write_text(f'{{"refresh_token": "{SENTINEL}"}}')

    assert files_holding(tmp_path, SENTINEL)


def test_a_connection_made_before_the_switch_is_still_found(monkeypatch, tmp_path):
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    store = keychain_over(monkeypatch, FakeSecurity(), legacy)

    loaded = store.load()

    assert loaded is not None
    assert loaded.request_ref == "req-old"
    assert loaded.credential == {"refresh_token": SENTINEL}


def test_a_second_authorization_cannot_slip_past_the_older_connection(monkeypatch, tmp_path):
    """The reason the fallback returns the record instead of refusing.

    Answering "not connected" would let a second authorization save into the
    Keychain while the first credential stayed on disk and live at the
    provider — bypassing the one refusal the claim exists to make.
    """
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    store = keychain_over(monkeypatch, FakeSecurity(), legacy)

    async def must_not_be_called(request_ref):
        raise AssertionError("a connection is already here")

    answer = asyncio.run(claim_connection("req-new", fetch=must_not_be_called, store=store))

    assert answer["ok"] is False
    assert answer["connected"] is False
    # The older connection survived the refusal — now in the Keychain, since
    # reading it also moves it, and no longer in the clear on disk.
    assert store.load().request_ref == "req-old"
    assert files_holding(tmp_path, SENTINEL) == []


def test_a_disconnect_after_the_switch_takes_the_older_copy_too(monkeypatch, tmp_path):
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    # A leftover from some earlier failed save, of the kind a crash still makes.
    (tmp_path / "connection.partial").write_text(f'{{"refresh_token": "{SENTINEL}"}}')
    store = keychain_over(monkeypatch, FakeSecurity(), legacy)

    store.clear()

    assert files_holding(tmp_path, SENTINEL) == []


def test_a_save_sweeps_the_older_copy_once_the_keychain_holds_it(monkeypatch, tmp_path):
    legacy = tmp_path / "connection.json"
    # Not a connection — a stale file the claim already accounted for.
    legacy.write_text("{}")
    fake = FakeSecurity()
    store = keychain_over(monkeypatch, fake, legacy)

    store.save(request_ref="req-new", provider="google", credential={"refresh_token": SENTINEL})

    assert not legacy.exists()
    assert store.load().credential == {"refresh_token": SENTINEL}
    # And the credential is in the Keychain, not in a file.
    assert files_holding(tmp_path, SENTINEL) == []


def test_a_keychain_write_that_failed_does_not_take_the_older_copy(monkeypatch, tmp_path):
    """Order, not just outcome. Sweeping first would destroy the only copy of a
    credential whose new home never accepted it."""
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    fake = FakeSecurity()

    def refuses_to_write(argv, *, input=None, **_kwargs):
        if argv[1] == "-i":
            return subprocess.CompletedProcess(argv, 1, b"", b"security: no")
        return fake(argv, input=input, **_kwargs)

    monkeypatch.setattr(keychain_store.subprocess, "run", refuses_to_write)
    store = KeychainConnectionStore("mac_testmachine", superseded=legacy)

    with pytest.raises(KeychainUnavailable):
        store.update(reference=store.load().reference, credential={"refresh_token": "second"})

    assert files_holding(tmp_path, SENTINEL) == [legacy]


def test_the_read_back_gate_is_not_satisfied_by_the_file_it_is_about_to_delete(
    monkeypatch, tmp_path
):
    """A write that silently stored nothing must still be caught.

    If the read-back fell through to the older file, a Keychain that accepted
    the command and kept nothing would look like a successful write — and the
    sweep would then delete the only remaining copy.
    """
    legacy = tmp_path / "connection.json"
    fake = FakeSecurity()

    def swallows_the_write(argv, *, input=None, **_kwargs):
        if argv[1] == "-i":
            # Accepted, stored nothing: the exact shape Jeff 220726 measured.
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return fake(argv, input=input, **_kwargs)

    monkeypatch.setattr(keychain_store.subprocess, "run", swallows_the_write)
    store = KeychainConnectionStore("mac_testmachine", superseded=legacy)
    # The older file holds EXACTLY the bytes being written — re-saving the
    # connection that is already here. A read-back that fell through to the
    # file would find them, call the swallowed write good, and then the sweep
    # would delete the only copy that exists. Anything else in the file makes
    # the gate pass for the wrong reason and proves nothing.
    body = store_module._encode(
        store_module.Connection(
            reference="r1", request_ref="r", provider="google",
            credential={"refresh_token": SENTINEL},
        )
    )
    legacy.write_bytes(body)

    with pytest.raises(KeychainUnavailable) as failed:
        store._put(body)

    assert "did not store what was written" in str(failed.value)
    # And the sweep did not run, so the only copy is still here.
    assert files_holding(tmp_path, SENTINEL) == [legacy]


def test_an_unreadable_older_copy_is_not_reported_as_not_connected(monkeypatch, tmp_path):
    """Same rule as the Keychain leg: cannot tell is not empty."""
    legacy = tmp_path / "connection.json"
    legacy.mkdir()  # reading it raises, rather than saying "absent"
    store = keychain_over(monkeypatch, FakeSecurity(), legacy)

    with pytest.raises(OSError):
        store.load()


# ---------------------------------------------------------------------------
# And it does not wait for a write to happen.
#
# Falling back to the older file keeps the connection working, but on its own
# it leaves the credential in plaintext on disk for as long as nothing writes
# — and nothing has to, because a refresh is reactive (Boris 221279).


def test_reading_the_older_copy_moves_it_into_the_keychain(monkeypatch, tmp_path):
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    fake = FakeSecurity()
    store = keychain_over(monkeypatch, fake, legacy)

    store.load()

    assert files_holding(tmp_path, SENTINEL) == []
    assert fake.items[("Puffo Agent-connector", "mac_testmachine")]


def test_the_move_keeps_the_same_connection(monkeypatch, tmp_path):
    """Not a re-mint. The page is already holding this reference."""
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    before = SkeletonConnectionStore(legacy).load()
    fake = FakeSecurity()
    store = keychain_over(monkeypatch, fake, legacy)

    after = store.load()

    assert (after.reference, after.request_ref, after.provider) == (
        before.reference, before.request_ref, before.provider
    )
    assert after.credential == before.credential
    # Read out of the Keychain item itself, not off the return value: without
    # that, this cell passes just as well when no move happened at all, since
    # the fallback answers with the same record either way.
    carried = json.loads(fake.items[("Puffo Agent-connector", "mac_testmachine")])
    assert carried["reference"] == before.reference
    assert carried["credential"] == before.credential


def test_a_move_that_fails_still_answers_with_the_connection(monkeypatch, tmp_path):
    """A failed migration must not cost a computer a connection it had."""
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    fake = FakeSecurity()

    def refuses_to_write(argv, *, input=None, **_kwargs):
        if argv[1] == "-i":
            return subprocess.CompletedProcess(argv, 1, b"", b"security: no")
        return fake(argv, input=input, **_kwargs)

    monkeypatch.setattr(keychain_store.subprocess, "run", refuses_to_write)
    store = KeychainConnectionStore("mac_testmachine", superseded=legacy)

    loaded = store.load()

    assert loaded.credential == {"refresh_token": SENTINEL}
    # The file is still the only copy, so it had better still be there.
    assert files_holding(tmp_path, SENTINEL) == [legacy]


def test_a_record_that_is_not_one_is_not_copied_into_the_keychain(monkeypatch, tmp_path):
    """Fail closed on it; do not carry it over first."""
    legacy = tmp_path / "connection.json"
    legacy.write_bytes(b'{"reference":null,"request_ref":"r","provider":"p","credential":{}}')
    fake = FakeSecurity()
    store = keychain_over(monkeypatch, fake, legacy)

    with pytest.raises(ValueError):
        store.load()

    assert fake.items == {}
    assert legacy.exists()


# ---------------------------------------------------------------------------
# What a failure part way through actually leaves behind.
#
# Jeff 221284, by fault injection: the old docstring claimed a partial clear
# left the connection unusable. It does not. These cells fix what is true
# instead, so the next person reads the behaviour rather than the wish.


def test_a_clear_takes_the_file_even_when_the_keychain_delete_fails(monkeypatch, tmp_path):
    """Stopping at the first error would leave a copy we could have deleted."""
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    fake = FakeSecurity()

    def cannot_delete(argv, *, input=None, **_kwargs):
        if argv[1] == "delete-generic-password":
            return subprocess.CompletedProcess(argv, 1, b"", b"security: no")
        return fake(argv, input=input, **_kwargs)

    monkeypatch.setattr(keychain_store.subprocess, "run", cannot_delete)
    store = KeychainConnectionStore("mac_testmachine", superseded=legacy)

    with pytest.raises(KeychainUnavailable):
        store.clear()

    # The leg that could run, ran.
    assert files_holding(tmp_path, SENTINEL) == []


def test_a_clear_that_could_not_finish_does_not_claim_it_did(monkeypatch, tmp_path):
    """And the connection may well still be here — said out loud, not wished away."""
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    fake = FakeSecurity()

    def cannot_unlink(self, missing_ok=False):
        raise OSError("read-only file system")

    store = keychain_over(monkeypatch, fake, legacy)
    monkeypatch.setattr(Path, "unlink", cannot_unlink)

    with pytest.raises(OSError):
        store.clear()

    # Not "unusable": the file survived, so the connection is still readable.
    assert store.load().request_ref == "req-old"


def test_a_write_whose_sweep_failed_is_still_committed(monkeypatch, tmp_path):
    """The exception says the older copy stayed, not that the write missed."""
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    fake = FakeSecurity()
    store = keychain_over(monkeypatch, fake, legacy)

    def cannot_unlink(self, missing_ok=False):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "unlink", cannot_unlink)
    # The migration writes and verifies but cannot remove the file, so the two
    # copies are byte-identical with the file still there — which is the state
    # the update below then makes differ.
    reference = store.load().reference

    with pytest.raises(SupersededCopyRemains):
        store.update(reference=reference, credential={"refresh_token": "second"})

    # Committed anyway: this is what the caller has to know. Read out of the
    # Keychain rather than through ``load``, because the un-swept file now
    # holds a second credential for this connection and ``load`` refuses to
    # pick between them — the cost of failing closed, pinned in its own cell.
    item = json.loads(fake.items[("Puffo Agent-connector", "mac_testmachine")])
    assert item["credential"] == {"refresh_token": "second"}
    with pytest.raises(ConflictingCredentials):
        store.load()


# ---------------------------------------------------------------------------
# What the move must not do (Boris 221326, both found by reading the code).


def test_a_move_never_overwrites_something_that_arrived_first(monkeypatch, tmp_path):
    """The silent one: an older file value landing on top of a newer credential.

    The read-back would pass — it reads back what it just wrote — the sweep
    would delete the file, and if the provider rotated the token the surviving
    credential is already dead. Nothing raises.
    """
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    fake = FakeSecurity()
    key = ("Puffo Agent-connector", "mac_testmachine")
    # A refresh, which is the scenario Boris described: the same connection
    # with a fresher credential. The reference is the file's own, because a
    # refresh keeps it — an injected value for some *other* connection is a
    # different property and has its own cell below.
    newer = json.dumps(
        {
            "reference": SkeletonConnectionStore(legacy).load().reference,
            "request_ref": "req-old",
            "provider": "google",
            "credential": {"refresh_token": "NEWER"},
        }
    ).encode()

    lookups = []

    def a_refresh_lands_in_between(argv, *, input=None, **_kwargs):
        # Empty on the first lookup, committed by the time the move re-checks.
        if argv[1] == "find-generic-password":
            lookups.append(1)
            if len(lookups) == 2:
                fake.items[key] = newer
        return fake(argv, input=input, **_kwargs)

    monkeypatch.setattr(keychain_store.subprocess, "run", a_refresh_lands_in_between)
    store = KeychainConnectionStore("mac_testmachine", superseded=legacy)

    # Refused rather than answered — but the property this cell is about is
    # that the older file value never lands on top of the newer Keychain one,
    # and that is what the assertions below check.
    with pytest.raises(ConflictingCredentials):
        store.load()

    assert fake.items[key] == newer
    # Neither copy deleted: which of the two the provider still honours is not
    # knowable from here, so neither is thrown away.
    assert files_holding(tmp_path, SENTINEL) == [legacy]


def test_a_refused_move_is_not_retried_on_every_read(monkeypatch, tmp_path):
    """Each retry is another ``security`` call, and any prompt it raises lands
    on the operator's screen where this process cannot see it (220858)."""
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    fake = FakeSecurity()
    writes = []

    def refuses_to_write(argv, *, input=None, **_kwargs):
        if argv[1] == "-i":
            writes.append(1)
            return subprocess.CompletedProcess(argv, 1, b"", b"security: no")
        return fake(argv, input=input, **_kwargs)

    monkeypatch.setattr(keychain_store.subprocess, "run", refuses_to_write)
    store = KeychainConnectionStore("mac_testmachine", superseded=legacy)

    for _ in range(5):
        assert store.load().credential == {"refresh_token": SENTINEL}

    assert len(writes) == 1


def test_a_new_process_tries_the_move_again(monkeypatch, tmp_path):
    """Giving up for the process is not giving up forever — positive control,
    so "it never retries" cannot pass by the move being broken outright."""
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    fake = FakeSecurity()
    store = keychain_over(monkeypatch, fake, legacy)
    keychain_store._MOVE_ATTEMPTED.add(("Puffo Agent-connector", "mac_testmachine"))

    assert store.load().credential == {"refresh_token": SENTINEL}
    assert files_holding(tmp_path, SENTINEL) == [legacy]  # skipped, as asked

    keychain_store._MOVE_ATTEMPTED.clear()  # what a restart amounts to here

    assert store.load().credential == {"refresh_token": SENTINEL}
    assert files_holding(tmp_path, SENTINEL) == []


def test_a_sweep_that_failed_during_the_move_converges_on_a_later_read(monkeypatch, tmp_path):
    """The one a restart does not fix (Jeff 221335/221343, 測試姬 221338).

    Once the Keychain holds the value, every later read short-circuits on it.
    If the move's unlink had failed, the plaintext would sit there for good —
    the entry outlives the process, so restarting lands in the same branch.
    """
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    fake = FakeSecurity()
    store = keychain_over(monkeypatch, fake, legacy)

    def cannot_unlink(self, missing_ok=False):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "unlink", cannot_unlink)
    # The move writes and verifies, but cannot remove the file.
    assert store.load().credential == {"refresh_token": SENTINEL}
    assert files_holding(tmp_path, SENTINEL) == [legacy]

    monkeypatch.undo()  # the permission problem goes away
    monkeypatch.setattr(keychain_store.subprocess, "run", fake)
    # A plain read — no write, no clear — and this is a fresh store object, so
    # it stands in for the process restart too.
    reread = KeychainConnectionStore("mac_testmachine", superseded=legacy)

    assert reread.load().credential == {"refresh_token": SENTINEL}
    assert files_holding(tmp_path, SENTINEL) == []


def test_a_read_that_converges_does_not_need_the_keychain_twice(monkeypatch, tmp_path):
    """The sweep on the short-circuit path is filesystem only.

    It runs on every read, so if it cost a ``security`` call it would be a
    Keychain round trip — and a possible prompt — per read, which is the thing
    the once-per-process guard exists to prevent.
    """
    legacy = tmp_path / "connection.json"
    # Byte-identical copies — the only shape that gets swept on a read.
    one_record = b'{"reference":"a","request_ref":"r","provider":"p","credential":{"t":1}}'
    legacy.write_bytes(one_record)
    fake = FakeSecurity()
    fake.items[("Puffo Agent-connector", "mac_testmachine")] = one_record
    store = keychain_over(monkeypatch, fake, legacy)

    store.load()

    assert fake.verbs() == ["find-generic-password"]
    assert not legacy.exists()


# ---------------------------------------------------------------------------
# One gate in front of the sweep and the answer.
#
# Both places can hold a record, and both read paths used to take "the Keychain
# has a value" for "the Keychain has the connection" — then delete the file and
# return the value on the strength of it. Jeff 221356 and 測試姬 221362 took
# that apart from two sides: validate before deleting, and do not silently
# answer with a value you could not establish is the current one.


def a_keychain_holding(fake: FakeSecurity, body: bytes) -> None:
    fake.items[("Puffo Agent-connector", "mac_testmachine")] = body


@pytest.mark.parametrize("value", [b"null", b"{}", b"[]"], ids=["null", "object", "array"])
def test_a_keychain_value_that_is_not_a_connection_does_not_take_the_file(
    monkeypatch, tmp_path, value
):
    """Delete-then-validate, in the order that cost the only readable copy.

    All three parse, so the Keychain leg says yes to them; ``load`` refuses
    them a moment later, but the sweep had already run and the credential the
    file was holding was gone (Jeff 221356, all three reproduced).
    """
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    (tmp_path / "connection.partial").write_text(f'{{"refresh_token": "{SENTINEL}"}}')
    fake = FakeSecurity()
    a_keychain_holding(fake, value)
    store = keychain_over(monkeypatch, fake, legacy)

    with pytest.raises(ValueError):
        store.load()

    # Refused, and the copy that is actually a connection is still here.
    assert sorted(files_holding(tmp_path, SENTINEL)) == sorted(
        [legacy, tmp_path / "connection.partial"]
    )
    assert SkeletonConnectionStore(legacy).load().credential == {"refresh_token": SENTINEL}


@pytest.mark.parametrize("value", [b"null", b"{}", b"[]"], ids=["null", "object", "array"])
def test_a_value_that_is_not_a_connection_does_not_take_the_file_during_a_move_either(
    monkeypatch, tmp_path, value
):
    """The second entry point, which had the same bug for the same reason.

    ``_migrate`` re-reads the Keychain before writing, and whatever it finds
    there went down the same "it has a value, so sweep" path (測試姬 221362:
    both places, one gate).
    """
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    fake = FakeSecurity()
    lookups = []

    def something_lands_before_the_recheck(argv, *, input=None, **_kwargs):
        if argv[1] == "find-generic-password":
            lookups.append(1)
            if len(lookups) == 2:
                a_keychain_holding(fake, value)
        return fake(argv, input=input, **_kwargs)

    monkeypatch.setattr(keychain_store.subprocess, "run", something_lands_before_the_recheck)
    store = KeychainConnectionStore("mac_testmachine", superseded=legacy)

    with pytest.raises(ValueError):
        store.load()

    assert files_holding(tmp_path, SENTINEL) == [legacy]


def test_a_file_naming_another_connection_is_neither_deleted_nor_talked_over(
    monkeypatch, tmp_path
):
    """The rollback path, which is a real one (Boris 221354, Jeff 221356).

    Turn this store off, the file store says not connected, the user
    reconnects into the file, turn it back on. The Keychain's record may be the
    one the provider revoked. Deleting the file destroys the live credential;
    answering with the Keychain's value hands out the dead one. Their
    references are random hex, so nothing here can order them.
    """
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    fake = FakeSecurity()
    a_keychain_holding(
        fake,
        b'{"reference":"someone-else","request_ref":"q","provider":"google",'
        b'"credential":{"refresh_token":"STALE"}}',
    )
    store = keychain_over(monkeypatch, fake, legacy)

    with pytest.raises(ConflictingConnections):
        store.load()

    assert files_holding(tmp_path, SENTINEL) == [legacy]
    assert fake.items[("Puffo Agent-connector", "mac_testmachine")].endswith(b'"STALE"}}')


def test_another_connection_arriving_during_a_move_is_refused_rather_than_answered(
    monkeypatch, tmp_path
):
    """Same conflict, reached through ``_migrate``'s re-read.

    The re-read exists so a value that arrived first is not written over. That
    is still true here — but "not written over" is not "hand it to the caller":
    a record for a different connection is no more orderable at this entry
    point than at the other one.
    """
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    fake = FakeSecurity()
    other = (
        b'{"reference":"someone-else","request_ref":"q","provider":"google",'
        b'"credential":{"refresh_token":"OTHER"}}'
    )
    lookups = []

    def another_connection_lands_in_between(argv, *, input=None, **_kwargs):
        if argv[1] == "find-generic-password":
            lookups.append(1)
            if len(lookups) == 2:
                a_keychain_holding(fake, other)
        return fake(argv, input=input, **_kwargs)

    monkeypatch.setattr(keychain_store.subprocess, "run", another_connection_lands_in_between)
    store = KeychainConnectionStore("mac_testmachine", superseded=legacy)

    with pytest.raises(ConflictingConnections):
        store.load()

    assert files_holding(tmp_path, SENTINEL) == [legacy]
    assert fake.items[("Puffo Agent-connector", "mac_testmachine")] == other


def test_a_file_that_cannot_be_read_is_not_assumed_to_agree(monkeypatch, tmp_path):
    """Cannot tell is not absent — and here, not "nothing that could disagree".

    The cost is deliberate and worth writing down: a file this store cannot
    read takes down a load the Keychain could have answered on its own.
    """
    legacy = tmp_path / "connection.json"
    legacy.mkdir()  # reading it raises, rather than saying "absent"
    fake = FakeSecurity()
    a_keychain_holding(
        fake, b'{"reference":"r","request_ref":"q","provider":"p","credential":{"t":1}}'
    )
    store = keychain_over(monkeypatch, fake, legacy)

    with pytest.raises(OSError):
        store.load()

    assert legacy.exists()


def test_a_leftover_is_still_swept_when_there_is_no_record_to_compare_it_to(
    monkeypatch, tmp_path
):
    """The hole the reference guard would otherwise open.

    A clear that removed the record but not the ``.partial`` leaves a
    credential in the clear with nothing left to compare against. Nothing ever
    reads a ``.partial``, so it cannot be the connection this computer holds —
    it is residue, and the sweep has to converge on it too.
    """
    legacy = tmp_path / "connection.json"
    (tmp_path / "connection.partial").write_text(f'{{"refresh_token": "{SENTINEL}"}}')
    fake = FakeSecurity()
    a_keychain_holding(
        fake, b'{"reference":"r","request_ref":"q","provider":"p","credential":{"t":1}}'
    )
    store = keychain_over(monkeypatch, fake, legacy)

    assert store.load().reference == "r"

    assert files_holding(tmp_path, SENTINEL) == []


def test_a_move_whose_only_failure_was_the_sweep_does_not_say_it_will_not_retry(
    monkeypatch, tmp_path, caplog
):
    """The log line Jeff 221356 caught contradicting the code under it.

    A write that landed and was read back is done; there is nothing to retry,
    and the next read sweeps the file. It shared a branch — and therefore a
    message — with a write that never landed, which needs the opposite said
    about it.
    """
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    fake = FakeSecurity()
    store = keychain_over(monkeypatch, fake, legacy)

    def cannot_unlink(self, missing_ok=False):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "unlink", cannot_unlink)
    with caplog.at_level(logging.WARNING, logger=keychain_store.__name__):
        assert store.load().credential == {"refresh_token": SENTINEL}

    said = caplog.text
    assert "now in the Keychain" in said
    assert "restarts" not in said
    # And the write really did land, which is what makes the message true.
    assert fake.items[("Puffo Agent-connector", "mac_testmachine")]


def test_a_move_the_keychain_refused_does_say_it_will_not_retry(monkeypatch, tmp_path, caplog):
    """Positive control for the cell above: the other branch still says it."""
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    fake = FakeSecurity()

    def refuses_to_write(argv, *, input=None, **_kwargs):
        if argv[1] == "-i":
            return subprocess.CompletedProcess(argv, 1, b"", b"security: no")
        return fake(argv, input=input, **_kwargs)

    monkeypatch.setattr(keychain_store.subprocess, "run", refuses_to_write)
    store = KeychainConnectionStore("mac_testmachine", superseded=legacy)

    with caplog.at_level(logging.WARNING, logger=keychain_store.__name__):
        assert store.load().credential == {"refresh_token": SENTINEL}

    assert "will not be retried until this process restarts" in caplog.text
    assert "now in the Keychain" not in caplog.text


def test_a_write_that_landed_but_could_not_sweep_says_so_by_its_type(monkeypatch, tmp_path):
    """The caller of an explicit save needs the two apart as well.

    ``_put`` raising used to leave "did the write land" to be read out of a
    message. The committed case has its own type now, and it is a subclass of
    ``OSError`` so nothing upstream had to learn about it.
    """
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    fake = FakeSecurity()
    store = keychain_over(monkeypatch, fake, legacy)

    def cannot_unlink(self, missing_ok=False):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "unlink", cannot_unlink)
    reference = store.load().reference

    with pytest.raises(SupersededCopyRemains):
        store.update(reference=reference, credential={"refresh_token": "second"})


def test_a_write_the_keychain_refused_is_not_that_type(monkeypatch, tmp_path):
    """Positive control: the type has to be capable of not being raised."""
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    fake = FakeSecurity()

    def refuses_to_write(argv, *, input=None, **_kwargs):
        if argv[1] == "-i":
            return subprocess.CompletedProcess(argv, 1, b"", b"security: no")
        return fake(argv, input=input, **_kwargs)

    monkeypatch.setattr(keychain_store.subprocess, "run", refuses_to_write)
    store = KeychainConnectionStore("mac_testmachine", superseded=legacy)

    with pytest.raises(KeychainUnavailable) as refused:
        store.update(reference=store.load().reference, credential={"refresh_token": "second"})

    assert not isinstance(refused.value, SupersededCopyRemains)


def test_a_disconnect_still_clears_both_copies_while_a_conflict_is_present(
    monkeypatch, tmp_path
):
    """The way out of a fail-closed state, pinned rather than assumed.

    A conflict makes every read raise, so if the disconnect path ever grew a
    ``load`` in front of its ``clear`` — to log which connection was being
    given up, say — the only way out of the state would be deleting files by
    hand. It has no such read today, and now that failing closed is what gets
    a user into the state, that is worth a cell rather than an inspection
    (Boris 221371 asked; nothing was holding it).
    """
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    (tmp_path / "connection.partial").write_text(f'{{"refresh_token": "{SENTINEL}"}}')
    fake = FakeSecurity()
    a_keychain_holding(
        fake,
        b'{"reference":"someone-else","request_ref":"q","provider":"google",'
        b'"credential":{"refresh_token":"STALE"}}',
    )
    store = keychain_over(monkeypatch, fake, legacy)
    # The state really is the wedged one, or the disconnect below proves nothing.
    with pytest.raises(ConflictingConnections):
        store.load()

    asyncio.run(disconnect(store))

    assert files_holding(tmp_path, SENTINEL) == []
    assert fake.items == {}


def test_one_connection_with_two_credentials_is_refused_and_nothing_is_deleted(
    monkeypatch, tmp_path, caplog
):
    """Both legs need a proposition this daemon cannot evaluate.

    Jeff 221374 measured the version that deleted: the Keychain holding an
    older credential and the file a newer one under one reference, and the
    newer one gone. Deleting needs "the file holds nothing the Keychain does
    not"; answering needs "this credential still works". Matching references
    give neither, and a daemon that treats the credential as opaque cannot get
    either (測試姬 221378, 221381; Jeff 221379 for why answering is not the
    safe half it looks like).
    """
    legacy = tmp_path / "connection.json"
    legacy.write_bytes(
        b'{"reference":"r","request_ref":"q","provider":"google",'
        b'"credential":{"refresh_token":"NEWER-IN-THE-FILE"}}'
    )
    fake = FakeSecurity()
    a_keychain_holding(
        fake,
        b'{"reference":"r","request_ref":"q","provider":"google",'
        b'"credential":{"refresh_token":"OLDER-IN-THE-KEYCHAIN"}}',
    )
    store = keychain_over(monkeypatch, fake, legacy)

    with caplog.at_level(logging.ERROR, logger=keychain_store.__name__):
        with pytest.raises(ConflictingCredentials):
            store.load()

    # Neither deleted: either could be the one the provider still honours.
    assert files_holding(tmp_path, "NEWER-IN-THE-FILE") == [legacy]
    assert fake.items[("Puffo Agent-connector", "mac_testmachine")]
    # And not silent, because two credentials are readable on this disk.
    assert "different credentials for connection r" in caplog.text
    assert "disconnect" in caplog.text


def test_a_conflict_is_loud_enough_to_tell_apart_from_a_flaky_read(
    monkeypatch, tmp_path, caplog
):
    """A wedged computer must not look like "try again".

    The claim maps a conflict to the same ``read_local`` stage as any other
    unreadable store, so from the page it is indistinguishable from a
    transient failure — and a disconnect, which is the way out, only happens
    if someone knows to do it (測試姬 221378). The references are minted here
    and are not credentials, so naming them is what makes the state
    diagnosable at all.
    """
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    fake = FakeSecurity()
    a_keychain_holding(
        fake,
        b'{"reference":"someone-else","request_ref":"q","provider":"google",'
        b'"credential":{"refresh_token":"STALE"}}',
    )
    store = keychain_over(monkeypatch, fake, legacy)

    with caplog.at_level(logging.ERROR, logger=keychain_store.__name__):
        with pytest.raises(ConflictingConnections):
            store.load()

    said = caplog.text
    assert "two records naming different connections" in said
    assert "someone-else" in said
    assert SkeletonConnectionStore(legacy).load().reference in said
    # The way out is named, since nothing else on this path will name it.
    assert "disconnect" in said
    # And the credential itself never reaches the log.
    assert SENTINEL not in said


def test_a_failed_cleanup_after_a_refresh_costs_the_connection_until_a_disconnect(
    monkeypatch, tmp_path
):
    """The price of failing closed, written down rather than discovered.

    A write whose Keychain half landed and whose sweep did not leaves one
    connection with two credentials — and from the next read on this computer
    refuses to load at all. A cleanup failure becomes an outage, and the way
    out is a disconnect. Nobody in the review raised this; it is the cost of
    the trade and it belongs in real-machine acceptance, so it gets a cell
    rather than a sentence.
    """
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    fake = FakeSecurity()
    store = keychain_over(monkeypatch, fake, legacy)

    def cannot_unlink(self, missing_ok=False):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "unlink", cannot_unlink)
    # A migration whose sweep failed: two byte-identical copies, which load
    # still handles. The refresh on top of it is what makes them differ.
    reference = store.load().reference
    with pytest.raises(SupersededCopyRemains):
        store.update(reference=reference, credential={"refresh_token": "fresh"})
    monkeypatch.undo()  # the permission problem goes away
    monkeypatch.setattr(keychain_store.subprocess, "run", fake)

    # It does not converge, and it does not answer.
    with pytest.raises(ConflictingCredentials):
        store.load()

    # A disconnect is the way out, and it works: both copies go.
    asyncio.run(disconnect(store))
    assert store.load() is None
    assert not legacy.exists()


def test_the_two_disagreements_are_different_types(monkeypatch, tmp_path):
    """Positive control for the pair, and the thing that makes them usable.

    The claim flattens both into one ``read_local`` stage, so a stable type is
    what an operator has to tell them apart by (Jeff 221379). One base so a
    caller can catch "the copies disagree" without knowing which.
    """
    assert issubclass(ConflictingConnections, keychain_store.LocalCopiesDisagree)
    assert issubclass(ConflictingCredentials, keychain_store.LocalCopiesDisagree)
    assert not issubclass(ConflictingCredentials, ConflictingConnections)
    assert not issubclass(ConflictingConnections, ConflictingCredentials)


def a_store_whose_copies_disagree(monkeypatch, tmp_path, *, same_connection: bool):
    """Two local copies that cannot be reconciled, one way or the other."""
    legacy = a_computer_that_connected_before_the_switch(tmp_path)
    reference = SkeletonConnectionStore(legacy).load().reference
    fake = FakeSecurity()
    a_keychain_holding(
        fake,
        json.dumps(
            {
                "reference": reference if same_connection else "someone-else",
                "request_ref": "q",
                "provider": "google",
                "credential": {"refresh_token": "OTHER"},
            }
        ).encode(),
    )
    return keychain_over(monkeypatch, fake, legacy)


@pytest.mark.parametrize(
    "same_connection, expected",
    [(True, "ConflictingCredentials"), (False, "ConflictingConnections")],
    ids=["two-credentials", "two-connections"],
)
@pytest.mark.asyncio
async def test_the_claim_carries_which_disagreement_it_was(
    monkeypatch, tmp_path, same_connection, expected
):
    """The type name is the only distinguisher that reaches the caller.

    Every local-read failure lands on one stage, so the stage says "unreadable"
    for a flaky read and for a computer nothing but a disconnect will unstick.
    Jeff 221379 set "a stable, distinguishable error type" as the bar and
    221387 measured that it holds here — but nothing in this suite was holding
    it, so a later tidy-up of that message would take the distinction away and
    stay green.
    """
    store = a_store_whose_copies_disagree(monkeypatch, tmp_path, same_connection=same_connection)

    async def must_not_be_called(request_ref):
        raise AssertionError("nothing is claimable while the copies disagree")

    answer = await claim_connection("req-new", fetch=must_not_be_called, store=store)

    assert answer["ok"] is False
    assert answer["stage"] == STAGE_READ_LOCAL
    assert expected in answer["reason"]
    # And the credential does not ride out on the reason string.
    assert SENTINEL not in answer["reason"]
