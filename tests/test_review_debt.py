"""The identity rule for review-debt records, over the cases the review rounds found.

Each case below was, on gate #10, a separate review finding answered by a
separate sed edit. Here they are one contract.
"""
from __future__ import annotations

import hashlib
import io
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts import review_debt as rd  # noqa: E402

P1 = "**![P1 Badge](https://img.shields.io/badge/P1-orange) [BLOCKER] Fix the thing**"


def test_identity_is_a_bounded_hash_of_path_and_the_full_line():
    fp = rd.fingerprint("api/x.py", P1)
    assert fp == hashlib.sha256(b"api/x.py\0" + P1.encode()).hexdigest()[:12]
    assert rd.title("api/x.py", P1) == f"[review-debt] api/x.py: Fix the thing ({fp})"


def test_display_strips_badge_html_bold_and_tag_but_identity_keeps_them():
    line = "**![P1 Badge](x) <sub><sub></sub></sub> [BLOCKER]  Recheck   the head **"
    assert rd.display_name(line) == "Recheck the head"
    assert rd.fingerprint("f", line) != rd.fingerprint("f", "Recheck the head")


def test_an_empty_name_still_titles_as_finding():
    assert rd.display_name("**![P1 Badge](x)**") == "finding"


def test_same_headline_in_two_files_is_two_findings():
    assert rd.title("api/x.py", P1) != rd.title("api/y.py", P1)


def test_same_finding_at_two_locations_in_one_file_is_one_record():
    """Accepted loss, recorded as policy: no signal tells a moved finding from a
    duplicated defect, and a line-aware key would re-file after every edit."""
    assert rd.title("api/x.py", P1) == rd.title("api/x.py", P1)
    planned = list(rd.plan(
        [("api/x.py", "https://e/c/1", P1), ("api/x.py", "https://e/c/2", P1)], []
    ))
    assert [s for s, _, _ in planned] == ["new", "open"]


def test_the_pr_number_is_not_part_of_the_key():
    """One defect found in two PRs is one defect; the body names the PR, the
    title does not."""
    t = rd.title("api/x.py", P1)
    assert "#" not in t
    b1 = rd.body(path="api/x.py", url="u", line=P1, pr="7", head="h", round_="4", full_rounds="3")
    b2 = rd.body(path="api/x.py", url="u", line=P1, pr="8", head="h", round_="4", full_rounds="3")
    assert "PR: #7" in b1 and "PR: #8" in b2


def test_two_headlines_that_collide_past_the_display_bound_stay_distinct():
    long_a = "**![P1 Badge](x) " + "y" * 250 + "A**"
    long_b = "**![P1 Badge](x) " + "y" * 250 + "B**"
    ta, tb = rd.title("api/x.py", long_a), rd.title("api/x.py", long_b)
    assert ta.split(" (")[0] == tb.split(" (")[0], "display parts agree past the bound"
    assert ta != tb, "identity must not"


@pytest.mark.parametrize("path", ["src/" + "nested/" * 40 + "module.py", "a.py"])
def test_a_title_never_exceeds_the_api_limit(path):
    t = rd.title(path, "**![P1 Badge](x) " + "y" * 300 + "**")
    assert len(t) <= 256
    assert t.startswith("[review-debt] ") and t.endswith(")")


def test_re_emission_after_an_edit_above_the_finding_is_the_same_record():
    """Nothing in the key moves when the file does: no line, no comment id."""
    first = ("api/x.py", "https://e/c/1", P1)
    later = ("api/x.py", "https://e/c/999", P1)
    assert rd.title(*first[::2]) == rd.title(*later[::2])
    planned = list(rd.plan([later], [rd.title("api/x.py", P1)]))
    assert planned == [("open", rd.title("api/x.py", P1), "")]


def test_records_are_tab_separated_and_a_line_less_record_is_skipped():
    text = "api/x.py\thttps://e/c/1\t" + P1 + "\n\n\nreview body\thttps://e/r/5\t" + P1 + "\napi/z.py\thttps://e/c/3\t\n"
    recs = list(rd.parse_records(io.StringIO(text)))
    assert recs == [
        ("api/x.py", "https://e/c/1", P1),
        ("review body", "https://e/r/5", P1),
    ]


def test_the_cli_writes_one_body_per_new_record_and_reports_status(tmp_path):
    open_titles = tmp_path / "open"
    open_titles.write_text(rd.title("api/y.py", P1) + "\n", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    records = "\n".join([
        f"api/x.py\thttps://e/c/1\t{P1}",
        f"api/y.py\thttps://e/c/2\t{P1}",
        f"api/x.py\thttps://e/c/3\t{P1}",
    ])
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "review_debt.py"), "plan",
         "--pr", "7", "--round", "4", "--full-rounds", "3", "--head", "h" * 40,
         "--open-titles", str(open_titles), "--out", str(out)],
        input=records, capture_output=True, text=True, check=True,
    )
    lines = [ln.split("\t") for ln in result.stdout.splitlines()]
    assert [(n, s) for n, s, _ in lines] == [("1", "new"), ("2", "open"), ("3", "open")]
    assert lines[0][2] == rd.title("api/x.py", P1)
    assert sorted(p.name for p in out.iterdir()) == ["1.body"]
    body = (out / "1.body").read_text(encoding="utf-8")
    assert "Round 4 of PR #7 exceeded the review budget (3 full rounds)" in body
    assert "Head: " + "h" * 40 in body and "File: api/x.py" in body
    assert "Finding: https://e/c/1" in body and P1 in body
    assert body.endswith("Fix the invariant, not the instance — see AGENTS.md.")
