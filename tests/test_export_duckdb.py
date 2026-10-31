from __future__ import annotations

import importlib.util
from pathlib import Path

import psycopg
import pytest

pytest.importorskip("duckdb")

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_duckdb.py"
_spec = importlib.util.spec_from_file_location("export_duckdb", _SCRIPT)
export_duckdb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export_duckdb)


def _seed(dsn: str) -> None:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tracked_agents (agent_key, display_name, discovery_query) "
            "VALUES ('codex', 'OpenAI Codex', 'is:pr head:codex/') ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO repositories (repo_full_name, owner_login, repo_name, api_url, html_url) "
            "VALUES ('acme/widgets', 'acme', 'widgets', 'api/acme/widgets', 'html/acme/widgets')"
        )
        for n in (1, 2, 3):
            cur.execute(
                "INSERT INTO pull_requests (github_pr_id, repo_full_name, pr_number, agent_key, "
                "state, title, author_login, html_url, api_url, created_at, updated_at) "
                "VALUES (%s, 'acme/widgets', %s, 'codex', 'open', 't', 'dev', %s, %s, NOW(), NOW())",
                (1000 + n, n, f"html/pr/{n}", f"api/pr/{n}"),
            )
        conn.commit()


@pytest.mark.integration
def test_export_verify_passes_when_counts_match(temp_postgres, tmp_path):
    _seed(temp_postgres)
    out = tmp_path / "snap.duckdb"
    counts = export_duckdb.export(out, temp_postgres, verify=True)
    assert counts["pull_requests"] == 3
    assert counts["repositories"] == 1
    assert out.exists()


@pytest.mark.integration
def test_export_verify_fails_when_source_changes(temp_postgres, tmp_path, monkeypatch):
    _seed(temp_postgres)
    real_export = export_duckdb.export

    # Make the source grow after the copy and before the check, as a live
    # crawler would.
    import duckdb

    class _GrowingConnection:
        def __init__(self, con):
            self._con = con
            self._grew = False

        def execute(self, sql, *args):
            if sql.startswith("SELECT COUNT(*) FROM src.") and not self._grew:
                self._grew = True
                with psycopg.connect(temp_postgres) as conn:
                    conn.execute(
                        "INSERT INTO repositories (repo_full_name, owner_login, repo_name, api_url, html_url) "
                        "VALUES ('acme/late', 'acme', 'late', 'api/acme/late', 'html/acme/late')"
                    )
            return self._con.execute(sql, *args)

        def close(self):
            self._con.close()

    real_connect = duckdb.connect
    monkeypatch.setattr(duckdb, "connect", lambda path: _GrowingConnection(real_connect(path)))
    with pytest.raises(export_duckdb.VerifyError, match="repositories"):
        real_export(tmp_path / "snap.duckdb", temp_postgres, verify=True)
