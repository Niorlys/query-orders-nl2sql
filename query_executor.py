"""Bounded, read-only SQLite execution used by the query service."""

import json
import math
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from errors import (
    InternalServerError, InvalidLLMResponseError, QueryResourceLimitError, SQLPolicyViolationError,
)
from schemas import QueryResponse
from sql_policy import ALLOWED_FUNCTIONS, ALLOWED_RELATIONS, MAX_SQL_BYTES, validate_sql


ALLOWED_VIEWS = frozenset({"ventas_por_mes_pais", "top_10_clientes"})


@dataclass(frozen=True)
class ExecutionLimits:
    max_rows: int = 100
    max_response_bytes: int = 65536
    max_vm_steps: int = 200000
    timeout_seconds: float = 1.0
    progress_interval: int = 1000
    max_value_bytes: int = 1048576

    def __post_init__(self):
        for name in ("max_rows", "max_response_bytes", "max_vm_steps", "progress_interval", "max_value_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")


class ReadOnlyAuthorizer:
    def __init__(self):
        self.denied = False

    def __call__(self, action, first, second, database, source):
        source = source.lower() if source else None
        allowed_source = source is None or source in ALLOWED_VIEWS
        if allowed_source and action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if allowed_source and action == sqlite3.SQLITE_FUNCTION:
            if second and second.lower() in ALLOWED_FUNCTIONS:
                return sqlite3.SQLITE_OK
        if allowed_source and action == sqlite3.SQLITE_READ:
            table = first.lower() if first else ""
            # SQLite reports database=None for COUNT(*) with no extracted column.
            main_database = database == "main" or (database is None and second == "")
            allowed_table = table in ALLOWED_RELATIONS if source is None else table == "pedidos"
            if main_database and allowed_table:
                return sqlite3.SQLITE_OK
        self.denied = True
        return sqlite3.SQLITE_DENY


@contextmanager
def readonly_connection(database_path: Path, limits: ExecutionLimits):
    uri = Path(database_path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=min(limits.timeout_seconds, 1.0), cached_statements=0)
    try:
        # Trusted setup precedes installation of the deny-by-default authorizer.
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        if not hasattr(connection, "setlimit"):
            raise InternalServerError("Safe query execution requires Python 3.11 or newer")
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_BYTES)
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, limits.max_value_bytes)
        connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 50)
        connection.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 100)
        connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        authorizer = ReadOnlyAuthorizer()
        connection.set_authorizer(authorizer)
        yield connection, authorizer
    finally:
        connection.close()


class ExecutionBudget:
    def __init__(self, limits: ExecutionLimits):
        self.limits = limits
        self.interval = min(limits.progress_interval, limits.max_vm_steps)
        self.steps = 0
        self.deadline = time.monotonic() + limits.timeout_seconds
        self.exhausted = False

    def progress(self):
        self.steps += self.interval
        self.exhausted = self.steps >= self.limits.max_vm_steps or time.monotonic() >= self.deadline
        return int(self.exhausted)

    def check_time(self):
        if self.exhausted or time.monotonic() >= self.deadline:
            self.exhausted = True
            raise QueryResourceLimitError("Execution budget exceeded")


def json_size(value) -> int:
    # Same UTF-8 JSON options as JSONResponse; include envelope and column names.
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))


def execute_query(
    sql: str, database_path: Path, *, limits: ExecutionLimits | None = None,
) -> QueryResponse:
    sql = validate_sql(sql)
    limits = limits or ExecutionLimits()
    budget = ExecutionBudget(limits)
    authorizer = None
    try:
        with readonly_connection(database_path, limits) as (connection, authorizer):
            connection.set_progress_handler(budget.progress, budget.interval)
            budget.check_time()
            cursor = connection.execute(sql)
            columns = [column[0] for column in cursor.description]
            rows = []
            # Reserve false (5 bytes), which is longer than true (4 bytes).
            size = json_size({"columns": columns, "rows": [], "truncated": False})
            if size > limits.max_response_bytes:
                raise QueryResourceLimitError("Column metadata exceeds response budget")
            truncated = False
            while True:
                budget.check_time()
                row = cursor.fetchone()
                if row is None:
                    break
                if len(rows) >= limits.max_rows:
                    truncated = True
                    break
                if any(type(cell) not in (str, int, float, bool, type(None))
                       or (isinstance(cell, float) and not math.isfinite(cell)) for cell in row):
                    raise InvalidLLMResponseError("Query returned a non-JSON scalar")
                row = list(row)
                additional_size = json_size(row) + (1 if rows else 0)
                if size + additional_size > limits.max_response_bytes:
                    truncated = True
                    break
                rows.append(row)
                size += additional_size
            budget.check_time()
            return QueryResponse(columns=columns, rows=rows, truncated=truncated)
    except sqlite3.Error as error:
        if budget.exhausted:
            raise QueryResourceLimitError("SQLite execution interrupted by budget") from error
        if authorizer is not None and authorizer.denied:
            raise SQLPolicyViolationError("SQLite authorizer denied the query") from error
        code = getattr(error, "sqlite_errorcode", None)
        if code == sqlite3.SQLITE_TOOBIG:
            raise QueryResourceLimitError("SQLite value size limit exceeded") from error
        if code == sqlite3.SQLITE_ERROR:
            raise InvalidLLMResponseError("SQL cannot be executed against this schema") from error
        raise InternalServerError("SQLite query execution failed") from error
