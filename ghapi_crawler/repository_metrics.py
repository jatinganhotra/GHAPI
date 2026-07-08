from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from ghapi_crawler.db import _ensure_repository

if TYPE_CHECKING:
    import psycopg
    from psycopg.rows import DictRow
    Connection = psycopg.Connection[DictRow]
else:
    Connection = Any

_BASE_REPOSITORY_COLUMNS: tuple[str, ...] = (
    "repo_full_name",
    "owner_login",
    "repo_name",
    "api_url",
    "html_url",
    "last_seen_at",
)

_OPTIONAL_REPOSITORY_COLUMNS: tuple[str, ...] = (
    "stargazers_count",
    "forks_count",
    "watchers_count",
    "open_issues_count",
    "size_kb",
    "default_branch",
    "pushed_at",
    "file_count",
    "blob_size_bytes",
    "tree_truncated",
    "loc_estimate",
    "metadata_refreshed_at",
)

_DIRECT_UPDATE_COLUMNS = {"owner_login", "repo_name"}
# api_url and html_url share unique constraints with repo_full_name.  They are
# set once on insert by _ensure_repository and never overwritten here, since
# overwriting them risks UniqueViolation if another row already owns the URL.
_SKIP_UPDATE_COLUMNS = {"api_url", "html_url"}
_REPOSITORY_COLUMNS_CACHE: set[str] | None = None


def summarize_repository_tree(
    tree_payload: Any,
) -> tuple[int | None, int | None, bool | None]:
    if not isinstance(tree_payload, dict):
        return None, None, None

    entries = tree_payload.get("tree")
    if not isinstance(entries, list):
        return None, None, None

    file_count = 0
    blob_size_bytes = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "blob":
            continue
        file_count += 1
        size = _int_or_none(entry.get("size"))
        if size is not None and size >= 0:
            blob_size_bytes += size

    tree_truncated: bool | None = None
    if "truncated" in tree_payload:
        tree_truncated = bool(tree_payload.get("truncated"))

    return file_count, blob_size_bytes, tree_truncated


def estimate_loc_from_code_frequency(payload: Any) -> int | None:
    if not isinstance(payload, list):
        return None
    if not payload:
        return 0

    net_lines = 0
    used_rows = 0
    for row in payload:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        additions = _int_or_none(row[1])
        deletions = _int_or_none(row[2])
        net_lines += int(additions or 0) - int(deletions or 0)
        used_rows += 1

    if used_rows == 0:
        return None
    return max(net_lines, 0)


def upsert_repository_metadata(
    conn: Connection,
    repo_full_name: str,
    repository: dict[str, Any],
    *,
    file_count: int | None,
    blob_size_bytes: int | None,
    tree_truncated: bool | None,
    loc_estimate: int | None,
) -> None:
    default_owner, default_repo = _split_repo_full_name(repo_full_name)
    owner_login = str((repository.get("owner") or {}).get("login") or default_owner)
    repo_name = str(repository.get("name") or default_repo)
    api_url = str(repository.get("url") or f"https://api.github.com/repos/{repo_full_name}")

    available_columns = _repository_columns(conn)
    required_columns = set(_BASE_REPOSITORY_COLUMNS)
    if not required_columns.issubset(available_columns):
        missing = sorted(required_columns - available_columns)
        raise RuntimeError(
            "repositories table is missing required columns: " + ", ".join(missing)
        )

    # Ensure the row exists.  _ensure_repository handles both unique constraints
    # on repositories (repo_full_name, api_url) and returns the canonical name
    # actually stored in the table — important when the same physical repo lives
    # under a different name (rename).  The pure UPDATE below cannot raise
    # UniqueViolation because we never write to api_url/html_url.
    canonical_name = _ensure_repository(conn, repo_full_name, api_url)

    values_by_column: dict[str, Any] = {
        "owner_login": owner_login,
        "repo_name": repo_name,
        "stargazers_count": _int_or_none(repository.get("stargazers_count")),
        "forks_count": _int_or_none(repository.get("forks_count")),
        "watchers_count": _int_or_none(repository.get("watchers_count")),
        "open_issues_count": _int_or_none(repository.get("open_issues_count")),
        "size_kb": _int_or_none(repository.get("size")),
        "default_branch": repository.get("default_branch"),
        "pushed_at": _parse_github_datetime(repository.get("pushed_at")),
        "file_count": file_count,
        "blob_size_bytes": blob_size_bytes,
        "tree_truncated": tree_truncated,
        "loc_estimate": loc_estimate,
        "metadata_refreshed_at": dt.datetime.now(dt.timezone.utc),
    }

    set_clauses: list[str] = ["last_seen_at = NOW()"]
    params: list[Any] = []
    ordered_candidates = _BASE_REPOSITORY_COLUMNS + _OPTIONAL_REPOSITORY_COLUMNS
    for column in ordered_candidates:
        if column in {"repo_full_name", "last_seen_at"}:
            continue
        if column in _SKIP_UPDATE_COLUMNS:
            continue
        if column not in available_columns:
            continue
        if column not in values_by_column:
            continue
        if column in _DIRECT_UPDATE_COLUMNS:
            set_clauses.append(f"{column} = %s")
            params.append(values_by_column[column])
            continue
        set_clauses.append(f"{column} = COALESCE(%s, {column})")
        params.append(values_by_column[column])

    params.append(canonical_name)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE repositories
            SET {", ".join(set_clauses)}
            WHERE repo_full_name = %s
            """,
            tuple(params),
        )


def _split_repo_full_name(value: str) -> tuple[str, str]:
    parts = value.split("/")
    if len(parts) != 2:
        raise ValueError(f"Invalid repo_full_name: {value}")
    return parts[0], parts[1]


def _parse_github_datetime(value: Any) -> dt.datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            dt.timezone.utc
        )
    except ValueError:
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _repository_columns(conn: Connection) -> set[str]:
    global _REPOSITORY_COLUMNS_CACHE
    if _REPOSITORY_COLUMNS_CACHE is not None:
        return _REPOSITORY_COLUMNS_CACHE

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'repositories'
            """
        )
        rows = cur.fetchall()

    columns: set[str] = set()
    for row in rows:
        name: Any = None
        try:
            name = row["column_name"]
        except Exception:
            pass
        if name is None:
            try:
                name = row[0]
            except Exception:
                name = None
        if isinstance(name, str):
            columns.add(name)

    _REPOSITORY_COLUMNS_CACHE = columns
    return columns
