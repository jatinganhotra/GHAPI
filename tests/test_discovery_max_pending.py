from __future__ import annotations

import dataclasses
import datetime as dt

import psycopg
import pytest

from ghapi_crawler.agents import AGENTS
from ghapi_crawler.discovery import DiscoveryJob
from tests.test_discovery_sharding import _settings

CODEX = next(a for a in AGENTS if a.key == "codex")


class _ShardsCalled(Exception):
    pass


class _Job(DiscoveryJob):
    def _count_query(self, query, start, end):
        raise _ShardsCalled()


def _seed_unhydrated(dsn: str, count: int) -> None:
    t0 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tracked_agents (agent_key, display_name, discovery_query) "
            "VALUES ('codex', 'OpenAI Codex', 'is:pr head:codex/') ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO repositories (repo_full_name, owner_login, repo_name, api_url, html_url) "
            "VALUES ('o/r', 'o', 'r', 'api/o/r', 'html/o/r')"
        )
        for n in range(1, count + 1):
            cur.execute(
                "INSERT INTO pull_requests (github_pr_id, repo_full_name, pr_number, agent_key, "
                "state, title, author_login, html_url, api_url, created_at, updated_at) "
                "VALUES (%s, 'o/r', %s, 'codex', 'open', 't', 'dev', %s, %s, %s, %s)",
                (n, n, f"html/pr/{n}", f"api/pr/{n}", t0, t0),
            )
        conn.commit()


def _job(dsn: str, max_pending: int) -> _Job:
    settings = dataclasses.replace(
        _settings(),
        database_url=dsn,
        discovery_start_utc=dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
        discovery_max_pending=max_pending,
    )
    return _Job(settings)


@pytest.mark.integration
def test_discovery_skips_agent_at_pending_limit(temp_postgres):
    _seed_unhydrated(temp_postgres, 2)
    summary = _job(temp_postgres, max_pending=2)._run_agent(CODEX)
    assert summary["skipped_pending"] == 2
    assert summary["items_seen"] == 0


@pytest.mark.integration
def test_discovery_runs_below_pending_limit(temp_postgres):
    _seed_unhydrated(temp_postgres, 2)
    with pytest.raises(_ShardsCalled):
        _job(temp_postgres, max_pending=3)._run_agent(CODEX)


@pytest.mark.integration
def test_pending_limit_zero_means_no_limit(temp_postgres):
    _seed_unhydrated(temp_postgres, 2)
    with pytest.raises(_ShardsCalled):
        _job(temp_postgres, max_pending=0)._run_agent(CODEX)
