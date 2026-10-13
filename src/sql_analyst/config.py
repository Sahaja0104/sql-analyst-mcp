"""Central settings for the SQL Analyst MCP server.

Values come from environment variables. A local .env file is loaded
automatically, so you never hard-code passwords in the Python code.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Reads the .env file (if it exists) and adds its values to the environment.
load_dotenv()

DEFAULT_DATABASE_URL = "postgresql://mcp_reader:reader_pass@localhost:5433/chinook"


@dataclass(frozen=True)
class Settings:
    database_url: str          # how to connect (uses the read-only mcp_reader login)
    max_rows: int              # most rows a single query may return
    statement_timeout_ms: int  # longest a query may run, in milliseconds


def get_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL),
        max_rows=int(os.getenv("MAX_ROWS", "100")),
        statement_timeout_ms=int(os.getenv("STATEMENT_TIMEOUT_MS", "5000")),
    )