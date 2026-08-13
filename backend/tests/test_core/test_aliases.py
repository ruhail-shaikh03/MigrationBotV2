"""Turning one people-cell into the people it names.

60 of 257 assigned cells on the reference tracker name two people
("Minhaj Alam & Dawood", "Ahmed Qamar/Asif"). Each became its own entry in the workload
panel, so a quarter of assignments were credited to a composite that does not exist
while the two real people got nothing.
"""

import pytest

from app.core.people import split_cell


@pytest.mark.parametrize("raw,expected", [
    ("Umair Ziad/Saba Haleem", ["Umair Ziad", "Saba Haleem"]),
    ("Minhaj Alam & Dawood", ["Minhaj Alam", "Dawood"]),
    ("Syed Ali Haider/ Muhammad Ashar Mateen", ["Syed Ali Haider", "Muhammad Ashar Mateen"]),
    ("Ahmed Qamar/Asif", ["Ahmed Qamar", "Asif"]),
])
def test_slash_and_ampersand_split_into_separate_people(raw, expected):
    assert split_cell(raw) == expected


def test_a_comma_is_never_a_delimiter():
    """"Shaikh, Rohail" is plausibly one person written surname-first. Inventing a
    colleague is a worse failure than under-splitting a shared assignment."""
    assert split_cell("Shaikh, Rohail") == ["Shaikh, Rohail"]


def test_a_plain_name_is_returned_unchanged():
    assert split_cell("Madiha Shah Bukhari") == ["Madiha Shah Bukhari"]


def test_honorifics_are_left_alone():
    """Stripping "Mr." would be English vocabulary hardcoded into the engine. The
    splitter knows delimiters and nothing else; honorifics are an alias-map concern."""
    assert split_cell("Mr. Minhaj Alam/ Dawood") == ["Mr. Minhaj Alam", "Dawood"]


def test_blank_fragments_are_dropped():
    assert split_cell("Babar Ali //& ") == ["Babar Ali"]


def test_blank_and_none_yield_nothing():
    assert split_cell("") == []
    assert split_cell(None) == []


def test_internal_whitespace_is_collapsed():
    assert split_cell("Amna   Lateef  Korejo") == ["Amna Lateef Korejo"]


# --- resolution -------------------------------------------------------------

from app.core.aliases import PersonResolver  # noqa: E402


def _resolver(**pairs):
    """alias -> one-or-more canonical names, keyed as normalise_person would key it."""
    return PersonResolver({k: (v if isinstance(v, list) else [v]) for k, v in pairs.items()})


def test_an_unknown_name_passes_through_unchanged():
    assert _resolver().resolve_cell("Babar Ali") == ["Babar Ali"]


def test_an_alias_resolves_to_its_canonical_name():
    r = _resolver(**{"madiha": "Madiha Shah Bukhari"})
    assert r.resolve_cell("Madiha") == ["Madiha Shah Bukhari"]


def test_alias_lookup_is_case_and_whitespace_insensitive():
    r = _resolver(**{"babar": "Babar Ali"})
    assert r.resolve_cell("  BABAR  ") == ["Babar Ali"]


def test_each_fragment_of_a_shared_cell_is_resolved():
    r = _resolver(**{"dawood": "Muhammad Dawood Umer"})
    assert r.resolve_cell("Minhaj Alam & Dawood") == ["Minhaj Alam", "Muhammad Dawood Umer"]


def test_a_whole_cell_alias_beats_the_splitter():
    """The escape hatch that makes automatic splitting safe: any cell the delimiter rule
    gets wrong — a team called "R&D", a name containing a slash — is fixed with one row."""
    r = _resolver(**{"r&d": "R&D Team"})
    assert r.resolve_cell("R&D") == ["R&D Team"]


def test_one_alias_may_name_several_people():
    """How a comma-separated shared cell is expressed without teaching the splitter
    about commas."""
    r = _resolver(**{"shaikh, rohail": ["Shaikh", "Rohail"]})
    assert r.resolve_cell("Shaikh, Rohail") == ["Shaikh", "Rohail"]


def test_resolution_is_exactly_one_hop():
    """A canonical name is never looked up again, so a cycle cannot form."""
    r = _resolver(**{"a": "B", "b": "A"})
    assert r.resolve_cell("A") == ["B"]


def test_a_person_named_twice_in_one_cell_is_credited_once():
    r = _resolver(**{"umair": "Umair Ziad"})
    assert r.resolve_cell("Umair Ziad / Umair") == ["Umair Ziad"]


def test_order_of_first_appearance_is_preserved():
    assert _resolver().resolve_cell("Zara/Ali/Zara") == ["Zara", "Ali"]


def test_an_empty_resolver_still_splits():
    assert PersonResolver().resolve_cell("Ahmed Qamar/Asif") == ["Ahmed Qamar", "Asif"]


# --- assignments ------------------------------------------------------------

from app.core.people import collect_assignments  # noqa: E402

DEV = {"key": "developer_name", "label": "Developer Name", "header": "Developer Name"}
CONSULTANT = {"key": "consultant", "label": "Consultant", "header": "Consultant"}


def test_a_shared_cell_becomes_one_assignment_per_person():
    out = collect_assignments({"Developer Name": "Minhaj Alam & Dawood"}, [DEV])
    assert [a["person"] for a in out] == ["Minhaj Alam", "Dawood"]
    assert {a["role_label"] for a in out} == {"Developer Name"}


def test_aliases_apply_when_a_resolver_is_supplied():
    r = _resolver(**{"dawood": "Muhammad Dawood Umer"})
    out = collect_assignments({"Developer Name": "Minhaj Alam & Dawood"}, [DEV], r)
    assert [a["person"] for a in out] == ["Minhaj Alam", "Muhammad Dawood Umer"]


def test_splitting_happens_without_a_resolver():
    """Attribution must not depend on the database being reachable."""
    out = collect_assignments({"Developer Name": "Ahmed Qamar/Asif"}, [DEV], None)
    assert len(out) == 2


def test_one_person_in_two_roles_yields_two_assignments():
    """Existing behaviour, preserved: the workload panel counts per person per role."""
    r = _resolver(**{"madiha": "Madiha Shah Bukhari"})
    out = collect_assignments(
        {"Consultant": "Madiha", "Developer Name": "Madiha Shah Bukhari"}, [CONSULTANT, DEV], r
    )
    assert [a["role_key"] for a in out] == ["consultant", "developer_name"]
    assert {a["person_key"] for a in out} == {"madiha shah bukhari"}


def test_person_key_is_derived_from_the_resolved_name():
    """Otherwise the alias would show a merged label over unmerged totals."""
    r = _resolver(**{"babar": "Babar Ali"})
    out = collect_assignments({"Developer Name": "BABAR"}, [DEV], r)
    assert out[0]["person"] == "Babar Ali"
    assert out[0]["person_key"] == "babar ali"


def test_a_blank_cell_yields_nothing():
    assert collect_assignments({"Developer Name": "  "}, [DEV]) == []
