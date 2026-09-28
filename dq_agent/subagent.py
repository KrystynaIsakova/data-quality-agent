"""Data Quality Subagent: the check logic behind a callable, structured API.

Another agent (e.g. an analytics orchestrator) calls it in-process:

    dq = DataQualitySubagent.create()
    result = dq.check_tables(["enrollments"])      # deterministic, no LLM
    result = dq.run("check progress_pct")          # natural language via Gemini
    result.to_dict()                               # JSON-serialisable

Each result covers only the checks of that call; the ResultStore and the
Markdown report stay cumulative for the subagent's lifetime.
"""
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import psycopg

from dq_agent import report, tools
from dq_agent.agent import DataQualityAgent
from dq_agent.config import REPORT_PATH, RULES_DIR, ConfigError, Settings, load_settings
from dq_agent.db import Database
from dq_agent.results import FAIL, PASS, CheckResult, ResultStore
from dq_agent.rules import Rules, RulesError
from dq_agent.schema import Schema

# Deterministic check sequence for check_tables(), in execution order.
CHECKS = {
    "describe": tools.describe_table,
    "count": tools.count_rows,
    "missing": tools.check_missing_values,
    "duplicates": tools.check_duplicates,
    "out_of_range": tools.check_out_of_range,
}

STATUS_PASS, STATUS_FAIL = "PASS", "FAIL"
STATUS_NO_CHECKS, STATUS_ERROR = "NO_CHECKS", "ERROR"


class StartupError(RuntimeError):
    pass


class AgentError(RuntimeError):
    pass


@dataclass
class QualityResult:
    """Structured outcome of one subagent call."""
    status: str                     # PASS / FAIL / NO_CHECKS / ERROR
    tables: list[str] = field(default_factory=list)
    confirmed: list[dict] = field(default_factory=list)   # FAIL, basis == "rule"
    possible: list[dict] = field(default_factory=list)    # FAIL, assumption/heuristic/user
    passed: list[dict] = field(default_factory=list)
    info: list[dict] = field(default_factory=list)
    facts: dict[str, dict] = field(default_factory=dict)  # per table: rows, columns, ...
    questions: list[str] = field(default_factory=list)    # for the business
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    summary: str = ""
    report_path: str | None = None

    @property
    def all_pass(self) -> bool:
        return self.status == STATUS_PASS

    def to_dict(self) -> dict:
        return {**asdict(self), "all_pass": self.all_pass}

    @classmethod
    def failed(cls, error: str) -> "QualityResult":
        return cls(status=STATUS_ERROR, errors=[error], summary=error)


@dataclass
class DataQualitySubagent:
    settings: Settings
    db: Database
    schema: Schema
    rules: Rules
    rule_problems: list[str]
    store: ResultStore
    ctx: tools.ToolContext
    agent: DataQualityAgent | None       # None: deterministic checks only
    report_path: Path | None = field(default=None)

    @classmethod
    def create(cls, on_tool_call: Callable[[str], None] = print,
               llm: bool = True) -> "DataQualitySubagent":
        """Load settings and rules, connect to the DB, load the schema and
        (with llm=True) open a Gemini chat."""
        try:
            settings = load_settings()
            rules = Rules.load(RULES_DIR)
        except (ConfigError, RulesError) as error:
            raise StartupError(f"Configuration error: {error}") from error

        db = Database(settings.database_url)
        try:
            schema = Schema.load(db.run_select)
        except psycopg.Error as error:
            db.close()
            raise StartupError(
                f"Cannot connect to the database: {settings.redact(str(error)).strip()}"
            ) from error

        store = ResultStore()
        ctx = tools.ToolContext(db.run_select, schema, rules, store, on_tool_call)
        return cls(
            settings=settings, db=db, schema=schema, rules=rules,
            rule_problems=rules.validate_against(schema), store=store, ctx=ctx,
            agent=DataQualityAgent(settings, ctx) if llm else None,
        )

    # ------------------------------------------------------------ entry points

    def check_tables(self, tables: list[str], checks: list[str] | None = None) -> QualityResult:
        """Run the standard checks on tables without an LLM.

        checks: subset of CHECKS keys (default: all). Tool errors (unknown
        table, DB error) are collected in `errors`, not raised.
        """
        unknown = [c for c in checks or [] if c not in CHECKS]
        if unknown:
            return QualityResult.failed(
                f"Unknown checks: {', '.join(unknown)}. Available: {', '.join(CHECKS)}")
        selected = [c for c in CHECKS if checks is None or c in checks]

        errors, skipped = [], []
        with tools.use(self.ctx):
            self.store.begin_run()
            for table in tables:
                try:
                    table = self.schema.require_table(table)
                except ValueError as error:
                    errors.append(str(error))
                    continue
                for name in selected:
                    if name == "out_of_range" and not self.rules.ranges_for(table):
                        skipped.append(f"{table}: out_of_range (no rules in rules/ranges.yaml)")
                        continue
                    output = CHECKS[name](table)
                    if "error" in output:
                        errors.append(f"{table}: {name}: {self.settings.redact(output['error'])}")
                    skipped += [f"{table}: {s}" for s in output.get("skipped", [])]

        result = self._collect(errors=errors, skipped=skipped)
        result.summary = _summary(result)
        return result

    def run(self, request: str) -> QualityResult:
        """Natural-language request through Gemini; errors become status ERROR."""
        try:
            return self._run(request)
        except AgentError as error:
            return QualityResult.failed(str(error))

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------ internals

    def _run(self, request: str) -> QualityResult:
        """Like run(), but raises AgentError (used by the UIs)."""
        if self.agent is None:
            raise AgentError("This subagent was created with llm=False; use check_tables().")
        with tools.use(self.ctx):
            self.store.begin_run()
            try:
                reply = self.agent.ask(request)
            except Exception as error:  # Gemini / network / DB errors
                raise AgentError(self.settings.redact(str(error))) from error
        self.store.add_exchange(request, reply.text, reply.questions)
        result = self._collect(questions=reply.questions)
        result.summary = reply.text
        return result

    def _collect(self, errors=(), skipped=(), questions=()) -> QualityResult:
        """Build the result for the current run and rewrite the report."""
        if len(self.store) or self.store.tables:
            self.report_path = report.write(
                self.store, REPORT_PATH, self.rules.label, self.rule_problems)

        run = self.store.run_results()
        tables = self.store.run_tables()
        if any(r.status == FAIL for r in run):
            status = STATUS_FAIL
        elif run:
            status = STATUS_PASS
        elif errors:
            status = STATUS_ERROR
        else:
            status = STATUS_NO_CHECKS
        return QualityResult(
            status=status,
            tables=tables,
            confirmed=[_check(r, "confirmed") for r in run if r.is_confirmed],
            possible=[_check(r, "possible") for r in run if r.is_possible],
            passed=[_check(r, "pass") for r in run if r.status == PASS],
            info=[_check(r, "info") for r in run
                  if r.status != PASS and not (r.is_confirmed or r.is_possible)],
            facts={t: dict(self.store.tables[t]) for t in tables if t in self.store.tables},
            questions=list(questions),
            skipped=list(skipped),
            errors=list(errors),
            report_path=str(self.report_path) if self.report_path else None,
        )


def _check(result: CheckResult, section: str) -> dict:
    return {**asdict(result), "target": result.target, "section": section}


def _summary(result: QualityResult) -> str:
    parts = [
        f"{result.status}: {len(result.confirmed)} confirmed, "
        f"{len(result.possible)} possible, {len(result.passed)} passed"
    ]
    if result.tables:
        parts.append(f"tables: {', '.join(result.tables)}")
    if result.errors:
        parts.append(f"{len(result.errors)} error(s)")
    return "; ".join(parts)
