CREATE OR REPLACE VIEW v_agent_daily_metrics AS
SELECT
    DATE_TRUNC('day', p.created_at)::date AS day_utc,
    p.agent_key,
    COUNT(*)::bigint AS pr_count,
    COUNT(*) FILTER (WHERE p.merged_at IS NOT NULL)::bigint AS merged_count,
    AVG(m.files_total)::numeric(12,2) AS avg_files_total,
    AVG(m.hunks_total)::numeric(12,2) AS avg_hunks_total,
    AVG(m.linked_issue_count)::numeric(12,2) AS avg_linked_issue_count,
    AVG(m.seconds_pr_created_to_first_review)::numeric(12,2) AS avg_seconds_pr_created_to_first_review,
    AVG(m.seconds_pr_created_to_merged)::numeric(12,2) AS avg_seconds_pr_created_to_merged
FROM pr_metrics m
JOIN pull_requests p ON p.github_pr_id = m.github_pr_id
GROUP BY 1, 2;

CREATE OR REPLACE VIEW v_agent_language_distribution AS
SELECT
    p.agent_key,
    COALESCE(f.language, 'Other') AS language,
    COUNT(*)::bigint AS file_count,
    COUNT(DISTINCT f.github_pr_id)::bigint AS pr_count
FROM pull_request_files f
JOIN pull_requests p ON p.github_pr_id = f.github_pr_id
GROUP BY 1, 2;

CREATE OR REPLACE VIEW v_agent_overview AS
SELECT
    p.agent_key,
    COUNT(*)::bigint AS pr_count,
    COUNT(*) FILTER (WHERE p.merged_at IS NOT NULL)::bigint AS merged_count,
    AVG(m.files_total)::numeric(12,2) AS avg_files_total,
    AVG(m.hunks_total)::numeric(12,2) AS avg_hunks_total,
    AVG(m.linked_issue_count)::numeric(12,2) AS avg_linked_issue_count,
    AVG(m.issue_word_count_total)::numeric(12,2) AS avg_issue_word_count_total,
    AVG(m.issue_comment_count)::numeric(12,2) AS avg_issue_comment_count,
    AVG(m.seconds_issue_created_to_assigned)::numeric(12,2) AS avg_seconds_issue_created_to_assigned,
    AVG(m.seconds_issue_assigned_to_pr_created)::numeric(12,2) AS avg_seconds_issue_assigned_to_pr_created,
    AVG(m.seconds_pr_created_to_first_review)::numeric(12,2) AS avg_seconds_pr_created_to_first_review,
    AVG(m.seconds_first_review_to_merged)::numeric(12,2) AS avg_seconds_first_review_to_merged,
    AVG(m.seconds_pr_created_to_merged)::numeric(12,2) AS avg_seconds_pr_created_to_merged
FROM pr_metrics m
JOIN pull_requests p ON p.github_pr_id = m.github_pr_id
GROUP BY 1;

