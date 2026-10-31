from ghapi_crawler.hydration import (
    _graphql_pull_request_to_rest_shape,
    _infer_agent_key_from_pr_commits,
)


def test_graphql_pull_request_mapping_to_rest_shape():
    node = {
        "title": "Improve pipeline",
        "body": "Body",
        "state": "OPEN",
        "isDraft": True,
        "author": {"login": "alice"},
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T01:00:00Z",
        "closedAt": None,
        "mergedAt": None,
        "additions": 120,
        "deletions": 30,
        "changedFiles": 7,
        "commits": {"totalCount": 3},
        "comments": {"totalCount": 2},
    }
    out = _graphql_pull_request_to_rest_shape(node)
    assert out["title"] == "Improve pipeline"
    assert out["draft"] is True
    assert out["state"] == "open"
    assert out["user"]["login"] == "alice"
    assert out["commits"] == 3
    assert out["comments"] == 2
    assert out["changed_files"] == 7


def test_infer_agent_from_pr_commits_prefers_claude_bot_logins():
    commits = [
        {
            "author": {"login": "claude-app[bot]"},
            "committer": {"login": "web-flow"},
            "commit": {
                "author": {"name": "Claude", "email": "123+claude-app[bot]@users.noreply.github.com"},
                "committer": {"name": "GitHub", "email": "noreply@github.com"},
            },
        }
    ]
    assert _infer_agent_key_from_pr_commits(commits) == "claude"


def test_infer_agent_from_pr_commits_matches_claude_email_domains():
    commits = [
        {
            "author": {"login": "release-user"},
            "committer": {"login": "release-user"},
            "commit": {
                "author": {"name": "Automation", "email": "ci-bot@anthropic.com"},
                "committer": {"name": "Automation", "email": "ci-bot@anthropic.com"},
            },
        }
    ]
    assert _infer_agent_key_from_pr_commits(commits) == "claude"


def test_infer_agent_from_pr_commits_returns_none_without_claude_signal():
    commits = [
        {
            "author": {"login": "alice"},
            "committer": {"login": "bob"},
            "commit": {
                "author": {"name": "Alice", "email": "alice@example.com"},
                "committer": {"name": "Bob", "email": "bob@example.com"},
            },
        },
        {
            "author": {"login": "claudette"},
            "commit": {
                "author": {"name": "Claudette", "email": "claudette@example.com"},
                "committer": {"name": "Claudette", "email": "claudette@example.com"},
            },
        },
    ]
    assert _infer_agent_key_from_pr_commits(commits) is None
