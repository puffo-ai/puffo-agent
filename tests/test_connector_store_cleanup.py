"""A save that fails after writing must not leave the credential on disk.

The judgement here is deliberately not "the file named ``connection.partial``
is gone": a rename, a move, or a clear written as a truncate would all keep
that assertion green while the credential stayed readable on disk (测试姬
219875). What is checked instead is the property — no plaintext credential
anywhere under this computer's connector directory — and the scan that checks
it carries its own positive control.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from puffo_agent.portal.connector.store import SkeletonConnectionStore

# Unique per save, and plain alphanumerics so json.dumps cannot escape it into
# pieces the scan would miss.
SENTINEL = "SENTINEL7f3a9c21refreshtoken"


def files_holding(directory: Path, needle: str) -> list[Path]:
    """Every readable file under ``directory`` whose bytes contain ``needle``.

    Reads bytes directly rather than shelling out: this machine's ``grep`` is a
    wrapper that injects ``-I`` and can drop a file while reporting "no match".
    """
    found = []
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            if needle.encode() in path.read_bytes():
                found.append(path)
    return found


def save_into(home: Path) -> SkeletonConnectionStore:
    return SkeletonConnectionStore(home / "connection.json")


def fail_the_rename(home: Path) -> None:
    """Make ``os.replace`` fail with the credential already written.

    A directory cannot be renamed over, so the write and the fsync both
    succeed and only the final rename raises. Blocking the temporary file
    instead would fail at ``os.open`` — before any credential byte reaches the
    disk — which is a different case and cannot show cleanup working at all
    (Jeff 219890).
    """
    (home / "connection.json").mkdir()


def test_the_scan_can_see_a_credential_that_is_there(tmp_path):
    """Positive control: a green scan below has to mean something."""
    (tmp_path / "left-behind").write_text(f'{{"refresh_token": "{SENTINEL}"}}')

    assert files_holding(tmp_path, SENTINEL)


def test_a_save_that_fails_after_writing_leaves_no_credential_behind(tmp_path):
    fail_the_rename(tmp_path)
    store = save_into(tmp_path)

    with pytest.raises(OSError):
        store.save(request_ref="req-1", provider="fake", credential={"refresh_token": SENTINEL})

    assert files_holding(tmp_path, SENTINEL) == []


def test_a_saved_credential_lives_in_the_finished_file_and_nowhere_else(tmp_path):
    """The success path keeps the credential — in one place, on purpose.

    "The scan finds nothing" is the wrong reading of a successful save: what
    disappears is the temporary copy, not the credential (Jeff 219895).
    """
    store = save_into(tmp_path)

    store.save(request_ref="req-1", provider="fake", credential={"refresh_token": SENTINEL})

    assert files_holding(tmp_path, SENTINEL) == [tmp_path / "connection.json"]


def test_a_cleanup_that_also_fails_still_reports_why_the_save_failed(tmp_path, monkeypatch, caplog):
    """Both fail: the save error survives and the leftover is not hidden.

    Asserting "no credential remains" here would demand something the code
    cannot deliver — the cleanup really did fail, so the file really is there
    (Jeff 219877). What it owes is an honest report, not a clean disk.
    """
    fail_the_rename(tmp_path)
    store = save_into(tmp_path)

    def cannot_unlink(self, missing_ok=False):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "unlink", cannot_unlink)

    with pytest.raises(IsADirectoryError):  # the save error, not the cleanup's
        store.save(request_ref="req-1", provider="fake", credential={"refresh_token": SENTINEL})

    assert "a credential may remain on disk" in caplog.text
    # And it does — said plainly rather than wished away.
    assert files_holding(tmp_path, SENTINEL) == [tmp_path / "connection.partial"]


def test_clearing_takes_the_leftover_too(tmp_path):
    """A disconnect that left the temporary copy would be a silent breach."""
    store = save_into(tmp_path)
    store.save(request_ref="req-1", provider="fake", credential={"refresh_token": SENTINEL})
    # A leftover from some earlier failed save, of the kind a crash still makes.
    (tmp_path / "connection.partial").write_text(f'{{"refresh_token": "{SENTINEL}"}}')

    store.clear()

    assert files_holding(tmp_path, SENTINEL) == []
