ALTER TABLE pr_metrics
    ADD COLUMN IF NOT EXISTS issue_assigned_login TEXT,
    ADD COLUMN IF NOT EXISTS issue_assignment_source TEXT,
    ADD COLUMN IF NOT EXISTS issue_assignment_confidence TEXT;

