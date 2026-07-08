from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Iterable
from pathlib import Path
from urllib.parse import urlparse

from ghapi_crawler.agents import AGENTS, AgentDefinition
from ghapi_crawler.config import Settings

if TYPE_CHECKING:
    import psycopg
    from psycopg.rows import DictRow
    Connection = psycopg.Connection[DictRow]
else:
    Connection = Any


def open_connection(settings: Settings) -> Connection:
    configure_libpq_library_paths()
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError(
            "psycopg is required for database operations. "
            "Install dependencies with: pip install -r ghapi_crawler/requirements.txt"
        ) from exc
    return psycopg.connect(settings.database_url, row_factory=dict_row)


def configure_libpq_library_paths() -> None:
    candidates = [
        os.getenv("PG_LIB_DIR", ""),
        "/opt/homebrew/opt/libpq/lib",
        "/usr/local/opt/libpq/lib",
        "/Applications/Postgres.app/Contents/Versions/latest/lib",
        "/Applications/Postgres.app/Contents/Versions/18/lib",
    ]
    existing_paths = [path for path in candidates if path and Path(path).exists()]
    if not existing_paths:
        return

    for var in ("DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH", "LD_LIBRARY_PATH"):
        current = os.getenv(var, "")
        parts = [part for part in current.split(":") if part]
        updated = list(parts)
        changed = False
        for path in existing_paths:
            if path not in updated:
                updated.insert(0, path)
                changed = True
        if changed:
            os.environ[var] = ":".join(updated)


# Backward-compat alias for older references.
_configure_libpq_library_paths = configure_libpq_library_paths


def parse_github_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        dt.timezone.utc
    )


def ensure_tracked_agents(conn: Connection) -> None:
    with conn.cursor() as cur:
        for agent in AGENTS:
            cur.execute(
                """
                INSERT INTO tracked_agents (agent_key, display_name, discovery_query, metadata)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (agent_key)
                DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    discovery_query = EXCLUDED.discovery_query,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
                """,
                (
                    agent.key,
                    agent.display_name,
                    agent.discovery_query,
                    _json(asdict(agent)),
                ),
            )


def get_discovery_cursor(
    conn: Connection, agent_key: str
) -> dt.datetime | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cursor_created_at
            FROM discovery_state
            WHERE agent_key = %s
            """,
            (agent_key,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return row["cursor_created_at"]


def set_discovery_cursor(
    conn: Connection,
    agent_key: str,
    cursor_created_at: dt.datetime,
    notes: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO discovery_state (agent_key, cursor_created_at, notes)
            VALUES (%s, %s, %s)
            ON CONFLICT (agent_key)
            DO UPDATE SET
                cursor_created_at = EXCLUDED.cursor_created_at,
                notes = EXCLUDED.notes,
                updated_at = NOW()
            """,
            (agent_key, cursor_created_at, notes),
        )


def _ensure_repository(conn: Connection, repo_full_name: str, api_url: str) -> str:
    """Insert the repository if absent and return the canonical repo_full_name.

    repositories has two unique constraints (repo_full_name, api_url).  The
    same physical repo can appear under a different name (rename, case mismatch)
    so either key may already conflict.  ON CONFLICT DO NOTHING handles both
    silently; the follow-up SELECT by api_url returns whichever name the table
    actually stores so callers always reference a row that genuinely exists.
    """
    parts = repo_full_name.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid repo_full_name: {repo_full_name!r}")
    owner, repo_name = parts
    html_url = f"https://github.com/{repo_full_name}"

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO repositories (
                repo_full_name,
                owner_login,
                repo_name,
                api_url,
                html_url
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (repo_full_name, owner, repo_name, api_url, html_url),
        )
        cur.execute(
            "SELECT repo_full_name FROM repositories WHERE api_url = %s",
            (api_url,),
        )
        row = cur.fetchone()

    # None only if DO NOTHING fired on a concurrent insert that later rolled
    # back — vanishingly rare with single-writer discovery.  Fall back to the
    # parsed name; the downstream FK insert will surface the absence.
    return row["repo_full_name"] if row else repo_full_name


def upsert_repository_from_search_item(
    conn: Connection, item: dict[str, Any]
) -> str:
    api_url = item["repository_url"]
    full_name, _, _ = _parse_repo_identity(api_url)
    return _ensure_repository(conn, full_name, api_url)


def upsert_pull_request_from_search_item(
    conn: Connection, agent: AgentDefinition, item: dict[str, Any]
) -> None:
    repo_full_name = upsert_repository_from_search_item(conn, item)
    pr_block = item.get("pull_request") or {}
    github_pr_id = int(item["id"])
    pr_number = int(item["number"])
    draft = bool(item.get("draft", False))
    created_at = parse_github_datetime(item.get("created_at"))
    updated_at = parse_github_datetime(item.get("updated_at"))
    closed_at = parse_github_datetime(item.get("closed_at"))
    merged_at = parse_github_datetime(pr_block.get("merged_at"))
    # Use db_key if set (e.g. claude_head → 'claude'); otherwise fall back to key.
    stored_agent_key = agent.db_key or agent.key
    update_values = (
        stored_agent_key,
        item.get("node_id"),
        item.get("state"),
        draft,
        item.get("title"),
        (item.get("user") or {}).get("login"),
        item.get("html_url"),
        item.get("url"),
        pr_block.get("url"),
        pr_block.get("diff_url"),
        pr_block.get("patch_url"),
        created_at,
        updated_at,
        closed_at,
        merged_at,
        repo_full_name,
        pr_number,
    )
    insert_values = (
        github_pr_id,
        repo_full_name,
        pr_number,
        stored_agent_key,
        item.get("node_id"),
        item.get("state"),
        draft,
        item.get("title"),
        (item.get("user") or {}).get("login"),
        item.get("html_url"),
        item.get("url"),
        pr_block.get("url"),
        pr_block.get("diff_url"),
        pr_block.get("patch_url"),
        created_at,
        updated_at,
        closed_at,
        merged_at,
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE pull_requests
            SET
                agent_key = %s,
                node_id = %s,
                state = %s,
                draft = %s,
                title = %s,
                author_login = %s,
                html_url = %s,
                api_url = %s,
                pr_api_url = %s,
                diff_url = %s,
                patch_url = %s,
                created_at = %s,
                updated_at = %s,
                closed_at = %s,
                merged_at = %s,
                last_seen_at = NOW()
            WHERE repo_full_name = %s
              AND pr_number = %s
            RETURNING github_pr_id
            """,
            update_values,
        )
        if cur.fetchone():
            return

        cur.execute(
            """
            INSERT INTO pull_requests (
                github_pr_id,
                repo_full_name,
                pr_number,
                agent_key,
                node_id,
                state,
                draft,
                title,
                author_login,
                html_url,
                api_url,
                pr_api_url,
                diff_url,
                patch_url,
                created_at,
                updated_at,
                closed_at,
                merged_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (github_pr_id)
            DO UPDATE SET
                repo_full_name = EXCLUDED.repo_full_name,
                pr_number = EXCLUDED.pr_number,
                agent_key = EXCLUDED.agent_key,
                node_id = EXCLUDED.node_id,
                state = EXCLUDED.state,
                draft = EXCLUDED.draft,
                title = EXCLUDED.title,
                author_login = EXCLUDED.author_login,
                html_url = EXCLUDED.html_url,
                api_url = EXCLUDED.api_url,
                pr_api_url = EXCLUDED.pr_api_url,
                diff_url = EXCLUDED.diff_url,
                patch_url = EXCLUDED.patch_url,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at,
                closed_at = EXCLUDED.closed_at,
                merged_at = EXCLUDED.merged_at,
                last_seen_at = NOW()
            """,
            insert_values,
        )


def list_pull_requests_for_hydration(
    conn: Connection,
    limit: int,
    include_agents: Iterable[str] | None = None,
    exclude_github_pr_ids: Iterable[int] | None = None,
) -> list[dict[str, Any]]:
    return claim_pull_requests_for_hydration(
        conn=conn,
        limit=limit,
        include_agents=include_agents,
        exclude_github_pr_ids=exclude_github_pr_ids,
        shard_index=0,
        shard_count=1,
    )


def claim_pull_requests_for_hydration(
    conn: Connection,
    limit: int,
    include_agents: Iterable[str] | None = None,
    exclude_github_pr_ids: Iterable[int] | None = None,
    shard_index: int = 0,
    shard_count: int = 1,
    created_from: dt.datetime | None = None,
    created_to: dt.datetime | None = None,
    agent_cap: int | None = None,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    _validate_hydration_shard_inputs(shard_index=shard_index, shard_count=shard_count)
    if agent_cap is not None and not include_agents:
        raise ValueError("agent_cap requires include_agents to scope the cap")

    with conn.cursor() as cur:
        params: list[Any] = []
        where_parts = [
            "(last_hydrated_at IS NULL OR updated_at > last_hydrated_at)",
            "(hydration_error IS NULL OR hydration_error NOT ILIKE '%%GitHub request failed without retry%%')",
        ]
        if include_agents:
            where_parts.append("agent_key = ANY(%s)")
            params.append(list(include_agents))
        if exclude_github_pr_ids:
            where_parts.append("github_pr_id <> ALL(%s)")
            params.append(list(exclude_github_pr_ids))
        # Optional creation-window bound: when a closed backfill window is pinned,
        # only claim PRs created inside it so hydration is spent on in-window PRs
        # instead of refreshing the out-of-window rolling buffer.
        if created_from is not None:
            where_parts.append("created_at >= %s")
            params.append(created_from)
        if created_to is not None:
            where_parts.append("created_at < %s")
            params.append(created_to)
        # Optional hard corpus cap: stop claiming once the agent(s)' total
        # already-hydrated in-window count reaches agent_cap. Re-evaluated as a
        # correlated subquery on every claim call (the hydration loop claims
        # one row at a time), so overshoot across parallel shards is bounded by
        # in-flight claims at the exact boundary, not by a whole shard's
        # --limit. Unlike --target (HydrationJob._apply_agent_target), which
        # ramps hydration_limit down as a shard approaches the target but keeps
        # claiming past it, this is a genuine stop: once the subquery reaches
        # agent_cap, claim_pull_requests_for_hydration returns no rows.
        if agent_cap is not None:
            cap_where = ["agent_key = ANY(%s)", "last_hydrated_at IS NOT NULL"]
            cap_params: list[Any] = [list(include_agents)]
            if created_from is not None:
                cap_where.append("created_at >= %s")
                cap_params.append(created_from)
            if created_to is not None:
                cap_where.append("created_at < %s")
                cap_params.append(created_to)
            where_parts.append(
                "(SELECT COUNT(*) FROM pull_requests WHERE "
                + " AND ".join(cap_where)
                + ") < %s"
            )
            params.extend(cap_params)
            params.append(agent_cap)

        if shard_count > 1:
            where_parts.append("MOD(ABS(github_pr_id), %s) = %s")
            params.extend([shard_count, shard_index])

        where = f"WHERE {' AND '.join(where_parts)}"
        params.append(limit)

        cur.execute(
            f"""
            WITH candidates AS (
                SELECT github_pr_id
                FROM pull_requests
                {where}
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            SELECT
                p.github_pr_id,
                p.repo_full_name,
                p.pr_number,
                p.agent_key,
                p.node_id,
                p.updated_at,
                p.last_hydrated_at
            FROM pull_requests p
            INNER JOIN candidates c
                ON c.github_pr_id = p.github_pr_id
            ORDER BY p.updated_at DESC
            """,
            tuple(params),
        )
        return list(cur.fetchall())


def _validate_hydration_shard_inputs(shard_index: int, shard_count: int) -> None:
    if shard_count < 1:
        raise ValueError(f"shard_count must be >= 1; received {shard_count}")
    if shard_index < 0 or shard_index >= shard_count:
        raise ValueError(
            f"shard_index must be in [0, {shard_count - 1}]; received {shard_index}"
        )


def upsert_pull_request_detail(
    conn: Connection,
    github_pr_id: int,
    detail: dict[str, Any],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE pull_requests
            SET
                title = %s,
                state = %s,
                draft = %s,
                author_login = %s,
                created_at = %s,
                updated_at = %s,
                closed_at = %s,
                merged_at = %s,
                additions = %s,
                deletions = %s,
                changed_files = %s,
                commits = %s,
                review_comments = %s,
                comments_count = %s,
                hydration_error = NULL
            WHERE github_pr_id = %s
            """,
            (
                detail.get("title"),
                detail.get("state"),
                bool(detail.get("draft", False)),
                (detail.get("user") or {}).get("login"),
                parse_github_datetime(detail.get("created_at")),
                parse_github_datetime(detail.get("updated_at")),
                parse_github_datetime(detail.get("closed_at")),
                parse_github_datetime(detail.get("merged_at")),
                detail.get("additions"),
                detail.get("deletions"),
                detail.get("changed_files"),
                detail.get("commits"),
                detail.get("review_comments"),
                detail.get("comments"),
                github_pr_id,
            ),
        )


def mark_pull_request_hydrated(
    conn: Connection,
    github_pr_id: int,
    hydrated_at: dt.datetime,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE pull_requests
            SET
                last_hydrated_at = %s,
                hydration_error = NULL
            WHERE github_pr_id = %s
            """,
            (hydrated_at, github_pr_id),
        )


def mark_pull_request_hydration_error(
    conn: Connection,
    github_pr_id: int,
    error: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE pull_requests
            SET hydration_error = %s
            WHERE github_pr_id = %s
            """,
            (error[:2000], github_pr_id),
        )


def replace_pull_request_files(
    conn: Connection,
    github_pr_id: int,
    files: list[dict[str, Any]],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM pull_request_files WHERE github_pr_id = %s",
            (github_pr_id,),
        )

        for file_item in files:
            cur.execute(
                """
                INSERT INTO pull_request_files (
                    github_pr_id,
                    filename,
                    status,
                    additions,
                    deletions,
                    changes,
                    file_sha,
                    previous_filename,
                    file_extension,
                    language,
                    is_test_file,
                    hunk_count_total
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (github_pr_id, filename) DO NOTHING
                """,
                (
                    github_pr_id,
                    file_item["filename"],
                    file_item.get("status"),
                    file_item.get("additions"),
                    file_item.get("deletions"),
                    file_item.get("changes"),
                    file_item.get("sha"),
                    file_item.get("previous_filename"),
                    file_item.get("file_extension"),
                    file_item.get("language"),
                    bool(file_item.get("is_test_file", False)),
                    int(file_item.get("hunk_count_total", 0)),
                ),
            )


def replace_pull_request_reviews(
    conn: Connection,
    github_pr_id: int,
    reviews: list[dict[str, Any]],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM pull_request_reviews WHERE github_pr_id = %s",
            (github_pr_id,),
        )

        for review in reviews:
            cur.execute(
                """
                INSERT INTO pull_request_reviews (
                    review_id,
                    github_pr_id,
                    user_login,
                    state,
                    body,
                    submitted_at,
                    created_at,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    review["id"],
                    github_pr_id,
                    (review.get("user") or {}).get("login"),
                    review.get("state"),
                    review.get("body"),
                    parse_github_datetime(review.get("submitted_at")),
                    parse_github_datetime(review.get("submitted_at")),
                    parse_github_datetime(review.get("submitted_at")),
                ),
            )


def replace_pull_request_comments(
    conn: Connection,
    github_pr_id: int,
    comments: list[dict[str, Any]],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM pull_request_comments WHERE github_pr_id = %s",
            (github_pr_id,),
        )

        for comment in comments:
            cur.execute(
                """
                INSERT INTO pull_request_comments (
                    comment_id,
                    github_pr_id,
                    user_login,
                    body,
                    created_at,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    comment["id"],
                    github_pr_id,
                    (comment.get("user") or {}).get("login"),
                    comment.get("body"),
                    parse_github_datetime(comment.get("created_at")),
                    parse_github_datetime(comment.get("updated_at")),
                ),
            )


def replace_pull_request_review_comments(
    conn: Connection,
    github_pr_id: int,
    comments: list[dict[str, Any]],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM pull_request_review_comments WHERE github_pr_id = %s",
            (github_pr_id,),
        )

        for comment in comments:
            cur.execute(
                """
                INSERT INTO pull_request_review_comments (
                    comment_id,
                    github_pr_id,
                    user_login,
                    body,
                    path,
                    line,
                    side,
                    commit_id,
                    created_at,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    comment["id"],
                    github_pr_id,
                    (comment.get("user") or {}).get("login"),
                    comment.get("body"),
                    comment.get("path"),
                    comment.get("line"),
                    comment.get("side"),
                    comment.get("commit_id"),
                    parse_github_datetime(comment.get("created_at")),
                    parse_github_datetime(comment.get("updated_at")),
                ),
            )


def replace_pull_request_timeline_events(
    conn: Connection,
    github_pr_id: int,
    events: list[dict[str, Any]],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM pull_request_timeline_events WHERE github_pr_id = %s",
            (github_pr_id,),
        )

        for idx, event in enumerate(events):
            event_key = _event_key(prefix=f"pr:{github_pr_id}", event=event, fallback=idx)
            cur.execute(
                """
                INSERT INTO pull_request_timeline_events (
                    event_key,
                    github_pr_id,
                    event_type,
                    actor_login,
                    created_at
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    event_key,
                    github_pr_id,
                    event.get("event"),
                    (event.get("actor") or {}).get("login"),
                    parse_github_datetime(event.get("created_at")),
                ),
            )


def upsert_issue(
    conn: Connection,
    repo_full_name: str,
    issue: dict[str, Any],
    body_word_count: int,
) -> int:
    # issues.repo_full_name has a FK to repositories.  The issue's repo may
    # never have gone through discovery (it's parsed from PR body/timeline
    # links and can be any repo), so we must ensure the row exists first.
    api_url = issue.get("repository_url") or f"https://api.github.com/repos/{repo_full_name}"
    repo_full_name = _ensure_repository(conn, repo_full_name, api_url)

    issue_number = int(issue["number"])
    state = issue.get("state")
    title = issue.get("title")
    body = issue.get("body")
    author_login = (issue.get("user") or {}).get("login")
    comments_count = issue.get("comments", 0)
    created_at = parse_github_datetime(issue.get("created_at"))
    updated_at = parse_github_datetime(issue.get("updated_at"))
    closed_at = parse_github_datetime(issue.get("closed_at"))

    with conn.cursor() as cur:
        # Match by (repo_full_name, issue_number) first so the path stays safe
        # if GitHub ever returned a stale id under the new name (rename window).
        # Returning the existing github_issue_id keeps follow-up FK writes
        # (issue_comments, issue_timeline_events) attached to the actual row.
        cur.execute(
            """
            UPDATE issues
            SET
                state = %s,
                title = %s,
                body = %s,
                body_word_count = %s,
                author_login = %s,
                comments_count = %s,
                created_at = %s,
                updated_at = %s,
                closed_at = %s,
                last_seen_at = NOW()
            WHERE repo_full_name = %s
              AND issue_number = %s
            RETURNING github_issue_id
            """,
            (
                state, title, body, body_word_count, author_login,
                comments_count, created_at, updated_at, closed_at,
                repo_full_name, issue_number,
            ),
        )
        row = cur.fetchone()
        if row:
            return int(row["github_issue_id"])

        cur.execute(
            """
            INSERT INTO issues (
                github_issue_id,
                repo_full_name,
                issue_number,
                state,
                title,
                body,
                body_word_count,
                author_login,
                comments_count,
                created_at,
                updated_at,
                closed_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (github_issue_id)
            DO UPDATE SET
                repo_full_name = EXCLUDED.repo_full_name,
                issue_number = EXCLUDED.issue_number,
                state = EXCLUDED.state,
                title = EXCLUDED.title,
                body = EXCLUDED.body,
                body_word_count = EXCLUDED.body_word_count,
                author_login = EXCLUDED.author_login,
                comments_count = EXCLUDED.comments_count,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at,
                closed_at = EXCLUDED.closed_at,
                last_seen_at = NOW()
            """,
            (
                issue["id"],
                repo_full_name,
                issue_number,
                state,
                title,
                body,
                body_word_count,
                author_login,
                comments_count,
                created_at,
                updated_at,
                closed_at,
            ),
        )
    return int(issue["id"])


def replace_issue_comments(
    conn: Connection,
    github_issue_id: int,
    comments: list[dict[str, Any]],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM issue_comments WHERE github_issue_id = %s",
            (github_issue_id,),
        )
        for comment in comments:
            cur.execute(
                """
                INSERT INTO issue_comments (
                    comment_id,
                    github_issue_id,
                    user_login,
                    body,
                    created_at,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    comment["id"],
                    github_issue_id,
                    (comment.get("user") or {}).get("login"),
                    comment.get("body"),
                    parse_github_datetime(comment.get("created_at")),
                    parse_github_datetime(comment.get("updated_at")),
                ),
            )


def replace_issue_timeline_events(
    conn: Connection,
    github_issue_id: int,
    events: list[dict[str, Any]],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM issue_timeline_events WHERE github_issue_id = %s",
            (github_issue_id,),
        )

        for idx, event in enumerate(events):
            event_key = _event_key(
                prefix=f"issue:{github_issue_id}", event=event, fallback=idx
            )
            cur.execute(
                """
                INSERT INTO issue_timeline_events (
                    event_key,
                    github_issue_id,
                    event_type,
                    actor_login,
                    assigned_login,
                    created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    event_key,
                    github_issue_id,
                    event.get("event"),
                    (event.get("actor") or {}).get("login"),
                    _assigned_login_from_issue_event(event),
                    parse_github_datetime(event.get("created_at")),
                ),
            )


def replace_pull_request_issue_links(
    conn: Connection,
    github_pr_id: int,
    links: list[dict[str, Any]],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM pull_request_issue_links WHERE github_pr_id = %s",
            (github_pr_id,),
        )

        for link in links:
            cur.execute(
                """
                INSERT INTO pull_request_issue_links (
                    github_pr_id,
                    github_issue_id,
                    link_type,
                    source,
                    confidence,
                    confidence_score,
                    explainability
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (github_pr_id, github_issue_id, source)
                DO UPDATE SET
                    link_type = EXCLUDED.link_type,
                    confidence = EXCLUDED.confidence,
                    confidence_score = EXCLUDED.confidence_score,
                    explainability = EXCLUDED.explainability
                """,
                (
                    github_pr_id,
                    link["github_issue_id"],
                    link.get("link_type", "linked"),
                    link.get("source", "unknown"),
                    link.get("confidence", "medium"),
                    float(link.get("confidence_score", 0.7)),
                    _json(link.get("explainability") or {}),
                ),
            )


def _parse_repo_identity(api_url: str) -> tuple[str, str, str]:
    path = urlparse(api_url).path.strip("/")
    # Expected: repos/<owner>/<repo>
    parts = path.split("/")
    if len(parts) < 3 or parts[0] != "repos":
        raise ValueError(f"Unexpected repository_url format: {api_url}")
    owner = parts[1]
    repo = parts[2]
    return f"{owner}/{repo}", owner, repo


def _json(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def _assigned_login_from_issue_event(event: dict[str, Any]) -> str | None:
    assignee = event.get("assignee") or {}
    assigned_login = assignee.get("login")
    if assigned_login:
        return str(assigned_login)

    for candidate in event.get("assignees") or []:
        login = candidate.get("login")
        if login:
            return str(login)
    return None


def _event_key(prefix: str, event: dict[str, Any], fallback: int) -> str:
    event_id = event.get("id")
    if event_id is not None:
        return f"{prefix}:{event_id}"

    blob = _json(event)
    digest = hashlib.sha1(blob.encode("utf-8")).hexdigest()
    return f"{prefix}:hash:{fallback}:{digest}"
