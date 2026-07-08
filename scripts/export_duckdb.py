#!/usr/bin/env python3
"""
Export GHAPI's Postgres crawler tables into a local DuckDB file.

Part of crawl-expansion-202607: "dump to DuckDB periodically; save monthly
snapshots" (see plans/crawl_expansion_20260704.md in the parent repo for the
full design discussion). This script does the "dump" half — a full,
from-scratch export of every analysis-relevant table. Pair with
merge_duckdb.py for cheap incremental refreshes between full exports, and
see .github/workflows/duckdb_snapshot.yml for how CI turns this into a
periodic artifact + monthly GitHub Release.

Usage:
    # Uses the same DATABASE_URL / INGESTOR_WRITER_URL resolution as the
    # crawler itself (ghapi_crawler.config.load_settings), so this points at
    # whatever Postgres the crawl is currently writing to:
    .venv/bin/python scripts/export_duckdb.py

    # Explicit output path (relative paths resolve against the repo root):
    .venv/bin/python scripts/export_duckdb.py --output snapshots/ghapi_2026-07.duckdb

    # Explicit source override (bypasses .env / DATABASE_URL resolution):
    .venv/bin/python scripts/export_duckdb.py \\
        --database-url 'postgresql://user:pass@host:5432/postgres?sslmode=require'

Requires the `duckdb` and `pytz` packages (neither is in requirements.txt —
the core crawler never needs them): `.venv/bin/pip install duckdb==1.2.2 pytz`.
`pytz` isn't imported directly here but DuckDB's own timestamptz handling
needs it importable for some source tables; see merge_duckdb.py's docstring
for the exception this throws otherwise.

Excluded on purpose:
    - github_api_cache: ephemeral GitHub API response cache, not analysis
      data (and empty in production — the README recommends keeping the
      cache backend in memory precisely to avoid Postgres growth here).
    - ingestion_runs: defined in migrations/001_initial.sql but never
      written by any current job; nothing to export.
    - schema_migrations: migration bookkeeping, not analysis data.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ghapi_crawler.config import load_settings  # noqa: E402

# Every table a downstream analysis might reasonably want, in the order the
# migrations introduced them — a diff against a new migration's tables is
# then just "does this list still match the schema."
TABLES: tuple[str, ...] = (
    "tracked_agents",
    "repositories",
    "pull_requests",
    "pull_request_files",
    "pull_request_reviews",
    "pull_request_comments",
    "pull_request_review_comments",
    "pull_request_timeline_events",
    "pull_request_issue_links",
    "issues",
    "issue_comments",
    "issue_timeline_events",
    "discovery_state",
    "pr_metrics",
)


def export(output_path: Path, database_url: str, tables: tuple[str, ...] = TABLES) -> None:
    try:
        import duckdb
    except ImportError:
        sys.exit("Error: duckdb not installed. Run: .venv/bin/pip install duckdb==1.2.2")

    if output_path.exists():
        print(f"Removing existing {output_path}")
        output_path.unlink()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(output_path))
    try:
        print("Loading postgres extension ...", flush=True)
        con.execute("INSTALL postgres; LOAD postgres;")
        safe_url = database_url.split("@")[-1]  # strip credentials before logging
        print(f"Attaching Postgres source (...@{safe_url}) ...", flush=True)
        con.execute(f"ATTACH '{database_url}' AS src (TYPE postgres, READ_ONLY)")

        for table in tables:
            print(f"  Exporting {table} ...", flush=True)
            con.execute(f"CREATE TABLE {table} AS SELECT * FROM src.public.{table}")
            n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"    {n:,} rows")

        size_mb = output_path.stat().st_size / 1e6
        print(f"\nDone. Wrote {output_path} ({size_mb:.1f} MB)")
    finally:
        con.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="snapshots/ghapi_latest.duckdb",
        help="Output .duckdb path, relative to the GHAPI repo root unless "
        "absolute (default: snapshots/ghapi_latest.duckdb).",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Postgres source URL. Defaults to the crawler's own DATABASE_URL "
        "/ INGESTOR_WRITER_URL resolution (ghapi_crawler.config.load_settings).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    database_url = args.database_url or load_settings().database_url
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = REPO_ROOT / output_path
    export(output_path=output_path, database_url=database_url)


if __name__ == "__main__":
    main()
