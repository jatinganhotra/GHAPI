import datetime as dt

from ghapi_crawler.agents import AGENT_BY_KEY
from ghapi_crawler.assignment import select_issue_assignment_event


def _event(login: str, ts: str) -> dict:
    return {
        "created_at": dt.datetime.fromisoformat(ts).replace(tzinfo=dt.timezone.utc),
        "assigned_login": login,
    }


def _legacy_event(login: str, ts: str) -> dict:
    return {
        "created_at": dt.datetime.fromisoformat(ts).replace(tzinfo=dt.timezone.utc),
        "raw_payload": {"assignee": {"login": login}},
    }


def test_select_assignment_prefers_exact_agent_login():
    agent = AGENT_BY_KEY["devin"]
    events = [
        _event("random-user", "2026-01-02T10:00:00"),
        _event("devin-ai-integration[bot]", "2026-01-02T11:00:00"),
    ]
    selected = select_issue_assignment_event(agent, events)
    assert selected.assignment_source == "agent_login_match"
    assert selected.assignment_confidence == "high"
    assert selected.assigned_login == "devin-ai-integration[bot]"


def test_select_assignment_falls_back_to_token():
    agent = AGENT_BY_KEY["codex"]
    events = [
        _event("codex-assistant", "2026-01-02T10:00:00"),
        _event("user1", "2026-01-02T09:00:00"),
    ]
    selected = select_issue_assignment_event(agent, events)
    assert selected.assignment_source == "agent_token_match"
    assert selected.assignment_confidence == "medium"
    assert selected.assigned_login == "codex-assistant"


def test_select_assignment_returns_none_for_human_only_assignments():
    agent = AGENT_BY_KEY["copilot"]
    events = [
        _event("alice", "2026-01-02T09:00:00"),
        _event("bob", "2026-01-02T10:00:00"),
    ]
    selected = select_issue_assignment_event(agent, events)
    assert selected.assignment_source == "none"
    assert selected.assignment_confidence == "none"
    assert selected.assigned_login is None


def test_select_assignment_generic_bot_fallback():
    agent = AGENT_BY_KEY["copilot"]
    events = [
        _event("alice", "2026-01-02T09:00:00"),
        _event("triage-bot", "2026-01-02T10:00:00"),
    ]
    selected = select_issue_assignment_event(agent, events)
    assert selected.assignment_source == "generic_bot_assignment"
    assert selected.assignment_confidence == "low"
    assert selected.assigned_login == "triage-bot"


def test_select_assignment_prefers_pre_pr_signal():
    agent = AGENT_BY_KEY["codex"]
    pr_created_at = dt.datetime(2026, 1, 2, 10, 30, tzinfo=dt.timezone.utc)
    events = [
        _event("codex-assistant", "2026-01-02T10:00:00"),
        _event("codex[bot]", "2026-01-02T11:00:00"),
    ]
    selected = select_issue_assignment_event(agent, events, pr_created_at=pr_created_at)
    assert selected.assignment_source == "agent_token_match"
    assert selected.assignment_confidence == "medium"
    assert selected.assigned_login == "codex-assistant"


def test_select_assignment_uses_post_pr_match_when_no_pre_pr_signal():
    agent = AGENT_BY_KEY["devin"]
    pr_created_at = dt.datetime(2026, 1, 2, 10, 0, tzinfo=dt.timezone.utc)
    events = [
        _event("human-dev", "2026-01-02T09:00:00"),
        _event("devin-ai-integration[bot]", "2026-01-02T11:00:00"),
    ]
    selected = select_issue_assignment_event(agent, events, pr_created_at=pr_created_at)
    assert selected.assignment_source == "agent_login_match_after_pr_created"
    assert selected.assignment_confidence == "medium"
    assert selected.assigned_login == "devin-ai-integration[bot]"


def test_select_assignment_supports_legacy_raw_payload_shape():
    agent = AGENT_BY_KEY["codex"]
    events = [
        _legacy_event("codex-assistant", "2026-01-02T10:00:00"),
    ]
    selected = select_issue_assignment_event(agent, events)
    assert selected.assignment_source == "agent_token_match"
    assert selected.assignment_confidence == "medium"
    assert selected.assigned_login == "codex-assistant"
