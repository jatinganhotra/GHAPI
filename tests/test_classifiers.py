from ghapi_crawler.classifiers import count_hunks, detect_language, is_test_file, word_count


def test_is_test_file_by_directory():
    assert is_test_file("src/tests/test_api.py")
    assert is_test_file("pkg/__tests__/widget.ts")
    assert is_test_file("integration/payments/flow.go")


def test_is_test_file_by_filename():
    assert is_test_file("src/user_service_test.go")
    assert is_test_file("web/auth.spec.ts")
    assert is_test_file("cli/parse.test.js")
    assert is_test_file("python/test_api.py")
    assert is_test_file("java/PaymentServiceTest.java")
    assert is_test_file("java/PaymentFlowIT.java")
    assert is_test_file("java/PaymentSpec.java", language="Java")
    assert is_test_file("dotnet/OrderTests.cs", language="C#")
    assert is_test_file("python/api_tests.py", language="Python")
    assert not is_test_file("src/main.py")
    assert not is_test_file("src/latest_config.ts")
    assert not is_test_file("src/contest.py", language="Python")


def test_is_test_file_by_patch_signal_when_name_is_ambiguous():
    rust_patch = "@@ -0,0 +1,4 @@\n+#[test]\n+fn it_works() { assert!(true); }\n"
    js_patch = "@@ -0,0 +1,4 @@\n+describe('x', () => { it('y', () => {}); });\n"
    py_patch = "@@ -0,0 +1,3 @@\n+def test_parser_roundtrip():\n+    assert True\n"

    assert is_test_file("src/checks/custom.rs", language="Rust", patch=rust_patch)
    assert is_test_file("web/src/helper.ts", language="TypeScript", patch=js_patch)
    assert is_test_file("scripts/runner", language="Python", patch=py_patch)


def test_detect_language_defaults():
    assert detect_language("src/main.py") == "Python"
    assert detect_language("backend/service.rs") == "Rust"
    assert detect_language("unknown/file.customext") == "Unknown"
    assert detect_language("Dockerfile") == "Dockerfile"
    assert detect_language("build/BUILD.bazel") == "Starlark"
    assert detect_language("java/build.gradle") == "Groovy"
    assert detect_language("kotlin/build.gradle.kts") == "Kotlin"
    assert detect_language("web/runtime.mjs") == "JavaScript"
    assert detect_language("mobile/lib/app.dart") == "Dart"
    assert detect_language("docs/content.mdx") == "Markdown"
    assert detect_language("assets/logo.svg") == "Non-code"
    assert detect_language("README") == "Non-code"


def test_detect_language_from_patch_hints():
    python_patch = "@@ -0,0 +1,2 @@\n+#!/usr/bin/env python3\n+print('ok')\n"
    shell_patch = "@@ -0,0 +1,2 @@\n+#!/bin/bash\n+echo hi\n"
    ts_hint_patch = "@@ -0,0 +1,2 @@\n+export default class Worker {}\n+interface Foo {}\n"

    assert detect_language("scripts/runjob", patch=python_patch) == "Python"
    assert detect_language("scripts/release", patch=shell_patch) == "Shell"
    assert detect_language("src/unknown.custom", patch=ts_hint_patch) == "TypeScript"


def test_count_hunks_and_word_count():
    patch = """@@ -1,2 +1,3 @@\n line\n@@ -10,3 +10,4 @@\n line\n"""
    assert count_hunks(patch) == 2
    assert count_hunks(None) == 0
    assert word_count("Fix parser bug in payment retry loop.") == 7
    assert word_count("") == 0
