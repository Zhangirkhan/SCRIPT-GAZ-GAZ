#!/usr/bin/env python3
"""Load TEM deal CSVs into SQLite (data/korem_tem.db)."""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CSV_DIR = DATA_DIR / "csv"
DB_PATH = DATA_DIR / "korem_tem.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    file_id INTEGER PRIMARY KEY,
    trade_date TEXT,
    registry_no INTEGER,
    delivery_start TEXT,
    delivery_end TEXT,
    zone_pair TEXT,
    total_volume_mw REAL,
    transfer_capacity_mw REAL,
    limit_tariff REAL,
    pdf_path TEXT,
    csv_path TEXT,
    loaded_at TEXT
);

CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    trade_date TEXT,
    row_num INTEGER,
    organization TEXT,
    deal_number TEXT NOT NULL,
    volume_mw REAL,
    price REAL,
    amount_no_vat REAL,
    amount_with_vat REAL,
    org_zone TEXT,
    UNIQUE (file_id, deal_number),
    FOREIGN KEY (file_id) REFERENCES files(file_id)
);

CREATE INDEX IF NOT EXISTS idx_deals_trade_date ON deals(trade_date);
CREATE INDEX IF NOT EXISTS idx_deals_org ON deals(organization);
CREATE INDEX IF NOT EXISTS idx_deals_zone ON deals(org_zone);
"""


def file_id_from_csv(path: Path) -> int | None:
    m = re.search(r"_(\d+)\.csv$", path.name)
    return int(m.group(1)) if m else None


def pdf_path_guess(csv_path: Path) -> str:
    rel = csv_path.relative_to(CSV_DIR)
    return str(Path("korem_files") / rel.with_suffix(".pdf"))


def to_float(val: str | None) -> float | None:
    if val is None:
        return None
    s = str(val).strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


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
        rows = list(csv.DictReader(f))

    first = rows[0] if rows else {}
    conn.execute(
        """
        INSERT INTO files (
            file_id, trade_date, registry_no, delivery_start, delivery_end,
            zone_pair, total_volume_mw, transfer_capacity_mw, limit_tariff,
            pdf_path, csv_path, loaded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(file_id) DO UPDATE SET
            trade_date=excluded.trade_date,
            registry_no=excluded.registry_no,
            delivery_start=excluded.delivery_start,
            delivery_end=excluded.delivery_end,
            zone_pair=excluded.zone_pair,
            total_volume_mw=excluded.total_volume_mw,
            transfer_capacity_mw=excluded.transfer_capacity_mw,
            limit_tariff=excluded.limit_tariff,
            pdf_path=excluded.pdf_path,
            csv_path=excluded.csv_path,
            loaded_at=excluded.loaded_at
        """,
        (
            file_id,
            first.get("trade_date") or None,
            to_int(first.get("registry_no")),
            first.get("delivery_start") or None,
            first.get("delivery_end") or None,
            first.get("zone_pair") or None,
            to_float(first.get("total_volume_mw")),
            to_float(first.get("transfer_capacity_mw")),
            to_float(first.get("limit_tariff")),
            pdf_path_guess(csv_path),
            str(csv_path.relative_to(ROOT)),
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )

    obs = []
    for row in rows:
        deal_number = (row.get("deal_number") or "").strip()
        if not deal_number:
            continue
        obs.append(
            (
                file_id,
                row.get("trade_date") or None,
                to_int(row.get("row_num")),
                row.get("organization") or None,
                deal_number,
                to_float(row.get("volume_mw")),
                to_float(row.get("price")),
                to_float(row.get("amount_no_vat")),
                to_float(row.get("amount_with_vat")),
                row.get("org_zone") or None,
            )
        )

    conn.executemany(
        """
        INSERT INTO deals (
            file_id, trade_date, row_num, organization, deal_number,
            volume_mw, price, amount_no_vat, amount_with_vat, org_zone
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(file_id, deal_number) DO UPDATE SET
            trade_date=excluded.trade_date,
            row_num=excluded.row_num,
            organization=excluded.organization,
            volume_mw=excluded.volume_mw,
            price=excluded.price,
            amount_no_vat=excluded.amount_no_vat,
            amount_with_vat=excluded.amount_with_vat,
            org_zone=excluded.org_zone
        """,
        obs,
    )
    return len(obs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH, help="SQLite DB path")
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
                r[0] for r in conn.execute("SELECT file_id FROM files").fetchall()
            }

        loaded = skipped = failed = total_rows = 0
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
                print(f"  OK  {csv_path.name} ({n} deals)")
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
