from __future__ import annotations

import argparse

from ghapi_crawler.config import load_settings
from ghapi_crawler.discovery import DiscoveryJob
from ghapi_crawler.hydration import HydrationJob
from ghapi_crawler.metrics import MetricJob
from ghapi_crawler.migrate import run_migrations


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run GHAPI crawler pipeline")
    parser.add_argument("--hydration-limit", type=int, default=250)
    parser.add_argument("--metrics-limit", type=int, default=750)
    parser.add_argument("--hydration-shard-index", type=int, default=0)
    parser.add_argument("--hydration-shard-count", type=int, default=1)
    parser.add_argument(
        "--agent",
        action="append",
        default=None,
        help="Optional agent key filter (repeatable).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.hydration_shard_count < 1:
        raise SystemExit(
            f"--hydration-shard-count must be >= 1; received {args.hydration_shard_count}"
        )
    if (
        args.hydration_shard_index < 0
        or args.hydration_shard_index >= args.hydration_shard_count
    ):
        raise SystemExit(
            "--hydration-shard-index must be in "
            f"[0, {args.hydration_shard_count - 1}]; received {args.hydration_shard_index}"
        )
    settings = load_settings()

    applied = run_migrations()
    print(f"Migrations applied: {applied}")

    discovery_summary = DiscoveryJob(settings).run(include_agents=args.agent)
    print(f"Discovery complete: {discovery_summary}")

    hydration_summary = HydrationJob(settings).run(
        limit=args.hydration_limit,
        include_agents=args.agent,
        shard_index=args.hydration_shard_index,
        shard_count=args.hydration_shard_count,
    )
    print(f"Hydration complete: {hydration_summary}")

    metrics_summary = MetricJob(settings).run(limit=args.metrics_limit, include_agents=args.agent)
    print(f"Metrics complete: {metrics_summary}")


if __name__ == "__main__":
    main()
