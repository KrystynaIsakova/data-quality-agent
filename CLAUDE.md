# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Data Quality Agent**: a standalone Python app with a Streamlit web UI and a terminal UI. The user types a request, Gemini (`google-genai`, automatic function calling) picks Python tools, the tools run read-only aggregate SELECTs against PostgreSQL (`DATABASE_URL` from `.env`), and the session's results are rendered to `reports/data_quality_report.md`.

## Commands

```bash
source .venv/bin/activate          # venv lives in the project root
pip install -r requirements.txt
streamlit run app.py               # web UI (needs .env; see .env.example)
python main.py                     # terminal UI
pytest                             # all tests; no DB or Gemini needed
pytest tests/test_tools.py::test_duplicates_four_slices   # single test
```

## Division of responsibilities (important)

- **Claude Code only develops the application.** Do not run data-quality checks, profile tables or write validation reports yourself, and do not run `python main.py` or `streamlit run app.py` against the real DB without the user's explicit permission. Checking data is the agent's job.
- The `course-db` MCP server (`mcp__course-db__query`) is for **schema exploration only** (`information_schema`). The app itself never uses MCP; it connects via `DATABASE_URL`.
- `.claude/agents/data-quality-agent.md` and `.claude/skills/db-quality-check/` are disabled leftovers of an earlier approach. `db-quality-check/SKILL.md` stays as a reference for SQL check recipes not yet ported (formats of text dates, logical contradictions, cross-table orphans).

## Architecture

Both UIs (`main.py` terminal, `app.py` Streamlit) are thin shells over `dq_agent/session.py`: `start_session()` loads settings/rules, connects, loads the schema and opens the Gemini chat; `Session.ask()` runs a request, records it and rewrites the report. Put shared behaviour there, not in a UI.

Request flow: `Session.ask` → `agent.py` (Gemini chat, `TOOLS`) → a tool in `tools.py` → `schema.py` whitelist → SQL built with `psycopg.sql.Identifier` + `%s` params → `db.run_select` → `sql_guard.assert_safe_select` → read-only connection → `CheckResult`s into `results.ResultStore` → `report.write` after every answer.

Non-obvious points:
- **Tools receive context through a module global.** Gemini needs plain functions, so `tools.configure(ToolContext(...))` injects `run_select`, schema, rules, the result store and `on_tool_call` (tool-call log: `print` in the terminal, a per-tab list in Streamlit). `Session.ask` re-configures it under a lock on every request, because Streamlit keeps one `Session` per browser tab in `st.session_state`. Tests swap in `FakeDB` (`tests/conftest.py`), which also runs every query through `sql_guard`; `tests/test_app.py` drives the UI with `streamlit.testing.AppTest` and a fake session.
- **Tool signatures and docstrings are the Gemini tool schema.** Keep type hints JSON-friendly (`str`, `float | None`, `list[str] | None`) and the `Args:` docstring up to date. `test_tools_have_gemini_declarations` guards this.
- **Report section placement is deterministic.** A FAIL goes to "confirmed" only when `basis == "rule"` (from `rules/*.yaml`); `assumption` / `heuristic` / `user` go to "possible". The LLM does not decide this. `ALL PASS` = at least one check and no FAIL.
- **Business questions** come from a `QUESTIONS:` block at the end of the model's answer, parsed by `report.split_questions`.
- **Tools return aggregates only.** No raw rows, no person IDs, no free text; `check_out_of_range` returns top violating *values* (measures) to judge systematic vs random.
- Re-running a check replaces its previous result (`CheckResult.key`); a check that no longer fails is removed with `ResultStore.discard`.
- `tests/conftest.py::SCHEMA_SNAPSHOT` is a snapshot of `information_schema.columns`. Update it (via MCP) if the DB schema changes; `test_repo_rules_match_schema` checks the YAML rules against it.

## Rules and methodology

- `specs/data-quality-agent.md` is the spec (requirements, tool contracts, classification, acceptance criteria, backlog). Keep it in sync when tools, rules format or report structure change.

- `rules/ranges.yaml` (numeric bounds + `basis`) and `rules/keys.yaml` (ID and natural keys) are the source of truth for checks.
- `docs/methodology.md` is the ported `validate-dataset` skill and is embedded verbatim into the system prompt, so edits there change agent behaviour.

## SQL safety

Three layers, keep all of them: `dq_agent/sql_guard.py` (ported from `.claude/hooks/select_only.py`, which separately guards Claude Code's own MCP queries), the read-only session with `statement_timeout` and `MAX_ROWS` in `db.py`, and identifier whitelisting in `schema.py`. Secrets are never printed; use `Settings.redact()` for any error text that may contain them.
