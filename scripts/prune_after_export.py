#!/usr/bin/env python3
"""
Delete exported, hydrated pull requests from Postgres to free space.

The crawl runs on the Supabase free tier (500 MB). Every 14 days,
scripts/export_duckdb.py copies the database into a local DuckDB file. This
script then deletes the rows that the file holds, so that the crawl can go on.

Rule. A pull request is deleted only if all of these are true:
    1. It is hydrated (last_hydrated_at is set).
    2. The DuckDB file has the same PR with the same last_hydrated_at. So the
       file holds the PR in the state that Postgres holds now.
The PR rows in all the per-PR tables go too (ON DELETE CASCADE). Then the
script deletes each issue that the DuckDB file has and that no PR in Postgres
links to any more.

The script keeps these rows:
    - unhydrated PRs, and PRs that were hydrated again after the export
    - repositories, tracked_agents, discovery_state, and the other tables
    - one row per deleted PR in pruned_pull_requests (migration 012). Discovery
      skips those PRs, and the hydration cap counts them.

The check in rule 2 is per PR, so the crawler can run during the export.
export_duckdb.py copies pull_requests before the per-PR tables, and hydration
writes a PR's rows before it sets last_hydrated_at. So when the file has a
PR's last_hydrated_at, it also has that PR's rows.

Usage:
    .venv/bin/python scripts/prune_after_export.py --snapshot <file.duckdb>          # dry run
    .venv/bin/python scripts/prune_after_export.py --snapshot <file.duckdb> --apply  # delete

The database URL comes from --database-url, or else from the crawler config
(ghapi_crawler.config.load_settings), like export_duckdb.py.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ghapi_crawler.config import load_settings  # noqa: E402

BATCH_SIZE = 2000

CHILD_TABLES = (
    "pull_request_files",
    "pull_request_reviews",
    "pull_request_comments",
    "pull_request_review_comments",
    "pull_request_timeline_events",
    "pull_request_issue_links",
    "pr_metrics",
)


def _read_snapshot(snapshot: Path) -> tuple[list[tuple], list[int]]:
    """Return (github_pr_id, last_hydrated_at) of hydrated PRs, and the issue ids, in the file."""
    import duckdb

    con = duckdb.connect(str(snapshot), read_only=True)
    try:
        prs = con.execute(
            "SELECT github_pr_id, last_hydrated_at FROM pull_requests "
            "WHERE last_hydrated_at IS NOT NULL"
        ).fetchall()
        issues = [r[0] for r in con.execute("SELECT github_issue_id FROM issues").fetchall()]
    finally:
        con.close()
    return prs, issues


def _load_temp_tables(cur, prs: list[tuple], issues: list[int]) -> None:
    cur.execute(
        "CREATE TEMP TABLE snap_prs (github_pr_id BIGINT PRIMARY KEY, last_hydrated_at TIMESTAMPTZ NOT NULL)"
        " ON COMMIT DROP"
    )
    cur.execute("CREATE TEMP TABLE snap_issues (github_issue_id BIGINT PRIMARY KEY) ON COMMIT DROP")
    with cur.copy("COPY snap_prs (github_pr_id, last_hydrated_at) FROM STDIN") as copy:
        for row in prs:
            copy.write_row(row)
    with cur.copy("COPY snap_issues (github_issue_id) FROM STDIN") as copy:
        for issue_id in issues:
            copy.write_row((issue_id,))
    cur.execute(
        """
        CREATE TEMP TABLE prune_ids ON COMMIT DROP AS
        SELECT p.github_pr_id
        FROM pull_requests p
        JOIN snap_prs s USING (github_pr_id)
        WHERE p.last_hydrated_at IS NOT NULL
          AND p.last_hydrated_at = s.last_hydrated_at
        """
    )


def _count(cur, sql: str) -> int:
    cur.execute(sql)
    return int(cur.fetchone()[0])


def prune(snapshot: Path, database_url: str, apply: bool = False) -> dict[str, int]:
    """Delete the PRs that the rule allows. Return counts. With apply=False, delete nothing."""
    import psycopg

    if not snapshot.exists():
        raise FileNotFoundError(snapshot)
    prs, issues = _read_snapshot(snapshot)
    summary: dict[str, int] = {"snapshot_hydrated_prs": len(prs), "snapshot_issues": len(issues)}

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            _load_temp_tables(cur, prs, issues)
            summary["prs_to_delete"] = _count(cur, "SELECT COUNT(*) FROM prune_ids")
            for table in CHILD_TABLES:
                summary[f"{table}_to_delete"] = _count(
                    cur,
                    f"SELECT COUNT(*) FROM {table} WHERE github_pr_id IN (SELECT github_pr_id FROM prune_ids)",
                )
            summary["prs_kept"] = _count(cur, "SELECT COUNT(*) FROM pull_requests") - summary["prs_to_delete"]
            if not apply:
                conn.rollback()
                return summary

            # Write the tombstones first, in the same transaction as the deletes.
            cur.execute(
                """
                INSERT INTO pruned_pull_requests (github_pr_id, agent_key, created_at, last_hydrated_at)
                SELECT p.github_pr_id, p.agent_key, p.created_at, p.last_hydrated_at
                FROM pull_requests p
                WHERE p.github_pr_id IN (SELECT github_pr_id FROM prune_ids)
                ON CONFLICT (github_pr_id) DO UPDATE
                    SET last_hydrated_at = EXCLUDED.last_hydrated_at, pruned_at = NOW()
                """
            )
            cur.execute("SELECT github_pr_id FROM prune_ids ORDER BY github_pr_id")
            ids = [r[0] for r in cur.fetchall()]
            deleted = 0
            for start in range(0, len(ids), BATCH_SIZE):
                cur.execute(
                    "DELETE FROM pull_requests WHERE github_pr_id = ANY(%s)",
                    (ids[start : start + BATCH_SIZE],),
                )
                deleted += cur.rowcount
            summary["prs_deleted"] = deleted
            cur.execute(
                """
                DELETE FROM issues i
                WHERE i.github_issue_id IN (SELECT github_issue_id FROM snap_issues)
                  AND NOT EXISTS (
                      SELECT 1 FROM pull_request_issue_links l
                      WHERE l.github_issue_id = i.github_issue_id
                  )
                """
            )
            summary["issues_deleted"] = cur.rowcount
        conn.commit()
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", required=True, type=Path, help="DuckDB file from export_duckdb.py.")
    parser.add_argument("--database-url", default=None, help="Postgres URL. Default: the crawler config.")
    parser.add_argument("--apply", action="store_true", help="Delete the rows. Without it, only count them.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    database_url = args.database_url or load_settings().database_url
    summary = prune(args.snapshot, database_url, apply=args.apply)
    for key, value in summary.items():
        print(f"{key:44s} {value:>10,}")
    if not args.apply:
        print("Dry run. Nothing was deleted. Add --apply to delete.")


if __name__ == "__main__":
    main()
