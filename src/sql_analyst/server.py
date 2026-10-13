"""The MCP server: exposes three read-only tools to an LLM client such as Claude Desktop.

  get_schema     - what tables and columns exist
  run_query      - run one SELECT query (validated, read-only, capped)
  explain_query  - show how Postgres would run a query, without running it

Start it with:  python -m sql_analyst.server
"""

import datetime
import decimal
import uuid
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import db, schema
from .validator import ValidationError, validate_query

INSTRUCTIONS = (
    "You are connected to a read-only PostgreSQL database (a music store called Chinook). "
    "Always call get_schema first so you know the exact table and column names. "
    "Then write ONE PostgreSQL SELECT query and run it with run_query. "
    "If a tool returns an error, read the message, fix the query and try again."
)

mcp = MCPServer("sql-analyst", instructions=INSTRUCTIONS)

# Tells the client that these tools never change anything.
READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


def _jsonable(value: Any) -> Any:
    """Convert database values (Decimal, dates, ...) into plain JSON-friendly values."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, (uuid.UUID, datetime.timedelta)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


@mcp.tool(annotations=READ_ONLY)
def get_schema(table: str | None = None, include_samples: bool = True) -> str:
    """Describe the database: tables, columns, types, primary keys, foreign keys and sample rows.

    Call this FIRST, before writing any SQL.

    Args:
        table: Describe only this table. Leave empty to describe all tables.
        include_samples: Include 3 example rows per table so you can see real values.
    """
    try:
        return schema.get_schema(table=table, sample_rows=3 if include_samples else 0)
    except db.QueryError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=READ_ONLY)
def run_query(sql: str) -> dict[str, Any]:
    """Run ONE read-only PostgreSQL SELECT query and return the rows.

    Only SELECT is allowed (WITH and UNION are fine). Results are limited to 100 rows,
    and queries are cancelled after 5 seconds. Use LIMIT, WHERE and aggregates to keep results small.

    Args:
        sql: A single PostgreSQL SELECT statement.
    """
    try:
        cleaned = validate_query(sql)
        result = db.execute_select(cleaned)
    except (ValidationError, db.QueryError) as exc:
        raise ToolError(str(exc)) from exc

    response: dict[str, Any] = {
        "columns": result.columns,
        "rows": [[_jsonable(v) for v in row] for row in result.rows],
        "row_count": result.row_count,
        "truncated": result.truncated,
        "elapsed_ms": result.elapsed_ms,
    }
    if result.truncated:
        response["note"] = (
            f"Only the first {result.row_count} rows are shown. "
            "Add a WHERE clause, an aggregate or a smaller LIMIT to see what you need."
        )
    return response


@mcp.tool(annotations=READ_ONLY)
def explain_query(sql: str) -> str:
    """Show how PostgreSQL would execute a SELECT query, WITHOUT running it.

    Useful for checking whether a query is expensive before running it.

    Args:
        sql: A single PostgreSQL SELECT statement.
    """
    try:
        cleaned = validate_query(sql)
        return db.explain_select(cleaned)
    except (ValidationError, db.QueryError) as exc:
        raise ToolError(str(exc)) from exc


def main() -> None:
    """Start the server over stdio (the way Claude Desktop launches local servers)."""
    mcp.run()


if __name__ == "__main__":
    main()