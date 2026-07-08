ALTER TABLE pull_requests
    ADD COLUMN IF NOT EXISTS additions INTEGER,
    ADD COLUMN IF NOT EXISTS deletions INTEGER,
    ADD COLUMN IF NOT EXISTS changed_files INTEGER,
    ADD COLUMN IF NOT EXISTS commits INTEGER,
    ADD COLUMN IF NOT EXISTS review_comments INTEGER,
    ADD COLUMN IF NOT EXISTS comments_count INTEGER,
    ADD COLUMN IF NOT EXISTS last_hydrated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS hydration_error TEXT;

CREATE TABLE IF NOT EXISTS pull_request_files (
    github_pr_id BIGINT NOT NULL REFERENCES pull_requests(github_pr_id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    status TEXT,
    additions INTEGER,
    deletions INTEGER,
    changes INTEGER,
    file_sha TEXT,
    previous_filename TEXT,
    file_extension TEXT,
    language TEXT,
    is_test_file BOOLEAN NOT NULL DEFAULT FALSE,
    hunk_count_total INTEGER NOT NULL DEFAULT 0,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (github_pr_id, filename)
);

CREATE INDEX IF NOT EXISTS idx_pull_request_files_pr
    ON pull_request_files (github_pr_id);

CREATE INDEX IF NOT EXISTS idx_pull_request_files_language
    ON pull_request_files (language);

CREATE TABLE IF NOT EXISTS pull_request_reviews (
    review_id BIGINT PRIMARY KEY,
    github_pr_id BIGINT NOT NULL REFERENCES pull_requests(github_pr_id) ON DELETE CASCADE,
    user_login TEXT,
    state TEXT,
    body TEXT,
    submitted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_pull_request_reviews_pr
    ON pull_request_reviews (github_pr_id);

CREATE TABLE IF NOT EXISTS pull_request_comments (
    comment_id BIGINT PRIMARY KEY,
    github_pr_id BIGINT NOT NULL REFERENCES pull_requests(github_pr_id) ON DELETE CASCADE,
    user_login TEXT,
    body TEXT,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_pull_request_comments_pr
    ON pull_request_comments (github_pr_id);

CREATE TABLE IF NOT EXISTS pull_request_review_comments (
    comment_id BIGINT PRIMARY KEY,
    github_pr_id BIGINT NOT NULL REFERENCES pull_requests(github_pr_id) ON DELETE CASCADE,
    user_login TEXT,
    body TEXT,
    path TEXT,
    line INTEGER,
    side TEXT,
    commit_id TEXT,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_pull_request_review_comments_pr
    ON pull_request_review_comments (github_pr_id);

CREATE TABLE IF NOT EXISTS pull_request_timeline_events (
    event_key TEXT PRIMARY KEY,
    github_pr_id BIGINT NOT NULL REFERENCES pull_requests(github_pr_id) ON DELETE CASCADE,
    event_type TEXT,
    actor_login TEXT,
    created_at TIMESTAMPTZ,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_pull_request_timeline_events_pr
    ON pull_request_timeline_events (github_pr_id);

CREATE TABLE IF NOT EXISTS issues (
    github_issue_id BIGINT PRIMARY KEY,
    repo_full_name TEXT NOT NULL REFERENCES repositories(repo_full_name),
    issue_number INTEGER NOT NULL,
    state TEXT,
    title TEXT,
    body TEXT,
    body_word_count INTEGER NOT NULL DEFAULT 0,
    author_login TEXT,
    comments_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (repo_full_name, issue_number)
);

CREATE INDEX IF NOT EXISTS idx_issues_repo_number
    ON issues (repo_full_name, issue_number DESC);

CREATE TABLE IF NOT EXISTS issue_comments (
    comment_id BIGINT PRIMARY KEY,
    github_issue_id BIGINT NOT NULL REFERENCES issues(github_issue_id) ON DELETE CASCADE,
    user_login TEXT,
    body TEXT,
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_issue_comments_issue
    ON issue_comments (github_issue_id);

CREATE TABLE IF NOT EXISTS issue_timeline_events (
    event_key TEXT PRIMARY KEY,
    github_issue_id BIGINT NOT NULL REFERENCES issues(github_issue_id) ON DELETE CASCADE,
    event_type TEXT,
    actor_login TEXT,
    created_at TIMESTAMPTZ,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_issue_timeline_events_issue
    ON issue_timeline_events (github_issue_id);

CREATE TABLE IF NOT EXISTS pull_request_issue_links (
    github_pr_id BIGINT NOT NULL REFERENCES pull_requests(github_pr_id) ON DELETE CASCADE,
    github_issue_id BIGINT NOT NULL REFERENCES issues(github_issue_id) ON DELETE CASCADE,
    link_type TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (github_pr_id, github_issue_id, source)
);

CREATE INDEX IF NOT EXISTS idx_pull_request_issue_links_pr
    ON pull_request_issue_links (github_pr_id);

