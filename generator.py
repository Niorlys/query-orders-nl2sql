"""Reproducible synthetic orders and an audit trail, using only the standard library."""

import csv
import hashlib
import io
import json
import random
from datetime import date, timedelta
from pathlib import Path

DEFAULT_SEED = 20250301
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "data" / "raw"
START_DATE = date(2024, 1, 1)
END_DATE = date(2025, 12, 15)
FIELD_NAMES = (
    "pedido_id",
    "fecha",
    "fecha_completado",
    "cliente",
    "país",
    "producto",
    "importe",
    "moneda",
    "estado",
    "cliente_id",
)
COUNTRY_VARIANTS = {
    "España": ("espana", "ESPAÑA", " España "),
    "México": ("mexico", "MÉXICO", " México "),
    "Argentina": ("argentina", "ARGENTINA", " Argentina "),
    "Colombia": ("colombia", "COLOMBIA", " Colombia "),
    "Chile": ("chile", "CHILE", " Chile "),
    "Estados Unidos": ("USA", "estados unidos", " Estados Unidos "),
}
# Prices in USD cents; one product per order.
PRODUCTS = (
    ("Teclado", 4500),
    ("Ratón", 2500),
    ("Monitor", 24900),
    ("Auriculares", 6900),
    ("Webcam", 5900),
    ("Portátil", 89900),
)
INVALID_DATES = ("2025-02-30", "2025-13-01", "2025-03", "")


def generate_dataset(seed: int = DEFAULT_SEED) -> tuple[list[dict], dict]:
    rng = random.Random(seed)
    original_count = rng.randint(1001, 2000)
    duplicate_count = rng.randint(0, 40)
    invalid_date_count = rng.randint(0, 40)
    empty_amount_count = rng.randint(0, 10)
    customers = [
        (str(index), f"Cliente {index:03d}", rng.choice(tuple(COUNTRY_VARIANTS)))
        for index in range(1, 121)
    ]
    rows = []
    for order_id in range(1, original_count + 1):
        created_date = START_DATE + timedelta(days=rng.randint(0, (END_DATE - START_DATE).days))
        status = rng.choices(("pendiente", "completado", "cancelado"), weights=(20, 70, 10), k=1)[0]
        completed_date = (
            created_date + timedelta(days=rng.randint(0, 14)) if status == "completado" else None
        )
        customer_id, customer, country = rng.choice(customers)
        product, amount_cents = rng.choice(PRODUCTS)
        rows.append(
            {
                "pedido_id": str(order_id),
                "fecha": created_date.isoformat(),
                "fecha_completado": completed_date.isoformat() if completed_date else "",
                "cliente": customer,
                "país": country,
                "producto": product,
                "importe": f"{amount_cents // 100}.{amount_cents % 100:02d}",
                "moneda": "USD",
                "estado": status,
                "cliente_id": customer_id,
            }
        )
    errors = []

    def inject_error(index: int, field: str, value: str, category: str) -> None:
        row = rows[index]
        errors.append(
            {
                "category": category,
                "order_id": row["pedido_id"],
                "field": field,
            }
        )
        row[field] = value

    # Independent samples can overlap; sorting keeps their traversal stable.
    for index in sorted(rng.sample(range(original_count), original_count // 4)):
        rows[index]["país"] = rng.choice(COUNTRY_VARIANTS[rows[index]["país"]])
    for index in sorted(rng.sample(range(original_count), original_count // 5)):
        field = rng.choice(("cliente", "producto", "estado"))
        rows[index][field] = f"  {rows[index][field]} "
    for index in sorted(rng.sample(range(original_count), original_count * 3 // 10)):
        fields = ["fecha", "fecha_completado"] if rows[index]["fecha_completado"] else ["fecha"]
        field = rng.choice(fields)
        parsed_date = date.fromisoformat(rows[index][field])
        date_format = rng.choice(("%d/%m/%Y", "%Y/%m/%d"))
        rows[index][field] = parsed_date.strftime(date_format)

    invalid_indices = sorted(rng.sample(range(original_count), invalid_date_count))
    empty_indices = sorted(rng.sample(range(original_count), empty_amount_count))
    for index in invalid_indices:
        fields = ["fecha", "fecha_completado"] if rows[index]["fecha_completado"] else ["fecha"]
        inject_error(index, rng.choice(fields), rng.choice(INVALID_DATES), "invalid_date")
    for index in empty_indices:
        inject_error(index, "importe", "", "empty_amount")

    # Copy raw rows after corrupting them, preserving the original order ID.
    duplicates = []
    for index in sorted(rng.sample(range(original_count), duplicate_count)):
        duplicates.append(
            {
                "order_id": rows[index]["pedido_id"],
                "source_csv_row_number": index + 2,
                "duplicate_csv_row_number": len(rows) + 2,
            }
        )
        rows.append(rows[index].copy())

    rejected_indices = set(invalid_indices) | set(empty_indices)
    counts = {
        "duplicate": duplicate_count,
        "invalid_date": invalid_date_count,
        "empty_amount": empty_amount_count,
        "country_variant": original_count // 4,
        "whitespace": original_count // 5,
        "complete_date_format": original_count * 3 // 10,
    }
    accepted_rows = [
        row for index, row in enumerate(rows[:original_count]) if index not in rejected_indices
    ]
    manifest = {
        "schema_version": 2,
        "seed": seed,
        "currency": "USD",
        "creation_date_range": {"start": START_DATE.isoformat(), "end": END_DATE.isoformat()},
        "completion_delay_days": {"min": 0, "max": 14},
        "original_order_count": original_count,
        "total_csv_row_count": len(rows),
        "injection_counts": counts,
        "injection_percentages_of_originals": {
            category: round(count / original_count * 100, 6) for category, count in counts.items()
        },
        "categories_may_overlap": True,
        "invalid_date_and_empty_amount_overlap_count": len(
            set(invalid_indices) & set(empty_indices)
        ),
        "errors": errors,
        "duplicates": duplicates,
        "expected_after_deduplication": {
            "accepted_order_count": len(accepted_rows),
            "rejected_order_count": len(rejected_indices),
            "rejected_order_ids": [str(index + 1) for index in sorted(rejected_indices)],
            "accepted_status_counts": {
                status: sum(row["estado"].strip() == status for row in accepted_rows)
                for status in ("pendiente", "completado", "cancelado")
            },
        },
    }
    return rows, manifest


def write_dataset(output_dir: Path = DEFAULT_OUTPUT_DIR, seed: int = DEFAULT_SEED) -> dict:
    """Replace the two raw artifacts with deterministic UTF-8/LF files."""
    rows, manifest = generate_dataset(seed)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=FIELD_NAMES, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    csv_bytes = buffer.getvalue().encode("utf-8")
    manifest["csv_file"] = "pedidos.csv"
    manifest["csv_sha256"] = hashlib.sha256(csv_bytes).hexdigest()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "pedidos.csv").write_bytes(csv_bytes)
    (output_dir / "manifest.json").write_bytes(
        (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    return manifest
