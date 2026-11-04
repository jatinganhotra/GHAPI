-- Let the crawler role write pruned_pull_requests when row-level security is on.
--
-- Supabase turns on row-level security for each new table. Migration 012 made
-- pruned_pull_requests with no policy, so the prune failed for ingestor_writer:
-- "new row violates row-level security policy for table pruned_pull_requests".
-- The other crawl tables have row-level security off.
--
-- A database without the role (a local test cluster) skips the policy.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ingestor_writer') THEN
        DROP POLICY IF EXISTS ingestor_writer_all ON pruned_pull_requests;
        CREATE POLICY ingestor_writer_all ON pruned_pull_requests
            FOR ALL TO ingestor_writer USING (true) WITH CHECK (true);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_reader') THEN
        DROP POLICY IF EXISTS dashboard_reader_select ON pruned_pull_requests;
        CREATE POLICY dashboard_reader_select ON pruned_pull_requests
            FOR SELECT TO dashboard_reader USING (true);
    END IF;
END
$$;
