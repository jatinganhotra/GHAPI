from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

import psycopg
import pytest

pytest.importorskip("duckdb")

from ghapi_crawler.agents import AgentDefinition  # noqa: E402
from ghapi_crawler.db import (  # noqa: E402
    claim_pull_requests_for_hydration,
    upsert_pull_request_from_search_item,
)

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


export_duckdb = _load("export_duckdb")
prune_after_export = _load("prune_after_export")

T0 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)

# PR 1: hydrated, then exported. It must go.
# PR 2: hydrated, exported, then hydrated again. It must stay.
# PR 3: never hydrated. It must stay.
# Issue 501 is linked only to PR 1. It must go.
# Issue 502 is linked to PR 1 and PR 2. It must stay.


def _seed(dsn: str) -> None:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tracked_agents (agent_key, display_name, discovery_query) "
            "VALUES ('codex', 'OpenAI Codex', 'is:pr head:codex/') ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO repositories (repo_full_name, owner_login, repo_name, api_url, html_url) "
            "VALUES ('o/r', 'o', 'r', 'api/o/r', 'html/o/r')"
        )
        for n in (1, 2, 3):
            cur.execute(
                "INSERT INTO pull_requests (github_pr_id, repo_full_name, pr_number, agent_key, "
                "state, title, author_login, html_url, api_url, created_at, updated_at, last_hydrated_at) "
                "VALUES (%s, 'o/r', %s, 'codex', 'closed', 't', 'dev', %s, %s, %s, %s, %s)",
                (n, n, f"html/pr/{n}", f"api/pr/{n}", T0, T0, T0 if n < 3 else None),
            )
            cur.execute(
                "INSERT INTO pull_request_files (github_pr_id, filename) VALUES (%s, 'a.py')", (n,)
            )
        for issue_id, number in ((501, 10), (502, 11)):
            cur.execute(
                "INSERT INTO issues (github_issue_id, repo_full_name, issue_number) VALUES (%s, 'o/r', %s)",
                (issue_id, number),
            )
        for pr_id, issue_id in ((1, 501), (1, 502), (2, 502)):
            cur.execute(
                "INSERT INTO pull_request_issue_links (github_pr_id, github_issue_id, link_type, source) "
                "VALUES (%s, %s, 'closes', 'body')",
                (pr_id, issue_id),
            )
        conn.commit()


def _rehydrate_pr2(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "UPDATE pull_requests SET last_hydrated_at = %s WHERE github_pr_id = 2",
            (T0 + dt.timedelta(hours=1),),
        )
        conn.commit()


def _ids(dsn: str, sql: str) -> set[int]:
    with psycopg.connect(dsn) as conn:
        return {r[0] for r in conn.execute(sql).fetchall()}


def _search_item(pr_id: int) -> dict:
    return {
        "id": pr_id,
        "number": pr_id,
        "title": "t",
        "state": "closed",
        "user": {"login": "dev"},
        "html_url": f"https://github.com/o/r/pull/{pr_id}",
        "url": f"https://api.github.com/repos/o/r/issues/{pr_id}",
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-02T00:00:00Z",
        "repository_url": "https://api.github.com/repos/o/r",
        "pull_request": {"url": f"https://api.github.com/repos/o/r/pulls/{pr_id}"},
    }


@pytest.mark.integration
def test_dry_run_deletes_nothing(temp_postgres, tmp_path):
    _seed(temp_postgres)
    snap = tmp_path / "snap.duckdb"
    export_duckdb.export(snap, temp_postgres)
    _rehydrate_pr2(temp_postgres)

    summary = prune_after_export.prune(snap, temp_postgres, apply=False)

    assert summary["prs_to_delete"] == 1
    assert summary["pull_request_files_to_delete"] == 1
    assert _ids(temp_postgres, "SELECT github_pr_id FROM pull_requests") == {1, 2, 3}
    assert _ids(temp_postgres, "SELECT github_pr_id FROM pruned_pull_requests") == set()


@pytest.mark.integration
def test_apply_deletes_only_unchanged_hydrated_prs(temp_postgres, tmp_path):
    _seed(temp_postgres)
    snap = tmp_path / "snap.duckdb"
    export_duckdb.export(snap, temp_postgres)
    _rehydrate_pr2(temp_postgres)

    summary = prune_after_export.prune(snap, temp_postgres, apply=True)

    assert summary["prs_deleted"] == 1
    assert summary["issues_deleted"] == 1
    assert _ids(temp_postgres, "SELECT github_pr_id FROM pull_requests") == {2, 3}
    assert _ids(temp_postgres, "SELECT github_pr_id FROM pull_request_files") == {2, 3}
    assert _ids(temp_postgres, "SELECT github_issue_id FROM issues") == {502}
    assert _ids(temp_postgres, "SELECT github_pr_id FROM pruned_pull_requests") == {1}
    assert _ids(temp_postgres, "SELECT 1 FROM repositories") == {1}


@pytest.mark.integration
def test_pruned_prs_are_skipped_by_discovery_and_counted_by_the_cap(temp_postgres, tmp_path):
    _seed(temp_postgres)
    snap = tmp_path / "snap.duckdb"
    export_duckdb.export(snap, temp_postgres)
    prune_after_export.prune(snap, temp_postgres, apply=True)  # removes PR 1 and PR 2
    agent = AgentDefinition(key="codex", display_name="OpenAI Codex", discovery_query="is:pr head:codex/")

    with psycopg.connect(temp_postgres) as conn:
        assert upsert_pull_request_from_search_item(conn, agent, _search_item(1)) is False
        assert upsert_pull_request_from_search_item(conn, agent, _search_item(4)) is True
        conn.commit()
    assert _ids(temp_postgres, "SELECT github_pr_id FROM pull_requests") == {3, 4}

    from psycopg.rows import dict_row

    with psycopg.connect(temp_postgres, row_factory=dict_row) as conn:
        # 2 pruned PRs count as hydrated, so a cap of 2 is already met.
        assert claim_pull_requests_for_hydration(conn=conn, limit=10, include_agents=["codex"], agent_cap=2) == []
        claimed = claim_pull_requests_for_hydration(conn=conn, limit=10, include_agents=["codex"], agent_cap=3)
        assert {int(r["github_pr_id"]) for r in claimed} == {3, 4}


@pytest.mark.integration
def test_agent_target_counts_pruned_prs(temp_postgres, tmp_path):
    from types import SimpleNamespace

    from psycopg.rows import dict_row

    from ghapi_crawler.hydration import HydrationJob

    _seed(temp_postgres)
    snap = tmp_path / "snap.duckdb"
    export_duckdb.export(snap, temp_postgres)
    prune_after_export.prune(snap, temp_postgres, apply=True)  # 2 pruned, 0 hydrated left

    job = object.__new__(HydrationJob)  # no GitHub client needed
    job.settings = SimpleNamespace(discovery_end_utc=None, discovery_start_utc=T0)
    with psycopg.connect(temp_postgres, row_factory=dict_row) as conn:
        limit = job._apply_agent_target(
            conn, limit=10, include_agents=["codex"], agent_target=3, shard_index=0, shard_count=1
        )
    assert limit == 1  # target 3 minus the 2 pruned PRs
