ALTER TABLE issue_timeline_events
    ADD COLUMN IF NOT EXISTS assigned_login TEXT;

UPDATE issue_timeline_events
SET assigned_login = COALESCE(
    assigned_login,
    raw_payload->'assignee'->>'login',
    CASE
        WHEN jsonb_typeof(raw_payload->'assignees') = 'array'
            THEN raw_payload->'assignees'->0->>'login'
        ELSE NULL
    END
)
WHERE event_type = 'assigned'
  AND assigned_login IS NULL;

ALTER TABLE repositories
    DROP COLUMN IF EXISTS raw_payload;

ALTER TABLE pull_requests
    DROP COLUMN IF EXISTS body,
    DROP COLUMN IF EXISTS raw_search_payload;

ALTER TABLE pull_request_files
    DROP COLUMN IF EXISTS raw_payload;

ALTER TABLE pull_request_reviews
    DROP COLUMN IF EXISTS raw_payload;

ALTER TABLE pull_request_comments
    DROP COLUMN IF EXISTS raw_payload;

ALTER TABLE pull_request_review_comments
    DROP COLUMN IF EXISTS raw_payload;

ALTER TABLE pull_request_timeline_events
    DROP COLUMN IF EXISTS raw_payload;

ALTER TABLE issues
    DROP COLUMN IF EXISTS raw_payload;

ALTER TABLE issue_comments
    DROP COLUMN IF EXISTS raw_payload;

ALTER TABLE issue_timeline_events
    DROP COLUMN IF EXISTS raw_payload;
