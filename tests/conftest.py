from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ghapi_crawler.db import configure_libpq_library_paths

configure_libpq_library_paths()


import pytest

from ghapi_crawler import config as _config


@pytest.fixture(autouse=True)
def _isolate_from_live_database(monkeypatch):
    """Keep every test away from the live database.

    load_settings() prefers INGESTOR_WRITER_URL over DATABASE_URL, and it reads
    .env on first use. A developer .env holds the live Supabase writer URL, so a
    test that sets only DATABASE_URL would still connect to Supabase. This fixture
    stops the .env load and removes both variables. Tests that need Postgres set
    DATABASE_URL to their own throwaway cluster.
    """
    monkeypatch.setattr(_config, "_DOTENV_LOADED", True)
    monkeypatch.delenv("INGESTOR_WRITER_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)


def _find_pg_bin():
    import os
    import shutil

    from_env = os.getenv("PG_BIN_DIR")
    if from_env and Path(from_env).exists():
        return Path(from_env)
    for candidate in ("/Applications/Postgres.app/Contents/Versions/latest/bin",):
        if Path(candidate).exists():
            return Path(candidate)
    found = shutil.which("initdb")
    return Path(found).parent if found else None


@pytest.fixture
def temp_postgres(monkeypatch):
    """Start a throwaway Postgres cluster, apply the migrations, and yield its DSN.

    DATABASE_URL points at the cluster for the length of the test. The cluster
    and its data folder are removed afterwards. Skips when the Postgres
    binaries are not available.
    """
    import socket
    import subprocess
    import tempfile
    import time

    bin_dir = _find_pg_bin()
    if bin_dir is None:
        pytest.skip("Postgres binaries not found (set PG_BIN_DIR)")
    tools = {name: bin_dir / name for name in ("initdb", "pg_ctl", "pg_isready", "createdb")}
    for tool in tools.values():
        if not tool.exists():
            pytest.skip(f"Missing Postgres binary: {tool}")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])

    def run(*cmd):
        subprocess.run([str(c) for c in cmd], check=True, text=True, capture_output=True)

    with tempfile.TemporaryDirectory(prefix="ghapi-pg-") as tmp:
        data_dir = Path(tmp) / "pgdata"
        run(tools["initdb"], "-D", data_dir, "-A", "trust", "-U", "postgres")
        run(tools["pg_ctl"], "-D", data_dir, "-l", Path(tmp) / "pg.log", "-o", f"-p {port}", "start")
        try:
            for _ in range(40):
                ready = subprocess.run(
                    [str(tools["pg_isready"]), "-h", "127.0.0.1", "-p", str(port)],
                    capture_output=True,
                )
                if ready.returncode == 0:
                    break
                time.sleep(0.5)
            else:
                raise AssertionError("Postgres did not become ready in time")
            run(tools["createdb"], "-h", "127.0.0.1", "-p", port, "-U", "postgres", "ghapi_test")
            dsn = f"postgresql://postgres@127.0.0.1:{port}/ghapi_test"
            monkeypatch.setenv("DATABASE_URL", dsn)
            from ghapi_crawler.migrate import run_migrations

            run_migrations()
            yield dsn
        finally:
            subprocess.run(
                [str(tools["pg_ctl"]), "-D", str(data_dir), "-m", "fast", "stop"],
                capture_output=True,
            )
