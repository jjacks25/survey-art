"""Download document URLs to ./tmp/ with an organized directory structure."""

import asyncio
import re
from pathlib import Path

import httpx

from survey_art.geocode import County
from survey_art.types import DocumentLink

USER_AGENT = "LandSurveyScraper/1.0 (property records research)"
DEFAULT_TMP = Path("tmp")
MAX_CONCURRENT_DOWNLOADS = 16


def _slug(s: str) -> str:
    """Safe path segment from string."""
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[-\s]+", "_", s).strip("_")
    return s[:80] or "property"


def _address_slug(address_one_line: str) -> str:
    return _slug(address_one_line.replace(",", " "))


def download_dir(county: County, address_one_line: str, base: Path = DEFAULT_TMP) -> Path:
    """Return the directory path where files for this address will be saved."""
    county_key = county.key()
    addr_slug = _address_slug(address_one_line)
    return base / county_key / addr_slug


def _filename_from_url(url: str, content_disposition: str | None) -> str | None:
    """Derive a safe filename from URL or Content-Disposition."""
    if content_disposition:
        for part in content_disposition.split(";"):
            part = part.strip().lower()
            if part.startswith("filename*=utf-8''"):
                name = part[15:].strip("'\"")
                break
            if part.startswith("filename="):
                name = part[9:].strip("'\"")
                break
        else:
            name = None
        if name:
            name = name.split("/")[-1]
            if name and all(c.isalnum() or c in "._-" for c in name):
                return _slug(name) or None
    path = url.split("?")[0].rstrip("/")
    if "/" in path:
        name = path.split("/")[-1]
        if name and "." in name:
            return _slug(name) or "document"
    return None


def download_file(
    link: DocumentLink,
    dest_dir: Path,
    *,
    skip_existing: bool = True,
    client: httpx.Client | None = None,
) -> Path | None:
    """
    Download one document to dest_dir. Returns path of saved file or None on failure.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    own_client = client is None
    if client is None:
        client = httpx.Client(
            follow_redirects=True, timeout=60.0, headers={"User-Agent": USER_AGENT}
        )

    try:
        resp = client.get(link.url)
        resp.raise_for_status()
        cd = resp.headers.get("content-disposition")
        name = _filename_from_url(link.url, cd)
        if not name:
            ext = ".bin"
            for e in (".pdf", ".tif", ".tiff", ".jpg", ".jpeg", ".png"):
                if e in link.url.lower():
                    ext = e
                    break
            name = _slug(link.text)[:40] or "document" + ext
        elif "." not in name:
            ct = resp.headers.get("content-type", "")
            if "pdf" in ct:
                name += ".pdf"
            elif "image" in ct or "jpeg" in ct or "png" in ct:
                name += ".jpg"
            else:
                name += ".bin"
        dest = dest_dir / name
        if skip_existing and dest.exists():
            return dest
        dest.write_bytes(resp.content)
        return dest
    except Exception:
        return None
    finally:
        if own_client:
            client.close()


async def _download_one(
    link: DocumentLink,
    dest_dir: Path,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    skip_existing: bool,
) -> Path | None:
    """Download a single file with semaphore-limited concurrency."""
    async with semaphore:
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            resp = await client.get(link.url)
            resp.raise_for_status()
        except Exception:
            return None
        cd = resp.headers.get("content-disposition")
        name = _filename_from_url(link.url, cd)
        if not name:
            ext = ".bin"
            for e in (".pdf", ".tif", ".tiff", ".jpg", ".jpeg", ".png"):
                if e in link.url.lower():
                    ext = e
                    break
            name = _slug(link.text)[:40] or "document" + ext
        elif "." not in name:
            ct = resp.headers.get("content-type", "")
            if "pdf" in ct:
                name += ".pdf"
            elif "image" in ct or "jpeg" in ct or "png" in ct:
                name += ".jpg"
            else:
                name += ".bin"
        dest = dest_dir / name
        if skip_existing and dest.exists():
            return dest
        dest.write_bytes(resp.content)
        return dest


def download_all(
    links: list[DocumentLink],
    county: County,
    address_one_line: str,
    base: Path = DEFAULT_TMP,
    skip_existing: bool = True,
) -> list[Path]:
    """
    Download all document links (sync). For parallel downloads use download_all_async.
    """
    dest = download_dir(county, address_one_line, base)
    saved: list[Path] = []
    client = httpx.Client(follow_redirects=True, timeout=60.0, headers={"User-Agent": USER_AGENT})
    try:
        for link in links:
            p = download_file(link, dest, skip_existing=skip_existing, client=client)
            if p:
                saved.append(p)
    finally:
        client.close()
    return saved


async def download_all_async(
    links: list[DocumentLink],
    county: County,
    address_one_line: str,
    base: Path = DEFAULT_TMP,
    skip_existing: bool = True,
) -> list[Path]:
    """
    Download all document links in parallel using async httpx.
    Connection pool and semaphore limit concurrency for speed without overwhelming hosts.
    """
    dest = download_dir(county, address_one_line, base)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=60.0,
        headers={"User-Agent": USER_AGENT},
        limits=httpx.Limits(max_connections=MAX_CONCURRENT_DOWNLOADS),
    ) as client:
        tasks = [_download_one(link, dest, client, semaphore, skip_existing) for link in links]
        results = await asyncio.gather(*tasks)
    return [p for p in results if p is not None]
