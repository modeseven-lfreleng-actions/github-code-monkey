# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Offline verification of an agent's proposal against the trusted selection.

Runs without credentials. Cross-checks the untrusted manifest against
``selection.json``, verifies the git bundle against a fresh fetch of
the recorded base commit, walks every commit's diff against the file
policy, checks each message, and produces a ``Check`` whose verdict
the publisher acts on. See DESIGN.md sections 8 and 18.3.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, cast

import monkey_github as github
import proposal_policy as policy
from proposal_model import Check, Context, load_json, read_usage
from proposal_policy import Identity, PublishError, Rejection


def git(workdir: Path, *args: str, binary: bool = False) -> bytes | str:
    """Run git in the working clone, failing loudly on error."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(workdir), *args],
            capture_output=True,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublishError(f"git {' '.join(args)}: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise PublishError(f"git {' '.join(args[:2])} failed: {detail[:500]}")
    return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")


def field_str(data: dict[str, Any], key: str, context: str) -> str:
    """A required string in a local file; a miss is a publish fault, not GitHub's."""
    try:
        return github.require_str(data, key, context)
    except github.GitHubError as exc:
        raise PublishError(str(exc)) from exc


def field_int(data: dict[str, Any], key: str, context: str) -> int:
    """A required positive integer in a local file."""
    try:
        return github.require_int(data, key, context)
    except github.GitHubError as exc:
        raise PublishError(str(exc)) from exc


def selection_entry(selection: dict[str, Any], key: str) -> dict[str, Any]:
    """Find the trusted selection entry for a matrix key."""
    if not policy.KEY_RE.fullmatch(key):
        raise PublishError(f"invalid selection key {key!r}")
    issues = selection.get("issues")
    if not isinstance(issues, list):
        raise PublishError("selection carries no issue list")
    for entry in cast("list[Any]", issues):
        if isinstance(entry, dict) and cast("dict[str, Any]", entry).get("key") == key:
            data = cast("dict[str, Any]", entry)
            for name in ("repository", "base_sha", "branch", "default_branch", "url"):
                field_str(data, name, "selection entry")
            field_int(data, "number", "selection entry")
            if not policy.SHA_RE.fullmatch(str(data["base_sha"])):
                raise PublishError("selection base_sha is not a commit SHA")
            return data
    raise PublishError(f"selection has no entry for {key!r}")


def selection_identity(
    selection: dict[str, Any], coauthors: dict[str, Any]
) -> Identity:
    """Resolve the trailer identities from the trusted selection."""
    bot = selection.get("bot")
    if not isinstance(bot, dict):
        raise PublishError("selection carries no bot identity")
    bot_data = cast("dict[str, Any]", bot)
    model = field_str(selection, "model", "selection")
    return Identity(
        coauthor=policy.coauthor_for(model, coauthors),
        bot_login=field_str(bot_data, "login", "bot"),
        bot_email=field_str(bot_data, "email", "bot"),
    )


def read_manifest(path: Path, check: Check) -> dict[str, Any]:
    """Read the untrusted manifest and cross-check it against the selection."""
    manifest = load_json(path, "manifest")
    outcome = manifest.get("outcome")
    if outcome not in policy.MANIFEST_OUTCOMES:
        raise Rejection(f"manifest outcome {outcome!r} is not recognised")
    if outcome != "proposed":
        reason = manifest.get("reason")
        check.verdict = str(outcome)
        check.reasons.append(
            reason if isinstance(reason, str) and reason else "no reason given"
        )
        return manifest
    expected = {
        "repository": check.repository,
        "issue": check.issue,
        "base_sha": check.base_sha,
        "branch": check.branch,
    }
    mismatches = [
        name for name, value in expected.items() if manifest.get(name) != value
    ]
    if mismatches:
        raise Rejection(
            "manifest disagrees with the trusted selection on " + ", ".join(mismatches)
        )
    commands = manifest.get("commands")
    if isinstance(commands, list):
        for item in cast("list[Any]", commands)[:50]:
            if isinstance(item, dict):
                data = cast("dict[str, Any]", item)
                command = data.get("command")
                code = data.get("exit_code")
                if isinstance(command, str):
                    check.commands.append(
                        {
                            "command": command[:200],
                            "exit_code": code if type(code) is int else None,
                        }
                    )
    return manifest


def prepare_clone(workdir: Path, repository: str, base_sha: str) -> Path:
    """Fetch the base commit from GitHub into a fresh clone without credentials."""
    clone = workdir / "clone"
    clone.mkdir(parents=True, exist_ok=True)
    git(clone, "init", "-q")
    git(clone, "remote", "add", "origin", f"https://github.com/{repository}.git")
    git(
        clone,
        "-c",
        "protocol.version=2",
        "fetch",
        "-q",
        "--depth=1",
        "origin",
        base_sha,
    )
    resolved = str(git(clone, "rev-parse", "--verify", "FETCH_HEAD^{commit}")).strip()
    if resolved != base_sha:
        raise PublishError("fetched commit does not match the recorded base")
    return clone


def import_bundle(clone: Path, bundle: Path, branch: str, base_sha: str) -> list[str]:
    """Verify and import the bundle; return the new commits oldest first."""
    try:
        git(clone, "bundle", "verify", str(bundle))
    except PublishError as exc:
        raise Rejection(f"bundle failed verification: {exc}") from exc
    heads = str(git(clone, "bundle", "list-heads", str(bundle))).split()
    if f"refs/heads/{branch}" not in heads:
        raise Rejection(f"bundle does not carry refs/heads/{branch}")
    git(clone, "fetch", "-q", str(bundle), f"refs/heads/{branch}:refs/bundle/head")
    listing = str(
        git(
            clone, "rev-list", "--reverse", "--parents", f"{base_sha}..refs/bundle/head"
        )
    ).split("\n")
    commits: list[str] = []
    expected_parent = base_sha
    for line in listing:
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 2:
            raise Rejection("history is not linear: a merge commit is present")
        sha, parent = parts
        if parent != expected_parent:
            raise Rejection("history does not descend linearly from the base")
        commits.append(sha)
        expected_parent = sha
    if not commits:
        raise Rejection("bundle adds no commits on top of the base")
    if len(commits) > policy.MAX_COMMITS:
        raise Rejection(
            f"{len(commits)} commits exceed the limit of {policy.MAX_COMMITS}"
        )
    return commits


def gitlint_text(clone: Path, base_sha: str) -> str | None:
    """The target's .gitlint at the base commit, if it has one."""
    try:
        return str(git(clone, "show", f"{base_sha}:.gitlint"))
    except PublishError:
        return None


def is_binary(clone: Path, blob: str) -> bool:
    """Treat a NUL byte in the first 8 KiB as binary."""
    content = cast("bytes", git(clone, "cat-file", "blob", blob, binary=True))
    return b"\0" in content[:8192]


def walk_diff(clone: Path, parent: str, sha: str, check: Check) -> dict[str, Any]:
    """Classify one commit's changes against the file policy."""
    raw = cast(
        "bytes",
        git(
            clone,
            "diff-tree",
            "-r",
            "-z",
            "--no-renames",
            "--no-commit-id",
            parent,
            sha,
            binary=True,
        ),
    )
    fields = raw.split(b"\0")
    additions: list[dict[str, Any]] = []
    deletions: list[str] = []
    index = 0
    while index < len(fields) and fields[index]:
        meta = fields[index].decode("utf-8", "replace")
        path = (
            fields[index + 1].decode("utf-8", "replace")
            if index + 1 < len(fields)
            else ""
        )
        index += 2
        parts = meta.lstrip(":").split()
        if len(parts) != 5:
            raise PublishError(f"unexpected diff-tree entry {meta!r}")
        old_mode, new_mode, _old_blob, new_blob, status = parts
        if not policy.safe_path(path):
            raise Rejection(f"unsafe path {path!r}")
        if policy.protected(path):
            raise Rejection(f"{path} is protected and cannot change")
        if status.startswith("D"):
            deletions.append(path)
            continue
        policy.check_change_mode(path, status, old_mode, new_mode)
        size = int(str(git(clone, "cat-file", "-s", new_blob)).strip())
        if is_binary(clone, new_blob) and size > policy.MAX_BINARY_BYTES:
            raise Rejection(
                f"{path}: binary file exceeds {policy.MAX_BINARY_BYTES} bytes"
            )
        check.added_bytes += size
        if check.added_bytes > policy.MAX_ADDED_BYTES:
            raise Rejection(f"total added bytes exceed {policy.MAX_ADDED_BYTES}")
        if path.startswith(policy.WORKFLOW_PREFIX):
            check.needs_workflows = True
        additions.append({"path": path, "blob": new_blob, "size": size})
    check.files_changed += len(additions) + len(deletions)
    return {"additions": additions, "deletions": deletions}


def verify_proposal(check: Check, manifest: dict[str, Any], context: Context) -> None:
    """Everything after the manifest says ``proposed``; raises Rejection."""
    bundle = context.proposal_dir / "changes.bundle"
    if not bundle.is_file():
        raise Rejection("proposal lacks changes.bundle")
    clone = prepare_clone(context.workdir, check.repository, check.base_sha)
    commits = import_bundle(clone, bundle, check.branch, check.base_sha)
    limit = policy.parse_title_limit(gitlint_text(clone, check.base_sha))
    parent = check.base_sha
    for sha in commits:
        diff = walk_diff(clone, parent, sha, check)
        message = str(git(clone, "log", "-1", "--format=%B", sha))
        headline, body = policy.compose_message(message, context.identity, limit)
        check.commits.append({"sha": sha, "headline": headline, "body": body, **diff})
        parent = sha
    single = check.commits[0]["headline"] if len(check.commits) == 1 else None
    title, body = policy.check_pull_request_text(
        manifest.get("pr_title"),
        manifest.get("pr_body"),
        issue=check.issue,
        single_headline=single,
    )
    check.pr_title = title
    provenance = policy.provenance_block(
        model=context.model,
        run_url=context.run_url,
        issue_url=context.issue_url,
        base_sha=check.base_sha,
        commands=check.commands,
    )
    check.pr_body = body + "\n" + provenance


def run_check(
    *,
    selection_path: Path,
    key: str,
    proposal_dir: Path,
    workdir: Path,
    coauthors_path: Path,
    run_url: str,
) -> Check:
    """The offline verification pipeline."""
    selection = load_json(selection_path, "selection")
    entry = selection_entry(selection, key)
    context = Context(
        proposal_dir=proposal_dir,
        workdir=workdir,
        identity=selection_identity(selection, load_json(coauthors_path, "coauthors")),
        model=field_str(selection, "model", "selection"),
        run_url=run_url,
        issue_url=str(entry["url"]),
    )
    check = Check.from_entry(key, entry)
    read_usage(proposal_dir / "usage.json", check)
    try:
        manifest = read_manifest(proposal_dir / "manifest.json", check)
        if check.verdict == "proposed":
            verify_proposal(check, manifest, context)
    except Rejection as exc:
        check.verdict = "rejected"
        check.reasons.append(str(exc))
        check.commits = []
    return check
