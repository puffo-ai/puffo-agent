"""The update check should work when anonymous GitHub API quota is spent."""

import io
import urllib.error
import urllib.request

from puffo_agent.portal.cli import fetch_latest_release_tag


class _Response(io.BytesIO):
    def __init__(self, body: bytes = b"", url: str = "") -> None:
        super().__init__(body)
        self._url = url

    def geturl(self) -> str:
        return self._url


def test_check_update_prefers_release_api(monkeypatch):
    calls = []

    def open_url(req, timeout):
        calls.append(req)
        return _Response(b'{"tag_name": "v2.0.14"}')

    monkeypatch.setattr(urllib.request, "urlopen", open_url)
    assert fetch_latest_release_tag() == "2.0.14"
    assert len(calls) == 1


def test_check_update_uses_release_page_after_api_rate_limit(monkeypatch):
    calls = []

    def open_url(req, timeout):
        calls.append(req)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 403, "rate limited", {}, None)
        return _Response(url="https://github.com/puffo-ai/puffo-agent/releases/tag/v2.0.14")

    monkeypatch.setattr(urllib.request, "urlopen", open_url)
    assert fetch_latest_release_tag() == "2.0.14"
    assert len(calls) == 2
    assert calls[1].get_method() == "HEAD"


def test_check_update_rejects_unexpected_release_redirect(monkeypatch):
    calls = 0

    def open_url(req, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError(req.full_url, 403, "rate limited", {}, None)
        return _Response(url="https://example.com/puffo-ai/puffo-agent/releases/tag/v99")

    monkeypatch.setattr(urllib.request, "urlopen", open_url)
    assert fetch_latest_release_tag() is None
