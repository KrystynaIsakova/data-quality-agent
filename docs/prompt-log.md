# Course Pulse: prompt and decision log

How the project grew from a single Data Quality Agent into an analytics product: the key prompts, whether each step was **planning** or **implementation**, and the decisions made along the way.

Type key:

- **Planning**: the assistant proposed an approach and waited for approval; no code changed.
- **Implementation**: code, tests or docs were written.
- **Testing**: the system was run and its behaviour reviewed.
- **Design**: visual or UX work.
- **Review**: checking an input the user provided.

## Summary

| # | Prompt (short) | Type | Key decision | Result |
|---|---|---|---|---|
| 1 | Check the course-db MCP connection | Exploration | MCP is used for schema only | Connected as `course_mcp_reader` to `coursera_capstone` |
| 2 | Refactor the Data Quality Agent into a subagent | Planning → Implementation | Two entry points: deterministic `check_tables()` and LLM `run()`; structured `QualityResult` | `dq_agent/subagent.py` |
| 3 | "Semantic layer is fixed" | Review | Leave the YAML as it is; flag integer division and the text `completed_at` | Two risks documented |
| 4 | Add KPI tools | Planning → Implementation | SQL per metric in code, a test guarding against YAML drift; decimal division; course/specialization breakdowns | `dq_agent/kpis.py` |
| 5 | Create an Orchestrator Agent | Planning → Implementation | Data-quality check runs **inside** `get_kpi` in Python; the LLM only picks the metric and explains | `dq_agent/orchestrator.py`, `analyst.py` |
| 6 | Test the complete system | Testing | No architecture changes; one bug fixed | 5 real questions answered; issues listed |
| 7 | Dashboard mockup | Design | Every number shows its trust status | Artifact, version 1 |
| 8 | "Less admin panel, more product" | Design | Insight and KPIs first, Ask prominent, data quality secondary | Artifact, version 2 |
| 9 | Implement the dashboard in Streamlit | Planning → Implementation | Presentation layer only; replace the time trend with breakdowns | `dashboard.py` |
| 10 | README launch guide + architecture SVG | Implementation | Quick start first; diagram of one question's path | `README.md`, `docs/architecture.svg` |
| 11 | Push changes | Implementation | Rebase onto the web upload instead of merging | Pushed `80f351c` |

## 1. MCP connection check

**Type:** Exploration

> Use the course-db MCP server to execute this read-only query: `SELECT current_user, current_database();` Explain the result in one sentence.

**Result:** the MCP server connects as the read-only role `course_mcp_reader` to the `coursera_capstone` database.

**Decision:** MCP stays a schema-exploration tool for Claude Code. The app itself connects through `DATABASE_URL`.

## 2. Data Quality Agent → Data Quality Subagent

**Type:** Planning (plan mode, approved) → Implementation

> Refactor the existing Data Quality Agent into a specialized Data Quality Subagent. Reuse the existing validation logic. Do not rewrite functionality that already works. The subagent should be callable by another agent. Return structured results that another agent can use. Keep database access read-only. Do not change semantic_layer.yaml. Before modifying any code: explain what you will reuse, explain what you will change, show the proposed workflow, wait for my approval.

**Planning decisions:**

- Reuse unchanged: all five check tools, `CheckResult` / `ResultStore`, the "confirmed vs possible" rule, the three SQL safety layers, the rules and the Gemini chat.
- New `DataQualitySubagent` with two entry points:
  - `check_tables(tables, checks)` is deterministic and uses no LLM. It is fast, reproducible and free, which suits an orchestrator that needs a quality gate.
  - `run(request)` is a natural-language request through Gemini.
- A JSON-ready `QualityResult` that contains **only this call's checks**, while the session store and the report stay cumulative.
- `tools.use(ctx)` with a reentrant lock, so an orchestrator tool can call the subagent without deadlocking.
- `Session` becomes a thin subclass, so both existing UIs keep working unchanged.

**Implementation:** `dq_agent/subagent.py`, small edits to `results.py`, `tools.py` and `session.py`, and `tests/test_subagent.py`.

**Also flagged:** in the original `semantic_layer.yaml`, the dimension keys (`CourseURL`) didn't match the real columns (`course_url`), and "completed enrollment" wasn't defined.

## 3. Semantic layer review

**Type:** Review

> semantic layer is fixed

**Result:** all tables and columns now match the schema, and completion is defined as `completed_at IS NOT NULL`.

**Decision:** the YAML stays as it is, and two risks were flagged:

- The YAML formula `COUNT / COUNT * 100` would use integer division in PostgreSQL and return 0.
- `completed_at` is stored as text, so empty strings would count as completed. The subagent's missing-values check detects this.

## 4. KPI tools

**Type:** Planning → Implementation

> Now add KPI tools to the project. Read metric definitions from semantic_layer.yaml. Create deterministic Python functions that calculate these KPIs using the database. The LLM must not calculate KPI values itself. Keep KPI tools separate from agent logic. First show me which KPI tools you plan to create and which tables/columns each one will use. Wait for my approval before implementing.

**Proposed:** `total_enrollments`, `average_progress`, `completion_rate`, plus `calculate_kpi(name)` and `list_kpis()`. Two questions went to the user.

**User decisions:**

> 1. Do not make integer decision
> 2. breakdown the course specialisation yes

**Resulting design:**

- `* 100.0` decimal division. This implements the YAML formula; the definition itself is unchanged.
- An optional `by="course"` or `by="specialization"` argument.
- By specialization joins **distinct** `(course_url, specialization_url)` pairs from `dim_course`, so duplicate rows there can't inflate counts.
- The YAML `calculation` text is never pasted into SQL. Each metric's SQL lives in `kpis.METRICS`, and a test fails if it drifts from the YAML or the schema.

**Implementation:** `dq_agent/kpis.py` and `tests/test_kpis.py`.

## 5. Orchestrator Agent

**Type:** Planning → Implementation

> Create a simple Orchestrator Agent using Gemini. For KPI questions it should: understand which metric the user is asking about, use the definition from semantic_layer.yaml, identify the required data, use the Data Quality Subagent to validate relevant data, call the appropriate KPI tool, return the KPI result together with relevant data quality warnings. The LLM must never calculate KPI values itself. Keep the orchestration simple. Before implementing: show the proposed flow for one example question.

**Key design decision:** the data-quality check runs **inside** the `get_kpi` tool, in Python. Gemini only picks the metric and dimension and writes the answer, so it can't skip validation or compute numbers.

**Other decisions:**

- Warnings are filtered to the columns the KPI actually reads. For example, a `progress_pct` problem doesn't warn on `completion_rate`.
- Checks are cached per table for the session.
- `OrchestratorReply.kpi_results` carries the exact tool outputs, so the UI never has to trust numbers in the model's text.
- Questions about metrics that aren't defined get a refusal, not an improvised answer.

**Approval:**

> Implement orchestration

**Implementation:** `dq_agent/orchestrator.py`, `analyst.py` and `tests/test_orchestrator.py`.

## 6. End-to-end test

**Type:** Testing

> Let's test the complete system before building a UI. Run several analytical questions that use different KPI tools. For each question show: metric identified, semantic layer definition used, Data Quality checks performed, KPI tool called, final answer. Do not modify the architecture yet.

**Bug found and fixed:** the Gemini client was garbage-collected, which closed the chat connection. One-line fix; no architecture change.

**Results (real database and Gemini):**

| Question | Metric | Value | Data quality |
|---|---|---|---|
| Total enrollments | `total_enrollments` | 94,705 | no relevant issues |
| Average progress by course | `average_progress` by course | per course | confirmed: 277 rows = 104% |
| Completion rate by specialization | `completion_rate` by specialization | 723 groups | no relevant issues |
| Completion rate (asked in Ukrainian) | `completion_rate` | 5.19% | no relevant issues |
| Total revenue | none | refused | none |

**Issues recorded for later:**

- The model made up readable course names.
- It re-ranked a partial breakdown and presented it as the top list.
- `MAX_ROWS = 1000` cuts off the course breakdown (2,912 courses).
- Gemini's free tier allows 5 requests per minute, and there's no retry.

## 7–8. Dashboard design

**Type:** Design

> Create an interactive dashboard mockup for a Data Analytics Agent. The user should immediately understand: 1. What is happening with the business? 2. Can I trust these numbers? 3. Can I ask a follow-up question? Use realistic mock data. Focus only on UX and visual design.

**Version 1:** KPI cards with trust badges, a cohort trend chart, a checks list and an analyst panel.

> Make the dashboard less like an admin panel and more like a modern analytics product. Make KPIs and the main business insight the primary focus. Data Quality should be visible but secondary. Make "Ask your data" prominent. Reduce visual clutter.

**Version 2 decisions:**

- The page opens with a headline insight.
- Three large KPIs without card frames.
- One clean chart next to a tinted "Ask your data" panel with the input at the top.
- Data quality shrinks to small per-KPI marks, a header pill, a compact section and a side panel.

## 9. Streamlit dashboard

**Type:** Planning → Implementation

> Implement this dashboard in Streamlit and connect it to our existing analytics system. KPI cards → existing KPI tools. Data Quality status → Data Quality Subagent. Analytical questions → Orchestrator. Metric definitions → semantic_layer.yaml. Do not duplicate KPI calculations or Data Quality logic inside Streamlit. Streamlit should only be the presentation layer. Before writing code: explain how each UI component maps to the backend, list the files you will modify, wait for my approval.

**Planning decisions:**

- KPI cards and the chart use a new `Orchestrator.kpi_report()`: the `get_kpi` steps without Gemini, so the page doesn't spend Gemini quota on load.
- Data health uses `Orchestrator.data_health()`, sharing the same check cache.
- The headline is a sentence template filled with tool values; there's no LLM on page load.
- The mockup's monthly trend, "vs last year" changes and sparklines aren't supported. The semantic layer has no time dimension and `enrolled_at` is text, so they were replaced with course and specialization breakdowns.
- The dashboard is a new `dashboard.py`; `app.py` stays unchanged.

**Approval:**

> Implement the approved dashboard.

**Implementation:**

- `dashboard.py`, `.streamlit/config.toml` and `tests/test_dashboard.py`.
- A test fails if the dashboard ever imports anything except the orchestrator and subagent, or contains SQL.
- Previewing with fake data exposed a CSS rule that broke Streamlit's icons; it was fixed.

## 10. Documentation

**Type:** Implementation

> update readme explaining how to launch the product

**Result:** a quick start (prerequisites → install → `.env` → tests → `streamlit run dashboard.py`), a guide to what's on screen, the other ways to run the system, and troubleshooting.

> add svg image to the readme to visualise how our agent work

**Result:** `docs/architecture.svg` shows one question's path: UI → Orchestrator (Gemini) → the `get_kpi` steps (definition → required data → Data Quality Subagent → KPI tools) → read-only database access. It is coloured by role and supports dark mode.

## 11. Publishing

**Type:** Implementation

> push changes

**Issue 1:** the SSH key wasn't loaded in the agent. The commit was made locally, and the user loaded the key.

> there is a conflict

**Issue 2:** a web upload on GitHub (`weekly_activity_clean.csv`) had diverged from the local commit.

**Decision:** rebase the local commit onto the upload rather than merge. The two commits touched different files, so this kept a linear history. Pushed as `80f351c`.

## Principles that held throughout

- **Plan before code.** Every structural change started with a proposal and an explicit approval.
- **The LLM never calculates.** Numbers come from SQL; Gemini picks tools and explains.
- **Validate before calculating.** Every KPI is checked by the Data Quality Subagent first, and the answer carries the relevant warnings.
- **Approved definitions only.** `semantic_layer.yaml` is read, never modified. Undefined metrics are refused.
- **Read-only, three layers.** `sql_guard`, a read-only session and a schema whitelist protect every query.
- **Reuse, don't duplicate.** Each new layer (subagent → KPI tools → orchestrator → dashboard) calls the one below it.
