"""Read-only PostgreSQL access. Every query goes through sql_guard first."""
from typing import Any, Callable, Sequence

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from dq_agent.sql_guard import assert_safe_select

MAX_ROWS = 1000
STATEMENT_TIMEOUT_MS = 30_000
CONNECT_TIMEOUT_S = 10

Query = str | sql.Composable
RunSelect = Callable[[Query, Sequence[Any] | None], list[dict]]


class Database:
    def __init__(self, database_url: str):
        self._url = database_url
        self._conn: psycopg.Connection | None = None

    def _connection(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._url,
                options=(
                    "-c default_transaction_read_only=on "
                    f"-c statement_timeout={STATEMENT_TIMEOUT_MS}"
                ),
                connect_timeout=CONNECT_TIMEOUT_S,
                row_factory=dict_row,
            )
            self._conn.read_only = True
        return self._conn

    def run_select(self, query: Query, params: Sequence[Any] | None = None) -> list[dict]:
        """Validate and run one SELECT; return at most MAX_ROWS rows."""
        conn = self._connection()
        text = query.as_string(conn) if isinstance(query, sql.Composable) else query
        assert_safe_select(text)
        try:
            with conn.cursor() as cur:
                cur.execute(text, params)
                return cur.fetchmany(MAX_ROWS)
        finally:
            conn.rollback()

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
