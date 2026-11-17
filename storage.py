"""CSV input and transactional SQLite persistence, separate from cleaning rules."""

import csv
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from cleaning import RAW_FIELDS, ClassifiedRow, classify_rows, summarize_rows


DEFAULT_DATABASE_PATH = Path(__file__).resolve().parent / "data" / "processed" / "pedidos.sqlite3"
SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS pedidos (
        pedido_id INTEGER PRIMARY KEY CHECK (pedido_id > 0),
        fecha TEXT NOT NULL,
        fecha_completado TEXT,
        cliente TEXT NOT NULL,
        país TEXT NOT NULL,
        producto TEXT NOT NULL,
        importe_centavos INTEGER NOT NULL
            CHECK (typeof(importe_centavos) = 'integer' AND importe_centavos > 0),
        moneda TEXT NOT NULL CHECK (moneda = 'USD'),
        estado TEXT NOT NULL CHECK (estado IN ('pendiente', 'completado', 'cancelado')),
        row_number INTEGER NOT NULL UNIQUE,
        original_json TEXT NOT NULL,
        CHECK ((estado = 'completado' AND fecha_completado IS NOT NULL
                AND fecha_completado >= fecha)
            OR (estado IN ('pendiente', 'cancelado') AND fecha_completado IS NULL))
    )""",
    """CREATE TABLE IF NOT EXISTS pedidos_rechazados (
        row_number INTEGER PRIMARY KEY,
        original_json TEXT NOT NULL,
        normalized_json TEXT NOT NULL,
        reasons_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS pedidos_duplicados (
        row_number INTEGER PRIMARY KEY,
        original_json TEXT NOT NULL,
        normalized_json TEXT NOT NULL,
        reasons_json TEXT NOT NULL,
        duplicate_of INTEGER NOT NULL CHECK (duplicate_of < row_number)
    )""",
)


def read_csv_rows(input_path: Path) -> list[dict]:
    with Path(input_path).open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source, restkey="__extra_columns__", strict=True)
        headers = reader.fieldnames
        if not headers or len(headers) != len(RAW_FIELDS) or set(headers) != RAW_FIELDS:
            raise ValueError("El CSV debe tener exactamente las columnas del generador, sin repetirlas.")
        return list(reader)


def encode_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def save_sqlite(records: list[ClassifiedRow], database_path: Path) -> dict:
    """Replace this dataset's three tables atomically; retain the old data on failure."""
    summary = summarize_rows(records)
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        with connection:
            # Explicit BEGIN also includes schema creation in the transaction.
            connection.execute("BEGIN IMMEDIATE")
            for statement in SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.execute("DELETE FROM pedidos_duplicados")
            connection.execute("DELETE FROM pedidos_rechazados")
            connection.execute("DELETE FROM pedidos")
            for record in records:
                if record.category == "accepted":
                    values = record.normalized
                    connection.execute(
                        """INSERT INTO pedidos (
                            pedido_id, fecha, fecha_completado, cliente, país, producto,
                            importe_centavos, moneda, estado, row_number, original_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (int(values["pedido_id"]), values["fecha"], values["fecha_completado"],
                         values["cliente"], values["país"], values["producto"],
                         values["importe_centavos"], values["moneda"], values["estado"],
                         record.row_number, encode_json(record.original)),
                    )
                elif record.category in {"rejected", "duplicate"}:
                    values = (record.row_number, encode_json(record.original),
                              encode_json(record.normalized), encode_json(record.reasons))
                    if record.category == "rejected":
                        connection.execute(
                            "INSERT INTO pedidos_rechazados VALUES (?, ?, ?, ?)", values,
                        )
                    else:
                        connection.execute(
                            "INSERT INTO pedidos_duplicados VALUES (?, ?, ?, ?, ?)",
                            (*values, record.duplicate_of),
                        )
                else:
                    raise ValueError(f"Categoría desconocida: {record.category}")
            stored_counts = {
                "accepted": connection.execute("SELECT COUNT(*) FROM pedidos").fetchone()[0],
                "rejected": connection.execute("SELECT COUNT(*) FROM pedidos_rechazados").fetchone()[0],
                "duplicate": connection.execute("SELECT COUNT(*) FROM pedidos_duplicados").fetchone()[0],
            }
            if any(count != summary[category] for category, count in stored_counts.items()):
                raise ValueError("Los conteos guardados no coinciden con la clasificación.")
    return summary


def clean_to_sqlite(input_path: Path, database_path: Path = DEFAULT_DATABASE_PATH) -> dict:
    input_path, database_path = Path(input_path), Path(database_path)
    if input_path.resolve() == database_path.resolve() or (
        input_path.exists() and database_path.exists() and input_path.samefile(database_path)
    ):
        raise ValueError("El CSV de entrada y la base SQLite deben ser archivos diferentes.")
    # Parse and classify completely before modifying an existing database.
    records = classify_rows(read_csv_rows(input_path))
    return save_sqlite(records, database_path)
