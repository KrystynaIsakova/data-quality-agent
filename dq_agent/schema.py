"""Whitelist of tables and columns loaded from information_schema.

Table and column names come from the LLM, so every name is checked here before
it is placed into SQL (and then quoted with psycopg.sql.Identifier).
"""
from dataclasses import dataclass

from dq_agent.db import RunSelect

SCHEMA = "public"
NUMERIC_TYPES = {
    "smallint", "integer", "bigint", "numeric", "decimal", "real", "double precision",
}
TEXT_TYPES = {"text", "character varying", "character"}

COLUMNS_QUERY = """
SELECT table_name, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = %s
ORDER BY table_name, ordinal_position
"""


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str
    nullable: bool

    @property
    def is_numeric(self) -> bool:
        return self.data_type in NUMERIC_TYPES

    @property
    def is_text(self) -> bool:
        return self.data_type in TEXT_TYPES


class Schema:
    def __init__(self, tables: dict[str, dict[str, Column]]):
        self._tables = tables

    @classmethod
    def load(cls, run_select: RunSelect) -> "Schema":
        tables: dict[str, dict[str, Column]] = {}
        for row in run_select(COLUMNS_QUERY, [SCHEMA]):
            tables.setdefault(row["table_name"], {})[row["column_name"]] = Column(
                name=row["column_name"],
                data_type=row["data_type"],
                nullable=row["is_nullable"] == "YES",
            )
        return cls(tables)

    def tables(self) -> list[str]:
        return sorted(self._tables)

    def require_table(self, table: str) -> str:
        name = str(table).strip().strip('"').lower()
        if name.startswith(f"{SCHEMA}."):
            name = name[len(SCHEMA) + 1:]
        if name not in self._tables:
            raise ValueError(
                f"Unknown table '{table}'. Available tables: {', '.join(self.tables())}"
            )
        return name

    def require_column(self, table: str, column: str) -> Column:
        table = self.require_table(table)
        name = str(column).strip().strip('"').lower()
        if name not in self._tables[table]:
            raise ValueError(
                f"Unknown column '{column}' in table '{table}'. "
                f"Available columns: {', '.join(self._tables[table])}"
            )
        return self._tables[table][name]

    def columns(self, table: str) -> list[Column]:
        return list(self._tables[self.require_table(table)].values())

    def numeric_columns(self, table: str) -> list[Column]:
        return [c for c in self.columns(table) if c.is_numeric]

    def has_column(self, table: str, column: str) -> bool:
        return column in self._tables.get(table, {})
