"""Resolution of the column a bar starts at.

This is `get_due_column`'s mirror and it guards the same two traps, because detection
writes `date_columns.start` by the same mechanism that produced the `go_live: null`
failure of §16.6:

  * a key that *exists* holding None ignores `dict.get`'s default, so the default never
    applies and the lookup silently yields nothing;
  * a header scan that is too eager claims a column that is not a date at all.

The second trap is specific to this side. `schema_detect._structural_fallback` matches a
start column with the word list ["start", "created", "opened", "assigned"] — and
"assigned" is a substring of "Assigned To", which is a *people* column on most trackers.
Matching it here would draw every bar from a person's name.
"""

from app.core.schema import get_start_column


# --- the null-default trap (the §16.6 shape, on the other end of the bar) ----

def test_explicit_null_start_does_not_resolve_to_a_column():
    assert get_start_column({"date_columns": {"start": None}}) is None


def test_a_mapped_start_column_wins_over_the_header_scan():
    """The header order matters to this test. "Begin Work" is scanned first and matches
    `_START_WORDS`, so a scan-only implementation returns it; only an implementation that
    reads the schema key first returns "Kickoff". With the two in the other order both
    implementations agree and the test proves nothing."""
    headers = ["Begin Work", "Kickoff"]
    assert get_start_column({"date_columns": {"start": "Kickoff"}}, headers) == "Kickoff"


def test_blank_string_is_treated_as_unmapped():
    assert get_start_column({"date_columns": {"start": "  "}}, ["Start Date"]) == "Start Date"


# --- the header scan ---------------------------------------------------------

def test_header_scan_finds_a_start_column_when_the_schema_maps_nothing():
    headers = ["WRICEF No.", "Start Date", "Expected Completetion Date", "Status"]
    assert get_start_column({}, headers) == "Start Date"


def test_header_scan_returns_the_verbatim_header():
    """Trailing whitespace is real data and is used as a lookup key."""
    assert get_start_column({}, ["Start Date "]) == "Start Date "


def test_header_scan_is_case_insensitive():
    assert get_start_column({}, ["KICKOFF DATE"]) == "KICKOFF DATE"


def test_raised_on_is_recognised_as_a_start():
    """The wording schema_detect's own prompt example uses."""
    assert get_start_column({}, ["ID", "Raised On", "Status"]) == "Raised On"


# --- what must NOT be treated as a start -------------------------------------

def test_a_people_column_is_never_claimed_as_a_start_date():
    """"assigned" is a substring of "Assigned To". Matching it draws every bar from a name."""
    assert get_start_column({}, ["ID", "Assigned To", "Status"]) is None


def test_a_completion_column_is_never_a_start():
    assert get_start_column({}, ["ID", "Date Completion", "Sign-Off Date"]) is None


def test_no_start_column_reports_none_rather_than_guessing():
    assert get_start_column({}, ["ID", "Owner", "Notes"]) is None


def test_created_by_is_a_person_not_a_start():
    """Same trap as "Assigned To": a name parses to no date, so every row would land in
    the `unparsed` bucket that is reserved for genuinely malformed dates."""
    assert get_start_column({}, ["ID", "Created By", "Status"]) is None


def test_raised_by_is_a_person_not_a_start():
    assert get_start_column({}, ["ID", "Raised By", "Status"]) is None


def test_opened_by_is_a_person_not_a_start():
    assert get_start_column({}, ["ID", "Opened By", "Status"]) is None


def test_by_is_rejected_as_a_token_not_as_a_substring():
    """"Standby Start Date" is a real start column whose *substring* "by" must not reject
    it. The header has to contain a start word to reach the guard at all, which is why
    this is not the plain "Standby Date" — that resolves to None for the ordinary reason
    that nothing in it looks like a start, and so proves nothing about the guard."""
    assert get_start_column({}, ["ID", "Standby Start Date", "Status"]) == "Standby Start Date"


# --- the collision with _DUE_WORDS -------------------------------------------

def test_the_resolved_due_column_is_never_also_returned_as_the_start():
    """"Planned Start" contains "planned", which is in _DUE_WORDS. If get_due_column has
    already claimed it, returning it here too gives every row a zero-length bar — which
    renders as data rather than as the mapping error it is."""
    headers = ["ID", "Planned Start", "Status"]
    assert get_start_column({}, headers, exclude="Planned Start") is None


def test_exclusion_matching_ignores_whitespace_and_case():
    headers = ["ID", "Start Date "]
    assert get_start_column({}, headers, exclude="start date") is None


def test_exclusion_does_not_suppress_a_genuinely_different_column():
    headers = ["ID", "Start Date", "Due Date"]
    assert get_start_column({}, headers, exclude="Due Date") == "Start Date"


def test_an_excluded_explicit_mapping_reports_nothing_rather_than_scanning_on():
    """A mapping that collides with the due column is a mapping error, and the honest
    answer is no start column at all. Falling through to the header scan here would hand
    back "Kickoff Date" — a column the human never mapped — in place of the decision they
    did make, which is the substitution the schema-first rule exists to prevent."""
    headers = ["ID", "Planned Start", "Kickoff Date"]
    schema = {"date_columns": {"start": "Planned Start"}}
    assert get_start_column(schema, headers, exclude="Planned Start") is None


# --- the actual pair, on a tab that records four dates -------------------------

from app.core.schema import resolve_actual_columns  # noqa: E402

FOUR = ["ID", "Planned Start Date", "Planned Finish Date",
        "Actual Start Date", "Actual Finish Date", "Owner"]


def test_a_two_date_tab_has_no_actual_pair():
    """The property that keeps every existing tracker drawing exactly as it did. Most
    sheets record one pair, and on those this whole feature must be inert."""
    assert resolve_actual_columns(
        {}, ["ID", "Start Date", "Expected Completetion Date"],
        exclude=("Start Date", "Expected Completetion Date"),
    ) == (None, None)


def test_the_qualifier_scan_finds_both_actual_columns():
    assert resolve_actual_columns(
        {}, FOUR, exclude=("Planned Start Date", "Planned Finish Date"),
    ) == ("Actual Start Date", "Actual Finish Date")


def test_the_actual_pair_never_reuses_a_column_the_planned_pair_took():
    """One column cannot be both ends of both segments. Without the exclusion a tab whose
    planned deadline is the only completion-ish header hands it to the baseline too, and
    the row's plan and its outcome are drawn as the same bar."""
    headers = ["ID", "Start Date", "Date Completion"]
    assert resolve_actual_columns(
        {}, headers, exclude=("Start Date", "Date Completion"),
    ) == (None, None)


def test_a_deadline_wearing_the_word_completion_is_not_an_actual_finish():
    """"Expected Completion Date" is a promise, not a record. Reading it as the day the
    work landed would mark unfinished rows delivered and cancel their overdue flag — the
    §16.6 inversion in the other direction."""
    headers = ["ID", "Start Date", "Expected Completetion Date", "Target Completion Date"]
    _, actual_due = resolve_actual_columns(
        {}, headers, exclude=("Start Date", "Expected Completetion Date"),
    )
    assert actual_due is None


def test_a_genuine_completion_column_is_taken_even_without_the_word_actual():
    """"Date Completion" on the reference tracker records when work finished and carries no
    qualifier. Requiring the word "actual" would miss every sheet spelled that way."""
    headers = ["ID", "Start Date", "Expected Completetion Date", "Date Completion"]
    _, actual_due = resolve_actual_columns(
        {}, headers, exclude=("Start Date", "Expected Completetion Date"),
    )
    assert actual_due == "Date Completion"


def test_the_schema_completion_role_outranks_the_scan():
    """Detection has been writing `completion` all along. Reading it is what lets a sheet
    detected years ago grow a baseline without being re-detected."""
    schema = {"date_columns": {"completion": "Sign-off Date"}}
    headers = ["ID", "Start Date", "Date Completion", "Sign-off Date"]
    _, actual_due = resolve_actual_columns(schema, headers, exclude=("Start Date",))
    assert actual_due == "Sign-off Date"


def test_a_null_completion_key_falls_through_to_the_scan():
    """Detection writes `date_columns` keys holding a literal null on the LLM path. A
    truthiness test is the difference between finding the column and finding nothing —
    §16.6, which broke `summarize(overdue)` on every registered project."""
    schema = {"date_columns": {"completion": None, "actual_due": None}}
    headers = ["ID", "Start Date", "Date Completion"]
    _, actual_due = resolve_actual_columns(schema, headers, exclude=("Start Date",))
    assert actual_due == "Date Completion"


def test_an_actual_start_must_wear_the_qualifier():
    """Every unqualified start-looking header is a candidate for the *planned* start.
    Claiming one here would move the baseline without saying so."""
    headers = ["ID", "Planned Start Date", "Planned Finish Date", "Kickoff Date",
               "Actual Finish Date"]
    actual_start, _ = resolve_actual_columns(
        {}, headers, exclude=("Planned Start Date", "Planned Finish Date"),
    )
    assert actual_start is None


def test_created_by_is_not_an_actual_start():
    """The `by` guard applies here too — it is `header_reads_as_a_start` doing the work, so
    a people column cannot become half a baseline."""
    headers = ["ID", "Planned Start Date", "Planned Finish Date", "Actual Created By"]
    actual_start, _ = resolve_actual_columns(
        {}, headers, exclude=("Planned Start Date", "Planned Finish Date"),
    )
    assert actual_start is None
