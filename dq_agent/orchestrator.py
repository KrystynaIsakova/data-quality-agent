"""Analytics Orchestrator: answers KPI questions with Gemini, computes nothing itself.

Gemini only picks the metric/dimension and words the answer. get_kpi() runs in
Python: definition from semantic_layer.yaml -> required tables/columns ->
Data Quality Subagent (deterministic checks) -> kpis.calculate_kpi (SQL).
Numbers reach the user unchanged via OrchestratorReply.kpi_results.
"""
import json
from dataclasses import dataclass, field
from typing import Callable

import psycopg
from google import genai
from google.genai import types

from dq_agent import kpis
from dq_agent.subagent import AgentError, DataQualitySubagent, QualityResult

MAX_TOOL_CALLS = 10
ROWS_FOR_MODEL = 30          # breakdown rows sent to Gemini; the app shows all
DQ_CHECKS = ["missing", "duplicates", "out_of_range"]

ROLE = """\
You are an Analytics Orchestrator for a Coursera PostgreSQL database.
You answer analytical questions ONLY with the provided tools.

For a KPI question:
1. Map the question to one metric (and optionally one dimension) from the list below.
   If nothing matches, say which metrics exist; do not improvise a metric.
2. Call get_kpi. It validates the data with the Data Quality Subagent and
   calculates the KPI with SQL using the approved definition.
3. Answer with the KPI value(s) and the data-quality warnings from the tool.
For a pure data-quality question, call check_data_quality.

Rules:
- NEVER calculate, estimate, re-round, sum or average numbers yourself. Copy
  values exactly as the tools return them, with their unit.
- State the metric's approved definition in one short line.
- Always report data_quality: confirmed warnings first (they reduce the
  reliability of the KPI), then possible ones, and briefly explain how each can
  bias this KPI. Say "no data-quality issues found" only when status is PASS.
- For breakdowns mention a few top rows; the full table is shown by the app.
- If a tool returns "error", explain it; do not invent a result.
- Answer in the user's language, briefly.
"""


@dataclass
class OrchestratorReply:
    text: str
    kpi_results: list[dict] = field(default_factory=list)   # exact tool outputs
    warnings: list[dict] = field(default_factory=list)


class Orchestrator:
    def __init__(self, subagent: DataQualitySubagent, layer: dict | None = None,
                 chat=None, on_tool_call: Callable[[str], None] = print):
        self.subagent = subagent
        self.layer = layer or kpis.load_semantic_layer()
        self.on_tool_call = on_tool_call
        self._dq_cache: dict[str, QualityResult] = {}
        self._kpi_results: list[dict] = []
        self._warnings: list[dict] = []
        self.tools = self._build_tools()
        self._chat = chat or self._create_chat()

    @classmethod
    def create(cls, on_tool_call: Callable[[str], None] = print) -> "Orchestrator":
        """One read-only DB connection (via the subagent), one Gemini chat."""
        subagent = DataQualitySubagent.create(on_tool_call=on_tool_call, llm=False)
        return cls(subagent, on_tool_call=on_tool_call)

    def ask(self, question: str) -> OrchestratorReply:
        self._kpi_results, self._warnings = [], []
        try:
            response = self._chat.send_message(question)
        except Exception as error:  # Gemini / network errors
            raise AgentError(self.subagent.settings.redact(str(error))) from error
        text = response.text or "The orchestrator did not produce an answer."
        return OrchestratorReply(text, list(self._kpi_results), _unique(self._warnings))

    def close(self) -> None:
        self.subagent.close()

    # ------------------------------------------------------------ direct API (no LLM)

    def kpi_report(self, metric: str, by: str | None = None) -> dict:
        """The get_kpi pipeline without Gemini, for dashboards: data-quality
        checks on the required data, then the KPI via SQL. Returns the full
        (untrimmed) KPI, required_data and data_quality, or {"error": ...}."""
        return self._guard(lambda: self._kpi_with_quality(metric, by))

    def data_health(self, tables: list[str]) -> dict[str, QualityResult]:
        """Data Quality Subagent results per table (same session cache as get_kpi)."""
        return {table: self._checked(table) for table in tables}

    # ------------------------------------------------------------ Gemini chat

    def _create_chat(self):
        # Keep a reference: a garbage-collected Client closes the chat's HTTP connection.
        self._client = genai.Client(api_key=self.subagent.settings.gemini_api_key)
        catalog = json.dumps(kpis.list_kpis(self.layer), ensure_ascii=False, indent=1)
        return self._client.chats.create(
            model=self.subagent.settings.gemini_model,
            config=types.GenerateContentConfig(
                system_instruction=f"{ROLE}\nApproved metrics and dimensions:\n{catalog}",
                tools=self.tools,
                temperature=0,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    maximum_remote_calls=MAX_TOOL_CALLS,
                ),
            ),
        )

    # ------------------------------------------------------------ tools

    def _build_tools(self) -> list:
        orch = self

        def list_kpis() -> dict:
            """List the approved metrics and dimensions from semantic_layer.yaml."""
            orch.on_tool_call("  📐 list_kpis()")
            return kpis.list_kpis(orch.layer)

        def get_kpi(metric: str, by: str | None = None) -> dict:
            """Validate the data behind a metric with the Data Quality Subagent,
            then calculate the metric with SQL using its approved definition.
            Returns the KPI and the relevant data-quality warnings.

            Args:
                metric: Metric name from semantic_layer.yaml, e.g. completion_rate.
                by: Optional dimension name from semantic_layer.yaml, e.g. course or specialization.
            """
            orch.on_tool_call(f"  📐 get_kpi({metric!r}, by={by!r})")
            return orch._guard(lambda: orch._get_kpi(metric, by))

        def check_data_quality(tables: list[str]) -> dict:
            """Run the standard data-quality checks (missing values, duplicates,
            out-of-range values) on tables and return the findings.

            Args:
                tables: Table names in schema public, e.g. ["enrollments"].
            """
            orch.on_tool_call(f"  📐 check_data_quality({tables!r})")
            return orch._guard(lambda: orch._check_quality(tables))

        return [list_kpis, get_kpi, check_data_quality]

    def _get_kpi(self, metric: str, by: str | None) -> dict:
        report = self._kpi_with_quality(metric, by)
        self._kpi_results.append(report["kpi"])
        self._warnings += report["data_quality"]["warnings"]
        return {**report, "kpi": _for_model(report["kpi"])}

    def _kpi_with_quality(self, metric: str, by: str | None) -> dict:
        metrics, dims = self.layer.get("metrics", {}), self.layer.get("dimensions", {})
        if metric not in metrics or metric not in kpis.METRICS:
            raise ValueError(f"Unknown metric '{metric}'. Available: {', '.join(metrics)}")
        if by is not None and by not in dims:
            raise ValueError(f"Unknown dimension '{by}'. Available: {', '.join(dims)}")

        # 1-2. definition and the data it needs
        required = self._required_columns(metric, by)
        # 3. validate that data
        data_quality = self._quality_for(required)
        # 4. calculate with SQL
        result = kpis.calculate_kpi(self.subagent.db.run_select, metric, by, self.layer)
        return {"kpi": result, "required_data": required, "data_quality": data_quality}

    def _required_columns(self, metric: str, by: str | None) -> dict[str, list[str]]:
        """Table -> columns the KPI reads (mirrors the SQL in kpis.calculate_kpi)."""
        table = self.layer["metrics"][metric]["table"]
        required = {table: list(kpis.METRICS[metric]["columns"])}
        if by is not None:
            dim = self.layer["dimensions"][by]
            required[table].append(kpis.JOIN_KEY)
            if dim["key"] != kpis.JOIN_KEY:
                required.setdefault(dim["table"], []).extend([kpis.JOIN_KEY, dim["key"]])
        return {t: sorted(set(c)) for t, c in required.items()}

    def _quality_for(self, required: dict[str, list[str]]) -> dict:
        """Checks per table (cached per session), filtered to the required columns."""
        warnings, errors, statuses = [], [], []
        for table, columns in required.items():
            result = self._checked(table)
            statuses.append(result.status)
            errors += result.errors
            for severity, checks in (("confirmed", result.confirmed),
                                     ("possible", result.possible)):
                warnings += [_warning(c, severity) for c in checks if _relevant(c, columns)]
        if errors:
            status = "ERROR"
        elif any(w["severity"] == "confirmed" for w in warnings):
            status = "FAIL"
        elif warnings:
            status = "WARN"
        else:
            status = "PASS"
        return {"status": status, "warnings": warnings, "errors": errors,
                "checked_tables": sorted(required)}

    def _check_quality(self, tables: list[str]) -> dict:
        out = {}
        for table in tables:
            result = self._checked(table)
            warnings = [_warning(c, "confirmed") for c in result.confirmed] + \
                       [_warning(c, "possible") for c in result.possible]
            self._warnings += warnings
            out[table] = {"status": result.status, "warnings": warnings,
                          "passed": len(result.passed), "errors": result.errors,
                          "facts": result.facts}
        return out

    def _checked(self, table: str) -> QualityResult:
        if table not in self._dq_cache:
            result = self.subagent.check_tables([table], checks=DQ_CHECKS)
            if result.status == "ERROR":
                return result          # not cached: a later call may succeed
            self._dq_cache[table] = result
        return self._dq_cache[table]

    def _guard(self, func) -> dict:
        """Turn expected errors into {"error": ...} for the model."""
        try:
            return func()
        except ValueError as error:
            return {"error": str(error)}
        except psycopg.Error as error:
            message = str(error).strip().splitlines()[0] if str(error).strip() else ""
            return {"error": "database error: " + self.subagent.settings.redact(message)}


def _relevant(check: dict, columns: list[str]) -> bool:
    """Table-level findings, or findings on any of the required columns."""
    if not check.get("column"):
        return True
    return bool(set(check["column"].split(", ")) & set(columns))


def _warning(check: dict, severity: str) -> dict:
    return {
        "severity": severity, "table": check["table"], "column": check["column"],
        "check": check["check"], "rule": check["rule"], "actual": check["actual"],
        "rows": check["rows"], "share": check["share"],
        "systematic": check["systematic"] or None, "note": check["note"] or None,
    }


def _for_model(result: dict) -> dict:
    """Trim long breakdowns for the model; the full result stays in kpi_results."""
    if "rows" not in result or len(result["rows"]) <= ROWS_FOR_MODEL:
        return result
    return {**result, "rows": result["rows"][:ROWS_FOR_MODEL],
            "rows_total": len(result["rows"]), "rows_shown": ROWS_FOR_MODEL}


def _unique(warnings: list[dict]) -> list[dict]:
    seen, out = set(), []
    for w in warnings:
        key = (w["table"], w["column"], w["check"], w["rule"])
        if key not in seen:
            seen.add(key)
            out.append(w)
    return out
