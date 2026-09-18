#!/usr/bin/env python3
"""PDF → CSV for: Реестры победителей торгов МРГ
Uses tables/text; for scans run with --ocr (needs tesseract).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pdfplumber

SOURCE_KEY = "reestry-pobediteley"
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PDF_DIR = DATA_DIR / "source"
CSV_DIR = DATA_DIR / "csv"
ERRORS_PATH = DATA_DIR / "parse_errors.jsonl"

ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
RU_DATE_RE = re.compile(r"(?<!\d)(\d{2})[.](\d{2})[.](\d{2,4})(?!\d)")


def file_id_from_path(path: Path) -> int:
    m = re.search(r"_(\d+)_", path.name) or re.search(r"_(\d+)\.pdf$", path.name)
    if m:
        return int(m.group(1))
    return int(hashlib.md5(str(path.relative_to(ROOT)).encode()).hexdigest()[:8], 16)


def date_from_name(path: Path) -> str | None:
    m = ISO_DATE_RE.search(path.name)
    if m:
        y, mo, d = m.groups()
        try:
            return datetime(int(y), int(mo), int(d)).strftime("%Y-%m-%d")
        except ValueError:
            pass
    m = RU_DATE_RE.search(path.name)
    if not m:
        return None
    d, mo, y = m.groups()
    year = int(y) + (2000 if int(y) < 100 else 0)
    try:
        return datetime(year, int(mo), int(d)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def csv_path_for(pdf_path: Path) -> Path:
    year = pdf_path.parent.name
    fid = file_id_from_path(pdf_path)
    date = date_from_name(pdf_path) or "unknown-date"
    return CSV_DIR / year / f"{date}_{fid}.csv"


def normalize_cell(cell: Any) -> str:
    if cell is None:
        return ""
    return re.sub(r"\s+", " ", str(cell).replace("\xa0", " ")).strip()


def tables_to_rows(tables: list, page_no: int) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for ti, table in enumerate(tables):
        if not table:
            continue
        width = max(len(r) for r in table)
        header = [normalize_cell(c) for c in (table[0] + [""] * width)[:width]]
        if not any(header):
            header = [f"col_{i+1}" for i in range(width)]
        used: dict[str, int] = {}
        cols = []
        for h in header:
            base = h or "col"
            n = used.get(base, 0)
            used[base] = n + 1
            cols.append(base if n == 0 else f"{base}_{n+1}")
        for ri, raw in enumerate(table[1:], start=2):
            cells = [normalize_cell(c) for c in (list(raw) + [""] * width)[:width]]
            if not any(cells):
                continue
            row = {"page": str(page_no), "table": str(ti + 1), "row": str(ri), "source_method": "pdfplumber_table"}
            for col, val in zip(cols, cells):
                row[col] = val
            rows.append(row)
    return rows


def extract_with_pdfplumber(pdf_path: Path) -> tuple[list[dict[str, str]], str]:
    rows: list[dict[str, str]] = []
    text_chunks: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                text_chunks.append(text)
            rows.extend(tables_to_rows(page.extract_tables() or [], i))
    return rows, "\n\n".join(text_chunks)


def ocr_available() -> bool:
    return shutil.which("tesseract") is not None


def tesseract_langs() -> str:
    proc = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, check=False)
    available = set(proc.stdout.splitlines())
    langs = [lang for lang in ("rus", "eng") if lang in available]
    return "+".join(langs) if langs else "eng"


def extract_with_ocr(pdf_path: Path) -> list[dict[str, str]]:
    import tempfile
    import pypdfium2 as pdfium

    rows: list[dict[str, str]] = []
    langs = tesseract_langs()
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        with tempfile.TemporaryDirectory(prefix="ocr_") as tmp:
            tmp_path = Path(tmp)
            for i in range(len(doc)):
                page = doc[i]
                img = page.render(scale=2).to_pil()
                img_path = tmp_path / f"page_{i + 1}.png"
                img.save(img_path)
                proc = subprocess.run(
                    ["tesseract", str(img_path), "stdout", "-l", langs, "--psm", "6"],
                    check=True, capture_output=True, text=True,
                )
                for li, line in enumerate(proc.stdout.splitlines(), start=1):
                    line = line.strip()
                    if line:
                        rows.append({
                            "page": str(i + 1), "table": "1", "row": str(li),
                            "text": line, "source_method": "ocr",
                        })
    finally:
        doc.close()
    return rows


def text_to_rows(text: str) -> list[dict[str, str]]:
    rows = []
    for i, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if line:
            rows.append({
                "page": "1", "table": "1", "row": str(i),
                "text": line, "source_method": "pdfplumber_text",
            })
    return rows


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    seen: set[str] = set()
    for key in ("page", "table", "row", "source_method", "text"):
        if any(key in r for r in rows) and key not in seen:
            fields.append(key); seen.add(key)
    for r in rows:
        for k in r:
            if k not in seen:
                fields.append(k); seen.add(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def convert_one(pdf_path: Path, force: bool, use_ocr: bool) -> str:
    out = csv_path_for(pdf_path)
    if out.exists() and not force:
        return "skipped"
    rows, text = extract_with_pdfplumber(pdf_path)
    if not rows and text.strip():
        rows = text_to_rows(text)
    if not rows and use_ocr and ocr_available():
        rows = extract_with_ocr(pdf_path)
    if not rows:
        raise RuntimeError("no text/tables (scanned PDF; rerun with --ocr)")
    meta = []
    for r in rows:
        meta.append({
            "source": SOURCE_KEY,
            "year": pdf_path.parent.name,
            "file_id": str(file_id_from_path(pdf_path)),
            "source_file": str(pdf_path.relative_to(ROOT)),
            "doc_date": date_from_name(pdf_path) or "",
            **r,
        })
    write_csv(out, meta)
    return f"ok:{len(meta)}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    pdfs = sorted(PDF_DIR.rglob("*.pdf")) if PDF_DIR.exists() else []
    if args.year:
        pdfs = [p for p in pdfs if p.parent.name == args.year]
    print(f"pdfs={len(pdfs)}")
    ok = skipped = failed = 0
    for pdf_path in pdfs:
        try:
            status = convert_one(pdf_path, args.force, args.ocr)
            if status == "skipped":
                skipped += 1
                print(f"  SKIP {pdf_path.relative_to(ROOT)}")
            else:
                ok += 1
                print(f"  OK   {pdf_path.relative_to(ROOT)} → {csv_path_for(pdf_path).relative_to(ROOT)} ({status.split(':',1)[1]} rows)")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            with ERRORS_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"pdf": str(pdf_path), "error": str(exc)}, ensure_ascii=False) + "\n")
            print(f"  ERR  {pdf_path.name}: {exc}")
        if args.limit and ok >= args.limit:
            break
    print(f"Done. ok={ok} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
