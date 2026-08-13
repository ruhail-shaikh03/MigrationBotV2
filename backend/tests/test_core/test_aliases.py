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
