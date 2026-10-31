from ghapi_crawler.repository_metrics import (
    estimate_loc_from_code_frequency,
    summarize_repository_tree,
)


def test_summarize_repository_tree_counts_blob_files_and_bytes():
    tree_payload = {
        "truncated": False,
        "tree": [
            {"path": "README.md", "type": "blob", "size": 120},
            {"path": "src/main.py", "type": "blob", "size": 480},
            {"path": "src", "type": "tree"},
            {"path": "vendor/lib", "type": "blob", "size": "200"},
        ],
    }

    file_count, blob_size_bytes, tree_truncated = summarize_repository_tree(tree_payload)

    assert file_count == 3
    assert blob_size_bytes == 800
    assert tree_truncated is False


def test_summarize_repository_tree_handles_unexpected_payload():
    assert summarize_repository_tree({"tree": "bad-shape"}) == (None, None, None)
    assert summarize_repository_tree(None) == (None, None, None)


def test_estimate_loc_from_code_frequency_uses_net_additions_minus_deletions():
    payload = [
        [1700000000, 1200, 200],
        [1700600000, 400, 100],
        [1701200000, 50, 600],
    ]

    assert estimate_loc_from_code_frequency(payload) == 750


def test_estimate_loc_from_code_frequency_handles_empty_and_pending_stats():
    assert estimate_loc_from_code_frequency([]) == 0
    assert (
        estimate_loc_from_code_frequency({"message": "Statistics are being generated"})
        is None
    )
