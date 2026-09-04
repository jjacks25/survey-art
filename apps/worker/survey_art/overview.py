"""Per-property overview.json — incremental store written throughout scraping.

The overview file lives at `{tmp}/{county_key}/{address_slug}/overview.json`
and accumulates data as the pipeline progresses:

  Phase 1  →  identify_results, account_information, owners, property_report
  Phase 2  →  document_history (parsed table rows, not screenshots)
  Phase 3  →  per-document download status (downloaded_to, error, file_size)

Any phase can also read prior phases' data — e.g. Phase 3 reads
document_history to know which receptions to fetch.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class Overview:
    """Incremental JSON store for per-property scrape data.

    Each method that mutates state flushes to disk immediately so partial
    progress is preserved if a later phase crashes.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
                logger.info("Overview: loaded existing %s", path)
            except json.JSONDecodeError as exc:
                logger.warning("Overview: %s is corrupt, starting fresh: %s", path, exc)
                self.data = {}

    def set_section(self, section: str, value: dict | list) -> None:
        """Replace `section` wholesale."""
        self.data[section] = value
        self._touch()
        self.save()

    def merge_section(self, section: str, value: dict) -> None:
        """Shallow-merge `value` into an existing dict section."""
        existing = self.data.get(section)
        if not isinstance(existing, dict):
            existing = {}
        existing.update(value)
        self.data[section] = existing
        self._touch()
        self.save()

    def append_to(self, section: str, value: dict) -> None:
        """Append `value` to a list section (creating the list if needed)."""
        existing = self.data.setdefault(section, [])
        if not isinstance(existing, list):
            raise TypeError(f"Cannot append to non-list section {section!r}")
        existing.append(value)
        self._touch()
        self.save()

    def update_list_item(
        self, section: str, match_key: str, match_value: str, updates: dict
    ) -> bool:
        """Find the dict in list `section` where `match_key == match_value` and merge `updates` into it.

        Returns True if a match was found and updated.
        """
        items = self.data.get(section, [])
        if not isinstance(items, list):
            return False
        for item in items:
            if isinstance(item, dict) and item.get(match_key) == match_value:
                item.update(updates)
                self._touch()
                self.save()
                return True
        return False

    def get(self, section: str, default: Any = None) -> Any:
        return self.data.get(section, default)

    def _touch(self) -> None:
        meta = self.data.setdefault("meta", {})
        meta["last_updated"] = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=False))


def overview_path(tmp_dir: Path, county_key: str, address_slug: str) -> Path:
    """Canonical location for a property's overview.json."""
    return tmp_dir / county_key / address_slug / "overview.json"
