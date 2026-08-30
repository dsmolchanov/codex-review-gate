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


def test_waker_cannot_hang_a_runner():
    """A hung waker re-creates the very cost this design removes.

    The one wait it is allowed is the bounded re-check on an in-flight gate run,
    capped by WAKER_RECHECK_SECONDS at 240s — sized to the gate's own maximum
    lifetime (window 120s + grace 90s + settle 15s), because waiting any longer
    means the thing being waited on is not the gate. The job timeout leaves
    headroom over that cap and nothing more.
    """
    assert JOB["timeout-minutes"] <= 6
    m = re.search(r"WAKER_RECHECK_SECONDS:-(\d+)", SCRIPT)
    assert m, "the in-flight re-check lost its bound"
    assert int(m.group(1)) <= 240
