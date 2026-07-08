from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


# Parse closing-keyword style references in PR body.
KEYWORD_PATTERN = re.compile(
    r"\b(?P<verb>close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+"
    r"(?:https://github\.com/(?P<url_repo>[\w.\-]+/[\w.\-]+)/issues/(?P<url_num>\d+)"
    r"|(?P<repo_ref>[\w.\-]+/[\w.\-]+)#(?P<repo_num>\d+)"
    r"|#(?P<local_num>\d+))",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class IssueLinkCandidate:
    repo_full_name: str
    issue_number: int
    link_type: str
    source: str
    confidence: str
    confidence_score: float
    explainability: dict[str, Any]


def issue_link_candidates_from_pr_body(
    repo_full_name: str, body: str | None
) -> list[IssueLinkCandidate]:
    if not body:
        return []

    out: dict[tuple[str, int, str], IssueLinkCandidate] = {}
    for match in KEYWORD_PATTERN.finditer(body):
        ref_type = "unknown"
        issue_repo = repo_full_name
        issue_number = 0
        confidence = "medium"
        confidence_score = 0.82

        if match.group("url_repo") and match.group("url_num"):
            issue_repo = match.group("url_repo")
            issue_number = int(match.group("url_num"))
            ref_type = "url"
            confidence = "high"
            confidence_score = 0.95
        elif match.group("repo_ref") and match.group("repo_num"):
            issue_repo = match.group("repo_ref")
            issue_number = int(match.group("repo_num"))
            ref_type = "repo_ref"
            confidence = "high"
            confidence_score = 0.93
        elif match.group("local_num"):
            issue_repo = repo_full_name
            issue_number = int(match.group("local_num"))
            ref_type = "local_ref"
            confidence = "medium"
            confidence_score = 0.82
        else:
            continue

        candidate = IssueLinkCandidate(
            repo_full_name=issue_repo,
            issue_number=issue_number,
            link_type="closing_keyword",
            source="pr_body",
            confidence=confidence,
            confidence_score=confidence_score,
            explainability={
                "reason": "Explicit closing keyword in PR body.",
                "keyword": (match.group("verb") or "").lower(),
                "reference_type": ref_type,
                "match_text": match.group(0),
            },
        )
        _merge_candidate(out, candidate)

    return list(out.values())


def issue_link_candidates_from_timeline_events(
    events: list[dict[str, Any]],
) -> list[IssueLinkCandidate]:
    out: dict[tuple[str, int, str], IssueLinkCandidate] = {}

    for event in events:
        source = event.get("source") or {}
        issue = source.get("issue") or {}
        repo_url = issue.get("repository_url")
        issue_number = issue.get("number")
        if not repo_url or not issue_number:
            continue
        parts = repo_url.strip("/").split("/")
        if len(parts) < 2:
            continue
        repo_full_name = "/".join(parts[-2:])

        event_type = str(event.get("event") or "unknown").lower()
        confidence, score = _timeline_confidence(event_type)
        candidate = IssueLinkCandidate(
            repo_full_name=repo_full_name,
            issue_number=int(issue_number),
            link_type="cross_reference",
            source="pr_timeline",
            confidence=confidence,
            confidence_score=score,
            explainability={
                "reason": "Issue link inferred from PR timeline event.",
                "event_type": event_type,
                "actor_login": ((event.get("actor") or {}).get("login")),
            },
        )
        _merge_candidate(out, candidate)

    return list(out.values())


def linked_issues_from_pr_body(repo_full_name: str, body: str | None) -> set[tuple[str, int]]:
    return issues_from_link_candidates(issue_link_candidates_from_pr_body(repo_full_name, body))


def linked_issues_from_timeline_events(events: list[dict[str, Any]]) -> set[tuple[str, int]]:
    return issues_from_link_candidates(issue_link_candidates_from_timeline_events(events))


def issues_from_link_candidates(
    candidates: list[IssueLinkCandidate],
) -> set[tuple[str, int]]:
    return {(c.repo_full_name, c.issue_number) for c in candidates}


def _merge_candidate(
    out: dict[tuple[str, int, str], IssueLinkCandidate],
    candidate: IssueLinkCandidate,
) -> None:
    key = (candidate.repo_full_name, candidate.issue_number, candidate.source)
    existing = out.get(key)
    if existing is None or candidate.confidence_score > existing.confidence_score:
        out[key] = candidate


def _timeline_confidence(event_type: str) -> tuple[str, float]:
    mapping: dict[str, tuple[str, float]] = {
        "connected": ("high", 0.90),
        "cross-referenced": ("medium", 0.78),
        "referenced": ("medium", 0.72),
        "closed": ("high", 0.88),
    }
    return mapping.get(event_type, ("low", 0.60))
