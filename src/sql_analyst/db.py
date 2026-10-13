"""Safe query execution against PostgreSQL.

Every query runs:
  - as the read-only mcp_reader login (set in DATABASE_URL),
  - inside a READ ONLY transaction,
  - with a statement timeout enforced by Postgres,
  - through a server-side cursor, so we only ever fetch max_rows + 1 rows.
"""

import time
from dataclasses import dataclass
from typing import Any

import psycopg

from .config import get_settings


class QueryError(Exception):
    """Any problem running a query. The message is written to be shown to the LLM."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    row_count: int      # number of rows returned (never more than max_rows)
    truncated: bool     # True if the query had more rows than max_rows
    elapsed_ms: float


def get_connection() -> psycopg.Connection:
    """Open a new connection using the settings in config.py."""
    settings = get_settings()
    try:
        return psycopg.connect(settings.database_url, connect_timeout=5)
    except psycopg.OperationalError as exc:
        raise QueryError(f"Could not connect to the database: {exc}") from exc


def execute_select(query: str) -> QueryResult:
    """Run one SELECT query safely and return its rows (capped at max_rows)."""
    settings = get_settings()
    start = time.perf_counter()

    with get_connection() as conn:
        conn.read_only = True  # BEGIN READ ONLY: Postgres rejects any write
        try:
            with conn.cursor() as cur:
                # SET LOCAL lasts only for this transaction. The value is an int, so it is safe to format.
                cur.execute(f"SET LOCAL statement_timeout = {int(settings.statement_timeout_ms)}")

            # Named cursor = server-side cursor: rows are streamed, not all loaded at once.
            with conn.cursor(name="mcp_query") as cur:
                cur.execute(query)
                columns = [col.name for col in (cur.description or [])]
                fetched = cur.fetchmany(settings.max_rows + 1)
        except psycopg.errors.QueryCanceled as exc:
            raise QueryError(
                f"Query cancelled: it ran longer than {settings.statement_timeout_ms} ms. "
                "Try a simpler query, add filters, or avoid large joins."
            ) from exc
        except psycopg.Error as exc:
            message = (exc.diag.message_primary or str(exc)).strip()
            raise QueryError(f"Database error: {message}") from exc
        finally:
            conn.rollback()  # we never commit anything

    truncated = len(fetched) > settings.max_rows
    rows = [list(row) for row in fetched[: settings.max_rows]]
    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        elapsed_ms=round((time.perf_counter() - start) * 1000, 1),
    )


def explain_select(query: str) -> str:
    """Show Postgres' plan for a query WITHOUT running it.

    Uses plain EXPLAIN, never EXPLAIN ANALYZE (ANALYZE would actually execute the query).
    The caller must validate the query first.
    """
    settings = get_settings()
    with get_connection() as conn:
        conn.read_only = True
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = {int(settings.statement_timeout_ms)}")
                cur.execute("EXPLAIN " + query)
                plan_lines = [row[0] for row in cur.fetchall()]
        except psycopg.errors.QueryCanceled as exc:
            raise QueryError(
                f"Planning was cancelled after {settings.statement_timeout_ms} ms."
            ) from exc
        except psycopg.Error as exc:
            message = (exc.diag.message_primary or str(exc)).strip()
            raise QueryError(f"Database error: {message}") from exc
        finally:
            conn.rollback()
    return "\n".join(plan_lines)