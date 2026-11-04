from __future__ import annotations

import dataclasses
import datetime as dt

import psycopg
import pytest

from ghapi_crawler.agents import AGENTS
from ghapi_crawler.config import _parse_day_strides
from ghapi_crawler.db import get_discovery_cursor
from ghapi_crawler.discovery import DiscoveryJob, sampled_day_window
from tests.test_discovery_max_pending import _seed_unhydrated
from tests.test_discovery_sharding import _settings

UTC = dt.timezone.utc
OCT1 = dt.datetime(2026, 10, 1, tzinfo=UTC)
NOV1 = dt.datetime(2026, 11, 1, tzinfo=UTC)
CODEX = next(a for a in AGENTS if a.key == "codex")


def _day(n: int, hour: int = 0) -> dt.datetime:
    return OCT1 + dt.timedelta(days=n, hours=hour)


def test_parse_day_strides():
    assert _parse_day_strides(None) == ()
    assert _parse_day_strides(" codex=6, claude_head=8 ") == (("codex", 6), ("claude_head", 8))
    with pytest.raises(ValueError):
        _parse_day_strides("codex")
    with pytest.raises(ValueError):
        _parse_day_strides("codex=0")


def test_sampled_day_window():
    # No cursor: the first sampled day.
    assert sampled_day_window(OCT1, None, OCT1, 6) == (_day(0), _day(1))
    # Cursor inside a sampled day: continue that day from window_start.
    assert sampled_day_window(OCT1, _day(6, 10), _day(6, 9), 6) == (_day(6, 9), _day(7))
    # window_start before the day (overlap): clip to the day start.
    assert sampled_day_window(OCT1, _day(6, 0), _day(5, 23), 6) == (_day(6), _day(7))
    # Cursor at the end of a sampled day: the next sampled day.
    assert sampled_day_window(OCT1, _day(7), _day(6, 23), 6) == (_day(12), _day(13))
    # Cursor on a day that is not sampled: the next sampled day.
    assert sampled_day_window(OCT1, _day(9, 5), _day(9, 4), 6) == (_day(12), _day(13))


class _Job(DiscoveryJob):
    """Each window has one PR, created one hour after the window start."""

    def __init__(self, settings):
        super().__init__(settings)
        self.windows: list[tuple[dt.datetime, dt.datetime]] = []
        self.client.search_pull_requests = self._search

    def _count_query(self, query, start, end):
        return 1

    def _search(self, query, created_from, created_to, page, per_page):
        self.windows.append((created_from, created_to))
        n = len(self.windows)
        created = (created_from + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "items": [
                {
                    "id": 1000 + n,
                    "number": n,
                    "title": "t",
                    "state": "open",
                    "user": {"login": "dev"},
                    "html_url": f"https://github.com/o/r/pull/{n}",
                    "url": f"https://api.github.com/repos/o/r/issues/{n}",
                    "created_at": created,
                    "updated_at": created,
                    "repository_url": "https://api.github.com/repos/o/r",
                    "pull_request": {"url": f"https://api.github.com/repos/o/r/pulls/{n}"},
                }
            ]
        }


def _job(dsn: str, max_window_hours: int) -> _Job:
    settings = dataclasses.replace(
        _settings(),
        database_url=dsn,
        discovery_start_utc=OCT1,
        discovery_end_utc=NOV1,
        discovery_max_window_hours=max_window_hours,
        discovery_day_strides=(("codex", 6),),
    )
    return _Job(settings)


def _cursor(dsn: str) -> dt.datetime | None:
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return get_discovery_cursor(conn, "codex")


@pytest.mark.integration
def test_discovery_walks_only_the_sampled_days(temp_postgres):
    _seed_unhydrated(temp_postgres, 0)
    job = _job(temp_postgres, max_window_hours=0)
    for _ in range(7):
        job._run_agent(CODEX)
    starts = [start for start, _ in job.windows]
    assert starts == [_day(0), _day(6), _day(12), _day(18), _day(24), _day(30)]
    assert all(end - start == dt.timedelta(days=1) for start, end in job.windows)
    assert _cursor(temp_postgres) >= NOV1 - dt.timedelta(days=1)


@pytest.mark.integration
def test_capped_windows_finish_the_day_then_move_on(temp_postgres):
    _seed_unhydrated(temp_postgres, 0)
    job = _job(temp_postgres, max_window_hours=16)
    job._run_agent(CODEX)  # 00:00-16:00, no truncated shard: the cursor moves to 16:00
    assert _cursor(temp_postgres) == _day(0, 16)
    job._run_agent(CODEX)  # 15:00 (60 min overlap) to the day end
    assert job.windows[-1] == (_day(0, 15), _day(1))
    assert _cursor(temp_postgres) == _day(1)
    job._run_agent(CODEX)  # the next sampled day
    assert job.windows[-1] == (_day(6), _day(6, 16))
