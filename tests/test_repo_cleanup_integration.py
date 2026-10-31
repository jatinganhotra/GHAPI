from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import psycopg
import pytest

from ghapi_crawler.migrate import run_migrations
from ghapi_crawler.repo_cleanup import apply_repo_cleanup, collect_cleanup_candidates
from ghapi_crawler.repo_filters import RepoFilter


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
            [str(pg_isready), "-h", "127.0.0.1", "-p", str(port)],
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(0.25)
    raise AssertionError("Postgres did not become ready in time")


@pytest.mark.integration
def test_repo_cleanup_candidates_and_apply():
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

    with tempfile.TemporaryDirectory(prefix="ghapi_crawler-cleanup-") as tmp:
        tmp_path = Path(tmp)
        data_dir = tmp_path / "pgdata"
        log_file = tmp_path / "postgres.log"
        port = _free_port()

        _run([str(initdb), "-D", str(data_dir), "-A", "trust", "-U", "postgres"])
        _run(
            [
                str(pg_ctl),
                "-D",
                str(data_dir),
                "-l",
                str(log_file),
                "-o",
                f"-p {port}",
                "start",
            ]
        )

        db_name = "ghapi_cleanup_test"
        dsn = f"postgresql://postgres@127.0.0.1:{port}/{db_name}"
        try:
            _wait_ready(pg_isready=pg_isready, port=port)
            _run(
                [
                    str(createdb),
                    "-h",
                    "127.0.0.1",
                    "-p",
                    str(port),
                    "-U",
                    "postgres",
                    db_name,
                ]
            )

            prior_database_url = os.environ.get("DATABASE_URL")
            os.environ["DATABASE_URL"] = dsn
            try:
                run_migrations()
            finally:
                if prior_database_url is None:
                    del os.environ["DATABASE_URL"]
                else:
                    os.environ["DATABASE_URL"] = prior_database_url

            with psycopg.connect(dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO tracked_agents (agent_key, display_name, discovery_query)
                    VALUES ('codex', 'OpenAI Codex', 'is:pr head:codex/')
                    """
                )
                cur.execute(
                    """
                    INSERT INTO repositories (repo_full_name, owner_login, repo_name, api_url, html_url)
                    VALUES
                        ('good/repo', 'good', 'repo', 'https://api.github.com/repos/good/repo', 'https://github.com/good/repo'),
                        ('bad/repo', 'bad', 'repo', 'https://api.github.com/repos/bad/repo', 'https://github.com/bad/repo')
                    """
                )
                cur.execute(
                    """
                    INSERT INTO pull_requests (
                        github_pr_id, repo_full_name, pr_number, agent_key, html_url, api_url,
                        created_at, updated_at, last_hydrated_at
                    )
                    VALUES
                        (101, 'good/repo', 1, 'codex', 'https://github.com/good/repo/pull/1', 'https://api.github.com/repos/good/repo/issues/1', NOW(), NOW(), NOW()),
                        (102, 'bad/repo', 2, 'codex', 'https://github.com/bad/repo/pull/2', 'https://api.github.com/repos/bad/repo/issues/2', NOW(), NOW(), NOW())
                    """
                )
                cur.execute(
                    """
                    INSERT INTO issues (
                        github_issue_id, repo_full_name, issue_number, state, title, body, author_login
                    )
                    VALUES
                        (201, 'good/repo', 11, 'open', 'good issue', 'hello', 'alice'),
                        (202, 'bad/repo', 22, 'open', 'bad issue', 'hello', 'bob')
                    """
                )
                conn.commit()

            repo_filter = RepoFilter(denylist_patterns=("bad/*",))
            with psycopg.connect(dsn) as conn:
                candidates = collect_cleanup_candidates(conn, repo_filter)
                assert len(candidates) == 1
                assert candidates[0].repo_full_name == "bad/repo"
                assert candidates[0].pr_count == 1
                assert candidates[0].issue_count == 1

                summary = apply_repo_cleanup(conn, candidates)
                conn.commit()
                assert summary["repos_targeted"] == 1
                assert summary["pull_requests_deleted"] == 1
                assert summary["issues_deleted"] == 1
                assert summary["repositories_deleted"] == 1

            with psycopg.connect(dsn) as conn, conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM repositories WHERE repo_full_name='bad/repo'")
                assert cur.fetchone()[0] == 0
                cur.execute("SELECT COUNT(*) FROM pull_requests WHERE repo_full_name='bad/repo'")
                assert cur.fetchone()[0] == 0
                cur.execute("SELECT COUNT(*) FROM issues WHERE repo_full_name='bad/repo'")
                assert cur.fetchone()[0] == 0

                # Allowed repo should remain.
                cur.execute("SELECT COUNT(*) FROM repositories WHERE repo_full_name='good/repo'")
                assert cur.fetchone()[0] == 1
        finally:
            _run([str(pg_ctl), "-D", str(data_dir), "stop", "-m", "fast"], check=False)

