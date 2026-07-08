CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tracked_agents (
    agent_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    discovery_query TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS repositories (
    repo_full_name TEXT PRIMARY KEY,
    owner_login TEXT NOT NULL,
    repo_name TEXT NOT NULL,
    api_url TEXT NOT NULL UNIQUE,
    html_url TEXT NOT NULL,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pull_requests (
    github_pr_id BIGINT PRIMARY KEY,
    repo_full_name TEXT NOT NULL REFERENCES repositories(repo_full_name),
    pr_number INTEGER NOT NULL,
    agent_key TEXT NOT NULL REFERENCES tracked_agents(agent_key),
    node_id TEXT,
    state TEXT,
    draft BOOLEAN,
    title TEXT,
    body TEXT,
    author_login TEXT,
    html_url TEXT NOT NULL,
    api_url TEXT NOT NULL,
    pr_api_url TEXT,
    diff_url TEXT,
    patch_url TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ,
    merged_at TIMESTAMPTZ,
    raw_search_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (repo_full_name, pr_number)
);

CREATE TABLE IF NOT EXISTS discovery_state (
    agent_key TEXT PRIMARY KEY REFERENCES tracked_agents(agent_key),
    cursor_created_at TIMESTAMPTZ NOT NULL,
    notes TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id BIGSERIAL PRIMARY KEY,
    job_name TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running',
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_pull_requests_agent_created
    ON pull_requests (agent_key, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_pull_requests_repo_number
    ON pull_requests (repo_full_name, pr_number DESC);

CREATE INDEX IF NOT EXISTS idx_pull_requests_merged
    ON pull_requests (merged_at DESC NULLS LAST);

