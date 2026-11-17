import csv
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from cleaning import classify_rows
from generator import FIELD_NAMES, write_dataset
from storage import SCHEMA_VERSION, clean_to_sqlite, read_csv_rows, save_sqlite


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "manage.py"


def run_clean(input_path, database_path, cwd):
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "clean", "--input", str(input_path),
         "--database", str(database_path)],
        cwd=cwd, capture_output=True, text=True,
    )


def database_snapshot(database_path):
    with closing(sqlite3.connect(database_path)) as connection:
        return list(connection.iterdump())


def test_cli_persists_counts_audit_and_integer_amounts_idempotently(tmp_path):
    input_dir = tmp_path / "raw"
    manifest = write_dataset(input_dir)
    input_path = input_dir / "pedidos.csv"
    database_path = tmp_path / "processed" / "orders.sqlite3"
    original_bytes = input_path.read_bytes()
    expected = manifest["expected_after_deduplication"]
    snapshots = []
    for _ in range(2):
        result = run_clean(input_path, database_path, tmp_path)
        assert result.returncode == 0, result.stderr
        assert "Conteos verificados en SQLite" in result.stdout
        assert f"Filas leídas: {manifest['total_csv_row_count']}" in result.stdout
        assert f"Cargadas (pedidos): {expected['accepted_order_count']}" in result.stdout
        assert f"Duplicadas (pedidos_duplicados): {manifest['injection_counts']['duplicate']}" in result.stdout
        assert f"Rechazadas (pedidos_rechazados): {expected['rejected_order_count']}" in result.stdout
        assert "Motivos de rechazo" in result.stdout
        assert "importe:missing_amount: 1" in result.stdout
        with closing(sqlite3.connect(database_path)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM pedidos").fetchone()[0] == expected["accepted_order_count"]
            assert connection.execute("SELECT COUNT(*) FROM pedidos_rechazados").fetchone()[0] == expected["rejected_order_count"]
            assert connection.execute("SELECT COUNT(*) FROM pedidos_duplicados").fetchone()[0] == manifest["injection_counts"]["duplicate"]
            assert dict(connection.execute("SELECT estado, COUNT(*) FROM pedidos GROUP BY estado")) == expected["accepted_status_counts"]
            assert connection.execute("SELECT DISTINCT typeof(importe_centavos) FROM pedidos").fetchall() == [("integer",)]
            assert connection.execute("SELECT COUNT(*) FROM pedidos WHERE estado != 'completado' AND fecha_completado IS NOT NULL").fetchone()[0] == 0
            records = classify_rows(read_csv_rows(input_path))
            for record in records:
                if record.category == "accepted":
                    stored = connection.execute(
                        "SELECT original_json, importe_centavos FROM pedidos WHERE row_number = ?",
                        (record.row_number,),
                    ).fetchone()
                    assert json.loads(stored[0]) == record.original
                    assert stored[1] == record.normalized["importe_centavos"]
                else:
                    table = "pedidos_rechazados" if record.category == "rejected" else "pedidos_duplicados"
                    stored = connection.execute(
                        f"SELECT original_json, normalized_json, reasons_json FROM {table} WHERE row_number = ?",
                        (record.row_number,),
                    ).fetchone()
                    assert [json.loads(value) for value in stored] == [record.original, record.normalized, record.reasons]
                    if record.category == "duplicate":
                        assert connection.execute(
                            "SELECT duplicate_of FROM pedidos_duplicados WHERE row_number = ?",
                            (record.row_number,),
                        ).fetchone()[0] == record.duplicate_of
        snapshots.append(database_snapshot(database_path))
    assert snapshots[0] == snapshots[1]
    assert input_path.read_bytes() == original_bytes


def test_failed_load_rolls_back_all_tables(tmp_path):
    write_dataset(tmp_path / "raw")
    input_path = tmp_path / "raw" / "pedidos.csv"
    database_path = tmp_path / "orders.sqlite3"
    clean_to_sqlite(input_path, database_path)
    before = database_snapshot(database_path)
    records = classify_rows(read_csv_rows(input_path))
    accepted = [record for record in records if record.category == "accepted"]
    # Fail after DELETE and after at least one INSERT, not before the transaction.
    accepted[1].normalized["importe_centavos"] = 0
    with pytest.raises(sqlite3.IntegrityError):
        save_sqlite(records, database_path)
    assert database_snapshot(database_path) == before


@pytest.mark.parametrize("contents", ["", "pedido_id,importe\n1,10\n", ",".join(["pedido_id"] * len(FIELD_NAMES))])
def test_invalid_csv_header_reports_error_without_changing_database(tmp_path, contents):
    write_dataset(tmp_path / "raw")
    database_path = tmp_path / "orders.sqlite3"
    clean_to_sqlite(tmp_path / "raw" / "pedidos.csv", database_path)
    before = database_snapshot(database_path)
    invalid_path = tmp_path / "invalid.csv"
    invalid_path.write_text(contents, encoding="utf-8")
    result = run_clean(invalid_path, database_path, tmp_path)
    assert result.returncode != 0
    assert "columnas" in result.stderr
    assert "Traceback" not in result.stderr
    assert database_snapshot(database_path) == before


def test_missing_input_reports_clear_error(tmp_path):
    database_path = tmp_path / "orders.sqlite3"
    result = run_clean(tmp_path / "missing.csv", database_path, tmp_path)
    assert result.returncode != 0
    assert "Error al limpiar/cargar pedidos" in result.stderr
    assert "Traceback" not in result.stderr
    assert not database_path.exists()


def test_input_cannot_be_used_as_database(tmp_path):
    write_dataset(tmp_path)
    input_path = tmp_path / "pedidos.csv"
    original_bytes = input_path.read_bytes()
    with pytest.raises(ValueError, match="archivos diferentes"):
        clean_to_sqlite(input_path, input_path)
    assert input_path.read_bytes() == original_bytes


def test_conflicts_and_extra_cells_are_preserved_in_rejections(tmp_path):
    input_path = tmp_path / "orders.csv"
    base = ["1", "2025-03-31", "2025-04-02", "Cliente Uno", "España", "Teclado", "100", "USD", "completado", "1"]
    changed = base.copy()
    changed[6] = "200"
    extra = base.copy()
    extra[0] = "2"
    extra.append("unexpected")
    with input_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(FIELD_NAMES)
        writer.writerows([base, changed, extra])
    database_path = tmp_path / "orders.sqlite3"
    summary = clean_to_sqlite(input_path, database_path)
    assert summary["accepted"] == 0
    assert summary["duplicate"] == 0
    assert summary["rejected"] == 3
    with closing(sqlite3.connect(database_path)) as connection:
        original, reasons = connection.execute(
            "SELECT original_json, reasons_json FROM pedidos_rechazados WHERE row_number = 4"
        ).fetchone()
        assert json.loads(original)["__extra_columns__"] == ["unexpected"]
        assert "row:unexpected_fields" in json.loads(reasons)


@pytest.mark.parametrize(("field", "value"), [
    ("pedido_id", None), ("pedido_id", 0), ("pedido_id", -1), ("pedido_id", 1.5),
    ("pedido_id", "invalid"), ("fecha", None), ("fecha", ""),
    ("cliente_id", None), ("cliente_id", 0), ("cliente_id", -1), ("cliente_id", 1.5),
    ("cliente", None), ("cliente", ""), ("cliente", " \t\n "),
    ("producto", None), ("producto", ""), ("producto", "   "),
    ("país", None), ("país", ""), ("país", "Unknown"),
    ("importe_centavos", None), ("importe_centavos", 0), ("importe_centavos", -1),
    ("importe_centavos", 1.5), ("importe_centavos", "invalid"),
    ("estado", None), ("estado", "pagado"), ("moneda", None), ("moneda", "EUR"),
    ("row_number", None), ("row_number", 1), ("row_number", 2.5),
    ("original_json", None), ("original_json", ""),
    ("fecha_completado", None), ("fecha_completado", "2024-12-31"),
])
def test_sqlite_rejects_invalid_orders_directly(tmp_path, field, value):
    database_path = tmp_path / "orders.sqlite3"
    save_sqlite([], database_path)
    values = {
        "pedido_id": 1, "fecha": "2025-01-01", "fecha_completado": "2025-01-02",
        "cliente_id": 1, "cliente": "Cliente Uno", "país": "España", "producto": "Teclado",
        "importe_centavos": 10000, "moneda": "USD", "estado": "completado",
        "row_number": 2, "original_json": "{}",
    }
    values[field] = value
    with closing(sqlite3.connect(database_path)) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO pedidos ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
                tuple(values.values()),
            )
        assert connection.execute("SELECT COUNT(*) FROM pedidos").fetchone()[0] == 0


@pytest.mark.parametrize("table", ["pedidos_rechazados", "pedidos_duplicados"])
@pytest.mark.parametrize(("field", "value"), [
    ("row_number", None), ("row_number", 0), ("original_json", None),
    ("normalized_json", None), ("reasons_json", None), ("reasons_json", ""),
])
def test_sqlite_rejects_invalid_audit_rows(tmp_path, table, field, value):
    database_path = tmp_path / "orders.sqlite3"
    save_sqlite([], database_path)
    values = {"row_number": 3, "original_json": "{}", "normalized_json": "{}", "reasons_json": '["reason"]'}
    if table == "pedidos_duplicados":
        values["duplicate_of"] = 2
    values[field] = value
    with closing(sqlite3.connect(database_path)) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
                tuple(values.values()),
            )


@pytest.mark.parametrize("legacy_version", [0, 1])
def test_legacy_schema_upgrade_is_transactional(tmp_path, legacy_version):
    database_path = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("CREATE TABLE pedidos (pedido_id INTEGER PRIMARY KEY, cliente TEXT)")
        connection.execute("INSERT INTO pedidos VALUES (1, 'Previous data')")
        connection.execute(f"PRAGMA user_version = {legacy_version}")
        connection.commit()
    before = database_snapshot(database_path)
    write_dataset(tmp_path / "raw")
    input_path = tmp_path / "raw" / "pedidos.csv"
    records = classify_rows(read_csv_rows(input_path))
    accepted = next(record for record in records if record.category == "accepted")
    accepted.normalized["cliente"] = ""
    with pytest.raises(sqlite3.IntegrityError):
        save_sqlite(records, database_path)
    assert database_snapshot(database_path) == before
    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == legacy_version
    clean_to_sqlite(input_path, database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE pedidos SET cliente = ''")


@pytest.mark.parametrize("field", ["pedido_id", "row_number"])
def test_sqlite_rejects_duplicate_identifiers(tmp_path, field):
    write_dataset(tmp_path / "raw")
    database_path = tmp_path / "orders.sqlite3"
    clean_to_sqlite(tmp_path / "raw" / "pedidos.csv", database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        values = connection.execute(f"SELECT {field} FROM pedidos ORDER BY pedido_id LIMIT 2").fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f"UPDATE pedidos SET {field} = ? WHERE {field} = ?", (values[0][0], values[1][0]))
