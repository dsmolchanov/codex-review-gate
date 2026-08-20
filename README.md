# codex-review-gate

A merge gate that answers exactly one question: **does Codex report a
P0/P1/[BLOCKER] on the current head commit?**

Verdict states are PASS / BLOCK / UNKNOWN, and only PASS exits 0. Absence of a
verdict is not approval.

It exists as a standalone repository because a gate distributed as *content* is
reviewed once per consumer. Copying the body into eight repositories put the
same file in front of eight independent reviewers and produced 21 blocking
findings, no two repositories agreeing, several asking for tests that existed in
two of the eight. Here it is reviewed once, where its executable tests live.

## Use

```yaml
name: codex-review-window
on:
  pull_request:
    types: [opened, reopened, synchronize, ready_for_review]
  pull_request_review:
    types: [submitted]

concurrency:
  group: codex-review-window-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  codex-review-window:
    uses: dsmolchanov/codex-review-gate/.github/workflows/codex-review-window.yml@v1
    permissions:
      contents: read
      issues: read
      pull-requests: write
    secrets:
      CODEX_REQUEST_TOKEN: ${{ secrets.CODEX_REQUEST_TOKEN }}
      SLACK_WEBHOOK: ${{ secrets.SLACK_WEBHOOK }}
```

### The required status check is named `codex-review-window / codex-review-window`

A reusable workflow reports as `<caller job> / <called job>`. Branch protection
must require that exact context. Requiring the bare `codex-review-window` waits
on something that never reports, and every pull request blocks forever.

### The caller owns the concurrency group

Do **not** add `concurrency:` to a fork of the called workflow. A called
workflow declaring the same group as its caller enters a group the caller
already holds and, with `cancel-in-progress`, cancels it — the run ends
`failure` with zero jobs and no annotation, which reads as an invalid workflow
file. `actionlint` does not catch it.

### `CODEX_REQUEST_TOKEN`

Codex serves review requests per **requesting identity** and refuses
`github-actions[bot]`, so the gate posts its request as a Codex-connected
account. A pull request's conversation comments are *issue* comments in the
REST API, so a fine-grained token needs **`Issues: Read and write`** and
**`Pull requests: Read and write`** — and nothing else.

This workflow runs from the pull request's copy of its caller on a same-repo
branch, and same-repo pull requests receive base-repository secrets. Anyone who
can push a branch can read this token, so scope it to nothing but commenting.

Without the secret the gate depends on Codex Automatic Reviews, which fire on
PR open / ready only — never on a pushed head.

## Tests

```
python3 -m pytest tests -q
```

`tests/test_gate_behavior.py` executes the workflow's `run:` block under bash
against a fixture-driven `gh` stub and asserts exit codes; a check not
demonstrated to fail is not a check.
