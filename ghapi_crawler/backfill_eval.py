from __future__ import annotations

import argparse
import datetime as dt
import json
from math import ceil
from pathlib import Path
from typing import Any, Iterable

from ghapi_crawler.config import Settings, load_settings
from ghapi_crawler.discovery import DiscoveryJob, Shard, _select_agents


def estimate_from_shards(
    shards: list[Shard],
    *,
    search_per_page: int,
) -> dict[str, int]:
    pages_estimate = sum(min(ceil(shard.total_count / search_per_page), 10) for shard in shards)
    potential_results = sum(shard.total_count for shard in shards)
    retrievable_results = sum(min(shard.total_count, search_per_page * 10) for shard in shards)
    truncated_shards = sum(1 for shard in shards if shard.truncated)
    skipped_estimate = max(0, potential_results - retrievable_results)
    return {
        "shard_count": len(shards),
        "truncated_shard_count": truncated_shards,
        "potential_result_count": potential_results,
        "retrievable_result_count": retrievable_results,
        "estimated_skipped_count": skipped_estimate,
        "estimated_page_requests": pages_estimate,
    }


class _CountingDiscoveryJob(DiscoveryJob):
    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.count_query_calls = 0

    def _count_query(self, query: str, start: dt.datetime, end: dt.datetime) -> int:
        self.count_query_calls += 1
        return super()._count_query(query, start, end)


def evaluate_backfill_window(
    settings: Settings,
    *,
    window_start: dt.datetime,
    window_end: dt.datetime,
    include_agents: Iterable[str] | None = None,
) -> dict[str, Any]:
    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=dt.timezone.utc)
    else:
        window_start = window_start.astimezone(dt.timezone.utc)

    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=dt.timezone.utc)
    else:
        window_end = window_end.astimezone(dt.timezone.utc)

    if window_start >= window_end:
        raise ValueError("window_start must be earlier than window_end")

    selected_agents = _select_agents(include_agents)
    job = _CountingDiscoveryJob(settings)
    agent_rows: list[dict[str, Any]] = []
    total_count_queries = 0
    total_estimated_requests = 0

    try:
        for agent in selected_agents:
            before = job.count_query_calls
            shards = job._build_shards(agent.discovery_query, window_start, window_end)
            after = job.count_query_calls
            count_queries = after - before
            estimate = estimate_from_shards(shards, search_per_page=settings.search_per_page)
            estimated_total_requests = count_queries + estimate["estimated_page_requests"]

            agent_rows.append(
                {
                    "agent_key": agent.key,
                    "display_name": agent.display_name,
                    "count_query_calls": count_queries,
                    "estimated_total_requests": estimated_total_requests,
                    **estimate,
                }
            )
            total_count_queries += count_queries
            total_estimated_requests += estimated_total_requests
    finally:
        job.client.close()

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "agent_count": len(agent_rows),
        "search_per_page": settings.search_per_page,
        "total_count_query_calls": total_count_queries,
        "total_estimated_requests": total_estimated_requests,
        "agents": agent_rows,
    }


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Estimate historical backfill effort for PR discovery "
            "(count-only, no database writes)."
        )
    )
    parser.add_argument(
        "--start",
        default="2025-01-01T00:00:00Z",
        help="Backfill evaluation start timestamp (UTC).",
    )
    parser.add_argument(
        "--end",
        default="2025-12-31T23:59:59Z",
        help="Backfill evaluation end timestamp (UTC).",
    )
    parser.add_argument(
        "--agent",
        action="append",
        help="Optional agent key filter (repeatable). Defaults to all tracked agents.",
    )
    parser.add_argument(
        "--output",
        help="Optional path to write JSON output.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = load_settings()
    report = evaluate_backfill_window(
        settings=settings,
        window_start=_parse_utc_timestamp(args.start),
        window_end=_parse_utc_timestamp(args.end),
        include_agents=args.agent,
    )

    payload = json.dumps(report, indent=2)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload)
        print(f"Wrote backfill evaluation: {output_path}")
    else:
        print(payload)


if __name__ == "__main__":
    main()
