"""Download document URLs to ./tmp/ with an organized directory structure."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from survey_art.geocode import USER_AGENT, County

MAX_CONCURRENT_DOWNLOADS = 16


def slug(s: str) -> str:
    """Safe path segment from string."""
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[-\s]+", "_", s).strip("_")
    return s[:80] or "property"


def download_dir(county: County, address_one_line: str, base: Path) -> Path:
    """Return the directory path where files for this address will be saved."""
    return base / county.key() / slug(address_one_line.replace(",", " "))


def _filename(url: str, resp: httpx.Response, index: int) -> str:
    """A safe filename for `url`: Content-Disposition, else the URL's last path
    segment, else a numbered fallback; extension from Content-Type if missing."""
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*?=(?:utf-8'')?[\"']?([^\"';]+)", cd, re.IGNORECASE)
    name = (m.group(1) if m else urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]).strip()
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = slug(stem) if stem else f"document_{index}"
    if not ext:
        ct = resp.headers.get("content-type", "")
        ext = "pdf" if "pdf" in ct else "jpg" if "image" in ct else "bin"
    return f"{stem}.{slug(ext)}"


async def download_urls(urls: list[str], dest_dir: Path) -> list[Path]:
    """Download every URL into `dest_dir` in parallel. Failed downloads are skipped."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

    async def one(client: httpx.AsyncClient, index: int, url: str) -> Path | None:
        async with semaphore:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError:
                return None
        dest = dest_dir / _filename(url, resp, index)
        dest.write_bytes(resp.content)
        return dest

    async with httpx.AsyncClient(
        follow_redirects=True, timeout=60.0, headers={"User-Agent": USER_AGENT}
    ) as client:
        results = await asyncio.gather(*(one(client, i, u) for i, u in enumerate(urls)))
    return [p for p in results if p is not None]
