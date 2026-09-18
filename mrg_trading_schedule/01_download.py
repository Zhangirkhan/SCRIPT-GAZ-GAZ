#!/usr/bin/env python3
"""Download: График торгов МРГ
Source: https://korem.kz/ru/news/grafik-torgov-mrg
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, unquote

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://korem.kz"
LIST_PATH = "/ru/news/grafik-torgov-mrg"
SOURCE_KEY = "grafik-torgov"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PDF_DIR = DATA_DIR / "source"
MANIFEST_PATH = DATA_DIR / "manifest.jsonl"

FILE_ID_RE = re.compile(r"/file/download/(\d+)")
YEAR_RE = re.compile(r"[?&]year=(\d{4})")
DATE_RE = re.compile(r"(\d{2})[.\-](\d{2})[.\-](\d{2,4})")
SAFE_NAME_RE = re.compile(r"[^\w\-.#()\[\] +а-яА-ЯёЁәіңғүұқөһӘІҢҒҮҰҚӨҺ]+", re.UNICODE)


@dataclass
class ListingItem:
    year: int
    file_id: int
    url: str
    title: str


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


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


def discover_years(html: str) -> list[int]:
    years: set[int] = set()
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = YEAR_RE.search(href)
        if m:
            years.add(int(m.group(1)))
        text = a.get_text(strip=True)
        if text.isdigit() and len(text) == 4:
            years.add(int(text))
    return sorted(years)


def parse_listing(html: str, year: int) -> list[ListingItem]:
    soup = BeautifulSoup(html, "lxml")
    items: list[ListingItem] = []
    seen: set[int] = set()
    for a in soup.find_all("a", href=True):
        m = FILE_ID_RE.search(a["href"])
        if not m:
            continue
        file_id = int(m.group(1))
        if file_id in seen:
            continue
        seen.add(file_id)
        title = " ".join(a.get_text(" ", strip=True).split()) or f"file_{file_id}"
        items.append(
            ListingItem(year=year, file_id=file_id, url=urljoin(BASE_URL, a["href"]), title=title)
        )
    return items


def iter_items(sess: requests.Session, delay: float, years: list[int] | None) -> Iterable[ListingItem]:
    first_html = fetch_html(sess, f"{BASE_URL}{LIST_PATH}")
    use_years = years if years else discover_years(first_html)
    print(f"years: {use_years}")
    for year in use_years:
        time.sleep(delay)
        html = fetch_html(sess, f"{BASE_URL}{LIST_PATH}?year={year}")
        items = parse_listing(html, year)
        print(f"{year}: {len(items)} files")
        yield from items


def date_from_title(title: str) -> str | None:
    m = DATE_RE.search(title)
    if not m:
        return None
    d, mo, y = m.groups()
    year = int(y)
    if year < 100:
        year += 2000
    try:
        return datetime(year, int(mo), int(d)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def safe_stem(title: str, file_id: int) -> str:
    stem = SAFE_NAME_RE.sub("_", title).strip(" ._")
    stem = re.sub(r"_+", "_", stem)
    if len(stem) > 120:
        stem = stem[:120].rstrip("_")
    return stem or f"file_{file_id}"


def pdf_path_for(item: ListingItem) -> Path:
    date = date_from_title(item.title)
    stem = safe_stem(item.title, item.file_id)
    name = f"{date}_{item.file_id}_{stem}.pdf" if date else f"{item.year}_{item.file_id}_{stem}.pdf"
    return PDF_DIR / str(item.year) / name


def load_done() -> set[int]:
    done: set[int] = set()
    if MANIFEST_PATH.exists():
        for line in MANIFEST_PATH.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("status") == "ok" and "file_id" in row:
                done.add(int(row["file_id"]))
    if PDF_DIR.exists():
        for p in PDF_DIR.rglob("*.pdf"):
            m = re.search(r"_(\d+)_", p.name) or re.search(r"_(\d+)\.pdf$", p.name)
            if m:
                done.add(int(m.group(1)))
    return done


def append_manifest(row: dict) -> None:
    with MANIFEST_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def download_file(sess: requests.Session, item: ListingItem, dest: Path, retries: int = 4) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            with sess.get(item.url, timeout=180, stream=True) as resp:
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
    parser.add_argument("--years", nargs="+", type=int, default=None)
    parser.add_argument("--delay", type=float, default=0.4)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    sess = session()
    already = load_done()
    downloaded = skipped = failed = 0
    seen: set[int] = set()

    for item in iter_items(sess, args.delay, args.years):
        if item.file_id in seen:
            continue
        seen.add(item.file_id)
        dest = pdf_path_for(item)
        if item.file_id in already or dest.exists():
            skipped += 1
            continue
        time.sleep(args.delay)
        try:
            download_file(sess, item, dest)
            append_manifest({
                "source": SOURCE_KEY,
                "year": item.year,
                "file_id": item.file_id,
                "url": item.url,
                "title": item.title,
                "path": str(dest.relative_to(ROOT)),
                "status": "ok",
                "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            })
            downloaded += 1
            print(f"  OK  {item.file_id} → {dest.relative_to(ROOT)}")
            if args.limit and downloaded >= args.limit:
                break
        except Exception as exc:  # noqa: BLE001
            failed += 1
            append_manifest({
                "file_id": item.file_id,
                "url": item.url,
                "title": item.title,
                "status": "error",
                "error": str(exc),
            })
            print(f"  ERR {item.file_id}: {exc}")

    print(f"Done. downloaded={downloaded} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
