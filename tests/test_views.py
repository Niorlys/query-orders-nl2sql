import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import storage
from cleaning import classify_rows
from storage import clean_to_sqlite, read_csv_rows, save_sqlite


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "orders_views.csv"
MONTHLY_QUERY = """SELECT anio_mes, país, numero_pedidos, importe_total_centavos, moneda
                   FROM ventas_por_mes_pais ORDER BY anio_mes, país"""
TOP_QUERY = """SELECT cliente_id, nombre, numero_pedidos, importe_total_centavos, moneda
               FROM top_10_clientes
               ORDER BY importe_total_centavos DESC, numero_pedidos DESC, cliente_id ASC"""


def test_known_monthly_totals_exclude_non_sales_and_use_completion_month(tmp_path):
    database_path = tmp_path / "orders.sqlite3"
    summary = clean_to_sqlite(FIXTURE_PATH, database_path)
    assert (summary["total"], summary["accepted"], summary["rejected"], summary["duplicate"]) == (19, 16, 2, 1)
    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute(MONTHLY_QUERY).fetchall() == [
            ("2024-03", "España", 1, 500, "USD"),
            ("2025-03", "España", 11, 24000, "USD"),
            ("2025-03", "México", 1, 15000, "USD"),
            ("2025-04", "España", 1, 10000, "USD"),
        ]
        # Valid pending/cancelled orders remain available in the underlying table.
        assert connection.execute("SELECT COUNT(*) FROM pedidos WHERE estado != 'completado'").fetchone()[0] == 2


def test_top_ten_ties_and_same_name_different_customer_ids(tmp_path):
    database_path = tmp_path / "orders.sqlite3"
    clean_to_sqlite(FIXTURE_PATH, database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute(TOP_QUERY).fetchall() == [
            (2, "Ana", 2, 15000, "USD"),
            (1, "Bruno", 1, 15000, "USD"),
            (3, "Ana", 2, 10500, "USD"),
            (4, "Dora", 1, 1000, "USD"),
            (5, "Elena", 1, 1000, "USD"),
            (6, "Felipe", 1, 1000, "USD"),
            (7, "Gema", 1, 1000, "USD"),
            (8, "Hugo", 1, 1000, "USD"),
            (9, "Irene", 1, 1000, "USD"),
            (10, "Julia", 1, 1000, "USD"),
        ]


def test_views_are_reproducible_after_reload_in_reverse_order(tmp_path):
    database_path = tmp_path / "orders.sqlite3"
    raw_rows = read_csv_rows(FIXTURE_PATH)
    snapshots = []
    for ordered_rows in (raw_rows, list(reversed(raw_rows))):
        save_sqlite(classify_rows(ordered_rows), database_path)
        with closing(sqlite3.connect(database_path)) as connection:
            snapshots.append((connection.execute(MONTHLY_QUERY).fetchall(),
                              connection.execute(TOP_QUERY).fetchall()))
    assert snapshots[0] == snapshots[1]


def test_customer_name_choice_is_deterministic_without_splitting_totals(tmp_path):
    rows = read_csv_rows(FIXTURE_PATH)[:2]
    rows[0]["cliente"] = "Ana Maria"
    database_path = tmp_path / "orders.sqlite3"
    save_sqlite(classify_rows(rows), database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute(TOP_QUERY).fetchall() == [(2, "Ana", 2, 15000, "USD")]


@pytest.mark.parametrize("include_non_sales", [False, True])
def test_no_sales_produces_empty_views(tmp_path, include_non_sales):
    rows = read_csv_rows(FIXTURE_PATH)[4:6] if include_non_sales else []
    database_path = tmp_path / "orders.sqlite3"
    save_sqlite(classify_rows(rows), database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        assert connection.execute(MONTHLY_QUERY).fetchall() == []
        assert connection.execute(TOP_QUERY).fetchall() == []


@pytest.mark.parametrize("broken_sql", [
    "DROP VIEW top_10_clientes;\nTHIS IS INVALID;\n",
    "DROP VIEW top_10_clientes;\nCREATE VIEW top_10_clientes AS SELECT nonexistent FROM pedidos;\n",
])
def test_broken_views_roll_back_data_and_previous_views(tmp_path, monkeypatch, broken_sql):
    database_path = tmp_path / "orders.sqlite3"
    clean_to_sqlite(FIXTURE_PATH, database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        before = list(connection.iterdump())
    broken_path = tmp_path / "broken.sql"
    broken_path.write_text(broken_sql, encoding="utf-8")
    monkeypatch.setattr(storage, "VIEWS_PATH", broken_path)
    # A different replacement dataset ensures a premature commit would lose data.
    with pytest.raises(sqlite3.Error):
        save_sqlite([], database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        assert list(connection.iterdump()) == before
        assert len(connection.execute(TOP_QUERY).fetchall()) == 10
