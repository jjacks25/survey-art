"""Tests for the worker's best-effort overview.json (property metadata) loader."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from survey_art.worker import _load_overview


def test_load_overview_returns_none_when_absent(tmp_path: Path):
    assert _load_overview(tmp_path) is None


def test_load_overview_finds_nested_file(tmp_path: Path):
    nested = tmp_path / "CO_weld" / "123-main-st"
    nested.mkdir(parents=True)
    (nested / "overview.json").write_text(json.dumps({"meta": {"account": "R123"}}))

    result = _load_overview(tmp_path)

    assert result == {"meta": {"account": "R123"}}


def test_load_overview_parses_floats_as_decimal(tmp_path: Path):
    (tmp_path / "overview.json").write_text(json.dumps({"score": 1.5}))

    result = _load_overview(tmp_path)

    assert result["score"] == Decimal("1.5")


def test_load_overview_returns_none_on_corrupt_json(tmp_path: Path):
    (tmp_path / "overview.json").write_text("not json")

    assert _load_overview(tmp_path) is None
