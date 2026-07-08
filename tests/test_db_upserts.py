"""Integration tests for the FK-safe upsert helpers in ghapi_crawler.db /
ghapi_crawler.repository_metrics.

These cover the dual-unique-constraint paths on `repositories` plus the
issue/PR FK requirement, which the production crawler hit repeatedly during
April 2026 and which had no regression coverage.

Each test spins up an ephemeral local Postgres via Postgres.app or PG_BIN_DIR.
"""

from __future__ import annotations

import datetime as dt
import os
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
import pytest
from psycopg.rows import dict_row

from ghapi_crawler.db import (
    _ensure_repository,
    claim_pull_requests_for_hydration,
    ensure_tracked_agents,
    mark_pull_request_hydrated,
    upsert_issue,
    upsert_pull_request_from_search_item,
)
from ghapi_crawler.agents import AgentDefinition
from ghapi_crawler.hydration import _set_pull_request_agent_key
from ghapi_crawler.repository_metrics import upsert_repository_metadata


def _find_pg_bin() -> Path | None:
    from_env = os.getenv("PG_BIN_DIR")
    if from_env:
        path = Path(from_env)
        if path.exists():
            return path
    default = Path("/Applications/Postgres.app/Contents/Versions/latest/bin")
    if default.exists():
        return default
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def _wait_ready(pg_isready: Path, port: int, timeout_seconds: int = 20) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        result = _run(
            [str(pg_isready), "-h", "127.0.0.1", "-p", str(port)], check=False
        )
        if result.returncode == 0:
            return
        time.sleep(0.25)
    raise AssertionError("Postgres did not become ready in time")


@contextmanager
def _ephemeral_postgres() -> Iterator[str]:
    bin_dir = _find_pg_bin()
    if bin_dir is None:
        pytest.skip("Postgres binaries not found (set PG_BIN_DIR)")

    initdb = bin_dir / "initdb"
    pg_ctl = bin_dir / "pg_ctl"
    pg_isready = bin_dir / "pg_isready"
    createdb = bin_dir / "createdb"
    for required in (initdb, pg_ctl, pg_isready, createdb):
        if not required.exists():
            pytest.skip(f"Missing Postgres binary: {required}")

    with tempfile.TemporaryDirectory(prefix="ghapi-tests-") as tmp:
        tmp_path = Path(tmp)
        data_dir = tmp_path / "pgdata"
        log_file = tmp_path / "postgres.log"
        port = _free_port()

        _run([str(initdb), "-D", str(data_dir), "-A", "trust", "-U", "postgres"])
        _run(
            [
                str(pg_ctl), "-D", str(data_dir), "-l", str(log_file),
                "-o", f"-p {port}", "start",
            ]
        )
        try:
            _wait_ready(pg_isready=pg_isready, port=port)
            db_name = "ghapi_tests"
            _run(
                [
                    str(createdb), "-h", "127.0.0.1", "-p", str(port),
                    "-U", "postgres", db_name,
                ]
            )
            dsn = f"postgresql://postgres@127.0.0.1:{port}/{db_name}"
            _apply_migrations(dsn)
            yield dsn
        finally:
            _run([str(pg_ctl), "-D", str(data_dir), "stop", "-m", "fast"], check=False)


def _apply_migrations(dsn: str) -> None:
    migrations_dir = Path(__file__).resolve().parents[1] / "ghapi_crawler" / "migrations"
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        for path in sorted(migrations_dir.glob("*.sql")):
            cur.execute(path.read_text())
        conn.commit()


@contextmanager
def _conn(dsn: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        yield conn


def _seed_agent(conn: psycopg.Connection, key: str = "codex") -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tracked_agents (agent_key, display_name, discovery_query)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (key, "Test Agent", "is:pr"),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# _ensure_repository
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_ensure_repository_inserts_row_and_uses_repo_full_name_for_owner() -> None:
    """Owner/repo are derived from repo_full_name, not parsed from api_url —
    so a mismatched api_url (rename window) does not corrupt owner_login."""
    with _ephemeral_postgres() as dsn, _conn(dsn) as conn:
        canonical = _ensure_repository(
            conn,
            repo_full_name="oldowner/widgets",
            api_url="https://api.github.com/repos/newowner/widgets",
        )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT repo_full_name, owner_login, repo_name, api_url, html_url "
                "FROM repositories"
            )
            rows = cur.fetchall()

        assert canonical == "oldowner/widgets"
        assert len(rows) == 1
        row = rows[0]
        # owner_login must come from repo_full_name, not from api_url.
        assert row["owner_login"] == "oldowner"
        assert row["repo_name"] == "widgets"
        assert row["html_url"] == "https://github.com/oldowner/widgets"


@pytest.mark.integration
def test_ensure_repository_returns_canonical_name_when_api_url_collides() -> None:
    """When the api_url already belongs to a different repo_full_name (e.g. a
    repo seen first under its new name), the helper returns the existing
    canonical name so downstream FK writes target a real row."""
    with _ephemeral_postgres() as dsn, _conn(dsn) as conn:
        # Seed the canonical row.
        _ensure_repository(
            conn,
            repo_full_name="newowner/widgets",
            api_url="https://api.github.com/repos/newowner/widgets",
        )
        conn.commit()

        # Now a discovery path tries to insert under the old name with the
        # same api_url.  ON CONFLICT DO NOTHING swallows the api_url collision;
        # the SELECT-by-api_url finds the canonical row.
        canonical = _ensure_repository(
            conn,
            repo_full_name="oldowner/widgets",
            api_url="https://api.github.com/repos/newowner/widgets",
        )
        conn.commit()

        assert canonical == "newowner/widgets"

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM repositories")
            assert cur.fetchone()["c"] == 1


@pytest.mark.integration
def test_ensure_repository_rejects_invalid_repo_full_name() -> None:
    with _ephemeral_postgres() as dsn, _conn(dsn) as conn:
        with pytest.raises(ValueError):
            _ensure_repository(conn, "no-slash", "https://api.github.com/repos/x/y")
        with pytest.raises(ValueError):
            _ensure_repository(conn, "/missing-owner", "https://api.github.com/repos/x/y")


# ---------------------------------------------------------------------------
# upsert_issue — pre-UPDATE-then-INSERT pattern
# ---------------------------------------------------------------------------


def _make_issue(issue_id: int, number: int) -> dict:
    return {
        "id": issue_id,
        "number": number,
        "state": "open",
        "title": "Test issue",
        "body": "hello",
        "user": {"login": "alice"},
        "comments": 0,
        "created_at": "2026-04-01T00:00:00Z",
        "updated_at": "2026-04-01T00:00:00Z",
        "closed_at": None,
        "repository_url": "https://api.github.com/repos/owner/repo",
    }


@pytest.mark.integration
def test_upsert_issue_inserts_then_updates_in_place() -> None:
    with _ephemeral_postgres() as dsn, _conn(dsn) as conn:
        first = upsert_issue(conn, "owner/repo", _make_issue(500, 7), body_word_count=1)
        conn.commit()
        second_payload = _make_issue(500, 7)
        second_payload["title"] = "Updated title"
        second_payload["state"] = "closed"
        second = upsert_issue(conn, "owner/repo", second_payload, body_word_count=1)
        conn.commit()

        assert first == 500 == second
        with conn.cursor() as cur:
            cur.execute("SELECT title, state FROM issues WHERE github_issue_id = 500")
            row = cur.fetchone()
        assert row["title"] == "Updated title"
        assert row["state"] == "closed"


@pytest.mark.integration
def test_upsert_issue_returns_existing_id_when_repo_number_already_present() -> None:
    """If a row already exists under (repo_full_name, issue_number) with a
    different github_issue_id (rename edge case), the pre-UPDATE pattern hits
    it first and returns the existing id without raising on the composite
    UNIQUE constraint."""
    with _ephemeral_postgres() as dsn, _conn(dsn) as conn:
        # Seed an existing issue row directly so we control its id.
        _ensure_repository(
            conn, "owner/repo", "https://api.github.com/repos/owner/repo"
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO issues (github_issue_id, repo_full_name, issue_number,
                                    state, title)
                VALUES (1001, 'owner/repo', 42, 'open', 'old')
                """
            )
        conn.commit()

        payload = _make_issue(2002, 42)  # different id, same (repo, number)
        payload["title"] = "new title"
        result = upsert_issue(conn, "owner/repo", payload, body_word_count=1)
        conn.commit()

        # We get the EXISTING id back, not the GitHub-current one.
        assert result == 1001
        with conn.cursor() as cur:
            cur.execute(
                "SELECT title FROM issues WHERE github_issue_id = 1001"
            )
            assert cur.fetchone()["title"] == "new title"
            cur.execute("SELECT COUNT(*) AS c FROM issues")
            assert cur.fetchone()["c"] == 1


@pytest.mark.integration
def test_upsert_issue_ensures_repo_exists_for_undiscovered_repo() -> None:
    """upsert_issue is called with a repo_full_name parsed from PR body links —
    that repo may never have been through discovery.  The helper must create
    the row before the FK-constrained INSERT runs."""
    with _ephemeral_postgres() as dsn, _conn(dsn) as conn:
        # Note: no prior _ensure_repository or upsert_pull_request call.
        upsert_issue(conn, "nobody/heard-of-this", _make_issue(777, 1), body_word_count=1)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT repo_full_name FROM repositories "
                "WHERE repo_full_name = 'nobody/heard-of-this'"
            )
            assert cur.fetchone() is not None


# ---------------------------------------------------------------------------
# upsert_repository_metadata — refactored to ensure-then-update
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_upsert_repository_metadata_creates_then_updates_without_touching_api_url() -> None:
    with _ephemeral_postgres() as dsn, _conn(dsn) as conn:
        repo_payload = {
            "owner": {"login": "owner"},
            "name": "repo",
            "url": "https://api.github.com/repos/owner/repo",
            "html_url": "https://github.com/owner/repo",
            "stargazers_count": 100,
            "forks_count": 5,
            "default_branch": "main",
        }
        upsert_repository_metadata(
            conn, "owner/repo", repo_payload,
            file_count=42, blob_size_bytes=1024, tree_truncated=False, loc_estimate=500,
        )
        conn.commit()

        # Mutate api_url in the response to simulate a rename window — the
        # function must NOT overwrite the existing api_url (would risk
        # UniqueViolation if another row owns it) and must NOT raise.
        repo_payload["url"] = "https://api.github.com/repos/newowner/repo"
        repo_payload["stargazers_count"] = 250
        upsert_repository_metadata(
            conn, "owner/repo", repo_payload,
            file_count=43, blob_size_bytes=2048, tree_truncated=False, loc_estimate=600,
        )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT api_url, stargazers_count, file_count "
                "FROM repositories WHERE repo_full_name = 'owner/repo'"
            )
            row = cur.fetchone()
        assert row["api_url"] == "https://api.github.com/repos/owner/repo"
        assert row["stargazers_count"] == 250
        assert row["file_count"] == 43


@pytest.mark.integration
def test_upsert_repository_metadata_does_not_raise_when_api_url_owned_by_other_row() -> None:
    """The original bug: INSERT ... ON CONFLICT (repo_full_name) raised
    UniqueViolation on the api_url constraint when another row already owned
    it.  After the refactor, _ensure_repository returns the canonical row's
    name (the one that already owns the api_url) and the UPDATE lands on that
    row.  Crucially, no exception is raised and api_url is never overwritten."""
    with _ephemeral_postgres() as dsn, _conn(dsn) as conn:
        # Seed the canonical row that owns the contested api_url.
        _ensure_repository(
            conn, "newowner/repo", "https://api.github.com/repos/newowner/repo"
        )
        # Also seed a row under the old name, with its own api_url.
        _ensure_repository(
            conn, "oldowner/repo", "https://api.github.com/repos/oldowner/repo"
        )
        conn.commit()

        # Metadata fetched for "oldowner/repo" now returns the post-rename URL
        # owned by "newowner/repo".  Pre-refactor this raised UniqueViolation.
        repo_payload = {
            "owner": {"login": "newowner"},
            "name": "repo",
            "url": "https://api.github.com/repos/newowner/repo",
            "stargazers_count": 7,
        }
        upsert_repository_metadata(
            conn, "oldowner/repo", repo_payload,
            file_count=None, blob_size_bytes=None, tree_truncated=None, loc_estimate=None,
        )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT repo_full_name, api_url, stargazers_count "
                "FROM repositories ORDER BY repo_full_name"
            )
            rows = cur.fetchall()

        by_name = {row["repo_full_name"]: row for row in rows}
        # Update lands on the canonical row (the one owning the api_url).
        assert by_name["newowner/repo"]["stargazers_count"] == 7
        # The old-name row is untouched and keeps its original api_url.
        assert by_name["oldowner/repo"]["api_url"] == (
            "https://api.github.com/repos/oldowner/repo"
        )
        assert by_name["oldowner/repo"]["stargazers_count"] is None


# ---------------------------------------------------------------------------
# End-to-end: PR insert + linked issue insert (FK chain)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_pr_then_linked_issue_in_undiscovered_repo() -> None:
    """The full scenario that triggered the FK fix series: a PR is discovered
    in repo A, hydration extracts a linked issue in repo B (never discovered),
    upsert_issue must create the repo B row before inserting the issue."""
    with _ephemeral_postgres() as dsn, _conn(dsn) as conn:
        _seed_agent(conn, "codex")
        agent = AgentDefinition(
            key="codex",
            display_name="OpenAI Codex",
            discovery_query="is:pr",
        )
        item = {
            "id": 12345,
            "number": 1,
            "draft": False,
            "title": "Some PR",
            "html_url": "https://github.com/repoA/proj/pull/1",
            "url": "https://api.github.com/repos/repoA/proj/issues/1",
            "node_id": "PR_node",
            "state": "open",
            "user": {"login": "alice"},
            "created_at": "2026-04-01T00:00:00Z",
            "updated_at": "2026-04-01T00:00:00Z",
            "closed_at": None,
            "repository_url": "https://api.github.com/repos/repoA/proj",
            "pull_request": {
                "url": "https://api.github.com/repos/repoA/proj/pulls/1",
                "diff_url": "https://github.com/repoA/proj/pull/1.diff",
                "patch_url": "https://github.com/repoA/proj/pull/1.patch",
                "merged_at": None,
            },
        }
        upsert_pull_request_from_search_item(conn, agent, item)
        conn.commit()

        # Linked issue in a repo we've never seen — must not raise FK violation.
        linked_issue = _make_issue(99999, 7)
        linked_issue["repository_url"] = "https://api.github.com/repos/repoB/lib"
        upsert_issue(conn, "repoB/lib", linked_issue, body_word_count=1)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT repo_full_name FROM repositories ORDER BY repo_full_name"
            )
            names = [row["repo_full_name"] for row in cur.fetchall()]
        assert names == ["repoA/proj", "repoB/lib"]


# ---------------------------------------------------------------------------
# crawl-expansion-202607: new agent keys + hydration-time reclassification
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_ensure_tracked_agents_registers_all_configured_agent_keys() -> None:
    """Every AgentDefinition in ghapi_crawler.agents.AGENTS — including the
    crawl-expansion-202607 additions (cosine, tembo, openhands) that have no
    live discovery matrix entry yet — must get a tracked_agents row from the
    same ensure_tracked_agents() call that seeds codex/claude. Hydration-time
    reclassification (_set_pull_request_agent_key) depends on the row
    existing first, since pull_requests.agent_key REFERENCES
    tracked_agents(agent_key)."""
    with _ephemeral_postgres() as dsn, _conn(dsn) as conn:
        ensure_tracked_agents(conn)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT agent_key FROM tracked_agents")
            keys = {row["agent_key"] for row in cur.fetchall()}

        for expected in (
            "codex",
            "claude",
            "claude_head",
            "cosine",
            "tembo",
            "jules",
            "openhands",
        ):
            assert expected in keys, f"missing tracked_agents row for {expected!r}"


@pytest.mark.integration
def test_hydration_reclassification_to_new_agent_key_satisfies_fk() -> None:
    """A PR discovered under one agent's query must be reclassifiable at
    hydration time to a crawl-expansion-202607 agent key (openhands here)
    without a FK violation — this is exactly what
    hydration._infer_agent_key_from_pr_commits + _set_pull_request_agent_key
    do together when a commit-level signal overrides the discovery-time
    agent_key. Before ensure_tracked_agents() registers 'openhands', this
    update would fail with a foreign key violation."""
    with _ephemeral_postgres() as dsn, _conn(dsn) as conn:
        ensure_tracked_agents(conn)
        conn.commit()

        agent = AgentDefinition(
            key="codex", display_name="OpenAI Codex", discovery_query="is:pr head:codex/"
        )
        item = {
            "id": 42,
            "number": 1,
            "draft": False,
            "title": "Some PR",
            "html_url": "https://github.com/o/r/pull/1",
            "url": "https://api.github.com/repos/o/r/issues/1",
            "node_id": "PR_x",
            "state": "open",
            "user": {"login": "someone"},
            "created_at": "2026-07-01T00:00:00Z",
            "updated_at": "2026-07-01T00:00:00Z",
            "closed_at": None,
            "repository_url": "https://api.github.com/repos/o/r",
            "pull_request": {
                "url": "https://api.github.com/repos/o/r/pulls/1",
                "diff_url": "https://github.com/o/r/pull/1.diff",
                "patch_url": "https://github.com/o/r/pull/1.patch",
                "merged_at": None,
            },
        }
        upsert_pull_request_from_search_item(conn, agent, item)
        conn.commit()

        changed = _set_pull_request_agent_key(conn, github_pr_id=42, agent_key="openhands")
        conn.commit()
        assert changed is True

        with conn.cursor() as cur:
            cur.execute("SELECT agent_key FROM pull_requests WHERE github_pr_id = 42")
            assert cur.fetchone()["agent_key"] == "openhands"


# ---------------------------------------------------------------------------
# claim_pull_requests_for_hydration — agent_cap (crawl-resume-202607)
# ---------------------------------------------------------------------------


def _make_search_item(pr_id: int) -> dict:
    return {
        "id": pr_id,
        "number": pr_id,
        "draft": False,
        "title": "Some PR",
        "html_url": f"https://github.com/o/r/pull/{pr_id}",
        "url": f"https://api.github.com/repos/o/r/issues/{pr_id}",
        "node_id": f"PR_{pr_id}",
        "state": "open",
        "user": {"login": "someone"},
        "created_at": "2026-07-01T00:00:00Z",
        "updated_at": "2026-07-01T00:00:00Z",
        "closed_at": None,
        "repository_url": "https://api.github.com/repos/o/r",
        "pull_request": {
            "url": f"https://api.github.com/repos/o/r/pulls/{pr_id}",
            "diff_url": f"https://github.com/o/r/pull/{pr_id}.diff",
            "patch_url": f"https://github.com/o/r/pull/{pr_id}.patch",
            "merged_at": None,
        },
    }


@pytest.mark.integration
def test_claim_pull_requests_for_hydration_agent_cap_stops_at_boundary() -> None:
    """agent_cap must stop claiming once the agent's total already-hydrated
    count reaches the cap. Unlike --target (HydrationJob._apply_agent_target),
    which only ramps a shard's --limit down as it approaches the target but
    keeps claiming past it, this is a genuine stop enforced inside
    claim_pull_requests_for_hydration itself via a re-evaluated correlated
    subquery — the mechanism behind the crawl-resume-202607 ~100K Codex/Claude
    campaign cap."""
    with _ephemeral_postgres() as dsn, _conn(dsn) as conn:
        ensure_tracked_agents(conn)
        conn.commit()

        agent = AgentDefinition(
            key="codex", display_name="OpenAI Codex", discovery_query="is:pr head:codex/"
        )
        for pr_id in range(1, 4):
            upsert_pull_request_from_search_item(conn, agent, _make_search_item(pr_id))
        conn.commit()

        # Hydrate PR 1 only: 1 of 3 codex PRs is already hydrated.
        mark_pull_request_hydrated(
            conn, github_pr_id=1, hydrated_at=dt.datetime.now(dt.timezone.utc)
        )
        conn.commit()

        # Cap already met (1 hydrated >= cap of 1): no further claims allowed,
        # even though PRs 2 and 3 are still unhydrated and otherwise claimable.
        claimed = claim_pull_requests_for_hydration(
            conn=conn, limit=10, include_agents=["codex"], agent_cap=1
        )
        assert claimed == []

        # Cap not yet met (1 hydrated < cap of 2): the unhydrated PRs claim normally.
        claimed = claim_pull_requests_for_hydration(
            conn=conn, limit=10, include_agents=["codex"], agent_cap=2
        )
        claimed_ids = {int(row["github_pr_id"]) for row in claimed}
        assert claimed_ids == {2, 3}


@pytest.mark.integration
def test_claim_pull_requests_for_hydration_agent_cap_requires_include_agents() -> None:
    """agent_cap scopes its COUNT(*) by agent_key = ANY(include_agents); without
    include_agents the cap would be meaningless (or would have to scan every
    agent), so claim_pull_requests_for_hydration rejects the combination
    outright instead of silently doing the wrong thing."""
    with _ephemeral_postgres() as dsn, _conn(dsn) as conn:
        ensure_tracked_agents(conn)
        conn.commit()

        with pytest.raises(ValueError):
            claim_pull_requests_for_hydration(conn=conn, limit=10, agent_cap=5)
