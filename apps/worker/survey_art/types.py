"""Shared data types."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DocumentLink:
    """A discovered downloadable document."""

    url: str
    text: str
    content_type: str
