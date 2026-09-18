# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""GitHub reads behind the selection, exercised against patched transport."""

from __future__ import annotations

import base64
import json
import sys
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
github = import_module("monkey_github")
reads = import_module("issue_reads")

SHA = "c" * 40
REF_COMMIT: dict[str, Any] = {
    "ref": "refs/heads/main",
    "object": {"type": "commit", "sha": SHA},
}


def comment(
    association: str, body: Any = "text", login: Any = "someone"
) -> dict[str, Any]:
    """One issue comment as the REST API returns it."""
    user: Any = {"login": login} if login is not None else None
    return {
        "author_association": association,
        "body": body,
        "user": user,
        "created_at": "2026-01-01T00:00:00Z",
    }


class ReadsCase(unittest.TestCase):
    """Base class forbidding any real ``gh`` invocation."""

    def setUp(self) -> None:
        """Fail fast when a test reaches the subprocess layer."""
        guard = patch.object(
            github.subprocess,
            "run",
            side_effect=AssertionError("unexpected subprocess"),
        )
        guard.start()
        self.addCleanup(guard.stop)


class FilteredCommentsTest(ReadsCase):
    """``filtered_comments`` keeps trusted, well-formed, bounded comments."""

    def test_association_filter(self) -> None:
        """OWNER and MEMBER survive; everyone else is counted as dropped."""
        entries = [
            comment("OWNER", "a", "own"),
            comment("CONTRIBUTOR", "b"),
            comment("NONE", "c"),
            comment("MEMBER", "d", "mem"),
            comment("COLLABORATOR", "e"),
        ]
        with patch.object(github, "api_list", return_value=entries) as read:
            kept, dropped = reads.filtered_comments("org/repo", 4)
        read.assert_called_once_with("repos/org/repo/issues/4/comments")
        self.assertEqual(dropped, 3)
        self.assertEqual(
            kept,
            [
                {
                    "author": "own",
                    "association": "OWNER",
                    "created_at": "2026-01-01T00:00:00Z",
                    "body": "a",
                },
                {
                    "author": "mem",
                    "association": "MEMBER",
                    "created_at": "2026-01-01T00:00:00Z",
                    "body": "d",
                },
            ],
        )

    def test_malformed_comments_dropped(self) -> None:
        """A non-string body, a missing user or a non-string login drops it."""
        entries = [
            comment("MEMBER", body=None),
            comment("MEMBER", body=["list"]),
            comment("MEMBER", login=None),
            comment("MEMBER", login=42),
            {"author_association": "MEMBER", "body": "x"},
            comment("OWNER", "ok"),
        ]
        with patch.object(github, "api_list", return_value=entries):
            kept, dropped = reads.filtered_comments("org/repo", 1)
        self.assertEqual(dropped, 5)
        self.assertEqual([c["body"] for c in kept], ["ok"])

    def test_count_limit(self) -> None:
        """Only the first ``MAX_COMMENTS`` survive; the rest count as dropped."""
        entries = [comment("MEMBER", str(i)) for i in range(reads.MAX_COMMENTS + 5)]
        with patch.object(github, "api_list", return_value=entries):
            kept, dropped = reads.filtered_comments("org/repo", 1)
        self.assertEqual(len(kept), reads.MAX_COMMENTS)
        self.assertEqual(dropped, 5)
        self.assertEqual(kept[0]["body"], "0")

    def test_byte_limit(self) -> None:
        """The byte budget is enforced on UTF-8 size and skips, not truncates."""
        big = "é" * (reads.MAX_COMMENT_BYTES // 2 - 10)  # 2 bytes each
        entries = [
            comment("MEMBER", big),
            comment("MEMBER", "x" * 30),  # would exceed the budget
            comment("MEMBER", "tiny"),  # still fits
        ]
        with patch.object(github, "api_list", return_value=entries):
            kept, dropped = reads.filtered_comments("org/repo", 1)
        self.assertEqual([c["body"] for c in kept], [big, "tiny"])
        self.assertEqual(dropped, 1)

    def test_single_oversize_comment_dropped(self) -> None:
        """One comment bigger than the whole budget is dropped, not kept."""
        entries = [comment("OWNER", "z" * (reads.MAX_COMMENT_BYTES + 1))]
        with patch.object(github, "api_list", return_value=entries):
            kept, dropped = reads.filtered_comments("org/repo", 1)
        self.assertEqual(kept, [])
        self.assertEqual(dropped, 1)


class BotIdentityTest(ReadsCase):
    """``bot_identity`` resolves the App bot or marks a placeholder."""

    def test_placeholder_without_slug(self) -> None:
        """No slug yields the marked placeholder without any read."""
        with patch.object(github, "api_object") as read:
            identity = reads.bot_identity("")
        read.assert_not_called()
        self.assertEqual(
            identity,
            {
                "login": "code-monkey[bot]",
                "email": "code-monkey[bot]@users.noreply.github.com",
                "placeholder": True,
            },
        )

    def test_resolved_slug(self) -> None:
        """A slug looks up the bot user and builds the noreply address."""
        with patch.object(
            github, "api_object", return_value={"id": 987, "login": "lf-monkey[bot]"}
        ) as read:
            identity = reads.bot_identity("lf-monkey")
        read.assert_called_once_with("users/lf-monkey[bot]")
        self.assertEqual(
            identity,
            {
                "login": "lf-monkey[bot]",
                "email": "987+lf-monkey[bot]@users.noreply.github.com",
                "placeholder": False,
            },
        )

    def test_invalid_id_raises(self) -> None:
        """A non-positive or non-integer id is refused."""
        for bad in ({"id": "987"}, {"id": 0}, {"id": True}, {}):
            with (
                self.subTest(bad=bad),
                patch.object(github, "api_object", return_value=bad),
                self.assertRaises(github.GitHubError),
            ):
                reads.bot_identity("lf-monkey")


class PriorAttemptTest(ReadsCase):
    """``prior_attempt`` consults pull requests first, then the branch."""

    def test_pull_request_exists(self) -> None:
        """A non-empty PR list is enough; the branch is not read."""
        with (
            patch.object(github, "run_gh", return_value='[{"number": 3}]') as gh,
            patch.object(github, "api_object") as read,
        ):
            self.assertTrue(reads.prior_attempt("org/repo", "code-monkey/issue-3"))
        read.assert_not_called()
        args = gh.call_args.args[0]
        self.assertEqual(args[:2], ["pr", "list"])
        self.assertIn("--head", args)
        self.assertEqual(args[args.index("--head") + 1], "code-monkey/issue-3")
        self.assertEqual(args[args.index("--state") + 1], "all")

    def test_no_pull_request_no_branch(self) -> None:
        """An empty list and a 404 on the branch means no prior attempt."""
        with (
            patch.object(github, "run_gh", return_value="[]"),
            patch.object(
                github,
                "api_object",
                side_effect=github.GitHubError("gh: Not Found (HTTP 404)"),
            ) as read,
        ):
            self.assertFalse(reads.prior_attempt("org/repo", "code-monkey/issue-3"))
        read.assert_called_once_with("repos/org/repo/branches/code-monkey/issue-3")

    def test_branch_exists_without_pull_request(self) -> None:
        """An orphaned bot branch still counts as an attempt."""
        with (
            patch.object(github, "run_gh", return_value="[]"),
            patch.object(github, "api_object", return_value={"name": "x"}),
        ):
            self.assertTrue(reads.prior_attempt("org/repo", "code-monkey/issue-3"))

    def test_branch_read_failure_propagates(self) -> None:
        """A 500 is not silently treated as absence."""
        with (
            patch.object(github, "run_gh", return_value="[]"),
            patch.object(
                github,
                "api_object",
                side_effect=github.GitHubError("gh: boom (HTTP 500)"),
            ),
            self.assertRaises(github.GitHubError),
        ):
            reads.prior_attempt("org/repo", "code-monkey/issue-3")

    def test_non_list_reply_raises(self) -> None:
        """A non-array reply from ``gh pr list`` is an error."""
        with (
            patch.object(github, "run_gh", return_value='{"number": 1}'),
            self.assertRaises(github.GitHubError),
        ):
            reads.prior_attempt("org/repo", "b")


class HasOpenLinkedPrTest(ReadsCase):
    """``has_open_linked_pr`` reads the GraphQL total count."""

    @staticmethod
    def reply(count: Any) -> dict[str, Any]:
        """A GraphQL data object with the given total count."""
        return {
            "repository": {
                "issue": {"closedByPullRequestsReferences": {"totalCount": count}}
            }
        }

    def test_counts(self) -> None:
        """Positive counts are True, zero and non-integers False."""
        for count, expected in ((3, True), (0, False), ("2", False), (True, False)):
            with (
                self.subTest(count=count),
                patch.object(github, "graphql", return_value=self.reply(count)) as gql,
            ):
                self.assertIs(reads.has_open_linked_pr("org/repo", 9), expected)
            self.assertEqual(
                gql.call_args.args[1], {"owner": "org", "name": "repo", "number": 9}
            )

    def test_missing_data_raises(self) -> None:
        """A reply without the references object is an error, not False."""
        for data in ({}, {"repository": None}, {"repository": {"issue": None}}):
            with (
                self.subTest(data=data),
                patch.object(github, "graphql", return_value=data),
                self.assertRaises(github.GitHubError),
            ):
                reads.has_open_linked_pr("org/repo", 9)


class IssueDetailsTest(ReadsCase):
    """``issue_details`` types and bounds the issue payload."""

    def issue(self, **overrides: Any) -> dict[str, Any]:
        """A REST issue object."""
        data: dict[str, Any] = {
            "number": 7,
            "title": "Title",
            "body": "Body",
            "labels": [{"name": "bug"}, "enhancement"],
            "type": {"name": "Bug"},
            "assignees": [{"login": "alice"}, {"login": 5}, "bob"],
        }
        data.update(overrides)
        return data

    def test_extracts_fields(self) -> None:
        """Type, labels, assignees and priority are extracted and typed."""
        with (
            patch.object(github, "api_object", return_value=self.issue()),
            patch.object(
                github,
                "api_list",
                return_value=[
                    {"issue_field_name": "Size", "single_select_option": {"name": "L"}},
                    {
                        "issue_field_name": "Priority",
                        "single_select_option": {"name": "High"},
                    },
                ],
            ),
        ):
            details = reads.issue_details("org/repo", 7)
        self.assertEqual(
            details,
            {
                "title": "Title",
                "body": "Body",
                "labels": ["bug", "enhancement"],
                "type": "Bug",
                "assignees": ["alice"],
                "priority": "High",
            },
        )

    def test_missing_type_and_priority(self) -> None:
        """No type object and no Priority field yield None for both."""
        with (
            patch.object(
                github, "api_object", return_value=self.issue(type=None, body=None)
            ),
            patch.object(github, "api_list", return_value=[]),
        ):
            details = reads.issue_details("org/repo", 7)
        self.assertIsNone(details["type"])
        self.assertIsNone(details["priority"])
        self.assertEqual(details["body"], "")

    def test_rejects_pull_request(self) -> None:
        """An issue payload carrying ``pull_request`` is refused."""
        with (
            patch.object(
                github, "api_object", return_value=self.issue(pull_request={"url": "u"})
            ),
            self.assertRaises(github.GitHubError),
        ):
            reads.issue_details("org/repo", 7)

    def test_rejects_number_mismatch(self) -> None:
        """A reply for a different issue number is refused."""
        with (
            patch.object(github, "api_object", return_value=self.issue(number=8)),
            self.assertRaises(github.GitHubError),
        ):
            reads.issue_details("org/repo", 7)

    def test_rejects_non_string_body(self) -> None:
        """A body that is neither string nor null is refused."""
        with (
            patch.object(github, "api_object", return_value=self.issue(body=["x"])),
            self.assertRaises(github.GitHubError),
        ):
            reads.issue_details("org/repo", 7)

    def test_body_truncated(self) -> None:
        """The body is cut at ``MAX_BODY_BYTES``."""
        long_body = "b" * (reads.MAX_BODY_BYTES + 100)
        with (
            patch.object(github, "api_object", return_value=self.issue(body=long_body)),
            patch.object(github, "api_list", return_value=[]),
        ):
            details = reads.issue_details("org/repo", 7)
        self.assertEqual(len(details["body"]), reads.MAX_BODY_BYTES)


class ReadPriorityTest(ReadsCase):
    """``read_priority`` handles unset and malformed Priority fields."""

    def test_priority_without_option(self) -> None:
        """A Priority field with no selected option is None."""
        with patch.object(
            github,
            "api_list",
            return_value=[
                {"issue_field_name": "Priority", "single_select_option": None}
            ],
        ):
            self.assertIsNone(reads.read_priority("org/repo", 1))


class SearchOpenIssuesTest(ReadsCase):
    """``search_open_issues`` scopes the search and refuses truncation."""

    def test_repository_scope_args(self) -> None:
        """Each named repository becomes a ``--repo owner/name`` pair."""
        with patch.object(github, "run_gh", return_value="[]") as gh:
            self.assertEqual(reads.search_open_issues("org", ["a", "b"]), [])
        args = gh.call_args.args[0]
        self.assertEqual(args[:2], ["search", "issues"])
        self.assertEqual(args[args.index("--owner") + 1], "org")
        self.assertEqual(args[args.index("--state") + 1], "open")
        self.assertEqual(args[args.index("--limit") + 1], str(reads.SEARCH_LIMIT))
        self.assertEqual(
            [args[i + 1] for i, a in enumerate(args) if a == "--repo"],
            ["org/a", "org/b"],
        )

    def test_no_repo_args_for_owner_scan(self) -> None:
        """Without explicit repositories no ``--repo`` argument appears."""
        with patch.object(github, "run_gh", return_value='[{"number": 1}]') as gh:
            issues = reads.search_open_issues("org", [])
        self.assertNotIn("--repo", gh.call_args.args[0])
        self.assertEqual(issues, [{"number": 1}])

    def test_hitting_limit_raises(self) -> None:
        """A result set at the ceiling is treated as truncated."""
        payload = json.dumps([{"number": i} for i in range(reads.SEARCH_LIMIT)])
        with (
            patch.object(github, "run_gh", return_value=payload),
            self.assertRaises(reads.SelectionError),
        ):
            reads.search_open_issues("org", [])

    def test_non_object_entries_raise(self) -> None:
        """Array entries that are not objects are an API failure."""
        with (
            patch.object(github, "run_gh", return_value="[1, 2]"),
            self.assertRaises(github.GitHubError),
        ):
            reads.search_open_issues("org", [])


class BranchHeadTest(ReadsCase):
    """``branch_head`` resolves refs with slashes, tags and raw SHAs."""

    def test_slashed_branch_uses_git_refs_endpoint(self) -> None:
        """A branch such as docs/topic goes through git/ref/heads, not commits."""
        with patch.object(github, "api_object", return_value=REF_COMMIT) as read:
            self.assertEqual(reads.branch_head("org/repo", "docs/topic"), SHA)
        self.assertEqual(
            read.call_args.args[0], "repos/org/repo/git/ref/heads/docs/topic"
        )

    def test_raw_sha_short_circuits(self) -> None:
        """A commit SHA needs no API call."""
        with patch.object(github, "api_object") as read:
            self.assertEqual(reads.branch_head("org/repo", SHA), SHA)
        read.assert_not_called()

    def test_annotated_tag_follows_to_commit(self) -> None:
        """A missing branch falls through to tags; a tag object is dereferenced."""
        tag_sha = "b" * 40
        tag_ref: dict[str, Any] = {"object": {"type": "tag", "sha": tag_sha}}
        tag_obj: dict[str, Any] = {"object": {"type": "commit", "sha": SHA}}
        with patch.object(
            github,
            "api_object",
            side_effect=[
                github.GitHubError("gh: Not Found (HTTP 404)"),
                tag_ref,
                tag_obj,
            ],
        ) as read:
            self.assertEqual(reads.branch_head("org/repo", "v1.0.0"), SHA)
        self.assertEqual(
            read.call_args_list[1].args[0], "repos/org/repo/git/ref/tags/v1.0.0"
        )
        self.assertEqual(
            read.call_args_list[2].args[0], f"repos/org/repo/git/tags/{tag_sha}"
        )

    def test_unknown_ref_raises(self) -> None:
        """Neither branch nor tag: a clear error naming the ref."""
        missing = github.GitHubError("gh: Not Found (HTTP 404)")
        with (
            patch.object(github, "api_object", side_effect=[missing, missing]),
            self.assertRaises(github.GitHubError) as raised,
        ):
            reads.branch_head("org/repo", "nope")
        self.assertIn("nope", str(raised.exception))

    def test_other_errors_propagate(self) -> None:
        """A 500 on the branch lookup is not treated as absence."""
        with (
            patch.object(
                github, "api_object", side_effect=github.GitHubError("boom (HTTP 500)")
            ),
            self.assertRaises(github.GitHubError),
        ):
            reads.branch_head("org/repo", "main")

    def test_malformed_object_raises(self) -> None:
        """A ref without an object, or with a bad SHA, is an error."""
        for bad in ({"object": None}, {"object": {"type": "commit", "sha": "xyz"}}):
            with (
                patch.object(github, "api_object", return_value=bad),
                self.assertRaises(github.GitHubError),
            ):
                reads.branch_head("org/repo", "main")


class FetchGuidanceTest(ReadsCase):
    """``fetch_guidance`` pins the ref to a commit and decodes the content."""

    def test_decodes_base64(self) -> None:
        """Content is decoded and the resolved commit returned."""
        text = b"# AGENTS\n\nBe careful.\n"
        content = {"encoding": "base64", "content": base64.b64encode(text).decode()}
        with patch.object(
            github, "api_object", side_effect=[REF_COMMIT, content]
        ) as read:
            result = reads.fetch_guidance("org/.github", "main", "AGENTS.md")
        self.assertEqual(result, (text, SHA))
        self.assertEqual(
            read.call_args_list[1].args[0],
            f"repos/org/.github/contents/AGENTS.md?ref={SHA}",
        )

    def test_empty_content_raises(self) -> None:
        """Whitespace-only guidance is refused."""
        content = {"encoding": "base64", "content": base64.b64encode(b"  \n").decode()}
        with (
            patch.object(github, "api_object", side_effect=[REF_COMMIT, content]),
            self.assertRaises(github.GitHubError),
        ):
            reads.fetch_guidance("org/.github", "main", "AGENTS.md")

    def test_non_base64_encoding_raises(self) -> None:
        """Any encoding other than base64 is refused."""
        content = {"encoding": "utf-8", "content": "plain"}
        with (
            patch.object(github, "api_object", side_effect=[REF_COMMIT, content]),
            self.assertRaises(github.GitHubError),
        ):
            reads.fetch_guidance("org/.github", "main", "AGENTS.md")

    def test_undecodable_content_raises(self) -> None:
        """Content that is not valid base64 is refused."""
        content = {"encoding": "base64", "content": "@@@"}
        with (
            patch.object(github, "api_object", side_effect=[REF_COMMIT, content]),
            self.assertRaises(github.GitHubError),
        ):
            reads.fetch_guidance("org/.github", "main", "AGENTS.md")

    def test_bad_branch_sha_raises(self) -> None:
        """A head SHA that is not 40 lowercase hex characters is refused."""
        with (
            patch.object(github, "api_object", return_value={"sha": "ABC"}),
            self.assertRaises(github.GitHubError),
        ):
            reads.fetch_guidance("org/.github", "main", "AGENTS.md")


class LabelNamesTest(unittest.TestCase):
    """``label_names`` accepts objects and strings and rejects empties."""

    def test_objects_and_strings(self) -> None:
        """Both shapes contribute their names in order."""
        self.assertEqual(reads.label_names([{"name": "a"}, "b"]), ["a", "b"])

    def test_empty_list(self) -> None:
        """No labels is fine."""
        self.assertEqual(reads.label_names([]), [])

    def test_rejects_bad(self) -> None:
        """Empty names, missing names and non-list input are refused."""
        cases: tuple[Any, ...] = ([{"name": ""}], [{}], [""], [3], None, "a")
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(github.GitHubError):
                reads.label_names(bad)


class ListRepositoriesTest(ReadsCase):
    """``list_repositories`` maps names and flags."""

    def test_mapping(self) -> None:
        """Names fold to lower case; flags and default branch are typed."""
        payload = json.dumps(
            [
                {
                    "name": "Alpha",
                    "isArchived": True,
                    "isTemplate": False,
                    "isFork": False,
                    "defaultBranchRef": {"name": "develop"},
                },
                {"name": "beta", "defaultBranchRef": None},
            ]
        )
        with patch.object(github, "run_gh", return_value=payload):
            repositories = reads.list_repositories("org")
        self.assertEqual(
            repositories,
            {
                "alpha": {
                    "name": "Alpha",
                    "archived": True,
                    "template": False,
                    "fork": False,
                    "default_branch": "develop",
                },
                "beta": {
                    "name": "beta",
                    "archived": False,
                    "template": False,
                    "fork": False,
                    "default_branch": None,
                },
            },
        )

    def test_ceiling_raises(self) -> None:
        """A listing at the search ceiling is treated as truncated."""
        payload = json.dumps([{"name": f"r{i}"} for i in range(reads.SEARCH_LIMIT)])
        with (
            patch.object(github, "run_gh", return_value=payload),
            self.assertRaises(reads.SelectionError),
        ):
            reads.list_repositories("org")


class GitHubErrorTest(unittest.TestCase):
    """``GitHubError`` parses the status gh reports."""

    def test_status_parsing(self) -> None:
        """The HTTP status is extracted when present."""
        self.assertEqual(github.GitHubError("gh: Not Found (HTTP 404)").status, 404)
        self.assertIsNone(github.GitHubError("timed out").status)
        self.assertTrue(github.is_absent(github.GitHubError("x (HTTP 410)")))
        self.assertFalse(github.is_absent(github.GitHubError("x (HTTP 403)")))


if __name__ == "__main__":
    unittest.main()
