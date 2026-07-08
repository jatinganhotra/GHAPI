from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None


_DOTENV_LOADED = False


def _parse_utc_timestamp(value: str) -> dt.datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"

    if "T" not in raw:
        raw = raw + "T00:00:00+00:00"

    parsed = dt.datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _parse_optional_utc_timestamp(value: str | None) -> dt.datetime | None:
    if value is None:
        return None
    return _parse_utc_timestamp(value)


def _parse_patterns(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(
        pattern.strip()
        for pattern in value.split(",")
        if pattern and pattern.strip()
    )


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_cache_backend(value: str | None) -> str:
    if value is None:
        return "memory"
    normalized = value.strip().lower()
    if normalized in {"memory", "postgres", "redis"}:
        return normalized
    return "memory"


def _env_with_default(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip()
    if not normalized:
        return default
    return normalized


def _env_optional(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized


def _first_nonempty_env(*names: str) -> str | None:
    for name in names:
        value = _env_optional(name)
        if value:
            return value
    return None


def _read_allowlist_file(env_var: str) -> str | None:
    path = os.getenv(env_var)
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            content = fh.read().strip()
        return content or None
    return None


def _maybe_load_dotenv() -> None:
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True

    if load_dotenv is None:
        return

    candidates: list[Path] = []
    candidates.append(Path.cwd() / ".env")
    candidates.append(Path(__file__).resolve().parents[1] / ".env")
    candidates.append(Path(__file__).resolve().parents[2] / ".env")

    seen: set[Path] = set()
    for env_path in candidates:
        resolved = env_path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if env_path.exists():
            load_dotenv(dotenv_path=env_path, override=False)
            return


@dataclass(frozen=True)
class Settings:
    database_url: str
    github_token: str | None
    github_api_base: str
    request_timeout_seconds: int
    search_per_page: int
    search_delay_seconds: float
    search_max_retries: int
    discovery_start_utc: dt.datetime
    discovery_end_utc: dt.datetime | None
    discovery_overlap_minutes: int
    discovery_max_window_hours: int
    max_shard_depth: int
    repo_allowlist_patterns: tuple[str, ...]
    repo_denylist_patterns: tuple[str, ...]
    github_cache_enabled: bool
    github_cache_ttl_seconds: int
    github_cache_max_entries: int
    github_cache_backend: str
    github_cache_persistent_max_entries: int
    github_cache_persistent_cleanup_interval_seconds: int
    github_cache_redis_url: str
    github_cache_redis_key_prefix: str
    github_graphql_enabled: bool
    github_graphql_batch_size: int


def load_settings() -> Settings:
    _maybe_load_dotenv()
    return Settings(
        database_url=_first_nonempty_env(
            "INGESTOR_WRITER_URL",
            "DATABASE_URL",
        )
        or "postgresql://localhost:5432/ghapi_crawler",
        github_token=_env_optional("GITHUB_TOKEN"),
        github_api_base=_env_with_default(
            "GITHUB_API_BASE", "https://api.github.com"
        ),
        request_timeout_seconds=int(_env_with_default("REQUEST_TIMEOUT_SECONDS", "30")),
        search_per_page=min(
            max(int(_env_with_default("SEARCH_PER_PAGE", "100")), 1), 100
        ),
        search_delay_seconds=float(_env_with_default("SEARCH_DELAY_SECONDS", "0.2")),
        search_max_retries=max(int(_env_with_default("SEARCH_MAX_RETRIES", "4")), 1),
        discovery_start_utc=_parse_utc_timestamp(
            _env_with_default("PRARENA_START_DATE", "2026-01-01T00:00:00Z")
        ),
        # Optional absolute end-bound for the discovery forward-walk. When set,
        # discovery never crawls PRs created after this instant (used to pin a
        # closed historical window, e.g. the Codex/Claude overlap window).
        discovery_end_utc=_parse_optional_utc_timestamp(_env_optional("DISCOVERY_END_UTC")),
        discovery_overlap_minutes=max(
            int(_env_with_default("DISCOVERY_OVERLAP_MINUTES", "60")), 0
        ),
        discovery_max_window_hours=max(
            int(_env_with_default("DISCOVERY_MAX_WINDOW_HOURS", "4")), 0
        ),
        max_shard_depth=max(int(_env_with_default("MAX_SHARD_DEPTH", "20")), 1),
        repo_allowlist_patterns=_parse_patterns(
            os.getenv("REPO_ALLOWLIST") or _read_allowlist_file("REPO_ALLOWLIST_FILE")
        ),
        repo_denylist_patterns=_parse_patterns(os.getenv("REPO_DENYLIST")),
        github_cache_enabled=_parse_bool(os.getenv("GITHUB_CACHE_ENABLED"), True),
        github_cache_ttl_seconds=max(
            int(_env_with_default("GITHUB_CACHE_TTL_SECONDS", "900")), 0
        ),
        github_cache_max_entries=max(
            int(_env_with_default("GITHUB_CACHE_MAX_ENTRIES", "5000")), 1
        ),
        github_cache_backend=_parse_cache_backend(os.getenv("GITHUB_CACHE_BACKEND")),
        github_cache_persistent_max_entries=max(
            int(_env_with_default("GITHUB_CACHE_PERSISTENT_MAX_ENTRIES", "200000")), 1
        ),
        github_cache_persistent_cleanup_interval_seconds=max(
            int(
                _env_with_default(
                    "GITHUB_CACHE_PERSISTENT_CLEANUP_INTERVAL_SECONDS", "300"
                )
            ),
            1,
        ),
        github_cache_redis_url=_env_with_default(
            "GITHUB_CACHE_REDIS_URL", "redis://localhost:6379/0"
        ),
        github_cache_redis_key_prefix=_env_with_default(
            "GITHUB_CACHE_REDIS_KEY_PREFIX", "ghapi_crawler:github_cache"
        ),
        github_graphql_enabled=_parse_bool(os.getenv("GITHUB_GRAPHQL_ENABLED"), False),
        github_graphql_batch_size=max(
            int(_env_with_default("GITHUB_GRAPHQL_BATCH_SIZE", "30")), 1
        ),
    )
