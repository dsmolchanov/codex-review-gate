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
    REPO_ROOT
    / "scripts"
    / "repo-templates"
    / ".github"
    / "workflows"
    / "codex-review-window.yml"
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
for key in sorted(fixture.get("routes", {}), key=len, reverse=True):
    if key not in url:
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
        "DEBOUNCE_SECONDS": "0",
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
