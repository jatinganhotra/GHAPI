from __future__ import annotations

import datetime as dt
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import psycopg
import pytest

from ghapi_crawler.config import Settings
from ghapi_crawler.github_client import GitHubClient
from ghapi_crawler.migrate import run_migrations


class _DummyResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers = {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _DummySession:
    def __init__(self):
        self.calls = 0
        self.headers = {}

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls += 1
        return _DummyResponse(
            {
                "method": method,
                "url": url,
                "params": params or {},
                "json": json or {},
                "timeout": timeout,
            }
        )


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


def _settings(dsn: str) -> Settings:
    return Settings(
        database_url=dsn,
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
        github_cache_backend="postgres",
        github_cache_persistent_max_entries=1000,
        github_cache_persistent_cleanup_interval_seconds=1,
        github_cache_redis_url="redis://localhost:6379/0",
        github_cache_redis_key_prefix="ghapi_crawler:github_cache",
        github_graphql_enabled=False,
        github_graphql_batch_size=30,
    )


@pytest.mark.integration
def test_postgres_cache_reused_across_client_instances():
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

    with tempfile.TemporaryDirectory(prefix="ghapi_crawler-cache-") as tmp:
        tmp_path = Path(tmp)
        data_dir = tmp_path / "pgdata"
        log_file = tmp_path / "postgres.log"
        port = _free_port()
        db_name = "ghapi_cache_test"
        dsn = f"postgresql://postgres@127.0.0.1:{port}/{db_name}"

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
                    db_name,
                ]
            )

            prior_database_url = os.environ.get("DATABASE_URL")
            os.environ["DATABASE_URL"] = dsn
            try:
                assert run_migrations() >= 1
            finally:
                if prior_database_url is None:
                    del os.environ["DATABASE_URL"]
                else:
                    os.environ["DATABASE_URL"] = prior_database_url

            settings = _settings(dsn)
            client_a = GitHubClient(settings)
            session_a = _DummySession()
            client_a.session = session_a
            first = client_a.get_issue("acme", "repo", 99)
            assert session_a.calls == 1
            assert client_a.cache_stats()["persistent_writes"] == 1
            client_a.close()

            client_b = GitHubClient(settings)
            session_b = _DummySession()
            client_b.session = session_b
            second = client_b.get_issue("acme", "repo", 99)
            assert second == first
            assert session_b.calls == 0
            stats = client_b.cache_stats()
            assert stats["persistent_hits"] == 1
            assert stats["hits"] == 1
            assert stats["misses"] == 0
            client_b.close()

            with psycopg.connect(dsn) as conn, conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM github_api_cache")
                assert cur.fetchone()[0] == 1
        finally:
            _run([str(pg_ctl), "-D", str(data_dir), "stop", "-m", "fast"], check=False)
