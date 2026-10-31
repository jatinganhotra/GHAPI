from __future__ import annotations

import datetime as dt
import fnmatch
import time

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
        return _DummyResponse(
            {
                "method": method,
                "url": url,
                "params": params or {},
                "json": json or {},
                "timeout": timeout,
            }
        )


class _FakeRedis:
    def __init__(self):
        self.kv: dict[str, tuple[str, float]] = {}
        self.sorted_sets: dict[str, dict[str, float]] = {}

    def ping(self):
        return True

    def setex(self, key: str, ttl: int, value: str):
        self.kv[key] = (value, time.time() + ttl)

    def get(self, key: str):
        entry = self.kv.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at <= time.time():
            del self.kv[key]
            return None
        return value

    def zadd(self, key: str, mapping: dict[str, float]):
        bucket = self.sorted_sets.setdefault(key, {})
        bucket.update(mapping)

    def zcard(self, key: str):
        return len(self.sorted_sets.get(key, {}))

    def zrange(self, key: str, start: int, end: int):
        bucket = self.sorted_sets.get(key, {})
        ordered = sorted(bucket.items(), key=lambda row: (row[1], row[0]))
        if end < 0:
            end = len(ordered) + end
        selected = ordered[start : end + 1]
        return [value for value, _ in selected]

    def zremrangebyrank(self, key: str, start: int, end: int):
        values = self.zrange(key, start, end)
        bucket = self.sorted_sets.get(key, {})
        for value in values:
            bucket.pop(value, None)
        return len(values)

    def zrem(self, key: str, value: str):
        bucket = self.sorted_sets.get(key, {})
        if value in bucket:
            del bucket[value]
            return 1
        return 0

    def delete(self, *keys: str):
        removed = 0
        for key in keys:
            if key in self.kv:
                del self.kv[key]
                removed += 1
            if key in self.sorted_sets:
                del self.sorted_sets[key]
                removed += 1
        return removed

    def scan_iter(self, pattern: str):
        for key in list(self.kv.keys()):
            if fnmatch.fnmatch(key, pattern):
                yield key

    def close(self):
        return None


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
        github_cache_backend="redis",
        github_cache_persistent_max_entries=200000,
        github_cache_persistent_cleanup_interval_seconds=300,
        github_cache_redis_url="redis://localhost:6379/0",
        github_cache_redis_key_prefix="ghapi_crawler:github_cache",
        github_graphql_enabled=False,
        github_graphql_batch_size=30,
    )
    return Settings(**{**base.__dict__, **overrides})


def test_redis_cache_reused_across_clients(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(
        GitHubClient,
        "_ensure_redis_client",
        lambda self: fake,
    )

    first_client = GitHubClient(_settings())
    first_session = _DummySession()
    first_client.session = first_session
    first_payload = first_client.get_issue("acme", "repo", 12)
    assert first_session.calls == 1
    assert first_client.cache_stats()["persistent_writes"] == 1
    first_client.close()

    second_client = GitHubClient(_settings())
    second_session = _DummySession()
    second_client.session = second_session
    second_payload = second_client.get_issue("acme", "repo", 12)
    assert second_payload == first_payload
    assert second_session.calls == 0
    stats = second_client.cache_stats()
    assert stats["persistent_hits"] == 1
    assert stats["hits"] == 1
    second_client.close()


def test_redis_cache_clear_cache_clears_persistent_entries(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(
        GitHubClient,
        "_ensure_redis_client",
        lambda self: fake,
    )

    client = GitHubClient(_settings())
    session = _DummySession()
    client.session = session
    client.get_issue("acme", "repo", 44)
    assert session.calls == 1

    client.clear_cache()

    next_client = GitHubClient(_settings())
    next_session = _DummySession()
    next_client.session = next_session
    next_client.get_issue("acme", "repo", 44)
    assert next_session.calls == 1
    next_client.close()


def test_redis_cache_get_sanitizes_nul_bytes(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(
        GitHubClient,
        "_ensure_redis_client",
        lambda self: fake,
    )

    client = GitHubClient(_settings())
    cache_key = client._cache_key(
        "GET",
        "https://api.github.com/repos/acme/repo/issues/55",
        params=None,
        json_body=None,
        accept_header=None,
    )
    data_key = client._redis_cache_key(cache_key)
    fake.setex(
        data_key,
        client.settings.github_cache_ttl_seconds,
        "{\"body\":\"abc\\u0000def\"}",
    )
    fake.zadd(client._redis_access_key, {data_key: time.time()})

    session = _DummySession()
    client.session = session
    payload = client.get_issue("acme", "repo", 55)

    assert payload["body"] == "abcdef"
    assert session.calls == 0
