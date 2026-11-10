"""Stop a crawl job at a set time, before the runner cancels it.

GitHub Actions cancels a job when it reaches its timeout. Then the run shows as
cancelled, and the jobs that need it do not run. The workflow sets GHAPI_STOP_AT
(Unix time in seconds) to a time before the timeout. After that time:

- discovery requests no new page,
- hydration claims no new PR,
- the GitHub client starts no rate-limit wait that ends after the stop time.

The job then exits with success. The next run continues the work. Without
GHAPI_STOP_AT, nothing changes.
"""

from __future__ import annotations

import os
import time

STOP_AT_ENV = "GHAPI_STOP_AT"


class StopTimeReached(BaseException):
    """The job reached its stop time.

    It is a BaseException, as KeyboardInterrupt is. Thus an "except Exception"
    handler does not catch it, retry it, or record it as a hydration error.
    """


def stop_at() -> float | None:
    """Return the stop time from GHAPI_STOP_AT, or None if it is not set."""
    raw = (os.environ.get(STOP_AT_ENV) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{STOP_AT_ENV} must be Unix time in seconds, not {raw!r}") from None


def check(wait_seconds: float = 0.0) -> None:
    """Raise StopTimeReached if now plus wait_seconds is after the stop time."""
    limit = stop_at()
    if limit is None or time.time() + wait_seconds <= limit:
        return
    if wait_seconds > 0:
        raise StopTimeReached(f"a {wait_seconds:.0f}s rate-limit wait would end after the stop time")
    raise StopTimeReached("the stop time is reached")


def announce(job: str) -> None:
    """Print the stop time at the start of a job. An incorrect value stops the job here."""
    limit = stop_at()
    if limit is None:
        return
    minutes = (limit - time.time()) / 60
    print(f"[{job}] {STOP_AT_ENV}={limit:.0f}: the job stops in {minutes:.0f} minutes")
