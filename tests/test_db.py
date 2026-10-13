"""Integration tests: these run against a REAL PostgreSQL database (the Chinook one).

Start it first:   docker compose up -d
Run the tests:    python -m pytest -v

If PostgreSQL is not reachable the tests are skipped, so `pytest` still works
without Docker. In CI we set REQUIRE_DB=1 so a missing database is an error
instead of a silent skip.
"""

import json
import os
import time

import psycopg
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from sql_analyst import db, schema, server
from sql_analyst.config import get_settings


def _database_reachable() -> tuple[bool, str]:
    try:
        with psycopg.connect(get_settings().database_url, connect_timeout=3):
            return True, ""
    except psycopg.Error as exc:
        return False, str(exc).strip()


_reachable, _reason = _database_reachable()
if not _reachable:
    if os.getenv("REQUIRE_DB") == "1":
        raise RuntimeError(f"REQUIRE_DB=1 but PostgreSQL is not reachable: {_reason}")
    pytest.skip(f"PostgreSQL is not reachable ({_reason})", allow_module_level=True)


# ------------------------------------------------------------- db.py ---
def test_select_returns_rows():
    result = db.execute_select("SELECT COUNT(*) FROM artist")
    assert result.rows == [[275]]
    assert result.truncated is False


def test_small_result_is_not_truncated():
    result = db.execute_select("SELECT * FROM genre")
    assert result.row_count == 25
    assert result.truncated is False
    assert result.columns == ["genre_id", "name"]


def test_row_cap_limits_big_results():
    max_rows = get_settings().max_rows
    result = db.execute_select("SELECT * FROM track")  # the table has 3503 rows
    assert result.row_count == max_rows
    assert len(result.rows) == max_rows
    assert result.truncated is True


def test_slow_query_is_cancelled_by_postgres(monkeypatch):
    monkeypatch.setenv("STATEMENT_TIMEOUT_MS", "500")  # keep the test fast
    start = time.perf_counter()
    with pytest.raises(db.QueryError, match="cancelled"):
        db.execute_select("SELECT pg_sleep(10)")  # db.py alone does not block pg_sleep
    assert time.perf_counter() - start < 5


def test_database_errors_become_readable_messages():
    with pytest.raises(db.QueryError, match='relation "nope" does not exist'):
        db.execute_select("SELECT * FROM nope")


def test_write_statements_fail_even_without_the_validator():
    """db.py runs queries through a read-only cursor, so writes cannot even start."""
    with pytest.raises(db.QueryError):
        db.execute_select("DELETE FROM artist")
    assert db.execute_select("SELECT COUNT(*) FROM artist").rows == [[275]]


def test_explain_shows_a_plan_without_running_the_query():
    start = time.perf_counter()
    plan = db.explain_select("SELECT pg_sleep(3)")  # plain EXPLAIN must NOT execute this
    assert "Result" in plan
    assert time.perf_counter() - start < 2
    assert "artist" in db.explain_select("SELECT * FROM artist WHERE artist_id = 1")


# ------------------------------------- the read-only login (the real lock) ---
def test_login_has_read_only_defaults():
    with psycopg.connect(get_settings().database_url, autocommit=True) as conn:
        assert conn.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
        assert conn.execute("SHOW statement_timeout").fetchone()[0] == "5s"


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM artist",
        "UPDATE artist SET name = 'hacked'",
        "INSERT INTO artist (artist_id, name) VALUES (99999, 'hacked')",
        "TRUNCATE artist",
        "DROP TABLE artist",
        "CREATE TABLE evil (a int)",
    ],
)
def test_database_refuses_writes_even_if_read_only_mode_is_switched_off(statement):
    """The strongest layer: mcp_reader has SELECT permission only.

    We deliberately turn the read-only default OFF first. Postgres must still say no,
    because the role itself has no write permission.
    """
    with psycopg.connect(get_settings().database_url, autocommit=True) as conn:
        conn.execute("SET default_transaction_read_only = off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)
    assert db.execute_select("SELECT COUNT(*) FROM artist").rows == [[275]]


# --------------------------------------------------------- schema.py ---
def test_schema_lists_all_tables():
    text = schema.get_schema()
    assert text.count("TABLE ") == 11
    for table in ("album", "artist", "customer", "invoice", "invoice_line", "track"):
        assert f"TABLE {table}\n" in text


def test_schema_shows_keys_and_samples():
    text = schema.get_schema("album")
    assert "PRIMARY KEY (album_id)" in text
    assert "FOREIGN KEY (artist_id) REFERENCES artist(artist_id)" in text
    assert "sample rows" in text


def test_schema_without_samples():
    assert "sample rows" not in schema.get_schema("genre", sample_rows=0)


@pytest.mark.parametrize("bad_name", ["nope", 'x"; DROP TABLE artist; --'])
def test_schema_unknown_table_lists_the_available_ones(bad_name):
    with pytest.raises(db.QueryError, match="Available tables"):
        schema.get_schema(bad_name)


# ---------------------------------------------------------- server.py ---
def test_run_query_tool_returns_json_friendly_data():
    response = server.run_query("SELECT invoice_date, total FROM invoice LIMIT 2")
    json.dumps(response)  # dates and decimals must already be plain JSON values
    assert response["columns"] == ["invoice_date", "total"]
    assert response["row_count"] == 2
    assert response["truncated"] is False


def test_run_query_tool_adds_a_note_when_truncated():
    response = server.run_query("SELECT track_id FROM track")
    assert response["truncated"] is True
    assert "note" in response


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM artist",
        "WITH d AS (DELETE FROM artist RETURNING *) SELECT * FROM d",
        "SELECT pg_sleep(1)",
        "SELECT * FROM pg_user",
    ],
)
def test_run_query_tool_rejects_dangerous_queries_with_a_clear_message(sql):
    with pytest.raises(ToolError):
        server.run_query(sql)


def test_explain_query_tool_rejects_non_select():
    with pytest.raises(ToolError, match="Only SELECT"):
        server.explain_query("DROP TABLE artist")