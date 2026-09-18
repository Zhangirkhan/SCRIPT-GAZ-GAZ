#!/usr/bin/env python3
"""Shared document pipeline for KOREM auction registers and schedules."""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import pdfplumber
import requests
from bs4 import BeautifulSoup
from openpyxl import load_workbook

BASE_URL = "https://www.korem.kz"
USER_AGENT = "Mozilla/5.0 (compatible; KOREM-data-pipeline/1.0)"
DOWNLOAD_RE = re.compile(r"/file/download/(\d+)")
DATE_RE = re.compile(r"(\d{2})[.\-/](\d{2})[.\-/](\d{2,4})")
FIELDS = [
    "document_id", "document_date", "title", "source_file", "source_type",
    "sheet_or_page", "table_index", "row_index", "column_index", "value",
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def document_date(title: str) -> str:
    match = DATE_RE.search(title)
    if not match:
        return "unknown"
    day, month, year = match.groups()
    year = str(2000 + int(year)) if len(year) == 2 else year
    try:
        return datetime(int(year), int(month), int(day)).strftime("%Y-%m-%d")
    except ValueError:
        return "unknown"


def extension(response: requests.Response) -> str:
    content = response.content[:8]
    if content.startswith(b"%PDF"):
        return ".pdf"
    if content.startswith(b"PK\x03\x04"):
        return ".xlsx"
    if content.startswith(b"\xd0\xcf\x11\xe0"):
        return ".xls"
    ctype = response.headers.get("Content-Type", "").lower()
    if "pdf" in ctype:
        return ".pdf"
    if "excel" in ctype or "spreadsheet" in ctype:
        return ".xlsx"
    return ".bin"


def download_main(root: Path, *, title: str, list_path: str, years: list[int]) -> None:
    parser = argparse.ArgumentParser(description=f"Download KOREM documents: {title}")
    parser.add_argument("--years", nargs="+", type=int, default=years)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--delay", type=float, default=0.25)
    args = parser.parse_args()
    data = root / "data"
    source = data / "source"
    manifest = data / "manifest.jsonl"
    data.mkdir(parents=True, exist_ok=True)
    done = set()
    if manifest.exists():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
                if item.get("status") == "ok":
                    done.add(int(item["document_id"]))
            except (ValueError, KeyError):
                pass
    sess = requests.Session(); sess.headers["User-Agent"] = USER_AGENT
    successful = 0
    for year in args.years:
        first = sess.get(f"{BASE_URL}{list_path}?year={year}&page=1", timeout=60)
        first.raise_for_status()
        pages = [int(x) for x in re.findall(r"[?&](?:amp;)?page=(\d+)", first.text)] or [1]
        for page in range(1, max(pages) + 1):
            html = first.text if page == 1 else sess.get(
                f"{BASE_URL}{list_path}?year={year}&page={page}", timeout=60
            ).text
            soup = BeautifulSoup(html, "html.parser")
            seen = set()
            for link in soup.find_all("a", href=True):
                match = DOWNLOAD_RE.search(link["href"])
                if not match or match.group(1) in seen:
                    continue
                seen.add(match.group(1)); doc_id = int(match.group(1))
                if doc_id in done:
                    continue
                doc_title = " ".join(link.get_text(" ", strip=True).split()) or f"document_{doc_id}"
                date = document_date(doc_title)
                try:
                    response = sess.get(urljoin(BASE_URL, link["href"]), timeout=120)
                    response.raise_for_status()
                    dest = source / (date[:4] if date != "unknown" else str(year)) / (
                        date[5:7] if date != "unknown" else "unknown"
                    ) / f"{date}_{doc_id}{extension(response)}"
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(response.content)
                    row = {"document_id": doc_id, "title": doc_title, "document_date": date,
                           "url": response.url, "path": str(dest.relative_to(root)), "status": "ok", "at": now()}
                    done.add(doc_id); successful += 1
                    print(f"  OK  {doc_id} → {dest.relative_to(root)}")
                except Exception as exc:  # noqa: BLE001
                    row = {"document_id": doc_id, "title": doc_title, "status": "error", "error": str(exc), "at": now()}
                    print(f"  ERR {doc_id}: {exc}")
                with manifest.open("a", encoding="utf-8") as out:
                    out.write(json.dumps(row, ensure_ascii=False) + "\n")
                if args.limit and successful >= args.limit:
                    print(f"Done. downloaded={successful}"); return
    print(f"Done. downloaded={successful}")


def rows_for_file(path: Path, root: Path, meta: dict) -> list[dict]:
    rows: list[dict] = []
    def add(source_type: str, sheet: str, table: int, row: int, col: int, value: object) -> None:
        value = "" if value is None else str(value).strip()
        if value:
            rows.append({"document_id": meta["document_id"], "document_date": meta["document_date"],
                         "title": meta["title"], "source_file": str(path.relative_to(root)),
                         "source_type": source_type, "sheet_or_page": sheet, "table_index": table,
                         "row_index": row, "column_index": col, "value": value})
    if path.suffix.lower() == ".pdf":
        with pdfplumber.open(path) as pdf:
            for page_no, page in enumerate(pdf.pages, 1):
                tables = page.extract_tables() or []
                for table_no, table in enumerate(tables, 1):
                    for row_no, cells in enumerate(table, 1):
                        for col_no, value in enumerate(cells, 1):
                            add("pdf", str(page_no), table_no, row_no, col_no, value)
                text_lines = (page.extract_text() or "").splitlines()
                if not tables and not text_lines and shutil.which("tesseract"):
                    with tempfile.TemporaryDirectory() as tmp:
                        image_path = Path(tmp) / "page.png"
                        page.to_image(resolution=300).save(image_path, format="PNG")
                        result = subprocess.run(
                            ["tesseract", str(image_path), "stdout", "-l", "rus+eng", "--psm", "6"],
                            capture_output=True, text=True, check=False,
                        )
                        text_lines = result.stdout.splitlines()
                if not tables:
                    for row_no, line in enumerate(text_lines, 1):
                        add("pdf_text", str(page_no), 0, row_no, 1, line)
    elif path.suffix.lower() == ".xlsx":
        book = load_workbook(path, read_only=True, data_only=True)
        for sheet in book.worksheets:
            for row_no, cells in enumerate(sheet.iter_rows(values_only=True), 1):
                for col_no, value in enumerate(cells, 1):
                    add("xlsx", sheet.title, 1, row_no, col_no, value)
    else:
        raise ValueError(f"Unsupported document type: {path.suffix}")
    if not rows:
        raise ValueError("No content extracted; this scanned PDF requires Tesseract OCR")
    return rows


def csv_main(root: Path) -> None:
    parser = argparse.ArgumentParser(description="Convert KOREM source documents to long-format CSV")
    parser.add_argument("--limit", type=int, default=0); parser.add_argument("--force", action="store_true")
    args = parser.parse_args(); data = root / "data"; source = data / "source"; out_dir = data / "csv"
    manifest = {}
    for line in (data / "manifest.jsonl").read_text(encoding="utf-8").splitlines() if (data / "manifest.jsonl").exists() else []:
        try:
            item = json.loads(line)
            if item.get("status") == "ok": manifest[int(item["document_id"])] = item
        except (ValueError, KeyError): pass
    ok = skipped = failed = 0
    for path in sorted(p for p in source.rglob("*") if p.suffix.lower() in {".pdf", ".xlsx", ".xls"}):
        match = re.search(r"_(\d+)\.[^.]+$", path.name)
        if not match: continue
        dest = out_dir / path.relative_to(source).with_suffix(".csv")
        if dest.exists() and not args.force: skipped += 1; continue
        meta = manifest.get(int(match.group(1)), {"document_id": int(match.group(1)), "document_date": "unknown", "title": path.stem})
        try:
            rows = rows_for_file(path, root, meta); dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("w", encoding="utf-8", newline="") as out:
                writer = csv.DictWriter(out, fieldnames=FIELDS); writer.writeheader(); writer.writerows(rows)
            ok += 1; print(f"  OK  {path.name} ({len(rows)} cells)")
        except Exception as exc:  # noqa: BLE001
            failed += 1; print(f"  ERR {path.name}: {exc}")
        if args.limit and ok >= args.limit: break
    print(f"Done. ok={ok} skipped={skipped} failed={failed}")


def sqlite_main(root: Path, db_name: str) -> None:
    parser = argparse.ArgumentParser(description="Load KOREM CSV cells into SQLite")
    parser.add_argument("--force", action="store_true"); args = parser.parse_args()
    data = root / "data"; db = data / db_name; csv_dir = data / "csv"; data.mkdir(exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript("""CREATE TABLE IF NOT EXISTS documents (document_id INTEGER PRIMARY KEY, document_date TEXT, title TEXT, source_file TEXT, loaded_at TEXT);
CREATE TABLE IF NOT EXISTS cells (document_id INTEGER, document_date TEXT, source_file TEXT, source_type TEXT, sheet_or_page TEXT, table_index INTEGER, row_index INTEGER, column_index INTEGER, value TEXT, PRIMARY KEY(document_id, source_type, sheet_or_page, table_index, row_index, column_index));
CREATE INDEX IF NOT EXISTS idx_cells_document_date ON cells(document_date);""")
    loaded = skipped = 0
    for path in sorted(csv_dir.rglob("*.csv")):
        with path.open(encoding="utf-8", newline="") as src: rows = list(csv.DictReader(src))
        if not rows: continue
        doc_id = int(rows[0]["document_id"])
        if not args.force and conn.execute("SELECT 1 FROM documents WHERE document_id=?", (doc_id,)).fetchone(): skipped += 1; continue
        first = rows[0]
        conn.execute("INSERT OR REPLACE INTO documents VALUES (?, ?, ?, ?, ?)", (doc_id, first["document_date"], first["title"], first["source_file"], now()))
        conn.executemany("INSERT OR REPLACE INTO cells VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [tuple(r[k] for k in FIELDS if k not in {"title"}) for r in rows])
        conn.commit(); loaded += 1
    conn.close(); print(f"Done. loaded={loaded} skipped={skipped} db={db}")
