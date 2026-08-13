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
