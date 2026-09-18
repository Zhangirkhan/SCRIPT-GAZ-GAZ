#!/usr/bin/env python3
"""Parse KOREM miner result PDFs into long-format CSV files."""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pdfplumber

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PDF_DIR = DATA_DIR / "pdf"
CSV_DIR = DATA_DIR / "csv"
ERRORS_PATH = DATA_DIR / "parse_errors.jsonl"

CSV_FIELDS = [
    "trade_date",
    "delivery_date",
    "zone",
    "metric",
    "hour",
    "value",
    "unit",
    "start_price",
    "source_file",
    "file_id",
]

DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
PRICE_RE = re.compile(
    r"Бастапқы\s*баға|Стартовая\s*цена",
    re.IGNORECASE,
)
NUM_TOKEN_RE = re.compile(r"^-?\d[\d\s]*[.,]\d+$|^-?\d+$")

ZONE_PATTERNS = [
    (
        "north_south",
        re.compile(
            r"Северн|Южн|Солтүстік|Оңтүстік|North|South",
            re.IGNORECASE,
        ),
    ),
    (
        "west",
        re.compile(r"Запад|Батыс|West", re.IGNORECASE),
    ),
]

METRIC_PATTERNS = [
    (
        "quota_so",
        re.compile(
            r"Квоты.*предоставлен|ЖО\s*берілген\s*квота|предоставленные\s*СО",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "residual_north_south",
        re.compile(
            r"остаточн|пропускн|Север-Юг|Север\s*[-–]\s*Юг|қалдық\s*өткізу",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "bids",
        re.compile(
            r"Заявк|өтінім|поданные\s*цифровыми",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
]

BIDS_EMPTY_RE = re.compile(
    r"заявок\s*не\s*было|өтінімдер\s*болған\s*жоқ",
    re.IGNORECASE,
)


def parse_ru_number(raw: str | None) -> float | None:
    if raw is None:
        return None
    s = str(raw).replace("\xa0", " ").strip()
    s = s.replace("\n", " ")
    s = re.sub(r"\s+", "", s)
    if not s or s in {"-", "—", "–"}:
        return None
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_date_ddmmyyyy(text: str) -> str | None:
    m = DATE_RE.search(text)
    if not m:
        return None
    d, mo, y = m.groups()
    try:
        return datetime(int(y), int(mo), int(d)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def normalize_cell(cell: Any) -> str:
    if cell is None:
        return ""
    return re.sub(r"\s+", " ", str(cell).replace("\xa0", " ")).strip()


def detect_zone(text: str) -> str | None:
    # Prefer west first only when west is explicit and north/south not dominant
    lower = text
    if re.search(r"Западн|Батыс\s*аймақ|West", lower, re.IGNORECASE):
        if not re.search(r"Северн|Южн|Солтүстік|Оңтүстік", lower, re.IGNORECASE):
            return "west"
    if re.search(r"Северн|Южн|Солтүстік|Оңтүстік", lower, re.IGNORECASE):
        return "north_south"
    if re.search(r"Запад|Батыс|West", lower, re.IGNORECASE):
        return "west"
    return None


def detect_metric(label: str) -> str | None:
    for name, pat in METRIC_PATTERNS:
        if pat.search(label):
            return name
    return None


def extract_meta(text: str) -> dict[str, Any]:
    trade_date = None
    delivery_date = None
    start_prices: list[float] = []

    for line in text.splitlines():
        if "Дата торгов" in line or "Сауда-саттық күні" in line:
            trade_date = parse_date_ddmmyyyy(line) or trade_date
        if "Дата поставки" in line or "Жеткізу күні" in line:
            delivery_date = parse_date_ddmmyyyy(line) or delivery_date
        if PRICE_RE.search(line):
            # take last number on the line
            nums = DATE_RE.sub("", line)
            nums = re.findall(r"\d+[.,]\d+|\d+", nums)
            if nums:
                val = parse_ru_number(nums[-1])
                if val is not None:
                    start_prices.append(val)

    return {
        "trade_date": trade_date,
        "delivery_date": delivery_date,
        "start_prices": start_prices,
    }


def file_id_from_path(path: Path) -> int | None:
    m = re.search(r"_(\d+)\.pdf$", path.name)
    return int(m.group(1)) if m else None


def csv_path_for(pdf_path: Path) -> Path:
    rel = pdf_path.relative_to(PDF_DIR)
    return CSV_DIR / rel.with_suffix(".csv")


def rows_from_table(
    table: list[list[Any]],
    *,
    zone: str | None,
    start_price: float | None,
    trade_date: str | None,
    delivery_date: str | None,
    source_file: str,
    file_id: int | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not table:
        return rows

    for raw_row in table:
        cells = [normalize_cell(c) for c in raw_row]
        if not any(cells):
            continue
        label = " ".join(cells[:2]).strip()
        metric = detect_metric(label)
        if not metric:
            # sometimes label is only in first cell
            metric = detect_metric(cells[0] if cells else "")
        if not metric:
            continue

        # Find numeric hour values: prefer cells after unit column
        values: list[float | None] = []
        for cell in cells[1:]:
            if not cell:
                continue
            if re.fullmatch(r"\d{1,2}", cell):
                # hour header cell
                continue
            if "ед.изм" in cell or "өлш" in cell.lower() or "мың" in cell.lower() or "тыс" in cell.lower():
                continue
            if "Всего" in cell or "Барлығы" in cell:
                continue
            num = parse_ru_number(cell)
            if num is not None:
                values.append(num)

        if len(values) < 24:
            continue

        hour_vals = values[:24]
        total = values[24] if len(values) >= 25 else None
        unit = "тыс.кВт*ч"

        for hour, value in enumerate(hour_vals, start=1):
            rows.append(
                {
                    "trade_date": trade_date or "",
                    "delivery_date": delivery_date or "",
                    "zone": zone or "unknown",
                    "metric": metric,
                    "hour": hour,
                    "value": value,
                    "unit": unit,
                    "start_price": start_price if start_price is not None else "",
                    "source_file": source_file,
                    "file_id": file_id if file_id is not None else "",
                }
            )
        if total is not None:
            rows.append(
                {
                    "trade_date": trade_date or "",
                    "delivery_date": delivery_date or "",
                    "zone": zone or "unknown",
                    "metric": metric,
                    "hour": 0,
                    "value": total,
                    "unit": unit,
                    "start_price": start_price if start_price is not None else "",
                    "source_file": source_file,
                    "file_id": file_id if file_id is not None else "",
                }
            )
    return rows


def infer_zones_and_prices(text: str, meta: dict[str, Any]) -> list[tuple[str, float | None]]:
    """Return ordered zone blocks with associated start prices."""
    prices = list(meta.get("start_prices") or [])
    blocks: list[tuple[str, float | None]] = []

    chunks: list[str] = []
    for m in re.finditer(
        r"(?:Зона\s*торгов|Сауда-саттық\s*аймағы)[^\n]*",
        text,
        flags=re.IGNORECASE,
    ):
        chunks.append(text[m.start() : m.start() + 400])

    if not chunks:
        blocks.append(("north_south", prices[0] if prices else None))
        if "Запад" in text or "Батыс" in text:
            blocks.append(
                (
                    "west",
                    prices[1] if len(prices) > 1 else (prices[0] if prices else None),
                )
            )
        return blocks

    for i, chunk in enumerate(chunks):
        z = detect_zone(chunk)
        if not z:
            continue
        price = prices[i] if i < len(prices) else (prices[-1] if prices else None)
        if not any(b[0] == z for b in blocks):
            blocks.append((z, price))

    if not blocks:
        blocks.append(("north_south", prices[0] if prices else None))
    if ("west" not in {b[0] for b in blocks}) and (
        "Запад" in text or "Батыс" in text
    ):
        blocks.append(("west", prices[1] if len(prices) > 1 else None))
    return blocks


def parse_bids_empty(
    text: str,
    zone_blocks: list[tuple[str, float | None]],
    meta: dict[str, Any],
    source_file: str,
    file_id: int | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not BIDS_EMPTY_RE.search(text):
        return rows

    # Assign empty-bids markers per zone when mentioned near the phrase
    for zone, price in zone_blocks:
        # Check zone-specific empty message
        if zone == "west" and not re.search(
            r"(Запад|Батыс).{0,80}(заявок\s*не\s*было|өтінімдер\s*болған\s*жоқ)",
            text,
            re.IGNORECASE | re.DOTALL,
        ):
            # still mark if only global empty for that section
            if "Запад" not in text and "Батыс" not in text:
                continue
        rows.append(
            {
                "trade_date": meta.get("trade_date") or "",
                "delivery_date": meta.get("delivery_date") or "",
                "zone": zone,
                "metric": "bids_empty",
                "hour": 0,
                "value": 1,
                "unit": "flag",
                "start_price": price if price is not None else "",
                "source_file": source_file,
                "file_id": file_id if file_id is not None else "",
            }
        )
    return rows


def assign_table_zone(
    table: list[list[Any]],
    zone_blocks: list[tuple[str, float | None]],
    table_index: int,
    n_tables: int,
) -> tuple[str | None, float | None]:
    flat = " ".join(normalize_cell(c) for row in table for c in row)
    z = detect_zone(flat)
    if z:
        price = next((p for zz, p in zone_blocks if zz == z), None)
        return z, price

    # Heuristic: first half of numeric tables → north_south, later → west
    if len(zone_blocks) >= 2:
        mid = max(1, n_tables // 2)
        if table_index < mid:
            return zone_blocks[0]
        return zone_blocks[-1]
    if zone_blocks:
        return zone_blocks[0]
    return None, None


def parse_pdf(pdf_path: Path) -> list[dict[str, Any]]:
    file_id = file_id_from_path(pdf_path)
    source_file = str(pdf_path.relative_to(ROOT))
    all_rows: list[dict[str, Any]] = []

    with pdfplumber.open(pdf_path) as pdf:
        texts: list[str] = []
        tables: list[list[list[Any]]] = []
        for page in pdf.pages:
            texts.append(page.extract_text() or "")
            for t in page.extract_tables() or []:
                if t:
                    tables.append(t)

    full_text = "\n".join(texts)
    meta = extract_meta(full_text)
    zone_blocks = infer_zones_and_prices(full_text, meta)

    for i, table in enumerate(tables):
        zone, price = assign_table_zone(table, zone_blocks, i, len(tables))
        all_rows.extend(
            rows_from_table(
                table,
                zone=zone,
                start_price=price,
                trade_date=meta.get("trade_date"),
                delivery_date=meta.get("delivery_date"),
                source_file=source_file,
                file_id=file_id,
            )
        )

    all_rows.extend(
        parse_bids_empty(full_text, zone_blocks, meta, source_file, file_id)
    )

    # Deduplicate identical observation keys
    seen: set[tuple] = set()
    unique: list[dict[str, Any]] = []
    for row in all_rows:
        key = (row["zone"], row["metric"], row["hour"], row["value"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)

    if not any(r["metric"] not in {"bids_empty"} for r in unique):
        # No numeric metrics found — try text fallback for numbers is too fragile;
        # raise so caller logs error.
        if not unique:
            raise ValueError("No tables/metrics extracted from PDF")

    return unique


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def append_error(row: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with ERRORS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most N PDFs (0 = all)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-parse even if CSV already exists",
    )
    args = parser.parse_args()

    pdfs = sorted(PDF_DIR.rglob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found under {PDF_DIR}")
        return

    ok = 0
    skipped = 0
    failed = 0

    for pdf_path in pdfs:
        out = csv_path_for(pdf_path)
        if out.exists() and not args.force:
            skipped += 1
            continue
        try:
            rows = parse_pdf(pdf_path)
            write_csv(out, rows)
            ok += 1
            print(f"  OK  {pdf_path.name} → {out.relative_to(ROOT)} ({len(rows)} rows)")
            if args.limit and ok >= args.limit:
                break
        except Exception as exc:  # noqa: BLE001
            failed += 1
            append_error(
                {
                    "pdf": str(pdf_path.relative_to(ROOT)),
                    "error": str(exc),
                    "at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                }
            )
            print(f"  ERR {pdf_path.name}: {exc}")

    print(f"Done. ok={ok} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
