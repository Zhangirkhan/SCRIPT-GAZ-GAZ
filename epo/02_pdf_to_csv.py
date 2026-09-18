#!/usr/bin/env python3
"""Parse KOREM EPO result PDFs into long-format CSV."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import datetime, timezone
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
    "section",
    "participant",
    "node",
    "metric",
    "hour",
    "value",
    "unit",
    "source_file",
    "file_id",
]

DATE_RE = re.compile(r"(\d{2})[.\-](\d{2})[.\-](\d{2,4})")
NUM_RE = re.compile(r"^-?\d[\d\s]*[.,]\d+$|^-?\d+$")

ZONE_NORTH = re.compile(
    r"Север|Южн|Солтүстік|Оңтүстік|North|South|Север-Юг|Север\s*[-–]\s*Юг",
    re.IGNORECASE,
)
ZONE_WEST = re.compile(r"Запад|Батыс|West", re.IGNORECASE)

SECTION_BIDS = re.compile(r"Заявк|өтінім", re.IGNORECASE)
SECTION_RESULTS = re.compile(
    r"Итоги\s*торгов|қорытынды|Предварительные\s*итоги",
    re.IGNORECASE,
)
SECTION_DEMAND = re.compile(r"Спрос\s*от\s*СО|сұраныс", re.IGNORECASE)
SECTION_LIMITS = re.compile(
    r"Граничн|Энергоузел|Энерготорап|Узел",
    re.IGNORECASE,
)

VOLUME_UNIT_RE = re.compile(r"тыс|мың|кВт", re.IGNORECASE)
PRICE_UNIT_RE = re.compile(r"тг\s*/|тг/", re.IGNORECASE)
MIN_RE = re.compile(r"\bMIN\b", re.IGNORECASE)
MAX_RE = re.compile(r"\bMAX\b", re.IGNORECASE)
TOTAL_RE = re.compile(r"Всего|Барлығы|Бартығы", re.IGNORECASE)
SKIP_LABEL_RE = re.compile(
    r"Участник|торгов|ед[.,]?изм|№\s*п|Время|Өтінім|Сауда|Энерго|"
    r"Тех\s*min|набора|сброса|Дата|Зона|Аймақ",
    re.IGNORECASE,
)


def parse_ru_number(raw: str | None) -> float | None:
    if raw is None:
        return None
    s = str(raw).replace("\xa0", " ").strip().replace("\n", " ")
    s = re.sub(r"\s+", "", s)
    if not s or s in {"-", "—", "–"}:
        return None
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def normalize_cell(cell: Any) -> str:
    if cell is None:
        return ""
    return re.sub(r"\s+", " ", str(cell).replace("\xa0", " ")).strip()


def parse_date_token(text: str) -> str | None:
    m = DATE_RE.search(text)
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


def detect_zone(text: str) -> str | None:
    if ZONE_WEST.search(text) and not ZONE_NORTH.search(text):
        return "west"
    if ZONE_NORTH.search(text):
        return "north_south"
    if ZONE_WEST.search(text):
        return "west"
    return None


def file_id_from_path(path: Path) -> int:
    # Stable ID derived from the PDF's path inside data/pdf/.
    rel = path.relative_to(PDF_DIR)
    return int(hashlib.md5(str(rel).encode("utf-8")).hexdigest()[:8], 16)


def delivery_date_from_name(path: Path) -> str | None:
    return parse_date_token(path.name)


def csv_path_for(pdf_path: Path, delivery_date: str | None, file_id: int) -> Path:
    del delivery_date, file_id
    return CSV_DIR / pdf_path.relative_to(PDF_DIR).with_suffix(".csv")


def extract_meta(text: str, filename_date: str | None) -> dict[str, Any]:
    trade_date = None
    delivery_date = None
    for line in text.splitlines():
        if re.search(r"Дата\s*торгов|Сауда-саттық\s*күні", line, re.IGNORECASE):
            trade_date = parse_date_token(line) or trade_date
        if re.search(r"Дата\s*поставки|Жеткізу\s*күні", line, re.IGNORECASE):
            delivery_date = parse_date_token(line) or delivery_date
    if not delivery_date:
        delivery_date = filename_date
    if not trade_date and delivery_date:
        # early 2023 PDFs often put only one date at the top (= delivery)
        first = parse_date_token(text[:200])
        trade_date = first
    return {"trade_date": trade_date, "delivery_date": delivery_date}


def find_hour_start(header: list[str]) -> int | None:
    for i, cell in enumerate(header):
        if cell.strip() == "1":
            # confirm a run of hour headers
            nxt = [header[j].strip() for j in range(i, min(i + 5, len(header)))]
            if nxt[:3] == ["1", "2", "3"]:
                return i
    return None


def extract_hour_values(cells: list[str], start: int) -> tuple[list[float | None], float | None]:
    hour_vals: list[float | None] = []
    for i in range(start, start + 24):
        if i >= len(cells):
            hour_vals.append(None)
        else:
            hour_vals.append(parse_ru_number(cells[i]))
    total = None
    if start + 24 < len(cells):
        total = parse_ru_number(cells[start + 24])
    filled = sum(1 for v in hour_vals if v is not None)
    if filled < 12:
        return [], None
    return hour_vals, total


def metric_from_unit(unit: str) -> str | None:
    if PRICE_UNIT_RE.search(unit):
        return "price"
    if MIN_RE.search(unit):
        return "min"
    if MAX_RE.search(unit):
        return "max"
    if VOLUME_UNIT_RE.search(unit):
        return "volume"
    return None


def unit_norm(unit: str, metric: str) -> str:
    if metric == "price":
        return "тг/кВт*ч"
    if metric in {"volume", "demand", "min", "max"}:
        return "тыс.кВт*ч"
    return unit or ""


def emit_hours(
    *,
    trade_date: str | None,
    delivery_date: str | None,
    zone: str,
    section: str,
    participant: str,
    node: str,
    metric: str,
    hour_vals: list[float | None],
    total: float | None,
    unit: str,
    source_file: str,
    file_id: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for hour, value in enumerate(hour_vals, start=1):
        if value is None:
            continue
        rows.append(
            {
                "trade_date": trade_date or "",
                "delivery_date": delivery_date or "",
                "zone": zone,
                "section": section,
                "participant": participant,
                "node": node,
                "metric": metric,
                "hour": hour,
                "value": value,
                "unit": unit_norm(unit, metric),
                "source_file": source_file,
                "file_id": file_id,
            }
        )
    if total is not None:
        rows.append(
            {
                "trade_date": trade_date or "",
                "delivery_date": delivery_date or "",
                "zone": zone,
                "section": section,
                "participant": participant,
                "node": node,
                "metric": metric,
                "hour": 0,
                "value": total,
                "unit": unit_norm(unit, metric),
                "source_file": source_file,
                "file_id": file_id,
            }
        )
    return rows


def table_head_text(table: list[list[Any]], n: int = 4) -> str:
    return " ".join(normalize_cell(c) for row in table[:n] for c in row)


def classify_table(table: list[list[Any]]) -> str | None:
    flat = table_head_text(table)
    if SECTION_BIDS.search(flat):
        return "bids"
    if SECTION_DEMAND.search(flat):
        return "demand_so"
    if SECTION_RESULTS.search(flat):
        return "results"
    # node limits: has MIN/MAX and node-like header, not participant
    if (MIN_RE.search(flat) or MAX_RE.search(flat)) and SECTION_LIMITS.search(flat):
        if not re.search(r"Участник|қатысушы", flat, re.IGNORECASE):
            return "node_limit"
    # results without title row (continuation)
    for raw in table[:4]:
        header = [normalize_cell(c) for c in raw]
        joined = " ".join(header)
        if re.search(r"Участник", joined, re.IGNORECASE) and find_hour_start(header) is not None:
            return "results"
        if re.search(r"Энергоузел|Энерготорап|Узел", joined, re.IGNORECASE) and (
            MIN_RE.search(joined) or find_hour_start(header) is not None
        ):
            if not re.search(r"Участник", joined, re.IGNORECASE):
                return "node_limit"
    return None


def zone_from_table(table: list[list[Any]], fallback: str) -> str:
    head = table_head_text(table, 5)
    if re.search(r"\(Запад\)|Западная\s*зона|Батыс\s*аймақ", head, re.IGNORECASE):
        return "west"
    if re.search(r"\(Север|Северная\s*и\s*Южная|Солтүстік.*Оңтүстік|Север-Юг", head, re.IGNORECASE):
        return "north_south"
    z = detect_zone(head)
    return z or fallback


def parse_participant_table(
    table: list[list[Any]],
    *,
    section: str,
    zone: str,
    meta: dict[str, Any],
    source_file: str,
    file_id: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not table:
        return rows

    header_idx = None
    hour_start = None
    for i, raw in enumerate(table[:5]):
        cells = [normalize_cell(c) for c in raw]
        hs = find_hour_start(cells)
        if hs is not None:
            header_idx = i
            hour_start = hs
            break
    if header_idx is None or hour_start is None:
        return rows

    participant = ""
    node = ""
    for raw in table[header_idx + 1 :]:
        cells = [normalize_cell(c) for c in raw]
        if not any(cells):
            continue

        label0 = cells[0] if cells else ""
        label1 = cells[1] if len(cells) > 1 else ""

        if TOTAL_RE.search(label0) and not label0[0:1].isdigit():
            # zone totals line
            hour_vals, total = extract_hour_values(cells, hour_start if hour_start <= 2 else 1)
            if not hour_vals:
                # try from first numeric cluster
                nums = [parse_ru_number(c) for c in cells]
                vals = [v for v in nums if v is not None]
                if len(vals) >= 24:
                    hour_vals, total = vals[:24], (vals[24] if len(vals) > 24 else None)
            if hour_vals:
                rows.extend(
                    emit_hours(
                        trade_date=meta.get("trade_date"),
                        delivery_date=meta.get("delivery_date"),
                        zone=zone,
                        section=section,
                        participant="TOTAL",
                        node="",
                        metric="volume",
                        hour_vals=hour_vals,
                        total=total,
                        unit="тыс.кВт*ч",
                        source_file=source_file,
                        file_id=file_id,
                    )
                )
            continue

        unit_cell = ""
        if section == "bids":
            # unit typically at hour_start - 1
            unit_idx = max(0, hour_start - 1)
            unit_cell = cells[unit_idx] if unit_idx < len(cells) else ""
            if cells[0] and re.match(r"^\d+$", cells[0]):
                participant = cells[2] if len(cells) > 2 else participant
                node = cells[3] if len(cells) > 3 else node
            elif not cells[0] and not cells[2] if len(cells) > 2 else True:
                pass
            elif cells[2]:
                participant = cells[2]
                node = cells[3] if len(cells) > 3 else node
        else:
            # results: participant, node, unit, hours...
            unit_cell = cells[2] if len(cells) > 2 else ""
            if cells[0]:
                participant = cells[0]
                node = cells[1] if len(cells) > 1 else node
            elif cells[1] and not PRICE_UNIT_RE.search(cells[1]) and not VOLUME_UNIT_RE.search(cells[1]):
                node = cells[1]

        metric = metric_from_unit(unit_cell)
        if metric is None:
            # empty continuation without clear unit
            if PRICE_UNIT_RE.search(" ".join(cells[:3])):
                metric = "price"
                unit_cell = "тг/кВт*ч"
            elif VOLUME_UNIT_RE.search(" ".join(cells[:3])):
                metric = "volume"
                unit_cell = "тыс.кВт*ч"
            else:
                continue

        if SKIP_LABEL_RE.search(participant) and not participant.startswith("ТОО"):
            if section == "results" and not cells[0]:
                pass
            elif section == "bids" and cells[0] and not re.match(r"^\d+$", cells[0]):
                continue

        hour_vals, total = extract_hour_values(cells, hour_start)
        if not hour_vals:
            continue

        rows.extend(
            emit_hours(
                trade_date=meta.get("trade_date"),
                delivery_date=meta.get("delivery_date"),
                zone=zone,
                section=section,
                participant=participant,
                node=node,
                metric=metric,
                hour_vals=hour_vals,
                total=total if metric == "volume" else None,
                unit=unit_cell,
                source_file=source_file,
                file_id=file_id,
            )
        )
    return rows


def parse_demand_table(
    table: list[list[Any]],
    *,
    zone: str,
    meta: dict[str, Any],
    source_file: str,
    file_id: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in table:
        cells = [normalize_cell(c) for c in raw]
        joined = " ".join(cells)
        if SECTION_DEMAND.search(joined) and find_hour_start(cells) is None:
            continue
        z = detect_zone(joined) or zone
        # find first long numeric run
        hour_start = find_hour_start(cells)
        if hour_start is not None:
            # header
            continue
        nums_idx = None
        for i, c in enumerate(cells):
            if parse_ru_number(c) is not None and i + 5 < len(cells):
                # look ahead for more numbers
                if sum(1 for j in range(i, min(i + 8, len(cells))) if parse_ru_number(cells[j]) is not None) >= 5:
                    nums_idx = i
                    break
        if nums_idx is None:
            continue
        hour_vals, total = extract_hour_values(cells, nums_idx)
        if not hour_vals:
            continue
        rows.extend(
            emit_hours(
                trade_date=meta.get("trade_date"),
                delivery_date=meta.get("delivery_date"),
                zone=z,
                section="demand_so",
                participant="",
                node="",
                metric="demand",
                hour_vals=hour_vals,
                total=total,
                unit="тыс.кВт*ч",
                source_file=source_file,
                file_id=file_id,
            )
        )
    return rows


def parse_limits_table(
    table: list[list[Any]],
    *,
    zone: str,
    meta: dict[str, Any],
    source_file: str,
    file_id: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    header_idx = None
    hour_start = None
    for i, raw in enumerate(table[:4]):
        cells = [normalize_cell(c) for c in raw]
        hs = find_hour_start(cells)
        if hs is not None:
            header_idx = i
            hour_start = hs
            break
    if header_idx is None or hour_start is None:
        # sometimes hours start at col 2 without explicit "1","2" as separate if broken
        for i, raw in enumerate(table[:4]):
            cells = [normalize_cell(c) for c in raw]
            if re.search(r"Узел|Энергоузел|Энерготорап", " ".join(cells), re.IGNORECASE):
                header_idx = i
                hour_start = 2
                break
    if header_idx is None or hour_start is None:
        return rows

    for raw in table[header_idx + 1 :]:
        cells = [normalize_cell(c) for c in raw]
        if not any(cells):
            continue
        node = cells[0]
        if not node or TOTAL_RE.search(node) or SKIP_LABEL_RE.search(node) and "узел" not in node.lower():
            if not re.search(r"узел|торап|ВКО|Мангыстау|Атырау|Актюб|Павлодар|Экибастуз", node, re.IGNORECASE):
                continue
        unit_cell = cells[1] if len(cells) > 1 else ""
        metric = "min" if MIN_RE.search(unit_cell) else ("max" if MAX_RE.search(unit_cell) else None)
        if metric is None:
            label = (cells[1] if len(cells) > 1 else "").upper()
            if MIN_RE.search(label) or label == "MIN":
                metric = "min"
            elif MAX_RE.search(label) or label == "MAX":
                metric = "max"
            else:
                continue
        hour_vals, total = extract_hour_values(cells, hour_start)
        if not hour_vals:
            continue
        rows.extend(
            emit_hours(
                trade_date=meta.get("trade_date"),
                delivery_date=meta.get("delivery_date"),
                zone=zone,
                section="node_limit",
                participant="",
                node=node,
                metric=metric,
                hour_vals=hour_vals,
                total=total,
                unit="тыс.кВт*ч",
                source_file=source_file,
                file_id=file_id,
            )
        )
    return rows


def zone_switches_on_page(page: Any) -> list[tuple[float, str]]:
    """Y tops where zone headers appear on the page (pdfplumber coords)."""
    switches: list[tuple[float, str]] = []
    words = page.extract_words() or []
    bands: dict[int, list[str]] = {}
    tops: dict[int, float] = {}
    for w in words:
        key = int(round(float(w["top"])))
        bands.setdefault(key, []).append(w["text"])
        tops[key] = float(w["top"])
    for key, tokens in bands.items():
        line = " ".join(tokens)
        if re.search(
            r"Зона\s*торгов|Сауда-саттық\s*аймағы|Предварительные\s*итоги\s*ЦТ",
            line,
            re.IGNORECASE,
        ):
            z = detect_zone(line)
            if z:
                switches.append((tops[key], z))
        elif re.search(r"\(Запад\)", line, re.IGNORECASE):
            switches.append((tops[key], "west"))
        elif re.search(r"\(Север", line, re.IGNORECASE):
            switches.append((tops[key], "north_south"))
    switches.sort(key=lambda x: x[0])
    return switches


def zone_for_y(y: float, switches: list[tuple[float, str]], default: str) -> str:
    zone = default
    for top, z in switches:
        if y + 2 >= top:
            zone = z
        else:
            break
    return zone


def parse_pdf(pdf_path: Path) -> list[dict[str, Any]]:
    file_id = file_id_from_path(pdf_path)
    source_file = str(pdf_path.relative_to(ROOT))
    filename_date = delivery_date_from_name(pdf_path)
    all_rows: list[dict[str, Any]] = []
    texts: list[str] = []
    page_tables: list[list[tuple[float, list[list[Any]]]]] = []
    page_switches: list[list[tuple[float, str]]] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            texts.append(page.extract_text() or "")
            switches = zone_switches_on_page(page)
            page_switches.append(switches)
            found: list[tuple[float, list[list[Any]]]] = []
            for t in page.find_tables() or []:
                extracted = t.extract()
                if extracted:
                    found.append((float(t.bbox[1]), extracted))
            if not found:
                for tb in page.extract_tables() or []:
                    if tb:
                        found.append((0.0, tb))
            page_tables.append(found)

    meta = extract_meta("\n".join(texts), filename_date)
    zone = "north_south"

    for tables, switches in zip(page_tables, page_switches):
        page_default = zone
        if switches and switches[0][0] < 80:
            page_default = switches[0][1]
        for top, table in tables:
            local_zone = zone_from_table(
                table, zone_for_y(top, switches, page_default)
            )
            kind = classify_table(table)
            if kind in {"bids", "results"}:
                all_rows.extend(
                    parse_participant_table(
                        table,
                        section=kind,
                        zone=local_zone,
                        meta=meta,
                        source_file=source_file,
                        file_id=file_id,
                    )
                )
                zone = local_zone
            elif kind == "demand_so":
                all_rows.extend(
                    parse_demand_table(
                        table,
                        zone=local_zone,
                        meta=meta,
                        source_file=source_file,
                        file_id=file_id,
                    )
                )
            elif kind == "node_limit":
                all_rows.extend(
                    parse_limits_table(
                        table,
                        zone=local_zone,
                        meta=meta,
                        source_file=source_file,
                        file_id=file_id,
                    )
                )
                zone = local_zone
        if switches:
            zone = switches[-1][1]

    # Deduplicate
    seen: set[tuple] = set()
    unique: list[dict[str, Any]] = []
    for row in all_rows:
        key = (
            row["zone"],
            row["section"],
            row["participant"],
            row["node"],
            row["metric"],
            row["hour"],
            row["value"],
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)

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


def iter_pdfs() -> list[Path]:
    return sorted(PDF_DIR.rglob("*.pdf"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="Process at most N PDFs (0 = all)")
    parser.add_argument("--force", action="store_true", help="Re-parse even if CSV already exists")
    parser.add_argument("--year", type=str, default="", help="Only one year: 2023–2026")
    args = parser.parse_args()

    pdfs = iter_pdfs()
    if args.year:
        pdfs = [p for p in pdfs if p.relative_to(PDF_DIR).parts[0] == args.year]
    if not pdfs:
        print(f"No PDFs found under {PDF_DIR}")
        return

    ok = skipped = failed = 0
    for pdf_path in pdfs:
        file_id = file_id_from_path(pdf_path)
        delivery = delivery_date_from_name(pdf_path)
        out = csv_path_for(pdf_path, delivery, file_id)
        if out.exists() and not args.force:
            skipped += 1
            continue
        try:
            rows = parse_pdf(pdf_path)
            # prefer delivery_date from content for path consistency
            delivery = rows[0].get("delivery_date") or delivery
            out = csv_path_for(pdf_path, delivery, file_id)
            write_csv(out, rows)
            ok += 1
            print(f"  OK  {pdf_path.parent.name}/{pdf_path.name} → {out.relative_to(ROOT)} ({len(rows)} rows)")
            if args.limit and ok >= args.limit:
                break
        except Exception as exc:  # noqa: BLE001
            failed += 1
            append_error(
                {
                    "pdf": str(pdf_path.relative_to(ROOT)),
                    "error": str(exc),
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                }
            )
            print(f"  ERR {pdf_path.name}: {exc}")

    print(f"Done. ok={ok} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
