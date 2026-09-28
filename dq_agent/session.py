"""One agent session, shared by the terminal (main.py) and web (app.py) interfaces."""
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import psycopg

from dq_agent import report, tools
from dq_agent.agent import AgentReply, DataQualityAgent
from dq_agent.config import REPORT_PATH, RULES_DIR, ConfigError, Settings, load_settings
from dq_agent.db import Database
from dq_agent.results import ResultStore
from dq_agent.rules import Rules, RulesError
from dq_agent.schema import Schema

# Tools read their context from a module global, so requests from several
# sessions (e.g. two browser tabs) must not interleave.
_ASK_LOCK = threading.Lock()


class StartupError(RuntimeError):
    pass


class AgentError(RuntimeError):
    pass


@dataclass
class Session:
    settings: Settings
    db: Database
    schema: Schema
    rules: Rules
    rule_problems: list[str]
    store: ResultStore
    ctx: tools.ToolContext
    agent: DataQualityAgent
    report_path: Path | None = field(default=None)

    def ask(self, request: str) -> AgentReply:
        """Send a request to Gemini, record the answer and rewrite the report."""
        with _ASK_LOCK:
            tools.configure(self.ctx)
            try:
                reply = self.agent.ask(request)
            except Exception as error:  # Gemini / network / DB errors
                raise AgentError(self.settings.redact(str(error))) from error
        self.store.add_exchange(request, reply.text, reply.questions)
        if len(self.store) or self.store.tables:
            self.report_path = report.write(
                self.store, REPORT_PATH, self.rules.label, self.rule_problems)
        return reply

    def close(self) -> None:
        self.db.close()


def start_session(on_tool_call: Callable[[str], None] = print) -> Session:
    """Load settings and rules, connect to the DB, load the schema, open a Gemini chat."""
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
    return Session(
        settings=settings, db=db, schema=schema, rules=rules,
        rule_problems=rules.validate_against(schema), store=store, ctx=ctx,
        agent=DataQualityAgent(settings, ctx),
    )
