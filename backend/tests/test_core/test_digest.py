"""The per-person overdue digest.

A digest is the highest-consequence report this app produces, because it is the only one
intended to leave the system and arrive somewhere unasked. Two failures matter more than
the arithmetic:

* **Reporting zero when it means "cannot tell".** A tab with no deadline column cannot have
  anything overdue on it, and "0 overdue" reads as good news to whoever opens the mail.
* **Attributing work to a person who does not exist.** "Madiha Shah Bukhari" and "Madiha"
  as two names with separate lists is a data-quality problem the digest would make public.

`today` is injected everywhere so a digest is reproducible — one that cannot be regenerated
identically cannot be checked against what was sent.
"""

from datetime import date

import pytest

from app.core.aliases import PersonResolver
from app.core.digest import build_digest, render_text

TODAY = date(2026, 8, 18)

HEADERS = ["WRICEF No.", "Description", "Dev Status", "Due Date", "Developer Name", "Consultant"]
SCHEMA = {
    "primary_id_column": "WRICEF No.",
    "description_column": "Description",
    "status_column": "Dev Status",
    "date_columns": {"due": "Due Date"},
    "people_columns": [
        {"key": "dev", "label": "Developer", "header": "Developer Name"},
        {"key": "con", "label": "Consultant", "header": "Consultant"},
    ],
}


def _row(rid, due, status="In Progress", dev="Sara Iqbal", con="", title="A task"):
    return {
        "WRICEF No.": rid, "Description": title, "Dev Status": status,
        "Due Date": due, "Developer Name": dev, "Consultant": con,
    }


def _build(rows, **kwargs):
    kwargs.setdefault("today", TODAY)
    return build_digest(HEADERS, rows, SCHEMA, **kwargs)


# --- cannot-tell is not zero ---------------------------------------------------

def test_a_tab_with_no_deadline_column_says_so_instead_of_reporting_nothing_overdue():
    digest = build_digest(["Task", "Owner"], [{"Task": "x", "Owner": "Sara"}], {}, today=TODAY)
    assert digest["totals"]["overdue"] == 0
    assert digest["reason"] is not None


def test_a_tab_with_no_people_columns_says_so_too():
    schema = {"date_columns": {"due": "Due Date"}}
    digest = build_digest(["Due Date"], [{"Due Date": "2026-01-01"}], schema, today=TODAY)
    assert digest["reason"] is not None


def test_the_text_rendering_leads_with_the_reason_when_there_is_one():
    digest = build_digest(["Task"], [], {}, today=TODAY)
    assert "No digest:" in render_text(digest, "HEDP", "SD")


# --- what counts ---------------------------------------------------------------

def test_a_past_deadline_is_overdue_and_carries_how_late_it_is():
    digest = _build([_row("W-1", "2026-08-11")])
    person = digest["people"][0]
    assert person["overdue_count"] == 1
    assert person["overdue"][0]["days"] == 7


def test_work_inside_the_window_is_due_soon_not_overdue():
    digest = _build([_row("W-1", "2026-08-21")])
    person = digest["people"][0]
    assert person["due_soon_count"] == 1
    assert person["overdue_count"] == 0
    # Negative days means still to come, so one field sorts both buckets by urgency.
    assert person["due_soon"][0]["days"] == -3


def test_work_beyond_the_window_is_left_out_entirely():
    assert _build([_row("W-1", "2026-12-01")])["totals"]["rows"] == 0


def test_finished_work_is_never_chased():
    """Substring matching, so "Completed" is caught by the word "complete" (core/overdue)."""
    assert _build([_row("W-1", "2026-01-01", status="Completed")])["totals"]["overdue"] == 0


def test_a_row_with_no_deadline_is_not_overdue():
    assert _build([_row("W-1", "")])["totals"]["rows"] == 0


def test_an_unreadable_date_does_not_silently_become_overdue():
    """17/0/2026 exists on the real tracker; parse_date returns None for it."""
    assert _build([_row("W-1", "17/0/2026")])["totals"]["rows"] == 0


# --- who it is attributed to ----------------------------------------------------

def test_one_row_naming_two_roles_appears_under_both_people():
    digest = _build([_row("W-1", "2026-08-11", dev="Sara Iqbal", con="Bilal Ahmed")])
    assert {p["person"] for p in digest["people"]} == {"Sara Iqbal", "Bilal Ahmed"}
    assert digest["people"][0]["overdue"][0]["role"] in ("Developer", "Consultant")


def test_a_shared_cell_credits_both_people_not_a_composite():
    digest = _build([_row("W-1", "2026-08-11", dev="Minhaj Alam & Dawood")])
    assert {p["person"] for p in digest["people"]} == {"Minhaj Alam", "Dawood"}


def test_two_spellings_of_one_person_merge_through_the_alias_map():
    """Without this the digest names the same person twice and splits their list —
    the failure that makes mailing it worse than not."""
    resolver = PersonResolver({"madiha": ["Madiha Shah Bukhari"]})
    digest = _build(
        [_row("W-1", "2026-08-11", dev="Madiha"), _row("W-2", "2026-08-12", dev="Madiha Shah Bukhari")],
        resolver=resolver,
    )
    assert len(digest["people"]) == 1
    assert digest["people"][0]["overdue_count"] == 2


def test_overdue_work_nobody_owns_is_surfaced_rather_than_dropped():
    """The row most likely to stay overdue is the one no digest would otherwise mention."""
    digest = _build([_row("W-1", "2026-08-11", dev="", con="")])
    assert digest["unassigned"]["overdue_count"] == 1
    assert digest["people"] == []


# --- ordering -------------------------------------------------------------------

def test_the_person_with_the_most_overdue_work_comes_first():
    digest = _build([
        _row("W-1", "2026-08-11", dev="Sara Iqbal"),
        _row("W-2", "2026-08-12", dev="Sara Iqbal"),
        _row("W-3", "2026-08-13", dev="Bilal Ahmed"),
    ])
    assert digest["people"][0]["person"] == "Sara Iqbal"


def test_the_latest_item_is_listed_first_within_a_person():
    digest = _build([
        _row("W-1", "2026-08-16", dev="Sara Iqbal"),
        _row("W-2", "2026-08-01", dev="Sara Iqbal"),
    ])
    assert [i["ricefw_id"] for i in digest["people"][0]["overdue"]] == ["W-2", "W-1"]


# --- reproducibility ------------------------------------------------------------

def test_the_same_inputs_produce_the_same_digest():
    rows = [_row("W-1", "2026-08-11"), _row("W-2", "2026-08-20", dev="Bilal Ahmed")]
    assert _build(rows) == _build(rows)


@pytest.mark.parametrize("days", [1, 7, 30])
def test_the_lookahead_window_is_honoured(days):
    digest = _build([_row("W-1", "2026-09-10")], lookahead_days=days)
    assert digest["window_days"] == days
    assert digest["totals"]["due_soon"] == (1 if days == 30 else 0)


def test_the_rendering_names_people_and_their_counts():
    digest = _build([_row("W-1", "2026-08-11", title="Migrate BOM report")])
    text = render_text(digest, "HEDP Tracker", "SD")
    assert "Sara Iqbal" in text
    assert "Migrate BOM report" in text
    assert "HEDP Tracker" in text
