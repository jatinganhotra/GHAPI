from ghapi_crawler.linking import (
    issue_link_candidates_from_pr_body,
    issue_link_candidates_from_timeline_events,
    linked_issues_from_pr_body,
    linked_issues_from_timeline_events,
)


def test_linked_issues_from_pr_body_variants():
    body = """
    Fixes #12
    closes org/repo#45
    resolved https://github.com/acme/api/issues/77
    """
    links = linked_issues_from_pr_body("myorg/myrepo", body)
    assert ("myorg/myrepo", 12) in links
    assert ("org/repo", 45) in links
    assert ("acme/api", 77) in links


def test_issue_link_candidates_from_pr_body_include_confidence_and_explainability():
    body = "Fixes #12 and closes org/repo#45"
    candidates = issue_link_candidates_from_pr_body("myorg/myrepo", body)
    by_issue = {(row.repo_full_name, row.issue_number): row for row in candidates}

    local = by_issue[("myorg/myrepo", 12)]
    assert local.confidence == "medium"
    assert local.confidence_score > 0.8
    assert local.explainability["reference_type"] == "local_ref"

    repo_ref = by_issue[("org/repo", 45)]
    assert repo_ref.confidence == "high"
    assert repo_ref.confidence_score > local.confidence_score
    assert repo_ref.explainability["reference_type"] == "repo_ref"


def test_linked_issues_from_timeline_events():
    events = [
        {
            "event": "cross-referenced",
            "source": {
                "issue": {
                    "number": 101,
                    "repository_url": "https://api.github.com/repos/team/service",
                }
            },
        }
    ]
    links = linked_issues_from_timeline_events(events)
    assert links == {("team/service", 101)}


def test_issue_link_candidates_from_timeline_events_confidence():
    events = [
        {
            "event": "connected",
            "actor": {"login": "bot-a"},
            "source": {
                "issue": {
                    "number": 101,
                    "repository_url": "https://api.github.com/repos/team/service",
                }
            },
        },
        {
            "event": "referenced",
            "actor": {"login": "bot-b"},
            "source": {
                "issue": {
                    "number": 102,
                    "repository_url": "https://api.github.com/repos/team/service",
                }
            },
        },
    ]

    candidates = issue_link_candidates_from_timeline_events(events)
    by_issue = {(row.repo_full_name, row.issue_number): row for row in candidates}
    assert by_issue[("team/service", 101)].confidence == "high"
    assert by_issue[("team/service", 102)].confidence == "medium"
    assert (
        by_issue[("team/service", 101)].confidence_score
        > by_issue[("team/service", 102)].confidence_score
    )
