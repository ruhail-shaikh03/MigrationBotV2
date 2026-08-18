"""CSV export: the two things that make it safe rather than merely convenient.

The filters are not re-tested here — that is the point of the export calling `_filter_rows`,
the same function the grid calls, instead of reimplementing them. What is tested is the
formula-injection guard, because an export is the one place where data this app merely
displays becomes something the recipient's spreadsheet will *execute*.
"""

from app.api.dashboard import _csv_safe


def test_ordinary_text_passes_through_untouched():
    assert _csv_safe("Completed") == "Completed"
    assert _csv_safe("W-1") == "W-1"


def test_a_blank_cell_stays_blank():
    assert _csv_safe(None) == ""
    assert _csv_safe("") == ""


def test_a_value_that_would_run_as_a_formula_is_marked_as_text():
    """Someone types this into a Description field; a PM exports the tab and opens it."""
    assert _csv_safe("=HYPERLINK(\"http://evil\",\"click\")").startswith("'=")


def test_every_leader_a_spreadsheet_executes_is_covered():
    for leader in ("=", "+", "-", "@"):
        assert _csv_safe(f"{leader}cmd").startswith("'"), leader


def test_the_original_characters_are_preserved_not_stripped():
    """The guard makes the value readable-as-text; it must not silently edit the data."""
    assert _csv_safe("=SUM(A1:A2)") == "'=SUM(A1:A2)"


def test_a_negative_number_is_also_quoted_and_that_is_accepted():
    """A leading "-" is indistinguishable from a formula lead-in, so -5 is quoted too.

    Excel still reads '-5 as the text "-5", which is a visible, correctable annoyance —
    unlike the alternative, which is a silently executable cell.
    """
    assert _csv_safe("-5") == "'-5"


def test_a_tab_or_carriage_return_lead_is_neutralised():
    """Both are used to smuggle a formula past a naive first-character check."""
    assert _csv_safe("\t=cmd").startswith("'")
    assert _csv_safe("\r=cmd").startswith("'")
