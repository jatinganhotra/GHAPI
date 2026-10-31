from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
import psycopg

from ghapi_crawler.migrate import run_migrations


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
def test_postgres_migrations_apply_cleanly():
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

    with tempfile.TemporaryDirectory(prefix="ghapi_crawler-pg-") as tmp:
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
                    "ghapi_test",
                ]
            )

            dsn = f"postgresql://postgres@127.0.0.1:{port}/ghapi_test"
            prior_database_url = os.environ.get("DATABASE_URL")
            os.environ["DATABASE_URL"] = dsn
            try:
                applied = run_migrations()
                assert applied >= 1
                # Idempotency check.
                assert run_migrations() == 0
            finally:
                if prior_database_url is None:
                    del os.environ["DATABASE_URL"]
                else:
                    os.environ["DATABASE_URL"] = prior_database_url

            with psycopg.connect(dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        to_regclass('public.pull_requests') IS NOT NULL,
                        to_regclass('public.pr_metrics') IS NOT NULL,
                        to_regclass('public.pull_request_files') IS NOT NULL,
                        to_regclass('public.v_agent_overview') IS NOT NULL,
                        to_regclass('public.github_api_cache') IS NOT NULL
                    """
                )
                row = cur.fetchone()
                assert row == (True, True, True, True, True)

                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE column_name = 'confidence') AS has_confidence,
                        COUNT(*) FILTER (WHERE column_name = 'confidence_score') AS has_confidence_score,
                        COUNT(*) FILTER (WHERE column_name = 'explainability') AS has_explainability
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'pull_request_issue_links'
                    """
                )
                link_cols = cur.fetchone()
                assert link_cols == (1, 1, 1)
        finally:
            _run([str(pg_ctl), "-D", str(data_dir), "stop", "-m", "fast"], check=False)
