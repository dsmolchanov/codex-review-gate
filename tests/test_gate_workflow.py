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
    REPO_ROOT / ".github" / "workflows" / "codex-review-window.yml"
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
    # An allowlist, not an exact set: `workflow_call` is a legitimate addition
    # (consumers call this workflow rather than copying it), and an exact-set
    # assertion would have failed on it while catching nothing dangerous. What
    # must stay impossible is a comment- or schedule-driven trigger.
    assert set(ON.keys()) <= {"pull_request", "pull_request_review", "workflow_call"}


def test_the_callee_declares_no_concurrency_group():
    """The CALLER owns the group. A callee sharing it cancels its own caller.

    A called workflow declaring the same `concurrency.group` as the stub that
    calls it enters a group the caller already holds; with
    `cancel-in-progress: true` the callee then cancels the caller. The run ends
    `failure` with ZERO jobs and no annotation, which presents as an invalid
    workflow file — and `actionlint` reports both files clean. It cost four
    probe runs to find. Serialization belongs to the caller alone.
    """
    assert "concurrency" not in DOC


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


def test_the_gate_is_callable():
    """Consumers install a stub that calls this workflow; they do not copy it.

    Distributing the body put one file in front of eight independent reviewers
    and produced 21 blocking findings, no two repositories agreeing. Losing
    `workflow_call` would silently push everyone back to copying.
    """
    assert "workflow_call" in ON
    assert set(ON["workflow_call"]["secrets"]) == {"CODEX_REQUEST_TOKEN", "SLACK_WEBHOOK"}


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
    # The waker probe is the second permitted swallow: it selects a WAIT LENGTH,
    # not a verdict. On any failure DEFAULT_BRANCH comes back empty, the guard
    # reads that as "no waker", and the gate takes the LONG window — the
    # pre-waker behaviour that was merely expensive, never unsafe. Routing it
    # through api() would block every merge whenever this one informational
    # read hiccuped.
    WAKER_PROBE = 'gh api "repos/${REPO}" --jq \'.default_branch\''
    forbidden = [
        m for m in re.findall(r"gh api[^\n]*\|\| true", code)
        if not m.startswith(IDENTITY_PROBE) and not m.startswith(WAKER_PROBE)
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


def test_round_state_derives_from_completed_verdicts_not_anchors():
    """An anchor proves a request was posted, not that review completed.

    The gate posts its anchor BEFORE waiting, and a new push cancels the
    pending run — so an anchored head can be entirely unreviewed. Deriving the
    delta base or the round count from anchors would let base..old-head merge
    unreviewed while the next round looks only at old-head..new-head. The
    round-state block may therefore read only CODEX_BOT-authored artifacts:
    reviews (commit_id) and clean-summary comments (full SHA in the body).
    """
    idx = SCRIPT.index("VERDICT_LINES=$(")
    block = SCRIPT[idx : SCRIPT.index(")", SCRIPT.index("capture"))]
    assert 'api "repos/${REPO}/pulls/${PR}/reviews"' in block
    assert 'api "repos/${REPO}/issues/${PR}/comments"' in block
    assert "CODEX_BOT" in block
    # The anchor marker and the anchor author must play no part.
    assert "ANCHOR" not in block
    assert "codex-review-window head" not in block


def test_round_state_reads_fail_closed():
    """The round-state reads select the request text AND the blocking pattern.

    A swallowed failure here would read as "round 1" — a full pattern, which is
    the strict direction, but also a fresh full-review request that resets the
    delta chain. More importantly the same read shape feeds ACTIVE_PATTERN, so
    it must go through api() like every other verdict-bearing read.
    """
    idx = SCRIPT.index("VERDICT_LINES=$(")
    block = SCRIPT[idx : idx + 800]
    assert "gh api" not in block, "round-state read bypasses the api() wrapper"
    assert block.count("api \"repos/") == 2


def test_degraded_rounds_swap_only_the_marker_scan_pattern():
    """ACTIVE_PATTERN defaults to the full pattern and narrows only when
    DEGRADED, and only the count_matches scans consume it.

    The clean-signal jq must keep the FULL pattern in every round: a summary
    claiming "no major issues" while carrying any badge is contradictory, and
    the blocking reading is the safe one.
    """
    assert re.search(r"^\s*ACTIVE_PATTERN=\"\$\{PATTERN\}\"", SCRIPT, re.M)
    # Reassigned only under the DEGRADED check.
    reassign = [
        m.start() for m in re.finditer(r'ACTIVE_PATTERN="\$\{P0_PATTERN\}"', SCRIPT)
    ]
    assert len(reassign) == 1
    guard = SCRIPT[: reassign[0]].rsplit("if ", 1)[1]
    assert "DEGRADED" in guard
    # Both marker scans consume ACTIVE_PATTERN; nothing else does.
    assert SCRIPT.count('count_matches "${ACTIVE_PATTERN}"') == 2
    assert 'count_matches "${PATTERN}"' not in SCRIPT
    # The clean-comment filter still tests the full jq pattern.
    clean = SCRIPT[SCRIPT.index("codex_clean_comment_for_head() {") :][:400]
    assert "JQ_PATTERN" in clean


def test_the_degraded_pattern_is_p0_only():
    """[BLOCKER] is the P1 tag in this fleet's review policy.

    Including it in the degraded pattern would make the degrade a no-op for
    every prose-tagged P1; including P[01] would make it a no-op outright.
    """
    m = re.search(r"^\s*P0_PATTERN='([^']+)'", SCRIPT, re.M)
    assert m, "P0_PATTERN not found"
    pat = m.group(1)
    assert "badge/P0" in pat
    assert "P0 Badge" in pat
    assert "BLOCKER" not in pat
    assert "P[01]" not in pat
    assert "P1" not in pat


def test_round_one_request_text_is_unchanged():
    """Round 1 must stay byte-compatible with runner.py's first request.

    dev-agent's runner posts CODEX_INVENTORY_REQUEST when it creates a PR; the
    gate posts the same text on round 1. The two are kept in sync deliberately,
    and this sentence is the sync contract.
    """
    assert "comprehensive inventory" in SCRIPT
    assert (
        "Completeness on this pass matters more than brevity" in SCRIPT
    )


def test_followup_request_reemits_open_findings():
    """A delta review must not let an earlier unresolved P1 vanish.

    The gate scans only reviews bound to the CURRENT head, so a P1 on head A
    followed by an unrelated clean commit B would produce a clean delta verdict
    and merge with the P1 still open — unless every follow-up round requires
    still-open findings to be re-emitted with their badges, where the scan sees
    them again.
    """
    assert "fixed/still-open status" in SCRIPT
    assert "RE-EMIT each still-open P0/P1 with its severity badge" in SCRIPT
    # The range is a printf %s..%s filled from PREV_HEAD and HEAD_SHA.
    assert "commit range %s..%s" in SCRIPT
    assert '"${PREV_HEAD}" "${HEAD_SHA}"' in SCRIPT
    assert "force-push" in SCRIPT


def test_degraded_request_is_delta_and_keeps_p1_badges():
    """Capped rounds narrow the review, without hiding P1s from Auto-fix.

    Auto-fix selects findings by badge and [BLOCKER] tag. A degraded request
    that asked for unbadged P1s would remove them from the batch fix as well as
    from the gate — so the badges stay, and only the gate's scan pattern
    narrows.
    """
    assert "exceeds the full-review budget" in SCRIPT
    assert "Non-blocking notes" in SCRIPT
    assert "KEEPING their severity badges" in SCRIPT


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
    github" within seconds and never reviews. In one repository that silently blocked
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


# --------------------------------------------------------------------------
# Runner occupancy
#
# A gate that sleeps on a rented runner is billed for the sleeping. The 900s
# window cost 424 billed minutes across four repositories in one day, 263 of
# them in runs `cancel-in-progress` killed mid-sleep. The window is now short
# and the wait is event-driven; these tests hold that shape in place, because
# the tempting "fix" for any future missed verdict is to widen the window again.
# --------------------------------------------------------------------------


def test_the_short_window_is_earned_by_a_deployed_waker():
    """Re-entry, not a longer sleep, is how a slow verdict is caught — but only
    where re-entry can actually happen.

    An `issue_comment` workflow runs only from the DEFAULT branch, so on the
    pull request that installs the waker (and in any repository that has not
    merged it) a comment-shaped verdict cannot re-enter the gate. The gate
    therefore probes for the waker on the default branch and takes the short
    window only when it is deployed; otherwise it keeps the long pre-waker
    window, which was merely expensive, never unsafe.

    Widening the SHORT window back out is the obvious-looking remedy for a
    verdict that arrived late, and it is the wrong one: the waker re-runs the
    gate when a Codex summary comment lands, and `pull_request_review` re-runs
    it when a formal review lands. Both re-enter for free. Seconds spent in-run
    are paid on every push whether or not they are needed.
    """
    windows = re.findall(r'WINDOW="\$\{VERDICT_WINDOW_SECONDS:-(\d+)\}"', SCRIPT)
    assert len(windows) == 2, f"expected a deployed and a fallback window, got {windows}"
    short, long_ = int(windows[0]), int(windows[1])
    assert 0 < short <= 300, (
        f"the deployed-waker window is {short}s. A long in-run wait is billed "
        "runner time; catch late verdicts by re-entry."
    )
    # Not zero either. Exiting instantly would fail the check on every push and
    # flap the PR red before any re-entry could answer, and the loop's
    # check-at-least-once shape exists precisely because a zero window once made
    # the gate report "Codex never answered" without asking.
    assert long_ >= 600, (
        f"the no-waker fallback window is {long_}s; without re-entry the window "
        "must cover Codex's real turnaround, as it always did"
    )
    # The short window must be the guarded branch, not the unconditional one.
    guard = SCRIPT[SCRIPT.index('if [ "${WAKER_DEPLOYED}" -eq 1 ]') :]
    assert guard.index(f":-{short}") < guard.index(f":-{long_}")
    # The probe itself must fail toward the long window: a gh error selecting a
    # wait length must not hold the merge, so it must NOT use the fail-hard
    # api() wrapper.
    probe_start = SCRIPT.index("WAKER_PATH=")
    probe = SCRIPT[probe_start : SCRIPT.index("DEADLINE=$((SECONDS + WINDOW))")]
    assert not re.search(r'(?<!gh )api "repos/\$\{REPO\}', probe), (
        "the waker probe uses the fail-hard wrapper; an outage would block the "
        "merge over a wait-length decision"
    )


def test_a_rerun_does_not_pay_the_debounce():
    """The debounce exists to collapse a push burst; a re-run is not one.

    `test_rerun_skips_the_debounce` in the behavioural suite asserts the elapsed
    time. This asserts the wiring that makes it possible, so a dropped `env:`
    entry fails here with a clear cause rather than as a slow test.
    """
    assert "RUN_ATTEMPT" in JOB["env"], "the job cannot tell a re-run from a first run"
    assert JOB["env"]["RUN_ATTEMPT"] == "${{ github.run_attempt }}"
    # The attempt guard must be the branch that GUARDS the debounce, not merely
    # present somewhere in the file: an `if` that ran alongside the sleep rather
    # than instead of it would pass a containment check and sleep anyway.
    guard = re.search(
        r'if \[ "\$\{RUN_ATTEMPT:-1\}" -gt 1 \]; then\n(.*?\n)\s*elif .*?ACTION.*?synchronize',
        SCRIPT,
        re.S,
    )
    assert guard, "the debounce is not guarded by an attempt check on the same if/elif chain"
    assert "sleep" not in guard.group(1), "the re-run branch sleeps"


def test_the_no_verdict_error_does_not_prescribe_a_reflex_rerun():
    """A red gate is usually Codex still working, and re-entry is automatic.

    Telling an operator to re-run whenever the gate is red trains the one habit
    that defeats it: re-running a genuine BLOCK until something flakes green.
    The message must say the wait is automatic, and name the single case that
    genuinely needs a hand — a 👍 reaction, which fires no event at all.
    """
    m = re.search(r"No Codex verdict for \$\{HEAD_SHA\} within \$\{WINDOW\}s —.*", SCRIPT)
    assert m, "the no-verdict error was not found"
    msg = m.group(0)
    assert "automatically" in msg
    assert "reaction" in msg
    assert "wait rather than pushing a commit" in msg
