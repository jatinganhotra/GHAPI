from __future__ import annotations

import argparse
import datetime as dt
from typing import Any, Iterable

from ghapi_crawler.agents import AGENT_BY_KEY
from ghapi_crawler.assignment import select_issue_assignment_event
from ghapi_crawler.config import Settings, load_settings
from ghapi_crawler.db import ensure_tracked_agents, open_connection


class MetricJob:
    def __init__(self, settings: Settings):
        self.settings = settings

    def run(
        self, limit: int = 200, include_agents: Iterable[str] | None = None
    ) -> dict[str, int]:
        processed = 0
        success = 0
        failed = 0

        with open_connection(self.settings) as conn:
            ensure_tracked_agents(conn)
            conn.commit()

            candidates = self._list_candidates(conn, limit, include_agents)
            print(f"Metric candidates: {len(candidates)}")

            for pr in candidates:
                processed += 1
                pr_id = int(pr["github_pr_id"])
                try:
                    record = self._compute_record(conn, pr_id)
                    self._upsert_record(conn, record)
                    conn.commit()
                    success += 1
                except Exception as exc:
                    conn.rollback()
                    failed += 1
                    print(f"Metric compute failed for github_pr_id={pr_id}: {exc}")

        return {"processed": processed, "success": success, "failed": failed}

    def _list_candidates(
        self, conn, limit: int, include_agents: Iterable[str] | None
    ) -> list[dict[str, Any]]:
        params: list[Any] = [limit]
        where_agent = ""
        if include_agents:
            where_agent = "AND p.agent_key = ANY(%s)"
            params.insert(0, list(include_agents))

        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT p.github_pr_id
                FROM pull_requests p
                LEFT JOIN pr_metrics m ON m.github_pr_id = p.github_pr_id
                WHERE p.last_hydrated_at IS NOT NULL
                  AND (m.github_pr_id IS NULL OR m.computed_at < p.last_hydrated_at)
                  {where_agent}
                ORDER BY p.last_hydrated_at DESC
                LIMIT %s
                """,
                tuple(params),
            )
            return list(cur.fetchall())

    def _compute_record(self, conn, github_pr_id: int) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    github_pr_id,
                    agent_key,
                    repo_full_name,
                    pr_number,
                    created_at,
                    merged_at
                FROM pull_requests
                WHERE github_pr_id = %s
                """,
                (github_pr_id,),
            )
            pr = cur.fetchone()
            if not pr:
                raise RuntimeError(f"pull_requests row missing for {github_pr_id}")

            cur.execute(
                """
                SELECT is_test_file, hunk_count_total
                FROM pull_request_files
                WHERE github_pr_id = %s
                """,
                (github_pr_id,),
            )
            files = list(cur.fetchall())

            cur.execute(
                """
                SELECT state, submitted_at
                FROM pull_request_reviews
                WHERE github_pr_id = %s
                """,
                (github_pr_id,),
            )
            reviews = list(cur.fetchall())

            cur.execute(
                "SELECT COUNT(*) AS c FROM pull_request_comments WHERE github_pr_id = %s",
                (github_pr_id,),
            )
            pr_comment_count = int(cur.fetchone()["c"])

            cur.execute(
                "SELECT COUNT(*) AS c FROM pull_request_review_comments WHERE github_pr_id = %s",
                (github_pr_id,),
            )
            pr_review_comment_count = int(cur.fetchone()["c"])

            cur.execute(
                """
                SELECT DISTINCT l.github_issue_id
                FROM pull_request_issue_links l
                WHERE l.github_pr_id = %s
                """,
                (github_pr_id,),
            )
            issue_ids = [int(row["github_issue_id"]) for row in cur.fetchall()]

            issue_rows: list[dict[str, Any]] = []
            issue_assignment_events: list[dict[str, Any]] = []
            if issue_ids:
                cur.execute(
                    """
                    SELECT
                        i.github_issue_id,
                        i.body_word_count,
                        i.created_at,
                        (SELECT COUNT(*) FROM issue_comments ic WHERE ic.github_issue_id = i.github_issue_id) AS comment_count
                    FROM issues i
                    WHERE i.github_issue_id = ANY(%s)
                    """,
                    (issue_ids,),
                )
                issue_rows = list(cur.fetchall())

                cur.execute(
                    """
                    SELECT github_issue_id, created_at, assigned_login
                    FROM issue_timeline_events
                    WHERE github_issue_id = ANY(%s)
                      AND event_type = 'assigned'
                      AND created_at IS NOT NULL
                    ORDER BY created_at ASC
                    """,
                    (issue_ids,),
                )
                issue_assignment_events = list(cur.fetchall())

        files_total = len(files)
        files_test = sum(1 for row in files if row["is_test_file"])
        files_code = files_total - files_test

        hunks_total = sum(int(row["hunk_count_total"] or 0) for row in files)
        hunks_test = sum(
            int(row["hunk_count_total"] or 0) for row in files if row["is_test_file"]
        )
        hunks_code = hunks_total - hunks_test

        pr_review_count = len(reviews)
        first_review_at = min(
            (row["submitted_at"] for row in reviews if row["submitted_at"]),
            default=None,
        )
        pr_has_changes_requested = any(
            (row["state"] or "").upper() == "CHANGES_REQUESTED" for row in reviews
        )
        pr_has_reviews = pr_review_count > 0
        pr_has_comments = (pr_comment_count + pr_review_comment_count) > 0

        linked_issue_count = len(issue_rows)
        has_multiple_linked_issues = linked_issue_count > 1
        issue_word_count_total = sum(int(row["body_word_count"] or 0) for row in issue_rows)
        issue_comment_count = sum(int(row["comment_count"] or 0) for row in issue_rows)
        issue_has_comments = issue_comment_count > 0
        issue_first_created_at = min(
            (row["created_at"] for row in issue_rows if row["created_at"]),
            default=None,
        )
        pr_created_at = pr["created_at"]
        pr_merged_at = pr["merged_at"]

        agent = AGENT_BY_KEY.get(pr["agent_key"])
        if agent is None:
            raise RuntimeError(f"Unknown agent key for metrics: {pr['agent_key']}")
        assignment = select_issue_assignment_event(
            agent,
            issue_assignment_events,
            pr_created_at=pr_created_at,
        )
        issue_first_assigned_at = assignment.assigned_at

        seconds_issue_created_to_assigned = _seconds_between(
            issue_first_created_at, issue_first_assigned_at
        )
        seconds_issue_assigned_to_pr_created = _seconds_between(
            issue_first_assigned_at, pr_created_at
        )
        seconds_pr_created_to_first_review = _seconds_between(pr_created_at, first_review_at)
        seconds_first_review_to_merged = _seconds_between(first_review_at, pr_merged_at)
        seconds_pr_created_to_merged = _seconds_between(pr_created_at, pr_merged_at)

        pr_merged_without_changes_requested = bool(
            pr_merged_at is not None and not pr_has_changes_requested
        )

        return {
            "github_pr_id": github_pr_id,
            "agent_key": pr["agent_key"],
            "repo_full_name": pr["repo_full_name"],
            "pr_number": int(pr["pr_number"]),
            "computed_at": dt.datetime.now(dt.timezone.utc),
            "files_total": files_total,
            "files_test": files_test,
            "files_code": files_code,
            "hunks_total": hunks_total,
            "hunks_test": hunks_test,
            "hunks_code": hunks_code,
            "linked_issue_count": linked_issue_count,
            "has_multiple_linked_issues": has_multiple_linked_issues,
            "issue_word_count_total": issue_word_count_total,
            "issue_comment_count": issue_comment_count,
            "issue_has_comments": issue_has_comments,
            "pr_comment_count": pr_comment_count,
            "pr_review_count": pr_review_count,
            "pr_review_comment_count": pr_review_comment_count,
            "pr_has_comments": pr_has_comments,
            "pr_has_reviews": pr_has_reviews,
            "pr_has_changes_requested": pr_has_changes_requested,
            "pr_merged_without_changes_requested": pr_merged_without_changes_requested,
            "issue_first_created_at": issue_first_created_at,
            "issue_first_assigned_at": issue_first_assigned_at,
            "issue_assigned_login": assignment.assigned_login,
            "issue_assignment_source": assignment.assignment_source,
            "issue_assignment_confidence": assignment.assignment_confidence,
            "first_review_at": first_review_at,
            "seconds_issue_created_to_assigned": seconds_issue_created_to_assigned,
            "seconds_issue_assigned_to_pr_created": seconds_issue_assigned_to_pr_created,
            "seconds_pr_created_to_first_review": seconds_pr_created_to_first_review,
            "seconds_first_review_to_merged": seconds_first_review_to_merged,
            "seconds_pr_created_to_merged": seconds_pr_created_to_merged,
        }

    def _upsert_record(self, conn, record: dict[str, Any]) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO pr_metrics (
                    github_pr_id,
                    agent_key,
                    repo_full_name,
                    pr_number,
                    computed_at,
                    files_total,
                    files_test,
                    files_code,
                    hunks_total,
                    hunks_test,
                    hunks_code,
                    linked_issue_count,
                    has_multiple_linked_issues,
                    issue_word_count_total,
                    issue_comment_count,
                    issue_has_comments,
                    pr_comment_count,
                    pr_review_count,
                    pr_review_comment_count,
                    pr_has_comments,
                    pr_has_reviews,
                    pr_has_changes_requested,
                    pr_merged_without_changes_requested,
                    issue_first_created_at,
                    issue_first_assigned_at,
                    issue_assigned_login,
                    issue_assignment_source,
                    issue_assignment_confidence,
                    first_review_at,
                    seconds_issue_created_to_assigned,
                    seconds_issue_assigned_to_pr_created,
                    seconds_pr_created_to_first_review,
                    seconds_first_review_to_merged,
                    seconds_pr_created_to_merged
                )
                VALUES (
                    %(github_pr_id)s,
                    %(agent_key)s,
                    %(repo_full_name)s,
                    %(pr_number)s,
                    %(computed_at)s,
                    %(files_total)s,
                    %(files_test)s,
                    %(files_code)s,
                    %(hunks_total)s,
                    %(hunks_test)s,
                    %(hunks_code)s,
                    %(linked_issue_count)s,
                    %(has_multiple_linked_issues)s,
                    %(issue_word_count_total)s,
                    %(issue_comment_count)s,
                    %(issue_has_comments)s,
                    %(pr_comment_count)s,
                    %(pr_review_count)s,
                    %(pr_review_comment_count)s,
                    %(pr_has_comments)s,
                    %(pr_has_reviews)s,
                    %(pr_has_changes_requested)s,
                    %(pr_merged_without_changes_requested)s,
                    %(issue_first_created_at)s,
                    %(issue_first_assigned_at)s,
                    %(issue_assigned_login)s,
                    %(issue_assignment_source)s,
                    %(issue_assignment_confidence)s,
                    %(first_review_at)s,
                    %(seconds_issue_created_to_assigned)s,
                    %(seconds_issue_assigned_to_pr_created)s,
                    %(seconds_pr_created_to_first_review)s,
                    %(seconds_first_review_to_merged)s,
                    %(seconds_pr_created_to_merged)s
                )
                ON CONFLICT (github_pr_id)
                DO UPDATE SET
                    agent_key = EXCLUDED.agent_key,
                    repo_full_name = EXCLUDED.repo_full_name,
                    pr_number = EXCLUDED.pr_number,
                    computed_at = EXCLUDED.computed_at,
                    files_total = EXCLUDED.files_total,
                    files_test = EXCLUDED.files_test,
                    files_code = EXCLUDED.files_code,
                    hunks_total = EXCLUDED.hunks_total,
                    hunks_test = EXCLUDED.hunks_test,
                    hunks_code = EXCLUDED.hunks_code,
                    linked_issue_count = EXCLUDED.linked_issue_count,
                    has_multiple_linked_issues = EXCLUDED.has_multiple_linked_issues,
                    issue_word_count_total = EXCLUDED.issue_word_count_total,
                    issue_comment_count = EXCLUDED.issue_comment_count,
                    issue_has_comments = EXCLUDED.issue_has_comments,
                    pr_comment_count = EXCLUDED.pr_comment_count,
                    pr_review_count = EXCLUDED.pr_review_count,
                    pr_review_comment_count = EXCLUDED.pr_review_comment_count,
                    pr_has_comments = EXCLUDED.pr_has_comments,
                    pr_has_reviews = EXCLUDED.pr_has_reviews,
                    pr_has_changes_requested = EXCLUDED.pr_has_changes_requested,
                    pr_merged_without_changes_requested = EXCLUDED.pr_merged_without_changes_requested,
                    issue_first_created_at = EXCLUDED.issue_first_created_at,
                    issue_first_assigned_at = EXCLUDED.issue_first_assigned_at,
                    issue_assigned_login = EXCLUDED.issue_assigned_login,
                    issue_assignment_source = EXCLUDED.issue_assignment_source,
                    issue_assignment_confidence = EXCLUDED.issue_assignment_confidence,
                    first_review_at = EXCLUDED.first_review_at,
                    seconds_issue_created_to_assigned = EXCLUDED.seconds_issue_created_to_assigned,
                    seconds_issue_assigned_to_pr_created = EXCLUDED.seconds_issue_assigned_to_pr_created,
                    seconds_pr_created_to_first_review = EXCLUDED.seconds_pr_created_to_first_review,
                    seconds_first_review_to_merged = EXCLUDED.seconds_first_review_to_merged,
                    seconds_pr_created_to_merged = EXCLUDED.seconds_pr_created_to_merged
                """,
                record,
            )


def _seconds_between(start: dt.datetime | None, end: dt.datetime | None) -> int | None:
    if start is None or end is None:
        return None
    seconds = int((end - start).total_seconds())
    if seconds < 0:
        return None
    return seconds


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PR Arena v2 metric extraction job")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--agent", action="append")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = load_settings()
    job = MetricJob(settings)
    summary = job.run(limit=args.limit, include_agents=args.agent)
    print(
        "Metric summary: "
        f"processed={summary['processed']} success={summary['success']} failed={summary['failed']}"
    )


if __name__ == "__main__":
    main()
