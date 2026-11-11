from __future__ import annotations

import dataclasses
import datetime as dt

import psycopg
import pytest

from ghapi_crawler.agents import AGENTS
from ghapi_crawler.config import _parse_discovery_samples, load_settings
from ghapi_crawler.db import get_discovery_cursor
from ghapi_crawler.discovery import DiscoveryJob, sampled_window
from tests.test_discovery_max_pending import _seed_unhydrated
from tests.test_discovery_sharding import _settings

UTC = dt.timezone.utc
OCT1 = dt.datetime(2026, 10, 1, tzinfo=UTC)
NOV1 = dt.datetime(2026, 11, 1, tzinfo=UTC)
CODEX = next(a for a in AGENTS if a.key == "codex")


def _day(n: int, hour: int = 0) -> dt.datetime:
    return OCT1 + dt.timedelta(days=n, hours=hour)


def _hour(n: int) -> dt.datetime:
    return OCT1 + dt.timedelta(hours=n)


DAY = dt.timedelta(days=1)
HOUR = dt.timedelta(hours=1)


def test_parse_days_and_hour_windows():
    assert _parse_discovery_samples(None) == ()
    assert _parse_discovery_samples(" codex=6, claude_head=8 ") == (
        ("codex", 24, 144),
        ("claude_head", 24, 192),
    )
    assert _parse_discovery_samples("codex=8h/124h,claude_head=4h/124h,cursor=24h/124h") == (
        ("codex", 8, 124),
        ("claude_head", 4, 124),
        ("cursor", 24, 124),
    )


@pytest.mark.parametrize(
    "value",
    ["codex", "codex=0", "codex=x", "codex=4h", "codex=4/124", "codex=4h/124", "codex=0h/124h",
     "codex=8h/4h", "=4h/124h"],
)
def test_parse_refuses_an_incorrect_value(value):
    with pytest.raises(ValueError):
        _parse_discovery_samples(value)


def test_load_settings_reads_the_new_name_first(monkeypatch):
    monkeypatch.setenv("DISCOVERY_DAY_STRIDES", "codex=6")
    assert load_settings().discovery_samples == (("codex", 24, 144),)
    monkeypatch.setenv("DISCOVERY_SAMPLES", "codex=8h/124h")
    assert load_settings().discovery_samples == (("codex", 8, 124),)


def test_sample_window_setting():
    settings = dataclasses.replace(
        _settings(), discovery_samples=(("codex", 24, 144), ("cursor", 24, 24))
    )
    assert settings.sample_window("codex") == (DAY, 6 * DAY)
    # A window as long as its step covers the full period.
    assert settings.sample_window("cursor") is None
    assert settings.sample_window("devin") is None


def test_sampled_window_with_days():
    def window(cursor, start):
        return sampled_window(OCT1, cursor, start, DAY, 6 * DAY)

    # No cursor: the first sampled day.
    assert window(None, OCT1) == (_day(0), _day(1))
    # Cursor inside a sampled day: continue that day from window_start.
    assert window(_day(6, 10), _day(6, 9)) == (_day(6, 9), _day(7))
    # window_start before the day (overlap): clip to the day start.
    assert window(_day(6, 0), _day(5, 23)) == (_day(6), _day(7))
    # Cursor at the end of a sampled day: the next sampled day.
    assert window(_day(7), _day(6, 23)) == (_day(12), _day(13))
    # Cursor on a day that is not sampled: the next sampled day.
    assert window(_day(9, 5), _day(9, 4)) == (_day(12), _day(13))


def test_sampled_window_with_hours():
    def window(cursor, start):
        return sampled_window(OCT1, cursor, start, 4 * HOUR, 124 * HOUR)

    assert window(None, OCT1) == (_hour(0), _hour(4))
    # A cursor after the first window (Oct 1, 12:30): the second window, Oct 6 04:00-08:00.
    assert window(_hour(12) + 30 * dt.timedelta(minutes=1), _hour(11)) == (_hour(124), _hour(128))
    # Cursor inside the third window: continue it.
    assert window(_hour(249), _hour(248) + 30 * dt.timedelta(minutes=1)) == (
        _hour(248) + 30 * dt.timedelta(minutes=1),
        _hour(252),
    )
    # Cursor at the end of the third window: the fourth window, Oct 16 12:00-16:00.
    assert window(_hour(252), _hour(251)) == (_day(15, 12), _day(15, 16))
    # After the sixth window (Oct 26 20:00-24:00) the next window starts on Nov 1.
    assert window(_day(26), _day(25, 23)) == (NOV1, NOV1 + 4 * HOUR)


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


def _job(dsn: str, max_window_hours: int, samples=(("codex", 24, 144),)) -> _Job:
    settings = dataclasses.replace(
        _settings(),
        database_url=dsn,
        discovery_start_utc=OCT1,
        discovery_end_utc=NOV1,
        discovery_max_window_hours=max_window_hours,
        discovery_samples=samples,
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


@pytest.mark.integration
def test_discovery_walks_only_the_hour_windows(temp_postgres):
    _seed_unhydrated(temp_postgres, 0)
    job = _job(temp_postgres, max_window_hours=16, samples=(("codex", 4, 124),))
    for _ in range(7):
        job._run_agent(CODEX)
    starts = [start for start, _ in job.windows]
    # Six windows, each 4 hours, each at a different time of day. The seventh
    # window starts on Nov 1, after the end of the period.
    assert starts == [_hour(124 * k) for k in range(6)]
    assert sorted(start.hour for start in starts) == [0, 4, 8, 12, 16, 20]
    assert all(end - start == 4 * HOUR for start, end in job.windows)
    assert _cursor(temp_postgres) == NOV1
