CREATE TABLE IF NOT EXISTS github_api_cache (
    cache_key TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_accessed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_github_api_cache_expires_at
    ON github_api_cache (expires_at);

CREATE INDEX IF NOT EXISTS idx_github_api_cache_last_accessed_at
    ON github_api_cache (last_accessed_at);
