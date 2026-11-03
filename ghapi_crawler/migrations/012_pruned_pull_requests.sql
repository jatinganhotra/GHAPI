-- Tombstones for pull requests that left Supabase after a verified local export.
--
-- The crawl stays on the Supabase free tier. Every 14 days the hydrated PRs go to
-- a local DuckDB file, and scripts/prune_after_export.py deletes them here. This
-- table keeps one small row per deleted PR, for two reasons:
--   1. Discovery skips a PR that is in this table, so it does not hydrate it again.
--   2. The hydration target and the agent cap count these rows as hydrated.
CREATE TABLE IF NOT EXISTS pruned_pull_requests (
    github_pr_id BIGINT PRIMARY KEY,
    agent_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    last_hydrated_at TIMESTAMPTZ NOT NULL,
    pruned_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pruned_pull_requests_agent_created
    ON pruned_pull_requests (agent_key, created_at);
