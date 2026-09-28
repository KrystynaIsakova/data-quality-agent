"""Data-quality tools exposed to Gemini via automatic function calling.

Gemini reads each tool's signature and docstring as its description. Every tool:
- checks table/column names against the schema whitelist;
- builds SQL with psycopg.sql.Identifier and passes values as %s parameters;
- runs only aggregate SELECTs through run_select (which applies sql_guard);
- returns aggregates only (no raw rows, no IDs, no free text);
- records its results in the ResultStore used by the report.
"""
import functools
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator
from decimal import Decimal

import psycopg
from psycopg import sql

from dq_agent.db import RunSelect
from dq_agent.results import FAIL, INFO, PASS, CheckResult, ResultStore
from dq_agent.rules import RangeRule, Rules
from dq_agent.schema import SCHEMA, Schema

SENTINELS = ["na", "n/a", "null", "none", "-", "?", "unknown", "nan"]
TOP_VALUES = 5
MOSTLY_ONE_VALUE = 0.8
HIDDEN_MISSING = "приховані пропуски в тексті"

CONSTRAINTS_QUERY = """
SELECT tc.constraint_type, kcu.column_name
FROM information_schema.table_constraints AS tc
JOIN information_schema.key_column_usage AS kcu
  ON tc.constraint_name = kcu.constraint_name
 AND tc.table_schema = kcu.table_schema
 AND tc.table_name = kcu.table_name
WHERE tc.table_schema = %s AND tc.table_name = %s
  AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE', 'FOREIGN KEY')
ORDER BY tc.constraint_type, kcu.ordinal_position
"""


@dataclass
class ToolContext:
    run_select: RunSelect
    schema: Schema
    rules: Rules
    results: ResultStore
    on_tool_call: Callable[[str], None] = print   # where tool-call log lines go


_ctx: ToolContext | None = None
# Tools read their context from the module global above, so calls from several
# sessions (e.g. two browser tabs) must not interleave. Reentrant, so an
# orchestrator's tool call can invoke the subagent in the same thread.
_LOCK = threading.RLock()


def configure(ctx: ToolContext | None) -> None:
    global _ctx
    _ctx = ctx


@contextmanager
def use(ctx: ToolContext) -> Iterator[ToolContext]:
    """Run tools with ctx; restore the previous context afterwards."""
    with _LOCK:
        previous = _ctx
        configure(ctx)
        try:
            yield ctx
        finally:
            configure(previous)


def _context() -> ToolContext:
    if _ctx is None:
        raise RuntimeError("tools.configure() must be called before using the tools")
    return _ctx


def _tool(func):
    """Log the call and turn expected errors into {"error": ...} for the model."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        shown = ", ".join([repr(a) for a in args] + [f"{k}={v!r}" for k, v in kwargs.items()])
        _context().on_tool_call(f"  🔧 {func.__name__}({shown})")
        try:
            return func(*args, **kwargs)
        except ValueError as error:          # unknown names, bad bounds, UnsafeSQLError
            return {"error": str(error)}
        except psycopg.Error as error:
            message = str(error).strip().splitlines()[0] if str(error).strip() else ""
            return {"error": f"database error ({type(error).__name__}): {message}"}
    return wrapper


# ---------------------------------------------------------------- tools

@_tool
def describe_table(table: str) -> dict:
    """Show the structure of a table: columns, data types, nullability,
    primary / unique / foreign keys, key rules from rules/keys.yaml and text
    columns that look like dates. Call it before other checks on a table.

    Args:
        table: Table name in schema public, for example enrollments.
    """
    ctx = _context()
    table = ctx.schema.require_table(table)
    columns = ctx.schema.columns(table)
    constraints: dict[str, list[str]] = {}
    for row in ctx.run_select(CONSTRAINTS_QUERY, [SCHEMA, table]):
        constraints.setdefault(row["constraint_type"], []).append(row["column_name"])

    date_like_text = [c.name for c in columns if c.is_text and _looks_like_date(c.name)]
    key = ctx.rules.key_for(table)

    ctx.results.set_fact(table, "columns", len(columns))
    ctx.results.set_fact(table, "constraints", constraints)
    ctx.results.set_fact(table, "date_like_text", date_like_text)

    return {
        "table": table,
        "columns": [
            {"name": c.name, "type": c.data_type, "nullable": c.nullable} for c in columns
        ],
        "constraints": constraints or "none declared",
        "key_rules": {
            "id": key.id_column if key else None,
            "natural_key": list(key.natural_key) if key else [],
        },
        "date_like_text_columns": date_like_text,
        "numeric_columns": [c.name for c in ctx.schema.numeric_columns(table)],
        "range_rules": [r.describe() for r in ctx.rules.ranges_for(table)],
    }


@_tool
def count_rows(table: str) -> dict:
    """Count the exact number of rows in a table.

    Args:
        table: Table name in schema public, for example payments.
    """
    ctx = _context()
    table = ctx.schema.require_table(table)
    query = sql.SQL("SELECT count(*) AS n FROM {}").format(_table_ref(table))
    n = int(ctx.run_select(query, None)[0]["n"])
    ctx.results.set_fact(table, "rows", n)
    return {"table": table, "row_count": n}


@_tool
def check_missing_values(table: str, columns: list[str] | None = None) -> dict:
    """Check missing values per column: NULLs, and for text columns also empty
    strings, sentinel values (na, n/a, null, none, -, ?, unknown, nan) and
    leading/trailing whitespace. Returns counts and shares only.

    Args:
        table: Table name in schema public.
        columns: Optional list of columns to check; all columns if omitted.
    """
    ctx = _context()
    table = ctx.schema.require_table(table)
    cols = (
        [ctx.schema.require_column(table, c) for c in columns]
        if columns else ctx.schema.columns(table)
    )
    key = ctx.rules.key_for(table)
    id_column = key.id_column if key else None

    parts = [sql.SQL("count(*) AS total")]
    params: list = []
    for i, c in enumerate(cols):
        ident = sql.Identifier(c.name)
        parts.append(sql.SQL("count(*) FILTER (WHERE {c} IS NULL) AS {a}").format(
            c=ident, a=sql.Identifier(f"c{i}_null")))
        if c.is_text:
            parts.append(sql.SQL("count(*) FILTER (WHERE btrim({c}) = '') AS {a}").format(
                c=ident, a=sql.Identifier(f"c{i}_empty")))
            parts.append(sql.SQL(
                "count(*) FILTER (WHERE lower(btrim({c})) = ANY(%s)) AS {a}").format(
                c=ident, a=sql.Identifier(f"c{i}_sentinel")))
            params.append(SENTINELS)
            parts.append(sql.SQL(
                "count(*) FILTER (WHERE {c} <> btrim({c}) AND btrim({c}) <> '') AS {a}").format(
                c=ident, a=sql.Identifier(f"c{i}_space")))
    query = sql.SQL("SELECT {} FROM {}").format(sql.SQL(", ").join(parts), _table_ref(table))
    row = ctx.run_select(query, params)[0]

    total = int(row["total"])
    ctx.results.set_fact(table, "rows", total)
    report_cols, nulls_fact, hidden_failures = [], {}, 0
    for i, c in enumerate(cols):
        nulls = int(row[f"c{i}_null"])
        item = {"column": c.name, "nulls": nulls, "null_share": _share(nulls, total)}
        if nulls:
            nulls_fact[c.name] = nulls

        if c.name == id_column:
            ctx.results.add(CheckResult(
                check="missing_values", table=table, column=c.name,
                rule="ID-колонка без NULL (rules/keys.yaml)", expected="0 NULL",
                actual=f"{nulls} NULL", status=FAIL if nulls else PASS, basis="rule",
                rows=nulls, share=_share(nulls, total), condition=f"{c.name} IS NULL",
            ))

        if c.is_text:
            empty, sentinel, space = (int(row[f"c{i}_{s}"]) for s in ("empty", "sentinel", "space"))
            item.update(empty_strings=empty, sentinel_values=sentinel, edge_whitespace=space)
            hidden = empty + sentinel + space
            if not hidden:
                ctx.results.discard(("missing_values", table, c.name, HIDDEN_MISSING))
            else:
                hidden_failures += 1
                ctx.results.add(CheckResult(
                    check="missing_values", table=table, column=c.name,
                    rule=HIDDEN_MISSING, expected="0",
                    actual=f"порожніх {empty}, сентинелів {sentinel}, пробілів на краях {space}",
                    status=FAIL, basis="heuristic", rows=hidden, share=_share(hidden, total),
                    condition=(f"btrim({c.name}) = '' OR lower(btrim({c.name})) IN (сентинели) "
                               f"OR {c.name} <> btrim({c.name})"),
                    note="Може бути нормою для необов'язкових полів; перевірити, чи не маскує NULL.",
                ))
        report_cols.append(item)

    ctx.results.set_fact(table, "nulls", {**ctx.results.tables.get(table, {}).get("nulls", {}),
                                          **nulls_fact})
    text_checked = sum(1 for c in cols if c.is_text)
    if text_checked and not hidden_failures:
        ctx.results.add(CheckResult(
            check="missing_values", table=table, column=None,
            rule=HIDDEN_MISSING, expected="0",
            actual=f"0 у {text_checked} текстових колонках", status=PASS, basis="heuristic",
        ))
    ctx.results.add(CheckResult(
        check="missing_values", table=table, column=None, rule="NULL-значення",
        expected="інформаційно",
        actual=(", ".join(f"{k}: {v}" for k, v in nulls_fact.items())
                or f"немає NULL у {len(cols)} колонках"),
        status=INFO, basis="info",
    ))
    return {
        "table": table, "total_rows": total, "columns": report_cols,
        "note": "NULLs are informational unless the column is an ID; "
                "empty/sentinel/whitespace values are possible issues (assumptions).",
    }


@_tool
def check_duplicates(table: str, key_columns: list[str] | None = None) -> dict:
    """Check duplicates in 4 slices: full row, ID column, row without the ID,
    and natural key. ID and natural key come from rules/keys.yaml unless
    key_columns is given. For failing keys also returns group sizes to judge
    whether duplication is systematic.

    Args:
        table: Table name in schema public.
        key_columns: Optional natural key to test instead of the one in rules/keys.yaml.
    """
    ctx = _context()
    table = ctx.schema.require_table(table)
    all_cols = [c.name for c in ctx.schema.columns(table)]
    key = ctx.rules.key_for(table)
    id_column = key.id_column if key and key.id_column in all_cols else None
    if key_columns:
        natural = [ctx.schema.require_column(table, c).name for c in key_columns]
        natural_basis, natural_source = "user", "ключ із запиту"
    elif key and key.natural_key:
        natural = list(key.natural_key)
        natural_basis, natural_source = "rule", "rules/keys.yaml"
    else:
        natural, natural_basis, natural_source = [], "", ""
    non_id = [c for c in all_cols if c != id_column]

    parts = [
        sql.SQL("count(*) AS total"),
        sql.SQL("count(*) - count(DISTINCT t.*) AS full_row"),
    ]
    if id_column:
        ident = sql.Identifier(id_column)
        parts.append(sql.SQL("count({c}) - count(DISTINCT {c}) AS id_dup").format(c=ident))
        if non_id:
            parts.append(sql.SQL("count(*) - count(DISTINCT ROW({})) AS no_id_dup").format(
                _idents(non_id)))
    if natural:
        parts.append(sql.SQL("count(*) - count(DISTINCT ROW({})) AS natural_dup").format(
            _idents(natural)))
    query = sql.SQL("SELECT {} FROM {} AS t").format(
        sql.SQL(", ").join(parts), _table_ref(table))
    row = ctx.run_select(query, None)[0]
    total = int(row["total"])
    ctx.results.set_fact(table, "rows", total)

    slices, skipped = [], []

    def record(name, rule, extra, basis, key_cols, condition):
        systematic = ""
        groups = None
        if extra and key_cols:
            groups = _duplicate_groups(ctx, table, key_cols)
            systematic = _group_verdict(groups)
        ctx.results.add(CheckResult(
            check="duplicates", table=table, column=", ".join(key_cols) if key_cols else None,
            rule=rule, expected="0 зайвих рядків", actual=f"{extra} зайвих рядків",
            status=FAIL if extra else PASS, basis=basis, rows=extra,
            share=_share(extra, total), condition=condition, systematic=systematic,
        ))
        slices.append({
            "slice": name, "extra_rows": extra, "share": _share(extra, total),
            "duplicate_groups": groups, "systematic": systematic or None,
            "report_section": _section(extra, basis),
        })

    record("full_row", "дублікати повного рядка", int(row["full_row"]), "heuristic", [],
           "count(*) - count(DISTINCT t.*)")
    if id_column:
        record("id_column", f"унікальність ID ({id_column}, rules/keys.yaml)",
               int(row["id_dup"]), "rule", [id_column],
               f"GROUP BY {id_column} HAVING count(*) > 1")
        if non_id:
            record("row_without_id", "дублікати рядка без ID", int(row["no_id_dup"]),
                   "heuristic", [], f"count(DISTINCT ROW(всі колонки крім {id_column}))")
    else:
        skipped.append("id_column / row_without_id: no ID column in rules/keys.yaml")
    if natural:
        record("natural_key", f"унікальність природного ключа ({natural_source})",
               int(row["natural_dup"]), natural_basis, natural,
               f"GROUP BY {', '.join(natural)} HAVING count(*) > 1")
    else:
        skipped.append("natural_key: not defined in rules/keys.yaml; pass key_columns to test one")

    return {"table": table, "total_rows": total, "slices": slices, "skipped": skipped}


@_tool
def check_out_of_range(
    table: str,
    column: str | None = None,
    min_value: float | None = None,
    max_value: float | None = None,
) -> dict:
    """Find numeric values outside the allowed range. Without column, applies
    every range rule for the table from rules/ranges.yaml. With column only,
    applies that column's rule. With min_value and/or max_value, uses those
    bounds (inclusive) instead. Returns violation counts, min/max and the most
    frequent violating values to judge whether the defect is systematic.

    Args:
        table: Table name in schema public.
        column: Optional numeric column to check.
        min_value: Optional inclusive lower bound (requires column).
        max_value: Optional inclusive upper bound (requires column).
    """
    ctx = _context()
    table = ctx.schema.require_table(table)
    custom_bounds = min_value is not None or max_value is not None

    if column is None:
        if custom_bounds:
            raise ValueError("min_value / max_value require a column")
        rules = ctx.rules.ranges_for(table)
        if not rules:
            numeric = [c.name for c in ctx.schema.numeric_columns(table)]
            raise ValueError(
                f"No range rules for table '{table}' in rules/ranges.yaml. "
                f"Numeric columns: {', '.join(numeric) or 'none'}. "
                "Pass column with min_value and/or max_value."
            )
    else:
        col = ctx.schema.require_column(table, column)
        if not col.is_numeric:
            raise ValueError(f"Column '{table}.{col.name}' is {col.data_type}, not numeric")
        if custom_bounds:
            if min_value is not None and max_value is not None and min_value > max_value:
                raise ValueError("min_value is greater than max_value")
            rules = [RangeRule(table, col.name, min_value, max_value, "user",
                               "Межі задані в запиті користувача.")]
        else:
            rules = ctx.rules.ranges_for(table, col.name)
            if not rules:
                raise ValueError(
                    f"No range rule for '{table}.{col.name}' in rules/ranges.yaml. "
                    "Pass min_value and/or max_value."
                )

    return {"table": table, "checks": [_check_range(ctx, rule) for rule in rules]}


TOOLS = [describe_table, count_rows, check_missing_values, check_duplicates, check_out_of_range]


# ---------------------------------------------------------------- helpers

def _check_range(ctx: ToolContext, rule: RangeRule) -> dict:
    col = sql.Identifier(rule.column)
    cond, cond_params = _range_condition(col, rule.min, rule.max)
    query = sql.SQL(
        "SELECT count(*) AS total, count({c}) AS non_null, "
        "count(*) FILTER (WHERE {cond}) AS violations, "
        "count(DISTINCT {c}) FILTER (WHERE {cond}) AS distinct_violating, "
        "min({c}) AS min_value, max({c}) AS max_value FROM {t}"
    ).format(c=col, cond=cond, t=_table_ref(rule.table))
    row = ctx.run_select(query, cond_params * 2)[0]
    total, violations = int(row["total"]), int(row["violations"])
    ctx.results.set_fact(rule.table, "rows", total)

    top = []
    if violations:
        top_query = sql.SQL(
            "SELECT {c} AS value, count(*) AS n FROM {t} WHERE {cond} "
            "GROUP BY {c} ORDER BY n DESC, 1 LIMIT {limit}"
        ).format(c=col, t=_table_ref(rule.table), cond=cond, limit=sql.Literal(TOP_VALUES))
        top = [{"value": _num(r["value"]), "count": int(r["n"])}
               for r in ctx.run_select(top_query, cond_params)]
    systematic = _value_verdict(violations, int(row["distinct_violating"]), top)

    ctx.results.add(CheckResult(
        check="out_of_range", table=rule.table, column=rule.column, rule=rule.describe(),
        expected="0 порушень",
        actual=(f"{violations} порушень; min {_num(row['min_value'])}, "
                f"max {_num(row['max_value'])}"),
        status=FAIL if violations else PASS, basis=rule.basis, rows=violations,
        share=_share(violations, total), condition=_condition_text(rule),
        systematic=systematic, note=rule.note,
    ))
    return {
        "column": rule.column, "rule": rule.describe(), "basis": rule.basis,
        "total_rows": total, "non_null": int(row["non_null"]),
        "violations": violations, "share": _share(violations, total),
        "min": _num(row["min_value"]), "max": _num(row["max_value"]),
        "top_violating_values": top, "systematic": systematic or None,
        "report_section": _section(violations, rule.basis),
    }


def _range_condition(col: sql.Identifier, lo: float | None, hi: float | None):
    parts, params = [], []
    if lo is not None:
        parts.append(sql.SQL("{} < %s").format(col))
        params.append(lo)
    if hi is not None:
        parts.append(sql.SQL("{} > %s").format(col))
        params.append(hi)
    return sql.SQL("({})").format(sql.SQL(" OR ").join(parts)), params


def _condition_text(rule: RangeRule) -> str:
    parts = []
    if rule.min is not None:
        parts.append(f"{rule.column} < {_num(rule.min)}")
    if rule.max is not None:
        parts.append(f"{rule.column} > {_num(rule.max)}")
    return " OR ".join(parts)


def _duplicate_groups(ctx: ToolContext, table: str, key_cols: list[str]) -> dict:
    keys = _idents(key_cols)
    not_null = sql.SQL(" AND ").join(
        sql.SQL("{} IS NOT NULL").format(sql.Identifier(c)) for c in key_cols)
    query = sql.SQL(
        "SELECT count(*) AS n_groups, min(n) AS min_size, max(n) AS max_size FROM ("
        "SELECT count(*) AS n FROM {t} WHERE {nn} GROUP BY {k} HAVING count(*) > 1"
        ") AS g"
    ).format(t=_table_ref(table), nn=not_null, k=keys)
    row = ctx.run_select(query, None)[0]
    return {
        "groups": int(row["n_groups"]),
        "min_size": _num(row["min_size"]),
        "max_size": _num(row["max_size"]),
    }


def _group_verdict(groups: dict) -> str:
    if not groups or not groups["groups"]:
        return ""
    if groups["min_size"] == groups["max_size"]:
        return (f"систематичний: усі {groups['groups']} груп мають рівно "
                f"{groups['max_size']} рядки (схоже на повторне завантаження)")
    return (f"різний розмір груп ({groups['groups']} груп, від {groups['min_size']} "
            f"до {groups['max_size']} рядків) — потрібна перевірка людиною")


def _value_verdict(violations: int, distinct: int, top: list[dict]) -> str:
    if not violations:
        return ""
    if distinct == 1:
        return (f"систематичний: усі {violations} порушень мають одне значення "
                f"{top[0]['value']} (заглушка / технічний артефакт)")
    if top and top[0]["count"] / violations >= MOSTLY_ONE_VALUE:
        return (f"переважно систематичний: значення {top[0]['value']} дає "
                f"{top[0]['count'] / violations:.0%} порушень")
    return f"різні значення ({distinct}) — схоже на випадкові помилки, потрібна людина"


def _section(violations: int, basis: str) -> str:
    if not violations:
        return "pass"
    return "confirmed" if basis == "rule" else "possible (assumption)"


def _looks_like_date(name: str) -> bool:
    return name.endswith(("_at", "_date")) or name == "date"


def _table_ref(table: str) -> sql.Identifier:
    return sql.Identifier(SCHEMA, table)


def _idents(columns: list[str]) -> sql.Composed:
    return sql.SQL(", ").join(sql.Identifier(c) for c in columns)


def _share(n: int, total: int) -> float | None:
    return round(n / total, 6) if total else None


def _num(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value
