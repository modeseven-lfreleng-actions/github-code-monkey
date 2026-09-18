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
highest-ranked open issues, runs one Copilot CLI coding agent per
repository against a checkout of that repository, and pushes each
result to a bot branch. Pull requests open for human review; nothing
merges without a maintainer.

This repository is at the design stage. The
[design document](docs/DESIGN.md) records the intended architecture,
inputs, credentials, containment model and open questions. No
workflow exists yet.

## Relationship to `github-issues-triage`

The workflow runs after the daily
[issues triage](https://github.com/lfreleng-actions/github-issues-triage)
and consumes the labels, Priority and Type that triage applies. The
trusted/untrusted/trusted job layout, evidence sealing and runner
hardening carry over from that repository; the agent lane is Copilot
CLI alone.
