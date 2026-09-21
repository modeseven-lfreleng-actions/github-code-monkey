# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""GitHub reads the issue selection depends on.

Every function here reads and never writes. ``select_issues`` owns
the policy (what to skip, how to rank); this module answers the
questions it asks: which repositories exist, which issues are open,
what Priority and Type they carry, whether the bot already tried
one, and what the organisation guidance says at a recorded commit.
"""

from __future__ import annotations

import base64
import binascii
import re
import urllib.parse
from typing import Any, cast

import monkey_github as github

SEARCH_LIMIT = 1000
SHA_RE = re.compile(r"[0-9a-f]{40}")
COMMENT_ASSOCIATIONS = frozenset({"OWNER", "MEMBER"})
MAX_COMMENTS = 20
MAX_COMMENT_BYTES = 64 * 1024
MAX_BODY_BYTES = 256 * 1024
# Matches monkey_evidence.MAX_GUIDANCE_BYTES, which verifies agents.md.
MAX_GUIDANCE_BYTES = 1024 * 1024
PLACEHOLDER_BOT = "code-monkey[bot]"

SEARCH_FIELDS = (
    "repository,number,title,url,labels,assignees,authorAssociation,"
    "createdAt,isPullRequest,state"
)


class SelectionError(Exception):
    """The selection cannot proceed; an operational failure, not a skip."""


def list_repositories(org: str) -> dict[str, dict[str, Any]]:
    """Map lower-cased repository names to the metadata selection needs."""
    raw = github.run_gh(
        [
            "repo",
            "list",
            org,
            "--limit",
            str(SEARCH_LIMIT),
            "--json",
            "name,isArchived,isTemplate,isFork,isPrivate,visibility,defaultBranchRef",
        ]
    )
    parsed = github.decode_response(raw)
    if not isinstance(parsed, list):
        raise github.GitHubError("expected a repository array")
    repositories: dict[str, dict[str, Any]] = {}
    for entry in cast("list[Any]", parsed):
        if not isinstance(entry, dict):
            raise github.GitHubError("expected repository objects")
        data = cast("dict[str, Any]", entry)
        name = github.require_str(data, "name", "repository")
        default = data.get("defaultBranchRef")
        branch = (
            cast("dict[str, Any]", default).get("name")
            if isinstance(default, dict)
            else None
        )
        visibility = data.get("visibility")
        repositories[name.lower()] = {
            "name": name,
            "archived": bool(data.get("isArchived")),
            "template": bool(data.get("isTemplate")),
            "fork": bool(data.get("isFork")),
            # Private and internal repositories are out of scope (DESIGN
            # 2, 16): the author job clones without a credential.
            "public": not data.get("isPrivate")
            and isinstance(visibility, str)
            and visibility.upper() == "PUBLIC",
            "default_branch": branch if isinstance(branch, str) and branch else None,
        }
    if len(repositories) >= SEARCH_LIMIT:
        raise SelectionError("repository listing reached the search ceiling")
    return repositories


def search_open_issues(org: str, repositories: list[str]) -> list[dict[str, Any]]:
    """Return every open issue of the owner, or of the named repositories."""
    args = [
        "search",
        "issues",
        "--owner",
        org,
        "--state",
        "open",
        "--limit",
        str(SEARCH_LIMIT),
        "--json",
        SEARCH_FIELDS,
    ]
    for name in repositories:
        args.extend(["--repo", f"{org}/{name}"])
    parsed = github.decode_response(github.run_gh(args))
    if not isinstance(parsed, list):
        raise github.GitHubError("expected an issue array from search")
    entries = cast("list[Any]", parsed)
    if len(entries) >= SEARCH_LIMIT:
        raise SelectionError(
            f"search returned {SEARCH_LIMIT} results; narrow the repository scope"
        )
    issues: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise github.GitHubError("expected issue objects from search")
        issues.append(cast("dict[str, Any]", entry))
    return issues


def label_names(raw: Any) -> list[str]:
    """Extract label names from either label objects or bare strings."""
    if not isinstance(raw, list):
        raise github.GitHubError("expected a label array")
    names: list[str] = []
    for label in cast("list[Any]", raw):
        name = (
            cast("dict[str, Any]", label).get("name")
            if isinstance(label, dict)
            else label
        )
        if not isinstance(name, str) or not name:
            raise github.GitHubError("label without a name")
        names.append(name)
    return names


def truncate_utf8(text: str, limit: int) -> str:
    """Bound a string by encoded bytes, never splitting a character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", "ignore")


def issue_details(repo: str, number: int) -> dict[str, Any]:
    """Read type, priority, body and assignees for one candidate."""
    issue = github.api_object(f"repos/{repo}/issues/{number}")
    if github.require_int(issue, "number", "issue") != number:
        raise github.GitHubError("issue response does not match the request")
    if issue.get("pull_request") is not None:
        raise github.GitHubError(f"{repo}#{number} is a pull request")
    body = issue.get("body")
    if body is not None and not isinstance(body, str):
        raise github.GitHubError("invalid issue body")
    issue_type = issue.get("type")
    type_name: str | None = None
    if isinstance(issue_type, dict):
        name = cast("dict[str, Any]", issue_type).get("name")
        if isinstance(name, str) and name:
            type_name = name
    assignees_raw = issue.get("assignees")
    assignees: list[str] = []
    if isinstance(assignees_raw, list):
        for person in cast("list[Any]", assignees_raw):
            if isinstance(person, dict):
                login = cast("dict[str, Any]", person).get("login")
                if isinstance(login, str):
                    assignees.append(login)
    return {
        "title": github.require_str(issue, "title", "issue"),
        "body": truncate_utf8(body or "", MAX_BODY_BYTES),
        "labels": label_names(issue.get("labels")),
        "type": type_name,
        "assignees": assignees,
        "priority": read_priority(repo, number),
    }


def read_priority(repo: str, number: int) -> str | None:
    """Return the Priority field value, or None when unset."""
    values = github.api_list(f"repos/{repo}/issues/{number}/issue-field-values")
    for value in values:
        if value.get("issue_field_name") != "Priority":
            continue
        option = value.get("single_select_option")
        if isinstance(option, dict):
            name = cast("dict[str, Any]", option).get("name")
            if isinstance(name, str) and name:
                return name
        return None
    return None


def prior_attempt(repo: str, branch: str) -> bool:
    """Whether a pull request from the bot branch exists, open or closed.

    ``--head`` matches the branch name alone, and anyone can open a
    pull request from a fork branch of that name against a public
    repository. Only a pull request whose head lives in the target
    repository itself counts; the branch check below covers a branch
    with no pull request yet.
    """
    raw = github.run_gh(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--head",
            branch,
            "--state",
            "all",
            "--limit",
            "20",
            "--json",
            "number,isCrossRepository,headRepository",
        ]
    )
    parsed = github.decode_response(raw)
    if not isinstance(parsed, list):
        raise github.GitHubError("expected a pull request array")
    for entry in cast("list[Any]", parsed):
        if not isinstance(entry, dict):
            raise github.GitHubError("expected pull request objects")
        data = cast("dict[str, Any]", entry)
        head = data.get("headRepository")
        head_name = (
            cast("dict[str, Any]", head).get("nameWithOwner")
            if isinstance(head, dict)
            else None
        )
        if data.get("isCrossRepository") is False or (
            isinstance(head_name, str) and head_name.lower() == repo.lower()
        ):
            return True
    try:
        github.api_object(f"repos/{repo}/branches/{branch}")
    except github.GitHubError as exc:
        if github.is_absent(exc):
            return False
        raise
    return True


LINKED_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      closedByPullRequestsReferences(first: 10, includeClosedPrs: false) {
        totalCount
      }
    }
  }
}
"""


def has_open_linked_pr(repo: str, number: int) -> bool:
    """Whether an open pull request is linked to close the issue."""
    owner, _, name = repo.partition("/")
    data = github.graphql(
        LINKED_QUERY, {"owner": owner, "name": name, "number": number}
    )
    repository = data.get("repository")
    issue = (
        cast("dict[str, Any]", repository).get("issue")
        if isinstance(repository, dict)
        else None
    )
    refs = (
        cast("dict[str, Any]", issue).get("closedByPullRequestsReferences")
        if isinstance(issue, dict)
        else None
    )
    if not isinstance(refs, dict):
        raise github.GitHubError("linked pull request query returned no data")
    count = cast("dict[str, Any]", refs).get("totalCount")
    return type(count) is int and count > 0


def branch_head(repo: str, ref: str) -> str:
    """Resolve a branch, tag or commit to its commit SHA.

    ``repos/{repo}/commits/{ref}`` treats a slash in the ref as a path
    separator, so a branch such as ``docs/topic`` returns 422. The git
    refs endpoint takes the full name and follows annotated tags to
    the commit they point at.
    """
    if SHA_RE.fullmatch(ref):
        return ref
    quoted = urllib.parse.quote(ref, safe="/")
    for kind in ("heads", "tags"):
        try:
            data = github.api_object(f"repos/{repo}/git/ref/{kind}/{quoted}")
        except github.GitHubError as exc:
            if github.is_absent(exc):
                continue
            raise
        target = data.get("object")
        if not isinstance(target, dict):
            raise github.GitHubError(f"ref {ref!r} has no object")
        obj = cast("dict[str, Any]", target)
        sha = github.require_str(obj, "sha", "ref object")
        if obj.get("type") == "tag":
            tag = github.api_object(f"repos/{repo}/git/tags/{sha}")
            inner = tag.get("object")
            if not isinstance(inner, dict):
                raise github.GitHubError(f"tag {ref!r} has no target")
            obj = cast("dict[str, Any]", inner)
            sha = github.require_str(obj, "sha", "tag")
        # A tag may point at a tree, a blob or another tag; provenance
        # records a commit, so accept nothing else.
        if obj.get("type") != "commit":
            raise github.GitHubError(f"ref {ref!r} does not resolve to a commit")
        if not SHA_RE.fullmatch(sha):
            raise github.GitHubError(f"unexpected commit SHA {sha!r}")
        return sha
    raise github.GitHubError(f"no branch or tag named {ref!r} in {repo}")


def filtered_comments(repo: str, number: int) -> tuple[list[dict[str, Any]], int]:
    """Keep comments from owners and members, bounded by count and bytes."""
    entries = github.api_list(f"repos/{repo}/issues/{number}/comments")
    kept: list[dict[str, Any]] = []
    dropped = 0
    total = 0
    for entry in entries:
        association = entry.get("author_association")
        body = entry.get("body")
        user = entry.get("user")
        login = (
            cast("dict[str, Any]", user).get("login")
            if isinstance(user, dict)
            else None
        )
        if (
            association not in COMMENT_ASSOCIATIONS
            or not isinstance(body, str)
            or not isinstance(login, str)
        ):
            dropped += 1
            continue
        size = len(body.encode("utf-8"))
        if len(kept) >= MAX_COMMENTS or total + size > MAX_COMMENT_BYTES:
            dropped += 1
            continue
        total += size
        kept.append(
            {
                "author": login,
                "association": association,
                "created_at": entry.get("created_at"),
                "body": body,
            }
        )
    return kept, dropped


def fetch_guidance(repository: str, ref: str, path: str) -> tuple[bytes, str]:
    """Return the guidance bytes and the commit they came from."""
    commit = branch_head(repository, ref)
    data = github.api_object(f"repos/{repository}/contents/{path}?ref={commit}")
    if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
        raise github.GitHubError("guidance file is not base64 content")
    try:
        encoded = cast("str", data["content"]).replace("\n", "")
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise github.GitHubError(f"guidance content is not decodable: {exc}") from exc
    if not content.strip():
        raise github.GitHubError("guidance file is empty")
    if len(content) > MAX_GUIDANCE_BYTES:
        # The evidence verifier refuses a larger agents.md, so fail here
        # with a clear message instead of in every matrix entry later.
        raise github.GitHubError(
            f"guidance file is {len(content)} bytes; the limit is {MAX_GUIDANCE_BYTES}"
        )
    return content, commit


def bot_identity(slug: str) -> dict[str, Any]:
    """Resolve the App's bot user, or a marked placeholder without an App."""
    if not slug:
        return {
            "login": PLACEHOLDER_BOT,
            "email": f"{PLACEHOLDER_BOT}@users.noreply.github.com",
            "placeholder": True,
        }
    login = f"{slug}[bot]"
    user = github.api_object(f"users/{login}")
    identifier = github.require_int(user, "id", "bot user")
    return {
        "login": login,
        "email": f"{identifier}+{login}@users.noreply.github.com",
        "placeholder": False,
    }
