#!/usr/bin/env python3
"""
Incrementally merge new Postgres rows into an existing GHAPI DuckDB snapshot.

Cheaper than export_duckdb.py's full re-export for frequent (e.g. hourly or
daily) periodic runs once a snapshot already exists locally or as a
downloaded artifact/release asset. Part of crawl-expansion-202607 — see
plans/crawl_expansion_20260704.md in the parent repo for the periodic-export
design this implements.

Usage:
    .venv/bin/python scripts/export_duckdb.py   # once, to create the file
    .venv/bin/python scripts/merge_duckdb.py    # repeatedly, to top it up

Requires the `duckdb` and `pytz` packages (neither is in requirements.txt —
the core crawler never needs them): `.venv/bin/pip install duckdb==1.2.2 pytz`.
`pytz` isn't imported directly here; DuckDB's own timestamptz handling
(`MAX(created_at)` below) needs it importable and raises
`InvalidInputException` at query time otherwise — easy to miss since
export_duckdb.py's plain `SELECT *` never touches that code path.

Correctness note: tables created via `CREATE TABLE ... AS SELECT` (as
export_duckdb.py does) do not carry over Postgres's primary-key constraints,
so `INSERT OR IGNORE` alone cannot be trusted to dedupe. Every merge below
therefore uses an explicit `NOT EXISTS` anti-join on the table's real primary
key; the optional date-column filter is purely a performance pre-filter (it
narrows what the anti-join has to scan) and never the only guard against
duplicates.

Correctness note 2: DuckDB's Python cursor always reports `.rowcount == -1`
for INSERT statements (never implemented). Its own result set (`.fetchone()`
on the INSERT) usually reports the true count, but was observed during
testing to under-report on a self-referential `INSERT ... SELECT ... WHERE
NOT EXISTS (... FROM <same table>)` — plausibly the anti-join subquery
racing the write within one statement. Row counts below are therefore a
plain `COUNT(*)` before/after the INSERT, which cannot be fooled by either
issue. (The parent repo's analysis/snapshot/merge_duckdb.py uses `.rowcount`
directly and likely always logs -1; not fixed here — out of scope, different
repo.)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ghapi_crawler.config import load_settings  # noqa: E402

# (table, primary_key_columns, incremental_date_column_or_None). Mirrors
# export_duckdb.py's TABLES list — keep the two in sync when a migration
# adds a table.
MergeSpec = tuple[str, tuple[str, ...], str | None]

MERGE_SPECS: tuple[MergeSpec, ...] = (
    ("tracked_agents", ("agent_key",), None),
    ("repositories", ("repo_full_name",), None),
    ("pull_requests", ("github_pr_id",), "created_at"),
    ("pull_request_files", ("github_pr_id", "filename"), None),
    ("pull_request_reviews", ("review_id",), "submitted_at"),
    ("pull_request_comments", ("comment_id",), "created_at"),
    ("pull_request_review_comments", ("comment_id",), "created_at"),
    ("pull_request_timeline_events", ("event_key",), "created_at"),
    ("pull_request_issue_links", ("github_pr_id", "github_issue_id", "source"), None),
    ("issues", ("github_issue_id",), "created_at"),
    ("issue_comments", ("comment_id",), "created_at"),
    ("issue_timeline_events", ("event_key",), "created_at"),
    ("discovery_state", ("agent_key",), None),
    ("pr_metrics", ("github_pr_id",), None),
)


def merge(db_path: Path, database_url: str, specs: tuple[MergeSpec, ...] = MERGE_SPECS) -> None:
    try:
        import duckdb
    except ImportError:
        sys.exit("Error: duckdb not installed. Run: .venv/bin/pip install duckdb==1.2.2")

    if not db_path.exists():
        sys.exit(
            f"Error: {db_path} not found. Run export_duckdb.py first to create "
            "the initial snapshot."
        )

    con = duckdb.connect(str(db_path))
    try:
        con.execute("INSTALL postgres; LOAD postgres;")
        safe_url = database_url.split("@")[-1]
        print(f"Attaching Postgres source (...@{safe_url}) ...", flush=True)
        con.execute(f"ATTACH '{database_url}' AS src (TYPE postgres, READ_ONLY)")

        for table, pk_columns, date_column in specs:
            join = " AND ".join(f"dst.{col} = src.{col}" for col in pk_columns)
            where_clauses = [f"NOT EXISTS (SELECT 1 FROM {table} dst WHERE {join})"]

            if date_column:
                cutoff = con.execute(f"SELECT MAX({date_column}) FROM {table}").fetchone()[0]
                if cutoff is not None:
                    where_clauses.append(f"src.{date_column} > '{cutoff}'")

            where_sql = " AND ".join(where_clauses)
            # Measure by plain COUNT(*) before/after rather than trusting
            # cursor.rowcount (always -1 on DuckDB) or the INSERT statement's
            # own result set (observed to under-report on this specific
            # self-referential INSERT-SELECT-NOT-EXISTS shape) — see the
            # "Correctness note 2" above.
            before = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            con.execute(
                f"""
                INSERT INTO {table}
                SELECT src.* FROM src.public.{table} src
                WHERE {where_sql}
                """
            )
            after = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table}: +{after - before:,} rows")

        size_mb = db_path.stat().st_size / 1e6
        print(f"\nDone. {db_path} ({size_mb:.1f} MB)")
    finally:
        con.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default="snapshots/ghapi_latest.duckdb",
        help="Existing .duckdb path to merge into, relative to the GHAPI "
        "repo root unless absolute (default: snapshots/ghapi_latest.duckdb).",
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
    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = REPO_ROOT / db_path
    merge(db_path=db_path, database_url=database_url)


if __name__ == "__main__":
    main()
