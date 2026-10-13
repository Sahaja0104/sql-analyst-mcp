"""Tests for the SQL validator (the first safety layer).

Run with:  python -m pytest -v
No database is needed: the validator only reads the query text.
"""

import pytest

from sql_analyst.validator import MAX_QUERY_CHARS, ValidationError, validate_query


# ---------------------------------------------------------------- allowed ---
@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1",
        "SELECT * FROM artist",
        "SELECT * FROM artist;",
        "  select name from artist where name like 'A%' order by name limit 5 ;  ",
        "SELECT a.name, COUNT(*) FROM artist a JOIN album b ON b.artist_id = a.artist_id "
        "GROUP BY a.name HAVING COUNT(*) > 2",
        "WITH t AS (SELECT genre_id FROM genre) SELECT * FROM t",
        "SELECT name FROM artist UNION SELECT name FROM genre",
        "(SELECT name FROM artist) UNION ALL (SELECT name FROM genre)",
        "SELECT * FROM public.artist",
        "SELECT * FROM (SELECT artist_id FROM artist) s",
        "SELECT date_trunc('month', invoice_date), SUM(total) FROM invoice GROUP BY 1",
        "SELECT generate_series(1, 5)",
        "SELECT COALESCE(name, 'x'), ROW_NUMBER() OVER (ORDER BY artist_id) FROM artist",
        "SELECT CAST(total AS text) FROM invoice",
        "SELECT total::numeric(10,2) FROM invoice",
        "SELECT EXTRACT(year FROM invoice_date) FROM invoice",
    ],
)
def test_allows_normal_select_queries(query):
    assert validate_query(query)  # returns the cleaned query, so it is not empty


@pytest.mark.parametrize(
    "query",
    [
        "SELECT 'a;b' AS semicolon_inside_text",
        "SELECT 'DELETE FROM artist' AS just_text",
        "SELECT * FROM artist -- DROP TABLE artist",
    ],
)
def test_forbidden_words_inside_text_or_comments_are_fine(query):
    """The validator reads the SQL structure, so it is not fooled by keywords in text."""
    assert validate_query(query)


def test_returns_cleaned_query():
    assert validate_query("  SELECT 1 ;  ") == "SELECT 1"
    assert validate_query("SELECT * FROM artist;") == "SELECT * FROM artist"


# ----------------------------------------------------------- not a SELECT ---
@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM artist",
        "INSERT INTO artist VALUES (1, 'x')",
        "UPDATE artist SET name = 'x'",
        "DROP TABLE artist",
        "TRUNCATE artist",
        "CREATE TABLE x (a int)",
        "ALTER TABLE artist ADD COLUMN z int",
        "GRANT ALL ON artist TO public",
        "SET default_transaction_read_only = off",
        "SHOW all",
        "EXPLAIN SELECT 1",
        "EXPLAIN ANALYZE DELETE FROM artist",
        "COPY artist TO '/tmp/x'",
        "COPY (SELECT 1) TO PROGRAM 'ls'",
        "CALL foo()",
        "DO $$ BEGIN END $$",
        "VACUUM artist",
        "BEGIN",
        "COMMIT",
    ],
)
def test_blocks_statements_that_are_not_select(query):
    with pytest.raises(ValidationError):
        validate_query(query)


# ------------------------------------------------- writes hidden in SELECT ---
@pytest.mark.parametrize(
    "query",
    [
        "WITH d AS (DELETE FROM artist RETURNING *) SELECT * FROM d",
        "WITH u AS (UPDATE artist SET name = 'x' RETURNING *) SELECT * FROM u",
        "WITH i AS (INSERT INTO artist (name) VALUES ('x') RETURNING *) SELECT * FROM i",
        "SELECT * INTO newtable FROM artist",
        "SELECT * FROM artist FOR UPDATE",
    ],
)
def test_blocks_writes_hidden_inside_a_select(query):
    with pytest.raises(ValidationError):
        validate_query(query)


# ------------------------------------------------------ several statements ---
@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1; DROP TABLE artist",
        "SELECT 1; SELECT 2",
        "SELECT 1 /* comment */; DROP TABLE artist",
        "select 1;\nselect 2;",
    ],
)
def test_blocks_multiple_statements(query):
    with pytest.raises(ValidationError, match="one SQL statement|forbidden|Only SELECT"):
        validate_query(query)


# ---------------------------------------------------- dangerous functions ---
@pytest.mark.parametrize(
    "query",
    [
        "SELECT pg_sleep(10)",
        "SELECT PG_SLEEP(10)",
        "SELECT pg_catalog.pg_sleep(10)",
        'SELECT "pg_sleep"(10)',
        "SELECT * FROM pg_sleep(1)",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_ls_dir('.')",
        "SELECT lo_import('/etc/passwd')",
        "SELECT set_config('default_transaction_read_only', 'off', false)",
        "SELECT nextval('some_sequence')",
        "SELECT setval('some_sequence', 1)",
        "SELECT * FROM dblink('x', 'select 1') AS t(a int)",
        "SELECT pg_terminate_backend(1)",
        "SELECT pg_advisory_lock(1)",
    ],
)
def test_blocks_dangerous_functions(query):
    with pytest.raises(ValidationError):
        validate_query(query)


# ------------------------------------------------------- schemas / tables ---
@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM pg_catalog.pg_user",
        "SELECT * FROM pg_user",
        "SELECT * FROM pg_shadow",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM other_schema.some_table",
    ],
)
def test_blocks_system_tables_and_other_schemas(query):
    with pytest.raises(ValidationError):
        validate_query(query)


# --------------------------------------------------- bad or empty queries ---
@pytest.mark.parametrize("query", ["", "   ", ";", None])
def test_blocks_empty_queries(query):
    with pytest.raises(ValidationError, match="empty"):
        validate_query(query)


@pytest.mark.parametrize("query", ["SELEC 1", "SELECT FROM WHERE", "SELECT * FROM"])
def test_blocks_queries_that_do_not_parse(query):
    with pytest.raises(ValidationError):
        validate_query(query)


def test_blocks_very_long_queries():
    with pytest.raises(ValidationError, match="too long"):
        validate_query("SELECT '" + "x" * MAX_QUERY_CHARS + "'")


# ------------------------------------------------------------- messages ---
def test_error_messages_help_the_llm_fix_its_query():
    with pytest.raises(ValidationError, match="Only SELECT queries are allowed"):
        validate_query("DROP TABLE artist")
    with pytest.raises(ValidationError, match="get_schema"):
        validate_query("SELECT * FROM pg_user")
    with pytest.raises(ValidationError, match="pg_sleep"):
        validate_query("SELECT pg_sleep(1)")