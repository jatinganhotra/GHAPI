from __future__ import annotations

import datetime as dt
import re

from ghapi_crawler.config import Settings
from ghapi_crawler.github_client import GitHubClient


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
        github_graphql_enabled=True,
        github_graphql_batch_size=2,
    )
    return Settings(**{**base.__dict__, **overrides})


def test_batch_get_pull_request_metadata_chunks_and_maps(monkeypatch):
    client = GitHubClient(_settings(github_graphql_batch_size=2))
    calls = []

    def _fake_execute_graphql(query: str, variables=None):
        calls.append(query)
        ids = re.findall(r'node\(id:\s*"([^"]+)"\)', query)
        data = {}
        for idx, node_id in enumerate(ids):
            data[f"n{idx}"] = {
                "__typename": "PullRequest",
                "id": node_id,
                "title": f"PR {node_id}",
                "body": "body",
                "state": "OPEN",
                "isDraft": False,
                "createdAt": "2026-01-01T00:00:00Z",
                "updatedAt": "2026-01-01T00:00:00Z",
                "closedAt": None,
                "mergedAt": None,
                "additions": 1,
                "deletions": 1,
                "changedFiles": 1,
                "commits": {"totalCount": 1},
                "comments": {"totalCount": 0},
                "author": {"login": "bot"},
            }
        return {"data": data}

    monkeypatch.setattr(client, "execute_graphql", _fake_execute_graphql)

    out = client.batch_get_pull_request_metadata(["N1", "N2", "N3"])
    assert len(calls) == 2  # batch size=2 -> two requests
    assert set(out.keys()) == {"N1", "N2", "N3"}
    assert out["N2"]["title"] == "PR N2"


def test_batch_get_pull_request_metadata_disabled():
    client = GitHubClient(_settings(github_graphql_enabled=False))
    out = client.batch_get_pull_request_metadata(["N1", "N2"])
    assert out == {}
