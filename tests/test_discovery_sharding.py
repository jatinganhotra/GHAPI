import datetime as dt

from ghapi_crawler.config import Settings
from ghapi_crawler.discovery import DiscoveryJob


class FakeDiscoveryJob(DiscoveryJob):
    def __init__(self, settings: Settings, counter):
        super().__init__(settings)
        self._counter = counter

    def _count_query(self, query, start, end):
        return self._counter(start, end)


def _settings() -> Settings:
    return Settings(
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


def test_no_split_for_small_window():
    job = FakeDiscoveryJob(_settings(), counter=lambda _s, _e: 100)
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)
    shards = job._build_shards("is:pr head:codex/", start, end)
    assert len(shards) == 1
    assert shards[0].total_count == 100
    assert not shards[0].truncated


def test_split_when_count_exceeds_search_cap():
    def counter(start, end):
        span_hours = int((end - start).total_seconds() / 3600)
        return 1200 if span_hours >= 12 else 400

    job = FakeDiscoveryJob(_settings(), counter=counter)
    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 1, 3, tzinfo=dt.timezone.utc)
    shards = job._build_shards("is:pr head:codex/", start, end)
    assert len(shards) >= 2
    assert all(shard.total_count <= 1000 for shard in shards)
    assert not any(shard.truncated for shard in shards)
