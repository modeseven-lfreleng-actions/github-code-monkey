# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Verify an agent's proposal, replay it as signed commits, and open the PR.

Four commands, run in the trusted publish and report jobs:

``check`` works offline (see ``proposal_check``) and writes
``check.json`` with a verdict and the composed messages.

``apply`` needs ``GH_TOKEN``. For a ``proposed`` verdict outside
dry-run it creates the bot branch at the base SHA and replays each
commit through ``createCommitOnBranch``, which GitHub signs, then
opens the pull request when the mode asks for one.

``comment`` posts one line on the issue describing the outcome.

``report`` merges every ``result.json`` into one table.

A verdict other than ``proposed`` is data, not an error. The script
exits non-zero for operational failures alone.
"""

from __future__ import annotations

import argparse
import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import issue_comment as comments
import monkey_github as github
import proposal_check as checks
import proposal_model as model
import proposal_policy as policy
import proposal_report as reporting
from proposal_policy import PublishError, Rejection

SCHEMA = 1

COMMIT_MUTATION = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) {
    commit { oid url }
  }
}
"""


def file_changes(clone: Path, commit: dict[str, Any]) -> dict[str, Any]:
    """Build the createCommitOnBranch fileChanges payload for one commit."""
    additions: list[dict[str, str]] = []
    for item in cast("list[dict[str, Any]]", commit["additions"]):
        blob = str(item["blob"])
        if not policy.SHA_RE.fullmatch(blob):
            raise PublishError("check.json carries a malformed blob id")
        content = cast(
            "bytes", checks.git(clone, "cat-file", "blob", blob, binary=True)
        )
        additions.append(
            {"path": str(item["path"]), "contents": base64.b64encode(content).decode()}
        )
    deletions = [{"path": path} for path in cast("list[str]", commit["deletions"])]
    return {"additions": additions, "deletions": deletions}


def create_branch(repository: str, branch: str, base_sha: str) -> None:
    """Create the bot branch at the base; an existing branch is a rejection."""
    try:
        github.api_write(
            "POST",
            f"repos/{repository}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
    except github.GitHubError as exc:
        if exc.status == 422 and "already exists" in str(exc).lower():
            raise Rejection(f"branch {branch} already exists") from exc
        raise PublishError(str(exc)) from exc


def existing_pull_request(repository: str, branch: str) -> str | None:
    """The URL of a pull request already open from this repository's branch.

    A rerun of a publish job whose first attempt opened the pull
    request and then failed later (the comment, the upload) meets the
    branch it made. That is the run's own earlier success, not a
    conflict, so it is reported as published rather than rejected.
    """
    raw = github.run_gh(
        [
            "pr",
            "list",
            "--repo",
            repository,
            "--head",
            branch,
            "--state",
            "open",
            "--limit",
            "20",
            "--json",
            "url,isCrossRepository",
        ]
    )
    parsed = github.decode_response(raw)
    if not isinstance(parsed, list):
        raise github.GitHubError("expected a pull request array")
    for entry in cast("list[Any]", parsed):
        if isinstance(entry, dict):
            data = cast("dict[str, Any]", entry)
            url = data.get("url")
            if data.get("isCrossRepository") is False and isinstance(url, str):
                return url
    return None


def delete_branch(repository: str, branch: str) -> str | None:
    """Remove the bot branch after a failed publication; return a note on failure.

    A half-built branch would otherwise count as a prior attempt (the
    selector checks for the branch) and keep the issue out of every
    later run. Deletion is best effort: the original failure is what
    the caller reports, and a leftover branch is recorded beside it.
    """
    try:
        github.run_gh(
            ["api", "--method", "DELETE", f"repos/{repository}/git/refs/heads/{branch}"]
        )
    except github.GitHubError as exc:
        # After an ambiguous create the branch may never have existed;
        # GitHub answers 404 or 422 "Reference does not exist" then.
        if github.is_absent(exc) or "does not exist" in str(exc).lower():
            return None
        return f"branch {branch} could not be removed after the failure: {exc}"
    return None


def replay_commits(clone: Path, check: dict[str, Any]) -> list[str]:
    """Send each commit through createCommitOnBranch, chaining the head."""
    repository = str(check["repository"])
    head = str(check["base_sha"])
    created: list[str] = []
    for commit in cast("list[dict[str, Any]]", check["commits"]):
        payload = {
            "branch": {
                "repositoryNameWithOwner": repository,
                "branchName": str(check["branch"]),
            },
            "expectedHeadOid": head,
            "message": {
                "headline": str(commit["headline"]),
                "body": str(commit["body"]),
            },
            "fileChanges": file_changes(clone, commit),
        }
        try:
            data = github.graphql(COMMIT_MUTATION, {"input": payload})
        except github.GitHubError as exc:
            raise PublishError(f"createCommitOnBranch failed: {exc}") from exc
        result = data.get("createCommitOnBranch")
        node = (
            cast("dict[str, Any]", result).get("commit")
            if isinstance(result, dict)
            else None
        )
        if not isinstance(node, dict):
            raise PublishError("createCommitOnBranch returned no commit")
        oid = github.require_str(cast("dict[str, Any]", node), "oid", "commit")
        created.append(oid)
        head = oid
    return created


def open_pull_request(check: dict[str, Any]) -> tuple[str, str | None]:
    """Open the pull request; label it when the label exists.

    Returns the URL and a warning when labelling did not happen, so
    the result records it instead of the log swallowing it.
    """
    repository = str(check["repository"])
    data = github.api_write(
        "POST",
        f"repos/{repository}/pulls",
        {
            "title": str(check["pr_title"]),
            "head": str(check["branch"]),
            "base": str(check["default_branch"]),
            "body": str(check["pr_body"]),
            "maintainer_can_modify": True,
            "draft": False,
        },
    )
    url = github.require_str(data, "html_url", "pull request")
    number = github.require_int(data, "number", "pull request")
    try:
        github.api_object(f"repos/{repository}/labels/{policy.LABEL}")
        github.api_write(
            "POST",
            f"repos/{repository}/issues/{number}/labels",
            {"labels": [policy.LABEL]},
        )
    except github.GitHubError as exc:
        if github.is_absent(exc):
            return url, None
        return url, f"could not label the pull request: {exc}"
    return url, None


def run_apply(args: argparse.Namespace) -> dict[str, Any]:
    """Create the branch, replay commits and open the pull request."""
    check = model.load_json(args.check, "check")
    verdict = check.get("verdict")
    if verdict not in policy.VERDICTS:
        raise PublishError(f"check.json has an unknown verdict {verdict!r}")
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "key": check.get("key"),
        "repository": check.get("repository"),
        "issue": check.get("issue"),
        "verdict": verdict,
        "reasons": list(cast("list[Any]", check.get("reasons") or [])),
        "warnings": [],
        "dry_run": bool(args.dry_run),
        "mode": args.mode,
        "branch_url": None,
        "commits": [],
        "pull_request_url": None,
        "comment_url": None,
        "pr_title": check.get("pr_title") or None,
        "premium_requests": check.get("premium_requests"),
        "agent_seconds": check.get("agent_seconds"),
    }
    if verdict != "proposed" or args.dry_run or args.mode == "select":
        return result
    repository = str(check["repository"])
    branch = str(check["branch"])
    try:
        # Inside the rollback boundary: a create whose reply is lost
        # may still have made the ref, which would strand the issue.
        create_branch(repository, branch, str(check["base_sha"]))
        result["commits"] = replay_commits(args.workdir / "clone", check)
        if args.mode == "pull-requests":
            url, warning = open_pull_request(check)
            result["pull_request_url"] = url
            if warning:
                cast("list[str]", result["warnings"]).append(warning)
    except Rejection as exc:
        # The branch already existed. If an earlier attempt of this run
        # opened its pull request, report that; otherwise it is not
        # ours to touch. Either way, leave the branch alone.
        earlier = existing_pull_request(repository, branch)
        if earlier is not None:
            result["pull_request_url"] = earlier
            result["branch_url"] = f"https://github.com/{repository}/tree/{branch}"
            result["already_published"] = True
            cast("list[str]", result["warnings"]).append(
                "an earlier attempt already published this pull request"
            )
            return result
        result["verdict"] = "rejected"
        cast("list[str]", result["reasons"]).append(str(exc))
        return result
    except (PublishError, github.GitHubError) as exc:
        # Roll the branch back so the issue stays eligible, then fail
        # the step: this is the publisher unable to do its job.
        leftover = delete_branch(repository, branch)
        detail = str(exc) + (f"; {leftover}" if leftover else "; branch removed")
        result["verdict"] = "publish-failed"
        cast("list[str]", result["reasons"]).append(detail)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        raise PublishError(detail) from exc
    result["branch_url"] = f"https://github.com/{repository}/tree/{branch}"
    return result


def write_check(args: argparse.Namespace) -> None:
    """Run the offline check and write its outputs."""
    check = checks.run_check(
        selection_path=args.selection,
        key=args.key,
        proposal_dir=args.proposal_dir,
        workdir=args.workdir,
        coauthors_path=args.coauthors,
        run_url=args.run_url,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(check.to_json(), indent=2) + "\n", encoding="utf-8"
    )
    if args.summary:
        args.summary.write_text(model.check_summary(check), encoding="utf-8")
    reasons = policy.log_safe("; ".join(check.reasons))[:500]
    print(f"verdict: {check.verdict}; {reasons}")


def write_apply(args: argparse.Namespace) -> None:
    """Run apply and write result.json."""
    result = run_apply(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"apply: {result['verdict']}; pr={result['pull_request_url']}")


def build_parser() -> argparse.ArgumentParser:
    """Describe the four commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="offline verification and policy")
    check.add_argument("--selection", type=Path, required=True)
    check.add_argument("--key", required=True)
    check.add_argument("--proposal-dir", type=Path, required=True)
    check.add_argument("--workdir", type=Path, required=True)
    check.add_argument("--coauthors", type=Path, required=True)
    check.add_argument("--output", type=Path, required=True)
    check.add_argument("--summary", type=Path)
    check.add_argument("--run-url", default="")
    check.set_defaults(handler=write_check)

    apply = commands.add_parser("apply", help="create branch, commits and PR")
    apply.add_argument("--check", type=Path, required=True)
    apply.add_argument("--workdir", type=Path, required=True)
    apply.add_argument(
        "--mode", required=True, choices=("select", "branches", "pull-requests")
    )
    apply.add_argument("--dry-run", action="store_true")
    apply.add_argument("--output", type=Path, required=True)
    apply.set_defaults(handler=write_apply)

    comment = commands.add_parser("comment", help="comment the outcome on the issue")
    comment.add_argument("--result", type=Path, required=True)
    comment.add_argument("--run-url", default="")
    comment.set_defaults(handler=comments.run_comment)

    report = commands.add_parser("report", help="merge results into a table")
    report.add_argument("--results", type=Path, required=True)
    report.add_argument("--output-md", type=Path, required=True)
    report.add_argument("--output-json", type=Path, required=True)
    report.set_defaults(handler=reporting.run_report)
    return parser


def main(argv: list[str] | None = None) -> None:
    """Dispatch a command, keeping GitHub failures on the error path."""
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = cast("Callable[[argparse.Namespace], None]", args.handler)
    try:
        handler(args)
    except (OSError, ValueError, PublishError, github.GitHubError) as exc:
        message = ascii(str(exc)).replace("::", ": :").replace("##[", "# #[")
        parser.exit(1, f"publish: {message}\n")


if __name__ == "__main__":
    main()
