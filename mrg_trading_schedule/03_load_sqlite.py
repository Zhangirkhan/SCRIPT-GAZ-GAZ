#!/usr/bin/env python3
"""Load CSV into korem.db for: График торгов МРГ
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SOURCE_KEY = "grafik-torgov"
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CSV_DIR = DATA_DIR / "csv"
DB_PATH = DATA_DIR / "mrg_trading_schedule.db"

META_KEYS = {
    "page", "table", "row", "source_method", "text",
    "source", "year", "file_id", "source_file", "doc_date",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    file_id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    year TEXT,
    doc_date TEXT,
    pdf_path TEXT,
    csv_path TEXT,
    parsed_at TEXT
);
CREATE TABLE IF NOT EXISTS rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    year TEXT,
    doc_date TEXT,
    page INTEGER,
    table_no INTEGER,
    row_no INTEGER,
    source_method TEXT,
    text TEXT,
    payload TEXT,
    FOREIGN KEY (file_id) REFERENCES files(file_id)
);
CREATE INDEX IF NOT EXISTS idx_rows_source_year ON rows(source, year);
CREATE INDEX IF NOT EXISTS idx_rows_doc_date ON rows(doc_date);
CREATE INDEX IF NOT EXISTS idx_rows_file ON rows(file_id);
"""


def file_id_from_csv(path: Path) -> int | None:
    m = re.search(r"_(\d+)\.csv$", path.name)
    return int(m.group(1)) if m else None


def to_int(val: str | None) -> int | None:
    if val is None or str(val).strip() == "":
        return None
    try:
        return int(float(str(val).strip()))
    except ValueError:
        return None


def load_csv(conn: sqlite3.Connection, csv_path: Path) -> int:
    file_id = file_id_from_csv(csv_path)
    if file_id is None:
        raise ValueError(f"Cannot parse file_id from {csv_path.name}")
    with csv_path.open(encoding="utf-8", newline="") as f:
        data = list(csv.DictReader(f))
    first = data[0] if data else {}
    source = first.get("source") or SOURCE_KEY
    year = first.get("year") or csv_path.parent.name
    doc_date = first.get("doc_date") or None
    pdf_path = first.get("source_file") or ""
    conn.execute(
        """INSERT INTO files (file_id, source, year, doc_date, pdf_path, csv_path, parsed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(file_id) DO UPDATE SET
             source=excluded.source, year=excluded.year, doc_date=excluded.doc_date,
             pdf_path=excluded.pdf_path, csv_path=excluded.csv_path, parsed_at=excluded.parsed_at""",
        (file_id, source, year, doc_date or None, pdf_path, str(csv_path.relative_to(ROOT)),
         datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")),
    )
    conn.execute("DELETE FROM rows WHERE file_id = ?", (file_id,))
    batch = []
    for row in data:
        fid = to_int(row.get("file_id")) or file_id
        text = (row.get("text") or "").strip()
        extra = {k: v for k, v in row.items() if k not in META_KEYS and v not in (None, "")}
        if not text and extra:
            text = " | ".join(f"{k}: {v}" for k, v in extra.items())
        payload = {k: v for k, v in row.items() if v not in (None, "")}
        batch.append((
            fid, row.get("source") or source, row.get("year") or year,
            row.get("doc_date") or doc_date or None,
            to_int(row.get("page")), to_int(row.get("table")), to_int(row.get("row")),
            row.get("source_method") or None, text or None,
            json.dumps(payload, ensure_ascii=False),
        ))
    conn.executemany(
        """INSERT INTO rows (
            file_id, source, year, doc_date, page, table_no, row_no,
            source_method, text, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        batch,
    )
    return len(batch)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--year", default=None)
    args = parser.parse_args()

    csvs = sorted(CSV_DIR.rglob("*.csv")) if CSV_DIR.exists() else []
    if args.year:
        csvs = [p for p in csvs if p.parent.name == args.year]
    if not csvs:
        print(f"No CSVs under {CSV_DIR} (DB already present: {DB_PATH.exists()})")
        return

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)
        existing = set() if args.force else {r[0] for r in conn.execute("SELECT file_id FROM files")}
        loaded = skipped = failed = total = 0
        for csv_path in csvs:
            fid = file_id_from_csv(csv_path)
            if fid in existing and not args.force:
                skipped += 1
                continue
            try:
                n = load_csv(conn, csv_path)
                conn.commit()
                loaded += 1
                total += n
                print(f"  OK  {csv_path.relative_to(ROOT)} ({n} rows)")
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                failed += 1
                print(f"  ERR {csv_path.name}: {exc}")
        print(f"Done. loaded={loaded} skipped={skipped} failed={failed} rows={total} db={DB_PATH}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
