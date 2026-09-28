"""One agent session, shared by the terminal (main.py) and web (app.py) interfaces.

A thin chat-oriented wrapper over DataQualitySubagent: ask() returns text and
questions for display, the full structured result is kept in last_result.
"""
from dataclasses import dataclass, field
from typing import Callable

from dq_agent.agent import AgentReply
from dq_agent.subagent import AgentError, DataQualitySubagent, QualityResult, StartupError

__all__ = ["AgentError", "Session", "StartupError", "start_session"]


@dataclass
class Session(DataQualitySubagent):
    last_result: QualityResult | None = field(default=None)

    def ask(self, request: str) -> AgentReply:
        """Send a request to Gemini, record the answer and rewrite the report."""
        self.last_result = self._run(request)
        return AgentReply(self.last_result.summary, self.last_result.questions)


def start_session(on_tool_call: Callable[[str], None] = print) -> Session:
    """Load settings and rules, connect to the DB, load the schema, open a Gemini chat."""
    return Session.create(on_tool_call)
