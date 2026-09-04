"""CLI: resolve address to county and download property records to ./tmp/."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from land_survey_scraper import __version__
from land_survey_scraper.pipeline import COUNTY_SCRAPERS, run

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    parser = argparse.ArgumentParser(
        description="Resolve address to county, scrape property records, download to ./tmp/.",
    )
    parser.add_argument(
        "address", nargs="+", help="Street address (e.g. '123 Main St, Greeley, CO 80631')"
    )
    parser.add_argument(
        "-t", "--tmp", type=Path, default=Path("tmp"), help="Output directory (default: ./tmp)"
    )
    parser.add_argument(
        "--no-skip-existing", action="store_true", help="Re-download existing files"
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output")
    parser.add_argument(
        "--county",
        metavar="KEY",
        help=(
            "Override county key, bypassing geocoding "
            f"(supported: {', '.join(COUNTY_SCRAPERS)})"
        ),
    )
    parser.add_argument(
        "--str",
        dest="str_input",
        metavar="S,T,R",
        default="",
        help=(
            "Weld only: Section,Township,Range PLSS lookup (e.g. '15,5N,67W'). "
            "SOP Priority 2/3 — used when address is unknown."
        ),
    )
    parser.add_argument(
        "--owner",
        dest="owner_input",
        metavar="NAME",
        default="",
        help=(
            "Weld only: owner-name lookup (e.g. 'STRATUS DELANTERO LLC'). "
            "SOP Priority 4 — last resort when address and S/T/R fail."
        ),
    )
    parser.add_argument(
        "--sop-strict",
        action="store_true",
        help=(
            "Weld only: drive the literal SOP browser walk (GIS Hub → "
            "Interactive Maps → Property Portal → Identify Results) "
            "instead of the HTTP shortcut."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    args = parser.parse_args()
    address = " ".join(args.address)

    saved, err = run(
        address,
        tmp_dir=args.tmp,
        skip_existing=not args.no_skip_existing,
        quiet=args.quiet,
        county_override=args.county,
        str_input=args.str_input,
        owner_input=args.owner_input,
        sop_strict=args.sop_strict,
    )

    if err:
        logger.error(err)
        sys.exit(1)
    if args.quiet:
        logger.info("Saved %s file(s) to %s", len(saved), args.tmp)
        for p in saved:
            logger.info("  %s", p)


if __name__ == "__main__":
    main()
