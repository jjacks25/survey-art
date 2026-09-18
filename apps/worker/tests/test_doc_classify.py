"""Tests for doc_classify — pinned against the recorder's real vocabulary.

`weld_document_types.json` is every distinct Document Type the recorder returned
for one section (S32-T5N-R65W, 872 documents, 114 types) with how many times each
occurred. It's a fixture, not a guess: regenerate it by sweeping a section, not by
pasting classifier output.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from survey_art.doc_classify import (
    CATEGORIES,
    DEEDS,
    EASEMENTS,
    FINANCING,
    GOVERNMENT,
    LIENS,
    MINERAL,
    OTHER,
    SURVEYS,
    WATER,
    classify,
    reception_sort_key,
)

VOCABULARY: list[tuple[str, int]] = [
    (label, count)
    for label, count in json.loads(
        (Path(__file__).parent / "weld_document_types.json").read_text()
    )
]


def test_every_real_document_type_lands_in_a_known_category():
    assert {classify(label) for label, _ in VOCABULARY} <= set(CATEGORIES)


def test_almost_nothing_falls_through_to_other():
    """Other is for genuine miscellany (CONSENT, WAIVER, LETTER). If a rule
    stops matching, this is what notices — a category nobody opens is where
    documents go to be missed."""
    counts = Counter(classify(label) for label, _ in VOCABULARY)
    assert counts[OTHER] / len(VOCABULARY) < 0.15


def test_the_specific_reading_beats_the_general_one():
    # Every one of these contains a keyword from a category it must NOT land in.
    assert classify("MINERAL & ROYALTY DEED") == MINERAL
    assert classify("MINERAL QUIT CLAIM DEED") == MINERAL
    assert classify("ASSIGNMENT DEED OF TRUST") == FINANCING
    assert classify("PARTIAL RELEASE DEED OF TRUST") == FINANCING
    assert classify("RELEASE INHERITANCE TAX LIEN") == LIENS
    assert classify("ORDINANCE (RELATED TO A MAP)") == GOVERNMENT
    assert classify("ANNEXATION PLAT") == SURVEYS
    assert classify("EASEMENT RIGHT OF WAY & SURFACE USE AGM") == EASEMENTS
    assert classify("DRY UP COVENANT") == WATER


def test_the_documents_a_surveyor_opens_first():
    assert classify("SURVEY") == SURVEYS
    assert classify("RECORDED EXEMPTION") == SURVEYS
    assert classify("JOINT TENANCY WARRANTY DEED") == DEEDS
    assert classify("RIGHT OF WAY") == EASEMENTS
    assert classify("PETITION FOR IRRIGATION WATER ALLOTMENT") == WATER


def test_a_model_written_description_classifies_like_a_recorder_label():
    # Schedule B-2 exceptions carry the description read off the citing
    # document rather than a recorder type — same rules have to cope.
    assert classify("20' sewer easement") == EASEMENTS
    assert classify("Mineral Deed filed of record on August 9, 2021") == MINERAL
    assert classify("") == OTHER


def test_receptions_sort_as_numbers():
    receptions = ["999999", "1000000", "02050963", "Book 571 Page 55"]
    assert sorted(receptions, key=reception_sort_key) == [
        "999999",
        "1000000",  # string-sorted this would come before 999999
        "02050963",  # zero-padded, still sorts as 2050963
        "Book 571 Page 55",  # not a number at all — sorts last, not as 57155
    ]
