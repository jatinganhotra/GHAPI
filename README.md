# GHAPI

GitHub crawler that discovers PRs, hydrates PR/issue/review data, enriches repository popularity/footprint metrics, and writes analytics tables to Postgres.

## Local Run

```bash
pip install -r requirements.txt
python -m ghapi_crawler.migrate
python -m ghapi_crawler.pipeline \
  --agent codex \
  --agent claude
```

Environment variables are documented in `.env.example`.

## Storage model

Migration `ghapi_crawler/migrations/008_trim_payload_storage.sql` trims large payload storage:

- drops `pull_requests.body`
- drops `pull_requests.raw_search_payload`
- drops raw JSON payload columns across repository/PR timeline/review/comment/issue tables

Policy for production runs:

- store durable structured fields + derived metrics only
- keep PR URLs for traceability (`pull_requests.html_url`)
- do not persist long-lived raw payload blobs unless an explicit temporary analysis window is approved
- keep cache in memory (`GITHUB_CACHE_BACKEND=memory`) to avoid Postgres growth from cache tables

## Production GitHub Actions

Workflows:

- `.github/workflows/crawl.yml`
- `.github/workflows/crawler_alerts.yml` (failure notifications/ownership routing)

Behavior:

- Runs on schedule (`0 * * * *`) and on manual dispatch.
- Executes crawl as shard-aware jobs:
  - Discovery runs once per active agent (shard `0`).
  - Hydration runs on deterministic shards per agent.
  - Metrics runs in a follow-up job after crawl shards complete.
- Uses per-agent+shard concurrency groups to prevent overlap on the same shard across runs.
- Computes a per-run hydration throughput plan before crawl:
  - Codex can auto-scale shard count/limit based on pending backlog and pending rate-limit errors.
  - Claude agents (`claude`, `claude_head`, `claude_body`, `cross`) auto-scale shard count/limit based on pending backlog and target progress.
  - Manual dispatch overrides (`hydration_limit`, `codex_shards`, `claude_shards`) always take precedence.
  - Current shard ceiling is `12` for all agents (adaptive high-backlog profiles can scale above the baseline shard count).
- Scheduled runs execute discovery/hydration/metrics directly (no migrations in schedule).
- For reduced storage/API cost during the current phase, prefer codex/claude-only execution
  (manual dispatch with `agent`, or matrix narrowing in workflow config).
- Claude discovery uses three complementary queries, all stored as `agent_key='claude'` in the DB:
  - `claude` — `is:pr author:claude[bot]` (original; catches PRs where the bot is the GitHub author)
  - `claude_head` — `is:pr head:claude/` (branch-name prefix; catches ~2.3M PRs as of 2026-05)
  - `claude_body` — `is:pr "Generated with Claude Code"` (PR body footer; catches PRs on human-named branches where Claude Code still stamped its footer)
- Hydration includes multi-signal fallback that can reclassify PRs to `claude` when discovery identity is incomplete:
  - Commit author/committer login or email (e.g. `claude[bot]`, `@anthropic.com` domain)
  - `Co-Authored-By: Claude <noreply@anthropic.com>` trailer in commit message body (hardcoded in Claude Code system prompt; present even when attribution is disabled)
  - Session URL `https://claude.ai/code/session_<id>` in commit message body (appended by Claude Code Web even when attribution setting is off)
  - `[Claude Code](https://claude.ai/code)` footer in PR body
- Hydration queue selection now uses `FOR UPDATE SKIP LOCKED` and shard partitioning (`github_pr_id % shard_count = shard_index`) so parallel Codex shards do not process overlapping PR rows.

Migration policy:

- Apply schema migrations separately with an admin DB role (`postgres`) when new migration files are introduced.
- Keep scheduled crawler runs on the least-privilege `ingestor_writer` role.
- If migration `008_trim_payload_storage.sql` fails on `issue_timeline_events.raw_payload` missing, the table is already pruned; mark `008` in `schema_migrations` and continue with `009_repository_footprint_metrics.sql`.
- After applying `009`, run hydration to populate `repositories.stargazers_count`/`loc_estimate`; adding columns alone does not populate chart-ready metadata.

Required secrets:

- `INGESTOR_WRITER_URL` (primary)
- `GHAPI_DATABASE_URL` (legacy fallback; optional if `INGESTOR_WRITER_URL` is set)
- `GHAPI_GITHUB_TOKEN` (recommended)
- `GHAPI_MONITOR_ALERT_WEBHOOK_URL` (optional, for Slack/Teams/Pager channel alerts)

`INGESTOR_WRITER_URL` requirements:

- Supabase Session pooler host (`*.pooler.supabase.com`)
- database path must be `/postgres`
- include `sslmode=require`

Useful repository variables:

- `GHAPI_START_DATE` (ISO-8601 UTC, used when an agent has no discovery cursor)
- `GHAPI_DISCOVERY_END_DATE` (ISO-8601 UTC, discovery never stores PRs created after it)
- `GHAPI_DISCOVERY_MAX_PENDING` (default `15000`; see "Limit the discovery backlog")
- `GHAPI_DISCOVERY_DAY_STRIDES` (for example `codex=6,claude_head=8`; see "Sample days for a large agent")
- `GHAPI_HYDRATION_LIMIT` (default fallback: `900`; adaptive planner may increase/decrease for Codex unless dispatch overrides are set)
- `GHAPI_METRICS_LIMIT` (default fallback: `900`)
- `GHAPI_CODEX_HYDRATION_SHARDS` (default fallback: `6`, max supported by workflow matrix: `12`)
- `GHAPI_ADAPTIVE_HYDRATION_ENABLED` (optional, default enabled; set to `false` to disable Codex adaptive throughput planning)
- `GHAPI_GITHUB_CACHE_BACKEND` (recommended `memory`)
- `GHAPI_SEARCH_DELAY_SECONDS`
- `GHAPI_SEARCH_MAX_RETRIES`
- `GHAPI_MONITOR_OWNER` (GitHub mention for alert ownership, for example `@jatinganhotra` or `@org/team`)

Failure alert behavior:

- On non-success crawler runs, `crawler_alerts.yml` opens/updates an issue titled `[monitor] GHAPI crawler failing` with label `crawler-alert`.
- The issue body includes run URL/details and mentions `GHAPI_MONITOR_OWNER` if configured.
- If `GHAPI_MONITOR_ALERT_WEBHOOK_URL` is set, the alert workflow also posts a channel notification webhook.

Manual dispatch tips:

- `GHAPI_START_DATE` affects only agents without an existing discovery cursor.
- To backfill older history for an existing agent, use manual dispatch with both:
  - `start_date` (for example `2025-01-01T00:00:00Z`)
  - `reset_cursor=true`
- If discovery shows `items_seen=0` with a recent cursor, rerun with `reset_cursor=true` and a sufficiently old `start_date`.
- Manual dispatch supports optional inputs:
  - `agent` (single agent key)
  - `hydration_limit`
  - `metrics_limit`
  - `codex_shards` (Codex-only shard-count override, `1-12`)
  - `start_date`
  - `reset_cursor`
- If you are reclaiming space, pause scheduled runs before cleanup + vacuum, then resume after completion.

## crawl-expansion-202607: broader agent coverage

Branch `crawl-expansion-202607` (see `plans/crawl_expansion_20260704.md` in
the parent repo for the full design + volume estimates) adds agent-set
parity with the LogicStar "Agents in the Wild" dashboard
(insights.logicstar.ai):

- New discovery-time `AgentDefinition`s in `ghapi_crawler/agents.py`:
  `cosine` (`head:cosine/`, confirmed live), `tembo` (`author:tembo-io`,
  confirmed real account but currently 0 PRs — dormant, not dead), and
  `openhands` (no viable discovery query exists today; the entry only
  registers the `tracked_agents` row so hydration-time reclassification
  doesn't hit a FK violation).
- `copilot`, `cursor`, `devin`, `codegen`, `jules` were already defined
  pre-crawl-expansion but are not in `crawl.yml`'s matrix (`# Disabled
  pending stabilization`); enabling them is a separate follow-up once a live
  DB target exists (see the plan doc, section (c)/(e)).
- `ghapi_crawler/hydration.py`'s commit-signal reclassifier (previously
  Claude-only) is now table-driven (`COMMIT_AGENT_HINTS`) and also catches
  Jules (`google-labs-jules[bot]`) and OpenHands (`openhands`) via exact
  match against the commit author/committer login OR raw git author name —
  the same signal LogicStar's classifier uses. This runs on every hydrated
  PR regardless of which agent's query found it.

## DuckDB snapshot export

`scripts/export_duckdb.py` (full export) and `scripts/merge_duckdb.py`
(incremental top-up) dump the crawler's Postgres tables to a local DuckDB
file — the "dump to DuckDB periodically; save monthly snapshots" capability
from crawl-expansion-202607. Both reuse the crawler's own `DATABASE_URL` /
`INGESTOR_WRITER_URL` resolution (`ghapi_crawler.config.load_settings`).

```bash
.venv/bin/pip install duckdb==1.2.2 pytz   # not in requirements.txt; the crawler itself never needs them
.venv/bin/python scripts/export_duckdb.py                # full export -> snapshots/ghapi_latest.duckdb
.venv/bin/python scripts/merge_duckdb.py                  # cheap incremental top-up of that file
.venv/bin/python scripts/export_duckdb.py --verify \
    --output snapshots/ghapi_2026-11-15.duckdb            # dated export with a row-count check
```

`--verify` counts each source table again after the copy. If any count differs, the script
exits with status 3. A difference usually means that the crawler wrote rows during the export.
The file is still complete for each PR that it holds. Other errors exit with status 1.

### Free space after an export

The crawl runs on the Supabase free tier (500 MB). After each export,
`scripts/prune_after_export.py` deletes the exported, hydrated PRs from Postgres:

```bash
.venv/bin/python scripts/prune_after_export.py --snapshot snapshots/ghapi_2026-11-15.duckdb          # dry run: counts only
.venv/bin/python scripts/prune_after_export.py --snapshot snapshots/ghapi_2026-11-15.duckdb --apply  # delete
```

The script deletes a PR only if it is hydrated and the DuckDB file has the same
`last_hydrated_at` for it. The rows of that PR in the per-PR tables go too. Issues that the
file has and that no remaining PR links to go too. The script keeps unhydrated PRs,
repositories, and `discovery_state`. Because the check is per PR, the crawler can run during
the export and the prune.

Each deleted PR keeps one row in `pruned_pull_requests` (migration 012). Discovery skips those
PRs. The hydration target (`--target`) and the agent cap (`--agent-cap`) count them as
hydrated. So the dated DuckDB files together hold the full crawl, and each PR is in the file of
the export that deleted it.

### Daily snapshot workflow

`.github/workflows/duckdb_snapshot.yml` runs every day at 05:30 UTC, and you can also start it by
hand. It does these steps:

1. It exports the database to `ghapi_<UTC time stamp>.duckdb` with `--verify`.
2. It pushes the file to a private data repository as a plain git file, with no Git LFS. The
   secret `SNAPSHOT_REPO` (`owner/name`) names the repository. The secret `SNAPSHOT_DEPLOY_KEY`
   holds the private key of a deploy key with write access to it. GitHub refuses files over
   100 MB, so a larger export goes in 90 MB parts (`<name>.part00`, `.part01`, ...). Join them
   with `cat <name>.part* > <name>`, then compare with `sha256sum -c <name>.sha256`.
3. It runs `prune_after_export.py --apply`, but only if the push succeeded.

The free tier holds 500 MB. With one token, the crawl adds about 55 MB a day, so the prune must
run every day. The workflow never puts data in this public repository: it makes no workflow
artifact and no release. A manual run with `prune` set to false exports and pushes, and deletes
nothing.

A DELETE does not make the database files smaller. Postgres uses the free space again for new
rows, so the size stops at its highest point.

### Limit the discovery backlog

Discovery stores PRs faster than hydration can process them. Unhydrated PRs stay in Postgres,
because the prune deletes only hydrated PRs. Set `DISCOVERY_MAX_PENDING` (workflow variable
`GHAPI_DISCOVERY_MAX_PENDING`, default `15000` in `crawl.yml`) to limit the backlog. Discovery
then skips an agent while that agent has at least this many unhydrated PRs. `0` means no limit.

### Sample days for a large agent

Set `DISCOVERY_DAY_STRIDES` (workflow variable `GHAPI_DISCOVERY_DAY_STRIDES`), for example
`codex=6,claude_head=8`. Discovery for a listed agent then covers only every Nth UTC day,
counted from the start date: day 0, day N, day 2N, and so on. Agents that are not listed cover
every day. Use it with the agent cap to spread a capped agent's sample across the whole window.

## Supabase vacuum note

If Supabase SQL Editor returns `ERROR: VACUUM cannot run inside a transaction block`, that is expected.
Run vacuum from `psql` instead, one table per command, using a table-owner/admin role.

## Tests

Run the unit tests:

```bash
.venv/bin/python -m pytest -q tests -m "not integration"
```

The integration tests start a temporary Postgres cluster. They need the Postgres binaries
(`initdb`, `pg_ctl`, `psql`). Set `PG_BIN_DIR` to the folder that holds them:

```bash
PG_BIN_DIR=/opt/homebrew/bin .venv/bin/python -m pytest -q tests
```

The tests never use the database URLs in `.env`. A fixture in `tests/conftest.py` stops the
`.env` load and removes `INGESTOR_WRITER_URL` and `DATABASE_URL` before each test.

## Maintenance tools

- `python -m ghapi_crawler.backfill_eval --start <ISO> --end <ISO>` estimates the cost of a
  discovery backfill before you run it: shard count, truncated shards, and search page requests.
  Run `--help` for all the options.
- `python -m ghapi_crawler.repo_cleanup --json` lists repositories that the repo filters reject.
  Add `--apply` to delete them and their pull requests.
