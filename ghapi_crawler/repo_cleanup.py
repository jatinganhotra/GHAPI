from __future__ import annotations

import argparse
import datetime as dt
import json
from dataclasses import dataclass, asdict
from typing import Any

from ghapi_crawler.config import load_settings
from ghapi_crawler.db import open_connection
from ghapi_crawler.repo_filters import RepoFilter


@dataclass(frozen=True)
class CleanupCandidate:
    repo_full_name: str
    reason: str
    pr_count: int
    issue_count: int
    latest_pr_created_at: dt.datetime | None


def collect_cleanup_candidates(conn, repo_filter: RepoFilter) -> list[CleanupCandidate]:
    candidates: list[CleanupCandidate] = []

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT repo_full_name
            FROM repositories
            ORDER BY repo_full_name ASC
            """
        )
        repos = [_value(row, "repo_full_name", 0) for row in cur.fetchall()]

        for repo_full_name in repos:
            reason = repo_filter.reason(repo_full_name)
            if reason == "allowed":
                continue

            cur.execute(
                """
                SELECT
                    COUNT(*)::int AS pr_count,
                    MAX(created_at) AS latest_pr_created_at
                FROM pull_requests
                WHERE repo_full_name = %s
                """,
                (repo_full_name,),
            )
            pr_row = cur.fetchone()

            cur.execute(
                """
                SELECT COUNT(*)::int AS issue_count
                FROM issues
                WHERE repo_full_name = %s
                """,
                (repo_full_name,),
            )
            issue_row = cur.fetchone()

            candidates.append(
                CleanupCandidate(
                    repo_full_name=repo_full_name,
                    reason=reason,
                    pr_count=int(_value(pr_row, "pr_count", 0) or 0),
                    issue_count=int(_value(issue_row, "issue_count", 0) or 0),
                    latest_pr_created_at=_value(pr_row, "latest_pr_created_at", 1),
                )
            )

    return candidates


def apply_repo_cleanup(
    conn,
    candidates: list[CleanupCandidate],
    delete_repository_rows: bool = True,
) -> dict[str, Any]:
    summary = {
        "repos_targeted": len(candidates),
        "pull_requests_deleted": 0,
        "issues_deleted": 0,
        "repositories_deleted": 0,
    }

    with conn.cursor() as cur:
        for candidate in candidates:
            cur.execute(
                "DELETE FROM pull_requests WHERE repo_full_name = %s",
                (candidate.repo_full_name,),
            )
            summary["pull_requests_deleted"] += int(cur.rowcount or 0)

            cur.execute(
                "DELETE FROM issues WHERE repo_full_name = %s",
                (candidate.repo_full_name,),
            )
            summary["issues_deleted"] += int(cur.rowcount or 0)

            if delete_repository_rows:
                cur.execute(
                    "DELETE FROM repositories WHERE repo_full_name = %s",
                    (candidate.repo_full_name,),
                )
                summary["repositories_deleted"] += int(cur.rowcount or 0)

    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Repository historical cleanup for repos now blocked by allow/deny filters. "
            "Dry-run by default."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply deletions. Without this flag, only a dry-run report is printed.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print report as JSON.",
    )
    parser.add_argument(
        "--allowlist",
        default=None,
        help="Override allowlist patterns (comma-separated).",
    )
    parser.add_argument(
        "--denylist",
        default=None,
        help="Override denylist patterns (comma-separated).",
    )
    parser.add_argument(
        "--keep-repository-rows",
        action="store_true",
        help="Delete PR/issue history but keep rows in repositories table.",
    )
    return parser.parse_args()


def _parse_patterns(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(
        pattern.strip()
        for pattern in value.split(",")
        if pattern and pattern.strip()
    )


def _value(row: Any, key: str, index: int) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    return row[index]


def main() -> None:
    args = parse_args()
    settings = load_settings()

    allowlist = (
        _parse_patterns(args.allowlist)
        if args.allowlist is not None
        else settings.repo_allowlist_patterns
    )
    denylist = (
        _parse_patterns(args.denylist)
        if args.denylist is not None
        else settings.repo_denylist_patterns
    )
    repo_filter = RepoFilter(
        allowlist_patterns=allowlist,
        denylist_patterns=denylist,
    )

    with open_connection(settings) as conn:
        candidates = collect_cleanup_candidates(conn, repo_filter)

        payload = {
            "mode": "apply" if args.apply else "dry-run",
            "repo_filter": {"allowlist": allowlist, "denylist": denylist},
            "candidate_count": len(candidates),
            "candidates": [
                {
                    **asdict(candidate),
                    "latest_pr_created_at": (
                        candidate.latest_pr_created_at.isoformat()
                        if candidate.latest_pr_created_at
                        else None
                    ),
                }
                for candidate in candidates
            ],
        }

        if args.apply:
            summary = apply_repo_cleanup(
                conn,
                candidates=candidates,
                delete_repository_rows=not args.keep_repository_rows,
            )
            conn.commit()
            payload["summary"] = summary

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Mode: {payload['mode']}")
        print(f"Candidates: {payload['candidate_count']}")
        for candidate in payload["candidates"]:
            print(
                f"- {candidate['repo_full_name']} "
                f"(reason={candidate['reason']}, prs={candidate['pr_count']}, "
                f"issues={candidate['issue_count']})"
            )
        if "summary" in payload:
            print("Applied summary:")
            for key, value in payload["summary"].items():
                print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
