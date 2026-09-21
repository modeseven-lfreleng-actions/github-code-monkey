# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The verdict record the publisher builds and the report renders."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import proposal_policy as policy
from proposal_policy import Identity, PublishError

SCHEMA = 1


def load_json(path: Path, context: str) -> dict[str, Any]:
    """Parse a JSON object file, treating anything else as an error."""
    try:
        parsed: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise PublishError(f"{context}: unreadable JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PublishError(f"{context}: expected a JSON object")
    return cast("dict[str, Any]", parsed)


@dataclass
class Check:
    """Accumulates the verdict for one proposal."""

    key: str
    repository: str
    issue: int
    branch: str
    base_sha: str
    default_branch: str
    verdict: str = "proposed"
    reasons: list[str] = field(default_factory=lambda: cast("list[str]", []))
    needs_workflows: bool = False
    commits: list[dict[str, Any]] = field(
        default_factory=lambda: cast("list[dict[str, Any]]", [])
    )
    pr_title: str = ""
    pr_body: str = ""
    files_changed: int = 0
    added_bytes: int = 0
    commands: list[dict[str, Any]] = field(
        default_factory=lambda: cast("list[dict[str, Any]]", [])
    )
    premium_requests: int | None = None
    agent_seconds: int | None = None

    @classmethod
    def from_entry(cls, key: str, entry: dict[str, Any]) -> Check:
        """Start a check from a trusted selection entry."""
        return cls(
            key=key,
            repository=str(entry["repository"]),
            issue=int(entry["number"]),
            branch=str(entry["branch"]),
            base_sha=str(entry["base_sha"]),
            default_branch=str(entry["default_branch"]),
        )

    def to_json(self) -> dict[str, Any]:
        """Serialise for check.json."""
        return {
            "schema": SCHEMA,
            "key": self.key,
            "repository": self.repository,
            "issue": self.issue,
            "branch": self.branch,
            "base_sha": self.base_sha,
            "default_branch": self.default_branch,
            "verdict": self.verdict,
            "reasons": self.reasons,
            "needs_workflows": self.needs_workflows,
            "commits": self.commits,
            "pr_title": self.pr_title,
            "pr_body": self.pr_body,
            "stats": {
                "files_changed": self.files_changed,
                "added_bytes": self.added_bytes,
            },
            "commands": self.commands,
            "premium_requests": self.premium_requests,
            "agent_seconds": self.agent_seconds,
        }


def read_usage(path: Path, check: Check) -> None:
    """Pull the spend figures from the CLI's usage file, if present."""
    if not path.is_file():
        return
    try:
        usage = load_json(path, "usage")
    except PublishError as exc:
        check.reasons.append(f"usage.json unreadable: {exc}")
        return
    cost = usage_number(usage.get("totalPremiumRequestCost"))
    if cost is not None:
        check.premium_requests = int(round(cost))
    duration = usage_number(usage.get("totalApiDurationMs"))
    if duration is not None:
        check.agent_seconds = int(duration // 1000)


def usage_number(value: Any) -> float | None:
    """A finite, non-negative number from untrusted JSON, or None.

    Booleans are ints in Python and 1e309 parses to infinity; neither
    is a spend figure, and either would corrupt a total or make
    ``round`` raise inside the publisher.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        # A JSON integer with hundreds of digits does not fit a float.
        return None
    if not math.isfinite(number) or number < 0 or number > 1e12:
        return None
    return number


@dataclass(frozen=True)
class Context:
    """Everything the verification needs beyond the proposal itself."""

    proposal_dir: Path
    workdir: Path
    identity: Identity
    model: str
    run_url: str
    issue_url: str


def check_summary(check: Check) -> str:
    """Markdown for the step summary."""
    lines = [f"### {check.repository}#{check.issue} — `{check.verdict}`", ""]
    for reason in check.reasons:
        lines.append(f"- {policy.log_safe(reason)}")
    if check.commits:
        lines += ["", "| Commit | Files | Bytes |", "| --- | --- | --- |"]
        for commit in check.commits:
            files = len(commit["additions"]) + len(commit["deletions"])
            size = sum(int(item["size"]) for item in commit["additions"])
            headline = str(commit["headline"]).replace("|", "\\|")
            lines.append(f"| {headline} | {files} | {size} |")
        lines += ["", f"Needs `workflows: write`: {check.needs_workflows}", ""]
    return "\n".join(lines) + "\n"
