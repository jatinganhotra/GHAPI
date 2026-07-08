ALTER TABLE pull_request_issue_links
    ADD COLUMN IF NOT EXISTS confidence TEXT NOT NULL DEFAULT 'medium',
    ADD COLUMN IF NOT EXISTS confidence_score NUMERIC(6,4) NOT NULL DEFAULT 0.7000,
    ADD COLUMN IF NOT EXISTS explainability JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_pull_request_issue_links_confidence
    ON pull_request_issue_links (confidence, confidence_score DESC);
