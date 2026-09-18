<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# 🐒 GitHub Code Monkey

<!-- prettier-ignore-start -->
<!-- markdownlint-disable-next-line MD013 -->
[![Linux Foundation](https://img.shields.io/badge/Linux-Foundation-blue)](https://linuxfoundation.org/) [![Source Code](https://img.shields.io/badge/GitHub-100000?logo=github&logoColor=white&color=blue)](https://github.com/lfreleng-actions/github-code-monkey) [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0) [![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/lfreleng-actions/github-code-monkey/badge)](https://scorecard.dev/viewer/?uri=github.com/lfreleng-actions/github-code-monkey)
<!-- prettier-ignore-end -->

Scheduled AI authoring of pull requests for open GitHub issues across
the `lfreleng-actions` organisation. A reusable workflow selects the
highest-ranked triaged issues, runs one Copilot CLI coding agent per
repository against a checkout of that repository, and publishes the
result as GitHub-signed commits on a bot branch with a pull request.
Nothing merges without a maintainer.

The [design document](docs/DESIGN.md) covers the architecture, the
trust model, inputs, credentials and the rollout plan.

## How it works

```text
select (trusted)      author (untrusted, matrix)    publish (trusted)
  App: read token       no repository credential      App: per-repo token
  rank, one per repo    checkout + Copilot CLI        verify bundle
  selection.json -----> bundle + manifest ----------> signed commits, PR
```

The author job never holds a credential that can write to a
repository. It leaves a git bundle and a manifest; the publish job
verifies both offline against the trusted selection, then replays
the commits through GitHub's `createCommitOnBranch`, which signs
them, and opens the pull request. The agent reads the organisation's
`AGENTS.md` at a recorded commit and works to its rules.

## Schedule and dispatch

`code-monkey-cron.yaml` runs at 09:00 UTC on weekdays, two hours
after the daily issues triage, and stays in dry-run until the
rollout in the design document clears it. Organisation
administrators can dispatch it by hand with a model, a mode
(`select`, `branches`, `pull-requests`), issue and runtime caps,
and a repository list.

## Reusable workflow

```yaml
jobs:
  code-monkey:
    permissions:
      issues: read
      contents: read
      actions: read
    uses: lfreleng-actions/github-code-monkey/.github/workflows/code-monkey.yaml@<sha>
    with:
      org: my-org
      mode: pull-requests
      dry_run: false
      github_app_client_id: ${{ vars.CODE_MONKEY_CLIENT_ID }}
    secrets:
      copilot_token: ${{ secrets.COPILOT_CLI_TOKEN }}
      github_app_private_key: ${{ secrets.CODE_MONKEY_PRIVATE_KEY }}
```

Live runs need a GitHub App installed on the organisation with the
permissions in the design document (section 10.1) and a personal
fine-grained PAT carrying Copilot Requests and no repository grants.

## Development

```bash
uv run python -B -m unittest discover -s tests -v
prek run --all-files
aislop ci
```
