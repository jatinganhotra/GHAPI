from __future__ import annotations

import re
from pathlib import PurePosixPath


TEST_PATH_PATTERNS = (
    re.compile(r"(^|/)tests?(/|$)", re.IGNORECASE),
    re.compile(r"(^|/)specs?(/|$)", re.IGNORECASE),
    re.compile(r"(^|/)testing(/|$)", re.IGNORECASE),
    re.compile(r"(^|/)__tests__(/|$)", re.IGNORECASE),
    re.compile(r"(^|/)__snapshots__(/|$)", re.IGNORECASE),
    re.compile(r"(^|/)testdata(/|$)", re.IGNORECASE),
    re.compile(r"(^|/)e2e(/|$)", re.IGNORECASE),
    re.compile(r"(^|/)integration(/|$)", re.IGNORECASE),
    re.compile(r"(^|/)unit(/|$)", re.IGNORECASE),
)

TEST_FILENAME_PATTERNS = (
    re.compile(r"^test[_\-].+\.", re.IGNORECASE),
    re.compile(r"(^|[_\-])test\.", re.IGNORECASE),
    re.compile(r"(^|[_\-])spec\.", re.IGNORECASE),
    re.compile(r"[_\-]test\.", re.IGNORECASE),
    re.compile(r"[_\-]spec\.", re.IGNORECASE),
    re.compile(r"\.test\.", re.IGNORECASE),
    re.compile(r"\.spec\.", re.IGNORECASE),
    re.compile(r"Tests?\.(java|kt|kts|scala|cs)$", re.IGNORECASE),
    re.compile(r"(^|[_\-])it\.(java|kt|kts|scala|go|py|rs)$", re.IGNORECASE),
    re.compile(r"(?:^|[a-z0-9_])IT\.(java|kt|kts|scala)$", re.IGNORECASE),
)

LANGUAGE_TEST_FILENAME_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "Python": (
        re.compile(r"^test_[a-z0-9_]+\.py$", re.IGNORECASE),
        re.compile(r"^[a-z0-9_]+_test\.py$", re.IGNORECASE),
        re.compile(r"^[a-z0-9_]+_tests\.py$", re.IGNORECASE),
        re.compile(r"^conftest\.py$", re.IGNORECASE),
    ),
    "Java": (
        re.compile(r"^[A-Za-z0-9_]+(?:Test|Tests|IT|Spec)\.java$"),
    ),
    "Kotlin": (
        re.compile(r"^[A-Za-z0-9_]+(?:Test|Tests|IT|Spec)\.(kt|kts)$"),
    ),
    "Scala": (
        re.compile(r"^[A-Za-z0-9_]+(?:Test|Tests|IT|Spec)\.scala$"),
    ),
    "Go": (
        re.compile(r"^[a-z0-9_]+_test\.go$", re.IGNORECASE),
    ),
    "Rust": (
        re.compile(r"^[a-z0-9_]+_test\.rs$", re.IGNORECASE),
        re.compile(r"^[a-z0-9_]+_tests\.rs$", re.IGNORECASE),
    ),
    "JavaScript": (
        re.compile(r"^[a-z0-9_.-]+\.(test|spec)\.(js|jsx|mjs|cjs)$", re.IGNORECASE),
    ),
    "TypeScript": (
        re.compile(r"^[a-z0-9_.-]+\.(test|spec)\.(ts|tsx|mts|cts)$", re.IGNORECASE),
    ),
    "Ruby": (
        re.compile(r"^test_[a-z0-9_]+\.rb$", re.IGNORECASE),
        re.compile(r"^[a-z0-9_]+_spec\.rb$", re.IGNORECASE),
    ),
    "PHP": (
        re.compile(r"^[A-Za-z0-9_]+Test\.php$"),
    ),
    "C#": (
        re.compile(r"^[A-Za-z0-9_]+(?:Test|Tests|Spec)\.cs$"),
    ),
}

LANGUAGE_TEST_PATH_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "Python": (
        re.compile(r"(^|/)tests?(/|$)", re.IGNORECASE),
    ),
    "Java": (
        re.compile(r"(^|/)src/test/java(/|$)", re.IGNORECASE),
    ),
    "Kotlin": (
        re.compile(r"(^|/)src/test/kotlin(/|$)", re.IGNORECASE),
    ),
    "Scala": (
        re.compile(r"(^|/)src/test/scala(/|$)", re.IGNORECASE),
    ),
    "Go": (
        re.compile(r"(^|/)(internal/)?tests?(/|$)", re.IGNORECASE),
    ),
    "Rust": (
        re.compile(r"(^|/)tests?(/|$)", re.IGNORECASE),
    ),
    "JavaScript": (
        re.compile(r"(^|/)__tests__(/|$)", re.IGNORECASE),
    ),
    "TypeScript": (
        re.compile(r"(^|/)__tests__(/|$)", re.IGNORECASE),
    ),
    "Ruby": (
        re.compile(r"(^|/)spec(/|$)", re.IGNORECASE),
        re.compile(r"(^|/)test(/|$)", re.IGNORECASE),
    ),
    "PHP": (
        re.compile(r"(^|/)tests?(/|$)", re.IGNORECASE),
    ),
    "C#": (
        re.compile(r"(^|/)tests?(/|$)", re.IGNORECASE),
    ),
}

TEST_PATCH_HINTS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"^\s*def\s+test_[a-z0-9_]+\s*\(", re.IGNORECASE | re.MULTILINE), ("Python",)),
    (re.compile(r"^\s*func\s+Test[A-Z][A-Za-z0-9_]*\s*\(", re.MULTILINE), ("Go",)),
    (re.compile(r"^\s*#\s*\[\s*test\s*\]", re.MULTILINE), ("Rust",)),
    (re.compile(r"@\s*Test\b"), ("Java", "Kotlin", "Scala")),
    (
        re.compile(r"\b(describe|it|test)\s*\(", re.IGNORECASE),
        ("JavaScript", "TypeScript"),
    ),
    (re.compile(r"\bit\s+['\"][^'\"]+['\"]\s+do\b"), ("Ruby",)),
)

LANGUAGE_BY_EXTENSION = {
    ".py": "Python",
    ".pyi": "Python",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".gradle.kts": "Kotlin",
    ".scala": "Scala",
    ".rs": "Rust",
    ".go": "Go",
    ".mod": "Go",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".mts": "TypeScript",
    ".cts": "TypeScript",
    ".c": "C",
    ".h": "C/C++",
    ".cc": "C/C++",
    ".cpp": "C/C++",
    ".hpp": "C/C++",
    ".cs": "C#",
    ".rb": "Ruby",
    ".erb": "Ruby",
    ".php": "PHP",
    ".swift": "Swift",
    ".m": "Objective-C",
    ".mm": "Objective-C++",
    ".r": "R",
    ".jl": "Julia",
    ".lua": "Lua",
    ".pl": "Perl",
    ".sql": "SQL",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".fish": "Shell",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".json": "JSON",
    ".jsonl": "JSON",
    ".toml": "TOML",
    ".md": "Markdown",
    ".rst": "reStructuredText",
    ".graphql": "GraphQL",
    ".gql": "GraphQL",
    ".proto": "Protocol Buffers",
    ".dockerfile": "Dockerfile",
    ".tf": "Terraform",
    ".tfvars": "Terraform",
    ".hcl": "HCL",
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".sass": "Sass",
    ".less": "Less",
    ".xml": "XML",
    ".ini": "INI",
    ".cfg": "INI",
    ".ipynb": "Jupyter Notebook",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".bazel": "Starlark",
    ".bzl": "Starlark",
    ".gradle": "Groovy",
    ".groovy": "Groovy",
}

LANGUAGE_BY_SPECIAL_FILENAME = {
    "dockerfile": "Dockerfile",
    "makefile": "Makefile",
    "gnumakefile": "Makefile",
    "cmakelists.txt": "CMake",
    "build.bazel": "Starlark",
    "workspace": "Starlark",
    "workspace.bazel": "Starlark",
    "build": "Starlark",
    "jenkinsfile": "Groovy",
    "gemfile": "Ruby",
    "rakefile": "Ruby",
    "vagrantfile": "Ruby",
    "podfile": "Ruby",
    "procfile": "Procfile",
}

SHEBANG_LANGUAGE_PATTERNS = (
    (re.compile(r"^#!.*\bpython[0-9.]*\b", re.IGNORECASE), "Python"),
    (re.compile(r"^#!.*\b(node|deno)\b", re.IGNORECASE), "JavaScript"),
    (re.compile(r"^#!.*\b(bash|sh|zsh|fish)\b", re.IGNORECASE), "Shell"),
    (re.compile(r"^#!.*\bruby\b", re.IGNORECASE), "Ruby"),
    (re.compile(r"^#!.*\bperl\b", re.IGNORECASE), "Perl"),
    (re.compile(r"^#!.*\bphp\b", re.IGNORECASE), "PHP"),
)

PATCH_CONTENT_HINTS = (
    (re.compile(r"\bimport\s+torch\b|\bfrom\s+torch\s+import\b"), "Python"),
    (re.compile(r"\bpackage\s+[a-zA-Z0-9_.]+;"), "Java"),
    (re.compile(r"\bfn\s+main\s*\("), "Rust"),
    (re.compile(r"\bfunc\s+main\s*\("), "Go"),
    (re.compile(r"\bconsole\.log\s*\(|\brequire\s*\("), "JavaScript"),
    (re.compile(r"\bexport\s+default\b|\binterface\s+[A-Za-z0-9_]+"), "TypeScript"),
    (re.compile(r"^\s*<template>|^\s*<script setup", re.MULTILINE), "Vue"),
    (re.compile(r"^\s*provider\s+\"[a-z0-9_-]+\"", re.MULTILINE), "Terraform"),
)

HUNK_PATTERN = re.compile(r"^@@ .+ @@.*$", re.MULTILINE)


def is_test_file(
    path: str,
    language: str | None = None,
    patch: str | None = None,
) -> bool:
    normalized = path.strip("/")
    file_name = PurePosixPath(normalized).name

    for pattern in TEST_PATH_PATTERNS:
        if pattern.search(normalized):
            return True

    for pattern in TEST_FILENAME_PATTERNS:
        if pattern.search(file_name):
            return True

    resolved_language = language or detect_language(path, patch=patch)
    if resolved_language in LANGUAGE_TEST_PATH_PATTERNS:
        for pattern in LANGUAGE_TEST_PATH_PATTERNS[resolved_language]:
            if pattern.search(normalized):
                return True

    if resolved_language in LANGUAGE_TEST_FILENAME_PATTERNS:
        for pattern in LANGUAGE_TEST_FILENAME_PATTERNS[resolved_language]:
            if pattern.search(file_name):
                return True

    if _patch_has_test_signals(patch=patch, language=resolved_language):
        return True

    return False


def detect_language(path: str, patch: str | None = None) -> str:
    normalized = path.strip("/")
    file_name = PurePosixPath(normalized).name
    lower_name = file_name.lower()

    if lower_name in LANGUAGE_BY_SPECIAL_FILENAME:
        return LANGUAGE_BY_SPECIAL_FILENAME[lower_name]

    full_suffix = "".join(PurePosixPath(normalized).suffixes).lower()
    if full_suffix and full_suffix in LANGUAGE_BY_EXTENSION:
        return LANGUAGE_BY_EXTENSION[full_suffix]

    suffix = PurePosixPath(normalized).suffix.lower()
    if suffix in LANGUAGE_BY_EXTENSION:
        return LANGUAGE_BY_EXTENSION[suffix]

    patch_hint = _language_from_patch_hints(patch)
    if patch_hint:
        return patch_hint

    return "Other"


def extension_of(path: str) -> str:
    return PurePosixPath(path).suffix.lower()


def count_hunks(patch: str | None) -> int:
    if not patch:
        return 0
    return len(HUNK_PATTERN.findall(patch))


def word_count(text: str | None) -> int:
    if not text:
        return 0
    return len(re.findall(r"\b\w+\b", text))


def _language_from_patch_hints(patch: str | None) -> str | None:
    if not patch:
        return None

    lines = []
    for line in patch.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            lines.append(line[1:])
        elif not line.startswith("@@"):
            lines.append(line)

    if not lines:
        return None

    first_non_empty = next((line.strip() for line in lines if line.strip()), "")
    if first_non_empty:
        for pattern, language in SHEBANG_LANGUAGE_PATTERNS:
            if pattern.search(first_non_empty):
                return language

    joined = "\n".join(lines[:80])
    for pattern, language in PATCH_CONTENT_HINTS:
        if pattern.search(joined):
            return language

    return None


def _patch_has_test_signals(patch: str | None, language: str) -> bool:
    if not patch:
        return False

    lines = []
    for line in patch.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            lines.append(line[1:])
        elif not line.startswith("@@"):
            lines.append(line)

    if not lines:
        return False

    joined = "\n".join(lines[:120])
    for pattern, languages in TEST_PATCH_HINTS:
        if language in languages and pattern.search(joined):
            return True
    return False
