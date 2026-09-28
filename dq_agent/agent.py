"""Gemini chat session with the data-quality tools."""
from dataclasses import dataclass

from google import genai
from google.genai import types

from dq_agent.config import METHODOLOGY_PATH, Settings
from dq_agent.report import split_questions
from dq_agent.tools import TOOLS, ToolContext

MAX_TOOL_CALLS = 25

ROLE = """\
You are a Data Quality Agent for a Coursera PostgreSQL database (schema public).
You check data quality ONLY by calling the provided tools. They run read-only,
aggregate SELECT queries. You cannot run arbitrary SQL and must not ask for raw rows.

Rules:
- Never invent numbers. Use only values returned by the tools.
- If a tool returns "error", explain it or fix the arguments (e.g. a table name).
- Keep each tool's report_section: "confirmed" = confirmed issue,
  "possible (assumption)" = assumption. Never upgrade an assumption.
- Mention whether each defect looks systematic or random (field "systematic").
- Answer in the user's language, briefly: the 3-5 most important findings,
  then which checks passed.
- If there are questions only the business can answer, end your answer with a
  line "QUESTIONS:" followed by one "- question" per line. Otherwise omit it.
- A Markdown report is saved automatically after each answer; do not paste it.
"""


@dataclass
class AgentReply:
    text: str
    questions: list[str]


def build_system_prompt(ctx: ToolContext) -> str:
    methodology = METHODOLOGY_PATH.read_text(encoding="utf-8") if METHODOLOGY_PATH.exists() else ""
    tables = ", ".join(ctx.schema.tables())
    return (
        f"{ROLE}\nAvailable tables: {tables}\n"
        f"Rules loaded: {ctx.rules.label}\n\n# Methodology\n\n{methodology}"
    )


class DataQualityAgent:
    def __init__(self, settings: Settings, ctx: ToolContext):
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._chat = self._client.chats.create(
            model=settings.gemini_model,
            config=types.GenerateContentConfig(
                system_instruction=build_system_prompt(ctx),
                tools=TOOLS,
                temperature=0,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    maximum_remote_calls=MAX_TOOL_CALLS,
                ),
            ),
        )

    def ask(self, request: str) -> AgentReply:
        response = self._chat.send_message(request)
        text = response.text or (
            "The agent did not produce a text answer (the tool-call limit may have "
            "been reached). Try a narrower request."
        )
        text, questions = split_questions(text)
        return AgentReply(text, questions)
