#!/usr/bin/env python3
"""Download KOREM EPO trading-result PDFs for years 2023–2026."""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.korem.kz"
LIST_PATH = "/ru/news/rezultaty-torgov-epo"
YEARS = (2023, 2024, 2025, 2026)
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PDF_DIR = DATA_DIR / "pdf"
MANIFEST_PATH = DATA_DIR / "manifest.jsonl"

TITLE_DATE_RE = re.compile(
    r"Результат[ыа].*?на\s+(\d{2})-(\d{2})-(\d{2})",
    re.IGNORECASE,
)
FILE_ID_RE = re.compile(r"/file/download/(\d+)")


@dataclass
class ListingItem:
    file_id: int
    url: str
    title: str
    trade_date: str  # YYYY-MM-DD (best-effort from title)


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def parse_title_date(title: str) -> str | None:
    m = TITLE_DATE_RE.search(title)
    if not m:
        return None
    day, month, yy = m.groups()
    year = 2000 + int(yy)
    try:
        return datetime(year, int(month), int(day)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def fetch_html(sess: requests.Session, url: str, retries: int = 4) -> str:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = sess.get(url, timeout=60)
            if resp.status_code >= 500:
                raise requests.HTTPError(f"{resp.status_code} for {url}")
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_exc}")


def max_page(html: str) -> int:
    pages = [int(p) for p in re.findall(r"[?&](?:amp;)?page=(\d+)", html)]
    return max(pages) if pages else 1


def parse_listing(html: str) -> list[ListingItem]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[ListingItem] = []
    seen: set[int] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = FILE_ID_RE.search(href)
        if not m:
            continue
        file_id = int(m.group(1))
        if file_id in seen:
            continue
        seen.add(file_id)
        title = " ".join(a.get_text(" ", strip=True).split())
        if not title:
            title = a.get("download") or f"file_{file_id}"
        trade_date = parse_title_date(title) or "unknown"
        url = urljoin(BASE_URL, href)
        items.append(
            ListingItem(
                file_id=file_id,
                url=url,
                title=title,
                trade_date=trade_date,
            )
        )
    return items


def iter_year_items(
    sess: requests.Session, year: int, delay: float
) -> Iterable[ListingItem]:
    first_url = f"{BASE_URL}{LIST_PATH}?year={year}&page=1"
    first_html = fetch_html(sess, first_url)
    last = max_page(first_html)
    print(f"[{year}] pages: {last}")

    for page in range(1, last + 1):
        if page == 1:
            html = first_html
        else:
            time.sleep(delay)
            url = f"{BASE_URL}{LIST_PATH}?year={year}&page={page}"
            html = fetch_html(sess, url)
        items = parse_listing(html)
        print(f"[{year}] page {page}/{last}: {len(items)} files")
        yield from items


def pdf_path_for(item: ListingItem) -> Path:
    if item.trade_date != "unknown":
        y, m, _ = item.trade_date.split("-")
        return PDF_DIR / y / m / f"{item.trade_date}_{item.file_id}.pdf"
    return PDF_DIR / "unknown" / f"{item.file_id}.pdf"


def load_manifest_ids() -> set[int]:
    done: set[int] = set()
    if not MANIFEST_PATH.exists():
        return done
    with MANIFEST_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("status") == "ok" and "file_id" in row:
                done.add(int(row["file_id"]))
    return done


def append_manifest(row: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with MANIFEST_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def download_file(
    sess: requests.Session, item: ListingItem, dest: Path, retries: int = 4
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            with sess.get(item.url, timeout=120, stream=True) as resp:
                if resp.status_code >= 500:
                    raise requests.HTTPError(f"{resp.status_code} for {item.url}")
                resp.raise_for_status()
                with tmp.open("wb") as out:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            out.write(chunk)
            tmp.replace(dest)
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Download failed {item.file_id}: {last_exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--years",
        nargs="+",
        type=int,
        default=list(YEARS),
        help="Years to download (default: 2023 2024 2025 2026)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.4,
        help="Delay between HTTP requests in seconds",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Stop after N successful downloads (0 = no limit)",
    )
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)

    sess = session()
    already = load_manifest_ids()
    # Also treat existing pdf files as done
    for p in PDF_DIR.rglob("*.pdf"):
        m = re.search(r"_(\d+)\.pdf$", p.name)
        if m:
            already.add(int(m.group(1)))

    downloaded = 0
    skipped = 0
    failed = 0
    seen_ids: set[int] = set()

    for year in args.years:
        for item in iter_year_items(sess, year, args.delay):
            if item.file_id in seen_ids:
                continue
            seen_ids.add(item.file_id)

            dest = pdf_path_for(item)
            if item.file_id in already or dest.exists():
                skipped += 1
                continue

            time.sleep(args.delay)
            try:
                download_file(sess, item, dest)
                append_manifest(
                    {
                        "file_id": item.file_id,
                        "url": item.url,
                        "title": item.title,
                        "trade_date": item.trade_date,
                        "path": str(dest.relative_to(ROOT)),
                        "status": "ok",
                        "downloaded_at": datetime.utcnow().isoformat(timespec="seconds")
                        + "Z",
                    }
                )
                downloaded += 1
                print(f"  OK  {item.file_id} → {dest.relative_to(ROOT)}")
                if args.limit and downloaded >= args.limit:
                    print(f"Reached --limit={args.limit}")
                    print(
                        f"Done. downloaded={downloaded} skipped={skipped} failed={failed}"
                    )
                    return
            except Exception as exc:  # noqa: BLE001
                failed += 1
                append_manifest(
                    {
                        "file_id": item.file_id,
                        "url": item.url,
                        "title": item.title,
                        "trade_date": item.trade_date,
                        "path": str(dest.relative_to(ROOT)),
                        "status": "error",
                        "error": str(exc),
                        "downloaded_at": datetime.utcnow().isoformat(timespec="seconds")
                        + "Z",
                    }
                )
                print(f"  ERR {item.file_id}: {exc}")

    print(f"Done. downloaded={downloaded} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
