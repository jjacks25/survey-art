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
import shutil
from pathlib import Path

from browser_use import Agent

from survey_art.llm import agent_cost, get_llm

logger = logging.getLogger(__name__)

_GLO_URL = "https://glorecords.blm.gov/default.aspx"

_VALID_SUFFIXES = {".pdf", ".tif", ".tiff", ".jpg", ".jpeg", ".png"}


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
) -> tuple[list[Path], float, int, int]:
    """Download the GLO original survey plat, field notes, and any land patents.

    `township`/`range_` are SOP form ("5N", "67W") as stored on `ParcelInfo`.
    Returns (saved_paths, cost_usd, input_tokens, output_tokens). Never raises —
    a missing/incomplete GLO record is logged and returned as an empty list.
    """
    township_num, township_dir = _split_township_or_range(township)
    range_num, range_dir = _split_township_or_range(range_)
    if not (township_num and range_num):
        logger.info("GLO records: no usable Township/Range (%r/%r) — skipping.", township, range_)
        return [], 0.0, 0, 0

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
        f"Meridian='{meridian}'"
        + (f", Section='{section}'" if section else "")
        + ".\n"
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
        f"When finished, say 'Done — downloaded N file(s): survey plat, field notes, "
        f"and N_patents patent(s).'"
    )

    agent = Agent(task=task, llm=get_llm(), use_thinking=False, calculate_cost=True)
    await agent.run()
    cost, in_tok, out_tok = agent_cost(agent)

    local_paths = [
        Path(p)
        for p in (agent.available_file_paths or [])
        if Path(p).suffix.lower() in _VALID_SUFFIXES and Path(p).exists()
    ]
    logger.info("GLO records: browser downloaded %d file(s)", len(local_paths))

    saved: list[Path] = []
    if local_paths:
        dest_dir.mkdir(parents=True, exist_ok=True)
        for src in local_paths:
            dst = dest_dir / src.name
            shutil.copy(src, dst)
            saved.append(dst)

    return saved, cost, in_tok, out_tok
