#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _read_event_payload() -> dict[str, Any]:
    event_path = _env("GITHUB_EVENT_PATH")
    if not event_path:
        raise RuntimeError("GITHUB_EVENT_PATH is not set")
    with open(event_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _request_json(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url=url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "ghapi-crawler-alert",
        },
    )
    with urllib.request.urlopen(req) as resp:
        body = resp.read()
        if not body:
            return None
        return json.loads(body.decode("utf-8"))


def _request_json_allow_404(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url=url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "ghapi-crawler-alert",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read()
            if not body:
                return resp.status, None
            return resp.status, json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return 404, None
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API request failed ({exc.code}): {detail}") from exc


def _ensure_label(api_base: str, repo: str, token: str, label: str) -> None:
    encoded = urllib.parse.quote(label, safe="")
    status, _ = _request_json_allow_404(
        "GET",
        f"{api_base}/repos/{repo}/labels/{encoded}",
        token,
    )
    if status == 404:
        _request_json(
            "POST",
            f"{api_base}/repos/{repo}/labels",
            token,
            payload={
                "name": label,
                "color": "B60205",
                "description": "Automated GHAPI crawler failure alert",
            },
        )


def _build_issue_body(owner_mention: str, run: dict[str, Any], repo: str) -> str:
    run_url = run.get("html_url") or "<unavailable>"
    run_id = run.get("id")
    run_attempt = run.get("run_attempt")
    conclusion = run.get("conclusion")
    event = run.get("event")
    branch = run.get("head_branch")
    actor = ((run.get("actor") or {}).get("login")) or "unknown"
    created_at = run.get("created_at")
    updated_at = run.get("updated_at")
    owner_line = owner_mention if owner_mention else "_No owner configured_"

    return (
        "GHAPI crawler workflow failed.\n\n"
        f"- Owner: {owner_line}\n"
        f"- Repository: `{repo}`\n"
        f"- Conclusion: `{conclusion}`\n"
        f"- Event: `{event}`\n"
        f"- Branch: `{branch}`\n"
        f"- Actor: `{actor}`\n"
        f"- Run ID: `{run_id}` (attempt `{run_attempt}`)\n"
        f"- Created: `{created_at}`\n"
        f"- Updated: `{updated_at}`\n"
        f"- Run URL: {run_url}\n\n"
        "Next steps:\n"
        "1. Open the run URL and inspect failed step logs.\n"
        "2. Verify crawler progress (discovery/hydration/metrics) and DB connectivity preflight.\n"
        "3. Re-run the workflow after remediation.\n"
    )


def _upsert_issue(api_base: str, repo: str, token: str, title: str, label: str, body: str) -> int:
    issues = _request_json(
        "GET",
        f"{api_base}/repos/{repo}/issues?state=open&labels={urllib.parse.quote(label, safe='')}&per_page=100",
        token,
    )
    for issue in issues:
        if issue.get("title") == title:
            number = int(issue["number"])
            _request_json(
                "POST",
                f"{api_base}/repos/{repo}/issues/{number}/comments",
                token,
                payload={"body": body},
            )
            return number

    created = _request_json(
        "POST",
        f"{api_base}/repos/{repo}/issues",
        token,
        payload={
            "title": title,
            "body": body,
            "labels": [label],
        },
    )
    return int(created["number"])


def _post_webhook(webhook_url: str, message: str) -> None:
    payload = {"text": message}
    req = urllib.request.Request(
        url=webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "ghapi-crawler-alert",
        },
    )
    with urllib.request.urlopen(req):
        return


def main() -> None:
    payload = _read_event_payload()
    run = payload.get("workflow_run") or {}
    conclusion = (run.get("conclusion") or "").strip().lower()
    if conclusion == "success":
        print("Crawler run concluded with success; nothing to notify.")
        return

    repo = _env("GITHUB_REPOSITORY")
    token = _env("GITHUB_TOKEN")
    api_base = _env("GITHUB_API_URL", "https://api.github.com")
    owner = _env("GHAPI_MONITOR_OWNER")
    webhook = _env("GHAPI_MONITOR_ALERT_WEBHOOK_URL")

    if not repo:
        raise RuntimeError("GITHUB_REPOSITORY is not set")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is not set")

    issue_title = "[monitor] GHAPI crawler failing"
    issue_label = "crawler-alert"
    issue_body = _build_issue_body(owner_mention=owner, run=run, repo=repo)

    _ensure_label(api_base=api_base, repo=repo, token=token, label=issue_label)
    issue_number = _upsert_issue(
        api_base=api_base,
        repo=repo,
        token=token,
        title=issue_title,
        label=issue_label,
        body=issue_body,
    )
    print(f"Crawler alert issue upserted: #{issue_number}")

    if webhook:
        run_url = run.get("html_url") or "<unavailable>"
        summary = (
            f"{owner + ' ' if owner else ''}GHAPI crawler failure in {repo}. "
            f"Run: {run_url} Issue: #{issue_number}"
        ).strip()
        _post_webhook(webhook_url=webhook, message=summary)
        print("Posted crawler alert webhook.")
    else:
        print("GHAPI_MONITOR_ALERT_WEBHOOK_URL not set; skipped channel webhook.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"notify_crawler_failure.py failed: {exc}", file=sys.stderr)
        raise
