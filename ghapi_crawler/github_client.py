from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import time
from collections import OrderedDict
from itertools import islice
from typing import Any
from urllib.parse import quote

import requests

from ghapi_crawler.config import Settings

SEARCH_API_MIN_DELAY_SECONDS = 2.1


class GitHubClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "User-Agent": "prarena-v2-ingestor",
            }
        )
        if settings.github_token:
            self.session.headers["Authorization"] = f"token {settings.github_token}"
        self._cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._cache_hits = 0
        self._cache_misses = 0
        self._cache_evictions = 0
        backend = settings.github_cache_backend
        if backend not in {"postgres", "redis"}:
            backend = "memory"
        self._persistent_cache_backend = backend
        self._persistent_cache_enabled = (
            settings.github_cache_enabled
            and settings.github_cache_ttl_seconds > 0
            and self._persistent_cache_backend in {"postgres", "redis"}
        )
        self._persistent_cache_conn: Any | None = None
        self._persistent_cache_redis: Any | None = None
        self._persistent_cache_hits = 0
        self._persistent_cache_misses = 0
        self._persistent_cache_writes = 0
        self._persistent_cache_evictions = 0
        self._persistent_cache_errors = 0
        self._persistent_last_cleanup_at = 0.0
        self._persistent_writes_since_cleanup = 0
        self._redis_access_key = (
            f"{self.settings.github_cache_redis_key_prefix}:access_index"
        )

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

        if self._persistent_cache_conn is not None:
            try:
                self._persistent_cache_conn.close()
            except Exception:
                pass
            self._persistent_cache_conn = None

        if self._persistent_cache_redis is not None:
            try:
                close = getattr(self._persistent_cache_redis, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
            self._persistent_cache_redis = None

    def search_pull_requests(
        self,
        query: str,
        created_from: dt.datetime,
        created_to: dt.datetime,
        page: int,
        per_page: int,
    ) -> dict[str, Any]:
        q = f"{query} created:{_fmt(created_from)}..{_fmt(created_to)}"
        return self._request(
            "GET",
            f"{self.settings.github_api_base}/search/issues",
            params={
                "q": q,
                "sort": "created",
                "order": "asc",
                "page": page,
                "per_page": per_page,
            },
        )

    def execute_graphql(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = self._request(
            "POST",
            f"{self.settings.github_api_base}/graphql",
            json_body={"query": query, "variables": variables or {}},
            accept_header="application/json",
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"Expected dict payload from GraphQL, got {type(payload)}")
        if payload.get("errors"):
            raise RuntimeError(f"GraphQL query failed: {payload['errors']}")
        return payload

    def batch_get_pull_request_metadata(
        self, node_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        if not self.settings.github_graphql_enabled:
            return {}

        results: dict[str, dict[str, Any]] = {}
        unique_ids = [value for value in dict.fromkeys(node_ids) if value]
        if not unique_ids:
            return results

        batch_size = self.settings.github_graphql_batch_size
        for chunk in _chunked(unique_ids, batch_size):
            query = self._build_pull_request_batch_query(chunk)
            payload = self.execute_graphql(query=query)
            data = payload.get("data") or {}
            for idx, node_id in enumerate(chunk):
                alias = f"n{idx}"
                node = data.get(alias)
                if not node or node.get("__typename") != "PullRequest":
                    continue
                results[node_id] = node

        return results

    def get_pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return self._request(
            "GET",
            f"{self.settings.github_api_base}/repos/{owner}/{repo}/pulls/{number}",
        )

    def get_repository(self, owner: str, repo: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"{self.settings.github_api_base}/repos/{owner}/{repo}",
        )

    def get_repository_tree(
        self,
        owner: str,
        repo: str,
        ref: str,
        recursive: bool = True,
    ) -> dict[str, Any]:
        encoded_ref = quote(ref, safe="")
        params = {"recursive": 1} if recursive else None
        return self._request(
            "GET",
            f"{self.settings.github_api_base}/repos/{owner}/{repo}/git/trees/{encoded_ref}",
            params=params,
        )

    def get_repository_code_frequency(self, owner: str, repo: str) -> Any:
        return self._request(
            "GET",
            f"{self.settings.github_api_base}/repos/{owner}/{repo}/stats/code_frequency",
        )

    def list_pull_request_files(
        self, owner: str, repo: str, number: int
    ) -> list[dict[str, Any]]:
        return self._paginate(
            f"{self.settings.github_api_base}/repos/{owner}/{repo}/pulls/{number}/files"
        )

    def list_pull_request_reviews(
        self, owner: str, repo: str, number: int
    ) -> list[dict[str, Any]]:
        return self._paginate(
            f"{self.settings.github_api_base}/repos/{owner}/{repo}/pulls/{number}/reviews"
        )

    def list_pull_request_commits(
        self, owner: str, repo: str, number: int
    ) -> list[dict[str, Any]]:
        return self._paginate(
            f"{self.settings.github_api_base}/repos/{owner}/{repo}/pulls/{number}/commits"
        )

    def list_pull_request_review_comments(
        self, owner: str, repo: str, number: int
    ) -> list[dict[str, Any]]:
        return self._paginate(
            f"{self.settings.github_api_base}/repos/{owner}/{repo}/pulls/{number}/comments"
        )

    def list_pull_request_issue_comments(
        self, owner: str, repo: str, number: int
    ) -> list[dict[str, Any]]:
        # PR conversation comments are issue comments under the same number.
        return self._paginate(
            f"{self.settings.github_api_base}/repos/{owner}/{repo}/issues/{number}/comments"
        )

    def list_pull_request_timeline_events(
        self, owner: str, repo: str, number: int
    ) -> list[dict[str, Any]]:
        # Timeline API may need the timeline media type for complete event coverage.
        return self._paginate(
            f"{self.settings.github_api_base}/repos/{owner}/{repo}/issues/{number}/timeline",
            accept_header="application/vnd.github+json, application/vnd.github.mockingbird-preview+json",
        )

    def get_issue(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return self._request(
            "GET",
            f"{self.settings.github_api_base}/repos/{owner}/{repo}/issues/{number}",
        )

    def list_issue_comments(
        self, owner: str, repo: str, number: int
    ) -> list[dict[str, Any]]:
        return self._paginate(
            f"{self.settings.github_api_base}/repos/{owner}/{repo}/issues/{number}/comments"
        )

    def list_issue_timeline_events(
        self, owner: str, repo: str, number: int
    ) -> list[dict[str, Any]]:
        return self._paginate(
            f"{self.settings.github_api_base}/repos/{owner}/{repo}/issues/{number}/timeline",
            accept_header="application/vnd.github+json, application/vnd.github.mockingbird-preview+json",
        )

    def _paginate(
        self,
        url: str,
        accept_header: str | None = None,
        per_page: int = 100,
        max_pages: int = 100,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            payload = self._request(
                "GET",
                url,
                params={"page": page, "per_page": per_page},
                accept_header=accept_header,
            )
            if not isinstance(payload, list):
                raise RuntimeError(f"Expected list payload for {url}, got {type(payload)}")
            if not payload:
                break
            items.extend(payload)
            if len(payload) < per_page:
                break
        return items

    def _request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        accept_header: str | None = None,
    ) -> Any:
        cache_key = self._cache_key(method, url, params, json_body, accept_header)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        retries = self.settings.search_max_retries
        for attempt in range(1, retries + 1):
            try:
                headers: dict[str, str] = {}
                if accept_header:
                    headers["Accept"] = accept_header

                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers if headers else None,
                    timeout=self.settings.request_timeout_seconds,
                )

                if (
                    response.status_code == 403
                    and response.headers.get("X-RateLimit-Remaining") == "0"
                ):
                    reset_epoch = int(response.headers.get("X-RateLimit-Reset", "0"))
                    sleep_for = max(reset_epoch - int(time.time()), 1) + 1
                    print(
                        f"Rate limited by GitHub, sleeping {sleep_for}s (attempt {attempt}/{retries})"
                    )
                    time.sleep(sleep_for)
                    continue

                if response.status_code in {403, 429}:
                    if self._should_treat_as_secondary_rate_limit(response):
                        sleep_for = self._secondary_rate_limit_sleep_seconds(
                            response=response,
                            attempt=attempt,
                        )
                        print(
                            f"GitHub secondary rate limit detected, sleeping {sleep_for}s "
                            f"(attempt {attempt}/{retries})"
                        )
                        time.sleep(sleep_for)
                        continue

                if 500 <= response.status_code < 600:
                    raise requests.HTTPError(
                        f"GitHub server error {response.status_code}",
                        response=response,
                    )

                response.raise_for_status()
                payload = _strip_nul_chars(response.json())
                self._cache_set(cache_key, payload)
                delay_seconds = self._delay_after_request(url)
                if delay_seconds > 0:
                    time.sleep(delay_seconds)
                return payload
            except Exception as exc:
                if not self._should_retry_request_error(exc):
                    raise RuntimeError(f"GitHub request failed without retry: {url}") from exc
                if attempt == retries:
                    raise RuntimeError(
                        f"GitHub request failed after {retries} attempts: {url}"
                    ) from exc

                backoff = min(2 ** (attempt - 1), 30)
                print(
                    f"GitHub request retry {attempt}/{retries} after error: {exc}. "
                    f"Sleeping {backoff}s"
                )
                time.sleep(backoff)

        raise RuntimeError("Unreachable retry loop")

    def _should_treat_as_secondary_rate_limit(self, response: requests.Response) -> bool:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            return True

        message = self._response_message_text(response).lower()
        return (
            "secondary rate limit" in message
            or "abuse detection" in message
        )

    def _secondary_rate_limit_sleep_seconds(
        self,
        response: requests.Response,
        attempt: int,
    ) -> int:
        retry_after_header = response.headers.get("Retry-After")
        retry_after = 0
        if retry_after_header:
            try:
                retry_after = int(float(retry_after_header))
            except Exception:
                retry_after = 0

        reset_epoch_header = response.headers.get("X-RateLimit-Reset")
        reset_wait = 0
        if reset_epoch_header:
            try:
                reset_wait = max(int(reset_epoch_header) - int(time.time()), 0) + 1
            except Exception:
                reset_wait = 0

        fallback = min(15 * max(attempt, 1), 300)
        if retry_after > 0 or reset_wait > 0:
            return max(retry_after, reset_wait, 1)
        return max(fallback, 1)

    def _response_message_text(self, response: requests.Response) -> str:
        try:
            payload = response.json()
        except Exception:
            return response.text or ""
        if isinstance(payload, dict):
            message = payload.get("message")
            if isinstance(message, str):
                return message
        return response.text or ""

    def _should_retry_request_error(self, exc: Exception) -> bool:
        if not isinstance(exc, requests.HTTPError):
            return True

        response = exc.response
        if response is None:
            return True

        status_code = response.status_code
        if status_code in {403, 429}:
            return True
        if 500 <= status_code < 600:
            return True

        return status_code in {408, 409}

    def _delay_after_request(self, url: str) -> float:
        delay = self.settings.search_delay_seconds
        if "/search/" in url:
            return max(delay, SEARCH_API_MIN_DELAY_SECONDS)
        return delay

    def cache_stats(self) -> dict[str, int]:
        persistent_size = (
            self._persistent_cache_size() if self._persistent_cache_enabled else 0
        )
        return {
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "evictions": self._cache_evictions,
            "size": len(self._cache),
            "persistent_hits": self._persistent_cache_hits,
            "persistent_misses": self._persistent_cache_misses,
            "persistent_writes": self._persistent_cache_writes,
            "persistent_evictions": self._persistent_cache_evictions,
            "persistent_errors": self._persistent_cache_errors,
            "persistent_size": persistent_size,
        }

    def clear_cache(self) -> None:
        self._cache.clear()
        if not self._persistent_cache_enabled:
            return

        try:
            if self._persistent_cache_backend == "postgres":
                conn = self._ensure_postgres_cache_connection()
                if conn is None:
                    return
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM github_api_cache")
            elif self._persistent_cache_backend == "redis":
                client = self._ensure_redis_client()
                if client is None:
                    return
                keys = list(
                    client.scan_iter(f"{self.settings.github_cache_redis_key_prefix}:data:*")
                )
                if keys:
                    client.delete(*keys)
                client.delete(self._redis_access_key)
        except Exception as exc:
            self._disable_persistent_cache(exc)

    def _cache_key(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None,
        accept_header: str | None,
    ) -> str:
        normalized_params = []
        for key, value in sorted((params or {}).items(), key=lambda item: item[0]):
            if isinstance(value, (list, tuple)):
                norm_val = [str(v) for v in value]
            else:
                norm_val = str(value)
            normalized_params.append((str(key), norm_val))

        return json.dumps(
            {
                "method": method.upper(),
                "url": url,
                "accept": accept_header or "",
                "params": normalized_params,
                "json": json.dumps(json_body or {}, sort_keys=True, separators=(",", ":")),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def _cache_get(self, key: str) -> Any | None:
        if not self.settings.github_cache_enabled or self.settings.github_cache_ttl_seconds <= 0:
            return None

        now = time.time()
        entry = self._cache.get(key)
        if entry is not None:
            expires_at, payload = entry
            if expires_at > now:
                self._cache.move_to_end(key, last=True)
                self._cache_hits += 1
                return copy.deepcopy(payload)
            del self._cache[key]

        if self._persistent_cache_enabled:
            persistent_payload = self._persistent_cache_get(key)
            if persistent_payload is not None:
                self._cache_set_memory(key, persistent_payload)
                self._cache_hits += 1
                return copy.deepcopy(persistent_payload)

        self._cache_misses += 1
        return None

    def _cache_set(self, key: str, payload: Any) -> None:
        if not self.settings.github_cache_enabled or self.settings.github_cache_ttl_seconds <= 0:
            return

        self._cache_set_memory(key, payload)
        if self._persistent_cache_enabled:
            self._persistent_cache_set(key, payload)

    def _cache_set_memory(self, key: str, payload: Any) -> None:
        expires_at = time.time() + self.settings.github_cache_ttl_seconds
        self._cache[key] = (expires_at, copy.deepcopy(payload))
        self._cache.move_to_end(key, last=True)

        while len(self._cache) > self.settings.github_cache_max_entries:
            self._cache.popitem(last=False)
            self._cache_evictions += 1

    def _persistent_cache_get(self, key: str) -> Any | None:
        if self._persistent_cache_backend == "postgres":
            return self._postgres_cache_get(key)
        if self._persistent_cache_backend == "redis":
            return self._redis_cache_get(key)
        return None

    def _postgres_cache_get(self, key: str) -> Any | None:
        conn = self._ensure_postgres_cache_connection()
        if conn is None:
            return None

        now = dt.datetime.now(dt.timezone.utc)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT payload, expires_at
                    FROM github_api_cache
                    WHERE cache_key = %s
                    """,
                    (key,),
                )
                row = cur.fetchone()
                if row is None:
                    self._persistent_cache_misses += 1
                    return None

                expires_at = row["expires_at"]
                if expires_at is None or expires_at <= now:
                    cur.execute(
                        "DELETE FROM github_api_cache WHERE cache_key = %s",
                        (key,),
                    )
                    deleted = int(cur.rowcount or 0)
                    self._persistent_cache_evictions += deleted
                    self._persistent_cache_misses += 1
                    return None

                cur.execute(
                    """
                    UPDATE github_api_cache
                    SET last_accessed_at = NOW()
                    WHERE cache_key = %s
                    """,
                    (key,),
                )
                self._persistent_cache_hits += 1
                return copy.deepcopy(_strip_nul_chars(row["payload"]))
        except Exception as exc:
            self._disable_persistent_cache(exc)
            return None

    def _persistent_cache_set(self, key: str, payload: Any) -> None:
        if self._persistent_cache_backend == "postgres":
            self._postgres_cache_set(key, payload)
            return
        if self._persistent_cache_backend == "redis":
            self._redis_cache_set(key, payload)
            return

    def _postgres_cache_set(self, key: str, payload: Any) -> None:
        conn = self._ensure_postgres_cache_connection()
        if conn is None:
            return

        expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
            seconds=self.settings.github_cache_ttl_seconds
        )
        payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO github_api_cache (
                        cache_key,
                        payload,
                        expires_at
                    )
                    VALUES (%s, %s::jsonb, %s)
                    ON CONFLICT (cache_key)
                    DO UPDATE SET
                        payload = EXCLUDED.payload,
                        expires_at = EXCLUDED.expires_at,
                        updated_at = NOW(),
                        last_accessed_at = NOW()
                    """,
                    (key, payload_json, expires_at),
                )
            self._persistent_cache_writes += 1
            self._persistent_writes_since_cleanup += 1
            self._maybe_cleanup_postgres(conn)
        except Exception as exc:
            self._disable_persistent_cache(exc)

    def _persistent_cache_size(self) -> int:
        if self._persistent_cache_backend == "postgres":
            return self._postgres_cache_size()
        if self._persistent_cache_backend == "redis":
            return self._redis_cache_size()
        return 0

    def _postgres_cache_size(self) -> int:
        conn = self._ensure_postgres_cache_connection()
        if conn is None:
            return 0

        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS total_entries FROM github_api_cache")
                row = cur.fetchone()
            if row is None:
                return 0
            return int(row["total_entries"] or 0)
        except Exception as exc:
            self._disable_persistent_cache(exc)
            return 0

    def _ensure_postgres_cache_connection(self) -> Any | None:
        if not self._persistent_cache_enabled:
            return None

        if self._persistent_cache_conn is not None and not self._persistent_cache_conn.closed:
            return self._persistent_cache_conn

        try:
            from ghapi_crawler.db import open_connection

            conn = open_connection(self.settings)
            conn.autocommit = True
            self._persistent_cache_conn = conn
            return conn
        except Exception as exc:
            self._disable_persistent_cache(exc)
            return None

    def _maybe_cleanup_postgres(self, conn: Any) -> None:
        now = time.time()
        interval = self.settings.github_cache_persistent_cleanup_interval_seconds
        if (
            self._persistent_writes_since_cleanup < 100
            and (now - self._persistent_last_cleanup_at) < interval
        ):
            return

        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM github_api_cache WHERE expires_at <= NOW()")
                expired_deleted = int(cur.rowcount or 0)
                self._persistent_cache_evictions += expired_deleted

                cur.execute("SELECT COUNT(*) AS total_entries FROM github_api_cache")
                total_row = cur.fetchone()
                total_entries = int(total_row["total_entries"] or 0) if total_row else 0
                overflow = (
                    total_entries - self.settings.github_cache_persistent_max_entries
                )
                if overflow > 0:
                    cur.execute(
                        """
                        DELETE FROM github_api_cache
                        WHERE cache_key IN (
                            SELECT cache_key
                            FROM github_api_cache
                            ORDER BY last_accessed_at ASC
                            LIMIT %s
                        )
                        """,
                        (overflow,),
                    )
                    self._persistent_cache_evictions += int(cur.rowcount or 0)
            self._persistent_last_cleanup_at = now
            self._persistent_writes_since_cleanup = 0
        except Exception as exc:
            self._disable_persistent_cache(exc)

    def _ensure_redis_client(self) -> Any | None:
        if not self._persistent_cache_enabled:
            return None

        if self._persistent_cache_redis is not None:
            return self._persistent_cache_redis

        try:
            import redis

            client = redis.Redis.from_url(
                self.settings.github_cache_redis_url,
                decode_responses=True,
            )
            client.ping()
            self._persistent_cache_redis = client
            return client
        except Exception as exc:
            self._disable_persistent_cache(exc)
            return None

    def _redis_cache_key(self, key: str) -> str:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return f"{self.settings.github_cache_redis_key_prefix}:data:{digest}"

    def _redis_cache_get(self, key: str) -> Any | None:
        client = self._ensure_redis_client()
        if client is None:
            return None

        data_key = self._redis_cache_key(key)
        try:
            payload_json = client.get(data_key)
            if not payload_json:
                zrem = getattr(client, "zrem", None)
                if callable(zrem):
                    zrem(self._redis_access_key, data_key)
                self._persistent_cache_misses += 1
                return None

            payload = _strip_nul_chars(json.loads(payload_json))
            client.zadd(self._redis_access_key, {data_key: time.time()})
            self._persistent_cache_hits += 1
            return copy.deepcopy(payload)
        except Exception as exc:
            self._disable_persistent_cache(exc)
            return None

    def _redis_cache_set(self, key: str, payload: Any) -> None:
        client = self._ensure_redis_client()
        if client is None:
            return

        data_key = self._redis_cache_key(key)
        payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        try:
            client.setex(data_key, self.settings.github_cache_ttl_seconds, payload_json)
            client.zadd(self._redis_access_key, {data_key: time.time()})
            self._persistent_cache_writes += 1
            self._persistent_writes_since_cleanup += 1
            self._maybe_cleanup_redis(client)
        except Exception as exc:
            self._disable_persistent_cache(exc)

    def _redis_cache_size(self) -> int:
        client = self._ensure_redis_client()
        if client is None:
            return 0
        try:
            return int(client.zcard(self._redis_access_key) or 0)
        except Exception as exc:
            self._disable_persistent_cache(exc)
            return 0

    def _maybe_cleanup_redis(self, client: Any) -> None:
        now = time.time()
        interval = self.settings.github_cache_persistent_cleanup_interval_seconds
        if (
            self._persistent_writes_since_cleanup < 100
            and (now - self._persistent_last_cleanup_at) < interval
        ):
            return

        try:
            total_entries = int(client.zcard(self._redis_access_key) or 0)
            overflow = total_entries - self.settings.github_cache_persistent_max_entries
            if overflow > 0:
                oldest_keys = client.zrange(self._redis_access_key, 0, overflow - 1)
                if oldest_keys:
                    client.delete(*oldest_keys)
                    client.zremrangebyrank(self._redis_access_key, 0, overflow - 1)
                    self._persistent_cache_evictions += len(oldest_keys)

            self._persistent_last_cleanup_at = now
            self._persistent_writes_since_cleanup = 0
        except Exception as exc:
            self._disable_persistent_cache(exc)

    def _disable_persistent_cache(self, exc: Exception) -> None:
        if self._persistent_cache_enabled:
            print(
                f"Disabling {self._persistent_cache_backend} cache backend due to error: {exc}"
            )
        self._persistent_cache_errors += 1
        self._persistent_cache_enabled = False
        self.close()

    def _build_pull_request_batch_query(self, node_ids: list[str]) -> str:
        fragments: list[str] = []
        for idx, node_id in enumerate(node_ids):
            quoted_id = json.dumps(node_id)
            fragments.append(
                f"""
                n{idx}: node(id: {quoted_id}) {{
                  __typename
                  ... on PullRequest {{
                    id
                    title
                    body
                    state
                    isDraft
                    createdAt
                    updatedAt
                    closedAt
                    mergedAt
                    additions
                    deletions
                    changedFiles
                    commits {{ totalCount }}
                    comments {{ totalCount }}
                    author {{ login }}
                  }}
                }}
                """
            )

        return "query {\n" + "\n".join(fragments) + "\n}"


def _fmt(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _chunked(values: list[str], size: int) -> list[list[str]]:
    chunks: list[list[str]] = []
    it = iter(values)
    while True:
        block = list(islice(it, size))
        if not block:
            break
        chunks.append(block)
    return chunks


def _strip_nul_chars(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_strip_nul_chars(item) for item in value]
    if isinstance(value, dict):
        return {key: _strip_nul_chars(item) for key, item in value.items()}
    return value
