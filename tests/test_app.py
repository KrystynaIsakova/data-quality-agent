"""Streamlit UI tests with a fake session (no DB, no Gemini)."""
import pytest
from streamlit.testing.v1 import AppTest

import dq_agent.session as session_module
import dq_agent.subagent as subagent_module
from dq_agent import tools
from dq_agent.agent import AgentReply
from dq_agent.config import PROJECT_ROOT, Settings
from dq_agent.results import FAIL, CheckResult, ResultStore
from dq_agent.session import AgentError, Session, StartupError

APP = str(PROJECT_ROOT / "app.py")


class FakeAgent:
    def __init__(self, ctx):
        self.ctx = ctx

    def ask(self, request):
        self.ctx.on_tool_call("  🔧 check_out_of_range('enrollments')")
        self.ctx.results.add(CheckResult(
            "out_of_range", "enrollments", "progress_pct", "progress_pct in [0, 100]",
            "0", "277", FAIL, "rule", rows=277))
        return AgentReply(f"Знайдено 277 порушень ({request}).", ["Яка шкала?"])


class FakeDatabase:
    def close(self):
        pass


@pytest.fixture
def fake_start(schema, rules, tmp_path, monkeypatch):
    monkeypatch.setattr(subagent_module, "REPORT_PATH", tmp_path / "data_quality_report.md")

    def start(on_tool_call=print):
        settings = Settings(gemini_api_key="k", database_url="postgresql://u:p@h/db")
        ctx = tools.ToolContext(lambda q, p=None: [], schema, rules, ResultStore(), on_tool_call)
        return Session(settings, FakeDatabase(), schema, rules, [], ctx.results, ctx, FakeAgent(ctx))

    monkeypatch.setattr(session_module, "start_session", start)


def test_chat_flow_shows_answer_tools_and_report(fake_start):
    at = AppTest.from_file(APP, default_timeout=10).run()
    assert not at.exception
    assert "Перевірок ще не було." in [c.value for c in at.sidebar.caption]

    at.chat_input[0].set_value("перевір enrollments").run()
    assert not at.exception
    markdown = " ".join(m.value for m in at.markdown)
    assert "Знайдено 277 порушень (перевір enrollments)." in markdown
    assert "Яка шкала?" in markdown
    assert "check_out_of_range('enrollments')" in at.code[0].value
    assert "# Data quality report" in markdown           # report tab
    assert [m.value for m in at.sidebar.metric][:2] == ["❌ Ні", "1"]


def test_example_button_sends_request(fake_start):
    at = AppTest.from_file(APP, default_timeout=10).run()
    at.button[0].click().run()
    assert any("Перевір таблицю enrollments" in m.value for m in at.markdown)


def test_agent_error_is_shown(fake_start, monkeypatch):
    def fail(self, request):
        raise AgentError("Gemini unavailable")
    monkeypatch.setattr(Session, "ask", fail)
    at = AppTest.from_file(APP, default_timeout=10).run()
    at.chat_input[0].set_value("x").run()
    assert any("Gemini unavailable" in e.value for e in at.error)


def test_startup_error_is_shown(monkeypatch):
    def fail(on_tool_call=print):
        raise StartupError("Configuration error: Missing environment variables: DATABASE_URL.")
    monkeypatch.setattr(session_module, "start_session", fail)
    at = AppTest.from_file(APP, default_timeout=10).run()
    assert "Missing environment variables" in at.error[0].value
    assert not at.chat_input
