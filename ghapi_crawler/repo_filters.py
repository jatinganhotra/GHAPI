from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase


@dataclass(frozen=True)
class RepoFilter:
    allowlist_patterns: tuple[str, ...] = ()
    denylist_patterns: tuple[str, ...] = ()

    def is_allowed(self, repo_full_name: str) -> bool:
        return self.reason(repo_full_name) == "allowed"

    def reason(self, repo_full_name: str) -> str:
        normalized = repo_full_name.strip().lower()

        if any(_match(normalized, pattern) for pattern in self.denylist_patterns):
            return "denied_by_pattern"

        if self.allowlist_patterns and not any(
            _match(normalized, pattern) for pattern in self.allowlist_patterns
        ):
            return "not_in_allowlist"

        return "allowed"


def _match(repo_full_name: str, pattern: str) -> bool:
    return fnmatchcase(repo_full_name, pattern.strip().lower())

