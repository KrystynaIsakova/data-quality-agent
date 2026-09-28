"""Deterministic KPI calculations from semantic_layer.yaml.

Independent of the agents: no Gemini, no tool context. Every value is computed
by one aggregate SELECT sent through run_select (sql_guard + read-only session).

semantic_layer.yaml is the approved source of definitions and is only read.
Its `calculation` text is not pasted into SQL; each metric's SQL is written
here to match it, and tests/test_kpis.py checks the two stay in sync.
"""
from decimal import Decimal
from pathlib import Path

import yaml
from psycopg import sql

from dq_agent.config import SEMANTIC_LAYER_PATH
from dq_agent.db import MAX_ROWS, RunSelect
from dq_agent.schema import SCHEMA, Schema

# SQL per metric over the fact table aliased `f`. Constants only, no user input.
METRICS = {
    "total_enrollments": {
        "value": "COUNT(f.enrollment_id)",
        "n": "COUNT(f.enrollment_id)",
        "columns": ["enrollment_id"],
        "unit": "count",
    },
    "average_progress": {
        "value": "AVG(f.progress_pct)",
        "n": "COUNT(f.progress_pct)",           # AVG ignores NULLs
        "columns": ["progress_pct"],
        "unit": "%",
    },
    "completion_rate": {
        # * 100.0: decimal division (integer COUNT / COUNT would truncate to 0).
        "value": "COUNT(CASE WHEN f.completed_at IS NOT NULL THEN 1 END) * 100.0 "
                 "/ NULLIF(COUNT(f.enrollment_id), 0)",
        "n": "COUNT(f.enrollment_id)",
        "columns": ["completed_at", "enrollment_id"],
        "unit": "%",
    },
}

# Column shared by the fact table and dimension tables, used for joins.
JOIN_KEY = "course_url"
DECIMALS = 2


def load_semantic_layer(path: Path = SEMANTIC_LAYER_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def validate_semantic_layer(layer: dict, schema: Schema) -> list[str]:
    """Problems between semantic_layer.yaml, METRICS and the DB schema."""
    problems = []
    for name, metric in layer.get("metrics", {}).items():
        if name not in METRICS:
            problems.append(f"metric '{name}' has no implementation in kpis.METRICS")
            continue
        table = metric.get("table")
        if table not in schema.tables():
            problems.append(f"metric '{name}': unknown table '{table}'")
            continue
        for column in METRICS[name]["columns"]:
            if not schema.has_column(table, column):
                problems.append(f"metric '{name}': unknown column '{table}.{column}'")
    for name, dim in layer.get("dimensions", {}).items():
        table, key = dim.get("table"), dim.get("key")
        if not schema.has_column(table, key):
            problems.append(f"dimension '{name}': unknown column '{table}.{key}'")
        if not schema.has_column(table, JOIN_KEY):
            problems.append(f"dimension '{name}': table '{table}' has no '{JOIN_KEY}'")
    return problems


def list_kpis(layer: dict | None = None) -> dict:
    """Available metrics and dimensions with their approved descriptions."""
    layer = layer or load_semantic_layer()
    return {
        "metrics": [
            {"name": name, "description": m.get("description", ""), "table": m.get("table"),
             "unit": METRICS.get(name, {}).get("unit")}
            for name, m in layer.get("metrics", {}).items()
        ],
        "dimensions": [
            {"name": name, "description": d.get("description", ""), "key": d.get("key")}
            for name, d in layer.get("dimensions", {}).items()
        ],
    }


def calculate_kpi(run_select: RunSelect, name: str, by: str | None = None,
                  layer: dict | None = None) -> dict:
    """Calculate one metric, overall or broken down by a dimension.

    Returns {"metric", "value", "unit", "n", ...}; with `by`, "value"/"n" are
    replaced by "rows": [{<dimension>: key, "value": ..., "n": ...}, ...].
    Raises ValueError for an unknown metric or dimension.
    """
    layer = layer or load_semantic_layer()
    metrics = layer.get("metrics", {})
    if name not in metrics or name not in METRICS:
        raise ValueError(f"Unknown metric '{name}'. Available: {', '.join(metrics)}")
    definition, impl = metrics[name], METRICS[name]

    select = [sql.SQL(impl["value"] + " AS value"), sql.SQL(impl["n"] + " AS n")]
    source = sql.SQL("{} AS f").format(sql.Identifier(SCHEMA, definition["table"]))
    group = sql.SQL("")
    if by is not None:
        dimensions = layer.get("dimensions", {})
        if by not in dimensions:
            raise ValueError(f"Unknown dimension '{by}'. Available: {', '.join(dimensions)}")
        dim = dimensions[by]
        key = sql.Identifier(dim["key"])
        if dim["key"] == JOIN_KEY:
            # The fact table carries the key itself (course): no join needed.
            column = sql.SQL("f.{}").format(key)
        else:
            # Distinct (join key, dimension key) pairs: duplicate rows in the
            # dimension table must not multiply the fact rows. A course that
            # belongs to several specializations is counted in each of them.
            source += sql.SQL(
                " LEFT JOIN (SELECT DISTINCT {j}, {k} FROM {t}) AS d ON d.{j} = f.{j}"
            ).format(j=sql.Identifier(JOIN_KEY), k=key, t=sql.Identifier(SCHEMA, dim["table"]))
            column = sql.SQL("d.{}").format(key)
        select.insert(0, column + sql.SQL(" AS dim"))
        group = sql.SQL(" GROUP BY {c} ORDER BY n DESC, {c}").format(c=column)

    query = sql.SQL("SELECT {} FROM {}").format(sql.SQL(", ").join(select), source) + group
    rows = run_select(query, None)

    result = {
        "metric": name,
        "description": definition.get("description", ""),
        "definition": " ".join(str(definition.get("definition", "")).split()),
        "calculation": " ".join(str(definition.get("calculation", "")).split()),
        "table": definition["table"],
        "unit": impl["unit"],
        "by": by,
    }
    if by is None:
        result.update(value=_round(rows[0]["value"]), n=int(rows[0]["n"]))
    else:
        result["rows"] = [
            {by: r["dim"], "value": _round(r["value"]), "n": int(r["n"])} for r in rows
        ]
        result["truncated"] = len(rows) >= MAX_ROWS
    return result


def total_enrollments(run_select: RunSelect, by: str | None = None) -> dict:
    return calculate_kpi(run_select, "total_enrollments", by)


def average_progress(run_select: RunSelect, by: str | None = None) -> dict:
    return calculate_kpi(run_select, "average_progress", by)


def completion_rate(run_select: RunSelect, by: str | None = None) -> dict:
    return calculate_kpi(run_select, "completion_rate", by)


def _round(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        value = float(value)
    return round(value, DECIMALS) if isinstance(value, float) else value
