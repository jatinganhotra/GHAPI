from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Iterable

from ghapi_crawler.agents import AgentDefinition


@dataclass(frozen=True)
class AssignmentSelection:
    assigned_at: dt.datetime | None
    assigned_login: str | None
    assignment_source: str
    assignment_confidence: str


def select_issue_assignment_event(
    agent: AgentDefinition,
    assigned_events: Iterable[dict[str, Any]],
    pr_created_at: dt.datetime | None = None,
) -> AssignmentSelection:
    parsed = sorted(
        (_extract_assignment_event(row) for row in assigned_events),
        key=lambda item: item["created_at"] or dt.datetime.max.replace(tzinfo=dt.timezone.utc),
    )
    parsed = [row for row in parsed if row["created_at"] is not None]

    if not parsed:
        return AssignmentSelection(
            assigned_at=None,
            assigned_login=None,
            assignment_source="none",
            assignment_confidence="none",
        )

    exact_logins = {value.lower() for value in agent.assignment_logins}
    pre_pr, post_pr = _partition_by_pr_time(parsed, pr_created_at)

    matched = _match_agent_assignment(pre_pr, exact_logins, agent.assignment_tokens)
    if matched is not None:
        return matched

    generic_bot = _first_bot_assignment(pre_pr)
    if generic_bot is not None:
        return generic_bot

    # Fall back to post-PR agent matches only when no earlier signal is available.
    matched_post = _match_agent_assignment(
        post_pr,
        exact_logins,
        agent.assignment_tokens,
        after_pr_created=True,
    )
    if matched_post is not None:
        return matched_post

    return AssignmentSelection(
        assigned_at=None,
        assigned_login=None,
        assignment_source="none",
        assignment_confidence="none",
    )


def _extract_assignment_event(row: dict[str, Any]) -> dict[str, Any]:
    assigned_login = row.get("assigned_login")
    if not assigned_login:
        # Backward-compat while historical rows still include raw_payload.
        raw = row.get("raw_payload") or {}
        assignee = raw.get("assignee") or {}
        assigned_login = assignee.get("login")
        if not assigned_login:
            for candidate in raw.get("assignees") or []:
                if candidate.get("login"):
                    assigned_login = candidate.get("login")
                    break
    created_at = row.get("created_at")
    return {
        "created_at": created_at,
        "assigned_login": assigned_login,
    }


def _partition_by_pr_time(
    events: list[dict[str, Any]],
    pr_created_at: dt.datetime | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if pr_created_at is None:
        return events, []
    pre_pr: list[dict[str, Any]] = []
    post_pr: list[dict[str, Any]] = []
    for row in events:
        if row["created_at"] <= pr_created_at:
            pre_pr.append(row)
        else:
            post_pr.append(row)
    return pre_pr, post_pr


def _match_agent_assignment(
    events: list[dict[str, Any]],
    exact_logins: set[str],
    tokens: tuple[str, ...],
    after_pr_created: bool = False,
) -> AssignmentSelection | None:
    source_suffix = "_after_pr_created" if after_pr_created else ""

    for row in events:
        login = (row["assigned_login"] or "").lower()
        if login and login in exact_logins:
            return AssignmentSelection(
                assigned_at=row["created_at"],
                assigned_login=row["assigned_login"],
                assignment_source=f"agent_login_match{source_suffix}",
                assignment_confidence="high" if not after_pr_created else "medium",
            )

    for row in events:
        login = (row["assigned_login"] or "").lower()
        for token in tokens:
            if _token_in_login(token, login):
                if _looks_like_bot_login(login):
                    return AssignmentSelection(
                        assigned_at=row["created_at"],
                        assigned_login=row["assigned_login"],
                        assignment_source=f"agent_token_bot_match{source_suffix}",
                        assignment_confidence="high" if not after_pr_created else "medium",
                    )
                return AssignmentSelection(
                    assigned_at=row["created_at"],
                    assigned_login=row["assigned_login"],
                    assignment_source=f"agent_token_match{source_suffix}",
                    assignment_confidence="medium" if not after_pr_created else "low",
                )

    return None


def _first_bot_assignment(events: list[dict[str, Any]]) -> AssignmentSelection | None:
    for row in events:
        login = (row["assigned_login"] or "").lower()
        if _looks_like_bot_login(login):
            return AssignmentSelection(
                assigned_at=row["created_at"],
                assigned_login=row["assigned_login"],
                assignment_source="generic_bot_assignment",
                assignment_confidence="low",
            )
    return None


def _token_in_login(token: str, login: str) -> bool:
    if not token or not login:
        return False
    pattern = re.compile(rf"(^|[^a-z0-9]){re.escape(token.lower())}([^a-z0-9]|$)")
    return bool(pattern.search(login))


def _looks_like_bot_login(login: str) -> bool:
    if not login:
        return False
    return (
        login.endswith("[bot]")
        or login.endswith("-bot")
        or login.endswith("_bot")
        or "[bot]" in login
    )
