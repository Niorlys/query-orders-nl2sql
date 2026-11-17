"""Closed SQLite SELECT policy. Validate the whole AST, including nested queries."""

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from errors import SQLPolicyViolationError


# Public business columns only. New physical columns remain private by default.
# SQLite enforces this mapping after resolving aliases, stars and view dependencies.
ALLOWED_COLUMNS = {
    "pedidos": frozenset(
        {
            "pedido_id",
            "fecha",
            "fecha_completado",
            "cliente_id",
            "cliente",
            "país",
            "producto",
            "importe_centavos",
            "moneda",
            "estado",
        }
    ),
    "ventas_por_mes_pais": frozenset(
        {
            "anio_mes",
            "país",
            "numero_pedidos",
            "importe_total_centavos",
            "moneda",
        }
    ),
    "top_10_clientes": frozenset(
        {
            "cliente_id",
            "nombre",
            "numero_pedidos",
            "importe_total_centavos",
            "moneda",
        }
    ),
}
ALLOWED_RELATIONS = frozenset(ALLOWED_COLUMNS)
ALLOWED_FUNCTIONS = frozenset(
    {
        "count",
        "sum",
        "avg",
        "min",
        "max",
        "round",
        "abs",
        "coalesce",
        "nullif",
        "lower",
        "upper",
        "length",
        "substr",
        "substring",
        "strftime",
        "date",
        "like",  # SQLite authorizes the LIKE operator as a function.
    }
)
ALLOWED_NODES = frozenset(
    {
        exp.Select,
        exp.From,
        exp.Table,
        exp.TableAlias,
        exp.Identifier,
        exp.Column,
        exp.Star,
        exp.Alias,
        exp.Literal,
        exp.Null,
        exp.Boolean,
        exp.Paren,
        exp.Where,
        exp.Group,
        exp.Having,
        exp.Order,
        exp.Ordered,
        exp.Limit,
        exp.Offset,
        exp.Distinct,
        exp.Join,
        exp.Subquery,
        exp.Exists,
        exp.EQ,
        exp.NEQ,
        exp.GT,
        exp.GTE,
        exp.LT,
        exp.LTE,
        exp.And,
        exp.Or,
        exp.Not,
        exp.Is,
        exp.In,
        exp.Between,
        exp.Like,
        exp.Add,
        exp.Sub,
        exp.Mul,
        exp.Div,
        exp.Mod,
        exp.Neg,
        exp.Count,
        exp.Sum,
        exp.Avg,
        exp.Min,
        exp.Max,
        exp.Round,
        exp.Abs,
        exp.Coalesce,
        exp.Nullif,
        exp.Lower,
        exp.Upper,
        exp.Length,
        exp.Substring,
        exp.TimeToStr,
        exp.TsOrDsToTimestamp,
        exp.Date,
    }
)
SELECT_ARGUMENTS = frozenset(
    {
        "expressions",
        "from_",
        "joins",
        "where",
        "group",
        "having",
        "order",
        "limit",
        "offset",
        "distinct",
    }
)
MAX_SQL_BYTES = 16384
MAX_AST_NODES = 500
MAX_AST_DEPTH = 50


def validate_sql(sql: str) -> str:
    """Return the original SQL after validation; never rewrite untrusted syntax."""
    if not isinstance(sql, str) or not sql.strip():
        raise SQLPolicyViolationError("Empty SQL")
    try:
        if len(sql.encode("utf-8")) > MAX_SQL_BYTES or "\x00" in sql:
            raise SQLPolicyViolationError("SQL text limit exceeded or NUL byte")
        statements = sqlglot.parse(sql, read="sqlite")
    except (SqlglotError, RecursionError, UnicodeError) as error:
        raise SQLPolicyViolationError("SQL parsing failed") from error
    if len(statements) != 1 or type(statements[0]) is not exp.Select:
        raise SQLPolicyViolationError("Exactly one SELECT is required")
    stack = [(statements[0], 0)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_AST_NODES or depth > MAX_AST_DEPTH:
            raise SQLPolicyViolationError("SQL AST complexity exceeded")
        if type(node) not in ALLOWED_NODES:
            raise SQLPolicyViolationError(f"Unsupported SQL node: {type(node).__name__}")
        if isinstance(node, exp.Func) and "start" in node.meta and "end" in node.meta:
            # Some aliases (e.g. NVL/IFNULL) normalize to the same AST as COALESCE.
            # Check the original function spelling too, when retained by the parser.
            spelling = sql[node.meta["start"] : node.meta["end"] + 1].strip('"`[]').lower()
            if spelling not in ALLOWED_FUNCTIONS:
                raise SQLPolicyViolationError("Function name is not allowed")
        if isinstance(node, exp.Select):
            if any(value and key not in SELECT_ARGUMENTS for key, value in node.args.items()):
                raise SQLPolicyViolationError("Unsupported SELECT clause")
        if isinstance(node, exp.Table):
            if (
                type(node.this) is not exp.Identifier
                or node.db
                or node.catalog
                or node.name.lower() not in ALLOWED_RELATIONS
            ):
                raise SQLPolicyViolationError("Forbidden relation or qualified database")
        if isinstance(node, exp.Column) and (node.db or node.catalog):
            raise SQLPolicyViolationError("Qualified databases are not allowed")
        if isinstance(node, exp.Join):
            if node.side not in ("", "LEFT") or node.kind not in ("", "INNER", "CROSS", "OUTER"):
                raise SQLPolicyViolationError("Unsupported join")
            if node.args.get("method"):
                raise SQLPolicyViolationError("Implicit/natural joins are not allowed")
        stack.extend((child, depth + 1) for child in node.iter_expressions())
    return sql
