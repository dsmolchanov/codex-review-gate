"""BEHAVIORAL tests: execute the gate's shell with a stubbed GitHub API.

The other gate tests are structural — they grep the YAML. Structural tests cannot
catch the class of defect that reached this branch twice:

* `findings=-n` in a waiver marker was passed to `grep -vxF "${WID}"`, where `-n`
  parses as an OPTION rather than a pattern. grep exited 2, a `|| true` masked it,
  the open-id list came back EMPTY, and an empty open set read as "every blocker
  waived" — a green gate on unreviewed blockers.
* With a zero verdict window, `while [ SECONDS -lt DEADLINE ]` never ran its
  body, so the gate reported "Codex never answered" without having asked.

Both were invisible to thirty greps and obvious the moment the script ran. The
waiver mechanism has since been cut entirely (see the gate's header), which is
the more durable fix — but the lesson stands: what matters is the exit code,
because exit 0 means "this pull request may merge".

So these tests run the real `run:` block under `bash` with `gh` replaced by a
fixture-driven stub, and assert the process exit status.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import textwrap

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
GATE = (
    REPO_ROOT / ".github" / "workflows" / "codex-review-window.yml"
)
SCRIPT = yaml.safe_load(GATE.read_text(encoding="utf-8"))["jobs"]["codex-review-window"][
    "steps"
][0]["run"]

HEAD = "a" * 40
BOT = "chatgpt-codex-connector[bot]"
ANCHOR_AUTHOR = "github-actions[bot]"
# What a Codex inline finding actually looks like; the gate greps for the badge
# or the [BLOCKER] tag.
P1_BODY = "**![P1 Badge](https://img.shields.io/badge/P1-orange) [BLOCKER] Fix the thing**"
# Codex marks severity with a badge and does NOT necessarily add `[BLOCKER]`, so
# the badge alone has to be enough to block. A P0 marked only by its badge is the
# most severe case, and it is the one a P1-only verdict pattern let through.
P0_BODY_NO_BLOCKER = "**![P0 Badge](https://img.shields.io/badge/P0-red) Data loss on retry**"
P1_BODY_NO_BLOCKER = "**![P1 Badge](https://img.shields.io/badge/P1-orange) Unbounded request body**"

GH_STUB = r'''#!/usr/bin/env python3
"""Fixture-driven `gh` stub.

Routes are keyed by URL substring; each value maps a substring of the `--jq`
filter to the output that filter would produce. jq is not reimplemented — the
fixture supplies the rendered result, which is what the gate consumes anyway.
`"*"` is the fallback for a call with no `--jq` or no matching pattern;
`"__FAIL__"` exits non-zero, simulating a 502.

An unmatched URL exits 0 with no output: the realistic shape for a list endpoint
with no results, and the shape most likely to be mistaken for "clean".
"""
import json, os, sys

fixture = json.load(open(os.environ["GH_FIXTURE"]))
args = sys.argv[1:]
with open(os.environ["GH_CALLS"], "a") as fh:
    fh.write(" ".join(args) + "\n")

if not args or args[0] != "api":
    sys.exit(0)

url = next((a for a in args[1:] if not a.startswith("-")), "")
jq = args[args.index("--jq") + 1] if "--jq" in args else ""


def emit(value):
    if isinstance(value, list):
        # A SEQUENCE: successive calls to the same route get successive entries,
        # the last repeating. This is how a fixture models a review that is
        # published a little after the clean signal that announced it.
        import os.path
        counter = os.environ["GH_CALLS"] + ".seq"
        n = 0
        if os.path.exists(counter):
            n = int(open(counter).read() or 0)
        open(counter, "w").write(str(n + 1))
        value = value[min(n, len(value) - 1)]
    if value == "__FAIL__":
        print("gh: HTTP 502 Bad Gateway", file=sys.stderr)
        sys.exit(1)
    if isinstance(value, str) and value.startswith("__HTTP_ERROR__"):
        # A rejected REST call. Real gh prints the API's error body to STDOUT
        # and exits non-zero — the combination that made a 403 read as a
        # successfully posted comment, because only the exit code says no.
        print(value.split(":", 1)[1])
        sys.exit(1)
    if value:
        print(value)
    sys.exit(0)


# Longest URL key first, so "pulls/7/reviews/555" beats "pulls/7".
# A leading "=" means EXACT url match — for endpoints like the bare repo read
# ("repos/owner/repo"), which as a SUBSTRING would swallow every other route.
for key in sorted(fixture.get("routes", {}), key=len, reverse=True):
    if key.startswith("="):
        if url != key[1:]:
            continue
    elif key not in url:
        continue
    value = fixture["routes"][key]
    if isinstance(value, str):
        emit(value)
    for pattern, out in value.items():
        if pattern == "*":
            continue
        # A leading "=" means EXACT jq match. Substring matching is the default
        # because it keeps fixtures short, but it silently mis-routes when one
        # key is a substring of another filter: ".id" (the anchor POST) also
        # occurs inside the clean-comment filter's "| .id] | .[]", so the stub
        # answered the wrong question and the gate read a verdict that was never
        # given.
        if pattern.startswith("="):
            if jq == pattern[1:]:
                emit(out)
        elif pattern in jq:
            emit(out)
    emit(value.get("*", ""))
sys.exit(0)
'''


def run_gate(
    tmp_path: pathlib.Path,
    fixture: dict,
    action: str = "synchronize",
    grace: str = "0",
    request_token: str = "pat-for-tests",
    debounce: str = "0",
    run_attempt: str = "1",
):
    """Execute the gate's run: block with a stubbed gh; return CompletedProcess."""
    # A unique subdir per call, so one test can run the gate more than once.
    run_dir = tmp_path / f"run{len(list(tmp_path.glob('run*')))}"
    run_dir.mkdir()
    bin_dir = run_dir / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gh"
    stub.write_text(GH_STUB, encoding="utf-8")
    stub.chmod(0o755)

    fixture_path = run_dir / "fixture.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    calls = run_dir / "calls.log"
    calls.touch()

    script = run_dir / "gate.sh"
    script.write_text(textwrap.dedent(SCRIPT), encoding="utf-8")

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GH_FIXTURE": str(fixture_path),
        "GH_CALLS": str(calls),
        "REPO": "owner/repo",
        "PR": "7",
        "CODEX_BOT": BOT,
        "ACTION": action,
        "ANCHOR_AUTHOR": ANCHOR_AUTHOR,
        "SLACK_WEBHOOK": "",
        # A Codex-connected identity. Without it the gate warns that a request
        # posted as github-actions[bot] would be refused — correct behaviour, but
        # it means these cases would exercise the misconfigured path instead of
        # the one under test. The stub answers `gh api user` with a login.
        "CODEX_REQUEST_TOKEN": request_token,
        "RUN_URL": "http://example/run",
        # Zero so the tests do not sleep. Production sets none of these three;
        # the window override exists only because a 15-minute wait is untestable.
        "DEBOUNCE_SECONDS": debounce,
        # A re-run must not pay the debounce again; `test_rerun_skips_the_debounce`
        # sets this to "2" and asserts the gate does not sleep.
        "RUN_ATTEMPT": run_attempt,
        "VERDICT_WINDOW_SECONDS": "0",
        "SETTLE_SECONDS": "0",
        # The grace window defaults to 90s in production, to cover the 59s
        # review-publication lag recorded in the motivating incident. Tests set it explicitly
        # per case: 0 where the wait is irrelevant, non-zero where the point IS
        # that a late review is caught.
        "GRACE_SECONDS": grace,
    }
    return subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=120
    )


def base_fixture() -> dict:
    """Codex reviewed HEAD (review 555) and reported one inline `[BLOCKER]`.

    The canonical "one blocker, must stay red" state.
    """
    return {
        "routes": {
            "pulls/7": {".head.sha": HEAD, "*": json.dumps({"head": {"sha": HEAD}})},
            # PAT identity probe: `gh api user --jq .login`.
            "user": {"*": "codex-requester"},
            "commits/": {"*": "2026-08-17T00:00:00Z"},
            # Match on argv-time text: the workflow writes `\"` to escape the
            # quote for the shell, but by the time gh sees it it is a plain `"`.
            "pulls/7/reviews": {'commit_id == "': "555", "*": ""},
            # The review body carries no marker; the finding is inline.
            "pulls/7/reviews/555": {"*": "Looks mostly fine."},
            # The anchor POST, matched EXACTLY: ".id" as a substring also occurs
            # in the clean-comment filter, which would route that query here.
            "issues/7/comments": {"=.id": "9001", "*": ""},
            "issues/7/reactions": {"*": ""},
            "pulls/7/comments": {"pull_request_review_id": P1_BODY, "*": ""},
        }
    }


def clean_fixture() -> dict:
    """Same, but review 555 reports nothing."""
    fx = base_fixture()
    fx["routes"]["pulls/7/comments"] = {"*": ""}
    return fx


# --------------------------------------------------------------------------
# The one property that matters: when may this PR merge?
# --------------------------------------------------------------------------


def test_open_blocker_is_red(tmp_path):
    result = run_gate(tmp_path, base_fixture())
    assert result.returncode != 0, result.stdout
    assert "BLOCKER" in result.stdout


def test_clean_review_is_green(tmp_path):
    result = run_gate(tmp_path, clean_fixture())
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "green" in result.stdout


def test_review_body_blocker_is_red(tmp_path):
    """A body-only finding has no inline comment to fall back on.

    If the body scan were dropped or its API failure swallowed, this blocker would
    vanish silently — so it gets its own case.
    """
    fx = clean_fixture()
    fx["routes"]["pulls/7/reviews/555"] = {"*": P1_BODY}
    result = run_gate(tmp_path, fx)
    assert result.returncode != 0, result.stdout


def test_no_verdict_is_red(tmp_path):
    """Absence of a verdict is not approval.

    This is the exact path that let three consecutive PRs merge with open P1s.
    """
    fx = base_fixture()
    fx["routes"]["pulls/7/reviews"] = {"*": ""}
    result = run_gate(tmp_path, fx)
    assert result.returncode != 0
    assert "UNKNOWN" in result.stdout


def test_fork_pr_without_a_verdict_is_red(tmp_path):
    """A read-only token cannot request a review, so the verdict stays unknown.

    Fork PRs get a read-only GITHUB_TOKEN, so the anchor comment cannot be
    posted. That must hold the merge and say why, not pass for lack of a signal.
    """
    fx = base_fixture()
    fx["routes"]["pulls/7/reviews"] = {"*": ""}
    fx["routes"]["issues/7/comments"] = {"*": ""}  # anchor POST returns nothing
    result = run_gate(tmp_path, fx)
    assert result.returncode != 0
    assert "read-only" in result.stdout
    assert "UNKNOWN" in result.stdout


def test_verdict_read_failure_is_red_not_clean(tmp_path):
    """An outage must hold the merge, never read as 'no findings'."""
    fx = base_fixture()
    fx["routes"]["pulls/7/reviews"] = "__FAIL__"
    result = run_gate(tmp_path, fx)
    assert result.returncode != 0
    assert "UNKNOWN" in result.stdout or "UNKNOWN" in result.stderr


def test_inline_finding_read_failure_is_red_not_clean(tmp_path):
    """The same, one level down: the blocker enumeration must fail hard too."""
    fx = base_fixture()
    fx["routes"]["pulls/7/comments"] = "__FAIL__"
    result = run_gate(tmp_path, fx)
    assert result.returncode != 0


def test_quota_exhaustion_is_red_and_named(tmp_path):
    """A quota outage blocks every merge behind the gate, so it must say so.

    It once went unnoticed for three days; eleven PRs merged unreviewed.
    """
    fx = base_fixture()
    fx["routes"]["pulls/7/reviews"] = {"*": ""}
    fx["routes"]["issues/7/comments"] = {
        # ") | .body" is unique to the quota scan; "select(.user.login" alone
        # also occurs in the clean-comment filter and mis-routed there.
        ") | .body": "You have reached your Codex usage limits",
        "=.id": "9001",
        "*": "",
    }
    result = run_gate(tmp_path, fx)
    assert result.returncode != 0
    assert "quota" in result.stdout.lower()


def test_a_repo_topic_cannot_bypass_the_gate(tmp_path):
    """The removed opt-out must stay removed, checked by execution.

    `fast-merge` disabled the only hard gate for every PR in a repository,
    indefinitely and with nothing on the PR to show it.
    """
    fx = base_fixture()
    fx["routes"]["topics"] = {"*": "fast-merge"}
    result = run_gate(tmp_path, fx)
    assert result.returncode != 0, (
        f"a repository topic still bypasses the gate:\n{result.stdout}"
    )


def test_unresolvable_head_is_red(tmp_path):
    fx = base_fixture()
    fx["routes"]["pulls/7"] = {"*": ""}
    result = run_gate(tmp_path, fx)
    assert result.returncode != 0


def test_gate_writes_no_labels(tmp_path):
    """The gate holds no lease and applies no label; a human does that.

    Asserted against the actual call log rather than the source, so adding a label
    write anywhere in the script fails here.
    """
    run_gate(tmp_path, base_fixture())
    calls = (tmp_path / "run0" / "calls.log").read_text()
    assert "/labels" not in calls, f"gate wrote a label:\n{calls}"
    assert "-X DELETE" not in calls


# --------------------------------------------------------------------------
# Severity coverage: the badge alone must block, at BOTH severities
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body,label",
    [
        (P0_BODY_NO_BLOCKER, "P0 badge, no [BLOCKER] tag"),
        (P1_BODY_NO_BLOCKER, "P1 badge, no [BLOCKER] tag"),
    ],
)
def test_severity_badge_alone_blocks(tmp_path, body, label):
    """Codex marks severity with a badge and may omit `[BLOCKER]` entirely.

    A P1-only verdict pattern read a P0-bearing review as marker-free and exited
    0 — so the single most severe class of finding was the one that could merge.
    Both severities must block on the badge alone, inline and in the review body.
    """
    fx = clean_fixture()
    fx["routes"]["pulls/7/comments"] = {"pull_request_review_id": body, "*": ""}
    result = run_gate(tmp_path, fx)
    assert result.returncode != 0, f"{label} did not block:\n{result.stdout}"

    fx = clean_fixture()
    fx["routes"]["pulls/7/reviews/555"] = {"*": body}
    result = run_gate(tmp_path, fx)
    assert result.returncode != 0, f"{label} in the review body did not block:\n{result.stdout}"


# --------------------------------------------------------------------------
# Availability: a valid PR must never be permanently unable to get a verdict
# --------------------------------------------------------------------------
#
# Both cases below were reported independently by three consumer repositories
# during the fleet rollout, and both block a valid pull request FOREVER rather
# than merging something bad. The existing suite missed them because it exercised
# only the `synchronize` path with a formal review present — neither the
# clean-summary counting path nor the `submitted` event was covered.


def test_a_single_clean_summary_comment_releases_the_gate(tmp_path):
    """One "Reviewed commit" summary and no formal review must read as clean.

    `api()` captured output with `$(...)`, which strips the trailing newline, and
    emitted it with `printf '%s'`. `codex_clean_comment_for_head` pipes that into
    `wc -l`, so a single unterminated record counted as ZERO. The head then looked
    unverified, waited the full window, and failed the hard gate on every rerun —
    with Codex having answered cleanly the whole time.
    """
    fx = base_fixture()
    fx["routes"]["pulls/7/reviews"] = {"*": ""}  # no formal review at all
    fx["routes"]["issues/7/comments"] = {
        # The gate matches the VERBATIM clean-verdict sentence, not a loose
        # substring: a body claiming no major issues while carrying a P0/P1 badge
        # is contradictory, and it reads that as blocking.
        "Didn't find any major issues": "9001",  # exactly one clean record
        "=.id": "9001",
        "*": "",
    }
    result = run_gate(tmp_path, fx)
    assert result.returncode == 0, (
        f"one clean summary was not counted, so the gate blocked a verified head:"
        f"\n{result.stdout}\n{result.stderr}"
    )


def test_no_clean_summary_still_means_no_verdict(tmp_path):
    """The counterpart: terminating output must not turn zero records into one.

    Printing a bare newline for empty output would make "nothing found" count as
    one clean record — the same bug with the sign flipped, and this one opens the
    gate instead of jamming it.
    """
    fx = base_fixture()
    fx["routes"]["pulls/7/reviews"] = {"*": ""}
    fx["routes"]["issues/7/comments"] = {"=.id": "9001", "*": ""}
    result = run_gate(tmp_path, fx)
    assert result.returncode != 0
    assert "UNKNOWN" in result.stdout


def test_a_review_submitted_event_still_requests_a_verdict(tmp_path):
    """`submitted` runs must be able to ask for a review.

    `pull_request_review: submitted` shares the cancel-in-progress concurrency
    group with `synchronize`. A late review — Codex's for an earlier head, or any
    human's — cancels the in-flight synchronize run during its debounce, and the
    replacement carries ACTION=submitted. While the request dispatcher excluded
    that action, the replacement never posted `@codex review`; Codex does not
    auto-review pushes, so it waited the window and failed, and re-running
    preserved the same action. The PR stayed blocked until an unrelated event
    happened to fire.
    """
    fx = base_fixture()
    fx["routes"]["pulls/7/reviews"] = {"*": ""}  # nothing for this head yet
    fx["routes"]["issues/7/comments"] = {"=.id": "9001", "*": ""}
    run_gate(tmp_path, fx, action="submitted")
    calls = (tmp_path / "run0" / "calls.log").read_text()
    assert "issues/7/comments" in calls and "-f body=" in calls, (
        "a submitted-event run posted no review request, so this head can never "
        f"obtain a verdict:\n{calls}"
    )


def test_the_request_is_not_duplicated_when_a_review_already_exists(tmp_path):
    """Adding `submitted` must not start asking twice.

    The guard fires only when the head has neither an anchor nor a review; with a
    review present, no request may be posted.
    """
    run_gate(tmp_path, base_fixture(), action="submitted")
    calls = (tmp_path / "run0" / "calls.log").read_text()
    assert "-f body=" not in calls, f"redundant review request:\n{calls}"


def test_a_review_published_after_the_clean_signal_still_blocks(tmp_path):
    """The core race, in its last surviving form.

    Codex announces a verdict with a summary comment or a 👍 and PUBLISHES the
    formal review moments later — 59 seconds later in the motivating incident. The gate broke
    out of its wait on the clean signal and rechecked ONCE after 15 seconds, so a
    review arriving at 59s was missed, the gate exited 0, and auto-merge consumed
    the green check before the P1s appeared. That is precisely the race this file
    exists to lose no more, reintroduced by a grace window shorter than the lag it
    cited.

    Here the reviews route answers "nothing" first and a blocking review second,
    which is what the poll must catch.
    """
    fx = base_fixture()
    # FOUR entries, and the count is the point. review_ids_for_head is consulted
    # by the anchor guard, then by the main wait loop, then by the grace check.
    # The old code stopped there — one look, after a fixed sleep — so a review
    # that had not appeared by then was missed. Answering only on the FOURTH call
    # means this passes only if the grace window POLLS.
    fx["routes"]["pulls/7/reviews"] = {'commit_id == "': ["", "", "", "555"], "*": ""}
    fx["routes"]["issues/7/comments"] = {
        "Didn't find any major issues": "9001",
        "=.id": "9001",
        "*": "",
    }
    env_grace = "30"  # long enough to poll more than once; the stub answers at once
    result = run_gate(tmp_path, fx, grace=env_grace)
    assert result.returncode != 0, (
        "a review published after the clean signal was missed, so a P1 could "
        f"merge:\n{result.stdout}"
    )
    assert "grace window" in result.stdout


def test_without_a_request_identity_the_gate_says_so_and_still_blocks(tmp_path):
    """Every other case injects a PAT, which hid the deployment failure.

    A repository that receives the gate without CODEX_REQUEST_TOKEN cannot ask
    for a review at all: Codex refuses github-actions[bot] and does not
    auto-review pushed heads. The gate must say that plainly rather than looking
    like a slow reviewer — and it must still fail closed.
    """
    fx = base_fixture()
    fx["routes"]["pulls/7/reviews"] = {"*": ""}
    result = run_gate(tmp_path, fx, request_token="")
    assert result.returncode != 0
    assert "No CODEX_REQUEST_TOKEN is configured" in result.stdout
    assert "Codex refuses" in result.stdout



def test_a_rejected_review_request_fails_fast_and_names_the_scope(tmp_path):
    """A 403 on the request must not read as a posted comment.

    gh prints the API's error body to stdout and exits non-zero, so the old
    `--jq '.id' 2>/dev/null || true` captured the error JSON as the comment id.
    The gate logged `Posted ... (comment {"message":"Resource not accessible by
    personal access token"...})`, waited out its full window, and then blamed
    Codex connectivity for a token-scope problem. It happened on seven
    repositories at once.
    """
    fx = base_fixture()
    fx["routes"]["pulls/7/reviews"] = {"*": ""}
    fx["routes"]["issues/7/comments"] = {
        "=.id": '__HTTP_ERROR__:{"message":"Resource not accessible by personal '
                'access token","status":"403"}',
        "*": "",
    }
    result = run_gate(tmp_path, fx)

    assert result.returncode == 1, result.stdout
    assert "cannot post the review request" in result.stdout
    assert "Issues: Read and write" in result.stdout
    assert "Posted an inventory-scoped" not in result.stdout
    # The point is not waiting: a rejected request can never yield a verdict.
    assert "UNKNOWN" not in result.stdout, "the gate waited out its window anyway"


def test_a_request_failure_without_an_identity_keeps_the_old_fallback(tmp_path):
    """Fork PRs get no secret, so a failed POST there is expected, not fatal.

    The hard failure above is about a CONFIGURED identity that cannot comment.
    Without one, the gate must keep saying so and stay red for want of a
    verdict, rather than adopting the new message.
    """
    fx = base_fixture()
    fx["routes"]["pulls/7/reviews"] = {"*": ""}
    fx["routes"]["issues/7/comments"] = {
        "=.id": '__HTTP_ERROR__:{"message":"Resource not accessible by '
                'integration","status":"403"}',
        "*": "",
    }
    result = run_gate(tmp_path, fx, request_token="")

    assert result.returncode == 1
    assert "cannot post the review request" not in result.stdout
    assert "No CODEX_REQUEST_TOKEN is configured" in result.stdout
    assert "UNKNOWN" in result.stdout


# --------------------------------------------------------------------------
# Round budget: delta re-review requests and the P0-only degrade
# --------------------------------------------------------------------------
#
# Round state is derived from COMPLETED verdicts only: formal reviews (their
# commit_id) and clean-summary comments (a full SHA in a CODEX_BOT body). The
# stub routes the round-state reads by unique jq-filter substrings:
# `submitted_at` for the reviews read, `capture(` for the comments read. A
# request anchor is neither, which is the point — an anchored-but-unverdicted
# head (a push cancelled the run that would have waited) must not advance the
# round or become a delta base.

OLD1 = "b" * 40
OLD2 = "c" * 40
OLD3 = "d" * 40


def verdict_heads(*shas):
    """Rendered output of the reviews-side round-state read: oldest first."""
    return "\n".join(
        f"2026-08-{i + 1:02d}T00:00:00Z {sha}" for i, sha in enumerate(shas)
    )


def round_fixture(*prior, head_review: bool):
    """N prior verdict-bearing heads; the current head reviewed or not."""
    fx = base_fixture()
    fx["routes"]["pulls/7/reviews"] = {
        "submitted_at": verdict_heads(*prior),
        'commit_id == "': "555" if head_review else "",
        "*": "",
    }
    if not head_review:
        fx["routes"]["pulls/7/comments"] = {"*": ""}
    return fx


def posted_bodies(tmp_path):
    return (tmp_path / "run0" / "calls.log").read_text()


def test_first_round_requests_the_full_inventory(tmp_path):
    """No completed verdict yet: ask for the full inventory, no commit range."""
    fx = round_fixture(head_review=False)
    result = run_gate(tmp_path, fx)
    calls = posted_bodies(tmp_path)
    assert "comprehensive inventory" in calls
    assert f"..{HEAD}" not in calls, f"round 1 asked for a delta:\n{calls}"
    assert "Review round 1:" in result.stdout
    assert result.returncode != 0  # no verdict ever arrives — still red


def test_an_anchored_but_unverdicted_head_is_not_reviewed(tmp_path):
    """The cancellation race: a request was posted, the run died, nothing came.

    The prior head left an anchor comment but no verdict of any kind. Anchors
    are invisible to the round-state reads (they are neither reviews nor
    clean summaries), so the next round must be a FULL round — treating the
    anchored head as reviewed would leave base..old-head unreviewed forever.
    """
    fx = round_fixture(head_review=False)
    # An anchor for the CURRENT head also exists (this run's predecessor posted
    # it before being cancelled): the gate then posts no new request, and the
    # round state must still say round 1.
    fx["routes"]["issues/7/comments"] = {
        "codex-review-window head": "9001",  # anchor_comment_id() finds it
        "capture(": "",                      # no clean-summary verdicts
        "=.id": "9001",
        "*": "",
    }
    result = run_gate(tmp_path, fx)
    assert "Review round 1:" in result.stdout
    assert "Degraded" not in result.stdout
    assert result.returncode != 0
    assert "UNKNOWN" in result.stdout


def test_second_round_requests_only_the_delta(tmp_path):
    """One completed verdict: the request scopes new findings to prev..head,
    with the delta base being the most recently VERDICTED head."""
    fx = round_fixture(OLD1, OLD2, head_review=False)
    result = run_gate(tmp_path, fx)
    calls = posted_bodies(tmp_path)
    assert f"{OLD2}..{HEAD}" in calls, f"delta range missing or wrong base:\n{calls}"
    assert "RE-EMIT each still-open P0/P1" in calls
    assert "Review round 3:" in result.stdout


def test_clean_summary_verdicts_count_toward_the_round(tmp_path):
    """A requested re-review that finds nothing comes back as a comment, not a
    review. It is a completed verdict and must advance the round."""
    fx = round_fixture(head_review=False)
    fx["routes"]["issues/7/comments"] = {
        "capture(": f"2026-08-01T00:00:00Z {OLD1}",
        "=.id": "9001",
        "*": "",
    }
    result = run_gate(tmp_path, fx)
    calls = posted_bodies(tmp_path)
    assert f"{OLD1}..{HEAD}" in calls
    assert "Review round 2:" in result.stdout


def test_p1_blocks_before_the_cap(tmp_path):
    """Two completed rounds: still a full round, a P1 badge still blocks."""
    fx = round_fixture(OLD1, OLD2, head_review=True)
    fx["routes"]["pulls/7/comments"] = {
        "pull_request_review_id": P1_BODY_NO_BLOCKER,
        "*": "",
    }
    result = run_gate(tmp_path, fx)
    assert "Review round 3:" in result.stdout
    assert result.returncode != 0, f"a P1 merged before the cap:\n{result.stdout}"


def test_p1_does_not_block_after_the_cap(tmp_path):
    """Three completed rounds: the budget is spent, P1s stop holding the merge."""
    fx = round_fixture(OLD1, OLD2, OLD3, head_review=True)
    fx["routes"]["pulls/7/comments"] = {
        "pull_request_review_id": P1_BODY_NO_BLOCKER,
        "*": "",
    }
    result = run_gate(tmp_path, fx)
    assert "Degraded round 4:" in result.stdout
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_p1_in_review_body_does_not_block_after_the_cap(tmp_path):
    fx = round_fixture(OLD1, OLD2, OLD3, head_review=True)
    fx["routes"]["pulls/7/comments"] = {"*": ""}
    fx["routes"]["pulls/7/reviews/555"] = {"*": P1_BODY_NO_BLOCKER}
    result = run_gate(tmp_path, fx)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


@pytest.mark.parametrize("where", ["inline", "body"])
def test_p0_still_blocks_after_the_cap(tmp_path, where):
    """The degrade narrows the gate to P0 — it never opens it."""
    fx = round_fixture(OLD1, OLD2, OLD3, head_review=True)
    if where == "inline":
        fx["routes"]["pulls/7/comments"] = {
            "pull_request_review_id": P0_BODY_NO_BLOCKER,
            "*": "",
        }
    else:
        fx["routes"]["pulls/7/comments"] = {"*": ""}
        fx["routes"]["pulls/7/reviews/555"] = {"*": P0_BODY_NO_BLOCKER}
    result = run_gate(tmp_path, fx)
    assert result.returncode != 0, f"a P0 merged on a degraded round:\n{result.stdout}"
    assert "P0" in result.stdout


def test_rerun_on_the_same_head_does_not_advance_the_round(tmp_path):
    """A verdict on the CURRENT head is this round's verdict, not a prior one.

    Re-running the gate on the same head (or the submitted-event replacement
    run) must not push the PR toward the degrade.
    """
    fx = round_fixture(OLD1, OLD2, HEAD, head_review=True)
    fx["routes"]["pulls/7/comments"] = {
        "pull_request_review_id": P1_BODY_NO_BLOCKER,
        "*": "",
    }
    result = run_gate(tmp_path, fx)
    assert "Review round 3:" in result.stdout
    assert result.returncode != 0


def test_verdict_history_read_failure_is_red(tmp_path):
    """The round-state comment read selects the blocking pattern, so an API
    failure there must be UNKNOWN — never 'round 1'."""
    fx = base_fixture()
    fx["routes"]["issues/7/comments"] = "__FAIL__"
    result = run_gate(tmp_path, fx)
    assert result.returncode != 0
    assert "UNKNOWN" in result.stdout + result.stderr


def test_no_verdict_is_still_red_after_the_cap(tmp_path):
    """The budget narrows severity; it never overrides a missing verdict."""
    fx = round_fixture(OLD1, OLD2, OLD3, head_review=False)
    result = run_gate(tmp_path, fx)
    calls = posted_bodies(tmp_path)
    assert "Non-blocking notes" in calls  # the degraded request was posted
    assert f"{OLD3}..{HEAD}" in calls
    assert result.returncode != 0
    assert "UNKNOWN" in result.stdout


# --------------------------------------------------------------------------
# The runner is billed by the second, so a wait that buys nothing is a defect.
#
# These two tests are a PAIR and only mean something together: the first shows
# the debounce still really sleeps, so the second's speed is evidence of the
# skip rather than evidence that the debounce never worked. Asserting on elapsed
# time is the only way to test a `sleep` — a grep cannot tell a live wait from a
# dead one, which is the same reason this file exists at all.
# --------------------------------------------------------------------------


def test_first_attempt_still_debounces(tmp_path):
    """A first synchronize run pays the quiet interval, as it always has."""
    import time

    started = time.monotonic()
    result = run_gate(tmp_path, clean_fixture(), debounce="3", run_attempt="1")
    elapsed = time.monotonic() - started

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert elapsed >= 3, (
        f"the debounce did not sleep ({elapsed:.1f}s) — this test's twin below "
        "proves nothing unless the debounce is real"
    )
    assert "Debouncing" in result.stdout


def test_rerun_skips_the_debounce(tmp_path):
    """A re-run is the waker answering a verdict, not a burst of pushes.

    Debouncing it would spend 90 more seconds of runner time re-reading a head
    the run already read fresh from the API, and would delay exactly the answer
    the waker exists to deliver.
    """
    import time

    started = time.monotonic()
    result = run_gate(tmp_path, clean_fixture(), debounce="3", run_attempt="2")
    elapsed = time.monotonic() - started

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert elapsed < 3, f"a re-run paid the debounce anyway ({elapsed:.1f}s)"
    assert "skipping the debounce" in result.stdout
    assert "Debouncing" not in result.stdout


def test_rerun_still_holds_the_merge_on_a_blocker(tmp_path):
    """Skipping the debounce must not skip the verdict.

    The debounce branch sits directly above the verdict read, so an `elif` that
    swallowed the rest of the pipeline would present as a fast green run — the
    one failure mode of this change that would let a blocker merge.
    """
    result = run_gate(tmp_path, base_fixture(), debounce="3", run_attempt="2")
    assert result.returncode != 0, result.stdout
    assert "BLOCKER" in result.stdout


# --------------------------------------------------------------------------
# The short verdict window is earned by a deployed waker, never assumed.
#
# An `issue_comment` workflow runs only from the DEFAULT branch, so the pull
# request that installs the waker cannot be woken by it. The gate probes for
# the waker where events can actually reach it and keeps the long pre-waker
# window until the probe finds it. All three tests run with the window
# OVERRIDDEN to zero, so what they assert is the gate's CHOICE — the message
# naming which window applies — not the wait itself.
# --------------------------------------------------------------------------


# What a consumer's waker STUB declares, and what the reusable HOST declares.
# The probe has to tell them apart: both are `state: active`, and only the first
# can ever be started by a comment.
STUB_WAKER = "name: codex-verdict-waker\non:\n  issue_comment:\n    types: [created]\n"
HOST_WAKER = (
    "name: codex-verdict-waker\n"
    "# `issue_comment` fires in the CONSUMER repository, so the consumer\n"
    "# installs a stub with the issue_comment trigger that calls this body.\n"
    "on:\n  workflow_call:\n"
)


def waker_deployed_fixture(state: str = "active", file: str = STUB_WAKER) -> dict:
    fx = clean_fixture()
    fx["routes"]["actions/workflows/codex-verdict-waker.yml"] = {"state": state, "*": ""}
    fx["routes"]["contents/.github/workflows/codex-verdict-waker.yml"] = {"*": file}
    return fx


def test_no_waker_keeps_the_long_window(tmp_path):
    """A repo that has not merged the waker must keep the pre-waker window.

    Shortening it there strands a comment-shaped verdict: nothing re-enters the
    gate, and the PR stays red until a human re-runs the check by hand.
    """
    result = run_gate(tmp_path, clean_fixture())
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "cannot re-enter the gate, so the in-run window stays" in result.stdout
    assert "late verdicts re-enter" not in result.stdout


def test_deployed_waker_takes_the_short_window(tmp_path):
    result = run_gate(tmp_path, waker_deployed_fixture())
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "late verdicts re-enter, so the in-run window is" in result.stdout


def test_a_workflow_call_host_is_not_a_waker(tmp_path):
    """`state: active` is necessary and not sufficient.

    In the gate's OWN repository the path resolves to the reusable host, which
    is `workflow_call` only: active, and impossible to start with a comment. The
    probe read that as a deployed waker and handed the one repository with no
    re-entry path the short window, so every clean verdict there needed a manual
    re-run. The file's prose mentions `issue_comment` repeatedly, so the check
    has to read the trigger, not the text.
    """
    result = run_gate(tmp_path, waker_deployed_fixture(file=HOST_WAKER))
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "cannot re-enter the gate, so the in-run window stays" in result.stdout
    assert "issue_comment trigger: 0" in result.stdout


def test_a_waker_file_that_cannot_be_read_keeps_the_long_window(tmp_path):
    """Same failure direction as the state probe: expensive, never unsafe."""
    fx = waker_deployed_fixture()
    fx["routes"]["contents/.github/workflows/codex-verdict-waker.yml"] = {"*": "__FAIL__"}
    result = run_gate(tmp_path, fx)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "cannot re-enter the gate, so the in-run window stays" in result.stdout


def test_disabled_waker_keeps_the_long_window(tmp_path):
    """A workflow disabled in the UI still has a file; a file wakes nothing.

    This is why the probe asks the Actions API for the workflow's STATE rather
    than the Contents API for the file's existence — the short window must be
    earned by something events will actually start.
    """
    result = run_gate(tmp_path, waker_deployed_fixture(state="disabled_manually"))
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "cannot re-enter the gate, so the in-run window stays" in result.stdout


def test_probe_failure_falls_back_to_the_long_window(tmp_path):
    """The probe selects a wait length, not a verdict, so it must fail SOFT.

    A gh outage on this one informational read must cost minutes (the long
    window), never the merge — the verdict machinery still decides, and here it
    still finds the clean review and exits 0.
    """
    fx = clean_fixture()
    fx["routes"]["actions/workflows/codex-verdict-waker.yml"] = {"*": "__FAIL__"}
    result = run_gate(tmp_path, fx)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "cannot re-enter the gate, so the in-run window stays" in result.stdout


# --------------------------------------------------------------------------
# Review debt: a degraded round that merges over P1s must record them.
#
# A finding that blocks nothing and is recorded nowhere is a finding deleted —
# the PR closes and the threads fall out of every queue. These tests pin the
# whole contract: the issue is filed exactly when P1s defer (never on full
# rounds, never on a clean head), it deduplicates by title, and its failure
# costs the record, never the merge.
# --------------------------------------------------------------------------


def issue_posts(tmp_path):
    """The bodies of POSTs to the repo-level issues endpoint (creation only)."""
    calls = posted_bodies(tmp_path)
    return [
        line
        for line in calls.splitlines()
        if "issues -f title=" in line.replace("repos/owner/repo/", "")
        or ("repos/owner/repo/issues " in line and "-f" in line)
    ]


def debt_record(path: str, body: str, url: str = "https://example/c/1") -> str:
    """What the debt jq renders for one inline finding: path, link, first line."""
    return f"{path}\t{url}\t{body}"


def test_deferred_p1s_merge_and_are_filed_as_review_debt(tmp_path):
    fx = round_fixture(OLD1, OLD2, OLD3, head_review=True)
    fx["routes"]["pulls/7/comments"] = {
        "pull_request_review_id": debt_record("api/x.py", P1_BODY_NO_BLOCKER),
        "*": "",
    }
    result = run_gate(tmp_path, fx)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    calls = posted_bodies(tmp_path)
    assert "[review-debt] Unbounded request body" in calls, (
        f"the deferred P1 was not filed under its own title:\n{calls}"
    )
    assert "filed 1, already open 0" in result.stdout


def test_a_debt_title_survives_the_badge_markup(tmp_path):
    """The title is the dedup key, so it must be the finding's own sentence.

    Codex wraps a finding in bold and a badge image; carried verbatim into a
    title, that is unreadable in a list AND unstable as a key.
    """
    fx = round_fixture(OLD1, OLD2, OLD3, head_review=True)
    fx["routes"]["pulls/7/comments"] = {
        "pull_request_review_id": debt_record("api/x.py", P1_BODY),
        "*": "",
    }
    run_gate(tmp_path, fx)
    calls = posted_bodies(tmp_path)
    assert "-f title=[review-debt] Fix the thing" in calls, calls
    assert "img.shields.io" not in calls.split("-f body=")[0]


def test_each_deferred_finding_gets_its_own_issue(tmp_path):
    """One bundle is open or closed for everything in it.

    A per-PR issue cannot retire a fixed finding without either closing its
    unfixed siblings or staying open as a reminder of nothing in particular.
    """
    fx = round_fixture(OLD1, OLD2, OLD3, head_review=True)
    fx["routes"]["pulls/7/comments"] = {
        "pull_request_review_id": (
            debt_record("api/x.py", P1_BODY_NO_BLOCKER)
            + "\n"
            + debt_record("api/y.py", "**![P1 Badge](x) Retry loop never terminates**")
        ),
        "*": "",
    }
    result = run_gate(tmp_path, fx)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    calls = posted_bodies(tmp_path)
    assert "[review-debt] Unbounded request body" in calls
    assert "[review-debt] Retry loop never terminates" in calls
    assert "filed 2, already open 0" in result.stdout


def test_a_finding_repeated_within_one_batch_is_filed_once(tmp_path):
    """Dedup must cover the batch, not only the repo as it was before it.

    One degraded round walks several reviews, and a still-open finding is
    re-emitted in each — so the same normalized title recurs inside a single
    run. The open-title snapshot is taken before the loop, so a title filed
    inside it has to be added as it goes.
    """
    fx = round_fixture(OLD1, OLD2, OLD3, head_review=True)
    fx["routes"]["pulls/7/comments"] = {
        "pull_request_review_id": (
            debt_record("api/x.py", P1_BODY_NO_BLOCKER)
            + "\n"
            + debt_record("api/x.py", P1_BODY_NO_BLOCKER, url="https://example/c/2")
        ),
        "*": "",
    }
    result = run_gate(tmp_path, fx)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    calls = posted_bodies(tmp_path)
    assert calls.count("-f title=[review-debt] Unbounded request body") == 1, (
        f"the same finding was filed twice in one batch:\n{calls}"
    )
    assert "filed 1, already open 1" in result.stdout


def test_review_debt_carries_the_finding_and_the_policy(tmp_path):
    fx = round_fixture(OLD1, OLD2, OLD3, head_review=True)
    fx["routes"]["pulls/7/comments"] = {
        "pull_request_review_id": debt_record("api/x.py", P1_BODY_NO_BLOCKER),
        "*": "",
    }
    run_gate(tmp_path, fx)
    calls = posted_bodies(tmp_path)
    assert "exceeded the review budget" in calls
    assert HEAD in calls, "the debt record does not name the merged head"
    assert "api/x.py" in calls, "the debt record does not name the file"
    assert "https://example/c/1" in calls, (
        "the debt record does not link back to the finding, so the issue "
        "carries only a headline"
    )


def test_no_debt_issue_on_a_full_round(tmp_path):
    """On rounds 1-3 a P1 blocks; filing debt there would double-report."""
    fx = round_fixture(OLD1, OLD2, head_review=True)
    fx["routes"]["pulls/7/comments"] = {
        "pull_request_review_id": P1_BODY_NO_BLOCKER,
        "*": "",
    }
    result = run_gate(tmp_path, fx)
    assert result.returncode != 0
    assert "Review debt:" not in posted_bodies(tmp_path)


def test_no_debt_issue_on_a_clean_degraded_round(tmp_path):
    """No findings, nothing to record — the exit 0 stays quiet."""
    fx = round_fixture(OLD1, OLD2, OLD3, head_review=True)
    fx["routes"]["pulls/7/comments"] = {"*": ""}
    result = run_gate(tmp_path, fx)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "Review debt:" not in posted_bodies(tmp_path)


def test_degraded_p0_blocks_and_files_no_debt(tmp_path):
    """A P0 holds the merge; the debt record is only for what merged over."""
    fx = round_fixture(OLD1, OLD2, OLD3, head_review=True)
    fx["routes"]["pulls/7/comments"] = {
        "pull_request_review_id": P0_BODY_NO_BLOCKER,
        "*": "",
    }
    result = run_gate(tmp_path, fx)
    assert result.returncode != 0
    assert "Review debt:" not in posted_bodies(tmp_path)


def test_an_already_recorded_finding_is_not_refiled(tmp_path):
    """A still-open finding is re-emitted every degraded round.

    Dedup is by open title, the same key the trunk review already uses, so the
    second round records nothing new rather than accumulating duplicates.
    """
    fx = round_fixture(OLD1, OLD2, OLD3, head_review=True)
    fx["routes"]["pulls/7/comments"] = {
        "pull_request_review_id": debt_record("api/x.py", P1_BODY_NO_BLOCKER),
        "*": "",
    }
    fx["routes"]["issues?state=open"] = {"*": "[review-debt] Unbounded request body"}
    result = run_gate(tmp_path, fx)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    calls = posted_bodies(tmp_path)
    assert "-f title=" not in calls, f"refiled an already-open finding:\n{calls}"
    assert "filed 0, already open 1" in result.stdout


def test_debt_filing_failure_never_holds_the_merge_and_never_duplicates(tmp_path):
    """The merge decision precedes the bookkeeping; a 403 costs the record.

    And when the DEDUP lookup is what failed, the gate must write NOTHING: an
    empty lookup result born from an error is indistinguishable from "no issue
    yet", and creating on it would duplicate the per-PR debt issue — the
    one-issue contract broken by the very mechanism meant to keep it.
    """
    fx = round_fixture(OLD1, OLD2, OLD3, head_review=True)
    fx["routes"]["pulls/7/comments"] = {
        "pull_request_review_id": P1_BODY_NO_BLOCKER,
        "*": "",
    }
    fx["routes"]["issues?state=open"] = "__FAIL__"
    result = run_gate(tmp_path, fx)
    assert result.returncode == 0, (
        f"a bookkeeping failure held a merge the verdict allowed:\n{result.stdout}\n{result.stderr}"
    )
    calls = posted_bodies(tmp_path)
    # The dedup lookup's --jq text legitimately names the title; what must be
    # absent is any WRITE — the creation's `-f title=` or an append to a
    # comments endpoint with a body.
    assert "-f title=" not in calls, (
        f"a failed dedup lookup created a possibly-duplicate issue:\n{calls}"
    )
    assert "writing nothing rather than risking a duplicate" in result.stdout
