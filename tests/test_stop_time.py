"""A crawl job stops at GHAPI_STOP_AT and exits with success.

GitHub cancels a job at its timeout, and a cancelled job stops the jobs that
need it. After the stop time, the crawler starts no new PR, page, or rate-limit
wait. A stop is not a hydration error.
"""

from __future__ import annotations

import argparse
import datetime as dt
import time
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from ghapi_crawler import discovery, hydration, stop_time
from ghapi_crawler.config import Settings
from ghapi_crawler.github_client import GitHubClient
from ghapi_crawler.hydration import HydrationJob
from ghapi_crawler.stop_time import STOP_AT_ENV, StopTimeReached


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
        github_cache_enabled=False,
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


# --- stop_time ---------------------------------------------------------------


def test_no_stop_time_means_no_stop(monkeypatch) -> None:
    monkeypatch.delenv(STOP_AT_ENV, raising=False)
    stop_time.check(10**9)


def test_check_raises_after_the_stop_time(monkeypatch) -> None:
    monkeypatch.setenv(STOP_AT_ENV, str(time.time() - 1))
    with pytest.raises(StopTimeReached):
        stop_time.check()


def test_check_raises_when_a_wait_ends_after_the_stop_time(monkeypatch) -> None:
    monkeypatch.setenv(STOP_AT_ENV, str(time.time() + 60))
    stop_time.check(30)
    with pytest.raises(StopTimeReached):
        stop_time.check(120)


def test_an_incorrect_stop_time_stops_the_job_at_the_start(monkeypatch) -> None:
    monkeypatch.setenv(STOP_AT_ENV, "soon")
    with pytest.raises(ValueError):
        stop_time.announce("hydration")


def test_except_exception_does_not_catch_the_stop() -> None:
    assert not issubclass(StopTimeReached, Exception)


# --- GitHub client -------------------------------------------------------------


class _RateLimited:
    status_code = 403
    text = "API rate limit exceeded"
    url = "https://api.github.com/repos/acme/repo/issues/7"

    def __init__(self, reset_epoch: int) -> None:
        self.headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset_epoch)}

    def json(self) -> dict:
        return {"message": self.text}

    def raise_for_status(self) -> None:
        raise requests.HTTPError("403", response=self)


class _Ok:
    status_code = 200
    headers: dict[str, str] = {}

    def json(self) -> dict:
        return {"number": 7}

    def raise_for_status(self) -> None:
        return None


class _Session:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.headers: dict[str, str] = {}

    def request(self, *args: Any, **kwargs: Any) -> Any:
        return self.responses.pop(0)


def test_client_does_not_wait_past_the_stop_time(monkeypatch) -> None:
    now = time.time()
    monkeypatch.setenv(STOP_AT_ENV, str(now + 60))
    monkeypatch.setattr(
        "ghapi_crawler.github_client.time.sleep",
        lambda seconds: pytest.fail(f"the client waited {seconds}s"),
    )
    client = GitHubClient(_settings())
    client.session = _Session([_RateLimited(int(now) + 3600)])
    with pytest.raises(StopTimeReached):
        client.get_issue("acme", "repo", 7)


def test_client_waits_when_the_wait_ends_before_the_stop_time(monkeypatch) -> None:
    now = time.time()
    monkeypatch.setenv(STOP_AT_ENV, str(now + 3600))
    sleeps: list[float] = []
    monkeypatch.setattr("ghapi_crawler.github_client.time.sleep", sleeps.append)
    client = GitHubClient(_settings())
    client.session = _Session([_RateLimited(int(now) + 30), _Ok()])
    assert client.get_issue("acme", "repo", 7) == {"number": 7}
    assert len(sleeps) == 1


# --- hydration -----------------------------------------------------------------


class _Conn:
    def __init__(self) -> None:
        self.rollbacks = 0

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        pass


@pytest.fixture
def hydration_job(monkeypatch):
    conn = _Conn()
    claims: list[int] = []
    hydrated: list[int] = []

    def claim(**kwargs: Any) -> list[dict]:
        pr_id = len(claims) + 1
        claims.append(pr_id)
        return [
            {
                "github_pr_id": pr_id,
                "repo_full_name": "acme/repo",
                "pr_number": pr_id,
                "agent_key": "codex",
                "node_id": None,
            }
        ]

    monkeypatch.setattr(hydration, "claim_pull_requests_for_hydration", claim)
    monkeypatch.setattr(
        hydration,
        "mark_pull_request_hydrated",
        lambda **kwargs: hydrated.append(kwargs["github_pr_id"]),
    )

    job = HydrationJob.__new__(HydrationJob)
    job.settings = SimpleNamespace(github_graphql_enabled=False)
    stats = {"hits": 0, "misses": 0, "evictions": 0, "size": 0}
    job.client = SimpleNamespace(cache_stats=lambda: stats, close=lambda: None)
    monkeypatch.setattr(job, "_hydration_window", lambda: (None, None))
    monkeypatch.setattr(job, "_open_db_connection", lambda: conn)
    monkeypatch.setattr(job, "_refresh_repository_metadata", lambda **kwargs: True)
    monkeypatch.setattr(
        job,
        "_persist_hydration_error",
        lambda **kwargs: pytest.fail(f"a stop was saved as an error: {kwargs['error']}"),
    )
    return job, conn, claims, hydrated


def test_hydration_claims_nothing_after_the_stop_time(monkeypatch, hydration_job) -> None:
    job, _conn, claims, _hydrated = hydration_job
    monkeypatch.setenv(STOP_AT_ENV, str(time.time() - 1))
    monkeypatch.setattr(job, "_hydrate_one", lambda **kwargs: pytest.fail("hydrated a PR"))
    result = job.run(limit=5)
    assert claims == []
    assert result["processed"] == 0


def test_a_stop_during_a_pr_is_not_an_error(monkeypatch, hydration_job) -> None:
    job, conn, claims, hydrated = hydration_job
    monkeypatch.setenv(STOP_AT_ENV, str(time.time() + 3600))

    def hydrate_one(**kwargs: Any) -> None:
        if kwargs["github_pr_id"] == 2:
            raise StopTimeReached("a 3000s rate-limit wait would end after the stop time")

    monkeypatch.setattr(job, "_hydrate_one", hydrate_one)
    result = job.run(limit=5)
    assert claims == [1, 2]
    assert hydrated == [1]
    assert conn.rollbacks == 1
    assert (result["processed"], result["success"], result["failed"]) == (1, 1, 0)


# --- discovery -----------------------------------------------------------------


def test_discovery_exits_normally_at_the_stop_time(monkeypatch) -> None:
    class _Job:
        def __init__(self, settings: Any) -> None:
            pass

        def run(self, **kwargs: Any) -> dict:
            raise StopTimeReached("the stop time is reached")

    monkeypatch.setattr(discovery, "DiscoveryJob", _Job)
    monkeypatch.setattr(discovery, "load_settings", lambda: SimpleNamespace(discovery_end_utc=None))
    monkeypatch.setattr(
        discovery,
        "parse_args",
        lambda: argparse.Namespace(
            until=None,
            agent=["codex"],
            hydration_batch_size=100,
            hydration_shard_index=0,
            hydration_shard_count=1,
        ),
    )
    discovery.main()
