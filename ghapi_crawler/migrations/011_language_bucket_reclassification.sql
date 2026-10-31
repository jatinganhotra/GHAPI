-- Reclassify legacy "Other" and NULL language rows into concrete language labels,
-- "Non-code", or "Unknown".

UPDATE pull_request_files
SET language = CASE
    WHEN file_extension = '.mjs' THEN 'JavaScript'
    WHEN file_extension = '.cjs' THEN 'JavaScript'
    WHEN file_extension = '.dart' THEN 'Dart'
    WHEN file_extension = '.astro' THEN 'Astro'
    WHEN file_extension = '.ex' THEN 'Elixir'
    WHEN file_extension = '.exs' THEN 'Elixir'
    WHEN file_extension = '.lean' THEN 'Lean'
    WHEN file_extension = '.as' THEN 'ActionScript'
    WHEN file_extension = '.csproj' THEN 'XML'
    WHEN file_extension = '.props' THEN 'XML'
    WHEN file_extension = '.targets' THEN 'XML'
    WHEN file_extension = '.mdx' THEN 'Markdown'
    ELSE language
END
WHERE (language IS NULL OR language = 'Other')
  AND file_extension IN (
      '.mjs', '.cjs', '.dart', '.astro', '.ex', '.exs', '.lean', '.as',
      '.csproj', '.props', '.targets', '.mdx'
  );

UPDATE pull_request_files
SET language = 'Non-code'
WHERE (language IS NULL OR language = 'Other')
  AND (
      file_extension IN (
          '.txt', '.csv', '.tsv', '.tab', '.log', '.lock', '.example',
          '.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.bmp', '.svg',
          '.pdf', '.zip', '.gz', '.tgz', '.tar', '.7z',
          '.dll', '.so', '.dylib', '.woff', '.woff2', '.ttf', '.otf', '.eot'
      )
      OR lower(filename) IN ('license', 'copying', 'notice', 'authors', 'contributors', 'changelog')
      OR lower(filename) LIKE 'readme%'
  );

UPDATE pull_request_files
SET language = 'Unknown'
WHERE language IS NULL OR language = 'Other';

DO $$
BEGIN
    EXECUTE $view$
        CREATE OR REPLACE VIEW v_agent_language_distribution AS
        SELECT
            p.agent_key,
            COALESCE(f.language, 'Unknown') AS language,
            COUNT(*)::bigint AS file_count,
            COUNT(DISTINCT f.github_pr_id)::bigint AS pr_count
        FROM pull_request_files f
        JOIN pull_requests p ON p.github_pr_id = f.github_pr_id
        GROUP BY 1, 2
    $view$;
EXCEPTION
    WHEN insufficient_privilege THEN
        RAISE NOTICE 'Skipping v_agent_language_distribution refresh (insufficient privilege).';
END $$;
