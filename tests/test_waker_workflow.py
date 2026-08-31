"""Structural tests for the codex-verdict-waker.

The waker is what makes the gate's short verdict window safe: a Codex verdict
delivered as a COMMENT fires `issue_comment`, which the gate deliberately cannot
listen to, and the waker turns that event into a re-run of the gate's PR-bound
run. If the waker silently stops working, clean pull requests stay red until a
human re-runs the gate by hand — annoying, visible, and fail-safe. If the waker
ever grows the power to DECIDE anything, that is the defect these tests exist to
catch early: it must only ever ask the gate to look again.
"""
from __future__ import annotations

import pathlib
import re

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WAKER = REPO_ROOT / ".github" / "workflows" / "codex-verdict-waker.yml"
RAW = WAKER.read_text(encoding="utf-8")
DOC = yaml.safe_load(RAW)
ON = DOC[True]
JOB = DOC["jobs"]["wake"]
SCRIPT = JOB["steps"][0]["run"]


def test_waker_is_call_only():
    """The trigger lives in the consumer stub; the body lives here, once.

    Declaring `issue_comment` here too would run the waker in THIS repository
    for its own comments — harmless but noisy — and would tempt a consumer to
    copy the body instead of calling it, which is how the gate ended up reviewed
    eight times before the host/stub split.
    """
    assert set(ON.keys()) == {"workflow_call"}


def test_the_callee_declares_no_concurrency_group():
    """Same trap as the gate: a callee sharing the caller's group cancels it."""
    assert "concurrency" not in DOC


def test_waker_wakes_for_a_codex_review_too():
    """A formal review must wake it, not only a comment.

    The gate listens to `pull_request_review` itself, so re-entry looked
    covered — but its fresh run is a NEW check run and the earlier run that
    failed for want of a verdict stays on the commit. GitHub's rollup counts
    that stale failure and the pull request reports BLOCKED with a green run of
    the same context beside it. Clearing it is this workflow's job.
    """
    guard = JOB["if"]
    assert "pull_request_review" in guard
    assert "github.event.review.user.login" in guard
    # A review carrying findings is exactly the case that must be cleared, so
    # the review path must NOT be narrowed by a body test the way the comment
    # path is.
    review_clause = guard[guard.index("pull_request_review") :]
    assert "contains(" not in review_clause


def test_waker_resolves_the_pr_number_from_either_event():
    """`issue_comment` carries the PR as an issue, `pull_request_review` as a
    pull request. Reading only one leaves the other waking on an empty number,
    which reads every gate run in the repository."""
    pr = JOB["env"]["PR"]
    assert "github.event.issue.number" in pr
    assert "github.event.pull_request.number" in pr


def test_waker_reruns_every_stale_run_not_just_the_newest():
    """A newer SUCCESS does not retire an older failure.

    Both check runs stay on the commit and the rollup counts the red one, so
    selecting `.[0]` of the non-green set left the pull request BLOCKED with a
    green run beside the red — cleared by hand four times before this loop
    existed.
    """
    assert "for TARGET in ${TARGETS}" in SCRIPT, "the waker re-runs a single run"
    # The selection must not collapse the set to one element.
    assert ".[0].id" not in SCRIPT
    assert "| .[].id" in SCRIPT


def test_waker_wakes_only_for_codex_verdict_comments():
    """Anyone can write a comment; only Codex's own may spend a runner.

    The guard grants no authority — the gate re-reads every verdict itself and
    trusts only CODEX_BOT-authored artifacts — but without it every PR comment
    in every consumer repository costs a runner spin-up, which is the bill this
    workflow exists to shrink.
    """
    guard = JOB["if"]
    assert "github.event.issue.pull_request != null" in guard
    assert "chatgpt-codex-connector[bot]" in guard
    assert "Codex Review" in guard


def test_waker_decides_nothing():
    """The waker may re-run the gate; it may never stand in for it.

    A waker that posts a status, approves, comments, or exits nonzero into a
    required context would become a second gate with none of the gate's
    fail-closed machinery. Its GITHUB_TOKEN write surface must stay exactly
    `actions: write`, and its script must contain no POST but the re-run.
    """
    perms = JOB["permissions"]
    writes = {k for k, v in perms.items() if v == "write"}
    assert writes == {"actions"}, f"unexpected write scopes: {writes}"
    posts = re.findall(r"--method POST[^\n]*", SCRIPT)
    # Exactly one POST SITE — inside the loop over stale runs — and it is the
    # re-run. More than one site would mean the waker learned to write
    # something else.
    assert len(posts) == 1 and "/rerun" in posts[0], posts


def test_waker_targets_the_current_head_only():
    """Re-running a stale run would green-check a commit nobody is merging."""
    assert "head_sha=${HEAD_SHA}" in SCRIPT
    # And only completed, non-green runs: an in-progress run is inside its own
    # window and re-running it is a 403; a green run needs nothing.
    assert '.status == "completed"' in SCRIPT
    assert '.conclusion != "success"' in SCRIPT


def test_waker_leaves_drafts_and_closed_prs_alone():
    """The gate skips drafts, so waking one would re-run nothing forever."""
    assert re.search(r'"\$\{STATE\}" != "open"', SCRIPT)
    assert re.search(r'"\$\{DRAFT\}" = "true"', SCRIPT)


def test_reruns_are_serialized_against_the_gate_concurrency_group():
    """Back-to-back re-runs would cancel each other.

    Every gate run shares the consumer stub's concurrency group, which sets
    `cancel-in-progress: true`. Firing the re-runs in a tight loop therefore
    has each attempt cancel the one before it — and a cancelled run is
    non-green, so the loop would manufacture the very stale artifact it exists
    to clear. Each re-run must wait for its predecessor to complete.
    """
    # From the budget declaration (just above the loop) to the end.
    loop = SCRIPT[SCRIPT.index("WAKER_RERUN_BUDGET") :]
    assert "SERIAL_DEADLINE" in loop, "re-runs are not serialized"
    assert 'RERUN_STATE' in loop and '"completed"' in loop, (
        "the loop does not wait for a re-run to finish before starting the next"
    )
    # And the wait must be skipped after the last target, so the ordinary
    # single-stale-run case pays nothing.
    assert 'REMAINING' in loop
    # Bounded on both axes: number of re-runs and time per wait.
    assert "WAKER_RERUN_BUDGET" in loop
    assert "WAKER_RECHECK_SECONDS" in loop


def test_waker_cannot_hang_a_runner():
    """A hung waker re-creates the very cost this design removes.

    The one wait it is allowed is the bounded re-check on an in-flight gate run,
    capped by WAKER_RECHECK_SECONDS at 240s — sized to the gate's own maximum
    lifetime (window 120s + grace 90s + settle 15s), because waiting any longer
    means the thing being waited on is not the gate. The job timeout leaves
    headroom over that cap and nothing more.
    """
    # Raised for the serialized re-runs: WAKER_RERUN_BUDGET (3) waits of
    # WAKER_RECHECK_SECONDS (240s) is 12 minutes worst case, and every wait is
    # deadline-capped so the job cannot outlive the arithmetic.
    assert JOB["timeout-minutes"] <= 15
    m = re.search(r"WAKER_RECHECK_SECONDS:-(\d+)", SCRIPT)
    assert m, "the in-flight re-check lost its bound"
    assert int(m.group(1)) <= 240
