"""Structural tests for the codex-review-window gate template.

The gate is the merge gate for every repository in the fleet, it ships as
inline shell inside a workflow, and nothing in the consumer repos lints it. The
safety property under test is narrow and absolute:

    No path may exit 0 while a blocker is open or a verdict is unknown.

That property is load-bearing rather than stylistic, because
`bootstrap-dsmolchanov-repo.sh` enables repo-wide auto-merge and requires zero
approving reviews. Any green check merges the pull request immediately, with no
human in the loop. Three PRs merged with open P1s in August 2026 because a
timeout path exited 0.
"""
from __future__ import annotations

import pathlib
import re

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
GATE = (
    REPO_ROOT
    / "scripts"
    / "repo-templates"
    / ".github"
    / "workflows"
    / "codex-review-window.yml"
)
RAW = GATE.read_text(encoding="utf-8")
DOC = yaml.safe_load(RAW)
# PyYAML parses the bare `on:` key as the boolean True.
ON = DOC[True]
JOB = DOC["jobs"]["codex-review-window"]
SCRIPT = JOB["steps"][0]["run"]


# --------------------------------------------------------------------------
# Required-check identity
# --------------------------------------------------------------------------


def test_job_key_is_the_required_context():
    """Branch protection pins this exact string; renaming removes the gate.

    A required context that never reports leaves the merge blocked forever, so
    the failure is loud — but a *renamed* job means branch protection waits on
    a context nothing produces, which is indistinguishable from an outage.
    """
    assert list(DOC["jobs"].keys()) == ["codex-review-window"]
    assert "name" not in JOB, "a job-level display name overrides the context key"


# --------------------------------------------------------------------------
# Triggers and permissions
# --------------------------------------------------------------------------


def test_gate_does_not_listen_to_issue_comment():
    """A comment event would make this gate cancel itself.

    `issue_comment` runs the workflow from the default branch, so its check run is
    bound to the default-branch SHA and can never satisfy a PR's required check.
    And because the concurrency group is keyed on the PR number, a
    comment-triggered run lands in the same lane and CANCELS the real
    PR-associated run — including the run that posted the gate's own `@codex
    review` anchor comment. The gate posts no comment it later trusts, and accepts no override command.
    """
    assert "issue_comment" not in ON
    assert set(ON.keys()) == {"pull_request", "pull_request_review"}


def test_concurrency_key_cannot_see_a_comment_event():
    """Keying on `pull_request.number` alone is only safe without issue_comment."""
    group = DOC["concurrency"]["group"]
    assert "github.event.issue.number" not in group
    assert "github.event.pull_request.number" in group


def test_late_review_reopens_the_gate():
    """A review landing after the window expired must re-run the check."""
    assert "pull_request_review" in ON
    assert ON["pull_request_review"]["types"] == ["submitted"]


def test_permissions_cover_every_write_the_script_makes():
    """A `permissions:` block zeroes every scope it omits.

    The gate reads the issue-side comments and reactions endpoints, and posts its
    review-request anchor as a PR comment. It writes no labels, so it needs no
    `issues: write` — a narrower token is one fewer thing a compromised workflow
    could do.
    """
    perms = JOB["permissions"]
    # `issues: read` suffices now that the gate writes no labels.
    assert perms["issues"] == "read"
    assert perms["pull-requests"] == "write"
    assert perms["contents"] == "read"


def test_concurrency_cancels_superseded_runs():
    assert DOC["concurrency"]["cancel-in-progress"] is True


# --------------------------------------------------------------------------
# The safety property
# --------------------------------------------------------------------------


def test_gating_step_has_no_if_condition():
    """A skipped or neutral required check SATISFIES branch protection.

    So the gating step must always run and always decide. An `if:` here would
    turn every skip into a merge permit — the precise failure this file exists
    to prevent.
    """
    steps = JOB["steps"]
    assert len(steps) == 1, "a second step invites an `if:`-guarded gating step"
    assert "if" not in steps[0]


def test_exactly_two_deliberate_exit_zero_paths():
    """Every exit 0 must be one of the three documented bypasses.

    Enumerated so that adding a fourth is a deliberate, reviewed act rather than
    an accident: the fast-merge topic, a clean head-bound signal with no formal
    review, and a head-bound review carrying no P1 marker.
    """
    assert len(re.findall(r"^\s*exit 0\s*$", SCRIPT, re.M)) == 2


def test_every_verdict_read_goes_through_the_failing_api_wrapper():
    """A swallowed API error must not read as a clean review.

    `gh api ... || true` on a 403, a 502, or a jq compile error yields an EMPTY
    result, and an empty finding list is indistinguishable from "no findings" —
    so an outage would have opened the gate. The `api()` wrapper exits 1
    (UNKNOWN) instead.
    """
    assert "api() {" in SCRIPT
    wrapper = SCRIPT[SCRIPT.index("api() {") : SCRIPT.index("count_matches()")]
    assert "exit 1" in wrapper
    assert "UNKNOWN" in wrapper

    # No verdict-bearing read may use the swallowing form. Strip comments first:
    # the wrapper's own docstring names the anti-pattern it replaces.
    code = "\n".join(
        line for line in SCRIPT.splitlines() if not line.lstrip().startswith("#")
    )
    # One swallowing call is permitted and named here: the PAT identity probe
    # (`gh api user`). It feeds no verdict — an empty result means the token is
    # unusable, and the very next branch fails closed with an explicit error
    # rather than falling back to the bot identity. Anything else swallowing a
    # failure is the bug this test exists for.
    IDENTITY_PROBE = "gh api user"
    forbidden = [
        m for m in re.findall(r"gh api[^\n]*\|\| true", code)
        if not m.startswith(IDENTITY_PROBE)
    ]
    assert not forbidden, "verdict read swallows failures:\n" + "\n".join(forbidden)


def test_the_two_reads_that_decide_blockers_fail_hard():
    """The verdict list and the review-body scan specifically.

    An API error in either is indistinguishable from "no findings", which is the
    one confusion that can open the gate. The reaction lookups are deliberately
    left swallowing, because they produce a CLEAN signal and a swallowed failure
    there yields "no verdict" — the fail-closed direction.
    """
    def code_after(marker: str, lines: int) -> str:
        """The next `lines` lines of CODE after a marker, comments stripped.

        Scoped by lines rather than characters: a generous character window
        spills into the following comment, which legitimately names `gh api`
        while explaining the pagination gotcha.
        """
        tail = SCRIPT[SCRIPT.index(marker) :].splitlines()
        code = [ln for ln in tail if not ln.lstrip().startswith("#")]
        return "\n".join(code[:lines])

    review_ids = code_after("review_ids_for_head() {", 4)
    assert 'api "repos/${REPO}/pulls/${PR}/reviews"' in review_ids
    assert "gh api" not in review_ids

    # The body scan sits inside the per-review loop, so assert on the line that
    # issues it rather than on a window after it.
    body_lines = [
        ln
        for ln in SCRIPT.splitlines()
        if "reviews/${RID}" in ln and not ln.lstrip().startswith("#")
    ]
    assert body_lines, "the review-body scan disappeared"
    for line in body_lines:
        assert "$(api " in line, line
        assert "gh api" not in line, line

    # And the deliberate exception is documented rather than accidental.
    assert "Failing toward blocking is the" in SCRIPT



def test_no_exit_zero_follows_a_blocker_or_unknown_verdict():
    """Walk the script and assert the terminal decision of each branch.

    Every error annotation the script can emit must be followed by `exit 1`,
    never `exit 0`. This is what a counter, budget, or timeout must never be
    able to override.
    """
    lines = SCRIPT.splitlines()
    for idx, line in enumerate(lines):
        if "::error::" not in line:
            continue
        tail = "\n".join(lines[idx : idx + 6])
        assert re.search(r"^\s*exit 1\s*$", tail, re.M), (
            f"::error:: at script line {idx + 1} is not followed by `exit 1`:\n{tail}"
        )



def test_missing_verdict_holds_the_merge():
    """Absence of a verdict is not approval."""
    assert "No Codex verdict for" in SCRIPT
    assert "UNKNOWN" in SCRIPT
    # The old template passed here; make sure the phrase that did it is gone.
    assert "advisory fallback" not in SCRIPT


def test_debounce_waits_and_never_skips():
    """A superseded run must exit 1, because a skip would permit the merge.

    It also must re-read the head after sleeping: deciding on a stale SHA is
    what lets an unreviewed head satisfy the gate.
    """
    assert "DEBOUNCE_SECONDS" in JOB["env"]
    debounce = SCRIPT[SCRIPT.index("Debouncing") :]
    assert "HEAD_NOW=" in debounce, "the head must be re-read after the quiet interval"
    head_moved = debounce[debounce.index('"${HEAD_NOW}" != "${HEAD_SHA}"') :]
    assert re.search(r"^\s*exit 1\s*$", head_moved[:600], re.M)


# --------------------------------------------------------------------------
# Bound counting
# --------------------------------------------------------------------------




# --------------------------------------------------------------------------
# Override authority
# --------------------------------------------------------------------------





# --------------------------------------------------------------------------
# Review steering
# --------------------------------------------------------------------------


def test_rereview_request_asks_for_one_inventory():
    """The re-review request must carry the same instruction as the first one.

    A bare `@codex review` yields whatever the reviewer happens to surface —
    typically one or two findings — and each additional pass costs a full
    review generation.
    """
    assert "comprehensive inventory" in SCRIPT
    assert "root invariant" in SCRIPT
    assert "sibling consumer" in SCRIPT


def test_first_and_rereview_requests_agree():
    """runner.py posts the first request; the gate posts the rest."""
    runner = (REPO_ROOT / "runner.py").read_text(encoding="utf-8")
    assert "CODEX_INVENTORY_REQUEST" in runner
    for phrase in ("comprehensive inventory", "root invariant", "sibling consumer"):
        assert phrase in runner, phrase
    # The bare request is gone from the PR-creation path.
    assert '"--body", "@codex review",' not in runner


def test_first_request_stays_guarded_to_new_prs():
    """Retries must not stack duplicate review requests on one PR.

    Each duplicate request can draw its own review, and every review of the head
    is a verdict the gate must reconcile — so a retry loop that re-posts would
    manufacture review generations the author never caused.
    """
    runner = (REPO_ROOT / "runner.py").read_text(encoding="utf-8")
    call_at = runner.index("CODEX_INVENTORY_REQUEST,")
    guard = runner.rindex("if pr_newly_created and pr_number is not None:", 0, call_at)
    # The guard must be the immediately enclosing condition, not one far above.
    assert 0 < call_at - guard < 400, "the inventory request is no longer guarded"












def test_both_verdict_patterns_cover_p0_and_p1():
    """P0 is the severest class and was the one that could merge.

    Codex marks severity with its own badge and does not necessarily add
    `[BLOCKER]`, so a P1-only pattern read a P0-bearing review as marker-free and
    exited 0. Two patterns must stay in lockstep: `PATTERN` for grep, and
    `JQ_PATTERN` for the jq `test()` that decides whether a "Reviewed commit"
    summary comment counts as a CLEAN signal. Missing P0 in the latter accepts a
    P0-bearing summary as a green verdict — the same hole by a different route,
    and one the behavioral stub cannot model because it does not implement jq.
    """
    grep_pat = re.search(r"^\s*PATTERN='([^']+)'", SCRIPT, re.M)
    jq_pat = re.search(r"^\s*JQ_PATTERN='([^']+)'", SCRIPT, re.M)
    assert grep_pat and jq_pat, "verdict patterns not found"

    for name, pat in (("PATTERN", grep_pat.group(1)), ("JQ_PATTERN", jq_pat.group(1))):
        assert "P[01]" in pat or ("P0" in pat and "P1" in pat), f"{name} omits P0: {pat}"
        assert "BLOCKER" in pat, f"{name} omits the [BLOCKER] tag: {pat}"

    # The jq form is the grep form with its brackets double-escaped, nothing else.
    assert jq_pat.group(1).replace("\\\\[", "\\[").replace("\\\\]", "\\]") == grep_pat.group(1), (
        "the two patterns have drifted apart"
    )


def test_there_is_no_repo_wide_opt_out():
    """A topic-based bypass is indefinite, unattributed, and invisible on the PR.

    It disabled the only hard gate for every pull request in a repository, with
    nothing on any PR to say so. An opt-out worth having needs an authenticated
    identity and a named head — controller work.
    """
    # The header still names it, explaining why it was removed. What must be
    # gone is any CODE that acts on it.
    code = "\n".join(
        ln for ln in RAW.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "fast-merge" not in code


def test_review_requests_use_a_codex_connected_identity():
    """Codex serves requests PER REQUESTING IDENTITY.

    Every GITHUB_TOKEN comment is authored by `github-actions[bot]`, an identity
    with no Codex account: Codex answers "create a Codex account and connect to
    github" within seconds and never reviews. On abrolia that silently blocked
    four PRs for six days across thirteen requests, presenting as a flaky timeout
    rather than a rejected request.
    """
    assert "CODEX_REQUEST_TOKEN" in SCRIPT
    # The anchor must be posted AS that identity...
    assert 'GH_TOKEN="${REQUEST_TOKEN:-${GH_TOKEN}}"' in SCRIPT
    # ...and the ownership check must move with it, or anchor_comment_id() would
    # never find the comment it just posted.
    assert 'ANCHOR_AUTHOR="${REQUEST_LOGIN}"' in SCRIPT


def test_a_broken_request_token_fails_closed():
    """A rotated or expired PAT must not fall back to the bot identity.

    Falling back reproduces the six-day silent stall while looking configured.
    """
    idx = SCRIPT.index("REQUEST_LOGIN=")
    window = SCRIPT[idx : idx + 900]
    assert "::error::" in window
    assert "exit 1" in window
