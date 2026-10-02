"""Defines what documents are relevant for land survey research.

`prompt_fragment()` is the single source of truth for what the browser-use agents
look for and download — it's injected into every agent task, so the LLM knows both
the document types to find and the file formats to collect.
"""

from __future__ import annotations

# Document types a land survey technician needs to research a project.
# These map directly to document type names used in county recording systems.
SURVEY_DOCUMENT_TYPES: list[str] = [
    # Survey documents
    "Land Survey Plat",
    "Improvement Survey Plat",
    "Improvement Location Certificate",
    "ALTA/NSPS Survey",
    "Boundary Survey",
    "Topographic Survey",
    "Construction Survey",
    # Subdivision & exemption plats
    "Subdivision Plat",
    "Subdivision Exemption",
    "Condominium Plat",
    "Amended Plat",
    "Correction Plat",
    "Vacating Plat",
    # Deeds (establish vesting / ownership chain)
    "Warranty Deed",
    "Special Warranty Deed",
    "Quit Claim Deed",
    "Deed of Trust",
    "Release of Deed of Trust",
    "Personal Representative Deed",
    "Trustee Deed",
    # Easements & right-of-way
    "Easement",
    "Easement Agreement",
    "Right of Way Dedication",
    "Right of Way Easement",
    "Access Easement",
    "Utility Easement",
    "Drainage Easement",
    "Conservation Easement",
    # Public land / government records
    "Notice of Condemnation",
    "Resolution",
    "Ordinance",
    "Certificate of Dedication",
    # Liens & encumbrances (affect title)
    "Lien",
    "Lis Pendens",
    "Notice of Election and Demand",
]

# File formats that can contain survey-relevant content.
# Not restricted to PDF — county systems also serve TIF scans, CAD files,
# GIS exports, and image files.
SURVEY_FILE_EXTENSIONS: list[str] = [
    # Documents
    ".pdf",
    ".doc",
    ".docx",
    # Scanned images (common for older recorded documents)
    ".tif",
    ".tiff",
    ".jpg",
    ".jpeg",
    ".png",
    # CAD / survey drawings
    ".dwg",
    ".dxf",
    ".dgn",
    # GIS / spatial data
    ".shp",
    ".kml",
    ".kmz",
    ".geojson",
    ".gdb",
    # Compressed archives (often wrap shapefiles or CAD sets)
    ".zip",
]


def prompt_fragment() -> str:
    """The instruction fragment injected into every browser-use agent task: what
    document types to find and what formats to collect."""
    return (
        f"Collect and download any of the following document types: "
        f"{', '.join(SURVEY_DOCUMENT_TYPES)}. "
        f"Accept files in any of these formats: {', '.join(SURVEY_FILE_EXTENSIONS)}. "
        "Survey plats (Land Survey Plat, Subdivision Plat, Improvement Survey Plat, "
        "ALTA/NSPS Survey, Boundary Survey) are the highest priority — never skip these. "
        "Deeds and easements are also important. "
        "Do not collect navigation links, search forms, help pages, or unrelated records."
    )
