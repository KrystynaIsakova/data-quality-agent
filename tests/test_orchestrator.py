"""Analytics Orchestrator: KPI flow with data-quality warnings (no DB, no Gemini)."""
import re
from decimal import Decimal

import pytest

from dq_agent.orchestrator import ROWS_FOR_MODEL, Orchestrator
from dq_agent.subagent import AgentError

from test_subagent import responder as dq_responder

# enrollments column order in SCHEMA_SNAPSHOT: completed_at is c7 in the missing-values query
COMPLETED_AT_EMPTY = "c7_empty"


def is_kpi(query):
    return re.search(r"\bAS f\b", query) is not None      # kpis.py aliases the fact table f


def responder(query, params):
    """KPI queries -> fixed values; enrollments.completed_at has 57 empty strings;
    progress_pct has 277 out-of-range values (from test_subagent's responder)."""
    if is_kpi(query):
        if "AS dim" in query:
            return [{"dim": f"s{i}", "value": Decimal("40.5"), "n": 10} for i in range(40)]
        return [{"value": Decimal("31.4159"), "n": 1000}]
    rows = dq_responder(query, params)
    if COMPLETED_AT_EMPTY in query:
        rows[0][COMPLETED_AT_EMPTY] = 57
    return rows


class FakeChat:
    """Plays the model: runs a scripted sequence of tool calls."""

    def __init__(self, script=None, error=None):
        self.script, self.error, self.tools = script, error, {}

    def send_message(self, question):
        if self.error:
            raise self.error
        outputs = [self.tools[name](**kwargs) for name, kwargs in self.script]
        self.script = []                          # one scripted turn only
        return type("Response", (), {"text": f"answer to {question!r}: {outputs!r:.40}"})


@pytest.fixture
def make_orchestrator(make_subagent):
    def factory(script=(), error=None, respond=responder):
        sub, db = make_subagent(respond)
        chat = FakeChat(list(script), error)
        orch = Orchestrator(sub, chat=chat, on_tool_call=lambda line: None)
        chat.tools = {f.__name__: f for f in orch.tools}
        return orch, db
    return factory


def tool(orch, name):
    return {f.__name__: f for f in orch.tools}[name]


def test_get_kpi_validates_before_calculating(make_orchestrator):
    orch, db = make_orchestrator()
    out = tool(orch, "get_kpi")("completion_rate")

    assert out["kpi"]["value"] == 31.42 and out["kpi"]["unit"] == "%"
    assert "completed_at IS NOT NULL" in out["kpi"]["definition"]
    assert out["required_data"] == {"enrollments": ["completed_at", "enrollment_id"]}
    kpi_index = next(i for i, (q, _) in enumerate(db.queries) if is_kpi(q))
    assert kpi_index == len(db.queries) - 1       # every DQ query ran before the KPI


def test_warnings_are_filtered_to_the_kpi_columns(make_orchestrator):
    orch, _ = make_orchestrator()
    dq = tool(orch, "get_kpi")("completion_rate")["data_quality"]
    targets = {(w["table"], w["column"]) for w in dq["warnings"]}
    assert ("enrollments", "completed_at") in targets        # 57 empty strings, relevant
    assert ("enrollments", "progress_pct") not in targets    # confirmed, but not used here
    assert dq["status"] == "WARN"                            # only 'possible' warnings

    dq = tool(orch, "get_kpi")("average_progress")["data_quality"]
    [confirmed] = [w for w in dq["warnings"] if w["severity"] == "confirmed"]
    assert confirmed["column"] == "progress_pct" and confirmed["rows"] == 277
    assert dq["status"] == "FAIL"


def test_dq_checks_are_cached_per_table(make_orchestrator):
    orch, db = make_orchestrator()
    tool(orch, "get_kpi")("total_enrollments")
    first = len(db.queries)
    tool(orch, "get_kpi")("average_progress")
    assert len(db.queries) == first + 1                      # only the KPI query


def test_by_specialization_also_checks_dim_course(make_orchestrator):
    orch, _ = make_orchestrator()
    out = tool(orch, "get_kpi")("completion_rate", by="specialization")
    assert out["required_data"] == {
        "enrollments": ["completed_at", "course_url", "enrollment_id"],
        "dim_course": ["course_url", "specialization_url"],
    }
    assert out["data_quality"]["checked_tables"] == ["dim_course", "enrollments"]
    # long breakdowns are trimmed for the model but complete in kpi_results
    assert len(out["kpi"]["rows"]) == ROWS_FOR_MODEL and out["kpi"]["rows_total"] == 40
    assert len(orch._kpi_results[0]["rows"]) == 40


def test_by_course_needs_no_dimension_table(make_orchestrator):
    orch, _ = make_orchestrator()
    out = tool(orch, "get_kpi")("total_enrollments", by="course")
    assert list(out["required_data"]) == ["enrollments"]


def test_unknown_metric_or_dimension_returns_error_without_queries(make_orchestrator):
    orch, db = make_orchestrator()
    assert "Unknown metric" in tool(orch, "get_kpi")("revenue")["error"]
    assert "Unknown dimension" in tool(orch, "get_kpi")("completion_rate", by="country")["error"]
    assert db.queries == []


def test_check_data_quality_tool(make_orchestrator):
    orch, _ = make_orchestrator()
    out = tool(orch, "check_data_quality")(["enrollments"])["enrollments"]
    assert out["status"] == "FAIL"
    assert any(w["column"] == "progress_pct" for w in out["warnings"])


def test_ask_returns_exact_tool_values_and_warnings(make_orchestrator):
    orch, _ = make_orchestrator(script=[("list_kpis", {}),
                                        ("get_kpi", {"metric": "completion_rate"})])
    reply = orch.ask("what is the completion rate?")
    assert [k["value"] for k in reply.kpi_results] == [31.42]
    assert [w["column"] for w in reply.warnings] == ["completed_at"]

    reply = orch.ask("hello")           # results do not leak into the next answer
    assert reply.kpi_results == [] and reply.warnings == []


def test_ask_errors_are_redacted(make_orchestrator):
    orch, _ = make_orchestrator(error=RuntimeError("bad key key123, pwd s3cret"))
    with pytest.raises(AgentError) as info:
        orch.ask("x")
    assert "key123" not in str(info.value) and "s3cret" not in str(info.value)


def test_database_errors_become_tool_errors(make_orchestrator):
    import psycopg

    def broken(query, params):
        raise psycopg.OperationalError("connection to postgresql://u:s3cret@h/db failed")

    orch, _ = make_orchestrator(respond=broken)
    out = tool(orch, "get_kpi")("total_enrollments")
    assert "database error" in out["error"] and "s3cret" not in out["error"]


def test_tools_have_gemini_declarations(make_orchestrator):
    from google.genai import types
    orch, _ = make_orchestrator()
    for func in orch.tools:
        decl = types.FunctionDeclaration.from_callable_with_api_option(callable=func)
        assert decl.name == func.__name__ and decl.description
    get_kpi = types.FunctionDeclaration.from_callable_with_api_option(callable=orch.tools[1])
    assert get_kpi.parameters.required == ["metric"]


def test_kpi_report_runs_pipeline_without_llm_or_trimming(make_orchestrator):
    orch, db = make_orchestrator(error=RuntimeError("Gemini must not be called"))
    out = orch.kpi_report("completion_rate", by="specialization")
    assert len(out["kpi"]["rows"]) == 40                    # full rows, not ROWS_FOR_MODEL
    assert out["data_quality"]["checked_tables"] == ["dim_course", "enrollments"]
    assert orch._kpi_results == [] and orch._warnings == []  # does not leak into the next ask()
    assert "Unknown metric" in orch.kpi_report("revenue")["error"]


def test_data_health_shares_the_session_cache(make_orchestrator):
    orch, db = make_orchestrator()
    health = orch.data_health(["enrollments"])
    assert health["enrollments"].status == "FAIL"
    before = len(db.queries)
    orch.kpi_report("total_enrollments")
    assert len(db.queries) == before + 1                     # checks reused, only the KPI query ran
