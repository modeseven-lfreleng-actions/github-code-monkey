# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Publication: apply, comment text, commit replay, report and the CLI."""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
github = import_module("monkey_github")
checks = import_module("proposal_check")
policy = import_module("proposal_policy")
publish = import_module("publish")
comments = import_module("issue_comment")
reporting = import_module("proposal_report")

BASE = "a" * 40
BLOB_ONE = "1" * 40
BLOB_TWO = "2" * 40
REPOSITORY = "owner/repo"
BRANCH = "code-monkey/issue-7"


def check_json(**overrides: Any) -> dict[str, Any]:
    """A check.json document with a proposed verdict and two commits."""
    data: dict[str, Any] = {
        "schema": 1,
        "key": "repo-7",
        "repository": REPOSITORY,
        "issue": 7,
        "branch": BRANCH,
        "base_sha": BASE,
        "default_branch": "main",
        "verdict": "proposed",
        "reasons": [],
        "needs_workflows": False,
        "commits": [
            {
                "sha": "c" * 40,
                "headline": "Fix: One",
                "body": "Body one\n\nSigned-off-by: b <b@x>",
                "additions": [{"path": "a.txt", "blob": BLOB_ONE, "size": 3}],
                "deletions": ["gone.txt"],
            },
            {
                "sha": "d" * 40,
                "headline": "Fix: Two",
                "body": "Signed-off-by: b <b@x>",
                "additions": [{"path": "dir/b.txt", "blob": BLOB_TWO, "size": 3}],
                "deletions": [],
            },
        ],
        "pr_title": "Fix: Both",
        "pr_body": "Closes #7\n",
        "stats": {"files_changed": 3, "added_bytes": 6},
        "commands": [],
        "premium_requests": 4,
        "agent_seconds": 90,
    }
    data.update(overrides)
    return data


def result_json(**overrides: Any) -> dict[str, Any]:
    """A result.json document."""
    data: dict[str, Any] = {
        "schema": 1,
        "key": "repo-7",
        "repository": REPOSITORY,
        "issue": 7,
        "verdict": "proposed",
        "reasons": [],
        "warnings": [],
        "dry_run": False,
        "mode": "pull-requests",
        "branch_url": f"https://github.com/{REPOSITORY}/tree/{BRANCH}",
        "commits": ["e" * 40],
        "pull_request_url": f"https://github.com/{REPOSITORY}/pull/8",
        "comment_url": None,
        "pr_title": "Fix: Both",
        "premium_requests": 4,
        "agent_seconds": 90,
    }
    data.update(overrides)
    return data


class NoNetworkCase(unittest.TestCase):
    """Base class failing any test that reaches a real subprocess."""

    def setUp(self) -> None:
        """Forbid ``gh`` and ``git`` subprocesses alike."""
        for module in (github, checks):
            guard = patch.object(
                module.subprocess,
                "run",
                side_effect=AssertionError("unexpected subprocess"),
            )
            guard.start()
            self.addCleanup(guard.stop)
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)

    def write_check(self, data: dict[str, Any]) -> Path:
        """Write a check.json and return its path."""
        path = self.root / "check.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def apply_args(
        self, check: Path, *, mode: str = "pull-requests", dry_run: bool = False
    ) -> argparse.Namespace:
        """The namespace ``run_apply`` receives."""
        return argparse.Namespace(
            check=check,
            workdir=self.root / "work",
            mode=mode,
            dry_run=dry_run,
            output=self.root / "result.json",
        )


class CommentBodyTest(unittest.TestCase):
    """``comment_body`` renders one line per verdict."""

    def test_proposed_pull_request(self) -> None:
        """A pull request URL names the pull request."""
        body = comments.comment_body(result_json(), "https://run")
        self.assertEqual(
            body,
            "🐒 An AI agent has proposed a change for this issue: pull request "
            f"https://github.com/{REPOSITORY}/pull/8",
        )

    def test_proposed_branch_only(self) -> None:
        """Without a pull request the branch URL is named."""
        body = comments.comment_body(result_json(pull_request_url=None), "")
        self.assertIsNotNone(body)
        self.assertIn("branch https://github.com/owner/repo/tree/", body or "")

    def test_proposed_without_url(self) -> None:
        """A proposed result with no URL (dry run) says nothing."""
        result = result_json(pull_request_url=None, branch_url=None)
        self.assertIsNone(comments.comment_body(result, ""))

    def test_abstain(self) -> None:
        """Abstain quotes the joined reasons."""
        result = result_json(verdict="abstain", reasons=["too big", "unclear"])
        self.assertEqual(
            comments.comment_body(result, ""),
            "🐒 An AI agent looked at this issue and did not attempt it: too big; unclear",
        )

    def test_rejected(self) -> None:
        """Rejected names the check and the run."""
        result = result_json(verdict="rejected", reasons=["symlink"])
        self.assertEqual(
            comments.comment_body(result, "https://run"),
            "🐒 An AI agent attempted this issue but its proposal failed a policy "
            "check (symlink). Run: https://run",
        )

    def test_author_failed(self) -> None:
        """author-failed says it did not finish; missing run URL is n/a."""
        result = result_json(verdict="author-failed", reasons=["timeout"])
        self.assertEqual(
            comments.comment_body(result, ""),
            "🐒 An AI agent attempted this issue but did not finish (timeout). Run: n/a",
        )

    def test_unknown_verdict(self) -> None:
        """An unknown verdict yields no comment."""
        self.assertIsNone(comments.comment_body(result_json(verdict="weird"), ""))

    def test_long_reasons_are_bounded_and_keep_the_run_url(self) -> None:
        """An oversized manifest reason cannot push the comment past GitHub's limit."""
        text = comments.comment_body(
            {"verdict": "abstain", "reasons": ["x" * 70_000]}, "https://run"
        )
        self.assertIsNotNone(text)
        self.assertLess(len(text or ""), policy.MAX_PR_BODY)
        self.assertTrue((text or "").endswith("…"))
        rejected = comments.comment_body(
            {"verdict": "rejected", "reasons": ["y" * 70_000]}, "https://run"
        )
        self.assertIn("https://run", rejected or "")

    def test_reasons_render_on_one_line(self) -> None:
        """A multiline reason cannot add blocks or mentions to the comment."""
        text = comments.comment_body(
            {"verdict": "abstain", "reasons": ["no\n\n# Owned\n@alice"]},
            "https://run",
        )
        self.assertNotIn("\n", text or "")
        self.assertNotIn("@alice", text or "")

    def test_publish_failed_names_the_run(self) -> None:
        """A publish failure tells the reader the issue stays eligible."""
        text = comments.comment_body(
            {"verdict": "publish-failed", "reasons": ["boom; branch removed"]},
            "https://run",
        )
        self.assertIsNotNone(text)
        self.assertIn("stays eligible", text or "")
        self.assertIn("https://run", text or "")


class RunApplyTest(NoNetworkCase):
    """``run_apply`` gates GitHub writes on verdict, dry-run and mode."""

    def test_rejected_verdict_touches_nothing(self) -> None:
        """A rejected check returns a result without any GitHub call."""
        path = self.write_check(check_json(verdict="rejected", reasons=["nope"]))
        with (
            patch.object(publish, "create_branch") as branch,
            patch.object(publish, "replay_commits") as replay,
            patch.object(publish, "open_pull_request") as pull,
        ):
            result = publish.run_apply(self.apply_args(path))
        branch.assert_not_called()
        replay.assert_not_called()
        pull.assert_not_called()
        self.assertEqual(result["verdict"], "rejected")
        self.assertEqual(result["reasons"], ["nope"])
        self.assertIsNone(result["branch_url"])
        self.assertIsNone(result["pull_request_url"])
        self.assertEqual(result["commits"], [])
        self.assertEqual(result["premium_requests"], 4)
        self.assertEqual(result["agent_seconds"], 90)
        self.assertEqual(result["pr_title"], "Fix: Both")

    def test_dry_run(self) -> None:
        """Dry run with a proposed verdict records the flag and stops."""
        path = self.write_check(check_json())
        with patch.object(publish, "create_branch") as branch:
            result = publish.run_apply(self.apply_args(path, dry_run=True))
        branch.assert_not_called()
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["verdict"], "proposed")
        self.assertIsNone(result["branch_url"])
        self.assertIsNone(result["pull_request_url"])

    def test_select_mode(self) -> None:
        """Select mode never writes."""
        path = self.write_check(check_json())
        with patch.object(publish, "create_branch") as branch:
            result = publish.run_apply(self.apply_args(path, mode="select"))
        branch.assert_not_called()
        self.assertFalse(result["dry_run"])
        self.assertIsNone(result["branch_url"])

    def test_branches_mode(self) -> None:
        """Branches mode creates and replays but opens no pull request."""
        path = self.write_check(check_json())
        with (
            patch.object(publish, "create_branch") as branch,
            patch.object(publish, "replay_commits", return_value=["f" * 40]) as replay,
            patch.object(publish, "open_pull_request") as pull,
        ):
            result = publish.run_apply(self.apply_args(path, mode="branches"))
        branch.assert_called_once_with(REPOSITORY, BRANCH, BASE)
        replay.assert_called_once()
        self.assertEqual(replay.call_args.args[0], self.root / "work" / "clone")
        self.assertEqual(replay.call_args.args[1]["key"], "repo-7")
        pull.assert_not_called()
        self.assertEqual(result["commits"], ["f" * 40])
        self.assertEqual(
            result["branch_url"], f"https://github.com/{REPOSITORY}/tree/{BRANCH}"
        )
        self.assertIsNone(result["pull_request_url"])
        self.assertEqual(result["warnings"], [])

    def test_pull_requests_mode_with_warning(self) -> None:
        """Pull-requests mode records the URL and any labelling warning."""
        path = self.write_check(check_json())
        with (
            patch.object(publish, "create_branch"),
            patch.object(publish, "replay_commits", return_value=["f" * 40]),
            patch.object(
                publish,
                "open_pull_request",
                return_value=(
                    "https://x/pull/1",
                    "could not label the pull request: x",
                ),
            ) as pull,
        ):
            result = publish.run_apply(self.apply_args(path))
        pull.assert_called_once()
        self.assertEqual(result["pull_request_url"], "https://x/pull/1")
        self.assertEqual(result["warnings"], ["could not label the pull request: x"])

    def test_replay_failure_rolls_back_the_branch(self) -> None:
        """A failure after branch creation deletes the branch and fails the step.

        Without the rollback the half-built branch would count as a prior
        attempt and keep the issue out of every later run.
        """
        path = self.write_check(check_json())
        with (
            patch.object(publish, "create_branch"),
            patch.object(
                publish,
                "replay_commits",
                side_effect=policy.PublishError("createCommitOnBranch failed"),
            ),
            patch.object(publish, "open_pull_request") as pull,
            patch.object(publish, "delete_branch", return_value=None) as delete,
            self.assertRaisesRegex(policy.PublishError, "branch removed"),
        ):
            publish.run_apply(self.apply_args(path))
        pull.assert_not_called()
        delete.assert_called_once_with("owner/repo", "code-monkey/issue-7")
        written = json.loads((self.root / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(written["verdict"], "publish-failed")
        self.assertIn("createCommitOnBranch failed", written["reasons"][0])
        self.assertIsNone(written["pull_request_url"])
        self.assertIsNone(written["branch_url"])

    def test_pr_failure_reports_leftover_branch(self) -> None:
        """When rollback itself fails, the leftover is named in the reason."""
        path = self.write_check(check_json())
        with (
            patch.object(publish, "create_branch"),
            patch.object(publish, "replay_commits", return_value=["f" * 40]),
            patch.object(
                publish,
                "open_pull_request",
                side_effect=github.GitHubError("boom (HTTP 502)"),
            ),
            patch.object(publish, "delete_branch", return_value="branch x stays"),
            self.assertRaisesRegex(policy.PublishError, "branch x stays"),
        ):
            publish.run_apply(self.apply_args(path))

    def test_rerun_after_success_reports_the_existing_pull_request(self) -> None:
        """A retry meeting its own open PR is published, not rejected, and silent."""
        path = self.write_check(check_json())
        with (
            patch.object(
                publish, "create_branch", side_effect=policy.Rejection("branch exists")
            ),
            patch.object(
                publish, "existing_pull_request", return_value="https://x/pull/5"
            ),
            patch.object(publish, "replay_commits") as replay,
            patch.object(publish, "delete_branch") as delete,
        ):
            result = publish.run_apply(self.apply_args(path))
        replay.assert_not_called()
        delete.assert_not_called()
        self.assertEqual(result["verdict"], "proposed")
        self.assertEqual(result["pull_request_url"], "https://x/pull/5")
        self.assertTrue(result["already_published"])
        self.assertIsNone(comments.comment_body(result, "https://run"))

    def test_branch_create_failure_is_rolled_back(self) -> None:
        """A create whose reply is lost is treated like any later failure."""
        path = self.write_check(check_json())
        with (
            patch.object(
                publish,
                "create_branch",
                side_effect=policy.PublishError("gh: Bad Gateway (HTTP 502)"),
            ),
            patch.object(publish, "replay_commits") as replay,
            patch.object(publish, "delete_branch", return_value=None) as delete,
            self.assertRaisesRegex(policy.PublishError, "branch removed"),
        ):
            publish.run_apply(self.apply_args(path))
        replay.assert_not_called()
        delete.assert_called_once_with("owner/repo", "code-monkey/issue-7")
        written = json.loads((self.root / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(written["verdict"], "publish-failed")

    def test_existing_branch_becomes_rejection(self) -> None:
        """An existing branch is a rejection and is never deleted by rollback."""
        path = self.write_check(check_json())
        with (
            patch.object(
                publish, "create_branch", side_effect=policy.Rejection("branch exists")
            ),
            patch.object(publish, "replay_commits") as replay,
            patch.object(publish, "delete_branch") as delete,
            patch.object(publish, "existing_pull_request", return_value=None),
        ):
            result = publish.run_apply(self.apply_args(path))
        replay.assert_not_called()
        delete.assert_not_called()
        self.assertEqual(result["verdict"], "rejected")
        self.assertEqual(result["reasons"], ["branch exists"])
        self.assertIsNone(result["branch_url"])

    def test_unknown_verdict_is_error(self) -> None:
        """A check.json with an unknown verdict is an operational failure."""
        path = self.write_check(check_json(verdict="maybe"))
        with self.assertRaises(policy.PublishError):
            publish.run_apply(self.apply_args(path))


class CreateBranchTest(NoNetworkCase):
    """``create_branch`` distinguishes an existing branch from other failures."""

    def test_success(self) -> None:
        """The ref is created at the base SHA."""
        with patch.object(github, "api_write", return_value={}) as write:
            publish.create_branch(REPOSITORY, BRANCH, BASE)
        write.assert_called_once_with(
            "POST",
            f"repos/{REPOSITORY}/git/refs",
            {"ref": f"refs/heads/{BRANCH}", "sha": BASE},
        )

    def test_already_exists_is_rejection(self) -> None:
        """A 422 naming an existing reference is a Rejection."""
        error = github.GitHubError("gh: Reference already exists (HTTP 422)")
        with (
            patch.object(github, "api_write", side_effect=error),
            self.assertRaisesRegex(policy.Rejection, "already exists"),
        ):
            publish.create_branch(REPOSITORY, BRANCH, BASE)

    def test_other_errors_are_operational(self) -> None:
        """Any other failure, including an unrelated 422, is a PublishError."""
        for message in ("gh: Forbidden (HTTP 403)", "gh: Validation Failed (HTTP 422)"):
            with (
                self.subTest(message=message),
                patch.object(
                    github, "api_write", side_effect=github.GitHubError(message)
                ),
                self.assertRaises(policy.PublishError),
            ):
                publish.create_branch(REPOSITORY, BRANCH, BASE)


class DeleteBranchTest(NoNetworkCase):
    """``delete_branch`` is best effort and reports rather than raises."""

    def test_deletes_via_git_refs(self) -> None:
        """The ref endpoint receives a DELETE for the branch."""
        with patch.object(github, "run_gh", return_value="") as run:
            self.assertIsNone(publish.delete_branch("o/r", "code-monkey/issue-1"))
        self.assertEqual(
            run.call_args.args[0],
            [
                "api",
                "--method",
                "DELETE",
                "repos/o/r/git/refs/heads/code-monkey/issue-1",
            ],
        )

    def test_absent_branch_is_a_clean_rollback(self) -> None:
        """Nothing to delete after an ambiguous create is not a leftover."""
        for message in (
            "gh: Not Found (HTTP 404)",
            "gh: Reference does not exist (HTTP 422)",
        ):
            with (
                self.subTest(message=message),
                patch.object(github, "run_gh", side_effect=github.GitHubError(message)),
            ):
                self.assertIsNone(publish.delete_branch("o/r", "b"))

    def test_failure_becomes_a_note(self) -> None:
        """A failed delete returns text for the result instead of raising."""
        with patch.object(
            github, "run_gh", side_effect=github.GitHubError("nope (HTTP 403)")
        ):
            note = publish.delete_branch("o/r", "b")
        self.assertIsNotNone(note)
        self.assertIn("could not be removed", note or "")


class ExistingPullRequestTest(NoNetworkCase):
    """``existing_pull_request`` finds this repository's own open PR only."""

    def test_same_repository_pr_found(self) -> None:
        """A same-repository PR from the branch is returned; forks are ignored."""
        reply = json.dumps(
            [
                {"url": "https://fork/pull/1", "isCrossRepository": True},
                {"url": "https://x/pull/2", "isCrossRepository": False},
            ]
        )
        with patch.object(github, "run_gh", return_value=reply) as run:
            url = publish.existing_pull_request("o/r", "code-monkey/issue-2")
        self.assertEqual(url, "https://x/pull/2")
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--state") + 1], "open")

    def test_none_when_only_forks(self) -> None:
        """Only fork PRs means no earlier attempt of ours."""
        reply = json.dumps([{"url": "https://fork/pull/1", "isCrossRepository": True}])
        with patch.object(github, "run_gh", return_value=reply):
            self.assertIsNone(publish.existing_pull_request("o/r", "b"))


class ReplayCommitsTest(NoNetworkCase):
    """``replay_commits`` chains createCommitOnBranch calls."""

    def fake_git(self, _workdir: Path, *args: str, binary: bool = False) -> bytes | str:
        """Serve blob contents for ``cat-file blob`` and nothing else."""
        self.assertEqual(args[:2], ("cat-file", "blob"))
        self.assertTrue(binary)
        return {BLOB_ONE: b"one", BLOB_TWO: b"two"}[args[2]]

    def test_payload_chaining(self) -> None:
        """Each commit carries the previous oid as expectedHeadOid."""
        oids = ["b" * 40, "e" * 40]
        replies = [
            {"createCommitOnBranch": {"commit": {"oid": oid, "url": "u"}}}
            for oid in oids
        ]
        with (
            patch.object(checks, "git", side_effect=self.fake_git),
            patch.object(github, "graphql", side_effect=replies) as gql,
        ):
            created = publish.replay_commits(self.root / "clone", check_json())
        self.assertEqual(created, oids)
        self.assertEqual(gql.call_count, 2)
        first = gql.call_args_list[0].args[1]["input"]
        second = gql.call_args_list[1].args[1]["input"]
        self.assertEqual(gql.call_args_list[0].args[0], publish.COMMIT_MUTATION)
        self.assertEqual(
            first["branch"],
            {"repositoryNameWithOwner": REPOSITORY, "branchName": BRANCH},
        )
        self.assertEqual(first["expectedHeadOid"], BASE)
        self.assertEqual(second["expectedHeadOid"], oids[0])
        self.assertEqual(
            first["message"],
            {"headline": "Fix: One", "body": "Body one\n\nSigned-off-by: b <b@x>"},
        )
        self.assertEqual(
            first["fileChanges"],
            {
                "additions": [
                    {"path": "a.txt", "contents": base64.b64encode(b"one").decode()}
                ],
                "deletions": [{"path": "gone.txt"}],
            },
        )
        self.assertEqual(
            second["fileChanges"],
            {
                "additions": [
                    {"path": "dir/b.txt", "contents": base64.b64encode(b"two").decode()}
                ],
                "deletions": [],
            },
        )

    def test_reply_without_commit_raises(self) -> None:
        """A reply lacking the commit node is an operational failure."""
        with (
            patch.object(checks, "git", side_effect=self.fake_git),
            patch.object(
                github,
                "graphql",
                return_value={"createCommitOnBranch": {"commit": None}},
            ),
            self.assertRaisesRegex(policy.PublishError, "returned no commit"),
        ):
            publish.replay_commits(self.root / "clone", check_json())

    def test_graphql_failure_wrapped(self) -> None:
        """A GitHubError from the mutation becomes a PublishError."""
        with (
            patch.object(checks, "git", side_effect=self.fake_git),
            patch.object(
                github, "graphql", side_effect=github.GitHubError("GraphQL errors: x")
            ),
            self.assertRaisesRegex(policy.PublishError, "createCommitOnBranch failed"),
        ):
            publish.replay_commits(self.root / "clone", check_json())

    def test_malformed_blob_refused(self) -> None:
        """A blob id that is not a SHA never reaches git."""
        check = check_json()
        check["commits"][0]["additions"][0]["blob"] = "--upload-pack=evil"
        with (
            patch.object(checks, "git") as git,
            patch.object(github, "graphql") as gql,
            self.assertRaisesRegex(policy.PublishError, "malformed blob"),
        ):
            publish.replay_commits(self.root / "clone", check)
        git.assert_not_called()
        gql.assert_not_called()


class OpenPullRequestTest(NoNetworkCase):
    """``open_pull_request`` opens, then labels when the label exists."""

    def test_labelled(self) -> None:
        """An existing label is applied and no warning returned."""
        with (
            patch.object(
                github,
                "api_write",
                side_effect=[{"html_url": "https://x/pull/1", "number": 1}, {}],
            ) as write,
            patch.object(github, "api_object", return_value={"name": "code-monkey"}),
        ):
            url, warning = publish.open_pull_request(check_json())
        self.assertEqual((url, warning), ("https://x/pull/1", None))
        opened = write.call_args_list[0].args
        self.assertEqual(opened[1], f"repos/{REPOSITORY}/pulls")
        self.assertEqual(opened[2]["head"], BRANCH)
        self.assertEqual(opened[2]["base"], "main")
        self.assertFalse(opened[2]["draft"])
        labelled = write.call_args_list[1].args
        self.assertEqual(labelled[1], f"repos/{REPOSITORY}/issues/1/labels")
        self.assertEqual(labelled[2], {"labels": ["code-monkey"]})

    def test_missing_label_is_silent(self) -> None:
        """A 404 on the label read is not a warning."""
        with (
            patch.object(
                github,
                "api_write",
                return_value={"html_url": "https://x/pull/1", "number": 1},
            ) as write,
            patch.object(
                github, "api_object", side_effect=github.GitHubError("x (HTTP 404)")
            ),
        ):
            url, warning = publish.open_pull_request(check_json())
        self.assertEqual((url, warning), ("https://x/pull/1", None))
        self.assertEqual(write.call_count, 1)

    def test_label_failure_is_warning(self) -> None:
        """Any other labelling failure is returned as a warning with the URL."""
        with (
            patch.object(
                github,
                "api_write",
                return_value={"html_url": "https://x/pull/1", "number": 1},
            ),
            patch.object(
                github, "api_object", side_effect=github.GitHubError("x (HTTP 403)")
            ),
        ):
            url, warning = publish.open_pull_request(check_json())
        self.assertEqual(url, "https://x/pull/1")
        self.assertIn("could not label the pull request", warning or "")


class RunCommentTest(NoNetworkCase):
    """``run_comment`` posts once and records the comment URL."""

    def write_result(self, data: dict[str, Any]) -> Path:
        """Write a result.json and return its path."""
        path = self.root / "result.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_posts_and_records(self) -> None:
        """The comment lands on the issue and its URL is written back."""
        path = self.write_result(result_json())
        with (
            patch.object(
                github, "api_write", return_value={"html_url": "https://x/c/1"}
            ) as write,
            redirect_stdout(io.StringIO()),
        ):
            comments.run_comment(argparse.Namespace(result=path, run_url="https://run"))
        self.assertEqual(
            write.call_args.args[1], f"repos/{REPOSITORY}/issues/7/comments"
        )
        self.assertIn("pull request", write.call_args.args[2]["body"])
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["comment_url"], "https://x/c/1")

    def test_dry_run_and_silent_verdicts_skip(self) -> None:
        """Dry run and results with nothing to say post nothing."""
        for data in (result_json(dry_run=True), result_json(verdict="weird")):
            path = self.write_result(data)
            with (
                self.subTest(verdict=data["verdict"], dry_run=data["dry_run"]),
                patch.object(github, "api_write") as write,
                redirect_stdout(io.StringIO()),
            ):
                comments.run_comment(argparse.Namespace(result=path, run_url=""))
            write.assert_not_called()


class ReportRowTest(unittest.TestCase):
    """``report_row`` renders one escaped table cell whatever the source."""

    def test_title_fallback_is_escaped_and_flattened(self) -> None:
        """A title with a pipe and a newline stays inside its cell."""
        row = reporting.report_row(
            {
                "repository": "o/r",
                "issue": 1,
                "verdict": "proposed",
                "reasons": [],
                "pr_title": "Feat: A | B\n::error::x",
                "pull_request_url": "https://x/pull/1",
            }
        )
        self.assertEqual(row.count("\n"), 0)
        self.assertIn("A \\| B", row)
        self.assertNotIn("::error::", row)
        self.assertEqual(row.count("|") - row.count("\\|"), 6)


class RunReportTest(NoNetworkCase):
    """``run_report`` merges nested result files into one table."""

    def test_report(self) -> None:
        """One row per result, an unreadable row and correct totals."""
        results = self.root / "results"
        first = results / "a" / "deep"
        second = results / "b"
        broken = results / "c"
        for directory in (first, second, broken):
            directory.mkdir(parents=True)
        (first / "result.json").write_text(json.dumps(result_json()), encoding="utf-8")
        (second / "result.json").write_text(
            json.dumps(
                result_json(
                    key="other-2",
                    issue=2,
                    verdict="rejected",
                    reasons=["a | b"],
                    warnings=["w"],
                    pull_request_url=None,
                    branch_url=None,
                    premium_requests=3,
                )
            ),
            encoding="utf-8",
        )
        (broken / "result.json").write_text("{nope", encoding="utf-8")
        (results / "ignored.json").write_text("[]", encoding="utf-8")
        output_md = self.root / "out" / "report.md"
        output_json = self.root / "out" / "report.json"
        reporting.run_report(
            argparse.Namespace(
                results=results, output_md=output_md, output_json=output_json
            )
        )
        markdown = output_md.read_text(encoding="utf-8")
        self.assertIn(
            f"| {REPOSITORY}#7 | proposed | https://github.com/{REPOSITORY}/pull/8 "
            "| 4 | Fix: Both |",
            markdown,
        )
        self.assertIn(f"| {REPOSITORY}#2 | rejected | — | 3 | a \\| b; w |", markdown)
        self.assertEqual(markdown.count("| unreadable |"), 1)
        self.assertIn(
            "Proposed 1, abstained 0, rejected 1, failed 0, publish failures 0; "
            "premium requests 7.",
            markdown,
        )
        report = json.loads(output_json.read_text(encoding="utf-8"))
        self.assertEqual(
            report["totals"],
            {
                "proposed": 1,
                "abstain": 0,
                "rejected": 1,
                "author-failed": 0,
                "publish-failed": 0,
            },
        )
        self.assertEqual(report["premium_requests"], 7)
        self.assertEqual(len(report["unreadable"]), 1)
        self.assertEqual(len(report["results"]), 2)

    def test_empty_report(self) -> None:
        """No results renders the placeholder row."""
        results = self.root / "results"
        results.mkdir()
        output_md = self.root / "report.md"
        output_json = self.root / "report.json"
        reporting.run_report(
            argparse.Namespace(
                results=results, output_md=output_md, output_json=output_json
            )
        )
        self.assertIn("| no proposals |", output_md.read_text(encoding="utf-8"))

    def test_dry_run_row(self) -> None:
        """A dry-run proposed result shows ``dry run`` instead of a URL."""
        row = reporting.report_row(result_json(dry_run=True))
        self.assertIn("| proposed | dry run |", row)


class CliTest(NoNetworkCase):
    """``build_parser`` and ``main`` wire the commands and the error path."""

    def test_check_requires_arguments(self) -> None:
        """``check`` without its required options is a usage error."""
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            publish.main(["check", "--key", "x"])
        self.assertEqual(caught.exception.code, 2)

    def test_no_command_is_usage_error(self) -> None:
        """A missing subcommand is a usage error."""
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            publish.main([])
        self.assertEqual(caught.exception.code, 2)

    def test_publish_error_exits_one(self) -> None:
        """An operational failure exits 1 with the ``publish:`` prefix."""
        path = self.write_check(check_json(verdict="maybe"))
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            publish.main(
                [
                    "apply",
                    "--check",
                    str(path),
                    "--workdir",
                    str(self.root),
                    "--mode",
                    "branches",
                    "--output",
                    str(self.root / "result.json"),
                ]
            )
        self.assertEqual(caught.exception.code, 1)
        self.assertTrue(stderr.getvalue().startswith("publish: "))
        self.assertIn("unknown verdict", stderr.getvalue())
        self.assertFalse((self.root / "result.json").exists())

    def test_apply_writes_result(self) -> None:
        """A dry-run apply writes result.json through the CLI."""
        path = self.write_check(check_json())
        output = self.root / "nested" / "result.json"
        with redirect_stdout(io.StringIO()) as stdout:
            publish.main(
                [
                    "apply",
                    "--check",
                    str(path),
                    "--workdir",
                    str(self.root),
                    "--mode",
                    "pull-requests",
                    "--dry-run",
                    "--output",
                    str(output),
                ]
            )
        self.assertEqual(stdout.getvalue(), "apply: proposed; pr=None\n")
        result = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["mode"], "pull-requests")

    def test_check_command_writes_outputs(self) -> None:
        """``check`` writes check.json and the summary from ``run_check``."""
        check = checks.Check(
            key="repo-7",
            repository=REPOSITORY,
            issue=7,
            branch=BRANCH,
            base_sha=BASE,
            default_branch="main",
            verdict="abstain",
            reasons=["nothing to do"],
        )
        output = self.root / "check.json"
        summary = self.root / "summary.md"
        with (
            patch.object(checks, "run_check", return_value=check) as run,
            redirect_stdout(io.StringIO()) as stdout,
        ):
            publish.main(
                [
                    "check",
                    "--selection",
                    "s.json",
                    "--key",
                    "repo-7",
                    "--proposal-dir",
                    "p",
                    "--workdir",
                    "w",
                    "--coauthors",
                    "c.json",
                    "--output",
                    str(output),
                    "--summary",
                    str(summary),
                    "--run-url",
                    "https://run",
                ]
            )
        self.assertEqual(run.call_args.kwargs["key"], "repo-7")
        self.assertEqual(run.call_args.kwargs["run_url"], "https://run")
        self.assertEqual(stdout.getvalue(), "verdict: abstain; nothing to do\n")
        self.assertEqual(
            json.loads(output.read_text(encoding="utf-8"))["verdict"], "abstain"
        )
        self.assertIn("`abstain`", summary.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
