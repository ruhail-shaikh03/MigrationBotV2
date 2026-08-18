"""Classifying a column as holding people, from its values alone.

The bug this guards: detection reasoned about header wording, so "Consultant" was
missed because the word reads like a job title in English. Header wording is the least
generic signal available — the next sheet says Owner, PIC, or a header in another
language. Value shape is the same on every sheet.

Fixtures are named by shape, never by the sheet they came from: the profiler must not
know that any customer has a "University" column, and neither must its tests.
"""

import pytest

from app.core.column_profile import (
    MAX_SAMPLE, MIN_SAMPLE, people_confidence, profile_column,
)


def _rep(values, times):
    """Repeat a small value set up to a realistic row count."""
    return [values[i % len(values)] for i in range(times)]


PEOPLE = ["Muhammad Zeshan Ayub", "Madiha Shah Bukhari", "Huzaima Ather", "Taymoor Ahmed",
          "Neeha Nehal", "Amna Lateef Korejo", "Arjmand Bano", "Babar Ali"]
UPPER_CODES = ["AROR", "AWKUM", "BNWU", "CUVAS", "EUM", "GUDGK", "HSA"]
CATEGORY_LABELS = ["Fiori", "Report", "Form", "Interface"]
ENUM_LABELS = ["Completed", "Not Started", "In Testing", "Hold"]
FREE_TEXT = ["URL Update (Transport, Hostel, Complaint and etc)",
             "Student fee challan generation for semester 2",
             "Bulk upload of transcript records from legacy",
             "Hostel allocation report with room mapping"]


# Row counts are chosen so each fixture's cardinality matches what the real trackers
# measure — people 0.077, short codes 0.071. That similarity is the whole point: the two
# are nearly identical on cardinality and repetition, so if a fixture were rejected for
# having too few distinct values it would prove nothing about the discriminator that
# actually separates them.

def test_multi_token_titlecase_names_are_people():
    profile = profile_column(_rep(PEOPLE, 100))
    assert round(profile.cardinality, 2) == 0.08
    assert people_confidence(profile) == "likely"


def test_short_uppercase_codes_are_not_people():
    """The discriminator under test: same cardinality band and repetition as names, but
    1 token, ~4 chars, and shouted rather than title-cased."""
    profile = profile_column(_rep(UPPER_CODES, 100))
    assert round(profile.cardinality, 2) == 0.07, "must sit in the same band as people"
    assert people_confidence(profile) == "unlikely"


def test_single_token_category_labels_are_not_people():
    assert people_confidence(profile_column(_rep(CATEGORY_LABELS, 160))) == "unlikely"


def test_free_text_is_not_people():
    assert people_confidence(profile_column(FREE_TEXT * 100)) == "unlikely"


def test_unique_identifiers_are_not_people():
    assert people_confidence(profile_column([f"SLCM-{i:04d}" for i in range(400)])) == "unlikely"


def test_a_short_workflow_vocabulary_is_never_read_as_a_roster():
    """Four labels are four whether the tab has 120 rows or 400.

    This used to depend on the row count rather than the values: at 400 rows the
    cardinality *ratio* fell under the floor and the column was rejected, but the same four
    labels in a 120-row tab measured 0.033, passed every criterion, and came back "likely".
    An absolute distinct-value count is what makes the answer a statement about the column.
    """
    for rows in (120, 400):
        assert people_confidence(profile_column(_rep(ENUM_LABELS, rows))) != "likely", rows


def test_a_longer_enum_abstains_rather_than_being_rejected():
    """Abstaining is not a vote — it neither applies a role nor blocks one, and it is the
    honest answer where shape runs out. Eight states clear the distinct-count floor exactly
    as a team of eight would."""
    eight_states = ENUM_LABELS + ["Cancelled", "In Review", "Deployed", "Blocked"]
    assert people_confidence(profile_column(_rep(eight_states, 400))) == "abstain"


def test_a_team_of_eight_is_still_told_apart_from_a_workflow_of_eight():
    """The two are not in fact statistically identical, as an earlier note here claimed:
    names run to 2.4 tokens and 14 characters, state labels to 1.4 and 8. Distinct count
    and repetition cannot separate them; token count and length can."""
    eight_states = ENUM_LABELS + ["Cancelled", "In Review", "Deployed", "Blocked"]
    assert people_confidence(profile_column(_rep(PEOPLE, 400))) == "likely"
    assert people_confidence(profile_column(_rep(eight_states, 400))) != "likely"


def test_a_very_small_team_is_not_promoted_and_that_is_a_known_limit():
    """A three-person project is real, and the profiler calls it "unlikely" — a stronger
    claim than the evidence supports, because `distinct` and `cardinality` measure the same
    property and both fail, so one characteristic is counted as two.

    Left alone deliberately. No caller distinguishes "unlikely" from "abstain" — both
    `_structural_fallback` and the health check test for `== "likely"` — so the only thing
    a further criterion would buy is a more accurate label that nothing reads. The column
    is still reachable through the keyword and LLM paths (§7.5), which is the point of the
    profiler being one signal rather than the decision.
    """
    assert people_confidence(profile_column(_rep(PEOPLE[:3], 400))) != "likely"


def test_small_samples_abstain():
    assert people_confidence(profile_column(PEOPLE[:5])) == "abstain"
    assert people_confidence(profile_column(_rep(PEOPLE, MIN_SAMPLE - 1))) == "abstain"


def test_blank_and_whitespace_values_are_ignored():
    profile = profile_column(["Babar Ali", "", "   ", None, "Neeha Nehal"])
    assert profile.n == 2


def test_empty_column_abstains():
    assert people_confidence(profile_column([])) == "abstain"


@pytest.mark.parametrize("field", [
    "cardinality", "mean_tokens", "mean_length", "alpha_ratio", "title_case_ratio", "repeat_ratio",
])
def test_profile_fields_are_computed_independently(field):
    """Signals stay separable so a non-Latin sheet can lean on the script-agnostic ones."""
    assert isinstance(getattr(profile_column(_rep(PEOPLE, 100)), field), float)


# --- scale ------------------------------------------------------------------
#
# cardinality is distinct-over-total, so without a bounded sample the same column is
# recognised on a small tracker and rejected on a large one — the profiler would quietly
# get worse as a sheet grew, which is the failure mode hardest to notice.

_MANY_PEOPLE = [f"{f} {l}" for f in
                ["Muhammad", "Madiha", "Huzaima", "Taymoor", "Neeha", "Amna", "Arjmand",
                 "Babar", "Sara", "Bilal", "Zainab"]
                for l in ["Zeshan Ayub", "Shah Bukhari", "Ather", "Ahmed", "Nehal",
                          "Korejo", "Bano", "Ali", "Iqbal", "Qamar", "Malik"]][:33]


@pytest.mark.parametrize("rows", [412, 2000, 20000, 100000])
def test_a_roster_is_recognised_at_any_tab_size(rows):
    """33 people in 20 000 rows measures 0.0017 unsampled, well under the 0.02 floor."""
    assert people_confidence(profile_column(_rep(_MANY_PEOPLE, rows))) == "likely"


@pytest.mark.parametrize("rows", [2000, 20000, 100000])
def test_the_sample_is_capped_and_the_profile_stops_changing_with_size(rows):
    profile = profile_column(_rep(_MANY_PEOPLE, rows))
    assert profile.n == MAX_SAMPLE
    assert round(profile.cardinality, 4) == round(len(_MANY_PEOPLE) / MAX_SAMPLE, 4)


def test_sampling_does_not_alias_on_periodic_data():
    """Sheet data is periodic — grouped by module, assigned round-robin. Taking every Nth
    row where N shares a factor with that period sees a fraction of the distinct values:
    33 names sampled every third row yields 11, and a cardinality a third of the truth."""
    profile = profile_column(_rep(_MANY_PEOPLE, 3000))
    assert profile.cardinality * profile.n > len(_MANY_PEOPLE) * 0.9


def test_the_same_column_profiles_identically_twice():
    """A verdict that changed between two reads of an unchanged sheet would be
    untraceable to anything, so the sample is drawn from a fixed seed."""
    column = _rep(_MANY_PEOPLE, 5000)
    assert profile_column(column) == profile_column(column)


@pytest.mark.parametrize("values", [UPPER_CODES, FREE_TEXT])
def test_sampling_does_not_start_admitting_non_people_at_scale(values):
    assert people_confidence(profile_column(_rep(values, 20000))) != "likely"


def test_unique_per_row_identifiers_stay_rejected_at_scale():
    assert people_confidence(profile_column([f"W-{i}" for i in range(20000)])) == "unlikely"


def test_a_column_shorter_than_the_cap_is_not_sampled_at_all():
    column = _rep(PEOPLE, 100)
    assert profile_column(column).n == 100


# --- detection ---------------------------------------------------------------

from app.core.schema_detect import _structural_fallback  # noqa: E402


def _sheet(header, values):
    return [[header]] + [[v] for v in values]


def test_fallback_detects_a_people_column_a_keyword_list_would_miss():
    """The motivating bug: a header whose wording reads like a job title rather than an
    owner field. Value shape does not care what the column is called."""
    rows = _sheet("Focal Point", _rep(PEOPLE, 100))
    people = [c["header"] for c in _structural_fallback(rows)["people_columns"]]
    assert people == ["Focal Point"]


def test_fallback_does_not_promote_short_code_columns():
    rows = _sheet("Site", _rep(UPPER_CODES, 100))
    assert _structural_fallback(rows)["people_columns"] == []


def test_fallback_keeps_keyword_matches_the_profiler_abstains_on():
    """Union, not intersection: a sparsely-filled "Owner" column abstains statistically
    but is still obviously a role by its name."""
    rows = _sheet("Owner", PEOPLE[:4])
    people = [c["header"] for c in _structural_fallback(rows)["people_columns"]]
    assert people == ["Owner"]


def test_fallback_preserves_the_verbatim_header():
    rows = _sheet("Focal Point ", _rep(PEOPLE, 100))
    assert _structural_fallback(rows)["people_columns"][0]["header"] == "Focal Point "


def test_a_column_is_never_listed_twice():
    """A header matching a keyword AND profiling as people must yield one entry."""
    rows = _sheet("Resource Owner", _rep(PEOPLE, 100))
    assert len(_structural_fallback(rows)["people_columns"]) == 1


def test_profiling_ignores_a_title_row_above_the_header():
    """Trackers put a title or spacer above the real header row. Profiling one row off
    would fold the header text into the value sample."""
    rows = [["Q3 Tracker", ""], ["ID", "Focal Point"]] + [
        [f"T-{i}", PEOPLE[i % len(PEOPLE)]] for i in range(100)
    ]
    people = [c["header"] for c in _structural_fallback(rows)["people_columns"]]
    assert people == ["Focal Point"]
