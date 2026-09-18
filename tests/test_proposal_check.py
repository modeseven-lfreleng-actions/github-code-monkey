# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Offline proposal verification against real git repositories and bundles.

Each test builds an upstream repository, an agent clone with a bot
branch, a bundle of the agent's commits and the trusted selection, then
runs ``proposal_check.run_check`` with ``prepare_clone`` patched to
fetch from the local upstream instead of GitHub.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
checks = import_module("proposal_check")
model = import_module("proposal_model")
github = import_module("monkey_github")
policy = import_module("proposal_policy")

BOT_LOGIN = "code-monkey[bot]"
BOT_EMAIL = "1+code-monkey[bot]@users.noreply.github.com"
SIGN_OFF = f"Signed-off-by: {BOT_LOGIN} <{BOT_EMAIL}>"
COAUTHOR = "Co-authored-by: Claude <noreply@anthropic.com>"
REPOSITORY = "owner/repo"
ISSUE = 7
BRANCH = f"code-monkey/issue-{ISSUE}"
KEY = f"repo-{ISSUE}"
ISSUE_URL = f"https://github.com/{REPOSITORY}/issues/{ISSUE}"
RUN_URL = "https://example/run"
COAUTHORS: dict[str, str] = {
    "claude-": "Claude <noreply@anthropic.com>",
    "gpt-": "ChatGPT <chatgpt@openai.com>",
}
GITLINT = "[title-max-length]\nline-length=60\n"
README = "# Repo\n\nA readme with a typo.\n"


def run_git(cwd: Path, *args: str) -> str:
    """Run git for fixture construction, failing loudly."""
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout


def local_prepare_clone(upstream: Path) -> Any:
    """A ``prepare_clone`` double that fetches from a local path, not GitHub."""

    def prepare(workdir: Path, repository: str, base_sha: str) -> Path:
        """Mirror the real steps with the remote pointed at ``upstream``."""
        if repository != REPOSITORY:
            raise AssertionError(f"unexpected repository {repository!r}")
        clone = workdir / "clone"
        clone.mkdir(parents=True, exist_ok=True)
        checks.git(clone, "init", "-q")
        checks.git(clone, "remote", "add", "origin", str(upstream))
        checks.git(
            clone,
            "-c",
            "protocol.version=2",
            "fetch",
            "-q",
            "--depth=1",
            "origin",
            base_sha,
        )
        resolved = str(
            checks.git(clone, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
        ).strip()
        if resolved != base_sha:
            raise policy.PublishError("fetched commit does not match the recorded base")
        return clone

    return prepare


class Fixture:
    """An upstream repository, an agent clone and the proposal directory."""

    def __init__(self, root: Path) -> None:
        """Create the upstream with one commit and clone it for the agent."""
        self.root = root
        self.upstream = root / "upstream"
        self.agent = root / "agent"
        self.proposal = root / "proposal"
        self.workdir = root / "work"
        self.proposal.mkdir()
        self.upstream.mkdir()
        run_git(self.upstream, "init", "-q", "-b", "main")
        (self.upstream / "README.md").write_text(README, encoding="utf-8")
        (self.upstream / ".gitlint").write_text(GITLINT, encoding="utf-8")
        (self.upstream / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
        (self.upstream / "old.txt").write_text("old\n", encoding="utf-8")
        run_git(self.upstream, "add", "-A")
        run_git(self.upstream, "commit", "-q", "-m", "Chore: Initial commit")
        self.base_sha = run_git(self.upstream, "rev-parse", "HEAD").strip()
        run_git(root, "clone", "-q", str(self.upstream), str(self.agent))
        self.branch = BRANCH
        run_git(self.agent, "checkout", "-q", "-b", self.branch)

    def commit(
        self,
        message: str,
        files: dict[str, str | None] | None = None,
        *,
        executable: tuple[str, ...] = (),
        symlinks: dict[str, str] | None = None,
        sign_off: bool = True,
    ) -> str:
        """Write, stage and commit files in the agent clone; return the SHA."""
        for path, content in (files or {}).items():
            target = self.agent / path
            if content is None:
                run_git(self.agent, "rm", "-q", path)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            if path in executable:
                target.chmod(0o755)
            run_git(self.agent, "add", path)
        for path, destination in (symlinks or {}).items():
            os.symlink(destination, self.agent / path)
            run_git(self.agent, "add", path)
        args = ["commit", "-q", "--allow-empty", "-m", message]
        if sign_off:
            args.insert(1, "-s")
        run_git(self.agent, *args)
        return run_git(self.agent, "rev-parse", "HEAD").strip()

    def bundle(self) -> Path:
        """Create changes.bundle from the base to the agent branch."""
        target = self.proposal / "changes.bundle"
        run_git(
            self.agent,
            "bundle",
            "create",
            "-q",
            str(target),
            f"{self.base_sha}..refs/heads/{self.branch}",
        )
        return target

    def manifest(self, **overrides: Any) -> None:
        """Write a proposed manifest, applying overrides."""
        data: dict[str, Any] = {
            "schema": 1,
            "outcome": "proposed",
            "repository": REPOSITORY,
            "issue": ISSUE,
            "base_sha": self.base_sha,
            "branch": BRANCH,
            "reason": None,
            "pr_title": "Fix(readme): Correct typo",
            "pr_body": f"Fix the typo in the readme.\n\nCloses #{ISSUE}\n",
            "commands": [{"command": "uv run pytest", "exit_code": 0}],
        }
        data.update(overrides)
        (self.proposal / "manifest.json").write_text(json.dumps(data), encoding="utf-8")

    def selection(self, **overrides: Any) -> dict[str, Any]:
        """The trusted selection document."""
        entry: dict[str, Any] = {
            "key": KEY,
            "repository": REPOSITORY,
            "repo_name": "repo",
            "number": ISSUE,
            "url": ISSUE_URL,
            "title": "Typo in readme",
            "body": "There is a typo.",
            "labels": ["bug"],
            "priority": "Low",
            "type": "Bug",
            "created_at": "2026-09-01T00:00:00Z",
            "author_association": "MEMBER",
            "default_branch": "main",
            "base_sha": self.base_sha,
            "branch": BRANCH,
            "comments": [],
            "comments_dropped": 0,
        }
        entry.update(overrides)
        return {
            "schema": 1,
            "org": "owner",
            "generated_at": "2026-09-18T00:00:00Z",
            "mode": "pull-requests",
            "dry_run": False,
            "model": "claude-opus-5",
            "bot": {"login": BOT_LOGIN, "email": BOT_EMAIL, "placeholder": False},
            "guidance": {},
            "candidates_seen": 1,
            "skipped": {},
            "issues": [entry],
        }

    def write_selection(self, selection: dict[str, Any] | None = None) -> Path:
        """Write selection.json and return its path."""
        path = self.root / "selection.json"
        path.write_text(json.dumps(selection or self.selection()), encoding="utf-8")
        return path

    def write_coauthors(self) -> Path:
        """Write coauthors.json and return its path."""
        path = self.root / "coauthors.json"
        path.write_text(json.dumps(COAUTHORS), encoding="utf-8")
        return path

    def run(self, *, key: str = KEY) -> Any:
        """Run the offline check with the clone pointed at the local upstream."""
        with patch.object(checks, "prepare_clone", local_prepare_clone(self.upstream)):
            return checks.run_check(
                selection_path=self.write_selection(),
                key=key,
                proposal_dir=self.proposal,
                workdir=self.workdir,
                coauthors_path=self.write_coauthors(),
                run_url=RUN_URL,
            )


class GitCase(unittest.TestCase):
    """Base class isolating git from the developer's own configuration."""

    def setUp(self) -> None:
        """Point git at a private global config and a fixed identity."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        config = self.root / "gitconfig"
        config.write_text(
            "[init]\n\tdefaultBranch = main\n"
            "[commit]\n\tgpgsign = false\n"
            "[tag]\n\tgpgsign = false\n"
            f"[core]\n\thooksPath = {self.root / 'nohooks'}\n"
            "[uploadpack]\n\tallowAnySHA1InWant = true\n",
            encoding="utf-8",
        )
        env = {
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": BOT_LOGIN,
            "GIT_AUTHOR_EMAIL": BOT_EMAIL,
            "GIT_COMMITTER_NAME": BOT_LOGIN,
            "GIT_COMMITTER_EMAIL": BOT_EMAIL,
            "GIT_AUTHOR_DATE": "2026-09-18T10:00:00Z",
            "GIT_COMMITTER_DATE": "2026-09-18T10:00:00Z",
        }
        environment = patch.dict(os.environ, env)
        environment.start()
        self.addCleanup(environment.stop)
        self.fixture = Fixture(self.root)


class RunCheckProposedTest(GitCase):
    """Proposals that pass every rule."""

    def test_happy_path(self) -> None:
        """A single clean commit yields ``proposed`` with composed messages."""
        fixed = README.replace("with a typo", "without a typo")
        sha = self.fixture.commit(
            "Fix(readme): Correct typo\n\nThe word was misspelt.\n",
            {"README.md": fixed},
        )
        self.fixture.bundle()
        self.fixture.manifest()
        check = self.fixture.run()
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertEqual(check.reasons, [])
        self.assertFalse(check.needs_workflows)
        self.assertEqual(len(check.commits), 1)
        commit = check.commits[0]
        self.assertEqual(commit["sha"], sha)
        self.assertEqual(commit["headline"], "Fix(readme): Correct typo")
        self.assertTrue(commit["body"].endswith(f"\n\n{COAUTHOR}\n{SIGN_OFF}"))
        self.assertEqual(commit["body"].count(SIGN_OFF), 1)
        self.assertIn("The word was misspelt.", commit["body"])
        self.assertEqual(commit["deletions"], [])
        (addition,) = commit["additions"]
        self.assertEqual(addition["path"], "README.md")
        self.assertTrue(policy.SHA_RE.fullmatch(addition["blob"]))
        self.assertEqual(addition["size"], len(fixed.encode("utf-8")))
        self.assertEqual(check.pr_title, "Fix(readme): Correct typo")
        provenance = policy.provenance_block(
            model="claude-opus-5",
            run_url=RUN_URL,
            issue_url=ISSUE_URL,
            base_sha=self.fixture.base_sha,
            commands=[{"command": "uv run pytest", "exit_code": 0}],
        )
        self.assertTrue(check.pr_body.endswith(provenance))
        self.assertTrue(
            check.pr_body.startswith(f"Fix the typo in the readme.\n\nCloses #{ISSUE}")
        )
        self.assertEqual(check.files_changed, 1)
        self.assertEqual(check.added_bytes, len(fixed.encode("utf-8")))
        self.assertEqual(check.commands, [{"command": "uv run pytest", "exit_code": 0}])
        self.assertIsNone(check.premium_requests)
        serialised = check.to_json()
        self.assertEqual(serialised["schema"], model.SCHEMA)
        self.assertEqual(
            serialised["stats"], {"files_changed": 1, "added_bytes": len(fixed)}
        )

    def test_workflow_change_flags_permission(self) -> None:
        """Touching a workflow file sets ``needs_workflows``."""
        self.fixture.commit(
            "CI: Add a workflow\n\nBody.\n",
            {".github/workflows/ci.yaml": "on: push\njobs: {}\n"},
        )
        self.fixture.bundle()
        self.fixture.manifest(pr_title="CI: Add a workflow")
        check = self.fixture.run()
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertTrue(check.needs_workflows)

    def test_multiple_commits_and_deletion(self) -> None:
        """Two commits keep their order; deletions are recorded; title is free."""
        first = self.fixture.commit(
            "Feat(core): Add module\n\nBody.\n", {"src/core.py": "x = 1\n"}
        )
        second = self.fixture.commit(
            "Chore: Remove old file\n\nBody.\n", {"old.txt": None}
        )
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Any title for two commits")
        check = self.fixture.run()
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertEqual([c["sha"] for c in check.commits], [first, second])
        self.assertEqual(check.commits[1]["additions"], [])
        self.assertEqual(check.commits[1]["deletions"], ["old.txt"])
        self.assertEqual(check.files_changed, 2)
        self.assertEqual(check.pr_title, "Any title for two commits")

    def test_usage_figures(self) -> None:
        """usage.json supplies premium requests and agent seconds."""
        (self.fixture.proposal / "usage.json").write_text(
            json.dumps({"totalPremiumRequestCost": 12, "totalApiDurationMs": 65000}),
            encoding="utf-8",
        )
        self.fixture.manifest(outcome="abstain", reason="nothing to do")
        check = self.fixture.run()
        self.assertEqual(check.premium_requests, 12)
        self.assertEqual(check.agent_seconds, 65)

    def test_unreadable_usage_is_a_note(self) -> None:
        """A corrupt usage.json is recorded, not fatal."""
        (self.fixture.proposal / "usage.json").write_text("{nope", encoding="utf-8")
        self.fixture.manifest(outcome="abstain", reason="nothing to do")
        check = self.fixture.run()
        self.assertEqual(check.verdict, "abstain")
        self.assertTrue(any("usage.json unreadable" in r for r in check.reasons))


class RunCheckOutcomeTest(GitCase):
    """Manifest outcomes other than ``proposed`` are recorded, not verified."""

    def test_abstain(self) -> None:
        """Abstain carries the reason and needs no bundle."""
        self.fixture.manifest(outcome="abstain", reason="issue needs a design")
        check = self.fixture.run()
        self.assertEqual(check.verdict, "abstain")
        self.assertEqual(check.reasons, ["issue needs a design"])
        self.assertEqual(check.commits, [])

    def test_author_failed(self) -> None:
        """author-failed without a reason gets a placeholder."""
        self.fixture.manifest(outcome="author-failed", reason=None)
        check = self.fixture.run()
        self.assertEqual(check.verdict, "author-failed")
        self.assertEqual(check.reasons, ["no reason given"])

    def test_unknown_outcome_rejected(self) -> None:
        """An outcome outside the contract is a rejection."""
        self.fixture.manifest(outcome="merged")
        check = self.fixture.run()
        self.assertEqual(check.verdict, "rejected")
        self.assertIn("not recognised", check.reasons[0])

    def test_missing_manifest_is_operational(self) -> None:
        """No manifest at all is a PublishError, not a verdict."""
        with self.assertRaises(policy.PublishError):
            self.fixture.run()

    def test_missing_bundle_rejected(self) -> None:
        """A proposed outcome without a bundle is rejected."""
        self.fixture.manifest()
        check = self.fixture.run()
        self.assertEqual(check.verdict, "rejected")
        self.assertIn("lacks changes.bundle", check.reasons[0])


class RunCheckRejectionTest(GitCase):
    """Proposals that break a rule are rejected with a reason."""

    def rejected(self) -> str:
        """Run the check, assert rejection, return the joined reasons."""
        check = self.fixture.run()
        self.assertEqual(check.verdict, "rejected", check.reasons)
        self.assertEqual(check.commits, [])
        return "; ".join(check.reasons)

    def test_executable_file(self) -> None:
        """An executable addition is rejected."""
        self.fixture.commit(
            "Feat: Add tool\n\nBody.\n",
            {"bin/tool.sh": "#!/bin/sh\n"},
            executable=("bin/tool.sh",),
        )
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Feat: Add tool")
        reasons = self.rejected()
        self.assertIn("executable", reasons)
        self.assertIn("bin/tool.sh", reasons)

    def test_symlink(self) -> None:
        """A symlink addition is rejected."""
        self.fixture.commit(
            "Feat: Add link\n\nBody.\n", symlinks={"link.md": "README.md"}
        )
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Feat: Add link")
        self.assertIn("symlink", self.rejected())

    def test_protected_file(self) -> None:
        """A change to AGENTS.md is rejected as protected."""
        self.fixture.commit(
            "Docs: Update agents\n\nBody.\n", {"AGENTS.md": "# Changed\n"}
        )
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Docs: Update agents")
        self.assertIn("AGENTS.md is protected", self.rejected())

    def test_subject_without_type(self) -> None:
        """A subject lacking a Conventional Commit type is rejected."""
        self.fixture.commit("fix readme\n\nBody.\n", {"README.md": "x\n"})
        self.fixture.bundle()
        self.fixture.manifest(pr_title="fix readme")
        self.assertIn("capitalised Conventional Commit type", self.rejected())

    def test_subject_over_gitlint_limit(self) -> None:
        """The target's .gitlint limit of 60 applies to the subject."""
        subject = "Fix(readme): " + "x" * 48
        self.assertEqual(len(subject), 61)
        self.fixture.commit(f"{subject}\n\nBody.\n", {"README.md": "x\n"})
        self.fixture.bundle()
        self.fixture.manifest(pr_title=subject)
        self.assertIn("exceeds 60 characters", self.rejected())

    def test_subject_within_gitlint_limit(self) -> None:
        """A 60-character subject, over the default 50, passes."""
        subject = "Fix(readme): " + "x" * 47
        self.assertEqual(len(subject), 60)
        self.fixture.commit(f"{subject}\n\nBody.\n", {"README.md": "x\n"})
        self.fixture.bundle()
        self.fixture.manifest(pr_title=subject)
        self.assertEqual(self.fixture.run().verdict, "proposed")

    def test_pr_title_mismatch(self) -> None:
        """A single-commit title that differs from the subject is rejected."""
        self.fixture.commit(
            "Fix(readme): Correct typo\n\nBody.\n", {"README.md": "x\n"}
        )
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Fix(readme): Something else")
        self.assertIn("must equal the subject", self.rejected())

    def test_pr_body_without_closes(self) -> None:
        """A body that does not close the issue is rejected."""
        self.fixture.commit(
            "Fix(readme): Correct typo\n\nBody.\n", {"README.md": "x\n"}
        )
        self.fixture.bundle()
        self.fixture.manifest(pr_body="Fixed it.\n\nCloses #8\n")
        self.assertIn(f"Closes #{ISSUE}", self.rejected())

    def test_manifest_repository_mismatch(self) -> None:
        """A manifest naming another repository disagrees with the selection."""
        self.fixture.commit(
            "Fix(readme): Correct typo\n\nBody.\n", {"README.md": "x\n"}
        )
        self.fixture.bundle()
        self.fixture.manifest(repository="owner/other", issue=ISSUE + 1)
        reasons = self.rejected()
        self.assertIn("disagrees", reasons)
        self.assertIn("repository", reasons)
        self.assertIn("issue", reasons)

    def test_too_many_commits(self) -> None:
        """Six commits exceed the limit of five."""
        for index in range(6):
            self.fixture.commit(
                f"Fix: Step {index}\n\nBody.\n", {f"f{index}.txt": f"{index}\n"}
            )
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Fix: Steps")
        self.assertIn("6 commits exceed the limit of 5", self.rejected())

    def test_bundle_branch_mismatch(self) -> None:
        """A bundle carrying a different branch name is rejected."""
        self.fixture.branch = "code-monkey/issue-8"
        run_git(self.fixture.agent, "checkout", "-q", "-b", self.fixture.branch)
        self.fixture.commit(
            "Fix(readme): Correct typo\n\nBody.\n", {"README.md": "x\n"}
        )
        self.fixture.bundle()
        self.fixture.manifest()
        self.assertIn(f"does not carry refs/heads/{BRANCH}", self.rejected())

    def test_merge_commit(self) -> None:
        """A merge in the bundled history is rejected as non-linear."""
        agent = self.fixture.agent
        run_git(agent, "checkout", "-q", "-b", "side", self.fixture.base_sha)
        self.fixture.commit("Fix: Side\n\nBody.\n", {"side.txt": "s\n"})
        run_git(agent, "checkout", "-q", BRANCH)
        self.fixture.commit("Fix: Main\n\nBody.\n", {"main.txt": "m\n"})
        run_git(agent, "merge", "-q", "--no-ff", "-m", "Merge side", "side")
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Fix: Merge")
        self.assertIn("linear", self.rejected())

    def test_missing_blank_line_after_subject(self) -> None:
        """Body text glued to the subject is rejected."""
        self.fixture.commit("Fix: X\nbody here\n", {"README.md": "x\n"})
        self.fixture.bundle()
        self.fixture.manifest(pr_title="Fix: X")
        self.assertIn("blank line after the subject", self.rejected())

    def test_unsigned_commit_still_gets_sign_off(self) -> None:
        """Without the agent's -s the publisher still appends the bot sign-off."""
        self.fixture.commit(
            "Fix(readme): Correct typo\n\nBody.\n", {"README.md": "x\n"}, sign_off=False
        )
        self.fixture.bundle()
        self.fixture.manifest()
        check = self.fixture.run()
        self.assertEqual(check.verdict, "proposed", check.reasons)
        self.assertTrue(check.commits[0]["body"].endswith(f"{COAUTHOR}\n{SIGN_OFF}"))

    def test_corrupt_bundle(self) -> None:
        """A file that is not a bundle fails verification."""
        (self.fixture.proposal / "changes.bundle").write_bytes(b"not a bundle")
        self.fixture.manifest()
        self.assertIn("bundle failed verification", self.rejected())

    def test_bundle_from_wrong_base(self) -> None:
        """A bundle whose prerequisite the clone lacks fails verification."""
        agent = self.fixture.agent
        run_git(agent, "checkout", "-q", "main")
        (agent / "extra.txt").write_text("e\n", encoding="utf-8")
        run_git(agent, "add", "extra.txt")
        run_git(agent, "commit", "-q", "-m", "Chore: Upstream moved on")
        moved = run_git(agent, "rev-parse", "HEAD").strip()
        run_git(agent, "checkout", "-q", BRANCH)
        run_git(agent, "rebase", "-q", moved)
        self.fixture.commit(
            "Fix(readme): Correct typo\n\nBody.\n", {"README.md": "x\n"}
        )
        target = self.fixture.proposal / "changes.bundle"
        run_git(agent, "bundle", "create", "-q", str(target), f"{moved}..{BRANCH}")
        self.fixture.manifest()
        self.assertIn("bundle failed verification", self.rejected())


class CheckSummaryTest(unittest.TestCase):
    """``check_summary`` renders the verdict and commit table."""

    def test_render(self) -> None:
        """Heading, reasons, one row per commit and the workflows line."""
        check = model.Check(
            key=KEY,
            repository=REPOSITORY,
            issue=ISSUE,
            branch=BRANCH,
            base_sha="a" * 40,
            default_branch="main",
            verdict="proposed",
            reasons=["usage.json unreadable: x"],
            needs_workflows=True,
            commits=[
                {
                    "sha": "b" * 40,
                    "headline": "Fix: A | B",
                    "body": "",
                    "additions": [{"path": "a", "blob": "c" * 40, "size": 10}],
                    "deletions": ["old"],
                }
            ],
        )
        summary = model.check_summary(check)
        self.assertIn(f"### {REPOSITORY}#{ISSUE} — `proposed`", summary)
        self.assertIn("- usage.json unreadable: x", summary)
        self.assertIn("| Fix: A \\| B | 2 | 10 |", summary)
        self.assertIn("Needs `workflows: write`: True", summary)

    def test_rejected_without_commits(self) -> None:
        """A rejection lists reasons and no table."""
        check = model.Check(
            key=KEY,
            repository=REPOSITORY,
            issue=ISSUE,
            branch=BRANCH,
            base_sha="a" * 40,
            default_branch="main",
            verdict="rejected",
            reasons=["nope"],
        )
        summary = model.check_summary(check)
        self.assertIn("`rejected`", summary)
        self.assertIn("- nope", summary)
        self.assertNotIn("| Commit |", summary)


class SelectionEntryTest(unittest.TestCase):
    """``selection_entry`` validates the trusted entry for a key."""

    @staticmethod
    def selection(**overrides: Any) -> dict[str, Any]:
        """A selection with one entry, overridable."""
        entry: dict[str, Any] = {
            "key": KEY,
            "repository": REPOSITORY,
            "number": ISSUE,
            "base_sha": "a" * 40,
            "branch": BRANCH,
            "default_branch": "main",
            "url": ISSUE_URL,
        }
        entry.update(overrides)
        return {"issues": [entry]}

    def test_found(self) -> None:
        """The matching entry is returned intact."""
        selection = self.selection()
        self.assertIs(checks.selection_entry(selection, KEY), selection["issues"][0])

    def test_bad_key(self) -> None:
        """A key with path characters is refused before lookup."""
        for bad in ("../x", "a/b", "", "a b"):
            with self.subTest(bad=bad), self.assertRaises(policy.PublishError):
                checks.selection_entry(self.selection(), bad)

    def test_missing_key(self) -> None:
        """No entry for the key is an error."""
        with self.assertRaisesRegex(policy.PublishError, "no entry"):
            checks.selection_entry(self.selection(), "other-1")

    def test_no_issue_list(self) -> None:
        """A selection without an issue list is an error."""
        with self.assertRaises(policy.PublishError):
            checks.selection_entry({"issues": "nope"}, KEY)

    def test_bad_base_sha(self) -> None:
        """A base_sha that is not 40 hex characters is refused."""
        for bad in ("abc", "A" * 40, "g" * 40):
            with self.subTest(bad=bad), self.assertRaises(policy.PublishError):
                checks.selection_entry(self.selection(base_sha=bad), KEY)

    def test_missing_fields(self) -> None:
        """Each required string or the number missing is an error.

        The field checks reuse ``monkey_github.require_*`` so a malformed
        local file surfaces as ``GitHubError``; ``publish.main`` catches
        both, so the behaviour is pinned rather than judged here.
        """
        for name in ("repository", "branch", "default_branch", "url", "number"):
            selection = self.selection()
            del selection["issues"][0][name]
            with (
                self.subTest(name=name),
                self.assertRaises((policy.PublishError, github.GitHubError)),
            ):
                checks.selection_entry(selection, KEY)


class SelectionIdentityTest(unittest.TestCase):
    """``selection_identity`` resolves trailers from the selection."""

    def test_identity(self) -> None:
        """Model prefix and bot fields build the identity."""
        identity = checks.selection_identity(
            {"model": "claude-opus-5", "bot": {"login": BOT_LOGIN, "email": BOT_EMAIL}},
            COAUTHORS,
        )
        self.assertEqual(identity.coauthor, "Claude <noreply@anthropic.com>")
        self.assertEqual(identity.sign_off, SIGN_OFF)

    def test_missing_bot(self) -> None:
        """A selection without a bot object is an error."""
        with self.assertRaises(policy.PublishError):
            checks.selection_identity({"model": "claude-opus-5"}, COAUTHORS)


class LoadJsonTest(unittest.TestCase):
    """``load_json`` accepts objects only."""

    def test_errors(self) -> None:
        """Missing, malformed and non-object files are PublishErrors."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.json"
            with self.assertRaises(policy.PublishError):
                model.load_json(path, "ctx")
            path.write_text("[1]", encoding="utf-8")
            with self.assertRaises(policy.PublishError):
                model.load_json(path, "ctx")
            path.write_text("{bad", encoding="utf-8")
            with self.assertRaisesRegex(policy.PublishError, "ctx: unreadable JSON"):
                model.load_json(path, "ctx")
            path.write_text('{"a": 1}', encoding="utf-8")
            self.assertEqual(model.load_json(path, "ctx"), {"a": 1})


if __name__ == "__main__":
    unittest.main()
