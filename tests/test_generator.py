import csv
import hashlib
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from generator import COUNTRY_VARIANTS, DEFAULT_SEED, generate_dataset, write_dataset


def parse_complete_date(value):
    for date_format in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            pass
    raise ValueError(value)


def test_two_cli_runs_are_byte_identical(tmp_path):
    script = Path(__file__).resolve().parents[1] / "manage.py"
    for directory in (tmp_path / "first", tmp_path / "second"):
        subprocess.run(
            [sys.executable, str(script), "generate", "--output-dir", str(directory)],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
    for filename in ("pedidos.csv", "manifest.json"):
        assert (tmp_path / "first" / filename).read_bytes() == (
            tmp_path / "second" / filename
        ).read_bytes()


def test_manifest_matches_csv_and_exact_duplicates(tmp_path):
    manifest = write_dataset(tmp_path)
    csv_bytes = (tmp_path / "pedidos.csv").read_bytes()
    assert hashlib.sha256(csv_bytes).hexdigest() == manifest["csv_sha256"]
    assert json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8")) == manifest
    with (tmp_path / "pedidos.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    original_count = manifest["original_order_count"]
    assert len(rows) == manifest["total_csv_row_count"]
    assert len(rows) - original_count == manifest["injection_counts"]["duplicate"]
    assert len({row["pedido_id"] for row in rows}) == original_count
    assert [row["pedido_id"] for row in rows[:original_count]] == [
        str(index) for index in range(1, original_count + 1)
    ]
    for duplicate in manifest["duplicates"]:
        assert (
            rows[duplicate["source_csv_row_number"] - 2]
            == rows[duplicate["duplicate_csv_row_number"] - 2]
        )


def test_error_counts_and_business_rules():
    for seed in (DEFAULT_SEED, *range(20)):
        rows, manifest = generate_dataset(seed)
        original_count = manifest["original_order_count"]
        counts = manifest["injection_counts"]
        assert 1001 <= original_count <= 2000
        assert 0 <= counts["duplicate"] <= 40
        assert 0 <= counts["invalid_date"] <= 40
        assert 0 <= counts["empty_amount"] <= 10
        actual_counts = Counter(entry["category"] for entry in manifest["errors"])
        for category, count in counts.items():
            if category in ("invalid_date", "empty_amount"):
                assert actual_counts[category] == count
            assert manifest["injection_percentages_of_originals"][category] == round(
                count / original_count * 100, 6
            )
        invalid_ids, empty_ids = set(), set()
        customer_names = {}
        accepted_statuses = Counter()
        for row in rows[:original_count]:
            assert 1 <= int(row["cliente_id"]) <= 120
            customer_name = row["cliente"].strip()
            assert customer_name == f"Cliente {int(row['cliente_id']):03d}"
            assert customer_names.setdefault(row["cliente_id"], customer_name) == customer_name
            assert row["moneda"] == "USD"
            status = row["estado"].strip()
            assert status in ("pendiente", "completado", "cancelado")
            if status != "completado":
                assert row["fecha_completado"] == ""
            try:
                created_date = parse_complete_date(row["fecha"])
                if status == "completado":
                    completed_date = parse_complete_date(row["fecha_completado"])
                    assert 0 <= (completed_date - created_date).days <= 14
            except ValueError:
                invalid_ids.add(row["pedido_id"])
            if row["importe"] == "":
                empty_ids.add(row["pedido_id"])
            if row["pedido_id"] not in invalid_ids | empty_ids:
                accepted_statuses[status] += 1
        assert len(invalid_ids) == counts["invalid_date"]
        assert len(empty_ids) == counts["empty_amount"]
        assert manifest["invalid_date_and_empty_amount_overlap_count"] == len(
            invalid_ids & empty_ids
        )
        expected = manifest["expected_after_deduplication"]
        assert set(expected["rejected_order_ids"]) == invalid_ids | empty_ids
        assert expected["rejected_order_count"] == len(invalid_ids | empty_ids)
        assert expected["accepted_order_count"] == original_count - len(invalid_ids | empty_ids)
        assert expected["accepted_status_counts"] == dict(accepted_statuses)

        original_rows = rows[:original_count]
        assert (
            sum(row["país"] not in COUNTRY_VARIANTS for row in original_rows)
            == counts["country_variant"]
        )
        assert (
            sum(
                any(row[field] != row[field].strip() for field in ("cliente", "producto", "estado"))
                for row in original_rows
            )
            == counts["whitespace"]
        )
        formatted_count = sum(
            any("/" in row[field] for field in ("fecha", "fecha_completado"))
            for row in original_rows
        )
        # Invalid dates can overwrite a previously injected complete date format.
        assert (
            formatted_count
            <= counts["complete_date_format"]
            <= formatted_count + counts["invalid_date"]
        )


def test_manifest_errors_match_actual_rejection_reasons():
    rows, manifest = generate_dataset()
    original_rows = rows[: manifest["original_order_count"]]
    actual_errors = []
    for row in original_rows:
        fields = ["fecha"]
        if row["estado"].strip() == "completado":
            fields.append("fecha_completado")
        for field in fields:
            try:
                parse_complete_date(row[field])
            except ValueError:
                actual_errors.append((row["pedido_id"], field, "invalid_date"))
        if not row["importe"]:
            actual_errors.append((row["pedido_id"], "importe", "empty_amount"))
    recorded_errors = []
    for entry in manifest["errors"]:
        assert set(entry) == {"order_id", "field", "category"}
        recorded_errors.append((entry["order_id"], entry["field"], entry["category"]))
    assert sorted(recorded_errors) == sorted(actual_errors)
