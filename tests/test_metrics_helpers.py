import datetime as dt

from ghapi_crawler.metrics import _seconds_between


def test_seconds_between_happy_path():
    start = dt.datetime(2026, 1, 1, 0, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 1, 1, 1, 0, tzinfo=dt.timezone.utc)
    assert _seconds_between(start, end) == 3600


def test_seconds_between_missing_or_negative():
    start = dt.datetime(2026, 1, 1, 1, 0, tzinfo=dt.timezone.utc)
    end = dt.datetime(2026, 1, 1, 0, 0, tzinfo=dt.timezone.utc)
    assert _seconds_between(start, end) is None
    assert _seconds_between(None, end) is None
    assert _seconds_between(start, None) is None

