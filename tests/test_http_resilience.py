import io
import urllib.error

from shared.http_resilience import ResilientHTTPClient


class _Response:
    headers = type("Headers", (), {"get_content_charset": lambda self: "utf-8"})()

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


def test_successful_get_is_cached(monkeypatch):
    calls = []
    user_agents = []

    def fake_urlopen(request, timeout):
        calls.append(1)
        user_agents.append(request.get_header("User-agent"))
        return _Response(b"feed")

    monkeypatch.setattr(
        "urllib.request.urlopen",
        fake_urlopen,
    )
    client = ResilientHTTPClient("test", cache_ttl_sec=60, user_agent="TestFeed/2.0")

    assert client.get_text("https://feeds.example/news") == "feed"
    assert client.get_text("https://feeds.example/news") == "feed"
    assert len(calls) == 1
    assert user_agents == ["TestFeed/2.0"]


def test_404_cools_only_the_failing_endpoint(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        if request.full_url.endswith("/gone"):
            raise urllib.error.HTTPError(request.full_url, 404, "not found", {}, io.BytesIO())
        return _Response(b"healthy")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = ResilientHTTPClient("test")

    assert client.get_text("https://feeds.example/gone") is None
    assert client.get_text("https://feeds.example/gone") is None
    assert client.get_text("https://feeds.example/healthy") == "healthy"
    assert calls == ["https://feeds.example/gone", "https://feeds.example/healthy"]


def test_429_honors_retry_after_for_host(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        headers = {"Retry-After": "120"}
        raise urllib.error.HTTPError(request.full_url, 429, "limited", headers, io.BytesIO())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = ResilientHTTPClient("test")

    assert client.get_text("https://api.example/one") is None
    assert client.get_text("https://api.example/two") is None
    assert calls == ["https://api.example/one"]