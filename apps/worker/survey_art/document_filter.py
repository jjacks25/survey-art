"""Defines what documents are relevant for land survey research.

The DocumentFilter is the single source of truth for what the agent should
look for and download. It gets injected into every browser-use agent task as
a prompt fragment, so the LLM understands both the document types to find and
the file formats to collect.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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


@dataclass
class DocumentFilter:
    """Controls what the browser-use agent looks for and downloads."""

    doc_types: list[str] = field(default_factory=lambda: list(SURVEY_DOCUMENT_TYPES))
    file_extensions: list[str] = field(default_factory=lambda: list(SURVEY_FILE_EXTENSIONS))

    def to_prompt_fragment(self) -> str:
        """
        Returns the instruction fragment injected into every agent task.
        Tells the LLM exactly what document types to find and what formats to collect.
        """
        types_str = ", ".join(self.doc_types)
        exts_str = ", ".join(self.file_extensions)
        return (
            f"Collect and download any of the following document types: {types_str}. "
            f"Accept files in any of these formats: {exts_str}. "
            "Survey plats (Land Survey Plat, Subdivision Plat, Improvement Survey Plat, "
            "ALTA/NSPS Survey, Boundary Survey) are the highest priority — never skip these. "
            "Deeds and easements are also important. "
            "Do not collect navigation links, search forms, help pages, or unrelated records."
        )


# Default filter used by all scrapers unless overridden.
DEFAULT_FILTER = DocumentFilter()
