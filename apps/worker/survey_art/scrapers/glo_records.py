"""BLM General Land Office Records — original survey of record (SOP Phase 4).

Retrieves the original U.S. Government cadastral survey for a township: the GLO
survey plat, its field notes, and (optionally) any federal land patent(s) for the
section. These are the earliest authoritative survey documents for a tract — every
later ALTA/NSPS survey ties its basis-of-bearings back to this original 6th P.M.
survey. Runs independent of whichever Phase 3 path a county scraper took; it only
needs Section/Township/Range (and the county/state used as GLO search filters).

glorecords.blm.gov has no address or reception-number search and its results pages
are dynamic ASP.NET controls, not stable deep-linkable URLs, so — like Denver's
Kofile portal — this goes through a browser-use agent rather than direct HTTP.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from survey_art.llm import copy_agent_downloads, run_agent

logger = logging.getLogger(__name__)

_GLO_URL = "https://glorecords.blm.gov/default.aspx"


def _split_township_or_range(value: str) -> tuple[str, str]:
    """'5N' -> ('5', 'N'); '67W' -> ('67', 'W'). Empty input -> ('', '')."""
    m = re.match(r"^\s*(\d+)\s*([NSEW])\s*$", value.strip(), re.IGNORECASE)
    if not m:
        return value.strip(), ""
    return m.group(1), m.group(2).upper()


async def fetch_glo_records(
    *,
    state: str,
    county: str,
    section: str,
    township: str,
    range_: str,
    dest_dir: Path,
    meridian: str = "6th PM",
) -> tuple[list[tuple[Path, str]], dict, float, int, int]:
    """Download the GLO original survey plat, field notes, and any land patents.

    `township`/`range_` are SOP form ("5N", "67W") as stored on `ParcelInfo`.
    Returns `([(path, doc_type)], log, cost_usd, input_tokens, output_tokens)`,
    `log` being the Step 4.7 record. A missing/incomplete GLO record is logged
    and returned as an empty list.
    """
    township_num, township_dir = _split_township_or_range(township)
    range_num, range_dir = _split_township_or_range(range_)
    if not (township_num and range_num):
        logger.info("GLO records: no usable Township/Range (%r/%r) — skipping.", township, range_)
        return [], {}, 0.0, 0, 0

    township_label = f"{township_num} {township_dir or 'N'}"
    range_label = f"{range_num} {range_dir or 'W'}"

    task = (
        f"You are researching the original U.S. Government cadastral survey of record "
        f"for a parcel, from the BLM General Land Office Records site.\n\n"
        f"Target: State={state}, County={county}, Meridian={meridian}, "
        f"Township={township_label}, Range={range_label}, Section={section or '(any)'}.\n\n"
        f"STEP 1 — Go to {_GLO_URL} and click 'Search Documents'.\n\n"
        f"STEP 2 — Retrieve the Survey Plat:\n"
        f"  Use the 'Search Documents By Type' tab. Select category 'Surveys'.\n"
        f"  Under Location enter State='{state}', County='{county}'.\n"
        f"  Under Land Description enter Township='{township_label}', Range='{range_label}', "
        f"Meridian='{meridian}'" + (f", Section='{section}'" if section else "") + ".\n"
        f"  Click Search. In the results, open the 'Original Survey' row for this "
        f"Township/Range.\n"
        f"  On the Survey Details page, open the 'Plat Image' tab. Use the Full Screen "
        f"Viewer's download/PDF icon to render and download the plat as a PDF "
        f"(it may show 'Generating PDF...Please wait' before the download starts).\n\n"
        f"STEP 3 — Retrieve the Field Notes:\n"
        f"  From the Survey Details page's 'Related Documents' tab, open the Field Note "
        f"volume linked for the same Township/Range.\n"
        f"  On the Field Note Volume Details page, use the page index to find the page(s) "
        f"covering Section {section or '(any)'}, then use the viewer's save/download icon "
        f"to download those field-note page images.\n\n"
        f"STEP 4 (optional) — Retrieve federal Land Patents, if any are indexed:\n"
        f"  Return to 'Search Documents By Type', select category 'Patents', and search the "
        f"same Location and Land Description. If any patents match, open each one, view the "
        f"Patent Image, and download it. Skip this step entirely if the search returns no "
        f"results — do not treat that as an error.\n\n"
        f"Download every file directly from each viewer's own save/download icon — do NOT "
        f"use the browser's Print function, Ctrl+P, or 'Save Page As'.\n\n"
        f"When finished, reply with one line per downloaded file and nothing else:\n"
        f"  PLAT | <survey accession or DM ID> | <downloaded file name>\n"
        f"  FIELD_NOTES | <field note volume> pages <page numbers> | <downloaded file name>\n"
        f"  PATENT | <patent document number> | <downloaded file name>\n"
        f"If nothing could be downloaded, reply NONE."
    )

    try:
        agent, answer, (cost, in_tok, out_tok) = await run_agent(task)
    except Exception as exc:  # the rest of the run's documents still upload
        logger.warning("GLO records agent failed: %s", exc)
        return [], {"error": str(exc)}, 0.0, 0, 0
    downloads = copy_agent_downloads(agent, dest_dir)
    tr = f"T{township_label}R{range_label}".replace(" ", "")
    rows = _parse_answer(answer)
    files: list[tuple[Path, str]] = []
    for i, path in enumerate(downloads, 1):
        kind, ref = _match_download(path, rows)
        name = {
            "PLAT": f"glo_survey_plat_{tr}",
            "FIELD_NOTES": f"glo_field_notes_{tr}",
            "PATENT": f"glo_patent_{_safe(ref) or i}",
        }[kind]
        target = dest_dir / f"{name}{path.suffix.lower()}"
        if target.exists():  # several field-note pages, or two plats for the township
            target = dest_dir / f"{name}_{i}{path.suffix.lower()}"
        files.append((path.rename(target), _DOC_TYPES[kind]))

    log = {
        "section_township_range": f"{section}-{township}-{range_} ({meridian})",
        "records": [{"kind": _DOC_TYPES[k], "reference": ref} for k, ref, _ in rows],
        "files": [p.name for p, _ in files],
        "note": (
            "The GLO plat and field notes are the original survey of record — cross-check "
            "them against the ALTA's basis of bearings and the recovered section corners."
        ),
    }
    logger.info("GLO records: %d file(s) for %s; agent reported %r", len(files), tr, answer)
    return files, log, cost, in_tok, out_tok


_DOC_TYPES = {
    "PLAT": "GLO Survey Plat",
    # "Survey" in the name files it with the plats rather than under Other.
    "FIELD_NOTES": "GLO Survey Field Notes",
    "PATENT": "GLO Land Patent",
}


def _parse_answer(answer: str) -> list[tuple[str, str, str]]:
    """The agent's `KIND | reference | file name` lines -> [(kind, reference, file)]."""
    rows = []
    for line in answer.splitlines():
        parts = [p.strip(" -*`") for p in line.split("|")]
        kind = parts[0].upper().replace(" ", "_")
        if kind in _DOC_TYPES and len(parts) >= 2:
            rows.append((kind, parts[1], parts[2] if len(parts) > 2 else ""))
    return rows


def _match_download(path: Path, rows: list[tuple[str, str, str]]) -> tuple[str, str]:
    """Which reported record a downloaded file is. The agent names the file it
    saved; when it doesn't (or names it differently), fall back to the file name,
    and a file nobody claimed is the plat — the one document every township has."""
    name = path.name.lower()
    for kind, ref, file in rows:
        if file and (file.lower() == name or Path(file).stem.lower() in name):
            return kind, ref
    if "patent" in name:
        return "PATENT", ""
    if "field" in name or "fn" in path.stem.lower().split("_"):
        return "FIELD_NOTES", ""
    return "PLAT", ""


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
