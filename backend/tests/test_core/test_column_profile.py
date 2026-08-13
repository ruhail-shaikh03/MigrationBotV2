"""Classifying a column as holding people, from its values alone.

The bug this guards: detection reasoned about header wording, so "Consultant" was
missed because the word reads like a job title in English. Header wording is the least
generic signal available — the next sheet says Owner, PIC, or a header in another
language. Value shape is the same on every sheet.

Fixtures are named by shape, never by the sheet they came from: the profiler must not
know that any customer has a "University" column, and neither must its tests.
"""

import pytest

from app.core.column_profile import MIN_SAMPLE, people_confidence, profile_column


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


def test_low_cardinality_enum_abstains_rather_than_being_rejected():
    """Documents the known limit: a team of eight and a workflow of eight states are
    statistically identical. Only meaning separates them, which is why the LLM stays in
    the loop. Abstaining is not a vote — it neither applies a role nor blocks one."""
    assert people_confidence(profile_column(_rep(ENUM_LABELS, 400))) == "abstain"


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
