from copy import deepcopy

import pytest

from cleaning import (
    classify_rows, normalize_country, normalize_row, normalize_status,
    normalize_text, parse_amount_cents, parse_date, summarize_rows,
)
from generator import DEFAULT_SEED, generate_dataset


def make_row(**changes):
    row = {
        "pedido_id": "1", "fecha": "2025-03-31", "fecha_completado": "2025-04-02",
        "cliente": "Cliente Uno", "país": "España", "producto": "Teclado",
        "importe": "100.00", "moneda": "USD", "estado": "completado",
    }
    return row | changes


@pytest.mark.parametrize(("raw", "expected"), [
    (" ESPANA ", "España"), ("me\u0301xico", "México"), (" usa ", "Estados Unidos"),
    ("Estados   Unidos", "Estados Unidos"), ("co", "Colombia"),
])
def test_country_normalization(raw, expected):
    assert normalize_country(raw) == expected


def test_normalization_preserves_original_data():
    raw = make_row(pedido_id=" 0001 ", cliente="  Cliente\t Uno ", país=" ESPANA ",
                   estado=" COMPLETADO ", moneda=" usd ", importe=" 100.0 ",
                   fecha="31/03/2025", fecha_completado="2025/04/02")
    untouched = deepcopy(raw)
    record = normalize_row(raw, 8)
    assert not record.reasons
    assert record.row_number == 8
    assert record.original == raw == untouched
    assert record.normalized == {
        **{key: value for key, value in make_row().items() if key != "importe"},
        "importe_centavos": 10000,
    }
    assert normalize_text("  Cliente\u00a0 Uno ") == "Cliente Uno"
    assert normalize_status(" Cancelado ") == "cancelado"
    record.original["cliente"] = "changed"
    assert raw == untouched


@pytest.mark.parametrize(("raw", "expected"), [
    ("2024-02-29", "2024-02-29"), ("29/02/2024", "2024-02-29"),
    ("2024/02/29", "2024-02-29"), ("03/04/2025", "2025-04-03"),
])
def test_known_dates(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", [
    "2025-02-29", "2025-02-30", "31/04/2025", "2025-13-01", "2025-03",
    "31/03", "03/31/2025", "2025-3-1", "", None, "0000-01-01",
])
def test_impossible_incomplete_or_unknown_dates(raw):
    with pytest.raises(ValueError, match="invalid_date"):
        parse_date(raw)


@pytest.mark.parametrize(("raw", "expected"), [
    ("0.01", 1), ("0.29", 29), (" 12.3 ", 1230), ("100", 10000),
    ("001.20", 120), ("90071992547409.93", 9007199254740993),
    ("92233720368547758.07", 2**63 - 1),
])
def test_amounts_are_exact_integer_cents(raw, expected):
    result = parse_amount_cents(raw)
    assert result == expected
    assert type(result) is int


@pytest.mark.parametrize("raw", [
    "", "  ", None, "abc", "NaN", "Infinity", "1e2", "1,23", "1,000.00",
    "$12", "-1", "0", "0.00", "1.001", ".50", "1.", "92233720368547758.08",
])
def test_missing_invalid_or_unsupported_amounts(raw):
    with pytest.raises(ValueError):
        parse_amount_cents(raw)


def test_normalized_duplicates_and_repeated_invalid_rows_are_exclusive():
    rows = [
        make_row(),
        make_row(pedido_id="001", país="espana", importe="100", fecha="31/03/2025"),
        make_row(pedido_id="2", importe="", fecha="2025-02-30"),
        make_row(pedido_id=" 02 ", importe="  ", fecha="2025-02-30"),
    ]
    records = classify_rows(rows)
    assert [record.category for record in records] == ["accepted", "duplicate", "rejected", "duplicate"]
    assert records[1].duplicate_of == 2
    assert records[3].duplicate_of == 4
    assert set(records[2].reasons) == {"importe:missing_amount", "fecha:invalid_date"}
    assert "importe:missing_amount" in records[3].reasons
    assert "pedido_id:duplicate" in records[3].reasons
    summary = summarize_rows(records)
    assert summary == {
        "total": 4, "accepted": 1, "duplicate": 2, "rejected": 1,
        "rejection_reasons": {"fecha:invalid_date": 1, "importe:missing_amount": 1},
    }


@pytest.mark.parametrize("changes", [
    {"importe": "200"}, {"importe": ""}, {"cliente": "Otro cliente"},
    {"fecha_completado": "2025-04-03"}, {"estado": "cancelado", "fecha_completado": ""},
    {"país": "México"}, {"producto": "Monitor"},
])
def test_all_versions_of_conflicting_id_are_rejected(changes):
    rows = [make_row(), make_row(**changes), make_row()]
    for ordered_rows in (rows, list(reversed(rows))):
        records = classify_rows(ordered_rows)
        assert all(record.category == "rejected" for record in records)
        assert all("pedido_id:conflicting_contents" in record.reasons for record in records)
        assert all(record.duplicate_of is None for record in records)


@pytest.mark.parametrize("order_id", ["", "0", "-1", "1.0", "ABC", None, "9223372036854775808"])
def test_invalid_ids_are_not_deduplicated(order_id):
    records = classify_rows([make_row(pedido_id=order_id), make_row(pedido_id=order_id)])
    assert all(record.category == "rejected" for record in records)
    assert all(record.duplicate_of is None for record in records)
    assert all("pedido_id:invalid_order_id" in record.reasons for record in records)


@pytest.mark.parametrize("status", ["pendiente", "cancelado"])
def test_uncompleted_orders_are_accepted_with_empty_completion_date(status):
    record = classify_rows([make_row(estado=status, fecha_completado="")])[0]
    assert record.category == "accepted"
    assert record.normalized["fecha_completado"] is None


@pytest.mark.parametrize(("changes", "reason"), [
    ({"fecha_completado": ""}, "fecha_completado:required_for_completed"),
    ({"fecha_completado": "2025-03-30"}, "fecha_completado:before_creation"),
    ({"fecha_completado": "2025-04"}, "fecha_completado:invalid_date"),
    ({"estado": "pendiente"}, "fecha_completado:unexpected_for_status"),
    ({"estado": "cancelado"}, "fecha_completado:unexpected_for_status"),
    ({"estado": "pagado"}, "estado:unknown_status"),
    ({"país": "desconocido"}, "país:unknown_country"),
    ({"moneda": "EUR"}, "moneda:unsupported_currency"),
    ({"cliente": " "}, "cliente:required"),
    ({"producto": ""}, "producto:required"),
])
def test_business_validation_reasons(changes, reason):
    record = classify_rows([make_row(**changes)])[0]
    assert record.category == "rejected"
    assert reason in record.reasons


def test_schema_errors_and_empty_input():
    assert summarize_rows(classify_rows([]))["total"] == 0
    row = make_row()
    del row["importe"]
    row["extra"] = "unexpected"
    record = classify_rows([row], first_row_number=10)[0]
    assert record.category == "rejected"
    assert record.row_number == 10
    assert "importe:missing_field" in record.reasons
    assert "importe:missing_amount" in record.reasons
    assert "row:unexpected_fields" in record.reasons
    assert record.original == row


@pytest.mark.parametrize("seed", [DEFAULT_SEED, 0, 1, 2, 42])
def test_generated_dataset_matches_manifest_without_using_it_for_cleaning(seed):
    rows, manifest = generate_dataset(seed)
    untouched = deepcopy(rows)
    records = classify_rows(rows)
    assert rows == untouched
    summary = summarize_rows(records)
    expected = manifest["expected_after_deduplication"]
    assert summary["total"] == len(rows)
    assert summary["total"] == sum(summary[category] for category in ("accepted", "duplicate", "rejected"))
    assert summary["accepted"] == expected["accepted_order_count"]
    assert summary["rejected"] == expected["rejected_order_count"]
    assert summary["duplicate"] == manifest["injection_counts"]["duplicate"]
    assert {record.normalized["pedido_id"] for record in records if record.category == "rejected"} == set(expected["rejected_order_ids"])
    for status, count in expected["accepted_status_counts"].items():
        assert sum(record.category == "accepted" and record.normalized["estado"] == status
                   for record in records) == count
