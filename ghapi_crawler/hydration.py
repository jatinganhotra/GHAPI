from __future__ import annotations

import argparse
import datetime as dt
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable

from ghapi_crawler.classifiers import count_hunks, detect_language, extension_of, is_test_file, word_count
from ghapi_crawler.config import Settings, load_settings
from ghapi_crawler.db import (
    claim_pull_requests_for_hydration,
    ensure_tracked_agents,
    mark_pull_request_hydrated,
    mark_pull_request_hydration_error,
    open_connection,
    replace_issue_comments,
    replace_issue_timeline_events,
    replace_pull_request_comments,
    replace_pull_request_files,
    replace_pull_request_issue_links,
    replace_pull_request_review_comments,
    replace_pull_request_reviews,
    replace_pull_request_timeline_events,
    upsert_issue,
    upsert_pull_request_detail,
)
from ghapi_crawler.github_client import GitHubClient
from ghapi_crawler.linking import (
    issue_link_candidates_from_pr_body,
    issue_link_candidates_from_timeline_events,
    issues_from_link_candidates,
)
from ghapi_crawler.repository_metrics import (
    estimate_loc_from_code_frequency,
    summarize_repository_tree,
    upsert_repository_metadata,
)


class HydrationJob:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = GitHubClient(settings)

    def run(
        self,
        limit: int = 100,
        include_agents: Iterable[str] | None = None,
        shard_index: int = 0,
        shard_count: int = 1,
        agent_target: int | None = None,
        agent_cap: int | None = None,
    ) -> dict[str, Any]:
        success = 0
        failed = 0
        processed = 0
        graphql_prefetched = 0
        graphql_used = 0
        repositories_metadata_refreshed = 0

        window_from, window_to = self._hydration_window()

        conn = self._open_db_connection()
        t0 = time.monotonic()
        try:
            if agent_target is not None:
                limit = self._apply_agent_target(
                    conn=conn,
                    limit=limit,
                    include_agents=include_agents,
                    agent_target=agent_target,
                    shard_index=shard_index,
                    shard_count=shard_count,
                )

            refreshed_repos: set[str] = set()
            attempted_github_pr_ids: set[int] = set()
            print(
                f"[hydration] start: limit={limit} "
                f"shard={shard_index}/{shard_count - 1}"
            )

            while processed < limit:
                try:
                    claimed = claim_pull_requests_for_hydration(
                        conn=conn,
                        limit=1,
                        include_agents=include_agents,
                        exclude_github_pr_ids=attempted_github_pr_ids,
                        shard_index=shard_index,
                        shard_count=shard_count,
                        created_from=window_from,
                        created_to=window_to,
                        agent_cap=agent_cap,
                    )
                except Exception as exc:
                    if not _is_db_connection_error(exc):
                        raise
                    print(f"Hydration queue query failed; reopening DB connection: {exc}")
                    conn = self._reopen_db_connection(conn)
                    continue

                if not claimed:
                    break

                row = claimed[0]
                processed += 1
                github_pr_id = int(row["github_pr_id"])
                attempted_github_pr_ids.add(github_pr_id)
                repo_full_name = row["repo_full_name"]
                pr_number = int(row["pr_number"])
                owner, repo = _split_repo_full_name(repo_full_name)

                print(f"Hydrating PR {repo_full_name}#{pr_number} (github_pr_id={github_pr_id})")
                try:
                    pr_detail_prefetch = None
                    node_id = row.get("node_id")
                    if self.settings.github_graphql_enabled and node_id:
                        try:
                            metadata_by_node_id = self.client.batch_get_pull_request_metadata(
                                [str(node_id)]
                            )
                            graphql_prefetched += len(metadata_by_node_id)
                            pr_detail_prefetch = metadata_by_node_id.get(str(node_id))
                            if pr_detail_prefetch is not None:
                                graphql_used += 1
                        except Exception as exc:
                            print(
                                "GraphQL prefetch failed for "
                                f"{repo_full_name}#{pr_number}: {exc}"
                            )

                    if repo_full_name not in refreshed_repos:
                        refreshed = self._refresh_repository_metadata(
                            conn=conn,
                            owner=owner,
                            repo=repo,
                            repo_full_name=repo_full_name,
                        )
                        refreshed_repos.add(repo_full_name)
                        if refreshed:
                            repositories_metadata_refreshed += 1
                    self._hydrate_one(
                        conn=conn,
                        github_pr_id=github_pr_id,
                        owner=owner,
                        repo=repo,
                        repo_full_name=repo_full_name,
                        pr_number=pr_number,
                        current_agent_key=str(row["agent_key"]),
                        pr_detail_prefetch=pr_detail_prefetch,
                    )
                    mark_pull_request_hydrated(
                        conn=conn,
                        github_pr_id=github_pr_id,
                        hydrated_at=dt.datetime.now(dt.timezone.utc),
                    )
                    conn.commit()
                    success += 1
                    if success % 10 == 0:
                        print(
                            f"[hydration] progress: "
                            f"processed={processed} success={success} failed={failed} "
                            f"limit={limit} shard={shard_index}/{shard_count - 1} "
                            f"elapsed={time.monotonic() - t0:.0f}s"
                        )
                except Exception as exc:
                    conn = self._rollback_or_reconnect(
                        conn=conn,
                        repo_full_name=repo_full_name,
                        pr_number=pr_number,
                        context="hydration failure",
                    )
                    conn = self._persist_hydration_error(
                        conn=conn,
                        github_pr_id=github_pr_id,
                        repo_full_name=repo_full_name,
                        pr_number=pr_number,
                        error=str(exc),
                    )
                    failed += 1
                    print(f"Hydration failed for {repo_full_name}#{pr_number}: {exc}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
            cache = self.client.cache_stats()
            print(f"GitHub cache stats: {cache}")
            self.client.close()

        return {
            "processed": processed,
            "success": success,
            "failed": failed,
            "graphql_prefetched": graphql_prefetched,
            "graphql_used": graphql_used,
            "repositories_metadata_refreshed": repositories_metadata_refreshed,
            "cache_hits": cache["hits"],
            "cache_misses": cache["misses"],
            "cache_evictions": cache["evictions"],
            "cache_size": cache["size"],
        }

    def _hydration_window(self) -> tuple[dt.datetime | None, dt.datetime | None]:
        # When a closed discovery window is pinned (DISCOVERY_END_UTC set), scope
        # hydration progress and claims to [start, end); otherwise stay unbounded.
        if self.settings.discovery_end_utc is not None:
            return self.settings.discovery_start_utc, self.settings.discovery_end_utc
        return None, None

    def _apply_agent_target(
        self,
        conn,
        *,
        limit: int,
        include_agents: Iterable[str] | None,
        agent_target: int,
        shard_index: int,
        shard_count: int,
    ) -> int:
        agents_list = list(include_agents) if include_agents else None
        window_from, window_to = self._hydration_window()
        where_parts = ["last_hydrated_at IS NOT NULL"]
        params: list[Any] = []
        if agents_list:
            where_parts.append("agent_key = ANY(%s)")
            params.append(agents_list)
        if window_from is not None:
            where_parts.append("created_at >= %s")
            params.append(window_from)
        if window_to is not None:
            where_parts.append("created_at < %s")
            params.append(window_to)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM pull_requests WHERE " + " AND ".join(where_parts),
                tuple(params),
            )
            row = cur.fetchone()
            already_hydrated = int(row["count"]) if row else 0

        agents_desc = ", ".join(agents_list) if agents_list else "all"
        remaining = max(0, agent_target - already_hydrated)
        if remaining == 0:
            print(
                f"Agent target={agent_target} for [{agents_desc}]: "
                f"already_hydrated={already_hydrated}, remaining=0 (target met); "
                f"using full limit={limit} for ongoing hydration"
            )
            return limit
        # Ceiling division distributes remaining across all shards without under-serving any
        shard_share = -(-remaining // shard_count) if shard_count > 0 else remaining
        effective_limit = min(limit, shard_share)
        print(
            f"Agent target={agent_target} for [{agents_desc}]: "
            f"already_hydrated={already_hydrated}, remaining={remaining}, "
            f"shard_share={shard_share} (shard={shard_index}/{shard_count - 1}), "
            f"effective_limit={effective_limit}"
        )
        return effective_limit

    def _open_db_connection(self):
        conn = open_connection(self.settings)
        ensure_tracked_agents(conn)
        conn.commit()
        return conn

    def _reopen_db_connection(self, conn):
        try:
            conn.close()
        except Exception:
            pass
        return self._open_db_connection()

    def _rollback_or_reconnect(
        self,
        conn,
        *,
        repo_full_name: str,
        pr_number: int,
        context: str,
    ):
        try:
            conn.rollback()
            return conn
        except Exception as exc:
            print(
                "Hydration DB rollback failed for "
                f"{repo_full_name}#{pr_number} during {context}: {exc}. "
                "Reopening DB connection."
            )
            return self._reopen_db_connection(conn)

    def _persist_hydration_error(
        self,
        conn,
        *,
        github_pr_id: int,
        repo_full_name: str,
        pr_number: int,
        error: str,
    ):
        last_exc: Exception | None = None
        for attempt in range(1, 3):
            try:
                mark_pull_request_hydration_error(
                    conn=conn,
                    github_pr_id=github_pr_id,
                    error=error,
                )
                conn.commit()
                return conn
            except Exception as exc:
                last_exc = exc
                conn = self._rollback_or_reconnect(
                    conn=conn,
                    repo_full_name=repo_full_name,
                    pr_number=pr_number,
                    context=f"hydration error persistence attempt {attempt}",
                )
        print(
            "Failed to persist hydration error for "
            f"{repo_full_name}#{pr_number}: {last_exc}"
        )
        return conn

    def _hydrate_one(
        self,
        conn,
        github_pr_id: int,
        owner: str,
        repo: str,
        repo_full_name: str,
        pr_number: int,
        current_agent_key: str,
        pr_detail_prefetch: dict[str, Any] | None = None,
    ) -> None:
        if pr_detail_prefetch is not None:
            pr_detail = _graphql_pull_request_to_rest_shape(pr_detail_prefetch)
        else:
            pr_detail = self.client.get_pull_request(owner, repo, pr_number)
        files = self.client.list_pull_request_files(owner, repo, pr_number)
        commits = self.client.list_pull_request_commits(owner, repo, pr_number)
        reviews = self.client.list_pull_request_reviews(owner, repo, pr_number)
        pr_comments = self.client.list_pull_request_issue_comments(owner, repo, pr_number)
        review_comments = self.client.list_pull_request_review_comments(owner, repo, pr_number)
        pr_timeline = self.client.list_pull_request_timeline_events(owner, repo, pr_number)

        upsert_pull_request_detail(conn, github_pr_id=github_pr_id, detail=pr_detail)
        replace_pull_request_files(
            conn=conn,
            github_pr_id=github_pr_id,
            files=[_enriched_file(row) for row in files],
        )
        replace_pull_request_reviews(conn=conn, github_pr_id=github_pr_id, reviews=reviews)
        replace_pull_request_comments(
            conn=conn, github_pr_id=github_pr_id, comments=pr_comments
        )
        replace_pull_request_review_comments(
            conn=conn,
            github_pr_id=github_pr_id,
            comments=review_comments,
        )
        replace_pull_request_timeline_events(
            conn=conn,
            github_pr_id=github_pr_id,
            events=pr_timeline,
        )
        inferred_agent = _infer_agent_key_from_pr_commits(commits)
        signal_source = "commit signals"
        if inferred_agent is None:
            inferred_agent = _infer_agent_key_from_pr_body(pr_detail.get("body"))
            signal_source = "PR body footer"
        if (
            inferred_agent is not None
            and inferred_agent != current_agent_key
            and _set_pull_request_agent_key(
                conn=conn,
                github_pr_id=github_pr_id,
                agent_key=inferred_agent,
            )
        ):
            print(
                f"Reclassified PR {repo_full_name}#{pr_number} "
                f"from {current_agent_key} to {inferred_agent} "
                f"via {signal_source}"
            )

        self._hydrate_linked_issues(
            conn=conn,
            github_pr_id=github_pr_id,
            repo_full_name=repo_full_name,
            pr_body=pr_detail.get("body"),
            pr_timeline=pr_timeline,
        )

    def _refresh_repository_metadata(
        self,
        conn,
        owner: str,
        repo: str,
        repo_full_name: str,
    ) -> bool:
        try:
            repository = self.client.get_repository(owner, repo)
        except Exception as exc:
            print(f"Repository metadata fetch failed for {repo_full_name}: {exc}")
            return False

        tree_ref = str(repository.get("default_branch") or "HEAD")
        file_count: int | None = None
        blob_size_bytes: int | None = None
        tree_truncated: bool | None = None
        try:
            tree_payload = self.client.get_repository_tree(owner, repo, tree_ref, recursive=True)
            file_count, blob_size_bytes, tree_truncated = summarize_repository_tree(
                tree_payload
            )
        except Exception as exc:
            print(f"Repository tree fetch failed for {repo_full_name}: {exc}")

        loc_estimate: int | None = None
        try:
            code_frequency = self.client.get_repository_code_frequency(owner, repo)
            loc_estimate = estimate_loc_from_code_frequency(code_frequency)
        except Exception as exc:
            print(f"Repository LOC estimate fetch failed for {repo_full_name}: {exc}")

        try:
            upsert_repository_metadata(
                conn=conn,
                repo_full_name=repo_full_name,
                repository=repository,
                file_count=file_count,
                blob_size_bytes=blob_size_bytes,
                tree_truncated=tree_truncated,
                loc_estimate=loc_estimate,
            )
        except Exception as exc:
            conn.rollback()
            print(f"Repository metadata upsert failed for {repo_full_name}: {exc}")
            return False
        return True

    def _hydrate_linked_issues(
        self,
        conn,
        github_pr_id: int,
        repo_full_name: str,
        pr_body: str | None,
        pr_timeline: list[dict[str, Any]],
    ) -> None:
        body_candidates = issue_link_candidates_from_pr_body(repo_full_name, pr_body)
        timeline_candidates = issue_link_candidates_from_timeline_events(pr_timeline)
        all_candidates = body_candidates + timeline_candidates
        union_matches = issues_from_link_candidates(all_candidates)

        links: list[dict[str, Any]] = []
        hydrated_issues: dict[tuple[str, int], int] = {}

        for issue_repo_full_name, issue_number in sorted(union_matches):
            issue_owner, issue_repo = _split_repo_full_name(issue_repo_full_name)
            try:
                issue = self.client.get_issue(issue_owner, issue_repo, issue_number)
            except Exception as exc:
                print(
                    f"Linked issue fetch failed for {issue_repo_full_name}#{issue_number} "
                    f"(skipping, non-fatal): {exc}"
                )
                continue
            # Skip pull requests returned from /issues endpoint.
            if issue.get("pull_request"):
                continue

            github_issue_id = upsert_issue(
                conn=conn,
                repo_full_name=issue_repo_full_name,
                issue=issue,
                body_word_count=word_count(issue.get("body")),
            )
            hydrated_issues[(issue_repo_full_name, issue_number)] = github_issue_id

            issue_comments = self.client.list_issue_comments(
                issue_owner, issue_repo, issue_number
            )
            issue_timeline = self.client.list_issue_timeline_events(
                issue_owner, issue_repo, issue_number
            )
            replace_issue_comments(conn, github_issue_id, issue_comments)
            replace_issue_timeline_events(conn, github_issue_id, issue_timeline)

        for candidate in all_candidates:
            key = (candidate.repo_full_name, candidate.issue_number)
            github_issue_id = hydrated_issues.get(key)
            if github_issue_id is None:
                continue

            links.append(
                {
                    "github_issue_id": github_issue_id,
                    "link_type": candidate.link_type,
                    "source": candidate.source,
                    "confidence": candidate.confidence,
                    "confidence_score": candidate.confidence_score,
                    "explainability": candidate.explainability,
                }
            )

        replace_pull_request_issue_links(conn, github_pr_id, links)


@dataclass(frozen=True)
class _CommitAgentHints:
    """One agent's commit-level identity signals, checked at hydration time.

    Matching happens against the SAME commit-field candidates regardless of
    agent: `login_hints`/`token_hints` are checked against both (a) the
    GitHub-linked login of the commit author/committer and (b) the raw git
    author/committer *name* string (GitHub only backfills a `login` when the
    commit's email matches a known account — bots that don't verify commits
    that way still stamp a recognizable name). This is what lets the same
    mechanism cover Claude (which usually does get a linked `claude[bot]`
    login) and Jules/OpenHands (which today are only visible via the raw
    name field) with one code path.
    """

    agent_key: str
    login_hints: frozenset[str] = frozenset()
    token_hints: tuple[str, ...] = ()
    email_domain_hints: tuple[str, ...] = ()
    message_patterns: tuple[re.Pattern[str], ...] = ()


# Co-Authored-By: Claude <noreply@anthropic.com> (hardcoded in Claude Code system prompt)
_CO_AUTHORED_BY_ANTHROPIC = re.compile(
    r"^Co-[Aa]uthored-[Bb]y:.*@anthropic\.com",
    re.MULTILINE,
)
# Session URL appended by Claude Code Web even when attribution is disabled (issue #41873)
_CLAUDE_CODE_SESSION_URL = re.compile(
    r"https://claude\.ai/code/session_[A-Za-z0-9]+",
)
# PR body footer added by Claude Code (two known URL variants)
_CLAUDE_CODE_PR_FOOTER = re.compile(
    r"\[Claude Code\]\(https://claude\.(ai|com)/claude-code\)",
    re.IGNORECASE,
)

# Per-agent commit-signal hints, checked in order against every hydrated PR's
# commit list regardless of which agent's discovery query originally found
# it (e.g. a PR whose branch/author didn't match any query directly, or that
# was mis-tagged by an ambiguous one). Claude is first to preserve its
# pre-crawl-expansion-202607 priority exactly when a commit somehow matches
# more than one agent's hints (should not happen in practice).
#
# Jules and OpenHands deliberately carry ONLY exact `login_hints` — no
# `token_hints` substring matching. Both names collide with plausible human
# identities ("Jules" is a common first name; "openhands" is closer to a
# whole-word product reference but still not worth loosening), and neither
# agent has a search-expressible discovery query today (verified via `gh`
# 2026-07-04 — see plans/crawl_expansion_20260704.md), so this commit check
# is their only realistic capture path and false positives are pure noise
# with no compensating recall benefit. LogicStar's own classifier
# (insights/backend/aitw/scrape/pr_classifier.py) matches the same way: an
# exact-equality check against the first commit's raw author name.
COMMIT_AGENT_HINTS: tuple[_CommitAgentHints, ...] = (
    _CommitAgentHints(
        agent_key="claude",
        login_hints=frozenset(
            {"claude[bot]", "claude-app[bot]", "claude-dev[bot]", "anthropic-ai[bot]"}
        ),
        token_hints=("claude", "anthropic"),
        email_domain_hints=("anthropic.com",),
        message_patterns=(_CO_AUTHORED_BY_ANTHROPIC, _CLAUDE_CODE_SESSION_URL),
    ),
    _CommitAgentHints(
        agent_key="jules",
        login_hints=frozenset({"google-labs-jules[bot]"}),
    ),
    _CommitAgentHints(
        # Unverified beyond LogicStar's source (no OpenHands commit has been
        # inspected directly yet); tighten/replace once real hydrated data
        # is available. See agents.py's "openhands" entry for the discovery
        # side of this same caveat.
        agent_key="openhands",
        login_hints=frozenset({"openhands", "openhands[bot]", "openhands-agent[bot]"}),
    ),
)

# Backward-compat aliases for the pre-crawl-expansion-202607 Claude-only
# names (kept in case anything outside this module still imports them
# directly; prefer COMMIT_AGENT_HINTS for new code).
CLAUDE_COMMIT_LOGIN_HINTS = COMMIT_AGENT_HINTS[0].login_hints
CLAUDE_COMMIT_TOKEN_HINTS = COMMIT_AGENT_HINTS[0].token_hints
CLAUDE_COMMIT_EMAIL_DOMAIN_HINTS = COMMIT_AGENT_HINTS[0].email_domain_hints


def _infer_agent_key_from_pr_commits(commits: list[dict[str, Any]]) -> str | None:
    for commit in commits:
        logins = [value.strip().lower() for value in _commit_login_candidates(commit)]
        emails = [value.strip().lower() for value in _commit_email_candidates(commit)]
        messages = _commit_message_candidates(commit)

        for hints in COMMIT_AGENT_HINTS:
            for normalized in logins:
                if normalized in hints.login_hints:
                    return hints.agent_key
                if any(_token_in_identity(token, normalized) for token in hints.token_hints):
                    return hints.agent_key

            for normalized in emails:
                if any(normalized.endswith(f"@{domain}") for domain in hints.email_domain_hints):
                    return hints.agent_key
                if any(_token_in_identity(token, normalized) for token in hints.token_hints):
                    return hints.agent_key

            for message in messages:
                if any(pattern.search(message) for pattern in hints.message_patterns):
                    return hints.agent_key

    return None


def _infer_agent_key_from_pr_body(body: str | None) -> str | None:
    if not body:
        return None
    if _CLAUDE_CODE_PR_FOOTER.search(body):
        return "claude"
    return None


def _set_pull_request_agent_key(conn, github_pr_id: int, agent_key: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE pull_requests
            SET
                agent_key = %s,
                last_seen_at = NOW()
            WHERE github_pr_id = %s
              AND agent_key <> %s
            """,
            (agent_key, github_pr_id, agent_key),
        )
        return bool(cur.rowcount)


def _commit_login_candidates(commit: dict[str, Any]) -> list[str]:
    candidates = [
        (commit.get("author") or {}).get("login"),
        (commit.get("committer") or {}).get("login"),
        ((commit.get("commit") or {}).get("author") or {}).get("name"),
        ((commit.get("commit") or {}).get("committer") or {}).get("name"),
    ]
    return [
        value.strip()
        for value in candidates
        if isinstance(value, str) and value.strip()
    ]


def _commit_email_candidates(commit: dict[str, Any]) -> list[str]:
    candidates = [
        ((commit.get("commit") or {}).get("author") or {}).get("email"),
        ((commit.get("commit") or {}).get("committer") or {}).get("email"),
    ]
    return [
        value.strip()
        for value in candidates
        if isinstance(value, str) and value.strip()
    ]


def _commit_message_candidates(commit: dict[str, Any]) -> list[str]:
    msg = ((commit.get("commit") or {}).get("message") or "").strip()
    return [msg] if msg else []


def _token_in_identity(token: str, value: str) -> bool:
    if not token or not value:
        return False
    pattern = re.compile(rf"(^|[^a-z0-9]){re.escape(token.lower())}([^a-z0-9]|$)")
    return bool(pattern.search(value))


def _is_db_connection_error(exc: Exception) -> bool:
    message = str(exc).lower()
    markers = (
        "ssl error",
        "unexpected eof",
        "connection is bad",
        "server closed the connection",
        "connection not open",
        "could not receive data from server",
        "broken pipe",
        "connection reset by peer",
        "statement timeout",
        "querycanceled",
        "canceling statement due to statement timeout",
    )
    return any(marker in message for marker in markers)


def _split_repo_full_name(value: str) -> tuple[str, str]:
    parts = value.split("/")
    if len(parts) != 2:
        raise ValueError(f"Invalid repo_full_name: {value}")
    return parts[0], parts[1]


def _enriched_file(file_item: dict[str, Any]) -> dict[str, Any]:
    filename = file_item["filename"]
    patch = file_item.get("patch")
    language = detect_language(filename, patch=patch)
    is_test = is_test_file(filename, language=language, patch=patch)
    enriched = dict(file_item)
    enriched["is_test_file"] = is_test
    enriched["file_extension"] = extension_of(filename)
    enriched["language"] = language
    enriched["hunk_count_total"] = count_hunks(patch)
    return enriched


def _graphql_pull_request_to_rest_shape(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": node.get("title"),
        "body": node.get("body"),
        "state": _graphql_state_to_rest_state(node.get("state"), node.get("mergedAt")),
        "draft": bool(node.get("isDraft")),
        "user": {"login": (node.get("author") or {}).get("login")},
        "created_at": node.get("createdAt"),
        "updated_at": node.get("updatedAt"),
        "closed_at": node.get("closedAt"),
        "merged_at": node.get("mergedAt"),
        "additions": node.get("additions"),
        "deletions": node.get("deletions"),
        "changed_files": node.get("changedFiles"),
        "commits": (node.get("commits") or {}).get("totalCount"),
        "comments": (node.get("comments") or {}).get("totalCount"),
        # Not fetched via GraphQL batch path right now.
        "review_comments": None,
    }


def _graphql_state_to_rest_state(state: str | None, merged_at: str | None) -> str | None:
    if state is None:
        return None
    normalized = state.upper()
    if normalized == "MERGED" or merged_at:
        return "closed"
    if normalized == "OPEN":
        return "open"
    if normalized == "CLOSED":
        return "closed"
    return state.lower()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PR Arena v2 hydration job")
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum number of PRs to hydrate in one run.",
    )
    parser.add_argument(
        "--agent",
        action="append",
        help="Optional agent key filter (repeatable).",
    )
    parser.add_argument(
        "--shard-index",
        type=int,
        default=0,
        help="Zero-based shard index for deterministic hydration partitioning.",
    )
    parser.add_argument(
        "--shard-count",
        type=int,
        default=1,
        help="Total number of deterministic hydration shards.",
    )
    parser.add_argument(
        "--target",
        type=int,
        default=None,
        help=(
            "Per-agent hydration target: stop when this many total PRs for the agent(s) "
            "are already hydrated. Each shard receives an equal share of the remaining quota. "
            "Ensures balanced hydration across agents when set to the same value for all."
        ),
    )
    parser.add_argument(
        "--agent-cap",
        type=int,
        default=None,
        help=(
            "Hard per-agent corpus cap: claim_pull_requests_for_hydration stops returning rows "
            "once this many total PRs for the agent(s) are already hydrated. Unlike --target "
            "(which only ramps the effective --limit down as a shard approaches the target but "
            "keeps claiming past it), this is a genuine stop, re-evaluated on every claim, so "
            "overshoot across parallel shards is bounded to in-flight claims at the boundary."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.shard_count < 1:
        raise SystemExit(f"--shard-count must be >= 1; received {args.shard_count}")
    if args.shard_index < 0 or args.shard_index >= args.shard_count:
        raise SystemExit(
            "--shard-index must be in "
            f"[0, {args.shard_count - 1}]; received {args.shard_index}"
        )
    settings = load_settings()
    job = HydrationJob(settings)
    summary = job.run(
        limit=args.limit,
        include_agents=args.agent,
        shard_index=args.shard_index,
        shard_count=args.shard_count,
        agent_target=args.target,
        agent_cap=args.agent_cap,
    )
    print(
        f"[hydration] done: "
        f"processed={summary['processed']} success={summary['success']} failed={summary['failed']} "
        f"shard={args.shard_index}/{args.shard_count - 1}"
    )


if __name__ == "__main__":
    main()
