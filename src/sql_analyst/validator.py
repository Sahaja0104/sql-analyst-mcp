"""SQL guardrails: decide whether a query is safe BEFORE it reaches the database.

Rules (all must pass):
  1. Exactly one statement, no longer than MAX_QUERY_CHARS.
  2. It must be a SELECT (CTEs with WITH and UNION/INTERSECT/EXCEPT are fine).
  3. Nothing inside it may change data, even hidden in a CTE.
  4. No SELECT ... INTO and no row locking (FOR UPDATE).
  5. Only tables in the "public" schema; no pg_* system tables.
  6. No dangerous functions (sleeping, reading server files, changing settings, ...).

This is only the FIRST layer. The read-only database role and read-only
transaction still protect us if something slips through.
"""

import logging
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

MAX_QUERY_CHARS = 10_000

# sqlglot prints a warning when it meets statements it does not fully understand
# (EXPLAIN, CALL, ...). We reject those anyway, so keep the console quiet.
logging.getLogger("sqlglot").setLevel(logging.ERROR)


class ValidationError(Exception):
    """The query is not allowed. The message is written so an LLM can fix its SQL."""


# The top-level statement must be one of these.
_ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)

# Nodes that must never appear anywhere in the query (this also catches
# data-changing statements hidden inside a WITH ... clause).
_FORBIDDEN_NODE_NAMES = [
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter",
    "Copy", "Command", "Set", "Transaction", "Commit", "Rollback",
    "Grant", "TruncateTable", "Analyze", "Use", "Pragma",
]
_FORBIDDEN_NODES = tuple(
    getattr(exp, name) for name in _FORBIDDEN_NODE_NAMES if hasattr(exp, name)
)

# Functions that can sleep, touch the server's files, change settings or sequences, etc.
_BLOCKED_FUNCTIONS = {
    "set_config", "nextval", "setval", "txid_current", "query_to_xml",
    "pg_sleep", "pg_sleep_for", "pg_sleep_until", "pg_stat_file",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
}
_BLOCKED_FUNCTION_PREFIXES = (
    "pg_read_", "pg_ls_", "pg_advisory", "pg_logical", "pg_replication",
    "pg_create_", "pg_drop_", "pg_switch_", "pg_promote", "pg_rotate",
    "lo_", "dblink",
)

_ANSI_ESCAPES = re.compile(r"\x1b\[[0-9;]*m")


def _clean_parse_error(exc: SqlglotError) -> str:
    """sqlglot's messages contain terminal colour codes; strip them and shorten."""
    text = _ANSI_ESCAPES.sub("", str(exc)).strip().replace("\n", " ")
    return text[:300]


def _function_names(tree: exp.Expression):
    for node in tree.find_all(exp.Func):
        if isinstance(node, exp.Anonymous):
            yield str(node.name).lower()
        else:
            yield node.sql_name().lower()


def _is_blocked_function(name: str) -> bool:
    return name in _BLOCKED_FUNCTIONS or name.startswith(_BLOCKED_FUNCTION_PREFIXES)


def validate_query(query: str) -> str:
    """Check a query. Returns the cleaned query (no trailing ';') or raises ValidationError."""
    if query is None or not query.strip():
        raise ValidationError("The query is empty. Send one SELECT statement.")
    if len(query) > MAX_QUERY_CHARS:
        raise ValidationError(f"The query is too long (limit: {MAX_QUERY_CHARS} characters).")

    # --- 1. Parse -------------------------------------------------------
    try:
        parsed = sqlglot.parse(query, read="postgres")
    except SqlglotError as exc:
        raise ValidationError(f"The SQL could not be parsed: {_clean_parse_error(exc)}") from exc

    statements = [s for s in parsed if s is not None]  # a trailing ';' gives an empty entry
    if not statements:
        raise ValidationError("The query is empty. Send one SELECT statement.")
    if len(statements) > 1:
        raise ValidationError("Only one SQL statement is allowed. Remove the extra statements.")
    tree = statements[0]

    # --- 2. Must be a SELECT -------------------------------------------
    root = tree
    while isinstance(root, exp.Subquery):  # "(SELECT ...)" with brackets
        root = root.this
    if not isinstance(root, _ALLOWED_ROOTS):
        kind = type(root).__name__.upper()
        raise ValidationError(
            f"Only SELECT queries are allowed (this looks like {kind}). "
            "This server is read-only."
        )

    # --- 3. Nothing that changes data, anywhere ------------------------
    for node in tree.find_all(*_FORBIDDEN_NODES):
        raise ValidationError(
            f"The query contains a forbidden operation ({type(node).__name__.upper()}). "
            "Only read-only SELECT queries are allowed."
        )

    # --- 4. No SELECT INTO, no row locks -------------------------------
    for select in tree.find_all(exp.Select):
        if select.args.get("into"):
            raise ValidationError("SELECT ... INTO is not allowed because it creates a table.")
        if select.args.get("locks"):
            raise ValidationError("Row locking (FOR UPDATE / FOR SHARE) is not allowed.")

    # --- 5. Only public tables, no system tables -----------------------
    for table in tree.find_all(exp.Table):
        schema = (table.db or "").lower()
        name = (table.name or "").lower()
        if schema and schema != "public":
            raise ValidationError(
                f"Schema '{schema}' is not allowed. Only tables in the public schema can be queried; "
                "use get_schema to see them."
            )
        if name.startswith("pg_"):
            raise ValidationError(
                f"System table '{name}' is not allowed. Use get_schema to see the available tables."
            )

    # --- 6. No dangerous functions -------------------------------------
    for name in _function_names(tree):
        if _is_blocked_function(name):
            raise ValidationError(f"The function '{name}' is not allowed.")

    return query.strip().rstrip(";").rstrip()