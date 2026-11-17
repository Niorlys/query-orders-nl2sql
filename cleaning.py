"""Pure normalization and exclusive row classification; no database or file I/O.

Cleaning rules are based on `DATA MINING AND PREDICTIVE ANALYTICS` book by DANIEL T. LAROSE and CHANTAL D. LAROSE

"""

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping


COUNTRY_ALIASES = {
    "espana": "España", "es": "España",
    "mexico": "México", "mx": "México",
    "argentina": "Argentina", "ar": "Argentina",
    "colombia": "Colombia", "co": "Colombia",
    "chile": "Chile", "cl": "Chile",
    "estados unidos": "Estados Unidos", "usa": "Estados Unidos",
    "us": "Estados Unidos", "united states": "Estados Unidos",
}
STATUSES = {"pendiente", "completado", "cancelado"}
RAW_FIELDS = {
    "pedido_id", "fecha", "fecha_completado", "cliente", "país",
    "producto", "importe", "moneda", "estado",
}
DATE_FORMATS = (
    (r"[0-9]{4}-[0-9]{2}-[0-9]{2}", "%Y-%m-%d"),
    (r"[0-9]{2}/[0-9]{2}/[0-9]{4}", "%d/%m/%Y"),
    (r"[0-9]{4}/[0-9]{2}/[0-9]{2}", "%Y/%m/%d"),
)
MAX_INTEGER = 2**63 - 1


def normalize_text(value: str | None) -> str:
    """Trim/collapse whitespace and normalize Unicode without changing text case."""
    if value is None:
        return ""
    return " ".join(unicodedata.normalize("NFC", value).split())


def normalize_country(value: str | None) -> str:
    text = normalize_text(value).casefold()
    key = "".join(character for character in unicodedata.normalize("NFD", text)
                  if not unicodedata.combining(character))
    if key not in COUNTRY_ALIASES:
        raise ValueError("unknown_country")
    return COUNTRY_ALIASES[key]


def normalize_status(value: str | None) -> str:
    status = normalize_text(value).casefold()
    if status not in STATUSES:
        raise ValueError("unknown_status")
    return status


def normalize_order_id(value: str | None) -> str:
    text = normalize_text(value)
    if not re.fullmatch(r"[0-9]+", text):
        raise ValueError("invalid_order_id")
    digits = text.lstrip("0")
    if not digits or len(digits) > 19 or int(digits) > MAX_INTEGER:
        raise ValueError("invalid_order_id")
    return str(int(digits))


def parse_date(value: str | None) -> str:
    """Accept only complete known calendar dates; return ISO, never infer a year."""
    text = normalize_text(value)
    for pattern, date_format in DATE_FORMATS:
        if re.fullmatch(pattern, text):
            try:
                return datetime.strptime(text, date_format).date().isoformat()
            except ValueError:
                break
    raise ValueError("invalid_date")


def parse_amount_cents(value: str | None) -> int:
    """Parse positive USD amounts exactly, without floats or rounding."""
    text = normalize_text(value)
    if not text:
        raise ValueError("missing_amount")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", text):
        raise ValueError("invalid_amount")
    whole, _, fraction = text.partition(".")
    whole = whole.lstrip("0") or "0"
    if len(whole) > 17:
        raise ValueError("amount_out_of_range")
    cents = int(whole) * 100 + int(fraction.ljust(2, "0"))
    if not 0 < cents <= MAX_INTEGER:
        raise ValueError("amount_out_of_range")
    return cents


@dataclass
class NormalizedRow:
    row_number: int
    original: dict
    normalized: dict
    reasons: list[str]


@dataclass
class ClassifiedRow(NormalizedRow):
    category: str
    duplicate_of: int | None = None


def normalize_row(row: Mapping[str, str | None], row_number: int) -> NormalizedRow:
    """Keep invalid values for audit/comparison and collect every detected reason."""
    original = dict(row)
    normalized = {field: normalize_text(row.get(field)) for field in sorted(RAW_FIELDS)}
    reasons = []
    # Input rows are CSV-style mappings. Schema errors must never be silently dropped.
    if set(row) - RAW_FIELDS:
        reasons.append("row:unexpected_fields")
    for field in sorted(RAW_FIELDS - set(row)):
        reasons.append(f"{field}:missing_field")

    parsers = {
        "pedido_id": normalize_order_id, "país": normalize_country,
        "estado": normalize_status, "fecha": parse_date, "importe": parse_amount_cents,
    }
    for field, parser in parsers.items():
        try:
            normalized[field] = parser(row.get(field))
        except ValueError as error:
            reasons.append(f"{field}:{error}")
    normalized["importe_centavos"] = normalized.pop("importe")
    for field in ("cliente", "producto"):
        if not normalized[field]:
            reasons.append(f"{field}:required")
    normalized["moneda"] = normalized["moneda"].upper()
    if normalized["moneda"] != "USD":
        reasons.append("moneda:unsupported_currency")

    completed_text = normalized["fecha_completado"]
    if completed_text:
        try:
            normalized["fecha_completado"] = parse_date(completed_text)
        except ValueError:
            reasons.append("fecha_completado:invalid_date")
    else:
        normalized["fecha_completado"] = None
    status = normalized["estado"]
    if status == "completado" and not completed_text:
        reasons.append("fecha_completado:required_for_completed")
    if status in {"pendiente", "cancelado"} and completed_text:
        reasons.append("fecha_completado:unexpected_for_status")
    if (completed_text and "fecha:invalid_date" not in reasons
            and "fecha_completado:invalid_date" not in reasons
            and normalized["fecha_completado"] < normalized["fecha"]):
        reasons.append("fecha_completado:before_creation")
    return NormalizedRow(row_number, original, normalized, reasons)


def classify_rows(
    rows: Iterable[Mapping[str, str | None]], *, first_row_number: int = 2,
) -> list[ClassifiedRow]:
    """Reject all conflicting ID versions; otherwise deduplicate before quality rejection.

    Identical invalid copies are duplicates of the first rejected row. Invalid IDs
    are always rejected individually. Row numbers are logical CSV records including
    the header, not physical lines (a quoted CSV value may span several lines).
    """
    prepared = [normalize_row(row, number)
                for number, row in enumerate(rows, start=first_row_number)]
    groups = defaultdict(list)
    for record in prepared:
        if "pedido_id:invalid_order_id" not in record.reasons:
            groups[record.normalized["pedido_id"]].append(record)
    conflicting_ids = set()
    for order_id, group in groups.items():
        first = group[0]
        # Reasons distinguish schema defects even if missing/blank values normalize alike.
        if any(record.normalized != first.normalized or record.reasons != first.reasons
               or {key: value for key, value in record.original.items() if key not in RAW_FIELDS}
               != {key: value for key, value in first.original.items() if key not in RAW_FIELDS}
               for record in group[1:]):
            conflicting_ids.add(order_id)

    results = []
    for record in prepared:
        reasons = record.reasons.copy()
        order_id = record.normalized["pedido_id"]
        duplicate_of = None
        if "pedido_id:invalid_order_id" in reasons:
            category = "rejected"
        elif order_id in conflicting_ids:
            category = "rejected"
            reasons.append("pedido_id:conflicting_contents")
        elif groups[order_id][0].row_number != record.row_number:
            category = "duplicate"
            duplicate_of = groups[order_id][0].row_number
            reasons.append("pedido_id:duplicate")
        else:
            category = "rejected" if reasons else "accepted"
        results.append(ClassifiedRow(
            record.row_number, record.original, record.normalized, reasons, category, duplicate_of,
        ))
    return results


def summarize_rows(rows: Iterable[ClassifiedRow]) -> dict:
    records = list(rows)
    counts = Counter(record.category for record in records)
    return {
        "total": len(records),
        "accepted": counts["accepted"], "duplicate": counts["duplicate"],
        "rejected": counts["rejected"],
        "rejection_reasons": dict(sorted(Counter(
            reason for record in records if record.category == "rejected"
            for reason in record.reasons
        ).items())),
    }
