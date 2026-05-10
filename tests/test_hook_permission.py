"""Unit tests for the PreToolUse permission hook.

The hook is stdlib-only; tests mock ``urllib.request`` at the function
level to drive happy/failure paths without a live chat server.
"""

import io
import json
from typing import Callable
from unittest.mock import patch

import pytest

from puffo_agent.hooks import permission as hook


# ── summarise_tool_input (pure) ──────────────────────────────────────────────


class TestSummariseToolInput:
    def test_dict_renders_as_bulleted_list(self):
        out = hook.summarise_tool_input({"command": "ls", "cwd": "/tmp"})
        assert "**command**" in out
        assert "`ls`" in out
        assert "**cwd**" in out

    def test_long_value_truncated(self):
        long = "x" * 500
        out = hook.summarise_tool_input({"cmd": long})
        assert "…" in out
        assert len(out) < 300

    def test_empty_dict_placeholder(self):
        assert hook.summarise_tool_input({}) == "(no input)"

    def test_none_placeholder(self):
        assert hook.summarise_tool_input(None) == "(no input)"

    def test_string_input(self):
        out = hook.summarise_tool_input("rm -rf /")
        assert "rm -rf /" in out


# ── read_current_turn ────────────────────────────────────────────────────────


class TestReadCurrentTurn:
    def test_returns_none_when_missing(self, tmp_path):
        assert hook.read_current_turn(str(tmp_path)) is None

    def test_returns_none_on_malformed_json(self, tmp_path):
        (tmp_path / ".puffo-agent").mkdir()
        (tmp_path / ".puffo-agent" / "current_turn.json").write_text("not-json")
        assert hook.read_current_turn(str(tmp_path)) is None

    def test_returns_none_without_channel_id(self, tmp_path):
        (tmp_path / ".puffo-agent").mkdir()
        (tmp_path / ".puffo-agent" / "current_turn.json").write_text(
            json.dumps({"root_id": "r1"})
        )
        assert hook.read_current_turn(str(tmp_path)) is None

    def test_happy_path(self, tmp_path):
        (tmp_path / ".puffo-agent").mkdir()
        (tmp_path / ".puffo-agent" / "current_turn.json").write_text(
            json.dumps({
                "channel_id": "c1", "root_id": "r1",
                "triggering_post_id": "p1",
            })
        )
        turn = hook.read_current_turn(str(tmp_path))
        assert turn == {
            "channel_id": "c1", "root_id": "r1",
            "triggering_post_id": "p1",
        }


# ── poll_for_reply (mocked urllib) ───────────────────────────────────────────


def _thread_payload(posts: list[dict]) -> dict:
    return {
        "order": [p["id"] for p in posts],
        "posts": {p["id"]: p for p in posts},
    }


class TestPollForReply:
    def _patch_get(self, handler: Callable[[str], dict]):
        def fake_get(url, headers, timeout=10.0):
            return handler(url)
        return patch.object(hook, "_http_get", fake_get)

    def test_allow_on_y_reply(self):
        owner = "owner-id"
        now = 10_000

        def handler(url):
            assert "/api/v4/posts/root-id/thread" in url
            return _thread_payload([
                {"id": "r1", "user_id": owner,
                 "create_at": (now + 1) * 1000, "message": "yes go"},
            ])

        with self._patch_get(handler), patch.object(hook.time, "sleep", lambda s: None):
            decision = hook.poll_for_reply(
                "http://x", {}, "root-id", owner,
                request_ts=now, timeout_seconds=5, sleep_seconds=0.01,
            )
        assert decision is True

    def test_approve_word_counts_as_allow(self):
        owner = "owner-id"
        now = 10_000
        def handler(url):
            return _thread_payload([
                {"id": "r1", "user_id": owner,
                 "create_at": (now + 1) * 1000, "message": "approved"},
            ])
        with self._patch_get(handler), patch.object(hook.time, "sleep", lambda s: None):
            decision = hook.poll_for_reply(
                "http://x", {}, "root-id", owner, now, 5, 0.01,
            )
        assert decision is True

    def test_deny_on_n_reply(self):
        owner = "owner-id"
        now = 10_000
        def handler(url):
            return _thread_payload([
                {"id": "r1", "user_id": owner,
                 "create_at": (now + 1) * 1000, "message": "nope, unsafe"},
            ])
        with self._patch_get(handler), patch.object(hook.time, "sleep", lambda s: None):
            decision = hook.poll_for_reply(
                "http://x", {}, "root-id", owner, now, 5, 0.01,
            )
        assert decision is False

    def test_timeout_returns_none(self):
        owner = "owner-id"
        now = 10_000
        def handler(url):
            return _thread_payload([])
        with self._patch_get(handler), patch.object(hook.time, "sleep", lambda s: None):
            decision = hook.poll_for_reply(
                "http://x", {}, "root-id", owner, now,
                timeout_seconds=0, sleep_seconds=0.01,
            )
        assert decision is None

    def test_ignores_bot_posts(self):
        # Thread root is the bot's own post and must never be
        # interpreted as approval.
        owner = "owner-id"
        bot = "bot-id"
        now = 10_000

        def handler(url):
            return _thread_payload([
                {"id": "root-id", "user_id": bot,
                 "create_at": (now + 1) * 1000,
                 "message": "yeah agent wants Bash"},  # starts with 'y'!
            ])

        with self._patch_get(handler), patch.object(hook.time, "sleep", lambda s: None):
            decision = hook.poll_for_reply(
                "http://x", {}, "root-id", owner, now,
                timeout_seconds=0, sleep_seconds=0.01,
            )
        assert decision is None

    def test_ignores_posts_before_request_ts(self):
        owner = "owner-id"
        now = 10_000
        def handler(url):
            return _thread_payload([
                {"id": "r1", "user_id": owner,
                 "create_at": (now - 10) * 1000,
                 "message": "y (to an older request)"},
            ])
        with self._patch_get(handler), patch.object(hook.time, "sleep", lambda s: None):
            decision = hook.poll_for_reply(
                "http://x", {}, "root-id", owner, now,
                timeout_seconds=0, sleep_seconds=0.01,
            )
        assert decision is None


# ── main() — integration ─────────────────────────────────────────────────────


def _write_current_turn(tmp_path, channel_id="c1", root_id="r1"):
    (tmp_path / ".puffo-agent").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".puffo-agent" / "current_turn.json").write_text(
        json.dumps({
            "channel_id": channel_id,
            "root_id": root_id,
            "triggering_post_id": root_id,
        })
    )


class TestMainFailOpen:
    def test_missing_url_fails_open(self, monkeypatch):
        monkeypatch.delenv("PUFFO_URL", raising=False)
        monkeypatch.setenv("PUFFO_BOT_TOKEN", "tok")
        monkeypatch.setenv("PUFFO_OPERATOR_USERNAME", "han")
        with pytest.raises(SystemExit) as exc:
            hook.main()
        assert exc.value.code == 0

    def test_missing_token_fails_open(self, monkeypatch):
        monkeypatch.setenv("PUFFO_URL", "http://x")
        monkeypatch.delenv("PUFFO_BOT_TOKEN", raising=False)
        monkeypatch.setenv("PUFFO_OPERATOR_USERNAME", "han")
        with pytest.raises(SystemExit) as exc:
            hook.main()
        assert exc.value.code == 0

    def test_missing_operator_fails_open(self, monkeypatch):
        monkeypatch.setenv("PUFFO_URL", "http://x")
        monkeypatch.setenv("PUFFO_BOT_TOKEN", "tok")
        monkeypatch.setenv("PUFFO_OPERATOR_USERNAME", "")
        with pytest.raises(SystemExit) as exc:
            hook.main()
        assert exc.value.code == 0

    def test_no_current_turn_fails_open(self, monkeypatch, tmp_path):
        """Proactive agent work (no current_turn.json) must fail open;
        otherwise every background MCP call would hang on a permission
        request with no thread to reply in."""
        monkeypatch.setenv("PUFFO_URL", "http://x")
        monkeypatch.setenv("PUFFO_BOT_TOKEN", "tok")
        monkeypatch.setenv("PUFFO_OPERATOR_USERNAME", "han")
        stdin = json.dumps({"tool_name": "Bash", "tool_input": {},
                            "cwd": str(tmp_path)})
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        with pytest.raises(SystemExit) as exc:
            hook.main()
        assert exc.value.code == 0


class TestMainDecisionPath:
    """Integration: stub urllib, drive main() through the three
    decision branches. current_turn.json is a real file on tmp_path.
    """

    def _setup_env(self, monkeypatch):
        monkeypatch.setenv("PUFFO_URL", "http://mm")
        monkeypatch.setenv("PUFFO_BOT_TOKEN", "tok")
        monkeypatch.setenv("PUFFO_OPERATOR_USERNAME", "han")
        monkeypatch.setenv("PUFFO_AGENT_ID", "han-local-cli")
        monkeypatch.setenv("PUFFO_PERMISSION_TIMEOUT", "1")

    def _setup_stdin(self, monkeypatch, tmp_path, tool="Bash", tool_input=None):
        stdin = json.dumps({
            "tool_name": tool,
            "tool_input": tool_input or {"command": "ls"},
            "cwd": str(tmp_path),
        })
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))

    def _patch_http(self, get_handler, post_handler):
        return [
            patch.object(hook, "_http_get", lambda u, h, timeout=10.0: get_handler(u)),
            patch.object(hook, "_http_post", lambda u, h, p, timeout=10.0: post_handler(u, p)),
            patch.object(hook.time, "sleep", lambda s: None),
        ]

    def test_allow_path(self, monkeypatch, tmp_path, capsys):
        self._setup_env(monkeypatch)
        self._setup_stdin(monkeypatch, tmp_path)
        _write_current_turn(tmp_path, channel_id="c1", root_id="r1")

        posted_payloads: list[dict] = []

        def get_handler(url):
            if "/users/username/han" in url:
                return {"id": "owner-id"}
            if "/posts/r1/thread" in url:
                return _thread_payload([
                    {"id": "reply", "user_id": "owner-id",
                     "create_at": 9_999_999_999_999, "message": "y"},
                ])
            raise AssertionError(f"unexpected GET {url}")

        def post_handler(url, payload):
            posted_payloads.append(payload)
            if "/posts" in url:
                return {"id": "perm-post-id"}
            raise AssertionError(f"unexpected POST {url}")

        patches = self._patch_http(get_handler, post_handler)
        for p in patches:
            p.start()
        try:
            with pytest.raises(SystemExit) as exc:
                hook.main()
            assert exc.value.code == 0
            out = capsys.readouterr().out
            doc = json.loads(out)
            assert doc["hookSpecificOutput"]["permissionDecision"] == "allow"
            # Permission message must be a threaded reply in the
            # triggering channel, @-mentioning the operator.
            assert len(posted_payloads) == 1
            msg = posted_payloads[0]
            assert msg["channel_id"] == "c1"
            assert msg["root_id"] == "r1"
            assert "@han" in msg["message"]
        finally:
            for p in patches:
                p.stop()

    def test_deny_path(self, monkeypatch, tmp_path):
        self._setup_env(monkeypatch)
        self._setup_stdin(monkeypatch, tmp_path)
        _write_current_turn(tmp_path)

        def get_handler(url):
            if "/users/username/han" in url:
                return {"id": "owner-id"}
            if "/posts/r1/thread" in url:
                return _thread_payload([
                    {"id": "reply", "user_id": "owner-id",
                     "create_at": 9_999_999_999_999, "message": "no thanks"},
                ])
            raise AssertionError(f"unexpected GET {url}")

        def post_handler(url, payload):
            return {"id": "perm-post-id"}

        patches = self._patch_http(get_handler, post_handler)
        for p in patches:
            p.start()
        try:
            with pytest.raises(SystemExit) as exc:
                hook.main()
            assert exc.value.code == 2
        finally:
            for p in patches:
                p.stop()

    def test_post_failure_fails_open(self, monkeypatch, tmp_path):
        self._setup_env(monkeypatch)
        self._setup_stdin(monkeypatch, tmp_path)
        _write_current_turn(tmp_path)

        def get_handler(url):
            if "/users/username/han" in url:
                return {"id": "owner-id"}
            raise AssertionError("should not reach poll")

        def post_handler(url, payload):
            raise RuntimeError("network is out")

        patches = self._patch_http(get_handler, post_handler)
        for p in patches:
            p.start()
        try:
            with pytest.raises(SystemExit) as exc:
                hook.main()
            # Fail-open on post failure: tool proceeds through claude's
            # native flow instead of breaking every call on a transient
            # outage.
            assert exc.value.code == 0
        finally:
            for p in patches:
                p.stop()
