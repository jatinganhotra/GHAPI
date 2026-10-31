from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ghapi_crawler.db import configure_libpq_library_paths

configure_libpq_library_paths()


import pytest

from ghapi_crawler import config as _config


@pytest.fixture(autouse=True)
def _isolate_from_live_database(monkeypatch):
    """Keep every test away from the live database.

    load_settings() prefers INGESTOR_WRITER_URL over DATABASE_URL, and it reads
    .env on first use. A developer .env holds the live Supabase writer URL, so a
    test that sets only DATABASE_URL would still connect to Supabase. This fixture
    stops the .env load and removes both variables. Tests that need Postgres set
    DATABASE_URL to their own throwaway cluster.
    """
    monkeypatch.setattr(_config, "_DOTENV_LOADED", True)
    monkeypatch.delenv("INGESTOR_WRITER_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
