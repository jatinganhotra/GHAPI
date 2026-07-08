ALTER TABLE repositories
    ADD COLUMN IF NOT EXISTS stargazers_count BIGINT,
    ADD COLUMN IF NOT EXISTS forks_count BIGINT,
    ADD COLUMN IF NOT EXISTS watchers_count BIGINT,
    ADD COLUMN IF NOT EXISTS open_issues_count BIGINT,
    ADD COLUMN IF NOT EXISTS size_kb BIGINT,
    ADD COLUMN IF NOT EXISTS default_branch TEXT,
    ADD COLUMN IF NOT EXISTS pushed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS file_count BIGINT,
    ADD COLUMN IF NOT EXISTS blob_size_bytes BIGINT,
    ADD COLUMN IF NOT EXISTS tree_truncated BOOLEAN,
    ADD COLUMN IF NOT EXISTS loc_estimate BIGINT,
    ADD COLUMN IF NOT EXISTS metadata_refreshed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_repositories_stargazers_count
    ON repositories (stargazers_count DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS idx_repositories_metadata_refreshed
    ON repositories (metadata_refreshed_at DESC NULLS LAST);
