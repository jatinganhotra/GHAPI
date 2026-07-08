CREATE TABLE IF NOT EXISTS pr_metrics (
    github_pr_id BIGINT PRIMARY KEY REFERENCES pull_requests(github_pr_id) ON DELETE CASCADE,
    agent_key TEXT NOT NULL REFERENCES tracked_agents(agent_key),
    repo_full_name TEXT NOT NULL REFERENCES repositories(repo_full_name),
    pr_number INTEGER NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    files_total INTEGER NOT NULL DEFAULT 0,
    files_test INTEGER NOT NULL DEFAULT 0,
    files_code INTEGER NOT NULL DEFAULT 0,
    hunks_total INTEGER NOT NULL DEFAULT 0,
    hunks_test INTEGER NOT NULL DEFAULT 0,
    hunks_code INTEGER NOT NULL DEFAULT 0,
    linked_issue_count INTEGER NOT NULL DEFAULT 0,
    has_multiple_linked_issues BOOLEAN NOT NULL DEFAULT FALSE,
    issue_word_count_total INTEGER NOT NULL DEFAULT 0,
    issue_comment_count INTEGER NOT NULL DEFAULT 0,
    issue_has_comments BOOLEAN NOT NULL DEFAULT FALSE,
    pr_comment_count INTEGER NOT NULL DEFAULT 0,
    pr_review_count INTEGER NOT NULL DEFAULT 0,
    pr_review_comment_count INTEGER NOT NULL DEFAULT 0,
    pr_has_comments BOOLEAN NOT NULL DEFAULT FALSE,
    pr_has_reviews BOOLEAN NOT NULL DEFAULT FALSE,
    pr_has_changes_requested BOOLEAN NOT NULL DEFAULT FALSE,
    pr_merged_without_changes_requested BOOLEAN NOT NULL DEFAULT FALSE,
    issue_first_created_at TIMESTAMPTZ,
    issue_first_assigned_at TIMESTAMPTZ,
    first_review_at TIMESTAMPTZ,
    seconds_issue_created_to_assigned BIGINT,
    seconds_issue_assigned_to_pr_created BIGINT,
    seconds_pr_created_to_first_review BIGINT,
    seconds_first_review_to_merged BIGINT,
    seconds_pr_created_to_merged BIGINT,
    raw_debug JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_pr_metrics_agent
    ON pr_metrics (agent_key, computed_at DESC);

CREATE INDEX IF NOT EXISTS idx_pr_metrics_repo
    ON pr_metrics (repo_full_name, computed_at DESC);

