from dataclasses import dataclass


@dataclass(frozen=True)
class AgentDefinition:
    key: str
    display_name: str
    discovery_query: str
    assignment_logins: tuple[str, ...] = ()
    assignment_tokens: tuple[str, ...] = ()
    # When set, PRs are stored under this agent_key in the DB instead of `key`.
    # The cursor and tracked_agents entry still use `key`.
    db_key: str | None = None


# Keep this aligned with PRarena/collect_data.py query identity for v1 parity.
AGENTS: tuple[AgentDefinition, ...] = (
    AgentDefinition(
        key="copilot",
        display_name="GitHub Copilot coding agent",
        discovery_query="is:pr head:copilot/",
        assignment_tokens=("copilot",),
    ),
    AgentDefinition(
        key="codex",
        display_name="OpenAI Codex",
        discovery_query="is:pr head:codex/",
        assignment_tokens=("codex",),
    ),
    AgentDefinition(
        key="claude",
        display_name="Anthropic Claude",
        discovery_query="is:pr author:claude[bot]",
        assignment_logins=(
            "claude[bot]",
            "claude-app[bot]",
            "anthropic-ai[bot]",
            "claude-dev[bot]",
        ),
        assignment_tokens=("claude", "anthropic"),
    ),
    # Discovers Claude Code PRs via branch-name pattern (head:claude/).
    # author:claude[bot] only captures ~13K PRs; head:claude/ captures ~2.3M.
    # Stores PRs as agent_key='claude' in the DB (same pool, separate cursor).
    AgentDefinition(
        key="claude_head",
        display_name="Anthropic Claude (head:claude/ branch pattern)",
        discovery_query="is:pr head:claude/",
        db_key="claude",
        assignment_logins=(
            "claude[bot]",
            "claude-app[bot]",
            "anthropic-ai[bot]",
            "claude-dev[bot]",
        ),
        assignment_tokens=("claude", "anthropic"),
    ),
    # Discovers Claude Code PRs via PR body footer ("Generated with Claude Code").
    # Catches PRs where the branch name is not head:claude/ but Claude Code still
    # stamped its footer — e.g. human-named branches, worktrees, or CI-triggered runs.
    # Stores PRs as agent_key='claude' in the DB (same pool, separate cursor).
    AgentDefinition(
        key="claude_body",
        display_name="Anthropic Claude (PR body footer)",
        discovery_query='is:pr "Generated with Claude Code"',
        db_key="claude",
        assignment_logins=(
            "claude[bot]",
            "claude-app[bot]",
            "anthropic-ai[bot]",
            "claude-dev[bot]",
        ),
        assignment_tokens=("claude", "anthropic"),
    ),
    # Discovers Claude Code PRs restricted to repos that also have Codex PRs.
    # Uses a dynamic REPO_ALLOWLIST built from the codex dataset at runtime.
    # Stored as agent_key='cross' for direct cross-agent comparison queries.
    AgentDefinition(
        key="cross",
        display_name="Cross-agent repos (Claude in Codex repos)",
        discovery_query="is:pr head:claude/",
        assignment_logins=(
            "claude[bot]",
            "claude-app[bot]",
            "anthropic-ai[bot]",
            "claude-dev[bot]",
        ),
        assignment_tokens=("claude", "anthropic"),
    ),
    AgentDefinition(
        key="cursor",
        display_name="Cursor Agents",
        discovery_query="is:pr head:cursor/",
        assignment_tokens=("cursor",),
    ),
    AgentDefinition(
        key="devin",
        display_name="Devin",
        discovery_query="is:pr author:devin-ai-integration[bot]",
        assignment_logins=("devin-ai-integration[bot]",),
        assignment_tokens=("devin",),
    ),
    # `codegen-sh[bot]` went dormant 2026-03-27 (0 PRs since). A
    # `Codegen-App` GitHub Organization was created 2026-05-29 ("an
    # autonomous coding workflow"), which reads like a rebrand in progress,
    # but live-verified 2026-07-05: it has authored 0 PRs, has only a
    # `.github` repo, and no `codegen-app[bot]`/`codegenapp[bot]` App account
    # exists yet. `is:pr head:codegen/` (5,195 hits) looked promising but is a
    # false lead — sampled PRs are unrelated developers' own "codegen/"
    # branches for literal code-generation tooling (OpenAPI/AsyncAPI sync
    # scripts), not this agent. No replacement fingerprint exists today;
    # re-check `Codegen-App` before allocating crawl budget here.
    AgentDefinition(
        key="codegen",
        display_name="Codegen",
        discovery_query="is:pr author:codegen-sh[bot]",
        assignment_logins=("codegen-sh[bot]",),
        assignment_tokens=("codegen",),
    ),
    AgentDefinition(
        key="jules",
        display_name="Google Labs Jules",
        discovery_query="is:pr author:google-labs-jules[bot]",
        assignment_logins=("google-labs-jules[bot]",),
        assignment_tokens=("jules", "google-labs-jules"),
    ),
    # --- crawl-expansion-202607: LogicStar-parity agent set --------------------
    # Verified live via gh search 2026-07-04 (see plans/crawl_expansion_20260704.md
    # in the parent repo for the full methodology + volume estimates).
    AgentDefinition(
        key="cosine",
        display_name="Cosine",
        # Confirmed: 5,284 all-time / 21 in June 2026 — real but very small.
        discovery_query="is:pr head:cosine/",
        assignment_tokens=("cosine",),
    ),
    AgentDefinition(
        key="tembo",
        display_name="Tembo",
        # `tembo-io` is a real GitHub Organization (verified via `gh api
        # users/tembo-io`) but `is:pr author:tembo-io` returns 0 PRs all-time
        # (verified 2026-07-04) and no `tembo-io[bot]`/`tembo-ai[bot]` App
        # account exists (422 "user does not exist"). Matches LogicStar's own
        # classifier (`pr_classifier.py`: `pr.actor.login == 'tembo-io'`), so
        # this is the correct fingerprint — it is simply dormant/negligible
        # today. Kept wired (zero cost) in case Tembo resumes opening PRs
        # under this login; re-check before allocating any shard budget.
        discovery_query="is:pr author:tembo-io",
        assignment_logins=("tembo-io",),
        assignment_tokens=("tembo",),
    ),
    # OpenHands has no bot-authored or branch-prefix fingerprint (the
    # `OpenHands` GitHub account is a real Organization but has authored 0
    # PRs; no `openhands*[bot]` App account exists). LogicStar's own
    # classifier only catches it via first-commit *author name* inspection
    # (raw git author name == "openhands") — see
    # hydration._infer_agent_key_from_pr_commits, which checks this signal on
    # every hydrated PR regardless of which agent's query found it.
    #
    # It DOES have a stable PR-body footer, live-verified 2026-07-05: PRs
    # opened by the hosted OpenHands product (app.all-hands.dev) carry the
    # exact sentence '_This pull request was created by [OpenHands]
    # (https://app.all-hands.dev) ...'. `is:pr "This pull request was created
    # by OpenHands" in:body` returns 186 all-time / low tens per month (Apr
    # 37, May 85, Jun 58, 2026) — small but a genuine signal, unlike the old
    # bare '"OpenHands" in:body' fallback (~3.7K/month, mostly PRs that merely
    # mention the project). Not yet wired into crawl.yml's matrix — that's a
    # separate decision (see plan, section (e), runbook item 5) — but this
    # query is now accurate enough to enable directly rather than relying
    # solely on the hydration-time reclassifier.
    AgentDefinition(
        key="openhands",
        display_name="OpenHands",
        discovery_query='is:pr "This pull request was created by OpenHands" in:body',
        assignment_tokens=("openhands",),
    ),
)


AGENT_BY_KEY: dict[str, AgentDefinition] = {agent.key: agent for agent in AGENTS}
