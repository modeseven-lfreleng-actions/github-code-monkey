# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Offline contracts over the workflow YAML: trust boundary, pins, plumbing.

The reusable workflow separates a trusted ``select`` job, an untrusted
``author`` matrix and a trusted ``publish`` job (docs/DESIGN.md sections
4, 7, 8 and 10). These tests read the parsed documents, never their
comments, and pin the properties a reviewer would otherwise re-derive by
hand on every change: which job holds which credential, that downloads
go by producer artifact ID, that every action is pinned to a commit.
"""

from __future__ import annotations

import json
import re
import unittest
from collections import Counter
from pathlib import Path
from typing import Any, ClassVar, cast

import yaml  # pyright: ignore[reportMissingModuleSource]

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
REUSABLE = WORKFLOWS / "code-monkey.yaml"
CRON = WORKFLOWS / "code-monkey-cron.yaml"
TESTING = WORKFLOWS / "testing.yaml"

# PyYAML reads the bare mapping key ``on`` as the boolean ``True``.
TRIGGERS = True

HARDEN_RUNNER = "step-security/harden-runner"
BLOCK_ACTION = "lfreleng-actions/harden-runner-block-action"
CHECKOUT = "actions/checkout"
APP_TOKEN = "actions/create-github-app-token"
DOWNLOAD = "actions/download-artifact"
UPLOAD = "actions/upload-artifact"
REUSABLE_CALL = "$/.github/workflows/code-monkey.yaml"

COMMIT_PIN = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
SECRET_REFERENCE = re.compile(r"secrets\.[A-Za-z0-9_]+")
EXPRESSION = re.compile(r"^\$\{\{\s*(.*?)\s*\}\}$")


def squash(text: str) -> str:
    """Collapse runs of whitespace so block scalars compare line-wrap free."""
    return re.sub(r"\s+", " ", text).strip()


def flatten(script: str) -> str:
    """Join backslash continuations, then squash, for shell substring checks."""
    return squash(re.sub(r"\\\n\s*", " ", script))


def unwrap(expression: str) -> str:
    """Return the expression body without ``${{ }}`` wrapping or wrap noise."""
    text = squash(expression)
    match = EXPRESSION.match(text)
    return match.group(1) if match else text


def uses_of(step: dict[str, Any]) -> str:
    """Return the action coordinate of a step, or an empty string."""
    return str(step.get("uses", ""))


def is_action(step: dict[str, Any], action: str) -> bool:
    """Match an action invocation independently of its commit pin."""
    return uses_of(step).startswith(action + "@")


def dumped(node: object) -> str:
    """Serialise a YAML subtree so a regex can scan every value in it."""
    return json.dumps(node, sort_keys=True)


def line_number(script: str, exact: str) -> int:
    """Return the index of the first line whose stripped text equals `exact`."""
    for number, line in enumerate(script.splitlines()):
        if line.strip() == exact:
            return number
    raise AssertionError(f"no line reads {exact!r}")


class WorkflowCase(unittest.TestCase):
    """Read workflow structure, never comments."""

    WORKFLOW: ClassVar[Path]

    def setUp(self) -> None:
        """Load a fresh document for each contract."""
        loaded = yaml.safe_load(self.WORKFLOW.read_text(encoding="utf-8"))
        self.workflow: dict[str | bool, Any] = cast(dict[str | bool, Any], loaded)
        self.jobs: dict[str, dict[str, Any]] = cast(
            dict[str, dict[str, Any]], self.workflow["jobs"]
        )
        self.triggers: dict[str, Any] = cast(dict[str, Any], self.workflow[TRIGGERS])

    def steps(self, job: str) -> list[dict[str, Any]]:
        """Return the step list of a job that runs on a runner."""
        return cast(list[dict[str, Any]], self.jobs[job]["steps"])

    def step(self, job: str, identity: str) -> dict[str, Any]:
        """Find exactly one step by id or name."""
        matches = [
            s for s in self.steps(job) if identity in (s.get("id"), s.get("name"))
        ]
        self.assertEqual(len(matches), 1, f"{job}: expected one {identity!r} step")
        return matches[0]

    def position(self, job: str, identity: str) -> int:
        """Return the index of a step so ordering contracts can compare."""
        return self.steps(job).index(self.step(job, identity))

    def actions(self, job: str, action: str) -> list[dict[str, Any]]:
        """Select action invocations independently of their commit pin."""
        return [s for s in self.steps(job) if is_action(s, action)]

    def action(self, job: str, action: str) -> dict[str, Any]:
        """Return the single invocation of an action within a job."""
        matches = self.actions(job, action)
        self.assertEqual(len(matches), 1, f"{job}: expected one {action} step")
        return matches[0]

    def assert_expression(self, actual: str, expected: str) -> None:
        """Compare whole expressions ignoring `${{ }}` wrapping and whitespace."""
        self.assertEqual(unwrap(actual), unwrap(expected))

    def assert_pinned_steps(self) -> None:
        """Every step-level `uses:` names a 40-hex commit, never a tag or branch."""
        for job in self.jobs:
            if "steps" not in self.jobs[job]:
                continue
            for step in self.steps(job):
                if "uses" not in step:
                    continue
                with self.subTest(job=job, uses=step["uses"]):
                    self.assertRegex(uses_of(step), COMMIT_PIN)


class ReusableWorkflowCase(WorkflowCase):
    """Shared fixture for the reusable workflow contracts."""

    WORKFLOW = REUSABLE


class TopLevelContracts(ReusableWorkflowCase):
    """Permissions, concurrency, pins and runner hardening across all jobs."""

    def test_default_permissions_are_empty(self) -> None:
        """Every job opts into its own grants; the workflow default is none."""
        self.assertEqual(self.workflow["permissions"], {})

    def test_concurrency_is_per_caller_and_owner_without_cancel(self) -> None:
        """One pipeline per caller and target owner; a queued run waits."""
        concurrency = cast(dict[str, Any], self.workflow["concurrency"])
        group = str(concurrency["group"])
        self.assertIn("github.repository", group)
        self.assertIn("inputs.org", group)
        self.assertIs(concurrency["cancel-in-progress"], False)

    def test_every_action_is_pinned_to_a_commit(self) -> None:
        """No `uses:` rides a tag or branch that could move underneath us."""
        self.assert_pinned_steps()

    def test_every_checkout_drops_credentials(self) -> None:
        """No checkout leaves a token in `.git/config` for later steps."""
        for job in self.jobs:
            for step in self.actions(job, CHECKOUT):
                with self.subTest(job=job, step=step.get("name")):
                    self.assertIs(step["with"]["persist-credentials"], False)

    def test_harden_runner_leads_every_job(self) -> None:
        """Only the allow-list loader may precede harden-runner in a job."""
        for job in self.jobs:
            steps = self.steps(job)
            with self.subTest(job=job):
                self.assertTrue(
                    is_action(steps[0], BLOCK_ACTION)
                    or is_action(steps[0], HARDEN_RUNNER),
                    f"{job}: first step is {uses_of(steps[0])!r}",
                )
                guarded = [
                    i for i, s in enumerate(steps) if is_action(s, HARDEN_RUNNER)
                ]
                self.assertEqual(len(guarded), 1, f"{job}: one harden-runner step")
                for earlier in steps[: guarded[0]]:
                    self.assertTrue(is_action(earlier, BLOCK_ACTION))
                    self.assertNotIn("run", earlier)

    def test_allow_list_summary_prints_once_from_select(self) -> None:
        """The select job alone renders the allow-list to the step summary."""
        summaries: list[tuple[str, Any]] = [
            (job, step["with"]["allow_list_summary"])
            for job in self.jobs
            for step in self.actions(job, BLOCK_ACTION)
        ]
        self.assertTrue(summaries, "no harden-runner-block-action steps found")
        enabled = [job for job, value in summaries if value == "true"]
        self.assertEqual(enabled, ["select"])
        for job, value in summaries:
            if job != "select":
                with self.subTest(job=job):
                    self.assertEqual(value, "false")

    def test_every_trusted_job_loads_the_allow_list(self) -> None:
        """Select, publish and report load the allow-list before hardening.

        CONNECTION_ALLOW_LIST does not cross job boundaries, and
        harden-runner in block mode with an empty list enforces nothing.
        """
        for job in ("select", "publish", "report"):
            with self.subTest(job=job):
                loaders = self.actions(job, BLOCK_ACTION)
                self.assertEqual(len(loaders), 1)
                self.assert_expression(
                    loaders[0]["with"]["config"], "inputs.egress_allow_config"
                )
                harden = self.action(job, HARDEN_RUNNER)
                self.assert_expression(
                    harden["with"]["allowed-endpoints"], "env.CONNECTION_ALLOW_LIST"
                )
        self.assertEqual(self.actions("author", BLOCK_ACTION), [])


class AuthorJobContracts(ReusableWorkflowCase):
    """The untrusted job: no App key, audit egress, evidence before checkout."""

    def test_author_holds_no_app_key(self) -> None:
        """The agent runner never mints, receives or references the App key."""
        author = self.jobs["author"]
        self.assertEqual(self.actions("author", APP_TOKEN), [])
        self.assertEqual(author["permissions"], {"contents": "read"})
        self.assertNotIn("secrets.github_app_private_key", dumped(author))
        references = Counter(SECRET_REFERENCE.findall(dumped(author)))
        self.assertEqual(references, Counter({"secrets.copilot_token": 1}))
        agent = self.step("author", "agent")
        self.assert_expression(
            agent["env"]["COPILOT_GITHUB_TOKEN"], "secrets.copilot_token"
        )

    def test_author_budget_and_gating_come_from_select(self) -> None:
        """Audit egress; timeouts, parallelism and matrix from select outputs."""
        author = self.jobs["author"]
        harden = self.action("author", HARDEN_RUNNER)
        self.assertEqual(harden["with"]["egress-policy"], "audit")
        self.assert_expression(
            author["timeout-minutes"],
            "fromJSON(needs.select.outputs.author_timeout)",
        )
        self.assert_expression(
            self.step("author", "agent")["timeout-minutes"],
            "fromJSON(needs.select.outputs.agent_timeout)",
        )
        strategy = cast(dict[str, Any], author["strategy"])
        self.assertIs(strategy["fail-fast"], False)
        self.assert_expression(
            strategy["max-parallel"], "fromJSON(needs.select.outputs.max_parallel)"
        )
        self.assert_expression(
            strategy["matrix"], "fromJSON(needs.select.outputs.matrix)"
        )
        self.assert_expression(author["if"], "needs.select.outputs.run_agent == 'true'")

    def test_target_checkout_is_the_recorded_base_commit(self) -> None:
        """The workspace is the selected repository at the selection's SHA."""
        checkout = self.step("author", "Checkout target repository")
        self.assertTrue(is_action(checkout, CHECKOUT))
        with_ = cast(dict[str, Any], checkout["with"])
        self.assert_expression(with_["repository"], "matrix.repository")
        self.assert_expression(with_["ref"], "matrix.base_sha")
        self.assertEqual(with_["path"], "workspace")
        self.assertIs(with_["persist-credentials"], False)

    def test_evidence_arrives_by_id_and_is_verified_first(self) -> None:
        """Evidence comes by artifact ID and its digests gate later steps."""
        download = self.action("author", DOWNLOAD)
        with_ = cast(dict[str, Any], download["with"])
        self.assertNotIn("name", with_)
        self.assertNotIn("pattern", with_)
        self.assert_expression(
            with_["artifact-ids"], "needs.select.outputs.evidence_id"
        )

        verify = self.step("author", "Verify evidence bytes")
        self.assertIn("monkey_evidence.py verify", flatten(verify["run"]))
        env = cast(dict[str, Any], verify["env"])
        self.assert_expression(
            env["SELECTION_SHA"], "needs.select.outputs.selection_sha256"
        )
        self.assert_expression(
            env["GUIDANCE_SHA"], "needs.select.outputs.guidance_sha256"
        )
        script = flatten(verify["run"])
        self.assertIn('--selection-sha256 "$SELECTION_SHA"', script)
        self.assertIn('--guidance-sha256 "$GUIDANCE_SHA"', script)

        fetched = self.steps("author").index(download)
        verified = self.position("author", "Verify evidence bytes")
        self.assertLess(fetched, verified)
        self.assertLess(verified, self.position("author", "Checkout target repository"))
        self.assertLess(verified, self.position("author", "agent"))

    def test_agent_invocation_is_confined(self) -> None:
        """PAT prefix check, deny list, secret masking and the workspace cwd."""
        raw = str(self.step("author", "agent")["run"])
        script = flatten(raw)
        self.assertRegex(
            script,
            r'case "\$COPILOT_GITHUB_TOKEN" in github_pat_\*\) ;; \*\) .*?exit 1 ;; esac',
        )

        deny = re.search(r"deny='([^']*)'", script)
        self.assertIsNotNone(deny, "deny list initialiser missing")
        assert deny is not None
        denied = set(deny.group(1).split(","))
        self.assertLessEqual({"shell(gh)", "shell(gh:*)", "shell(git push)"}, denied)
        self.assertIn('--deny-tool="$deny"', script)

        for flag in (
            "--secret-env-vars=COPILOT_GITHUB_TOKEN",
            "--no-custom-instructions",
            "--disable-builtin-mcps",
            "--no-auto-update",
            "--mode autopilot",
            "--allow-all-tools",
            "--usage-output-file",
            "--share=",
        ):
            with self.subTest(flag=flag):
                self.assertIn(flag, script)

        self.assertRegex(
            script,
            r'if \[ "\$ALLOW_SUBAGENTS" != true \]; then '
            r'deny="\$deny,task,list_agents,read_agent,write_agent";? fi',
        )
        self.assertRegex(
            script,
            r'if \[ "\$LOAD_REPO_SKILLS" != true \]; then deny="\$deny,skill";? fi',
        )
        self.assertLess(
            line_number(raw, "cd workspace"), line_number(raw, "copilot \\")
        )

    def test_toolchain_installs_the_pinned_cli(self) -> None:
        """The CLI version is a single top-level constant the install reads."""
        env = cast(dict[str, Any], self.workflow["env"])
        self.assertEqual(env["COPILOT_CLI_VERSION"], "1.0.83")
        install = self.step("author", "Install agent toolchain")
        self.assertIn("@github/copilot@$COPILOT_CLI_VERSION", str(install["run"]))

    def test_bundle_step_always_leaves_a_typed_manifest(self) -> None:
        """A missing manifest becomes `author-failed`; only `proposed` bundles."""
        bundle = self.step("author", "Bundle proposed commits")
        self.assert_expression(
            bundle["if"], "always() && steps.packet.outcome == 'success'"
        )
        script = flatten(bundle["run"])
        self.assertIn("if [ ! -s artefacts/manifest.json ]", script)
        self.assertIn('"author-failed"', script)
        guard = script.index('if [ "$outcome" = proposed ]')
        create = re.search(r"git (-C \S+ )?bundle create", script)
        self.assertIsNotNone(create, "git bundle create missing")
        assert create is not None
        self.assertLess(guard, create.start())

    def test_proposal_and_session_artifact_names(self) -> None:
        """Proposal is exactly three files under a namespaced, keyed name."""
        proposal = self.step("author", "proposal")
        self.assertTrue(is_action(proposal, UPLOAD))
        with_ = cast(dict[str, Any], proposal["with"])
        self.assertEqual(
            with_["name"],
            "monkey-proposal-${{ needs.select.outputs.namespace }}-${{ matrix.key }}",
        )
        self.assertEqual(with_["if-no-files-found"], "error")
        self.assertEqual(
            set(str(with_["path"]).split()),
            {
                "artefacts/manifest.json",
                "artefacts/changes.bundle",
                "artefacts/usage.json",
            },
        )
        session = self.step("author", "Preserve session output")
        self.assertTrue(is_action(session, UPLOAD))
        self.assertIn("github.run_attempt", str(session["with"]["name"]))

    def test_cleanup_never_gates_publish(self) -> None:
        """Scratch removal always runs, tolerates failure and drops the checkout."""
        cleanup = self.step("author", "Clear session scratch files")
        self.assert_expression(cleanup["if"], "always()")
        self.assertIs(cleanup["continue-on-error"], True)
        self.assertRegex(flatten(cleanup["run"]), r"rm -rf .*\bworkspace\b")


class PublishJobContracts(ReusableWorkflowCase):
    """The trusted publisher: verify before fetch, App tokens per repository."""

    def test_publish_runs_after_author_with_read_grants(self) -> None:
        """Every author entry gets a verdict; the job itself can write nothing."""
        publish = self.jobs["publish"]
        self.assertEqual(publish["needs"], ["select", "author"])
        condition = unwrap(str(publish["if"]))
        self.assertIn("!cancelled()", condition)
        self.assertIn("needs.select.result == 'success'", condition)
        self.assertIn("needs.select.outputs.run_agent == 'true'", condition)
        self.assertEqual(
            publish["permissions"], {"actions": "read", "contents": "read"}
        )

    def test_evidence_is_verified_before_the_proposal_is_fetched(self) -> None:
        """Trusted bytes by ID and digest first; the proposal by name after."""
        evidence = self.step("publish", "Fetch selection evidence")
        self.assertTrue(is_action(evidence, DOWNLOAD))
        self.assert_expression(
            evidence["with"]["artifact-ids"], "needs.select.outputs.evidence_id"
        )
        verified = self.step("publish", "verified")
        self.assertIn("monkey_evidence.py verify", flatten(verified["run"]))

        fetch = self.step("publish", "fetch")
        self.assertTrue(is_action(fetch, DOWNLOAD))
        with_ = cast(dict[str, Any], fetch["with"])
        self.assertIn("name", with_)
        self.assertNotIn("artifact-ids", with_)
        self.assertEqual(with_["path"], "untrusted-proposal")
        self.assertIs(fetch["continue-on-error"], True)

        steps = self.steps("publish")
        self.assertLess(steps.index(evidence), steps.index(verified))
        self.assertLess(steps.index(verified), steps.index(fetch))

        accept = self.step("publish", "Accept bounded proposal files")
        self.assert_expression(accept["env"]["FETCHED"], "steps.fetch.outcome")
        script = flatten(accept["run"])
        self.assertIn('if [ "$FETCHED" != success ]', script)
        # A failed acceptance falls through to the typed manifest too.
        self.assertIn(
            "elif ! python3 monkey-assets/scripts/monkey_evidence.py accept", script
        )
        self.assertIn("the proposal artifact failed bounded acceptance", script)
        self.assertIn("monkey_evidence.py accept", script)
        self.assertIn('"author-failed"', script)

    def test_offline_check_and_scoped_token_mints(self) -> None:
        """The check has no token; each mint names one repository and grants."""
        check = self.step("publish", "check")
        script = flatten(check["run"])
        self.assertIn("publish.py check", script)
        self.assertIn("--coauthors monkey-assets/config/coauthors.json", script)
        self.assertNotIn("GH_TOKEN", cast(dict[str, Any], check.get("env", {})))

        write = self.step("publish", "write-token")
        self.assertTrue(is_action(write, APP_TOKEN))
        condition = unwrap(str(write["if"]))
        self.assertIn("!inputs.dry_run", condition)
        self.assertIn("steps.check.outputs.verdict == 'proposed'", condition)
        self.assertIn("inputs.github_app_client_id != ''", condition)
        with_ = cast(dict[str, Any], write["with"])
        self.assert_expression(with_["repositories"], "matrix.repo_name")
        self.assertEqual(with_["permission-contents"], "write")
        self.assert_expression(
            with_["permission-pull-requests"],
            "inputs.mode == 'pull-requests' && 'write' || 'read'",
        )
        self.assert_expression(
            with_["permission-workflows"],
            "steps.check.outputs.needs_workflows == 'true' && 'write' || ''",
        )

        comment = self.step("publish", "comment-token")
        self.assertTrue(is_action(comment, APP_TOKEN))
        condition = unwrap(str(comment["if"]))
        self.assertIn("!inputs.dry_run", condition)
        # The comment must also reach the issue after a failed apply, so
        # the rollback is visible; never after cancellation.
        self.assertIn("!cancelled()", condition)
        self.assertIn("steps.apply.outcome != 'skipped'", condition)
        comment_with = cast(dict[str, Any], comment["with"])
        self.assertEqual(comment_with["permission-issues"], "write")
        self.assert_expression(comment_with["repositories"], "matrix.repo_name")

    def test_writes_never_fall_back_to_the_native_token(self) -> None:
        """Branch, pull request and comment writes use App tokens alone."""
        apply = self.step("publish", "Publish branch and pull request")
        self.assert_expression(
            apply["env"]["GH_TOKEN"], "steps.write-token.outputs.token"
        )
        comment = self.step("publish", "Comment on the issue")
        self.assert_expression(
            comment["env"]["GH_TOKEN"], "steps.comment-token.outputs.token"
        )
        self.assertNotIn("github.token", dumped(apply))
        self.assertNotIn("github.token", dumped(comment))
        self.assertNotIn("github.token", dumped(self.jobs["publish"]))

    def test_provenance_inputs_are_shape_checked(self) -> None:
        """Select outputs are validated as SHA, ID, digests and key patterns."""
        script = str(self.step("publish", "Require trusted provenance")["run"])
        for check in (
            '[[ "$ASSETS_SHA" =~ ^[0-9a-f]{40}$ ]]',
            '[[ "$EVIDENCE_ID" =~ ^[1-9][0-9]*$ ]]',
            '[[ "$SELECTION_SHA" =~ ^[0-9a-f]{64}$ ]]',
            '[[ "$GUIDANCE_SHA" =~ ^[0-9a-f]{64}$ ]]',
            '[[ "$KEY" =~ ^[A-Za-z0-9_.-]+$ ]]',
        ):
            with self.subTest(check=check):
                self.assertIn(check, script)


class SelectAndReportContracts(ReusableWorkflowCase):
    """Trusted selection outputs, org-wide read mint, result gathering."""

    def test_select_publishes_the_full_provenance_set(self) -> None:
        """Every value downstream jobs trust is an output of the select job."""
        outputs = cast(dict[str, Any], self.jobs["select"]["outputs"])
        self.assertEqual(
            set(outputs),
            {
                "assets_sha",
                "namespace",
                "evidence_id",
                "selection_sha256",
                "guidance_sha256",
                "count",
                "matrix",
                "run_agent",
                "author_timeout",
                "agent_timeout",
                "max_parallel",
            },
        )

    def test_live_runs_require_the_app(self) -> None:
        """A live run in any writing mode fails early without a client id."""
        guard = self.step("select", "Require App for live runs")
        condition = unwrap(str(guard["if"]))
        self.assertIn("!inputs.dry_run", condition)
        self.assertIn("inputs.mode != 'select'", condition)
        self.assertIn("inputs.github_app_client_id == ''", condition)

    def test_read_mint_scopes_to_the_requested_repositories(self) -> None:
        """Selection reads with the caller's repository list; empty is org-wide."""
        mint = self.step("select", "app-token")
        self.assertTrue(is_action(mint, APP_TOKEN))
        with_ = cast(dict[str, Any], mint["with"])
        self.assertEqual(with_["permission-issues"], "read")
        self.assertEqual(with_["permission-contents"], "read")
        self.assertEqual(
            with_["repositories"], "${{ steps.budget.outputs.repositories }}"
        )
        self.assertNotIn("write", json.dumps(with_))
        env = cast(dict[str, Any], mint["env"])
        self.assertEqual(env["INPUT_PERMISSION-ISSUE-FIELDS"], "read")
        self.assertEqual(env["INPUT_PERMISSION-ISSUE-TYPES"], "read")
        # A scoped list must still reach the guidance repository, or a
        # targeted run cannot read AGENTS.md with its own token.
        budget = squash(str(self.step("select", "budget")["run"]))
        self.assertIn('guidance_name="${GUIDANCE_REPOSITORY#*/}"', budget)
        self.assertIn('repositories="$repositories,$guidance_name"', budget)
        self.assertIn("sed '/^$/d'", budget)

    def test_manifest_outcome_is_printed_by_known_value(self) -> None:
        """The author job prints a recognised outcome, never raw manifest text."""
        script = squash(str(self.step("author", "Bundle proposed commits")["run"]))
        self.assertIn("*) outcome=unrecognised ;;", script)
        self.assertIn('echo "Manifest outcome: $outcome"', script)
        self.assertNotIn('echo "Manifest outcome: $(jq', script)

    def test_evidence_upload_is_mandatory_and_short_lived(self) -> None:
        """Evidence must exist and lives seven days, per the retention table."""
        upload = self.step("select", "evidence")
        self.assertTrue(is_action(upload, UPLOAD))
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        self.assertEqual(upload["with"]["retention-days"], 7)

    def test_report_gathers_namespaced_results_for_ninety_days(self) -> None:
        """The report waits on all jobs, reads this run's results, keeps 90d."""
        report = self.jobs["report"]
        self.assertEqual(report["needs"], ["select", "author", "publish"])
        gather = self.action("report", DOWNLOAD)
        self.assertEqual(
            gather["with"]["pattern"],
            "monkey-result-${{ needs.select.outputs.namespace }}-*",
        )
        final = self.step("report", "Attach run report")
        self.assertTrue(is_action(final, UPLOAD))
        self.assertEqual(final["with"]["retention-days"], 90)


class InterfaceContracts(ReusableWorkflowCase):
    """The `workflow_call` surface: inputs, secrets and their defaults."""

    def test_declared_inputs_and_secrets(self) -> None:
        """The input set matches DESIGN.md section 9.1; secrets are optional."""
        call = cast(dict[str, Any], self.triggers["workflow_call"])
        inputs = cast(dict[str, dict[str, Any]], call["inputs"])
        self.assertEqual(
            set(inputs),
            {
                "org",
                "mode",
                "dry_run",
                "model",
                "max_issues",
                "max_runtime_minutes",
                "max_concurrent_agents",
                "allow_subagents",
                "include_dotgithub",
                "include_assigned",
                "load_repo_skills",
                "repositories",
                "exclude_repos",
                "guidance_repository",
                "guidance_ref",
                "egress_policy",
                "egress_allow_config",
                "github_app_client_id",
                "assets_repository",
                "assets_ref",
            },
        )
        secrets = cast(dict[str, dict[str, Any]], call["secrets"])
        self.assertEqual(set(secrets), {"copilot_token", "github_app_private_key"})
        for name, secret in secrets.items():
            with self.subTest(secret=name):
                self.assertIs(secret["required"], False)

    def test_input_defaults(self) -> None:
        """Defaults keep an unconfigured call dry, bounded and sub-agent aware."""
        call = cast(dict[str, Any], self.triggers["workflow_call"])
        inputs = cast(dict[str, dict[str, Any]], call["inputs"])
        self.assertIs(inputs["org"]["required"], True)
        expected: dict[str, object] = {
            "dry_run": True,
            "mode": "pull-requests",
            "model": "claude-opus-5",
            "max_issues": "10",
            "max_runtime_minutes": "180",
            "max_concurrent_agents": "10",
            "allow_subagents": True,
            "include_dotgithub": False,
            "include_assigned": False,
            "load_repo_skills": False,
        }
        for name, default in expected.items():
            with self.subTest(input=name):
                actual: object = inputs[name]["default"]
                self.assertIs(type(actual), type(default))
                self.assertEqual(actual, default)


class CronCallerContracts(WorkflowCase):
    """The scheduled caller: schedule, dispatch form and what it forwards."""

    WORKFLOW = CRON

    def test_schedule_and_concurrency(self) -> None:
        """Weekday 09:00 UTC, one run at a time, later runs queue."""
        self.assertEqual(self.triggers["schedule"], [{"cron": "0 9 * * 1-5"}])
        concurrency = cast(dict[str, Any], self.workflow["concurrency"])
        self.assertEqual(concurrency["group"], "code-monkey")
        self.assertIs(concurrency["cancel-in-progress"], False)

    def test_dispatch_form(self) -> None:
        """The manual form mirrors DESIGN.md section 9.2 with safe defaults."""
        inputs = cast(
            dict[str, dict[str, Any]], self.triggers["workflow_dispatch"]["inputs"]
        )
        self.assertEqual(inputs["dry_run"]["type"], "boolean")
        self.assertIs(inputs["dry_run"]["default"], True)

        model = inputs["model"]
        self.assertEqual(model["type"], "choice")
        self.assertEqual(
            model["options"],
            ["Claude Opus 5", "Claude Fable 5.1", "Claude Sonnet 5", "GPT-6 Astra"],
        )
        self.assertEqual(model["default"], "Claude Opus 5")

        mode = inputs["mode"]
        self.assertEqual(mode["type"], "choice")
        self.assertEqual(mode["options"], ["select", "branches", "pull-requests"])
        self.assertEqual(mode["default"], "pull-requests")

        defaults: dict[str, object] = {
            "max_issues": "10",
            "max_runtime_minutes": "180",
            "max_concurrent_agents": "10",
            "allow_subagents": True,
            "include_dotgithub": False,
            "include_assigned": False,
            "load_repo_skills": False,
            "repositories": "",
        }
        for name, default in defaults.items():
            with self.subTest(input=name):
                actual: object = inputs[name]["default"]
                self.assertIs(type(actual), type(default))
                self.assertEqual(actual, default)

    def test_model_display_names_map_to_identifiers(self) -> None:
        """Each form choice resolves to a CLI identifier; anything else fails."""
        script = str(self.step("model", "map")["run"])
        arms = dict(re.findall(r"'([^']+)'\) id='([^']+)' ;;", script))
        self.assertEqual(
            arms,
            {
                "Claude Opus 5": "claude-opus-5",
                "Claude Fable 5.1": "claude-fable-5.1",
                "Claude Sonnet 5": "claude-sonnet-5",
                "GPT-6 Astra": "gpt-6-astra",
            },
        )
        self.assertRegex(script, r"\*\)[^\n]*exit 1")

    def test_caller_forwards_credentials_and_pins_assets(self) -> None:
        """Scheduled runs stay dry; secrets and App id come from org config."""
        job = self.jobs["code-monkey"]
        self.assertEqual(job["uses"], REUSABLE_CALL)
        with_ = cast(dict[str, Any], job["with"])
        self.assert_expression(with_["model"], "needs.model.outputs.id")
        self.assert_expression(
            with_["dry_run"],
            "github.event_name != 'workflow_dispatch' || inputs.dry_run",
        )
        self.assertIn(
            "vars.LF_CODE_MONKEY_CLIENT_ID", unwrap(str(with_["github_app_client_id"]))
        )
        self.assert_expression(with_["assets_ref"], "github.sha")
        # Trusted jobs hold App tokens: block mode with the pinned
        # organisation allow-list, never audit, on the live caller.
        self.assertEqual(with_["egress_policy"], "block")
        self.assertRegex(str(with_["egress_allow_config"]), r"^@[0-9a-f]{40}$")
        secrets = cast(dict[str, Any], job["secrets"])
        self.assertEqual(set(secrets), {"copilot_token", "github_app_private_key"})
        self.assert_expression(secrets["copilot_token"], "secrets.COPILOT_CLI_TOKEN")
        self.assert_expression(
            secrets["github_app_private_key"], "secrets.LF_CODE_MONKEY_PRIVATE_KEY"
        )

    def test_caller_steps_are_pinned(self) -> None:
        """The model job's actions are commit-pinned like the reusable ones."""
        self.assert_pinned_steps()


class TestingCallerContracts(WorkflowCase):
    """Pull request plumbing: secretless selection and the offline suite."""

    WORKFLOW = TESTING

    def test_plumbing_is_secretless_selection_of_the_pr_head(self) -> None:
        """PR runs select only, dry, with no secrets, from the PR's own assets."""
        job = self.jobs["plumbing"]
        self.assertEqual(job["uses"], REUSABLE_CALL)
        self.assertNotIn("secrets", job)
        with_ = cast(dict[str, Any], job["with"])
        self.assertEqual(with_["mode"], "select")
        self.assertIs(with_["dry_run"], True)
        self.assert_expression(
            with_["assets_repository"], "github.event.pull_request.head.repo.full_name"
        )
        self.assert_expression(
            with_["assets_ref"], "github.event.pull_request.head.sha"
        )

    def test_callers_read_guidance_from_the_organisation_default(self) -> None:
        """Test runs use the reusable default, <org>/.github at main.

        The organisation AGENTS.md has merged; a pinned fork branch here
        would test against guidance production never reads.
        """
        for name in ("plumbing", "dry-run"):
            with self.subTest(job=name):
                with_ = cast(dict[str, Any], self.jobs[name]["with"])
                self.assertNotIn("guidance_repository", with_)
                self.assertNotIn("guidance_ref", with_)

    def test_regression_runs_the_locked_unittest_suite(self) -> None:
        """The offline suite runs under the locked environment, verbosely."""
        step = self.step("regression", "Run offline regression suite")
        self.assertEqual(
            str(step["run"]).strip(),
            "uv run --locked python -B -m unittest discover -s tests -v",
        )

    def test_caller_steps_are_pinned(self) -> None:
        """The regression job's actions are commit-pinned."""
        self.assert_pinned_steps()


if __name__ == "__main__":
    unittest.main()
