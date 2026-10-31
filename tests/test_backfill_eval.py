import datetime as dt

import pytest

from ghapi_crawler.backfill_eval import (
    _parse_utc_timestamp,
    estimate_from_shards,
    evaluate_backfill_window,
)
from ghapi_crawler.config import Settings
from ghapi_crawler.discovery import Shard


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


def test_estimate_from_shards_counts_pages_and_skipped():
    start = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
    end = dt.datetime(2025, 1, 2, tzinfo=dt.timezone.utc)
    shards = [
        Shard(start=start, end=end, total_count=50, truncated=False),
        Shard(start=start, end=end, total_count=250, truncated=False),
        Shard(start=start, end=end, total_count=1500, truncated=True),
    ]

    out = estimate_from_shards(shards, search_per_page=100)
    assert out["shard_count"] == 3
    assert out["truncated_shard_count"] == 1
    assert out["potential_result_count"] == 1800
    assert out["retrievable_result_count"] == 1300
    assert out["estimated_skipped_count"] == 500
    assert out["estimated_page_requests"] == 14  # 1 + 3 + 10


def test_parse_utc_timestamp_supports_date_and_zulu():
    assert _parse_utc_timestamp("2025-01-01").isoformat() == "2025-01-01T00:00:00+00:00"
    assert (
        _parse_utc_timestamp("2025-01-01T12:34:56Z").isoformat()
        == "2025-01-01T12:34:56+00:00"
    )


def test_evaluate_backfill_window_rejects_invalid_range():
    ts = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
    with pytest.raises(ValueError):
        evaluate_backfill_window(
            settings=_settings(),
            window_start=ts,
            window_end=ts,
        )
