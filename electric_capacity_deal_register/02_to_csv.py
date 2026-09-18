#!/usr/bin/env python3
"""Parse KOREM TEM deal-registry PDFs from korem_files/ into CSV."""

from __future__ import annotations

import argparse
import csv
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pdfplumber
from ocrmac import ocrmac

ROOT = Path(__file__).resolve().parent
PDF_DIR = ROOT / "korem_files"
CSV_DIR = ROOT / "data" / "csv"
ERRORS_PATH = ROOT / "data" / "parse_errors.jsonl"

CSV_FIELDS = [
    "file_id",
    "trade_date",
    "registry_no",
    "delivery_start",
    "delivery_end",
    "zone_pair",
    "total_volume_mw",
    "transfer_capacity_mw",
    "limit_tariff",
    "row_num",
    "organization",
    "deal_number",
    "volume_mw",
    "price",
    "amount_no_vat",
    "amount_with_vat",
    "org_zone",
    "source_file",
]

DEAL_RE = re.compile(r"\b(\d{3})-(\d{6})\b")
DATE_RE = re.compile(r"(\d{2})[.\s]+(\d{2})[.\s]+(\d{4})")
PERIOD_RE = re.compile(
    r"(\d{2})[.\s]+(\d{2})[.\s]+(\d{4})\s*[-–—]\s*(\d{2})[.\s]+(\d{2})[.\s]+(\d{4})"
)
NUM_RE = re.compile(r"^-?\d+(?:[.,]\d+)?$")
ORG_HINT_RE = re.compile(
    r"(ТОО|TOO|АО|Акционерн|ЖШС|LLP|JSC|\")",
    re.IGNORECASE,
)


def trade_date_from_deal(deal_number: str) -> str | None:
    m = DEAL_RE.fullmatch(deal_number.strip())
    if not m:
        return None
    stamp = m.group(2)  # DDMMYY
    day, month, yy = int(stamp[:2]), int(stamp[2:4]), int(stamp[4:6])
    year = 2000 + yy
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return None


@dataclass
class OcrToken:
    text: str
    conf: float
    x: float
    y: float
    w: float
    h: float

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y_mid(self) -> float:
        return self.y + self.h / 2

    @property
    def x_mid(self) -> float:
        return self.x + self.w / 2


def parse_ru_number(raw: str | None) -> float | None:
    if raw is None:
        return None
    s = str(raw).replace("\xa0", " ").strip()
    s = s.replace("|", "").replace(" ", "").replace(",", ".")
    if not s or s in {"-", "—", "–"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(text: str) -> str | None:
    m = DATE_RE.search(text)
    if not m:
        return None
    d, mo, y = m.groups()
    try:
        return datetime(int(y), int(mo), int(d)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def file_id_from_path(path: Path) -> int | None:
    m = re.search(r"_(\d+)\.pdf$", path.name)
    return int(m.group(1)) if m else None


def csv_path_for(pdf_path: Path, pdf_dir: Path) -> Path:
    rel = pdf_path.relative_to(pdf_dir)
    return CSV_DIR / rel.with_suffix(".csv")


def ocr_image(img_path: Path) -> list[OcrToken]:
    raw = ocrmac.OCR(
        str(img_path),
        language_preference=["ru-RU", "en-US"],
    ).recognize()
    tokens: list[OcrToken] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        text, conf, bbox = item[0], float(item[1]), item[2]
        text = re.sub(r"\s+", " ", str(text)).strip()
        if not text:
            continue
        x, y, w, h = map(float, bbox)
        tokens.append(OcrToken(text=text, conf=conf, x=x, y=y, w=w, h=h))
    return tokens


def deal_y_spread(tokens: list[OcrToken]) -> float:
    ys = [t.y_mid for t in tokens if DEAL_RE.fullmatch(t.text.strip())]
    if len(ys) < 2:
        return 0.0
    return max(ys) - min(ys)


def orientation_score(tokens: list[OcrToken]) -> float:
    deals = [t for t in tokens if DEAL_RE.fullmatch(t.text.strip())]
    if not deals:
        return -1.0
    ys = [t.y_mid for t in deals]
    xs = [t.x_mid for t in deals]
    y_spread = max(ys) - min(ys) if len(ys) > 1 else 0.0
    x_spread = max(xs) - min(xs) if len(xs) > 1 else 0.0
    # Prefer a vertical column of deal numbers (large y-spread, small x-spread).
    if y_spread < 0.04:
        return y_spread * 0.1

    org_left = 0
    org_right = 0
    numeric_right = 0
    numeric_left = 0
    for d in deals:
        band = [t for t in tokens if abs(t.y_mid - d.y_mid) <= 0.018]
        left = [t for t in band if t.x_mid < d.x - 0.02]
        right = [t for t in band if t.x_mid > d.x2]
        if any(ORG_HINT_RE.search(t.text) for t in left):
            org_left += 1
        if any(ORG_HINT_RE.search(t.text) for t in right):
            org_right += 1
        if any(parse_ru_number(t.text) is not None for t in right):
            numeric_right += 1
        if any(parse_ru_number(t.text) is not None for t in left):
            numeric_left += 1

    x_consistency = 1.0 / (0.015 + x_spread)
    # Canonical layout: organization left, numbers right.
    return (
        y_spread * x_consistency
        + org_left * 3.0
        - org_right * 2.0
        + numeric_right * 2.0
        - numeric_left * 1.0
        + len(deals) * 0.05
    )


def layout_is_mirrored(tokens: list[OcrToken], deal_tokens: list[OcrToken]) -> bool:
    left_org = right_org = 0
    for d in deal_tokens:
        band = [t for t in tokens if abs(t.y_mid - d.y_mid) <= 0.02]
        if any(ORG_HINT_RE.search(t.text) for t in band if t.x_mid < d.x - 0.02):
            left_org += 1
        if any(ORG_HINT_RE.search(t.text) for t in band if t.x_mid > d.x2):
            right_org += 1
    return right_org > left_org


def ocr_page(page: Any, resolution: int = 240) -> list[OcrToken]:
    from PIL import Image

    tmp_path = Path(tempfile.mkstemp(suffix=".png")[1])
    base: Image.Image | None = None
    try:
        page.to_image(resolution=resolution).save(tmp_path)
        base = Image.open(tmp_path).convert("RGB")
        candidates: list[tuple[float, list[OcrToken]]] = []

        def try_img(img: Image.Image, tag: str) -> None:
            path = tmp_path.with_name(tmp_path.stem + f"_{tag}.png")
            img.save(path)
            try:
                toks = ocr_image(path)
            finally:
                path.unlink(missing_ok=True)
            candidates.append((orientation_score(toks), toks))

        try_img(base, "0")
        # Probe rotations when upright OCR doesn't look like a vertical deal table.
        if candidates[0][0] < 8.0:
            try_img(base.rotate(90, expand=True), "90")
            try_img(base.rotate(-90, expand=True), "270")
            try_img(base.rotate(180, expand=True), "180")

        candidates.sort(key=lambda c: c[0], reverse=True)
        return candidates[0][1]
    finally:
        tmp_path.unlink(missing_ok=True)
        if base is not None:
            base.close()


def join_text(tokens: list[OcrToken]) -> str:
    # Vision y grows upward; sort top→bottom, left→right.
    ordered = sorted(tokens, key=lambda t: (-t.y_mid, t.x))
    return "\n".join(t.text for t in ordered)


def tokens_in_band(
    tokens: list[OcrToken], y_mid: float, tol: float = 0.028
) -> list[OcrToken]:
    return [t for t in tokens if abs(t.y_mid - y_mid) <= tol]


def pick_number_in_x(
    tokens: list[OcrToken], x0: float, x1: float, *, prefer: str = "first"
) -> float | None:
    cands: list[tuple[float, float]] = []
    for t in tokens:
        if not (x0 <= t.x_mid <= x1):
            continue
        val = parse_ru_number(t.text)
        if val is None:
            continue
        cands.append((t.x_mid, val))
    if not cands:
        return None
    cands.sort(key=lambda p: p[0])
    vals = [v for _, v in cands]
    if prefer == "max_nonzero":
        nonzero = [v for v in vals if v != 0]
        return max(nonzero) if nonzero else vals[0]
    if prefer == "last":
        return vals[-1]
    return vals[0]


def pick_text_in_x(
    tokens: list[OcrToken], x0: float, x1: float, *, prefer_org: bool = False
) -> str:
    parts = [
        t
        for t in sorted(tokens, key=lambda t: (t.x, -t.y))
        if x0 <= t.x_mid <= x1
    ]
    if prefer_org:
        orgish = [t for t in parts if ORG_HINT_RE.search(t.text)]
        if orgish:
            parts = orgish
    return " ".join(t.text for t in parts).strip()


def extract_header(tokens: list[OcrToken], text: str) -> dict[str, Any]:
    trade_date = None
    # Prefer date near title / top of page
    top = [t for t in tokens if t.y_mid >= 0.85]
    for t in sorted(top, key=lambda t: -t.y_mid):
        trade_date = parse_date(t.text)
        if trade_date:
            break
    if not trade_date:
        trade_date = parse_date(text)

    registry_no = None
    m = re.search(r"Реестр\s*сделок\s*(\d+)", text, re.IGNORECASE)
    if m:
        registry_no = int(m.group(1))

    delivery_start = delivery_end = None
    pm = PERIOD_RE.search(text.replace("\n", " "))
    if pm:
        d1, m1, y1, d2, m2, y2 = pm.groups()
        try:
            delivery_start = datetime(int(y1), int(m1), int(d1)).strftime("%Y-%m-%d")
            delivery_end = datetime(int(y2), int(m2), int(d2)).strftime("%Y-%m-%d")
        except ValueError:
            pass

    zone_pair = None
    for t in tokens:
        low = t.text.lower()
        if "зона торгов" in low:
            nearby = [
                n
                for n in tokens_in_band(tokens, t.y_mid, tol=0.05)
                if n.x_mid > t.x_mid
                and "период" not in n.text.lower()
                and "объем" not in n.text.lower()
                and "объём" not in n.text.lower()
            ]
            nearby.sort(key=lambda n: (abs(n.y_mid - t.y_mid), n.x))
            parts = []
            for n in nearby:
                if n.x_mid > 0.75:
                    continue
                parts.append(n.text)
            joined = re.sub(r"\s+", " ", " ".join(parts)).strip(" -–—")
            joined = re.split(r"Период\s*поставки", joined, flags=re.IGNORECASE)[0].strip(
                " -–—"
            )
            if re.search(r"Север|Южн|Запад|зон", joined, re.IGNORECASE):
                joined = re.sub(
                    r"\d{2}[.\s]+\d{2}[.\s]+\d{4}.*$",
                    "",
                    joined,
                ).strip(" -–—")
                zone_pair = joined
                break
    if not zone_pair:
        zm = re.search(
            r"((?:Северн\w*|Южн\w*|Западн\w*|Солт\w*|Оңт\w*|Батыс)[^\n]{0,80}"
            r"[-–—][^\n]{0,80}(?:Северн\w*|Южн\w*|Западн\w*|зона))",
            text,
            re.IGNORECASE,
        )
        if zm:
            zone_pair = re.sub(r"\s+", " ", zm.group(1)).strip()

    # Header KPI numbers sit around mid-right, above the table (~y 0.64–0.78)
    header_nums = [
        (t.y_mid, parse_ru_number(t.text))
        for t in tokens
        if 0.64 <= t.y_mid <= 0.78 and 0.45 <= t.x_mid <= 0.58
    ]
    header_nums = [(y, v) for y, v in header_nums if v is not None]
    header_nums.sort(key=lambda p: -p[0])

    total_volume = None
    transfer_capacity = None
    limit_tariff = None
    if header_nums:
        # Typical order top→bottom: west volume / mangystau / total / transfer / tariff
        vals = [v for _, v in header_nums]
        if len(vals) >= 1:
            # Prefer the repeated/sum volume near "Суммарный"
            total_volume = vals[min(2, len(vals) - 1)] if len(vals) >= 3 else vals[0]
        if len(vals) >= 4:
            transfer_capacity = vals[3]
        if len(vals) >= 5:
            limit_tariff = vals[4]
        elif len(vals) >= 2:
            # fallback: last header number often tariff
            limit_tariff = vals[-1]

    # Stronger tariff lookup: number near "Предельный тариф"
    for t in tokens:
        if "Предельный" in t.text or "тариф" in t.text.lower():
            nearby = tokens_in_band(tokens, t.y_mid, tol=0.035)
            for n in nearby:
                if n.x_mid > 0.45:
                    val = parse_ru_number(n.text)
                    if val is not None and val >= 100:
                        limit_tariff = val

    # Total volume near "Суммарный"
    for t in tokens:
        if "Суммарный" in t.text or "Суммарн" in t.text:
            nearby = tokens_in_band(tokens, t.y_mid, tol=0.04)
            for n in sorted(nearby, key=lambda z: z.x):
                if n.x_mid > 0.45:
                    val = parse_ru_number(n.text)
                    if val is not None:
                        total_volume = val
                        break

    for t in tokens:
        if "ропускн" in t.text.lower() or "Пропускн" in t.text:
            nearby = tokens_in_band(tokens, t.y_mid, tol=0.04)
            for n in sorted(nearby, key=lambda z: z.x):
                if n.x_mid > 0.45:
                    val = parse_ru_number(n.text)
                    if val is not None:
                        transfer_capacity = val
                        break

    return {
        "trade_date": trade_date,
        "registry_no": registry_no,
        "delivery_start": delivery_start,
        "delivery_end": delivery_end,
        "zone_pair": zone_pair,
        "total_volume_mw": total_volume,
        "transfer_capacity_mw": transfer_capacity,
        "limit_tariff": limit_tariff,
    }


def deal_row_bands(deal_tokens: list[OcrToken]) -> list[tuple[OcrToken, float, float]]:
    """Return (deal_token, y_bottom, y_top] midpoints between neighboring deals."""
    deal_tokens = sorted(deal_tokens, key=lambda t: -t.y_mid)
    bands: list[tuple[OcrToken, float, float]] = []
    for i, dt in enumerate(deal_tokens):
        prev_y = deal_tokens[i - 1].y_mid if i > 0 else dt.y_mid + 0.03
        next_y = (
            deal_tokens[i + 1].y_mid if i + 1 < len(deal_tokens) else dt.y_mid - 0.03
        )
        y_top = (dt.y_mid + prev_y) / 2
        y_bot = (dt.y_mid + next_y) / 2
        # Keep a tiny minimum height for sparse tables.
        if y_top - y_bot < 0.008:
            pad = (0.008 - (y_top - y_bot)) / 2
            y_top += pad
            y_bot -= pad
        bands.append((dt, y_bot, y_top))
    return bands


def infer_volume(volume: float | None, price: float | None, amount: float | None) -> float | None:
    if volume is not None:
        return volume
    if price and amount and price != 0:
        inferred = amount / price
        # Round lightly — volumes are usually *.0 / *.8 style.
        return round(inferred, 4)
    return None


def extract_deals(tokens: list[OcrToken], meta: dict[str, Any]) -> list[dict[str, Any]]:
    deal_tokens = [t for t in tokens if DEAL_RE.fullmatch(t.text.strip())]
    if not deal_tokens:
        return []

    mirrored = layout_is_mirrored(tokens, deal_tokens)
    deals: list[dict[str, Any]] = []
    for dt, y_bot, y_top in deal_row_bands(deal_tokens):
        m = DEAL_RE.fullmatch(dt.text.strip())
        assert m
        row_num = int(m.group(1))
        band = [t for t in tokens if y_bot < t.y_mid <= y_top]

        org_side = [t for t in band if t.x_mid > dt.x2] if mirrored else [
            t for t in band if t.x_mid < dt.x - 0.02
        ]
        num_side = [t for t in band if t.x_mid < dt.x - 0.02] if mirrored else [
            t for t in band if t.x_mid > dt.x2
        ]

        organization = pick_text_in_x(
            org_side,
            min((t.x for t in org_side), default=0.0),
            max((t.x2 for t in org_side), default=1.0),
            prefer_org=True,
        )
        if not organization:
            organization = " ".join(
                t.text
                for t in sorted(org_side, key=lambda z: z.x)
                if not NUM_RE.match(t.text)
            ).strip()
        organization = re.sub(r"^\d+\s*", "", organization).strip()
        if organization.lower() in {"оператор торгов", "председатель правления"}:
            organization = ""
        if re.search(r"Номер\s*сдел|централизованн|электрической\s*мощн", organization, re.I):
            organization = ""

        if mirrored:
            # Numbers grow right→left toward the deal id: amounts, price, volume.
            amount_with_vat = pick_number_in_x(num_side, 0.10, 0.23)
            amount_no_vat = pick_number_in_x(num_side, 0.22, 0.32)
            price = pick_number_in_x(num_side, 0.30, 0.40)
            volume = pick_number_in_x(num_side, 0.40, 0.55, prefer="max_nonzero")
            zone_parts = [
                t.text
                for t in sorted(num_side, key=lambda z: z.x)
                if 0.50 <= t.x_mid <= 0.62
                and re.search(r"зон|Север|Южн|Запад|Батыс", t.text, re.IGNORECASE)
            ]
        else:
            volume = pick_number_in_x(num_side, 0.48, 0.60, prefer="max_nonzero")
            price = pick_number_in_x(num_side, 0.60, 0.72)
            amount_no_vat = pick_number_in_x(num_side, 0.70, 0.81)
            amount_with_vat = pick_number_in_x(num_side, 0.78, 0.90)
            if amount_with_vat is not None and amount_no_vat is not None:
                if amount_with_vat == amount_no_vat:
                    amount_with_vat = pick_number_in_x(num_side, 0.80, 0.92, prefer="last")
            zone_parts = [
                t.text
                for t in sorted(num_side, key=lambda z: z.x)
                if t.x_mid >= 0.85
                and re.search(r"зон|Север|Южн|Запад|Батыс", t.text, re.IGNORECASE)
            ]

        volume = infer_volume(volume, price, amount_no_vat)
        org_zone = re.sub(r"\s+", " ", " ".join(zone_parts)).strip()

        deal_number = dt.text.strip()
        inferred_date = trade_date_from_deal(deal_number)
        row_meta = dict(meta)
        if inferred_date:
            row_meta["trade_date"] = inferred_date

        deals.append(
            {
                **row_meta,
                "row_num": row_num,
                "organization": organization,
                "deal_number": deal_number,
                "volume_mw": volume,
                "price": price,
                "amount_no_vat": amount_no_vat,
                "amount_with_vat": amount_with_vat,
                "org_zone": org_zone,
            }
        )

    # Drop OCR false-positives that have no numeric payload on the row.
    deals = [
        d
        for d in deals
        if d.get("volume_mw") is not None
        or d.get("price") is not None
        or d.get("amount_no_vat") is not None
    ]

    vol_sum = sum(d["volume_mw"] for d in deals if d.get("volume_mw") is not None)
    if deals and (meta.get("total_volume_mw") is None or meta.get("total_volume_mw") == 0):
        meta["total_volume_mw"] = round(vol_sum, 4) if vol_sum else meta.get("total_volume_mw")
        for d in deals:
            d["total_volume_mw"] = meta["total_volume_mw"]
    elif deals and vol_sum and meta.get("total_volume_mw"):
        # If header total looks like transfer capacity (far from deal sum), prefer deal sum.
        header_total = float(meta["total_volume_mw"])
        if abs(header_total - vol_sum) > max(5.0, 0.25 * vol_sum):
            meta["total_volume_mw"] = round(vol_sum, 4)
            for d in deals:
                d["total_volume_mw"] = meta["total_volume_mw"]

    return deals


def parse_pdf(pdf_path: Path) -> list[dict[str, Any]]:
    file_id = file_id_from_path(pdf_path)
    source_file = str(pdf_path.relative_to(ROOT))
    all_deals: list[dict[str, Any]] = []

    with pdfplumber.open(pdf_path) as pdf:
        if not pdf.pages:
            raise ValueError("PDF has no pages")
        for page in pdf.pages:
            tokens = ocr_page(page)
            if not tokens:
                continue
            text = join_text(tokens)
            meta = extract_header(tokens, text)
            deals = extract_deals(tokens, meta)
            for deal in deals:
                deal["file_id"] = file_id if file_id is not None else ""
                deal["source_file"] = source_file
                all_deals.append(deal)

    if not all_deals:
        raise ValueError("No deals extracted from PDF")

    # Deduplicate by deal_number within file
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in all_deals:
        key = str(row.get("deal_number") or "")
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = {k: row.get(k, "") for k in CSV_FIELDS}
            for k, v in out.items():
                if v is None:
                    out[k] = ""
            writer.writerow(out)


def append_error(row: dict[str, Any]) -> None:
    ERRORS_PATH.parent.mkdir(parents=True, exist_ok=True)
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
    parser.add_argument(
        "--pdf-dir",
        type=Path,
        default=PDF_DIR,
        help="Directory with TEM PDFs",
    )
    args = parser.parse_args()

    pdf_dir = args.pdf_dir
    pdfs = sorted(pdf_dir.rglob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found under {pdf_dir}")
        return

    ok = 0
    skipped = 0
    failed = 0

    for pdf_path in pdfs:
        out = csv_path_for(pdf_path, pdf_dir)
        if out.exists() and not args.force:
            skipped += 1
            continue
        try:
            rows = parse_pdf(pdf_path)
            write_csv(out, rows)
            ok += 1
            print(f"  OK  {pdf_path.name} → {out.relative_to(ROOT)} ({len(rows)} deals)")
            if args.limit and ok >= args.limit:
                break
        except Exception as exc:  # noqa: BLE001
            failed += 1
            append_error(
                {
                    "pdf": str(pdf_path.relative_to(ROOT)),
                    "error": str(exc),
                    "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            )
            print(f"  ERR {pdf_path.name}: {exc}")

    print(f"Done. ok={ok} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
