"""Sort a recorder's document-type label into a category a surveyor thinks in.

Weld's Document Types picker lists hundreds of labels — 114 distinct ones turn
up in a single section (S32-T5N-R65W, 872 documents) — and the distinctions it
draws are the recorder's, not a surveyor's: JOINT TENANCY QUIT CLAIM DEED,
CORRECTED JOINT TENANCY QUIT CLAIM DEED and MINERAL QUIT CLAIM DEED are three
types, but the first two are the chain of title and the third is not. So the
labels are matched by keyword rather than enumerated, and the rules are ordered
— **first match wins** — because the specific reading has to beat the general
one:

    MINERAL & ROYALTY DEED          -> Mineral, Oil & Gas   (not Deeds)
    ASSIGNMENT DEED OF TRUST        -> Financing            (not Deeds)
    ORDINANCE (RELATED TO A MAP)    -> Government           (not Surveys)
    EASEMENT RIGHT OF WAY & SURFACE USE AGM -> Easements

`tests/test_doc_classify.py` pins the order against the real vocabulary, so
reordering `_RULES` without re-running it will silently re-file documents.

The label is whatever the run has: the recorder's own type for anything found
by search, and for a Schedule B-2 exception the description the model read off
the citing document ("20' sewer easement"). Both are free text as far as this
module is concerned, which is why it matches keywords instead of a fixed list.
"""

from __future__ import annotations

# Display order too — the Results tab renders categories in this sequence, so
# what a surveyor reaches for first is at the top of the page.
SURVEYS = "Surveys & Plats"
EASEMENTS = "Easements & Rights of Way"
DEEDS = "Deeds & Vesting"
WATER = "Water & Ditch"
MINERAL = "Mineral, Oil & Gas"
LIENS = "Liens & Encumbrances"
GOVERNMENT = "Government & Land Use"
FINANCING = "Financing"
NOTICES = "Affidavits, Notices & Agreements"
OTHER = "Other"

CATEGORIES: list[str] = [
    SURVEYS,
    EASEMENTS,
    DEEDS,
    WATER,
    MINERAL,
    LIENS,
    GOVERNMENT,
    FINANCING,
    NOTICES,
    OTHER,
]

# Ordered: the first category with a matching keyword wins. See the module
# docstring for why the order is load-bearing.
_RULES: list[tuple[str, tuple[str, ...]]] = [
    # Before Surveys, so "ORDINANCE (RELATED TO A MAP)" isn't filed as a plat.
    (GOVERNMENT, ("ORDINANCE", "RESOLUTION", "ANNEXATION AGREEMENT", "USE BY SPECIAL REVIEW",
                  "INCLUSION ORDER", "ZONING", "CONDEMNATION", "VACATION OF")),
    (SURVEYS, ("SURVEY", "PLAT", "EXEMPTION", "LOT LINE ADJUSTMENT", "MONUMENT",
               "IMPROVEMENT LOCATION", "SUBDIVISION", "MAP OF")),
    (EASEMENTS, ("EASEMENT", "RIGHT OF WAY", "RIGHT-OF-WAY", "R/W", "ROW GRANT",
                 "SURFACE USE", "SURFACE GRANT", "ACCESS")),
    # Before Mineral: a dry-up covenant is a water matter, not an oil & gas one.
    (WATER, ("DITCH", "IRRIGATION", "WATER", "DRY UP", "DRY-UP", "RESERVOIR", "AUGMENTATION")),
    (MINERAL, ("MINERAL", "ROYALTY", "OIL", "GAS", "POOLING", "PRODUCTION", "OVERRIDING",
               "OPERATING AGREEMENT", "SEVERANCE", "WELL")),
    # Before Liens, so a release *of a deed of trust* files with the loan.
    (FINANCING, ("DEED OF TRUST", "TRUST DEED", "MORTGAGE", "MTG", "ASSIGNMENT OF RENTS",
                 "LEASES & RENTS", "DISBURSER", "PARTIAL RELEASE", "SUBORDINAT", "PROMISSORY")),
    (LIENS, ("LIEN", "LIS PENDENS", "JUDGMENT", "ELECTION & DEMAND", "TREASURER",
             "FORECLOSURE", "ENCUMBRANCE", "COVENANT", "RESTRICTION")),
    (DEEDS, ("DEED", "QUIT CLAIM", "CONVEYANCE", "PATENT", "TRANSFER")),
    (NOTICES, ("AFFIDAVIT", "NOTICE", "CERTIFICATE", "POWER OF ATTORNEY", "STATEMENT OF AUTHORITY",
               "DEATH", "MARRIAGE", "LETTERS", "MEMORANDUM", "AGREEMENT", "ASSIGNMENT")),
]


def classify(doc_type: str) -> str:
    """The category `doc_type` belongs to — always one of `CATEGORIES`."""
    upper = (doc_type or "").upper()
    if not upper.strip():
        return OTHER
    for category, keywords in _RULES:
        if any(keyword in upper for keyword in keywords):
            return category
    return OTHER


def reception_sort_key(reception: str) -> tuple[int, int, str]:
    """Sort key for a reception number: numeric where it can be, text after.

    Receptions are printed with and without leading zeros and occasionally
    aren't numbers at all (a book/page reference that reached the results list),
    so string sorting puts 1000000 before 999999 and hides a document in the
    middle of a list a surveyor is scanning by eye.
    """
    text = (reception or "").strip()
    return (0, int(text), "") if text.isdigit() else (1, 0, text.upper())
