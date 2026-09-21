# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Merge every per-issue ``result.json`` into the run's report."""

from __future__ import annotations

import argparse
import json
from typing import Any, cast

import proposal_model as model
import proposal_policy as policy
from proposal_policy import PublishError

SCHEMA = 1


def report_row(item: dict[str, Any]) -> str:
    """One table row for a result."""
    verdict = str(item.get("verdict"))
    requests = item.get("premium_requests")
    output = item.get("pull_request_url") or item.get("branch_url") or "—"
    if item.get("dry_run") and verdict == "proposed":
        output = "dry run"
    notes = [str(r) for r in cast("list[Any]", item.get("reasons") or [])]
    notes += [str(w) for w in cast("list[Any]", item.get("warnings") or [])]
    # Choose the text first, then make it one escaped table cell: the
    # title and the reasons alike descend from agent output.
    text = "; ".join(notes) or str(item.get("pr_title") or "")
    detail = policy.log_safe(text).replace("|", "\\|")[:300]
    return (
        f"| {item.get('repository')}#{item.get('issue')} | {verdict} | {output} "
        f"| {requests if requests is not None else '—'} | {detail} |"
    )


def run_report(args: argparse.Namespace) -> None:
    """Merge every result.json into one table."""
    results: list[dict[str, Any]] = []
    unreadable: list[str] = []
    for path in sorted(args.results.rglob("result.json")):
        try:
            results.append(model.load_json(path, str(path)))
        except PublishError as exc:
            unreadable.append(str(exc))
    lines = [
        "## Code monkey results",
        "",
        "| Issue | Verdict | Output | Premium requests | Detail |",
        "| --- | --- | --- | --- | --- |",
    ]
    totals: dict[str, int] = dict.fromkeys(policy.VERDICTS, 0)
    spend = 0
    for item in results:
        verdict = str(item.get("verdict"))
        totals[verdict] = totals.get(verdict, 0) + 1
        requests = item.get("premium_requests")
        if type(requests) is int:
            spend += requests
        lines.append(report_row(item))
    for problem in unreadable:
        lines.append(f"| — | unreadable | — | — | {problem.replace('|', '/')[:300]} |")
    if not results and not unreadable:
        lines.append("| — | — | — | — | no proposals |")
    lines += [
        "",
        f"Proposed {totals['proposed']}, abstained {totals['abstain']}, "
        f"rejected {totals['rejected']}, failed {totals['author-failed']}, "
        f"publish failures {totals.get('publish-failed', 0)}; "
        f"premium requests {spend}.",
        "",
    ]
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.write_text("\n".join(lines), encoding="utf-8")
    args.output_json.write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "totals": totals,
                "premium_requests": spend,
                "unreadable": unreadable,
                "results": results,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
