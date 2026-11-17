import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse

from errors import (
    InternalServerError,
    InvalidLLMResponseError,
    QueryResourceLimitError,
    SQLPolicyViolationError,
)
from query_executor import ExecutionBudget, ExecutionLimits, execute_query, readonly_connection
from sql_policy import MAX_SQL_BYTES, validate_sql
from storage import clean_to_sqlite


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "orders_views.csv"


@pytest.fixture
def database_path(tmp_path):
    path = tmp_path / "orders ?mode=rw#.sqlite3"
    clean_to_sqlite(FIXTURE_PATH, path)
    return path


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT COUNT(*) FROM pedidos;",
        "SELECT país, SUM(importe_centavos) FROM pedidos WHERE estado='completado' GROUP BY país HAVING COUNT(*)>0 ORDER BY país",
        "SELECT * FROM ventas_por_mes_pais ORDER BY anio_mes, país",
        "SELECT * FROM top_10_clientes ORDER BY importe_total_centavos DESC, numero_pedidos DESC, cliente_id",
        "SELECT p.pedido_id FROM pedidos p WHERE EXISTS (SELECT 1 FROM pedidos q WHERE q.cliente_id = p.cliente_id)",
        "SELECT * FROM (SELECT cliente_id FROM pedidos WHERE importe_centavos > 1000) AS selected ORDER BY cliente_id",
        "SELECT p.pedido_id FROM pedidos p LEFT JOIN pedidos q ON p.pedido_id=q.pedido_id WHERE q.estado IN ('cancelado','pendiente')",
        "SELECT round(avg(importe_centavos),2), min(importe_centavos), max(importe_centavos), abs(-2), coalesce(NULL,1), nullif(1,1) FROM pedidos",
        "SELECT lower(cliente), upper(cliente), length(cliente), substr(fecha,1,7), strftime('%Y-%m',fecha), date(fecha) FROM pedidos LIMIT 1",
        "SELECT pedido_id FROM pedidos WHERE cliente LIKE 'Ana%' AND importe_centavos BETWEEN 1 AND 20000",
        "SELECT pedido_id FROM pedidos WHERE pedido_id IN (SELECT pedido_id FROM pedidos WHERE estado='completado')",
        "SELECT DISTINCT estado FROM pedidos WHERE NOT estado='cancelado' ORDER BY estado LIMIT 1 OFFSET 1",
        'SELECT "país" FROM "PEDIDOS" LIMIT 1',
        "SELECT '; DROP TABLE pedidos;' AS text /* ; ATTACH */",
    ],
)
def test_legitimate_selects_and_all_subquery_forms(sql, database_path):
    assert validate_sql(sql) == sql
    result = execute_query(sql, database_path)
    assert result.columns
    assert result.rows


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "-- comment only",
        "SELECT 1; SELECT 2",
        "SELECT 1; DELETE FROM pedidos",
        "DELETE FROM pedidos",
        "UPDATE pedidos SET importe_centavos=1",
        "INSERT INTO pedidos SELECT * FROM pedidos",
        "DROP TABLE pedidos",
        "CREATE TABLE stolen AS SELECT * FROM pedidos",
        "ALTER TABLE pedidos RENAME TO stolen",
        "ATTACH DATABASE ':memory:' AS extra",
        "DETACH DATABASE main",
        "PRAGMA table_info(pedidos)",
        "PRAGMA writable_schema=ON",
        "VACUUM",
        "BEGIN",
        "EXPLAIN SELECT * FROM pedidos",
        "SELECT * FROM pedidos_rechazados",
        "SELECT * FROM pedidos_duplicados",
        "SELECT * FROM sqlite_master",
        "SELECT * FROM sqlite_schema",
        "SELECT * FROM sqlite_temp_master",
        "SELECT (SELECT original_json FROM pedidos_rechazados LIMIT 1) FROM pedidos",
        "SELECT * FROM (SELECT * FROM sqlite_master) AS pedidos",
        "SELECT * FROM pedidos WHERE EXISTS(SELECT 1 FROM pedidos_duplicados)",
        "SELECT * FROM pedidos WHERE pedido_id IN (SELECT row_number FROM pedidos_rechazados)",
        "SELECT * FROM pedidos JOIN pedidos_rechazados ON 1=1",
        "SELECT * FROM main.pedidos",
        "SELECT * FROM temp.pedidos",
        "SELECT * FROM pragma_table_info('pedidos')",
        "SELECT * FROM json_each('[1,2]')",
        "SELECT load_extension('/tmp/extension')",
        "SELECT readfile('/etc/passwd')",
        "SELECT writefile('/tmp/stolen', 'secret')",
        "SELECT randomblob(1000000000)",
        "SELECT random()",
        "SELECT sqlite_version()",
        "SELECT group_concat(cliente) FROM pedidos",
        "SELECT printf('%1000000000s','x')",
        "SELECT replace('x','x','y')",
        "SELECT ifnull(NULL,1)",
        "SELECT nvl(NULL,1)",
        "SELECT (SELECT load_extension('x')) FROM pedidos",
        "WITH selected AS (SELECT * FROM pedidos) SELECT * FROM selected",
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT * FROM n",
        "SELECT pedido_id FROM pedidos UNION SELECT row_number FROM pedidos_rechazados",
        "SELECT ROW_NUMBER() OVER (ORDER BY pedido_id) FROM pedidos",
        "SELECT * FROM pedidos INTERSECT SELECT * FROM pedidos",
        "SELECT * FROM pedidos NATURAL JOIN pedidos",
        "SELECT * FROM pedidos FOR UPDATE",
        "SELECT ? FROM pedidos",
        "SELECT :value FROM pedidos",
        "SELECT 1\x00",
        "SELECT (",
        "SELECT CAST(1 AS BLOB)",
    ],
)
def test_forbidden_sql_is_blocked_by_validator(sql, database_path):
    with pytest.raises(SQLPolicyViolationError):
        validate_sql(sql)
    with pytest.raises(SQLPolicyViolationError):
        execute_query(sql, database_path)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM pedidos_rechazados",
        "SELECT COUNT(*) FROM sqlite_master",
        "SELECT * FROM pedidos_duplicados",
        "SELECT randomblob(100000)",
        "SELECT load_extension('x')",
        "PRAGMA table_info(pedidos)",
        "ATTACH ':memory:' AS other",
        "DELETE FROM pedidos",
        "CREATE TABLE stolen(x)",
    ],
)
def test_authorizer_blocks_access_even_without_ast_validator(database_path, sql):
    with readonly_connection(database_path, ExecutionLimits()) as (connection, authorizer):
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(sql)
        assert authorizer.denied


def test_readonly_uri_prevents_writes_even_without_authorizer(database_path):
    with readonly_connection(database_path, ExecutionLimits()) as (connection, _):
        connection.set_authorizer(None)
        connection.execute("PRAGMA query_only=OFF")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM pedidos")
    assert execute_query("SELECT COUNT(*) FROM pedidos", database_path).rows == [[16]]


def test_allowed_views_have_correct_totals_and_order(database_path):
    monthly = execute_query(
        "SELECT * FROM ventas_por_mes_pais ORDER BY anio_mes, país", database_path
    )
    assert monthly.rows == [
        ["2024-03", "España", 1, 500, "USD"],
        ["2025-03", "España", 11, 24000, "USD"],
        ["2025-03", "México", 1, 15000, "USD"],
        ["2025-04", "España", 1, 10000, "USD"],
    ]
    top = execute_query(
        "SELECT cliente_id FROM top_10_clientes ORDER BY importe_total_centavos DESC, numero_pedidos DESC, cliente_id",
        database_path,
    )
    assert top.rows == [[2], [1], [3], [4], [5], [6], [7], [8], [9], [10]]


def test_view_name_does_not_grant_access_to_forbidden_dependencies(database_path):
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("DROP VIEW ventas_por_mes_pais")
        connection.execute("CREATE VIEW ventas_por_mes_pais AS SELECT * FROM pedidos_rechazados")
    with pytest.raises(SQLPolicyViolationError):
        execute_query("SELECT * FROM ventas_por_mes_pais", database_path)


@pytest.mark.parametrize("suffix", ["", "LIMIT 10000", "LIMIT -1", "LIMIT (SELECT -1)"])
def test_row_cap_is_enforced_regardless_of_sql_limit(database_path, suffix):
    result = execute_query(
        f"SELECT pedido_id FROM pedidos ORDER BY pedido_id {suffix}",
        database_path,
        limits=ExecutionLimits(max_rows=3),
    )
    assert result.rows == [[1], [2], [3]]
    assert result.truncated is True


@pytest.mark.parametrize("limit", [0, 1, 3])
def test_truncation_is_false_when_entire_result_fits(database_path, limit):
    result = execute_query(
        f"SELECT pedido_id FROM pedidos ORDER BY pedido_id LIMIT {limit}",
        database_path,
        limits=ExecutionLimits(max_rows=3),
    )
    assert len(result.rows) == limit
    assert not result.truncated


def test_utf8_response_size_includes_envelope_and_column_names(database_path):
    sql = "SELECT 'España é ñ 😀' AS país FROM pedidos ORDER BY pedido_id"
    full = execute_query(sql, database_path)
    first = full.model_copy(update={"rows": full.rows[:2], "truncated": False})
    size_limit = len(JSONResponse(first.model_dump(mode="json")).body)
    result = execute_query(
        sql, database_path, limits=ExecutionLimits(max_response_bytes=size_limit)
    )
    assert len(result.rows) == 2
    assert result.truncated
    assert len(JSONResponse(result.model_dump(mode="json")).body) <= size_limit


def test_oversized_row_is_omitted_and_oversized_metadata_is_an_error(database_path):
    result = execute_query(
        "SELECT '" + "x" * 500 + "' AS value",
        database_path,
        limits=ExecutionLimits(max_response_bytes=100),
    )
    assert result.rows == [] and result.truncated
    with pytest.raises(QueryResourceLimitError):
        execute_query(
            'SELECT 1 AS "' + "x" * 500 + '"',
            database_path,
            limits=ExecutionLimits(max_response_bytes=100),
        )


def test_progress_handler_interrupts_costly_query_and_next_query_still_works(database_path):
    sql = "SELECT COUNT(*) FROM pedidos a CROSS JOIN pedidos b CROSS JOIN pedidos c CROSS JOIN pedidos d CROSS JOIN pedidos e CROSS JOIN pedidos f"
    with pytest.raises(QueryResourceLimitError):
        execute_query(
            sql,
            database_path,
            limits=ExecutionLimits(max_vm_steps=1000, progress_interval=100, timeout_seconds=5),
        )
    assert execute_query("SELECT COUNT(*) FROM pedidos", database_path).rows == [[16]]


def test_wall_clock_budget_without_sleep(monkeypatch):
    moments = iter([100.0, 102.0])
    monkeypatch.setattr("query_executor.time.monotonic", lambda: next(moments))
    budget = ExecutionBudget(ExecutionLimits(timeout_seconds=1))
    assert budget.progress() == 1
    assert budget.exhausted


def test_sql_text_and_ast_size_limits():
    with pytest.raises(SQLPolicyViolationError):
        validate_sql("SELECT '" + "x" * MAX_SQL_BYTES + "'")
    with pytest.raises(SQLPolicyViolationError):
        validate_sql("SELECT " + ",".join("1" for _ in range(600)))
    with pytest.raises(SQLPolicyViolationError):
        validate_sql("SELECT " + "(" * 70 + "1" + ")" * 70)


def test_runtime_sql_errors_are_classified_and_missing_db_is_not_created(database_path, tmp_path):
    with pytest.raises(InvalidLLMResponseError):
        execute_query("SELECT unknown_column FROM pedidos", database_path)
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(InternalServerError):
        execute_query("SELECT * FROM pedidos", missing)
    assert not missing.exists()


def test_non_json_result_is_rejected(database_path):
    with pytest.raises(InvalidLLMResponseError):
        execute_query("SELECT 1e999 AS infinite", database_path)


def test_sqlite_limits_oversized_values_before_building_response(database_path):
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("UPDATE pedidos SET producto=? WHERE pedido_id=1", ("x" * 1100000,))
        connection.commit()
    with pytest.raises(QueryResourceLimitError):
        execute_query("SELECT producto FROM pedidos WHERE pedido_id=1", database_path)


@pytest.mark.parametrize(
    "options",
    [
        {"max_rows": 0},
        {"max_response_bytes": -1},
        {"max_vm_steps": 0},
        {"timeout_seconds": 0},
        {"timeout_seconds": float("nan")},
    ],
)
def test_invalid_execution_limits(options):
    with pytest.raises(ValueError):
        ExecutionLimits(**options)
