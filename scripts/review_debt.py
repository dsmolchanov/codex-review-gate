#!/usr/bin/env python3
"""Identity, title and body of a review-debt record.

The gate files one GitHub issue per finding it merges over on a round past the
review budget. What identifies a finding decided four of the five review rounds
on gate #10, each time as a narrow fix in a shell pipeline. This module is the
rule those rounds were circling, written once, as a value with a name and a
test — see the ``## Policy`` block, then ``tests/test_review_debt.py``.

The workflow fetches this file at its own pinned revision (``job_workflow_sha``)
and runs ``plan``; the shell keeps only the API calls, all of them fail-soft,
because the merge decision precedes the bookkeeping.

## Policy

A finding's identity is ``sha256(path NUL finding-line)``, truncated to 12 hex
digits, where ``finding-line`` is the complete first line of the comment body as
Codex wrote it — badge markup and all. The 12 digits sit in the issue title in
parentheses, so the title is also the dedup key across rounds: open issues
expose titles, not bodies.

Three signals were considered and rejected, in each case because they turn a
rare silent loss into a frequent loud one:

* a **line number** moves whenever anything above it changes, which on an active
  PR is most pushes; keying on it re-files the same finding after every edit;
* the comment's **html_url** carries a comment id that is new on every
  re-emission, so it re-files deterministically;
* the **PR number**: the same defect found in two PRs is usually one defect.
  Keying on the PR guarantees two records for it, plus a fresh set after any
  reopen or rebase that re-anchors the reviews. One defect, one issue.

Consequently two findings with identical text at different locations in one
file share a record, as do identical findings raised by separate PRs. "Same
text, different line" is what both a moved finding and a duplicated defect look
like, and no available signal separates them. That loss is accepted and is the
rarer one.

The display part of the title is the path and the finding's own sentence,
bounded to 200 characters so a deep path cannot push the title past GitHub's
256-character limit into a rejected POST — which would land in the fail-soft
branch and merge the finding unrecorded.
"""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import re
import sys
from typing import Iterable, Iterator

TITLE_PREFIX = "[review-debt] "
DISPLAY_LIMIT = 200
FINGERPRINT_HEX = 12

_BADGE = re.compile(r"!\[[^]]*\]\([^)]*\)")
_TAG = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"\s+")

BODY_TEMPLATE = (
    "Round {round} of PR #{pr} exceeded the review budget ({full_rounds} full "
    "rounds), so this still-open finding no longer holds the merge and the PR "
    "merged over it. It is recorded here so the debt survives the PR.\n"
    "\n"
    "PR: #{pr}\n"
    "Head: {head}\n"
    "File: {path}\n"
    "Finding: {url}\n"
    "\n"
    "{line}\n"
    "\n"
    "Fix the invariant, not the instance — see AGENTS.md."
)


def fingerprint(path: str, line: str) -> str:
    """The identity of a finding: a bounded hash of where and what, untruncated."""
    digest = hashlib.sha256(f"{path}\0{line}".encode("utf-8")).hexdigest()
    return digest[:FINGERPRINT_HEX]


def display_name(line: str) -> str:
    """The finding's own sentence: badge image, HTML, bold and tag stripped."""
    name = _BADGE.sub("", line)
    name = _TAG.sub("", name)
    name = name.replace("**", "").replace("[BLOCKER]", "")
    name = _SPACE.sub(" ", name.strip())
    return name or "finding"


def title(path: str, line: str) -> str:
    """``[review-debt] <path>: <name> (<fingerprint>)``, display bounded."""
    display = f"{path}: {display_name(line)}"[:DISPLAY_LIMIT]
    return f"{TITLE_PREFIX}{display} ({fingerprint(path, line)})"


def body(*, path: str, url: str, line: str, pr: str, head: str, round_: str, full_rounds: str) -> str:
    return BODY_TEMPLATE.format(
        round=round_, pr=pr, full_rounds=full_rounds, head=head, path=path, url=url, line=line
    )


def parse_records(lines: Iterable[str]) -> Iterator[tuple[str, str, str]]:
    """``path TAB url TAB line`` per record; blank and line-less records skipped.

    A finding's title is model prose and may contain anything except a tab or
    a newline, which is why the workflow renders records tab-separated.
    """
    for raw in lines:
        raw = raw.rstrip("\n")
        if not raw.strip():
            continue
        parts = raw.split("\t", 2)
        if len(parts) < 3 or not parts[2]:
            continue
        yield parts[0], parts[1], parts[2]


def plan(
    records: Iterable[tuple[str, str, str]],
    open_titles: Iterable[str],
) -> Iterator[tuple[str, str, str]]:
    """Yield ``(status, title, body_key)`` per record, deduplicated.

    ``status`` is ``open`` when the title is already an open issue or was
    already planned in this batch — one degraded round walks several reviews
    and re-emits a still-open finding in each — and ``new`` otherwise.
    """
    seen = set(open_titles)
    for path, url, line in records:
        t = title(path, line)
        if t in seen:
            yield "open", t, ""
            continue
        seen.add(t)
        yield "new", t, url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="read records on stdin; write titles and bodies")
    p.add_argument("--pr", required=True)
    p.add_argument("--round", required=True)
    p.add_argument("--full-rounds", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--open-titles", required=True, type=pathlib.Path,
                   help="file with one open issue title per line")
    p.add_argument("--out", required=True, type=pathlib.Path,
                   help="directory receiving <n>.body per new record")
    args = parser.parse_args(argv)

    open_titles = [
        t for t in args.open_titles.read_text(encoding="utf-8").splitlines() if t
    ]
    records = list(parse_records(sys.stdin))
    for n, ((status, t, _url), (path, url, line)) in enumerate(
        zip(plan(records, open_titles), records), start=1
    ):
        if status == "new":
            (args.out / f"{n}.body").write_text(
                body(path=path, url=url, line=line, pr=args.pr, head=args.head,
                     round_=args.round, full_rounds=args.full_rounds),
                encoding="utf-8",
            )
        sys.stdout.write(f"{n}\t{status}\t{t}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
