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

import shlex
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
