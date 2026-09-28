"""DataQualitySubagent: structured results for a calling agent (no DB, no Gemini)."""
import json
from decimal import Decimal

import pytest

from dq_agent import tools
from dq_agent.agent import AgentReply
from dq_agent.results import FAIL, CheckResult


def responder(query, params):
    """Clean data everywhere, except progress_pct = 104 in 277 rows."""
    if "information_schema" in query:
        return []
    if "BETWEEN" in query or " < " in query or " > " in query:   # range checks
        bad = params[:2] == [0.0, 100.0] and "progress_pct" in query
        if "GROUP BY" in query:
            return [{"value": Decimal("104"), "n": 277}] if bad else []
        return [{"total": 1000, "non_null": 1000, "violations": 277 if bad else 0,
                 "distinct_violating": 1 if bad else 0,
                 "min_value": Decimal("0"), "max_value": Decimal("104" if bad else "100")}]
    return [_zero_row(query)]


def _zero_row(query):
    """One aggregate row: every requested alias = 0, totals = 1000."""
    row = {"n": 1000, "total": 1000}
    for token in query.replace(",", " ").split():
        alias = token.strip('"')
        if alias.isidentifier() and alias not in row:
            row[alias] = 0
    return row


def test_check_tables_returns_json_ready_structured_result(make_subagent):
    sub, db = make_subagent(responder)
    result = sub.check_tables(["enrollments"], checks=["count", "out_of_range"])

    data = json.loads(json.dumps(result.to_dict()))
    assert data["status"] == "FAIL" and data["all_pass"] is False
    assert data["tables"] == ["enrollments"]
    assert data["facts"]["enrollments"]["rows"] == 1000
    [issue] = data["confirmed"]
    assert issue["target"] == "enrollments.progress_pct"
    assert issue["rows"] == 277 and issue["section"] == "confirmed"
    assert data["report_path"].endswith("report.md")
    assert db.queries   # every query went through FakeDB -> sql_guard


def test_assumption_violation_goes_to_possible(make_subagent):
    def respond(query, params):
        if "course_no" in query and ("<" in query or "BETWEEN" in query):
            if "GROUP BY" in query:
                return [{"value": 0, "n": 5}]
            return [{"total": 1000, "non_null": 1000, "violations": 5,
                     "distinct_violating": 1, "min_value": 0, "max_value": 9}]
        return responder(query, params)

    sub, _ = make_subagent(respond)
    result = sub.check_tables(["enrollments"], checks=["out_of_range"])
    assert [c["column"] for c in result.possible] == ["course_no"]
    assert all(c["basis"] != "rule" for c in result.possible)


def test_result_covers_only_current_run_store_stays_cumulative(make_subagent):
    sub, _ = make_subagent(responder)
    sub.check_tables(["enrollments"], checks=["out_of_range"])
    second = sub.check_tables(["payments"], checks=["out_of_range"])

    assert second.tables == ["payments"]
    assert second.status == "PASS" and not second.confirmed
    assert {r.table for r in sub.store.all()} == {"enrollments", "payments"}
    assert "enrollments" in sub.report_path.read_text()


def test_unknown_table_and_check_are_errors_not_exceptions(make_subagent):
    sub, db = make_subagent(responder)
    result = sub.check_tables(["nope; DROP TABLE users"])
    assert result.status == "ERROR" and "Unknown table" in result.errors[0]
    assert db.queries == []

    assert sub.check_tables(["users"], checks=["bogus"]).status == "ERROR"


def test_table_without_range_rules_is_skipped(make_subagent):
    sub, _ = make_subagent(responder)
    result = sub.check_tables(["users"], checks=["out_of_range"])
    assert result.status == "NO_CHECKS" and not result.errors
    assert "users: out_of_range" in result.skipped[0]


def test_run_uses_llm_and_returns_structured_result(make_subagent):
    class FakeAgent:
        def ask(self, request):
            tools._context().results.add(CheckResult(
                "out_of_range", "reviews", "stars", "stars in [1, 5]", "0", "3", FAIL, "rule",
                rows=3))
            return AgentReply("3 bad ratings.", ["Is 0 stars allowed?"])

    sub, _ = make_subagent(responder, agent=FakeAgent())
    result = sub.run("check reviews")
    assert result.status == "FAIL" and result.summary == "3 bad ratings."
    assert result.questions == ["Is 0 stars allowed?"]
    assert result.confirmed[0]["target"] == "reviews.stars"


def test_run_errors_are_redacted_results(make_subagent):
    class BrokenAgent:
        def ask(self, request):
            raise RuntimeError("bad key key123, pwd s3cret")

    sub, _ = make_subagent(responder, agent=BrokenAgent())
    result = sub.run("x")
    assert result.status == "ERROR"
    assert "key123" not in result.summary and "s3cret" not in result.summary


def test_run_without_llm_is_an_error(make_subagent):
    sub, _ = make_subagent(responder, agent=None)
    assert sub.run("x").status == "ERROR"


def test_nested_use_restores_outer_context(make_subagent):
    outer, _ = make_subagent(responder)
    inner, _ = make_subagent(responder)
    with tools.use(outer.ctx):
        # An orchestrator's tool call invoking the subagent in the same thread.
        inner.check_tables(["payments"], checks=["count"])
        assert tools._context() is outer.ctx
    assert tools._ctx is None
