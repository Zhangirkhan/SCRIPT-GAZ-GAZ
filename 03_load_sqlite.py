#!/usr/bin/env python3
"""Load parsed CSV files into SQLite (data/korem.db)."""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CSV_DIR = DATA_DIR / "csv"
DB_PATH = DATA_DIR / "korem.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    file_id INTEGER PRIMARY KEY,
    trade_date TEXT,
    delivery_date TEXT,
    pdf_path TEXT,
    csv_path TEXT,
    downloaded_at TEXT
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    trade_date TEXT,
    delivery_date TEXT,
    zone TEXT NOT NULL,
    metric TEXT NOT NULL,
    hour INTEGER NOT NULL,
    value REAL,
    unit TEXT,
    start_price REAL,
    UNIQUE (file_id, zone, metric, hour),
    FOREIGN KEY (file_id) REFERENCES files(file_id)
);

CREATE INDEX IF NOT EXISTS idx_obs_trade_date ON observations(trade_date);
CREATE INDEX IF NOT EXISTS idx_obs_zone_metric_date
    ON observations(zone, metric, trade_date);
"""


def file_id_from_csv(path: Path) -> int | None:
    m = re.search(r"_(\d+)\.csv$", path.name)
    return int(m.group(1)) if m else None


def pdf_path_guess(csv_path: Path) -> str:
    rel = csv_path.relative_to(CSV_DIR)
    return str(Path("data/pdf") / rel.with_suffix(".pdf"))


def to_float(val: str) -> float | None:
    if val is None:
        return None
    s = str(val).strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_int(val: str) -> int | None:
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
        reader = csv.DictReader(f)
        rows = list(reader)

    trade_date = ""
    delivery_date = ""
    if rows:
        trade_date = rows[0].get("trade_date") or ""
        delivery_date = rows[0].get("delivery_date") or ""

    conn.execute(
        """
        INSERT INTO files (file_id, trade_date, delivery_date, pdf_path, csv_path, downloaded_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(file_id) DO UPDATE SET
            trade_date=excluded.trade_date,
            delivery_date=excluded.delivery_date,
            pdf_path=excluded.pdf_path,
            csv_path=excluded.csv_path,
            downloaded_at=excluded.downloaded_at
        """,
        (
            file_id,
            trade_date or None,
            delivery_date or None,
            pdf_path_guess(csv_path),
            str(csv_path.relative_to(ROOT)),
            datetime.utcnow().isoformat(timespec="seconds") + "Z",
        ),
    )

    obs = []
    for row in rows:
        obs.append(
            (
                file_id,
                row.get("trade_date") or None,
                row.get("delivery_date") or None,
                row.get("zone") or "unknown",
                row.get("metric") or "unknown",
                to_int(row.get("hour") or "0") or 0,
                to_float(row.get("value")),
                row.get("unit") or None,
                to_float(row.get("start_price")),
            )
        )

    conn.executemany(
        """
        INSERT INTO observations (
            file_id, trade_date, delivery_date, zone, metric, hour, value, unit, start_price
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(file_id, zone, metric, hour) DO UPDATE SET
            trade_date=excluded.trade_date,
            delivery_date=excluded.delivery_date,
            value=excluded.value,
            unit=excluded.unit,
            start_price=excluded.start_price
        """,
        obs,
    )
    return len(obs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=DB_PATH,
        help="SQLite database path",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reload CSVs even if file_id already in files table",
    )
    args = parser.parse_args()

    csvs = sorted(CSV_DIR.rglob("*.csv"))
    if not csvs:
        print(f"No CSVs found under {CSV_DIR}")
        return

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db)
    try:
        conn.executescript(SCHEMA)
        existing: set[int] = set()
        if not args.force:
            existing = {
                r[0]
                for r in conn.execute("SELECT file_id FROM files").fetchall()
            }

        loaded = 0
        skipped = 0
        failed = 0
        total_rows = 0

        for csv_path in csvs:
            fid = file_id_from_csv(csv_path)
            if fid is not None and fid in existing and not args.force:
                skipped += 1
                continue
            try:
                n = load_csv(conn, csv_path)
                conn.commit()
                loaded += 1
                total_rows += n
                print(f"  OK  {csv_path.name} ({n} rows)")
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                failed += 1
                print(f"  ERR {csv_path.name}: {exc}")

        print(
            f"Done. loaded={loaded} skipped={skipped} failed={failed} "
            f"rows={total_rows} db={args.db}"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
