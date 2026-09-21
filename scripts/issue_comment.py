# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The one-line outcome comment the publisher posts on each issue."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import Any, cast

import monkey_github as github
import proposal_model as model
import proposal_policy as policy


def _proposed_comment(
    result: dict[str, Any], _reasons: str, _run_url: str
) -> str | None:
    url = result.get("pull_request_url") or result.get("branch_url")
    if not url:
        return None
    noun = "pull request" if result.get("pull_request_url") else "branch"
    return f"🐒 An AI agent has proposed a change for this issue: {noun} {url}"


def _abstain_comment(_result: dict[str, Any], reasons: str, _run_url: str) -> str:
    return f"🐒 An AI agent looked at this issue and did not attempt it: {reasons}"


def _rejected_comment(_result: dict[str, Any], reasons: str, run_url: str) -> str:
    return (
        "🐒 An AI agent attempted this issue but its proposal failed a policy "
        f"check ({reasons}). Run: {run_url or 'n/a'}"
    )


def _failed_comment(_result: dict[str, Any], reasons: str, run_url: str) -> str:
    return (
        "🐒 An AI agent attempted this issue but did not finish "
        f"({reasons}). Run: {run_url or 'n/a'}"
    )


def _publish_failed_comment(_result: dict[str, Any], reasons: str, run_url: str) -> str:
    return (
        "🐒 An AI agent proposed a change for this issue but publishing it "
        f"failed ({reasons}); the issue stays eligible. Run: {run_url or 'n/a'}"
    )


COMMENTS: dict[str, Callable[[dict[str, Any], str, str], str | None]] = {
    "proposed": _proposed_comment,
    "abstain": _abstain_comment,
    "rejected": _rejected_comment,
    "author-failed": _failed_comment,
    "publish-failed": _publish_failed_comment,
}


def comment_body(result: dict[str, Any], run_url: str) -> str | None:
    """One line for the issue, or None when there is nothing worth saying.

    The reasons descend from the untrusted manifest, so they are cut
    to a size that keeps the whole comment inside GitHub's limit with
    the run URL intact.
    """
    template = COMMENTS.get(str(result.get("verdict")))
    if template is None or result.get("already_published"):
        # The earlier attempt commented, or will say so on its rerun.
        return None
    reasons = policy.comment_safe(
        "; ".join(str(r) for r in cast("list[Any]", result.get("reasons") or []))
    )
    if len(reasons) > policy.MAX_COMMENT_REASON:
        reasons = reasons[: policy.MAX_COMMENT_REASON - 1] + "…"
    return template(result, reasons, run_url)


def run_comment(args: argparse.Namespace) -> None:
    """Post the outcome comment and record its URL in result.json."""
    result = model.load_json(args.result, "result")
    if result.get("dry_run"):
        print("dry run: no comment")
        return
    body = comment_body(result, args.run_url)
    if body is None:
        print("nothing to comment")
        return
    repository = github.require_str(result, "repository", "result")
    issue = github.require_int(result, "issue", "result")
    data = github.api_write(
        "POST", f"repos/{repository}/issues/{issue}/comments", {"body": body}
    )
    result["comment_url"] = data.get("html_url")
    args.result.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
