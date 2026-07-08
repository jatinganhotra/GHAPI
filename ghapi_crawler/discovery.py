from __future__ import annotations

import argparse
import datetime as dt
import os
import time
from dataclasses import dataclass
from math import ceil
from typing import Iterable
from urllib.parse import urlparse

from ghapi_crawler.agents import AGENTS, AgentDefinition
from ghapi_crawler.config import Settings, load_settings
from ghapi_crawler.db import (
    ensure_tracked_agents,
    get_discovery_cursor,
    open_connection,
    parse_github_datetime,
    set_discovery_cursor,
    upsert_pull_request_from_search_item,
)
from ghapi_crawler.github_client import GitHubClient
from ghapi_crawler.repo_filters import RepoFilter


SEARCH_TOTAL_CAP = 1000


@dataclass(frozen=True)
class Shard:
    start: dt.datetime
    end: dt.datetime
    total_count: int
    truncated: bool


class DiscoveryJob:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = GitHubClient(settings)
        self.repo_filter = RepoFilter(
            allowlist_patterns=settings.repo_allowlist_patterns,
            denylist_patterns=settings.repo_denylist_patterns,
        )

    def run(
        self,
        include_agents: Iterable[str] | None = None,
        hydration_batch_size: int = 0,
        hydration_shard_index: int = 0,
        hydration_shard_count: int = 1,
    ) -> dict[str, dict[str, int]]:
        selected = _select_agents(include_agents)
        summary: dict[str, dict[str, int]] = {}
        try:
            with open_connection(self.settings) as conn:
                ensure_tracked_agents(conn)
                conn.commit()

            for agent in selected:
                print(f"Running discovery for agent={agent.key}")
                summary[agent.key] = self._run_agent_with_retry(
                    agent,
                    hydration_batch_size=hydration_batch_size,
                    hydration_shard_index=hydration_shard_index,
                    hydration_shard_count=hydration_shard_count,
                )
        finally:
            print(f"GitHub cache stats: {self.client.cache_stats()}")
            self.client.close()

        return summary

    def _run_agent_with_retry(
        self,
        agent: AgentDefinition,
        hydration_batch_size: int = 0,
        hydration_shard_index: int = 0,
        hydration_shard_count: int = 1,
    ) -> dict[str, int]:
        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            try:
                return self._run_agent(
                    agent,
                    hydration_batch_size=hydration_batch_size,
                    hydration_shard_index=hydration_shard_index,
                    hydration_shard_count=hydration_shard_count,
                )
            except Exception as exc:
                if attempt >= max_attempts or not _is_db_connection_error(exc):
                    raise
                # Exponential backoff so concurrent shard-0 discovery jobs
                # (claude_head/0 and codex/0 start in the same parallel wave)
                # have time to clear their current page's index writes before
                # we retry — without sleep the retry hits the same lock contention.
                delay = 30 * (2 ** (attempt - 1))  # 30s, 60s, 120s
                print(
                    f"Transient DB error during discovery for agent={agent.key}; "
                    f"retrying ({attempt}/{max_attempts - 1}) after {delay}s: {exc}"
                )
                time.sleep(delay)

        raise RuntimeError("unreachable")

    def _run_agent(
        self,
        agent: AgentDefinition,
        hydration_batch_size: int = 0,
        hydration_shard_index: int = 0,
        hydration_shard_count: int = 1,
    ) -> dict[str, int]:
        now = dt.datetime.now(dt.timezone.utc)
        # Optional absolute end-bound: pin a closed historical window by never
        # walking past discovery_end_utc (e.g. the Codex/Claude overlap window).
        if (
            self.settings.discovery_end_utc is not None
            and now > self.settings.discovery_end_utc
        ):
            now = self.settings.discovery_end_utc
        with open_connection(self.settings) as conn:
            prior_cursor = get_discovery_cursor(conn, agent.key)
        if prior_cursor is None:
            window_start = self.settings.discovery_start_utc
        else:
            window_start = max(
                self.settings.discovery_start_utc,
                prior_cursor
                - dt.timedelta(minutes=self.settings.discovery_overlap_minutes),
            )

        if self.settings.discovery_max_window_hours > 0:
            cap = window_start + dt.timedelta(hours=self.settings.discovery_max_window_hours)
            if now > cap:
                print(
                    f"[discovery] agent={agent.key} capping window end: "
                    f"{_fmt(now)} -> {_fmt(cap)} "
                    f"(DISCOVERY_MAX_WINDOW_HOURS={self.settings.discovery_max_window_hours})"
                )
                now = cap

        window_days = (now - window_start).total_seconds() / 86400
        print(
            f"[discovery] agent={agent.key} "
            f"window={_fmt(window_start)} -> {_fmt(now)} "
            f"({window_days:.1f} days) "
            f"prior_cursor={_fmt(prior_cursor) if prior_cursor else 'none'}"
        )

        if window_start >= now:
            print(f"[discovery] agent={agent.key} window_start>=now; no-op")
            with open_connection(self.settings) as conn:
                set_discovery_cursor(
                    conn, agent.key, now, notes="No-op; window_start>=now"
                )
                conn.commit()
            return {
                "shards": 0,
                "truncated_shards": 0,
                "items_seen": 0,
                "items_persisted": 0,
                "items_filtered_out": 0,
                "new_cursor_epoch": int(now.timestamp()),
            }

        print(f"[discovery] agent={agent.key} building shards ...")
        shard_t0 = time.monotonic()
        shards = self._build_shards(agent.discovery_query, window_start, now)
        print(
            f"[discovery] agent={agent.key} shards={len(shards)} "
            f"({time.monotonic() - shard_t0:.1f}s to build)"
        )

        items_seen = 0
        items_persisted = 0
        items_filtered_out = 0
        max_created = prior_cursor or window_start
        truncated_shards = 0
        items_since_last_flush = 0
        cursor_baseline = prior_cursor or window_start
        run_t0 = time.monotonic()

        with open_connection(self.settings) as conn:
            for i, shard in enumerate(shards):
                if shard.truncated:
                    truncated_shards += 1
                pages = min(ceil(shard.total_count / self.settings.search_per_page), 10)
                print(
                    f"[discovery] shard {i + 1}/{len(shards)}: "
                    f"{_fmt(shard.start)} -> {_fmt(shard.end)} "
                    f"count={shard.total_count} pages={pages}"
                    + (" [TRUNCATED]" if shard.truncated else "")
                )

                for page in range(1, pages + 1):
                    payload = self.client.search_pull_requests(
                        query=agent.discovery_query,
                        created_from=shard.start,
                        created_to=shard.end,
                        page=page,
                        per_page=self.settings.search_per_page,
                    )
                    items = payload.get("items", [])
                    if not items:
                        break

                    for item in items:
                        items_seen += 1
                        created_at = parse_github_datetime(item.get("created_at"))
                        if created_at and created_at > max_created:
                            max_created = created_at

                        repo_full_name = _repo_full_name_from_search_item(item)
                        if not self.repo_filter.is_allowed(repo_full_name):
                            items_filtered_out += 1
                            continue

                        upsert_pull_request_from_search_item(conn, agent, item)
                        items_persisted += 1
                        items_since_last_flush += 1

                    conn.commit()
                    print(
                        f"[discovery] shard {i + 1}/{len(shards)} page {page}/{pages}: "
                        f"+{len(items)} items "
                        f"| total seen={items_seen} persisted={items_persisted} "
                        f"filtered={items_filtered_out} "
                        f"elapsed={time.monotonic() - run_t0:.0f}s"
                    )

                    if (
                        hydration_batch_size > 0
                        and items_since_last_flush >= hydration_batch_size
                    ):
                        self._flush_hydration(
                            # Hydration claims rows by the stored agent_key (db_key),
                            # not the discovery key: claude_head/claude_body rows are
                            # stored as 'claude', so filtering on 'claude_head' matches
                            # nothing and the flush is a silent no-op.
                            agent_key=agent.db_key or agent.key,
                            batch_size=hydration_batch_size,
                            shard_index=hydration_shard_index,
                            shard_count=hydration_shard_count,
                        )
                        items_since_last_flush = 0

                    if len(items) < self.settings.search_per_page:
                        break

                # Save cursor after each shard so a timeout doesn't restart from scratch.
                if max_created > cursor_baseline:
                    set_discovery_cursor(
                        conn,
                        agent.key,
                        max_created,
                        notes=(
                            f"incremental shard={i + 1}/{len(shards)} "
                            f"persisted={items_persisted}"
                        ),
                    )
                    conn.commit()
                    print(
                        f"[discovery] cursor saved: agent={agent.key} "
                        f"cursor={_fmt(max_created)} "
                        f"(shard {i + 1}/{len(shards)})"
                    )

        # Use max seen created_at as a safe cursor so truncated shards do not skip data.
        if items_seen == 0:
            next_cursor = now
        else:
            next_cursor = max_created

        notes = (
            f"start={_fmt(window_start)}, end={_fmt(now)}, "
            f"shards={len(shards)}, truncated={truncated_shards}, items_seen={items_seen}, "
            f"persisted={items_persisted}, filtered_out={items_filtered_out}"
        )
        with open_connection(self.settings) as conn:
            set_discovery_cursor(conn, agent.key, next_cursor, notes=notes)
            conn.commit()

        print(
            f"[discovery] done: agent={agent.key} "
            f"shards={len(shards)} truncated={truncated_shards} "
            f"seen={items_seen} persisted={items_persisted} filtered={items_filtered_out} "
            f"elapsed={time.monotonic() - run_t0:.0f}s "
            f"new_cursor={_fmt(next_cursor)}"
        )

        return {
            "shards": len(shards),
            "truncated_shards": truncated_shards,
            "items_seen": items_seen,
            "items_persisted": items_persisted,
            "items_filtered_out": items_filtered_out,
            "new_cursor_epoch": int(next_cursor.timestamp()),
        }

    def _flush_hydration(
        self,
        *,
        agent_key: str,
        batch_size: int,
        shard_index: int,
        shard_count: int,
    ) -> None:
        from ghapi_crawler.hydration import HydrationJob  # noqa: PLC0415

        print(
            f"[discovery] hydration flush: agent={agent_key} "
            f"batch={batch_size} shard={shard_index}/{shard_count - 1}"
        )
        flush_t0 = time.monotonic()
        job = HydrationJob(self.settings)
        result = job.run(
            limit=batch_size,
            include_agents=[agent_key],
            shard_index=shard_index,
            shard_count=shard_count,
        )
        print(
            f"[discovery] hydration flush done: "
            f"processed={result['processed']} "
            f"success={result['success']} "
            f"failed={result['failed']} "
            f"elapsed={time.monotonic() - flush_t0:.0f}s"
        )

    def _build_shards(
        self,
        query: str,
        start: dt.datetime,
        end: dt.datetime,
        depth: int = 0,
    ) -> list[Shard]:
        total_count = self._count_query(query, start, end)
        if total_count == 0:
            return []
        if total_count <= SEARCH_TOTAL_CAP:
            return [Shard(start=start, end=end, total_count=total_count, truncated=False)]

        span_seconds = int((end - start).total_seconds())
        if depth >= self.settings.max_shard_depth or span_seconds <= 1:
            print(
                "Shard cannot be split further; truncating at 1000 search results. "
                f"query={query} start={_fmt(start)} end={_fmt(end)} count={total_count}"
            )
            return [Shard(start=start, end=end, total_count=total_count, truncated=True)]

        midpoint = start + dt.timedelta(seconds=span_seconds // 2)
        left_end = midpoint
        right_start = midpoint + dt.timedelta(seconds=1)
        if right_start > end:
            right_start = end

        left = self._build_shards(query, start, left_end, depth + 1)
        right = self._build_shards(query, right_start, end, depth + 1)
        return left + right

    def _count_query(self, query: str, start: dt.datetime, end: dt.datetime) -> int:
        payload = self.client.search_pull_requests(
            query=query,
            created_from=start,
            created_to=end,
            page=1,
            per_page=1,
        )
        return int(payload.get("total_count", 0))


def _select_agents(include_agents: Iterable[str] | None) -> list[AgentDefinition]:
    if not include_agents:
        return list(AGENTS)

    wanted = {value.strip().lower() for value in include_agents}
    selected = [agent for agent in AGENTS if agent.key in wanted]
    if not selected:
        raise ValueError(
            f"No matching agents in {sorted(wanted)}; valid keys: {[a.key for a in AGENTS]}"
        )
    return selected


def _fmt(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_full_name_from_search_item(item: dict) -> str:
    api_url = item.get("repository_url", "")
    path = urlparse(api_url).path.strip("/")
    parts = path.split("/")
    if len(parts) >= 3 and parts[0] == "repos":
        return f"{parts[1]}/{parts[2]}"
    raise ValueError(f"Unexpected repository_url format: {api_url}")


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PR Arena v2 discovery ingestion job")
    parser.add_argument(
        "--agent",
        action="append",
        help="Agent key to process (can be repeated). Defaults to all tracked agents.",
    )
    parser.add_argument(
        "--hydration-batch-size",
        type=int,
        default=0,
        help=(
            "Flush hydration inline every N persisted PRs during discovery. "
            "0 disables inline hydration (default)."
        ),
    )
    parser.add_argument(
        "--hydration-shard-index",
        type=int,
        default=0,
        help="Shard index to use for inline hydration flushes (default 0).",
    )
    parser.add_argument(
        "--hydration-shard-count",
        type=int,
        default=1,
        help="Total shard count to use for inline hydration flushes (default 1).",
    )
    parser.add_argument(
        "--until",
        default=None,
        help=(
            "Absolute end-bound for the discovery walk (UTC, e.g. 2026-03-22 or "
            "2026-03-22T00:00:00Z). Discovery never crawls PRs created after this "
            "instant. Overrides the DISCOVERY_END_UTC env var; default unbounded."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    # --until overrides the DISCOVERY_END_UTC env var read by load_settings().
    if args.until:
        os.environ["DISCOVERY_END_UTC"] = args.until
    settings = load_settings()
    if settings.discovery_end_utc is not None:
        print(f"[discovery] end-bound active: walk capped at {settings.discovery_end_utc.isoformat()}")
    job = DiscoveryJob(settings)
    summary = job.run(
        include_agents=args.agent,
        hydration_batch_size=args.hydration_batch_size,
        hydration_shard_index=args.hydration_shard_index,
        hydration_shard_count=args.hydration_shard_count,
    )

    print("Discovery summary:")
    for key, values in summary.items():
        print(
            f"- {key}: shards={values['shards']} truncated={values['truncated_shards']} "
            f"items_seen={values['items_seen']} persisted={values['items_persisted']} "
            f"filtered_out={values['items_filtered_out']} cursor={values['new_cursor_epoch']}"
        )


if __name__ == "__main__":
    main()
