from ghapi_crawler.repo_filters import RepoFilter


def test_repo_filter_allows_all_when_empty():
    flt = RepoFilter()
    assert flt.is_allowed("acme/repo")
    assert flt.reason("acme/repo") == "allowed"


def test_repo_filter_deny_precedence():
    flt = RepoFilter(
        allowlist_patterns=("acme/*",),
        denylist_patterns=("acme/noisy-repo",),
    )
    assert not flt.is_allowed("acme/noisy-repo")
    assert flt.reason("acme/noisy-repo") == "denied_by_pattern"
    assert flt.is_allowed("acme/clean-repo")


def test_repo_filter_allowlist_blocks_non_matching():
    flt = RepoFilter(allowlist_patterns=("acme/*", "openai/*"))
    assert flt.is_allowed("acme/repo")
    assert flt.is_allowed("openai/codex")
    assert not flt.is_allowed("other/repo")
    assert flt.reason("other/repo") == "not_in_allowlist"

