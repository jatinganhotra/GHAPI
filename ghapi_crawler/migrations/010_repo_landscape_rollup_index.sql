-- Improve /api/repo-landscape rollup scans on pull_requests.
-- This index supports grouping by repo and agent while filtering recent rows.
CREATE INDEX IF NOT EXISTS idx_pull_requests_repo_agent_created_hydrated
    ON pull_requests (repo_full_name, agent_key, created_at DESC)
    INCLUDE (last_hydrated_at);
