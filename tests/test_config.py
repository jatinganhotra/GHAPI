from ghapi_crawler.config import load_settings


def test_load_settings_parses_repo_patterns(monkeypatch):
    monkeypatch.setenv("REPO_ALLOWLIST", "openai/*, aavetis/*")
    monkeypatch.setenv("REPO_DENYLIST", "*/fork-*,acme/noisy")
    monkeypatch.setenv("GITHUB_CACHE_ENABLED", "false")
    monkeypatch.setenv("GITHUB_CACHE_TTL_SECONDS", "120")
    monkeypatch.setenv("GITHUB_CACHE_MAX_ENTRIES", "1234")
    monkeypatch.setenv("GITHUB_CACHE_BACKEND", "postgres")
    monkeypatch.setenv("GITHUB_CACHE_PERSISTENT_MAX_ENTRIES", "4321")
    monkeypatch.setenv("GITHUB_CACHE_PERSISTENT_CLEANUP_INTERVAL_SECONDS", "77")
    monkeypatch.setenv("GITHUB_CACHE_REDIS_URL", "redis://127.0.0.1:6380/1")
    monkeypatch.setenv("GITHUB_CACHE_REDIS_KEY_PREFIX", "ghapi_crawler:test-cache")
    settings = load_settings()
    assert settings.repo_allowlist_patterns == ("openai/*", "aavetis/*")
    assert settings.repo_denylist_patterns == ("*/fork-*", "acme/noisy")
    assert settings.github_cache_enabled is False
    assert settings.github_cache_ttl_seconds == 120
    assert settings.github_cache_max_entries == 1234
    assert settings.github_cache_backend == "postgres"
    assert settings.github_cache_persistent_max_entries == 4321
    assert settings.github_cache_persistent_cleanup_interval_seconds == 77
    assert settings.github_cache_redis_url == "redis://127.0.0.1:6380/1"
    assert settings.github_cache_redis_key_prefix == "ghapi_crawler:test-cache"


def test_load_settings_accepts_redis_cache_backend(monkeypatch):
    monkeypatch.setenv("GITHUB_CACHE_BACKEND", "redis")
    settings = load_settings()
    assert settings.github_cache_backend == "redis"

