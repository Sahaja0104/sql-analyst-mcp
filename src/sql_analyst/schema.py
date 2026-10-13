"""Schema introspection: tells the LLM which tables and columns exist.

Reads Postgres' own catalog tables (pg_class, pg_attribute, pg_constraint),
so it always matches the real database. Runs read-only, like db.py.
"""

from typing import Any

import psycopg
from psycopg import sql

from .config import get_settings
from .db import QueryError, get_connection

# One row per column of every ordinary table in the "public" schema.
_COLUMNS_SQL = """
SELECT c.relname AS table_name,
       a.attname AS column_name,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       a.attnotnull AS not_null
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
WHERE c.relnamespace = 'public'::regnamespace
  AND c.relkind IN ('r', 'p')
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY c.relname, a.attnum
"""

# Primary keys ('p') and foreign keys ('f'), already written out in readable form,
# e.g. "FOREIGN KEY (album_id) REFERENCES album(album_id)".
_CONSTRAINTS_SQL = """
SELECT conrelid::regclass::text AS table_name,
       pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE contype IN ('p', 'f')
  AND connamespace = 'public'::regnamespace
ORDER BY conrelid::regclass::text, contype DESC, conname
"""

MAX_SAMPLE_ROWS = 5
MAX_VALUE_CHARS = 40


def _format_value(value: Any) -> str:
    """Show one sample value compactly (long text is shortened)."""
    if value is None:
        return "NULL"
    if isinstance(value, str):
        text = value if len(value) <= MAX_VALUE_CHARS else value[:MAX_VALUE_CHARS] + "..."
        return repr(text)
    return str(value)


def get_schema(table: str | None = None, sample_rows: int = 3) -> str:
    """Describe the database as text.

    table:       describe only this table (default: all tables in the public schema)
    sample_rows: how many example rows to show per table (0 to 5)
    """
    settings = get_settings()
    sample_rows = max(0, min(int(sample_rows), MAX_SAMPLE_ROWS))

    columns: dict[str, list[tuple[str, str, bool]]] = {}
    constraints: dict[str, list[str]] = {}
    samples: dict[str, list[tuple]] = {}

    with get_connection() as conn:
        conn.read_only = True
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL statement_timeout = {int(settings.statement_timeout_ms)}")

                cur.execute(_COLUMNS_SQL)
                for table_name, column_name, data_type, not_null in cur.fetchall():
                    columns.setdefault(table_name, []).append((column_name, data_type, not_null))

                cur.execute(_CONSTRAINTS_SQL)
                for table_name, definition in cur.fetchall():
                    constraints.setdefault(table_name, []).append(definition)

                if table is not None and table not in columns:
                    available = ", ".join(sorted(columns))
                    raise QueryError(f"Table '{table}' not found. Available tables: {available}")

                names = [table] if table is not None else sorted(columns)
                if sample_rows > 0:
                    for name in names:
                        cur.execute(
                            sql.SQL("SELECT * FROM {} LIMIT {}").format(
                                sql.Identifier(name), sql.Literal(sample_rows)
                            )
                        )
                        samples[name] = cur.fetchall()
        except psycopg.Error as exc:
            message = (exc.diag.message_primary or str(exc)).strip()
            raise QueryError(f"Database error while reading the schema: {message}") from exc
        finally:
            conn.rollback()

    lines: list[str] = []
    for name in names:
        lines.append(f"TABLE {name}")
        for column_name, data_type, not_null in columns[name]:
            lines.append(f"  {column_name} {data_type}{' NOT NULL' if not_null else ''}")
        for definition in constraints.get(name, []):
            lines.append(f"  {definition}")
        if samples.get(name):
            lines.append("  sample rows (values in the column order above):")
            for row in samples[name]:
                lines.append("    (" + ", ".join(_format_value(v) for v in row) + ")")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"