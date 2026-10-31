from __future__ import annotations

import datetime as dt

import requests

from ghapi_crawler.config import Settings
from ghapi_crawler.github_client import GitHubClient


class _DummyResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers = {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _DummySession:
    def __init__(self):
        self.calls = 0
        self.headers = {}

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls += 1
        payload = {
            "method": method,
            "url": url,
            "params": params or {},
            "json": json or {},
            "timeout": timeout,
        }
        return _DummyResponse(payload)


class _NullByteSession:
    def __init__(self):
        self.calls = 0
        self.headers = {}

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls += 1
        return _DummyResponse(
            {
                "body": "abc\x00def",
                "nested": {"title": "one\x00two"},
                "items": [{"comment": "left\x00right"}],
            }
        )


class _ErrorResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict,
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = str(payload)
        self.url = "https://api.github.com/search/issues"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} error",
                response=self,
            )

    def json(self):
        return self._payload


class _SequenceSession:
    def __init__(self, responses):
        self.calls = 0
        self.headers = {}
        self._responses = list(responses)

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls += 1
        return self._responses[self.calls - 1]


def _settings(**overrides) -> Settings:
    base = Settings(
        database_url="postgresql://localhost:5432/ghapi_crawler",
        github_token=None,
        github_api_base="https://api.github.com",
        request_timeout_seconds=30,
        search_per_page=100,
        search_delay_seconds=0.0,
        search_max_retries=2,
        discovery_start_utc=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        discovery_end_utc=None,
        discovery_max_window_hours=0,
        discovery_overlap_minutes=60,
        max_shard_depth=20,
        repo_allowlist_patterns=(),
        repo_denylist_patterns=(),
        github_cache_enabled=True,
        github_cache_ttl_seconds=900,
        github_cache_max_entries=5000,
        github_cache_backend="memory",
        github_cache_persistent_max_entries=200000,
        github_cache_persistent_cleanup_interval_seconds=300,
        github_cache_redis_url="redis://localhost:6379/0",
        github_cache_redis_key_prefix="ghapi_crawler:github_cache",
        github_graphql_enabled=False,
        github_graphql_batch_size=30,
    )
    return Settings(**{**base.__dict__, **overrides})


def test_client_cache_hits_for_identical_get_request():
    client = GitHubClient(_settings())
    dummy = _DummySession()
    client.session = dummy

    a = client.get_issue("acme", "repo", 7)
    b = client.get_issue("acme", "repo", 7)

    assert dummy.calls == 1
    assert a == b
    stats = client.cache_stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1


def test_client_cache_respects_ttl(monkeypatch):
    now = [1000.0]

    def _now():
        return now[0]

    monkeypatch.setattr("ghapi_crawler.github_client.time.time", _now)

    client = GitHubClient(_settings(github_cache_ttl_seconds=1))
    dummy = _DummySession()
    client.session = dummy

    client.get_issue("acme", "repo", 8)  # miss
    now[0] = 1000.5
    client.get_issue("acme", "repo", 8)  # hit
    now[0] = 1002.0
    client.get_issue("acme", "repo", 8)  # expired -> miss

    assert dummy.calls == 2
    stats = client.cache_stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 2


def test_client_cache_can_be_disabled():
    client = GitHubClient(_settings(github_cache_enabled=False))
    dummy = _DummySession()
    client.session = dummy

    client.get_issue("acme", "repo", 9)
    client.get_issue("acme", "repo", 9)

    assert dummy.calls == 2
    stats = client.cache_stats()
    assert stats["hits"] == 0


def test_client_strips_nul_bytes_before_caching():
    client = GitHubClient(_settings())
    dummy = _NullByteSession()
    client.session = dummy

    first = client.get_issue("acme", "repo", 10)
    second = client.get_issue("acme", "repo", 10)

    assert dummy.calls == 1
    assert first["body"] == "abcdef"
    assert first["nested"]["title"] == "onetwo"
    assert first["items"][0]["comment"] == "leftright"
    assert second == first


def test_client_handles_secondary_rate_limit_with_retry_after(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(
        "ghapi_crawler.github_client.time.sleep",
        lambda value: sleeps.append(float(value)),
    )

    first = _ErrorResponse(
        status_code=403,
        payload={"message": "You have exceeded a secondary rate limit."},
        headers={"Retry-After": "2"},
    )
    second = _DummyResponse({"total_count": 0, "items": []})
    session = _SequenceSession([first, second])

    client = GitHubClient(_settings(search_delay_seconds=0.0, search_max_retries=2))
    client.session = session
    payload = client.search_pull_requests(
        "is:pr head:codex/",
        dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc),
        page=1,
        per_page=1,
    )

    assert payload["total_count"] == 0
    assert session.calls == 2
    assert len(sleeps) >= 2
    assert sleeps[0] == 2.0
    assert any(value >= 2.1 for value in sleeps[1:])


def test_search_requests_respect_minimum_delay(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(
        "ghapi_crawler.github_client.time.sleep",
        lambda value: sleeps.append(float(value)),
    )

    client = GitHubClient(_settings(search_delay_seconds=0.0))
    dummy = _DummySession()
    client.session = dummy
    client.search_pull_requests(
        "is:pr head:codex/",
        dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc),
        page=1,
        per_page=1,
    )

    assert dummy.calls == 1
    assert any(value >= 2.1 for value in sleeps)
