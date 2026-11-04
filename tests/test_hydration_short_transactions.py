"""Hydration must not make a GitHub call while a DB write is not committed.

A GitHub call can wait up to an hour for the rate limit. If the transaction
holds a new or updated row in that time, other jobs block on the row. Discovery
then fails with "canceling statement due to statement timeout".
"""

from __future__ import annotations

from typing import Any

import pytest

from ghapi_crawler import hydration
from ghapi_crawler.hydration import HydrationJob

WRITERS = [
    "replace_issue_comments",
    "replace_issue_timeline_events",
    "replace_pull_request_comments",
    "replace_pull_request_files",
    "replace_pull_request_issue_links",
    "replace_pull_request_review_comments",
    "replace_pull_request_reviews",
    "replace_pull_request_timeline_events",
    "upsert_pull_request_detail",
    "upsert_repository_metadata",
    "_set_pull_request_agent_key",
]


class _FakeConn:
    def __init__(self) -> None:
        self.dirty = False
        self.writes: list[str] = []

    def commit(self) -> None:
        self.dirty = False

    def rollback(self) -> None:
        self.dirty = False


class _FakeClient:
    """Every GitHub call fails the test if the transaction has uncommitted writes."""

    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        def call(*args: Any, **kwargs: Any) -> Any:
            assert not self.conn.dirty, f"GitHub call {name} inside an open write transaction"
            self.calls.append(name)
            if name == "get_pull_request":
                return {"body": "Fixes #5", "number": 1}
            if name == "get_issue":
                return {"number": 5, "body": "the bug", "state": "open"}
            if name == "get_repository":
                return {"default_branch": "main"}
            if name in ("get_repository_tree", "get_repository_code_frequency"):
                raise RuntimeError("not needed")
            return []

        return call


@pytest.fixture
def job(monkeypatch):
    conn = _FakeConn()

    def writer(name: str):
        def write(*args: Any, **kwargs: Any) -> Any:
            conn.dirty = True
            conn.writes.append(name)
            return False if name == "_set_pull_request_agent_key" else None

        return write

    for name in WRITERS:
        monkeypatch.setattr(hydration, name, writer(name))

    def upsert_issue(*args: Any, **kwargs: Any) -> int:
        # The real upsert_issue can insert a new repositories row.
        conn.dirty = True
        conn.writes.append("upsert_issue")
        return 99

    monkeypatch.setattr(hydration, "upsert_issue", upsert_issue)

    instance = HydrationJob.__new__(HydrationJob)
    instance.client = _FakeClient(conn)
    return instance, conn


def test_hydrate_one_makes_all_github_calls_before_the_first_write(job) -> None:
    instance, conn = job
    instance._hydrate_one(
        conn=conn,
        github_pr_id=1,
        owner="o",
        repo="r",
        repo_full_name="o/r",
        pr_number=1,
        current_agent_key="codex",
    )
    assert "get_issue" in instance.client.calls
    assert "list_issue_timeline_events" in instance.client.calls
    assert "upsert_issue" in conn.writes
    assert "replace_pull_request_issue_links" in conn.writes


def test_repository_refresh_commits_before_the_pr_calls(job) -> None:
    instance, conn = job
    assert instance._refresh_repository_metadata(
        conn=conn, owner="o", repo="r", repo_full_name="o/r"
    )
    assert conn.writes == ["upsert_repository_metadata"]
    assert not conn.dirty
    # The PR's calls come next, and they must not find an open write.
    instance._hydrate_one(
        conn=conn,
        github_pr_id=1,
        owner="o",
        repo="r",
        repo_full_name="o/r",
        pr_number=1,
        current_agent_key="codex",
    )
