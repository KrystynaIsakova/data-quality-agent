"""Dashboard (presentation layer) with a fake Orchestrator: no DB, no Gemini."""
import ast

import pytest
from streamlit.testing.v1 import AppTest

from dq_agent import kpis
from dq_agent.config import PROJECT_ROOT
from dq_agent.orchestrator import Orchestrator, OrchestratorReply
from dq_agent.subagent import AgentError, QualityResult, StartupError

DASHBOARD = str(PROJECT_ROOT / "dashboard.py")

PROGRESS_ISSUE = {"severity": "confirmed", "table": "enrollments", "column": "progress_pct",
                  "check": "out_of_range", "rule": "progress_pct in [0, 100]", "actual": "277 violations",
                  "rows": 277, "share": 0.0029, "systematic": "systematic", "note": None}
CHECK = {"check": "out_of_range", "table": "enrollments", "column": "progress_pct",
         "target": "enrollments.progress_pct", "rule": "progress_pct in [0, 100]",
         "actual": "277 violations", "systematic": "systematic", "note": ""}


def kpi(metric, value, n, unit, by=None, rows=None):
    base = {"metric": metric, "unit": unit, "by": by, "table": "enrollments", "definition": "",
            "calculation": "SQL", "description": ""}
    return {**base, "rows": rows, "truncated": False} if by else {**base, "value": value, "n": n}


class FakeOrchestrator:
    def __init__(self):
        self.layer = kpis.load_semantic_layer()
        self.calls, self.asked, self.closed = [], [], False
        self.ask_error = None

    def kpi_report(self, metric, by=None):
        self.calls.append((metric, by))
        warnings = [PROGRESS_ISSUE] if metric == "average_progress" else []
        status = "FAIL" if warnings else "PASS"
        values = {"total_enrollments": (94705, "count"), "completion_rate": (5.19, "%"),
                  "average_progress": (21.4, "%")}
        value, unit = values[metric]
        if by:
            rows = [{by: f"/specializations/s{i}", "value": 10.0 - i, "n": 600 + i} for i in range(5)]
            rows.append({by: "/specializations/tiny", "value": 77.27, "n": 22})
            result = kpi(metric, None, None, unit, by, rows)
        else:
            result = kpi(metric, value, 94705, unit)
        return {"kpi": result, "required_data": {"enrollments": []},
                "data_quality": {"status": status, "warnings": warnings, "errors": []}}

    def data_health(self, tables):
        return {t: QualityResult(status="FAIL" if t == "enrollments" else "PASS",
                                 confirmed=[CHECK] if t == "enrollments" else [],
                                 passed=[{**CHECK, "target": f"{t}.id"}] * 3)
                for t in tables}

    def ask(self, question):
        self.asked.append(question)
        if self.ask_error:
            raise self.ask_error
        return OrchestratorReply(
            f"Answer to: {question}", [kpi("completion_rate", 5.19, 94705, "%")], [])

    def close(self):
        self.closed = True


@pytest.fixture
def fake(monkeypatch):
    orch = FakeOrchestrator()
    monkeypatch.setattr(Orchestrator, "create", classmethod(lambda cls, on_tool_call=print: orch))
    return orch


def markdown(at):
    return " ".join(m.value for m in at.markdown)


def test_kpis_and_headline_come_from_the_backend(fake):
    at = AppTest.from_file(DASHBOARD, default_timeout=15).run()
    assert not at.exception
    text = markdown(at)
    assert "94,705" in text and "5.19" in text and "21.4" in text
    assert "Only <em>5.19%</em> of 94,705 enrollments" in text
    assert "Verified" in text and "1 confirmed issue" in text       # per-KPI trust badges
    assert "277 records fail the rule" in text                     # caveat in the insight
    assert {("total_enrollments", None), ("completion_rate", None), ("average_progress", None)} <= set(fake.calls)
    assert ("completion_rate", "specialization") in fake.calls       # main chart


def test_reruns_reuse_cached_reports(fake):
    at = AppTest.from_file(DASHBOARD, default_timeout=15).run()
    first = len(fake.calls)
    at.run()
    assert len(fake.calls) == first


def test_data_health_summary(fake):
    at = AppTest.from_file(DASHBOARD, default_timeout=15).run()
    text = markdown(at)
    assert "6 / 7" in text                          # 3 + 3 passed, 1 confirmed (dim_course + enrollments)
    assert "enrollments.progress_pct" in text
    assert any("Data health · 1 issue" in b.label for b in at.button)


def test_ask_shows_answer_and_exact_values(fake):
    at = AppTest.from_file(DASHBOARD, default_timeout=15).run()
    at.text_input[0].input("what is the completion rate?")
    next(b for b in at.button if b.label == "Ask").click().run()
    assert not at.exception
    assert fake.asked == ["what is the completion rate?"]
    text = markdown(at)
    assert "Answer to: what is the completion rate?" in text
    assert 'class="cp-big">5.19<small>%</small>' in text


def test_suggestion_button_asks(fake):
    at = AppTest.from_file(DASHBOARD, default_timeout=15).run()
    next(b for b in at.button if b.label == "What is the average progress?").click().run()
    assert fake.asked == ["What is the average progress?"]


def test_gemini_busy_is_friendly(fake):
    fake.ask_error = AgentError("429 RESOURCE_EXHAUSTED")
    at = AppTest.from_file(DASHBOARD, default_timeout=15).run()
    at.text_input[0].input("x")
    next(b for b in at.button if b.label == "Ask").click().run()
    assert any("Gemini is busy" in e.value for e in at.error)


def test_startup_error_is_shown(monkeypatch):
    def fail(cls, on_tool_call=print):
        raise StartupError("Configuration error: Missing environment variables: DATABASE_URL.")
    monkeypatch.setattr(Orchestrator, "create", classmethod(fail))
    at = AppTest.from_file(DASHBOARD, default_timeout=15).run()
    assert "Missing environment variables" in at.error[0].value


def test_dashboard_uses_only_the_orchestrator():
    """Presentation layer: no direct KPI, SQL or data-quality code."""
    tree = ast.parse(open(DASHBOARD, encoding="utf-8").read())
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    project = {m for m in modules if m and m.startswith("dq_agent")}
    assert project == {"dq_agent.orchestrator", "dq_agent.subagent"}
    source = open(DASHBOARD, encoding="utf-8").read()
    for forbidden in ("run_select", "calculate_kpi", "check_tables", "SELECT "):
        assert forbidden not in source
