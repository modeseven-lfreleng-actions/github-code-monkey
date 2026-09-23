<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# Design: Scheduled AI Authoring of Pull Requests

Status: **implemented, awaiting rollout** (§15). The workflow,
scripts and offline tests exist; no live run has yet published a
branch. §17 records the decisions taken on the first draft's open
questions and the three points that rollout, not further
discussion, will settle.

This document reuses the architecture, vocabulary and lessons of
[`github-issues-triage`](https://github.com/lfreleng-actions/github-issues-triage)
(its `docs/development/DESIGN.md`, §7 and §13.7 in particular). Where
this design departs from that one, the text says why.

## 1. Problem Statement

The `lfreleng-actions` organisation holds a backlog of open issues
across some thirty repositories. Daily triage now labels each one
with category, Priority and Type, which ranks the backlog; nobody has
capacity to work it. Most issues are small, well-scoped and sit in
repositories with a uniform toolchain and a written contribution
standard, which makes them tractable for a coding agent working under
tight constraints.

## 2. Goal

Each weekday, after triage completes, run a coding agent against the
highest-ranked open issues and open one pull request per issue. Each
pull request:

- lives on a bot-owned branch in the target repository;
- carries commits that follow the organisation's `AGENTS.md`
  (signed, DCO trailer, Conventional Commit subject, co-author
  trailer, PR title equal to a single commit's subject);
- has passed the repository's own pre-commit hooks, tests and the
  `aislop` gate in the agent's checkout;
- states what changed, why, what the agent ran, and which run
  produced it;
- waits for a human. The organisation requires a RelEng approval
  before merge, and nothing here can supply one.

The measure of success is a pull request a reviewer can merge after
reading it, or reject in less time than the issue would have cost
them.

### Non-goals

- **Merging.** The workflow never merges, never approves and never
  dismisses a review.
- **Revising an open bot pull request.** Copilot reviews every
  pull request on creation; responding to that review, or to a
  human one, is a later capability (§16).
- **Working the `.github` repository.** Its issues track
  organisation-wide tasks that span repositories. It stays out of
  scope unless a dispatcher opts in.
- **Claude Code and Gemini CLI lanes.** Copilot CLI is the single
  harness. The organisation's Enterprise subscription supplies the
  models this design names.
- **Private repositories.** The estate is public. §16 lists what a
  private repository would need.

## 3. Relationship to Issues Triage

The triage workflow runs at 07:00 UTC on weekdays and applies labels, Priority
and Type. This workflow depends on that output:

- Priority drives the ranking (§6).
- Type and category labels drive exclusions (§6) and the commit type
  the agent chooses.
- An issue without a Type has not seen triage yet and waits a day.

The two workflows live in separate repositories, which rules out a
`workflow_run` trigger. The schedule offsets by two hours instead
(§11). Triage's `excluded-repos.txt` lists repositories that produce
noise; this workflow keeps its own list with the same format, seeded
from that one.

Inherited from triage without change: the three-job trusted /
untrusted / trusted layout; artifact retrieval by producer ID and
trusted digest, never by name; a per-invocation namespace
(`run-attempt-uuid`); `INPUT_PERMISSION-*` environment forwarding
for App token permissions the pinned action does not declare; the
`github_pat_` guard on the model token; harden-runner with the
egress allow-list summary printed once; scratch cleanup by plain
deletion under `continue-on-error`.

## 4. Architecture

```text
select (trusted)      author (untrusted, matrix)      publish (trusted)
  App: issues read      no App credential               App: per-repo write
  rank + choose         checkout target repo            verify bundle
  selection.json        Copilot CLI session             replay commits (API)
  SHA + ID + digest --> proposal bundle + manifest -->  open PR, report
```

The shape matches triage. The difference is what the untrusted job
produces. Triage's agent emits JSON that a trusted applier turns
into label writes. This agent emits **commits**: a git bundle and a
manifest. The trusted publisher turns those into a branch and a pull
request. The agent job never holds a credential that can write to
any repository, and it never holds the App key.

### 4.1 Why tool policy cannot contain this agent

Triage confines its agent to `cat`, `jq` and four read utilities and
denies `git`, `gh` and the write tool. Run 35324225186 showed the CLI
auto-approving anything it classifies as a read, so even that
containment rests on the deny list rather than the allow list.

A coding agent needs `git`, the write tool, package managers, test
runners and `prek`. Any interpreter in that set (`python`, `node`,
`bash` itself) is a standing bypass of every shell-level denial.
Tool policy on this job is a productivity setting, not a boundary.

The boundary is:

1. **No write credential in the author job.** Its native
   `GITHUB_TOKEN` has `contents: read`. The model PAT has Copilot
   Requests and nothing else. Public repositories clone without
   authentication.
2. **A trusted publisher that treats the bundle as data.** It
   verifies provenance, inspects the diff against a policy, and
   makes every write through the App with a token scoped to one
   repository.
3. **A human review gate** the organisation already enforces.

Consider everything on the author runner (packet, checkout, model
PAT, Actions runtime token) exposed to the issue text and repository
content the agent reads. A prompt-injected issue can waste model
spend and produce a bad diff. It cannot push, cannot open a pull
request, and cannot reach another repository.

### 4.2 What the design trusts the caller with

The calling workflow supplies both the App key and the
`assets_repository`/`assets_ref` coordinate that the trusted jobs
execute. The design trusts the caller with both: a caller able to
point the assets at hostile code could as well edit its own workflow
to run that code with the same key. Both callers here pin the assets
to their own `github.sha`, and `issues-triage` makes the same
assumption.

The pull request plumbing job executes the pull request's own
scripts with the native token alone. GitHub gives a fork's
`pull_request` run a `GITHUB_TOKEN` without write scopes and no
secrets, and
here that token can read nothing beyond public issues and contents,
expires with the job, and runs under block-mode egress. That is the
sandbox GitHub intends for untrusted pull requests, and the job
needs no stronger one.

## 5. Signed Commits Without a Key on the Runner

The organisation enforces commit signatures: one unsigned commit
makes a pull request unmergeable. A GPG or SSH signing key on the
author runner would sit next to the agent, which §4.1 rules out.

The publisher creates commits through the GraphQL
`createCommitOnBranch` mutation with the App installation token.
GitHub signs commits made this way with its own key and shows them
as **Verified**, attributed to the App's bot user. The author job's
local commits exist for the agent's benefit (its hooks run on them,
gitlint checks the message); the publisher re-creates each one from
its tree diff and message rather than pushing the agent's objects.

Consequences the implementation has to handle:

- **Message composition.** The publisher takes the subject and body
  from the agent's commit, appends the `Co-authored-by` trailer for
  the **model** that ran (§10.3) if absent, then
  `Signed-off-by: <bot login>[bot] <id+login[bot]@users.noreply.github.com>`.
  It rejects a subject over the repository's `.gitlint` limit or
  lacking a capitalised Conventional Commit type.
- **File modes and symlinks.** `FileAddition` carries a path and
  base64 contents; it cannot set the executable bit or create a
  symlink, and nothing in the mutation says it preserves the mode of
  an existing executable file. The Git Data API can set modes, but
  commits made through it carry no signature, which defeats the
  purpose. An unsigned fallback push would produce an
  unmergeable pull request and more work than it saves, so the
  publisher **rejects** a bundle whose diff adds or changes an
  executable file, a symlink, a submodule, or any file mode. The
  prompt tells the agent to leave those steps out, complete the
  rest, and open the pull request body with an `INFO` banner listing
  the follow-up a human has to make (§7.3).
- **Renames** become a deletion plus an addition. Git history shows
  the rename through similarity detection as usual.
- **Sequencing and rollback.** The publisher creates the branch at
  the agent's recorded base SHA, then replays commits in order,
  passing each result as the next `expectedHeadOid`. A failure from
  branch creation onwards (a create that loses its reply, a mismatch,
  a transient API error, a rejected pull request) deletes the branch
  before the step fails, and a delete that finds no branch counts as
  clean. A branch that already existed stays untouched: the run did
  not create it and has no claim to remove it. If that branch carries
  an open pull request from the target repository, a rerun of the
  same publish job has met its own earlier success, and the result
  reports that pull request without commenting again; otherwise the
  existing branch is a rejection: a
  half-built branch would otherwise count as a prior attempt (§6)
  and keep the issue out of every later run. The result records
  `publish-failed` with the reason and whether the branch came off.
- **Payload size.** GitHub does not document the mutation's request
  limit. The publisher caps total added bytes at 4 MiB and rejects
  binaries over 512 KiB; the rollout (§15) confirms the cap holds.
  The composed pull request body, provenance included, must fit
  GitHub's 65,536-character limit, checked before any write.

## 6. Issue Selection

The select job holds an App token with `issues: read`,
`metadata: read`, `issue-fields: read` and `issue-types: read`
across the organisation. It builds a candidate list and writes
`selection.json`.

**Candidate.** An open issue (not a pull request) in a **public**,
non-archived, non-template, non-fork repository of the target owner
(the author job clones without a credential, so private and
internal repositories cannot take part), whose repository is not on
the exclusion list and is not `.github` (unless
`include_dotgithub`), and which:

- has a Type (triage has run);
- has no `question`, `breaking-change` or `chore` label;
- has no `no-agent` label (a human opt-out, per issue);
- has no assignee, unless the caller sets `include_assigned`: an
  assignee means a human has claimed the work;
- has no pull request, open or closed, from a `code-monkey/*`
  branch **in the target repository itself** (the workflow already
  tried, or a human declined the result); a pull request from a fork
  branch of the same name does not count, since anyone can open one;
- has no other open pull request linked to close it.

**Ranking.** Priority `Urgent`, then `High`, `Medium`, `Low`; issues
with no Priority sort last. Within a priority, older `created_at`
first.

**One issue per repository.** After ranking, the selector keeps the
first issue seen for each repository and drops the rest. Two agents
in one repository would race on `main` and on the toolchain cache;
one agent working two issues would juggle branches and widen the
blast radius of a mistake. The rest of that repository's backlog
waits for the next run.

**Cap.** The first `max_issues` survivors form the selection. `0`
lifts the cap as far as the Actions matrix limit of 256 jobs, which
the selector applies itself so a broad run degrades to a bounded
selection instead of failing at matrix expansion. A second bound
holds the serialised selection under 12 MiB, inside the 16 MiB the
verifier accepts, since 256 entries at the per-issue body and
comment caps would otherwise exceed it.

**Guidance ref.** `guidance_ref` may name a branch, a tag or a
commit. The selector resolves it to a commit, following an annotated
tag, and refuses a ref whose target is anything but a commit, so the
recorded provenance always names the commit whose `AGENTS.md` the
agent read.

**Explicit repositories.** When `repositories` names one or more
repositories, the candidate scan covers those alone and skips the
exclusion list and the `.github` rule. Separators are commas,
spaces, or both; the selector normalises and rejects a name that is
not `[A-Za-z0-9_.-]+`.

**Outputs.** `selection.json` lists, per chosen issue: repository,
number, title, body, Priority, Type, labels, `created_at`, the
`main` HEAD SHA observed at selection time, and the comments that
pass the filter below. The job publishes the artifact ID and a
SHA-256 of the file as job outputs. It also publishes a JSON matrix
for the author job.

**Comments.** Comments often carry the clarification the body
lacks; they are also where a passer-by can address the agent
directly. The packet includes a comment when its
`author_association` is `OWNER` or `MEMBER`, in order, up to 20
comments and 64 KiB in total, and drops the rest with a count of
what it dropped. The issue body itself is always present: external
reports are the point of the exercise, and the prompt treats every
body as data rather than instruction.

## 7. The Author Job

One matrix entry per selected issue, `max-parallel` set from
`max_concurrent_agents`, `timeout-minutes` set from
`max_runtime_minutes` plus twenty minutes of setup slack. Actions
expressions cannot add, so the select job computes both budgets and
publishes them as outputs. Hosted runners cap a job at 360 minutes,
which bounds `max_runtime_minutes` at 330.

### 7.1 Inputs to the session

- A clone of the target repository at the SHA recorded in
  `selection.json`, made without credentials.
- The issue packet for this single issue: title, body, labels,
  Priority, Type, filtered comments (§6), extracted from
  `selection.json` and a per-issue fetch made in the select job.
- The organisation `AGENTS.md`, fetched in the **select** job from
  `lfreleng-actions/.github` at a commit SHA the select job records,
  and carried in the evidence artifact. The author job does not
  fetch it live.
- The repository's own `AGENTS.md`, if present, read from the
  checkout.
- The prompt (`prompt/author.md`), from this repository's checkout
  at the reusable workflow's pinned SHA.

### 7.2 Copilot CLI invocation

Pinned `@github/copilot@1.0.83` on Node 22. Triage verified 1.0.80;
this workflow needs `--usage-output-file`, which 1.0.80 lacks and
1.0.83 provides with an otherwise identical tool set.

```text
cd workspace && copilot --prompt="$prompt" --model="$MODEL" \
  --mode autopilot --no-ask-user \
  --no-custom-instructions --disable-builtin-mcps --no-auto-update \
  --allow-all-tools \
  --deny-tool="$deny" \
  --add-dir "$artefacts" \
  --secret-env-vars=COPILOT_GITHUB_TOKEN \
  --log-dir="$artefacts/copilot-logs" \
  --usage-output-file "$artefacts/usage.json" \
  --share="$artefacts/session-summary.md"
```

The working directory is the target checkout; `--add-dir` grants
the artefacts directory, where the agent writes its manifest and
the workflow keeps the prompt, packet and guidance.

`--no-custom-instructions` stops the CLI loading instruction files
from the checkout on its own terms. The prompt injects the org
`AGENTS.md` text directly and asks the agent to read the local stub.
That keeps the trusted copy authoritative and pinned.

The CLI's `skill` tool runs skills it finds in the working
directory's `.github/skills`. Skills are code the agent runs, and
the checkout is content the agent is there to change, so that trust
is off by default: `$deny` includes `skill` unless the caller sets
`load_repo_skills`. The input exists for dispatch against a
repository whose skills a maintainer has read. It never applies to
content from outside the target repository; the workflow checks out
the default branch of an organisation repository and nothing else.

`gh` and `git push` stay denied. The agent has nothing to
authenticate with, and a call that fails looks to the model like a
bug to work around. The prompt says the workflow opens the pull
request.

`--allow-all-tools` is the honest setting given §4.1. The deny list
holds what the agent must not attempt rather than what it cannot do.

`allow_subagents=false` adds `task` to the deny list. In
`@github/copilot@1.0.83` that is the tool which spawns sub-agents;
`list_agents`, `read_agent` and `write_agent` manage custom agent
definitions and go on the same list. A later CLI pin re-checks these
names against `--available-tools` output.

### 7.3 What the prompt asks for

1. Read the issue, the org guidance, the local `AGENTS.md`, and the
   repository. Decide whether the issue is tractable: a change the
   agent can make, test and explain with the tools present.
2. If not, write `artefacts/manifest.json` with
   `"outcome": "abstain"` and a reason. Stop.
3. Otherwise create a branch `code-monkey/issue-<n>` from the
   checked-out HEAD, make the change, run the repository's tests,
   `prek run --all-files` and `aislop ci --changes`, and fix what
   they report. Never `--no-verify`. Workflow files under
   `.github/workflows/` are in scope: this is a DevOps estate and
   the bot's App holds the permission to push them.
4. Where the fix needs an executable bit, a symlink, or anything
   else §5 cannot carry, leave that step out, finish the rest, and
   start the pull request body with an `INFO` banner that lists
   what a human has to do next and why the bot could not.
5. Commit with `git commit -s` (unsigned; §5 covers signing), one
   logical change per commit, following §6 of the org guidance for
   the message. Set the author to the bot identity the packet names.
6. Write `manifest.json`: `"outcome": "proposed"`, base SHA, the
   list of commit SHAs, a pull request title (equal to the subject
   when a single commit), a pull request body in the org's format
   with a `Closes #<n>` line, and the commands run with their exit
   codes.
7. Do not push, do not call `gh`, do not open a pull request, do
   not touch files outside the checkout, do not delete scratch
   files; the workflow does that.

### 7.4 Outputs

The job runs `git bundle create` for the range from the base SHA to
`code-monkey/issue-N` and uploads `changes.bundle`,
`manifest.json` and `usage.json` as the **proposal** artifact for
that matrix entry, named with the namespace and the issue key. It
uploads the CLI logs and `session-summary.md` separately as the
**session** artifact. The publisher reads the proposal and never
opens the session artifact.

A cleanup step then deletes the CLI's spilled tool output, its
home directory and the checkout, under `continue-on-error`.

### 7.5 Egress

The author job runs harden-runner in `audit` mode. It needs npm,
PyPI, GitHub, the Copilot API and whatever hook repositories the
target's `.pre-commit-config.yaml` pins. A block-mode allow-list
that covers every repository's toolchain is a maintenance load this
design defers; the audit log records what each session reached.

The select, publish and report jobs run in `block` mode with the
organisation's allow-list from `harden-runner-block-action`, pinned
by commit in both callers, with `allow_list_summary: 'true'` on the
select job alone. `block` is the reusable workflow's default; a
caller may pass `audit` to diagnose a blocked endpoint.

## 8. The Publish Job

A trusted matrix job over the same selection, after the whole
author matrix, that runs even when some author entries failed
(`!cancelled()` and a successful selection) so each issue gets a
verdict. Each entry holds the App key and mints tokens for its own
repository alone. A matrix rather than one sequential job because
GitHub exposes no per-entry outputs from a matrix job: the publisher
for an issue finds its proposal by the artifact **name** the author
entry used, which is acceptable because the publisher trusts nothing
in that artifact whatever its name, and step 1 cross-checks its
content against the verified selection.

For each selected issue:

1. **Locate the proposal artifact** by name
   (`monkey-proposal-<namespace>-<key>`). A missing artifact records
   `author-failed` for that issue. The evidence file `selection.json`
   comes by the select job's artifact **ID** and must match its
   digest before anything else happens; `monkey_evidence.py accept`
   then copies the bounded manifest, bundle and usage files and
   nothing else out of the untrusted download. An artifact that
   fails that acceptance records `author-failed` the same way, so
   every issue reaches a result, a comment and a report row.
2. **Verify the bundle.** `git bundle verify` against a fresh
   credential-less fetch of the target at the recorded base SHA. The
   bundle's prerequisite must equal that SHA, the history must be
   linear, and the commit count ≤ 5. Manifest fields present, typed,
   and equal to the selection's repository, issue, base and branch.
   Outcome `abstain` or `author-failed` records and moves on.
3. **Policy-check the diff** across all commits: no executable
   files, symlinks, submodules or mode changes (§5); no path escaping
   the tree and every path valid UTF-8, since the API takes text and
   the check would otherwise see one name while the API creates
   another; at least one and at most 100 file changes per commit,
   the mutation's requirements; total added bytes under the cap; no change to
   `AGENTS.md`, `LICENSE*`, `REUSE.toml` or `.gitlint`. Changes
   under `.github/workflows/` pass and flag the token mint in step 5.
4. **Check the message** of each commit against the rules the org
   guidance makes mechanical: subject length from the target's
   `.gitlint` (fallback 50), capitalised type from the allowed list,
   no trailing punctuation, blank line after the subject, body lines
   ≤ 72 outside URL lines. Compose the trailers (§5).
5. **Mint a token** for that repository alone (`repositories:` the
   bare `repo_name`, relative to `owner`) with `contents: write`,
   `pull_requests: write` (read in `branches` mode), `metadata:
   read`, plus `workflows: write` when step 3 saw a workflow change.
   Dry-run and `select` mode never reach this step.
6. **Create the branch** `code-monkey/issue-<n>` at the base SHA
   (an existing branch is a rejection), then replay commits per §5.
7. **Open the pull request** (`pull-requests` mode) against the
   default branch: title from the manifest (equal to the subject on
   a single commit, enforced), body from the manifest with an
   appended provenance block (run URL, model, issue link, commands
   run), label `code-monkey` if the label exists. Not a draft: a
   ready pull request triggers the automatic Copilot review and
   notifies code owners; a draft does neither by default.
8. **Comment on the issue** with one line: the pull request URL on
   success; on `abstain`, the agent's reason; on a policy or
   provenance rejection, which check failed and the run URL. The
   reason descends from the untrusted manifest; the publisher cuts
   it to 2,000 characters when recording and again when rendering,
   which keeps the comment inside GitHub's limit with the run URL
   intact. The comment uses a second per-repository token carrying
   `issues: write` and nothing else, minted for this step alone.
   GitHub has no permission that stops at comments: `issues: write`
   also covers labels, assignees, milestones, close and reopen. The
   publisher's code path calls the comment endpoint and no other,
   the token lives in one trusted step, and labelling stays with
   triage. Dry-run skips the comment.
9. **Record** `result.json` and a step-summary section. A final
   report job gathers every `result.json` by artifact-name pattern
   and renders one table: issue, verdict, output URL, premium
   requests consumed (from `usage.json`), detail.

## 9. Inputs

### 9.1 Reusable workflow (`code-monkey.yaml`)

| Input | Type | Default | Notes |
| ----- | ---- | ------- | ----- |
| `org` | string | required | Target owner |
| `mode` | string | `pull-requests` | `select`, `branches`, `pull-requests` |
| `dry_run` | boolean | `true` | Author runs; publisher writes nothing |
| `model` | string | `claude-opus-5` | Copilot model identifier (§10.3) |
| `max_issues` | string | `'10'` | `0` = unbounded |
| `max_runtime_minutes` | string | `'180'` | Per agent |
| `max_concurrent_agents` | string | `'10'` | 1–30, matrix `max-parallel` |
| `allow_subagents` | boolean | `true` | §7.2 |
| `include_dotgithub` | boolean | `false` | §6 |
| `include_assigned` | boolean | `false` | §6: work issues a human holds |
| `load_repo_skills` | boolean | `false` | §7.2: trust checkout skills |
| `repositories` | string | `''` | Comma and/or space separated |
| `exclude_repos` | string | `''` | Overrides bundled list |
| `guidance_repository` | string | `<org>/.github` | Holds the org `AGENTS.md` |
| `guidance_ref` | string | `main` | Branch or tag recorded and used |
| `egress_policy` | string | `block` | Trusted jobs; author is always `audit` |
| `egress_allow_config` | string | `''` | Allow-list action coordinate |
| `github_app_client_id` | string | `''` | Empty limits runs to dry-run |
| `assets_repository` | string | this repo | Prompt and scripts |
| `assets_ref` | string | `''` | Resolved to a SHA in select |

Secrets: `copilot_token` (model PAT, §10), `github_app_private_key`
(select and publish alone).

Numeric inputs are strings because reusable-workflow `number`
inputs cannot carry validation and `fromJSON` handles the
conversion at the point of use. The select job rejects values
outside the stated ranges.

### 9.2 Dispatch caller (`code-monkey-cron.yaml`)

`schedule: '0 9 * * 1-5'` and `workflow_dispatch` with:

| Input | Type | Default |
| ----- | ---- | ------- |
| `dry_run` | boolean | `true` |
| `model` | choice | Claude Opus 5 |
| `mode` | choice | `pull-requests` |
| `max_issues` | string | `'10'` |
| `max_runtime_minutes` | string | `'180'` |
| `max_concurrent_agents` | string | `'10'` |
| `allow_subagents` | boolean | `true` |
| `include_dotgithub` | boolean | `false` |
| `include_assigned` | boolean | `false` |
| `load_repo_skills` | boolean | `false` |
| `repositories` | string | `''` |

The `model` choice shows display names and the caller maps them to
the identifiers in §10.3. The schedule runs `pull-requests` mode
with `dry_run: false` once §15 clears it; until then the schedule
stays dry-run, the path triage took.

`max_issues` semantics: `1` is the smallest bounded value, `0` means
unbounded, anything else non-numeric or negative fails the select
job.

### 9.3 Mode and dry-run matrix

| `mode` | `dry_run` | Select | Author | Branch | PR |
| ------ | --------- | ------ | ------ | ------ | -- |
| `select` | any | yes | no | no | no |
| `branches` | true | yes | yes | no | no |
| `branches` | false | yes | yes | yes | no |
| `pull-requests` | true | yes | yes | no | no |
| `pull-requests` | false | yes | yes | yes | yes |

Dry-run publishes the verified diff and the composed messages as
an artifact and in the step summary, which is how the prompt and
policy get tuned before anything reaches a repository.

## 10. Credentials

### 10.1 A second GitHub App

A dedicated App, distinct from the triage bot, so authored code
carries its own identity in history and an administrator can revoke
its permissions without affecting triage. The App is under
construction; the permissions below are the design's request,
subject to its final configuration:

| Permission | Level | Used by | For |
| ---------- | ----- | ------- | --- |
| Metadata | read | select, publish | Repository listing |
| Issues | write | select, publish | Scan; one comment per outcome (§8 step 8) |
| Issue fields | read | select | Priority |
| Issue types | read | select | Type |
| Contents | write | publish | Branch, commits |
| Pull requests | write | publish | Open PR, label |
| Workflows | write | publish | Commits that touch `.github/workflows/` |

The select job mints a token with the read levels alone, org-wide by
default and scoped to the named repositories plus the guidance
repository when the caller lists any;
`issues: write` on the App is a ceiling, and each mint requests the
lower level it needs. The publish job mints one token per
repository for the write steps and a separate one for the comment.
The App key never leaves those two jobs and no action in the author
job receives it, including as a post-step input.

Workflow edits are in scope from the start. The estate is
workflows, and an issue labelled `CI` is as fair a target as any
other; a bot that cannot touch `.github/workflows/` would abstain
from a large share of the backlog. The publisher adds
`workflows: write` to a mint when the verified diff
needs it.

### 10.2 The model credential

The same arrangement triage proved: a personal fine-grained PAT with
Copilot Requests and no repository grants, held as the org secret
`COPILOT_CLI_TOKEN`, presented to the CLI as `COPILOT_GITHUB_TOKEN`
and checked for the `github_pat_` prefix. Its owner's entitlement
pays for every premium request; `usage.json` from each session makes
the spend visible per run. Expiry (15 December 2026) needs an owner
and a rotation reminder.

Copilot Requests scope is enough for `--model`. The PAT grants no
repository access and the author job has no other credential.

### 10.3 Models and the co-author trailer

A check of the four identifiers below against the pinned CLI on
18 September 2026, with the entitlement behind `COPILOT_CLI_TOKEN`,
started a session for each one. An unknown name fails at once with
`Model "..." from --model flag is not available`, before any
request reaches the model. An interactive `/model` shows the same
list.

| Display name | `--model` | `Co-authored-by` |
| ------------ | --------- | ---------------- |
| Claude Opus 5 (default) | `claude-opus-5` | `Claude <noreply@anthropic.com>` |
| Claude Fable 5.1 | `claude-fable-5.1` | `Claude <noreply@anthropic.com>` |
| Claude Sonnet 5 | `claude-sonnet-5` | `Claude <noreply@anthropic.com>` |
| GPT-6 Astra | `gpt-6-astra` | `ChatGPT <chatgpt@openai.com>` |

The trailer names the model, not the harness, in line with the
table in §6.3 of the org guidance. The mapping is by identifier
prefix (`claude-` → Claude, `gpt-` → ChatGPT, `gemini-` → Gemini)
and lives in `config/coauthors.json` in this repository, where a
pull request reviews any change to it. An organisation-level
variable would suit a mapping that more than one workflow shares;
triage does not commit, so nothing else reads it today. Move it when
a second consumer appears.

## 11. Scheduling and Concurrency

- Cron `0 9 * * 1-5`, two hours after triage, which takes under
  thirty minutes. A `repository_dispatch` from triage's caller would
  remove the guess at the cost of a cross-repository token; the
  offset is the first cut and §16 lists the dispatch as a later
  option.
- Caller concurrency group `code-monkey`, `cancel-in-progress:
  false`: one run at a time, and a dispatch during the scheduled
  run queues behind it.
- Within a run, the branch-existence rule in §6 and the
  one-issue-per-repository rule make concurrent agents independent.
- Across runs, the `code-monkey/*` pull request check in §6 stops a
  second attempt at an issue whose first attempt a human has not yet
  processed.

## 12. Artefacts and Retention

<!-- markdownlint-disable MD013 -->

| Artifact | Producer | Content | Retention |
| -------- | -------- | ------- | --------- |
| `monkey-evidence-<ns>` | select | `selection.json`, `matrix.json`, `agents.md`, summary | 7 days |
| `monkey-proposal-<ns>-<key>` | author | bundle, manifest, usage | 7 days |
| `monkey-session-<ns>-<key>-<attempt>` | author | CLI logs, prompt, share summary | 7 days |
| `monkey-result-<ns>-<key>-<attempt>` | publish | `check.json`, `result.json`, summary | 90 days |
| `code-monkey-<ns>-<attempt>` | report | `report.md`, `report.json` | 90 days |

<!-- markdownlint-enable MD013 -->

`<ns>` is the select job's namespace and `<key>` the issue key.
Trusted evidence downloads use the producer's artifact ID; the
proposal download uses the name (§8). Names that append the run
attempt let a rerun of the publish job alone avoid immutable-name
conflicts while producer artifacts survive.

## 13. Failure Modes

<!-- markdownlint-disable MD013 -->

| Failure | Effect | Mitigation |
| ------- | ------ | ---------- |
| Agent times out | No proposal artifact | Publisher records `author-failed`; issue eligible next run |
| Agent abstains | Manifest says why | Recorded; publisher comments the reason on the issue (§8 step 8) |
| Hooks need network the runner blocks | Agent cannot pass gates | Author job runs in audit mode (§7.5) |
| `main` moves after selection | Branch created at old base | GitHub reports mergeability; reviewer rebases or agent retries later |
| Diff needs a mode change or symlink | Publish refused | Agent leaves the step out and banners the follow-up (§5, §7.3) |
| Two runs overlap | Duplicate branches | Caller concurrency group; branch-exists refusal |
| Model PAT expired | Every agent fails at start | Prefix guard cannot detect; rotation owner (§10.2) |
| Runaway spend | Long sessions in parallel | `max_runtime_minutes` × `max_concurrent_agents` bounds wall-clock; usage report per run |
| Bundle forged by agent | Publisher replays attacker commits | Provenance by artifact ID; policy checks; per-repo token; human review |
| Bot commits fail DCO check | PR blocked | Trailer matches the API author identity (§5); rollout step 2 confirms against DCOAPP |

<!-- markdownlint-enable MD013 -->

## 14. Repository Layout

```text
.github/workflows/code-monkey.yaml       reusable workflow
.github/workflows/code-monkey-cron.yaml  schedule and dispatch caller
.github/workflows/testing.yaml           PR plumbing and offline tests
prompt/author.md                         agent prompt
config/excluded-repos.txt                repositories to skip
config/coauthors.json                    model prefix to trailer (§10.3)
scripts/select_issues.py                 selection policy and outputs
scripts/issue_reads.py                   GitHub reads for selection
scripts/monkey_github.py                 gh wrapper, REST and GraphQL
scripts/monkey_evidence.py               bounded verified copies
scripts/proposal_policy.py               the rules a proposal must pass
scripts/proposal_check.py                offline bundle verification
scripts/proposal_model.py                verdict record and its rendering
scripts/publish.py                       replay, open PR, reconcile
scripts/issue_comment.py                 one-line outcome comment
scripts/proposal_report.py               merge results into the report
tests/                                   unittest suite, offline
pyproject.toml, uv.lock                  Python tooling
docs/DESIGN.md                           this document
```

The template's linting configuration stays as the repository's
gate: `prek` hooks including gitleaks, gitlint, ruff, mypy,
basedpyright, actionlint, gha-workflow-linter, reuse, markdownlint,
write-good and `aislop` at threshold 100.

## 15. Rollout

1. Land the workflow with the schedule in dry-run and
   `pull-requests` mode. Inspect diffs, messages and abstentions
   for a week of runs.
2. Dispatch `branches` mode live against one repository named in
   `repositories`. Confirm Verified commits, DCO status, message
   format and that the branch-exists guard trips on a second run.
3. Dispatch `pull-requests` mode live against the same repository.
   Confirm the PR title check, Copilot's automatic review, label and
   provenance block.
4. Flip the schedule to live. Watch spend from `usage.json` and the
   ratio of merged to closed bot pull requests.

Each step is a signed commit and a PR through the normal review
process. Disabling the workflow in the Actions UI is the kill
switch.

## 16. Later Capabilities

- **Revision mode.** Re-run the agent on an open `code-monkey/*`
  pull request with unresolved Copilot or human review threads,
  within the ten-round cap the org guidance sets.
- **More than one issue per repository per run**, sequentially in
  one session, if the one-per-repository rule starves the backlog.
- **Stub roll-out.** The organisation `AGENTS.md` is on `main` of
  `lfreleng-actions/.github`; a one-off mode could open its §12 stub
  pull request in every repository, which is itself an issue this
  workflow could work.
- **Dispatch from triage** in place of the two-hour cron offset
  (§11), once a cross-repository token has an owner.
- **Private repositories.** A `contents: read` token for the author
  job would be the first credential on that runner and needs its
  own containment answer.

## 17. Decisions and Remaining Questions

The questions the first draft left open, and how they closed:

<!-- markdownlint-disable MD013 -->

| Question | Decision |
| -------- | -------- |
| Model identifiers | Checked live against the pinned CLI; §10.3 |
| Mode changes and symlinks | Reject; agent banners the human follow-up in the PR body (§5, §7.3). Unsigned commits are unmergeable and create work |
| Payload cap | 4 MiB added, 512 KiB per binary; rollout confirms (§5) |
| Workflow edits | In scope from day one; App holds `workflows: write`, minted on demand (§10.1) |
| Assigned issues | Skipped by default; `include_assigned` overrides (§6) |
| Sub-agent tool | `task`; agent-definition tools denied alongside (§7.2) |
| Skills from the checkout | Off by default; `load_repo_skills` for a repository a maintainer has read (§7.2) |
| Issue feedback | One comment per outcome via a comment-step token; no labelling, which stays with triage (§8 step 8). GitHub has no permission that stops at comments |
| Trigger | Offset cron at 09:00 UTC; dispatch from triage is a later option (§11, §16) |
| DCO | DCOAPP today, a pre-commit lint soon. The trailer matches the API commit's author identity (§5) |
| Co-author trailer | The model's, by identifier prefix, from `config/coauthors.json` (§10.3) |
| Comments in the packet | `OWNER` and `MEMBER` authors, bounded by count and bytes (§6) |
| Private repositories | Out of scope for v1 (§2, §16) |

<!-- markdownlint-enable MD013 -->

Still to confirm during rollout rather than before it:

1. **DCOAPP and API commits** (§5, §15 step 2). The app compares the
   `Signed-off-by` trailer with the commit author. Commits made
   through `createCommitOnBranch` carry the App's bot identity as
   author, so the trailer the publisher appends uses that identity.
   The first live `branches` run proves it, and if the app exempts
   bot authors the check passes regardless.
2. **Pre-commit DCO lint and bot commits.** The coming hook runs on
   the agent's local commits, which carry the bot identity and the
   `-s` trailer; API replay does not run hooks. Confirm the hook
   accepts the `[bot]` form when it lands.
3. **`createCommitOnBranch` payload limit** (§5). If a 4 MiB diff
   fails in rollout, lower the cap; the agent's abstention path
   covers larger changes.

## 18. Data Contracts

The files the three jobs exchange. Every reader treats a file from a
less trusted producer as hostile input: typed, bounded, and
cross-checked against the trusted `selection.json`.

### 18.1 `selection.json` (select → author, publish; trusted)

```json
{
  "schema": 1,
  "org": "lfreleng-actions",
  "generated_at": "2026-09-18T09:02:11Z",
  "mode": "pull-requests",
  "dry_run": true,
  "model": "claude-opus-5",
  "bot": {
    "login": "lf-code-monkey[bot]",
    "email": "123456+lf-code-monkey[bot]@users.noreply.github.com"
  },
  "guidance": {
    "repository": "lfreleng-actions/.github",
    "path": "AGENTS.md",
    "ref": "main",
    "commit": "<40 hex>",
    "sha256": "<64 hex>"
  },
  "candidates_seen": 78,
  "skipped": {"pull_request": 0, "no_type": 3, "assigned": 2, "label": 5,
              "attempted": 1, "linked_pr": 0, "repository": 12,
              "one_per_repo": 30, "cap": 15},
  "issues": [
    {
      "key": "sbom-action-40",
      "repository": "lfreleng-actions/sbom-action",
      "repo_name": "sbom-action",
      "number": 40,
      "url": "https://github.com/lfreleng-actions/sbom-action/issues/40",
      "title": "...",
      "body": "...",
      "labels": ["feature"],
      "priority": "Medium",
      "type": "Feature",
      "created_at": "2026-09-18T18:52:05Z",
      "author_association": "MEMBER",
      "default_branch": "main",
      "base_sha": "<40 hex>",
      "branch": "code-monkey/issue-40",
      "comments": [
        {"author": "login", "association": "MEMBER",
         "created_at": "...", "body": "..."}
      ],
      "comments_dropped": 0
    }
  ]
}
```

`key` is `<repo_name>-<number>` and doubles as the matrix key and
the artifact-name suffix. Beside it the select job writes
`matrix.json` (`{"include": [{key, repository, repo_name, number,
base_sha, branch}]}`), `agents.md` (the guidance bytes whose SHA-256 the
`guidance` block records) and `excluded-repos.txt`. The job outputs
`selection_sha256` and `guidance_sha256` over the exact bytes.

### 18.2 `manifest.json` (author → publish; untrusted)

Written by the agent into the artefacts directory. When the agent
leaves none, the workflow writes one with `"outcome":
"author-failed"` so the publisher has something to report.

```json
{
  "schema": 1,
  "outcome": "proposed",
  "repository": "lfreleng-actions/sbom-action",
  "issue": 40,
  "base_sha": "<40 hex>",
  "branch": "code-monkey/issue-40",
  "reason": null,
  "pr_title": "Feat(sbom): Add CycloneDX 1.6 output",
  "pr_body": "...\n\nCloses #40\n",
  "commands": [{"command": "uv run pytest", "exit_code": 0}]
}
```

`outcome` is `proposed`, `abstain` or `author-failed`; the last two
need a `reason`. `changes.bundle` sits beside it when the
outcome is `proposed`, created by the workflow (not the agent) from
`base_sha..branch`. `usage.json` is the CLI's own usage output.
Bounds on acceptance: manifest 1 MiB, bundle 32 MiB, usage 1 MiB;
regular files, no symlinks.

### 18.3 `check.json` (publish, offline verdict; trusted)

The output of `publish.py check`, produced without credentials.

```json
{
  "schema": 1,
  "key": "sbom-action-40",
  "repository": "lfreleng-actions/sbom-action",
  "issue": 40,
  "branch": "code-monkey/issue-40",
  "base_sha": "<40 hex>",
  "default_branch": "main",
  "verdict": "proposed",
  "reasons": [],
  "needs_workflows": false,
  "commits": [
    {
      "sha": "<40 hex>",
      "headline": "Feat(sbom): Add CycloneDX 1.6 output",
      "body": "...\n\nCo-authored-by: ...\nSigned-off-by: ...",
      "additions": [{"path": "src/x.py", "size": 1234}],
      "deletions": ["old.py"]
    }
  ],
  "pr_title": "Feat(sbom): Add CycloneDX 1.6 output",
  "pr_body": "...",
  "stats": {"files_changed": 2, "added_bytes": 1234}
}
```

`verdict` is `proposed`, `abstain`, `rejected` or `author-failed`
here; `result.json` (§18.4) adds `publish-failed` for a write that
failed after verification, with the branch rolled back.
`reasons` explains anything but `proposed`. `commits[].body` is the
composed message the publisher will send: the agent's body plus the
trailers §5 describes. `pr_body` carries the appended provenance
block.

### 18.4 `result.json` (publish, per issue; trusted)

```json
{
  "schema": 1,
  "key": "sbom-action-40",
  "repository": "lfreleng-actions/sbom-action",
  "issue": 40,
  "verdict": "proposed",
  "reasons": [],
  "dry_run": false,
  "mode": "pull-requests",
  "branch_url": "https://github.com/.../tree/code-monkey/issue-40",
  "commits": ["<40 hex>"],
  "pull_request_url": "https://github.com/.../pull/41",
  "comment_url": "https://github.com/.../issues/40#issuecomment-1",
  "premium_requests": 37,
  "agent_seconds": 1543
}
```

URLs are `null` where the mode or a rejection stopped short. The
report job merges every `result.json` into one table.

### 18.5 Script interfaces

```text
select_issues.py --org ORG --output-dir DIR --mode MODE --model ID
    [--dry-run] [--repositories "a, b"] [--exclude-file PATH]
    [--exclude-repos "a,b"] [--include-dotgithub] [--include-assigned]
    --max-issues N --guidance-repository O/R [--guidance-ref REF]
    [--guidance-path AGENTS.md] [--bot-slug SLUG]

monkey_evidence.py verify --directory DIR --selection-sha256 HEX
    --guidance-sha256 HEX
monkey_evidence.py accept --directory UNTRUSTED --output ACCEPTED

publish.py check --selection PATH --key KEY --proposal-dir DIR
    --workdir DIR --coauthors PATH --output check.json
    [--summary check-summary.md] [--run-url URL]
publish.py apply --check check.json --workdir DIR --mode MODE
    [--dry-run] --output result.json
publish.py comment --result result.json [--run-url URL]
publish.py report --results DIR --output-md PATH --output-json PATH
```

`select_issues.py`, `publish.py apply` and `publish.py comment` read
`GH_TOKEN`. Everything else runs offline. A verdict other than
`proposed` is data, not an error: the scripts exit non-zero for
operational failures alone.
