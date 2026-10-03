"""Guard daily cadence, durable retry identity, and release-channel isolation."""
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from puffo_agent.portal import tool_updates as delivery
from puffo_agent.portal import tool_update_sources as sources
from puffo_agent.portal.control.store import ControlPairing


@pytest.fixture
def pairing():
    return ControlPairing("operator", "root", {}, "https://test.invalid", "host", 1)


@pytest.mark.asyncio
async def test_failed_ack_restart_reuses_envelope_and_success_suppresses_repeat(tmp_path, monkeypatch, pairing):
    """Lost HTTP ACK must not become a second notification after restart."""
    update = sources.Update("codex", "Codex", "1.0.0", "1.1.0", "latest", "npm", "docs")
    check = AsyncMock(return_value=[update])
    build = AsyncMock(return_value={"message_id": "fixed", "ts": 1})
    send = AsyncMock(side_effect=[TimeoutError(), None])
    monkeypatch.setattr(delivery, "load_pairings", lambda: {"operator": pairing})
    monkeypatch.setattr(delivery, "check_updates", check)
    monkeypatch.setattr(delivery, "_build_envelope", build)
    monkeypatch.setattr(delivery, "_deliver", send)
    path = tmp_path / "state.json"
    await delivery.ToolUpdateChecker(path)._tick(100)
    state = json.loads(path.read_text())
    assert next(iter(state["recipients"].values()))["notified"] == {}
    await delivery.ToolUpdateChecker(path)._tick(3700)
    assert send.await_args_list[0] == send.await_args_list[1]
    await delivery.ToolUpdateChecker(path)._tick(7300)
    assert send.await_count == 2
    assert build.await_count == 1
    assert check.await_count == 1
    await delivery.ToolUpdateChecker(path)._tick(86500)
    assert check.await_count == 2
    assert send.await_count == 2


@pytest.mark.asyncio
async def test_offline_check_is_daily_and_newer_release_notifies(tmp_path, monkeypatch, pairing):
    """Network failure cannot spin each hour; newer releases must escape dedup."""
    update = sources.Update("pi", "Pi", "1.0.0", "1.2.0", "latest", "npm", "docs")
    monkeypatch.setattr(delivery, "load_pairings", lambda: {"operator": pairing})
    check = AsyncMock(side_effect=[[], [update], [replace(update, latest="1.3.0")]])
    monkeypatch.setattr(delivery, "check_updates", check)
    monkeypatch.setattr(delivery, "_build_envelope", AsyncMock(return_value={"message_id": "id"}))
    send = AsyncMock()
    monkeypatch.setattr(delivery, "_deliver", send)
    checker = delivery.ToolUpdateChecker(tmp_path / "state.json")
    for now in (100, 3700, 86500, 172900):
        await checker._tick(now)
    assert check.await_count == 3
    assert send.await_count == 2


@pytest.mark.asyncio
async def test_unlink_during_recipient_lookup_prevents_send(tmp_path, monkeypatch, pairing):
    """A recipient fetched before unlink must not authorize a later notification."""
    pairings = {"operator": pairing}
    monkeypatch.setattr(delivery, "load_pairings", lambda: pairings)
    update = sources.Update("pi", "Pi", "1.0.0", "1.2.0", "latest", "npm", "docs")
    monkeypatch.setattr(delivery, "check_updates", AsyncMock(return_value=[update]))

    async def build(*args):
        pairings.clear()
        return {"message_id": "id"}

    monkeypatch.setattr(delivery, "_build_envelope", build)
    send = AsyncMock()
    monkeypatch.setattr(delivery, "_deliver", send)
    await delivery.ToolUpdateChecker(tmp_path / "state.json")._tick(100)
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_daemon_stop_interrupts_idle_wait(tmp_path, monkeypatch):
    """Shutdown must not wait for the one-hour retry timer."""
    checker = delivery.ToolUpdateChecker(tmp_path / "state.json")
    stop = asyncio.Event()

    async def tick(now):
        stop.set()

    monkeypatch.setattr(checker, "_tick", tick)
    await asyncio.wait_for(checker.run_loop(stop), 1)


@pytest.mark.asyncio
async def test_channel_versions_do_not_cross_stable_or_downgrade(tmp_path, monkeypatch):
    """Do not select largest registry version or treat lexical order as numeric."""
    monkeypatch.setattr(sources, "_npm_package", lambda tool, exe: tool.package)
    fetch = AsyncMock(return_value={"latest": "1.9.0", "alpha": "2.0.0-alpha.10", "linux-x64": "99.0.0"})
    monkeypatch.setattr(sources, "_fetch_json", fetch)
    tool = sources.TOOLS[0]
    assert await sources._check(tool, str(tmp_path / "codex"), "1.10.0") is None
    update = await sources._check(tool, str(tmp_path / "codex"), "2.0.0-alpha.9")
    assert update.latest == "2.0.0-alpha.10"
    fetch.return_value = {"latest": "3.0.0-beta.1"}
    with pytest.raises(ValueError):
        await sources._check(tool, str(tmp_path / "codex"), "1.10.0")
    with pytest.raises(ValueError):
        sources._version("something failed at 1.2.3")


@pytest.mark.asyncio
async def test_claude_stable_and_brew_use_their_own_release_channel(tmp_path, monkeypatch):
    """Native stable must not follow latest; brew must use cask availability."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "settings.json").write_text('{"autoUpdatesChannel":"stable"}')
    monkeypatch.setattr(sources, "_npm_package", lambda tool, exe: tool.package)
    fetch = AsyncMock(return_value={"latest": "3.0.0", "stable": "2.1.0"})
    monkeypatch.setattr(sources, "_fetch_json", fetch)
    tool = sources.TOOLS[1]
    update = await sources._check(tool, str(tmp_path / "claude"), "2.0.0")
    assert update.latest == "2.1.0"
    fetch.return_value = {"version": "2.0.0"}
    assert await sources._check(tool, str(tmp_path / "Caskroom/claude-code/2.0.0/claude"), "2.0.0") is None
    assert "cask/claude-code" in fetch.await_args.args[0]


@pytest.mark.asyncio
async def test_missing_tool_does_not_probe_and_bad_tool_does_not_block_others(monkeypatch):
    """Offline/malformed providers are isolated and absent tools do no work."""
    tools = tuple(replace(t, resolve=lambda: None) for t in sources.TOOLS)
    tools = (replace(tools[0], resolve=lambda: "/bad"), replace(tools[1], resolve=lambda: "/good"), *tools[2:])
    monkeypatch.setattr(sources, "TOOLS", tools)
    capture = AsyncMock(side_effect=[ValueError(), "1.0.0"])
    monkeypatch.setattr(sources, "_capture", capture)
    expected = sources.Update("claude", "Claude Code", "1.0.0", "2.0.0", "stable", "npm", "docs")
    monkeypatch.setattr(sources, "_check", AsyncMock(return_value=expected))
    assert await sources.check_updates() == [expected]
    assert capture.await_count == 2


def test_lingtai_reads_launcher_distribution_not_host_or_tui(tmp_path):
    """Wrong Python/TUI metadata must never generate a kernel upgrade alert."""
    binary = tmp_path / "venv/bin/lingtai-agent"
    binary.parent.mkdir(parents=True)
    binary.touch()
    info = tmp_path / "venv/lib/python3.12/site-packages/lingtai-1.0.5.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Name: lingtai\nVersion: 1.0.5\n")
    assert sources._lingtai_version(str(binary)) == "1.0.5"
    (info / "direct_url.json").write_text('{"dir_info":{"editable":true}}')
    with pytest.raises(ValueError):
        sources._lingtai_version(str(binary))


@pytest.mark.asyncio
async def test_lingtai_mirror_disagreement_is_not_an_update(monkeypatch):
    """Different published manifest bytes/hashes cannot establish a release."""
    manifests = [json.dumps({"schema": "lingtai.kernel.release/v1", "kernel_version": "1.0.5",
                            "kernel_tag": "v1.0.5", "artifacts": [{"sha256": h * 64, "filename": "lingtai-1.0.5.tar.gz"}]}).encode()
                 for h in ("a", "b")]
    urls = ["https://github.com/Lingtai-AI/lingtai-kernel/releases/download/v1.0.5/manifest.json",
            "https://gitee.com/huangzesen1997/lingtai-kernel/releases/download/v1.0.5/manifest.json"]
    monkeypatch.setattr(sources, "_fetch_json", AsyncMock(side_effect=[
        {key: [{"name": "lingtai-kernel-release-manifest.json", "browser_download_url": url}]}
        for key, url in zip(("assets", "attach_files"), urls)]))
    monkeypatch.setattr(sources, "_fetch_bytes", AsyncMock(side_effect=manifests))
    with pytest.raises(ValueError, match="disagree"):
        await sources._lingtai_latest()


@pytest.mark.asyncio
async def test_lingtai_public_release_fallback_requires_matching_manifest(monkeypatch):
    """Anonymous API failures may fall back to the published release page."""
    raw = json.dumps({"schema": "lingtai.kernel.release/v1", "kernel_version": "1.0.5",
                      "kernel_tag": "v1.0.5", "artifacts": [
                          {"sha256": "a" * 64, "filename": "lingtai-1.0.5.tar.gz"}]}).encode()
    monkeypatch.setattr(sources, "_fetch_json", AsyncMock(side_effect=ValueError("rate limited")))
    public = AsyncMock(return_value=(raw, "v1.0.5"))
    monkeypatch.setattr(sources, "_lingtai_public_latest", public)
    assert await sources._lingtai_latest() == "1.0.5"
    public.return_value = (raw, "v1.0.6")
    with pytest.raises(ValueError, match="disagree"):
        await sources._lingtai_latest()


@pytest.mark.asyncio
async def test_lingtai_public_release_rejects_unexpected_redirect(monkeypatch):
    """Only the kernel repository's stable tag may select a manifest URL."""
    class Response:
        status = 302
        headers = {"Location": "https://evil.invalid/Lingtai-AI/lingtai-kernel/releases/tag/v9.9.9"}

    @asynccontextmanager
    async def response_context(*_args, **_kwargs):
        yield Response()

    class Session:
        head = staticmethod(response_context)

    @asynccontextmanager
    async def session_context(*_args, **_kwargs):
        yield Session()

    monkeypatch.setattr(sources, "create_remote_http_session", session_context)
    fetch = AsyncMock()
    monkeypatch.setattr(sources, "_fetch_bytes", fetch)
    with pytest.raises(ValueError, match="unexpected kernel release redirect"):
        await sources._lingtai_public_latest()
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_version_probe_reaps_child_and_does_not_forward_credentials(tmp_path, monkeypatch):
    """A wedged CLI probe must not survive daemon shutdown or inherit API keys."""
    script = tmp_path / "sleep.py"
    script.write_text("import time\ntime.sleep(60)\n")
    monkeypatch.setattr(sources, "normalize_launch_argv", lambda _: [sys.executable, str(script)])
    monkeypatch.setenv("OPENAI_API_KEY", "never-forward-this")
    spawned = asyncio.Event()
    original = asyncio.create_subprocess_exec
    processes = []

    async def create(*args, **kwargs):
        if args[0] == sys.executable:
            assert "OPENAI_API_KEY" not in kwargs["env"]
        proc = await original(*args, **kwargs)
        if args[0] == sys.executable:
            processes.append(proc)
            spawned.set()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    task = asyncio.create_task(sources._capture("fixture"))
    await asyncio.wait_for(spawned.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 12)
    assert processes[0].returncode is not None


@pytest.mark.asyncio
async def test_delivery_requires_commit_ack_and_signs_the_exact_body(monkeypatch, pairing):
    """A 202/socket success/invalid response must leave the outbox pending."""
    response = type("Response", (), {"status": 200})()

    async def chunks(size):
        for chunk in (b'{"ok":', b'true}'):
            yield chunk

    response.content = type("Content", (), {"iter_chunked": staticmethod(chunks)})()

    class Context:
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *args):
            pass

    def sign(machine, method, path, body):
        assert method == "POST" and path == "/v2/machines/me/messages"
        assert json.loads(body) == {"operator_slug": "operator", "envelope": {"message_id": "same"}}
        return {"signed": body.decode()}

    def post(url, *, data, headers, allow_redirects):
        assert url == "https://test.invalid/v2/machines/me/messages"
        assert headers["signed"].encode() == data
        assert allow_redirects is False
        return Context(response)

    session = type("Session", (), {"post": staticmethod(post)})()
    monkeypatch.setattr(delivery, "create_remote_http_session", lambda *a, **k: Context(session))
    monkeypatch.setattr(delivery, "load_or_create_machine", lambda: object())
    monkeypatch.setattr(delivery.machine_auth, "signed_headers", sign)
    await delivery._deliver(pairing, {"message_id": "same"})
    response.status = 202
    with pytest.raises(RuntimeError):
        await delivery._deliver(pairing, {"message_id": "same"})


@pytest.mark.asyncio
async def test_pi_legacy_overlap_does_not_suggest_incompatible_latest(tmp_path, monkeypatch):
    """A moving legacy tag must not turn an older Node20 install into latest."""
    monkeypatch.setattr(sources, "_npm_package", lambda tool, exe: tool.package)
    monkeypatch.setattr(sources, "_fetch_json", AsyncMock(return_value={
        "latest": "0.85.1", "legacy-node20": "0.74.2",
    }))
    for current in ("0.74.1", "0.74.2"):
        with pytest.raises(ValueError, match="ambiguous"):
            await sources._check(sources.TOOLS[2], str(tmp_path / "pi"), current)
    update = await sources._check(sources.TOOLS[2], str(tmp_path / "pi"), "0.80.0")
    assert update.latest == "0.85.1"


@pytest.mark.skipif(sys.platform == "win32", reason="macOS app bundle symlink layout")
@pytest.mark.asyncio
async def test_app_bundled_symlink_does_not_use_standalone_releases(tmp_path, monkeypatch):
    """A PATH alias must preserve the application's release authority."""
    binary = tmp_path / "Example.app/Contents/MacOS/codex"
    binary.parent.mkdir(parents=True)
    binary.touch()
    alias = tmp_path / "codex"
    alias.symlink_to(binary)
    fetch = AsyncMock()
    monkeypatch.setattr(sources, "_fetch_json", fetch)
    with pytest.raises(ValueError, match="app-bundled"):
        await sources._check(sources.TOOLS[0], str(alias), "1.0.0")
    fetch.assert_not_awaited()
