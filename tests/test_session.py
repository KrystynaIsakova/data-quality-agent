import pytest

from dq_agent import subagent as subagent_module
from dq_agent import tools
from dq_agent.agent import AgentReply
from dq_agent.config import Settings
from dq_agent.results import PASS, CheckResult, ResultStore
from dq_agent.session import AgentError, Session


class FakeAgent:
    def __init__(self, reply=None, error=None, on_ask=None):
        self.reply, self.error, self.on_ask = reply, error, on_ask

    def ask(self, request):
        if self.on_ask:
            self.on_ask()
        if self.error:
            raise self.error
        return self.reply


class FakeDatabase:
    closed = False

    def close(self):
        self.closed = True


def make_session(schema, rules, agent):
    settings = Settings(gemini_api_key="key123", database_url="postgresql://u:s3cret@h/db")
    store = ResultStore()
    ctx = tools.ToolContext(lambda q, p=None: [], schema, rules, store)
    return Session(settings, FakeDatabase(), schema, rules, [], store, ctx, agent)


@pytest.fixture
def report_path(tmp_path, monkeypatch):
    path = tmp_path / "data_quality_report.md"
    monkeypatch.setattr(subagent_module, "REPORT_PATH", path)
    return path


def test_ask_records_exchange_and_writes_report(schema, rules, report_path):
    s = make_session(schema, rules, None)

    def run_check():
        # A tool call during the request: context must belong to this session.
        assert tools._context() is s.ctx
        s.store.add(CheckResult("count", "users", None, "r", "e", "a", PASS, "rule"))

    s.agent = FakeAgent(AgentReply("Готово.", ["Питання?"]), on_ask=run_check)
    reply = s.ask("перевір users")
    assert reply.text == "Готово."
    assert s.report_path == report_path
    text = report_path.read_text()
    assert "### Запит: перевір users" in text and "- Питання?" in text


def test_no_report_without_checks(schema, rules, report_path):
    s = make_session(schema, rules, FakeAgent(AgentReply("Привіт!", [])))
    s.ask("привіт")
    assert s.report_path is None and not report_path.exists()


def test_agent_errors_are_redacted(schema, rules, report_path):
    s = make_session(schema, rules, FakeAgent(error=RuntimeError("bad key key123, pwd s3cret")))
    with pytest.raises(AgentError) as info:
        s.ask("x")
    assert "key123" not in str(info.value) and "s3cret" not in str(info.value)


def test_tool_calls_go_to_session_callback(setup_tools):
    lines = []
    setup_tools(lambda q, p: [{"n": 1}])
    tools._context().on_tool_call = lines.append
    tools.count_rows("users")
    assert lines == ["  🔧 count_rows('users')"]
