# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Data Quality Agent**: a standalone Python app with a Streamlit web UI and a terminal UI. The user types a request, Gemini (`google-genai`, automatic function calling) picks Python tools, the tools run read-only aggregate SELECTs against PostgreSQL (`DATABASE_URL` from `.env`), and the session's results are rendered to `reports/data_quality_report.md`.

## Commands

```bash
source .venv/bin/activate          # venv lives in the project root
pip install -r requirements.txt
streamlit run app.py               # web UI (needs .env; see .env.example)
python main.py                     # terminal UI (data quality)
python analyst.py                  # terminal UI (Analytics Orchestrator: KPI questions)
streamlit run dashboard.py         # analytics dashboard (KPIs, chart, data health, Ask your data)
python -m pytest                   # all tests; no DB or Gemini needed (bare `pytest` cannot import dq_agent)
python -m pytest tests/test_tools.py::test_duplicates_four_slices   # single test
```

## Division of responsibilities (important)

- **Claude Code only develops the application.** Do not run data-quality checks, profile tables or write validation reports yourself, and do not run `python main.py` or `streamlit run app.py` against the real DB without the user's explicit permission. Checking data is the agent's job.
- The `course-db` MCP server (`mcp__course-db__query`) is for **schema exploration only** (`information_schema`). The app itself never uses MCP; it connects via `DATABASE_URL`.
- `.claude/agents/data-quality-agent.md` and `.claude/skills/db-quality-check/` are disabled leftovers of an earlier approach. `db-quality-check/SKILL.md` stays as a reference for SQL check recipes not yet ported (formats of text dates, logical contradictions, cross-table orphans).

## Architecture

The core is `dq_agent/subagent.py`: `DataQualitySubagent.create()` loads settings/rules, connects, loads the schema and (with `llm=True`) opens the Gemini chat. Other agents call it in-process: `check_tables(tables, checks)` runs the tools deterministically without an LLM, `run(request)` goes through Gemini. Both return a JSON-ready `QualityResult` covering only that call's checks (`ResultStore.begin_run()`), and both rewrite the cumulative report. `run()` returns `status="ERROR"` instead of raising.

Both UIs (`main.py` terminal, `app.py` Streamlit) are thin shells over `dq_agent/session.py`: `Session` subclasses the subagent, and `Session.ask()` returns `AgentReply` (text + questions) and keeps the full result in `last_result`. Put shared behaviour in the subagent, not in a UI.

The **Analytics Orchestrator** (`dq_agent/orchestrator.py`, entry `analyst.py`) is a separate Gemini chat on top of both. Its tools are closures over one `Orchestrator`: `list_kpis`, `get_kpi(metric, by)`, `check_data_quality(tables)`. The model only picks the metric and words the answer. `get_kpi` does everything else in Python, in this order: semantic-layer definition → required tables/columns → `subagent.check_tables` (cached per table per session, warnings filtered to the KPI's columns) → `kpis.calculate_kpi`. `ask()` returns `OrchestratorReply` whose `kpi_results` are the exact tool outputs, so the UI never has to trust numbers in the model's text. The subagent is created with `llm=False`, and its `db.run_select` is shared for the KPI queries.

`dashboard.py` is a **presentation layer only** over the Orchestrator (one per browser tab in `st.session_state`): KPI cards and the chart come from `Orchestrator.kpi_report(metric, by)` (the `get_kpi` pipeline without Gemini, full rows), data health comes from `Orchestrator.data_health(tables)` (same per-table cache), "Ask your data" goes to `Orchestrator.ask()`, and definitions come from `orch.layer`. It may sort, filter and format returned rows but never calculates; `tests/test_dashboard.py::test_dashboard_uses_only_the_orchestrator` enforces that it imports only `dq_agent.orchestrator` / `dq_agent.subagent`. KPI reports are cached per tab because Streamlit reruns the script on every click. No time trend yet: the semantic layer has no time dimension. `.streamlit/config.toml` holds the theme.

Request flow: `DataQualitySubagent._run` → `agent.py` (Gemini chat, `TOOLS`) → a tool in `tools.py` → `schema.py` whitelist → SQL built with `psycopg.sql.Identifier` + `%s` params → `db.run_select` → `sql_guard.assert_safe_select` → read-only connection → `CheckResult`s into `results.ResultStore` → `report.write` after every answer.

Non-obvious points:
- **Tools receive context through a module global.** Gemini needs plain functions, so `tools.configure(ToolContext(...))` injects `run_select`, schema, rules, the result store and `on_tool_call` (tool-call log: `print` in the terminal, a per-tab list in Streamlit). The subagent sets it with `tools.use(ctx)` (a reentrant lock that restores the previous context, so an orchestrator tool can nest a subagent call) on every request, because Streamlit keeps one `Session` per browser tab in `st.session_state`. Tests swap in `FakeDB` (`tests/conftest.py`), which also runs every query through `sql_guard`; `tests/test_app.py` drives the UI with `streamlit.testing.AppTest` and a fake session.
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

## Semantic Layer

Business metric definitions are stored in `semantic_layer.yaml`.

Always use these definitions when calculating or discussing KPIs.
Do not modify metric definitions unless explicitly requested.

KPIs are calculated by `dq_agent/kpis.py`, never by the LLM: `calculate_kpi(run_select, name, by=None)` (plus `total_enrollments` / `average_progress` / `completion_rate` wrappers, `list_kpis`). It is independent of the agents and uses the same read-only `run_select`. The YAML `calculation` text is never pasted into SQL; each metric's SQL lives in `kpis.METRICS`, and `tests/test_kpis.py` fails if it drifts from the YAML or the schema. When a metric is added to the YAML, add its entry to `METRICS`. `by="specialization"` joins distinct `(course_url, specialization_url)` pairs from `dim_course`.
