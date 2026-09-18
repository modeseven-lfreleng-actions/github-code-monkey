# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Choose the issues a run will work and write the trusted selection.

Runs in the trusted select job with a read token. Scans the open
issues of an owner, drops the ones policy excludes, ranks the rest
by Priority then age, keeps one per repository, and caps the list.
For each survivor it records the default-branch SHA the agent will
build on, the filtered comments, and the branch name the publisher
will create. It also fetches the organisation's ``AGENTS.md`` at a
recorded commit so the author job never reads it live.

Outputs in ``--output-dir``: ``selection.json``, ``matrix.json``,
``agents.md``, ``excluded-repos.txt`` and ``selection-summary.md``.
See DESIGN.md section 18.1 for the selection schema.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import issue_reads as reads
import monkey_github as github
from issue_reads import SelectionError

SCHEMA = 1
REPO_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
PRIORITY_RANK = {"Urgent": 0, "High": 1, "Medium": 2, "Low": 3}
SKIP_LABELS = frozenset({"question", "breaking-change", "chore", "no-agent"})
BRANCH_PREFIX = "code-monkey/issue-"


def parse_repositories(text: str) -> list[str]:
    """Split a repository list on commas and whitespace, in given order."""
    names: list[str] = []
    for token in re.split(r"[,\s]+", text.strip()):
        if not token:
            continue
        if not REPO_NAME_RE.fullmatch(token) or token in (".", ".."):
            raise SelectionError(f"invalid repository name {token!r}")
        lowered = token.lower()
        if lowered not in names:
            names.append(lowered)
    return names


def load_exclusions(file: Path | None, override: str) -> list[str]:
    """Resolve the exclusion list: an explicit override beats the bundled file."""
    if override.strip():
        return parse_repositories(override)
    if file is None:
        return []
    if not file.is_file():
        raise SelectionError(f"exclusion file {file} is not a regular file")
    names: list[str] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            names.extend(parse_repositories(stripped))
    return names


def parse_max_issues(text: str) -> int:
    """Accept a non-negative integer; zero lifts the cap."""
    if not re.fullmatch(r"\d+", text.strip()):
        raise SelectionError(f"max_issues must be a non-negative integer, got {text!r}")
    return int(text)


def rank_key(candidate: dict[str, Any]) -> tuple[int, str, str, int]:
    """Order by priority, then age, then a stable name for determinism."""
    priority = candidate.get("priority")
    rank = (
        PRIORITY_RANK.get(priority, len(PRIORITY_RANK))
        if priority
        else len(PRIORITY_RANK)
    )
    return (
        rank,
        str(candidate["created_at"]),
        str(candidate["repository"]),
        int(candidate["number"]),
    )


def cheap_filter(
    issues: list[dict[str, Any]],
    repositories: dict[str, dict[str, Any]],
    *,
    exclusions: set[str],
    explicit: list[str],
    include_dotgithub: bool,
    skipped: dict[str, int],
) -> list[dict[str, Any]]:
    """Apply the filters the search payload alone can answer."""
    kept: list[dict[str, Any]] = []
    for issue in issues:
        if issue.get("isPullRequest"):
            skipped["pull_request"] += 1
            continue
        repository = issue.get("repository")
        if not isinstance(repository, dict):
            raise github.GitHubError("search entry without a repository")
        repo_data = cast("dict[str, Any]", repository)
        full = github.require_str(repo_data, "nameWithOwner", "search repository")
        name = github.require_str(repo_data, "name", "search repository")
        lowered = name.lower()
        meta = repositories.get(lowered)
        if not explicit and (
            lowered in exclusions
            or (lowered == ".github" and not include_dotgithub)
            or meta is None
            or meta["archived"]
            or meta["template"]
            or meta["fork"]
        ):
            skipped["repository"] += 1
            continue
        if meta is None or not meta.get("default_branch"):
            skipped["repository"] += 1
            continue
        labels = reads.label_names(issue.get("labels"))
        if SKIP_LABELS.intersection(labels):
            skipped["label"] += 1
            continue
        kept.append(
            {
                "repository": full,
                "repo_name": name,
                "number": github.require_int(issue, "number", "search issue"),
                "url": github.require_str(issue, "url", "search issue"),
                "created_at": github.require_str(issue, "createdAt", "search issue"),
                "author_association": issue.get("authorAssociation"),
                "default_branch": meta["default_branch"],
            }
        )
    return kept


def enrich(
    candidates: list[dict[str, Any]], *, include_assigned: bool, skipped: dict[str, int]
) -> list[dict[str, Any]]:
    """Add the per-issue reads ranking needs and drop untriaged or claimed issues."""
    enriched: list[dict[str, Any]] = []
    for candidate in candidates:
        details = reads.issue_details(candidate["repository"], candidate["number"])
        if details["type"] is None:
            skipped["no_type"] += 1
            continue
        if details["assignees"] and not include_assigned:
            skipped["assigned"] += 1
            continue
        if SKIP_LABELS.intersection(details["labels"]):
            skipped["label"] += 1
            continue
        enriched.append({**candidate, **details})
    return enriched


def choose(
    ranked: list[dict[str, Any]], *, max_issues: int, skipped: dict[str, int]
) -> list[dict[str, Any]]:
    """Keep one issue per repository, run the expensive checks, and cap."""
    seen: set[str] = set()
    chosen: list[dict[str, Any]] = []
    for candidate in ranked:
        repo = str(candidate["repository"])
        if repo in seen:
            skipped["one_per_repo"] += 1
            continue
        if max_issues and len(chosen) >= max_issues:
            skipped["cap"] += 1
            continue
        number = int(candidate["number"])
        branch = f"{BRANCH_PREFIX}{number}"
        if reads.prior_attempt(repo, branch):
            skipped["attempted"] += 1
            continue
        if reads.has_open_linked_pr(repo, number):
            skipped["linked_pr"] += 1
            continue
        seen.add(repo)
        comments, dropped = reads.filtered_comments(repo, number)
        chosen.append(
            {
                "key": f"{candidate['repo_name']}-{number}",
                **{k: v for k, v in candidate.items() if k != "assignees"},
                "base_sha": reads.branch_head(repo, str(candidate["default_branch"])),
                "branch": branch,
                "comments": comments,
                "comments_dropped": dropped,
            }
        )
    return chosen


def summary_markdown(selection: dict[str, Any]) -> str:
    """Render the selection for the step summary."""
    lines = [
        "## Issue selection",
        "",
        f"Mode `{selection['mode']}`, dry run `{selection['dry_run']}`, "
        f"model `{selection['model']}`.",
        f"Candidates seen: {selection['candidates_seen']}; "
        f"selected: {len(selection['issues'])}.",
        "",
        "| Skipped because | Count |",
        "| --- | --- |",
    ]
    for reason, count in sorted(cast("dict[str, int]", selection["skipped"]).items()):
        lines.append(f"| {reason} | {count} |")
    lines += ["", "| Issue | Priority | Type | Title |", "| --- | --- | --- | --- |"]
    for issue in cast("list[dict[str, Any]]", selection["issues"]):
        title = str(issue["title"]).replace("|", "\\|")
        lines.append(
            f"| [{issue['repo_name']}#{issue['number']}]({issue['url']}) "
            f"| {issue['priority'] or '—'} | {issue['type']} | {title} |"
        )
    if not selection["issues"]:
        lines.append("| — | — | — | nothing to work |")
    return "\n".join(lines) + "\n"


def build_selection(args: argparse.Namespace) -> tuple[dict[str, Any], bytes]:
    """Run the whole selection and return it with the guidance bytes."""
    explicit = parse_repositories(args.repositories)
    exclusions = set(load_exclusions(args.exclude_file, args.exclude_repos))
    max_issues = parse_max_issues(args.max_issues)
    skipped: dict[str, int] = dict.fromkeys(
        (
            "pull_request",
            "repository",
            "label",
            "no_type",
            "assigned",
            "attempted",
            "linked_pr",
            "one_per_repo",
            "cap",
        ),
        0,
    )
    repositories = reads.list_repositories(args.org)
    for name in explicit:
        if name not in repositories:
            raise SelectionError(f"repository {args.org}/{name} not found")
    issues = reads.search_open_issues(args.org, explicit)
    candidates = cheap_filter(
        issues,
        repositories,
        exclusions=exclusions,
        explicit=explicit,
        include_dotgithub=args.include_dotgithub,
        skipped=skipped,
    )
    enriched = enrich(
        candidates, include_assigned=args.include_assigned, skipped=skipped
    )
    ranked = sorted(enriched, key=rank_key)
    chosen = choose(ranked, max_issues=max_issues, skipped=skipped)
    guidance, commit = reads.fetch_guidance(
        args.guidance_repository, args.guidance_ref, args.guidance_path
    )
    selection: dict[str, Any] = {
        "schema": SCHEMA,
        "org": args.org,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": args.mode,
        "dry_run": bool(args.dry_run),
        "model": args.model,
        "bot": reads.bot_identity(args.bot_slug),
        "guidance": {
            "repository": args.guidance_repository,
            "path": args.guidance_path,
            "ref": args.guidance_ref,
            "commit": commit,
            "sha256": hashlib.sha256(guidance).hexdigest(),
        },
        "exclusions": sorted(exclusions) if not explicit else [],
        "explicit_repositories": explicit,
        "candidates_seen": len(issues),
        "skipped": skipped,
        "issues": chosen,
    }
    return selection, guidance


def write_outputs(directory: Path, selection: dict[str, Any], guidance: bytes) -> None:
    """Write every file the later jobs consume."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "selection.json").write_text(
        json.dumps(selection, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    include = [
        {
            "key": issue["key"],
            "repository": issue["repository"],
            "number": issue["number"],
            "base_sha": issue["base_sha"],
            "branch": issue["branch"],
        }
        for issue in cast("list[dict[str, Any]]", selection["issues"])
    ]
    (directory / "matrix.json").write_text(
        json.dumps({"include": include}) + "\n", encoding="utf-8"
    )
    (directory / "agents.md").write_bytes(guidance)
    (directory / "excluded-repos.txt").write_text(
        "".join(f"{name}\n" for name in cast("list[str]", selection["exclusions"])),
        encoding="utf-8",
    )
    (directory / "selection-summary.md").write_text(
        summary_markdown(selection), encoding="utf-8"
    )


def build_parser() -> argparse.ArgumentParser:
    """Describe the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--mode", required=True, choices=("select", "branches", "pull-requests")
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repositories", default="")
    parser.add_argument("--exclude-file", type=Path)
    parser.add_argument("--exclude-repos", default="")
    parser.add_argument("--include-dotgithub", action="store_true")
    parser.add_argument("--include-assigned", action="store_true")
    parser.add_argument("--max-issues", default="10")
    parser.add_argument("--guidance-repository", required=True)
    parser.add_argument("--guidance-ref", default="main")
    parser.add_argument("--guidance-path", default="AGENTS.md")
    parser.add_argument("--bot-slug", default="")
    return parser


def main(argv: list[str] | None = None) -> None:
    """Run the selection and write its outputs."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        selection, guidance = build_selection(args)
        write_outputs(args.output_dir, selection, guidance)
    except (OSError, ValueError, SelectionError, github.GitHubError) as exc:
        message = ascii(str(exc)).replace("::", ": :").replace("##[", "# #[")
        parser.exit(1, f"select issues: {message}\n")
    print(
        f"Selected {len(selection['issues'])} issue(s) from "
        f"{selection['candidates_seen']} open; skipped {selection['skipped']}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
