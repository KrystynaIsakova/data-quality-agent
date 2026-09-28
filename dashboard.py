"""Analytics dashboard: streamlit run dashboard.py

Presentation layer only. Every number comes from the Orchestrator:
- KPI cards and the chart   -> Orchestrator.kpi_report (KPI tools, checked by the DQ subagent)
- Data health               -> Orchestrator.data_health (Data Quality Subagent)
- Ask your data             -> Orchestrator.ask (Gemini picks tools, never computes)
- Metric definitions        -> semantic_layer.yaml via Orchestrator.layer (read-only)
This page formats, sorts and filters returned rows; it never calculates a KPI.
"""
from html import escape

import altair as alt
import pandas as pd
import streamlit as st

from dq_agent.orchestrator import Orchestrator
from dq_agent.subagent import AgentError, StartupError

KPI_ORDER = ["total_enrollments", "completion_rate", "average_progress"]
KPI_LABELS = {"total_enrollments": "Total enrollments", "completion_rate": "Completion rate",
              "average_progress": "Average progress"}
CHART_METRICS = {"Completion": "completion_rate", "Progress": "average_progress",
                 "Enrollments": "total_enrollments"}
CHART_DIMENSIONS = {"By specialization": "specialization", "By course": "course"}
MIN_GROUP_SIZE = 500          # hide tiny groups whose rates rest on a few learners
TOP_GROUPS = 12
SUGGESTIONS = ["Which specializations complete most?", "What is the average progress?",
               "Can I trust the completion rate?"]
ACCENT, INK_3, BAD, WARN, OK = "#0D6B73", "#7E8A85", "#C2412D", "#A96C12", "#2E7D4F"

st.set_page_config(page_title="Course Pulse", page_icon="📈", layout="wide")

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=Schibsted+Grotesk:wght@600;700&display=swap');
.stApp, .stMarkdown, .stButton button, .stTextInput input, [data-testid="stCaptionContainer"] { font-family: "IBM Plex Sans", "Segoe UI", system-ui, sans-serif; }
.block-container { padding-top: 1.4rem; max-width: 1280px; }
[data-testid="stHeader"] { display: none; }
h1, h2, h3, .cp-display { font-family: "Schibsted Grotesk", "Helvetica Neue", Arial, sans-serif !important; letter-spacing: -0.02em; }
.cp-brand { font-family: "Schibsted Grotesk", Arial, sans-serif; font-weight: 700; font-size: 19px; display: flex; gap: 9px; align-items: center; padding-top: 4px; }
.cp-brand i { width: 24px; height: 24px; border-radius: 7px; background: #0D6B73; display: inline-block; }
.cp-eyebrow { font-size: 12px; letter-spacing: .08em; text-transform: uppercase; color: #7E8A85; font-weight: 500; }
.cp-hero h1 { font-size: clamp(28px, 3.4vw, 42px); line-height: 1.1; font-weight: 700; margin: 6px 0 8px; padding: 0; text-wrap: balance; }
.cp-hero h1 em { font-style: normal; color: #0D6B73; }
.cp-hero p { font-size: 16px; color: #4A5752; max-width: 64ch; margin: 0; }
.cp-kpi { border-top: 1px solid #E0E6E3; padding: 18px 0 6px; }
.cp-kpi-label { display: flex; align-items: center; gap: 8px; font-size: 13px; color: #4A5752; font-weight: 500; }
.cp-kpi-value { font-family: "Schibsted Grotesk", Arial, sans-serif; font-weight: 700; font-size: clamp(38px, 4.4vw, 56px); line-height: 1.05; letter-spacing: -0.03em; margin-top: 6px; }
.cp-kpi-value small { font-size: .5em; color: #4A5752; margin-left: 2px; }
.cp-kpi-foot { font-size: 12.5px; color: #7E8A85; margin-top: 4px; }
.cp-def { color: #4A5752; }
.cp-trust { margin-left: auto; display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: #7E8A85; }
.cp-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex: none; }
.cp-q { margin-left: auto; width: fit-content; max-width: 90%; background: #0D6B73; color: #fff; border-radius: 14px 14px 4px 14px; padding: 8px 12px; font-size: 14px; }
.cp-big { font-family: "Schibsted Grotesk", Arial, sans-serif; font-size: 30px; font-weight: 700; letter-spacing: -0.03em; }
.cp-note { font-size: 13px; color: #4A5752; display: flex; gap: 8px; align-items: baseline; margin: 2px 0; }
.cp-health-score { font-family: "Schibsted Grotesk", Arial, sans-serif; font-size: 28px; font-weight: 700; }
.cp-panel-title { font-family: "Schibsted Grotesk", Arial, sans-serif; font-size: 18px; font-weight: 600; letter-spacing: -0.01em; margin: 2px 0 4px; }
.st-key-suggest button { justify-content: flex-start; text-align: left; background: #fff; border-color: #DDEDEC; color: #4A5752; }
.st-key-ask { background: #E9F3F2; border-radius: 16px; padding: 18px 18px 10px; border: 1px solid #DDEDEC; }
.st-key-ask h2 { font-size: 22px; margin: 0; padding: 0; }
.st-key-ask [data-testid="stFormSubmitButton"] button { background: #0D6B73; color: #fff; border: 0; }
</style>
"""


# ---------------------------------------------------------------- session

def get_orchestrator() -> Orchestrator:
    """One Orchestrator (one read-only DB connection, one Gemini chat) per browser tab."""
    if "orchestrator" not in st.session_state:
        tool_log: list[str] = []
        try:
            with st.spinner("Connecting to the database and running data checks…"):
                orchestrator = Orchestrator.create(on_tool_call=tool_log.append)
        except StartupError as error:
            st.error(str(error))
            st.info("Copy `.env.example` to `.env`, fill it in and reload the page.")
            st.stop()
        st.session_state.update(orchestrator=orchestrator, tool_log=tool_log,
                                reports={}, messages=[])
    return st.session_state.orchestrator


def report(orchestrator: Orchestrator, metric: str, by: str | None = None) -> dict:
    """KPI report from the backend, cached for this tab (Streamlit reruns on every click)."""
    key = (metric, by)
    if key not in st.session_state.reports:
        st.session_state.reports[key] = orchestrator.kpi_report(metric, by)
    return st.session_state.reports[key]


def reset_session() -> None:
    orchestrator = st.session_state.pop("orchestrator", None)
    if orchestrator is not None:
        orchestrator.close()
    for key in ("tool_log", "reports", "messages"):
        st.session_state.pop(key, None)


# ---------------------------------------------------------------- formatting

def fmt(value, unit: str) -> str:
    if value is None:
        return "—"
    if unit == "count":
        return f"{value:,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".") if unit == "%" else str(value)


def value_html(kpi: dict) -> str:
    unit = "<small>%</small>" if kpi["unit"] == "%" else ""
    return f"{fmt(kpi.get('value'), kpi['unit'])}{unit}"


def trust(dq: dict) -> tuple[str, str]:
    """(color, label) for a KPI's data_quality block."""
    confirmed = sum(w["severity"] == "confirmed" for w in dq["warnings"])
    possible = sum(w["severity"] == "possible" for w in dq["warnings"])
    if dq["status"] == "ERROR":
        return BAD, "Checks failed"
    if confirmed:
        return BAD, f"{confirmed} confirmed issue" + ("s" if confirmed > 1 else "")
    if possible:
        return WARN, f"{possible} caveat" + ("s" if possible > 1 else "")
    return OK, "Verified"


def dot(color: str) -> str:
    return f'<span class="cp-dot" style="background:{color}"></span>'


def warning_line(w: dict) -> str:
    color = BAD if w["severity"] == "confirmed" else WARN
    target = f"{w['table']}.{w['column']}" if w["column"] else w["table"]
    rows = f" · {w['rows']:,} rows" if w.get("rows") else ""
    return (f'<div class="cp-note">{dot(color)}<span><b>{escape(target)}</b>: '
            f'{escape(w["rule"])} — {escape(str(w["actual"]))}{rows}</span></div>')


def dq_tables(layer: dict) -> list[str]:
    """Tables the approved metrics and dimensions read (from the semantic layer)."""
    tables = {m["table"] for m in layer.get("metrics", {}).values()}
    tables |= {d["table"] for d in layer.get("dimensions", {}).values()}
    return sorted(tables)


# ---------------------------------------------------------------- data health dialog

@st.dialog("Data health", width="large")
def show_health(health: dict) -> None:
    for table, result in health.items():
        st.markdown(f"**`{table}`** · {len(result.passed)} passed, {len(result.possible)} possible, "
                    f"{len(result.confirmed)} confirmed")
        for severity, checks in (("confirmed", result.confirmed), ("possible", result.possible)):
            for c in checks:
                color = BAD if severity == "confirmed" else WARN
                note = f"<br><span style='color:{INK_3}'>{escape(c['systematic'] or c['note'] or '')}</span>" \
                    if (c["systematic"] or c["note"]) else ""
                st.markdown(f'<div class="cp-note">{dot(color)}<span><code>{escape(c["target"])}</code> '
                            f'{escape(c["rule"])}: {escape(str(c["actual"]))}{note}</span></div>',
                            unsafe_allow_html=True)
        with st.expander(f"Passed checks ({len(result.passed)})"):
            for c in result.passed:
                st.markdown(f"- `{c['target']}` {c['rule']}")
        for error in result.errors:
            st.warning(error)


# ---------------------------------------------------------------- page

st.markdown(CSS, unsafe_allow_html=True)
orch = get_orchestrator()
layer = orch.layer
definitions = layer.get("metrics", {})          # semantic_layer.yaml, read-only
health = orch.data_health(dq_tables(layer))
passed = sum(len(r.passed) for r in health.values())
confirmed = sum(len(r.confirmed) for r in health.values())
possible = sum(len(r.possible) for r in health.values())
issues = confirmed + possible

# header
brand, pill, refresh = st.columns([6, 2.2, 1.2], vertical_alignment="center")
brand.markdown('<div class="cp-brand"><i></i>Course Pulse</div>', unsafe_allow_html=True)
if pill.button(f"{'🔴' if confirmed else '🟡' if possible else '🟢'} Data health · "
               f"{issues} issue{'s' if issues != 1 else ''}", width="stretch"):
    show_health(health)
if refresh.button("Refresh data", width="stretch", help="Start a new session: re-run checks and KPIs"):
    reset_session()
    st.rerun()

# KPI reports (backend)
reports = {m: report(orch, m) for m in KPI_ORDER if m in definitions}
kpi = {m: r["kpi"] for m, r in reports.items() if "kpi" in r}

# hero: the headline is a template over tool values, no LLM
if {"total_enrollments", "completion_rate"} <= kpi.keys():
    headline = (f"Only <em>{fmt(kpi['completion_rate']['value'], '%')}%</em> of "
                f"{fmt(kpi['total_enrollments']['value'], 'count')} enrollments end in a completion.")
else:
    headline = "Your learning KPIs"
sub = []
if "average_progress" in kpi:
    sub.append(f"Average progress across enrollments is {fmt(kpi['average_progress']['value'], '%')}%.")
    bad = [w for w in reports["average_progress"]["data_quality"]["warnings"] if w["severity"] == "confirmed"]
    if bad:
        sub.append(f"Treat it with care: {bad[0]['rows']:,} records fail the rule "
                   f"“{escape(bad[0]['rule'])}”.")
st.markdown(f'<div class="cp-hero"><span class="cp-eyebrow">Key insight · all enrollments</span>'
            f'<h1>{headline}</h1><p>{" ".join(sub)}</p></div>', unsafe_allow_html=True)

# KPI cards
for column, metric in zip(st.columns(len(reports), gap="large"), reports):
    r = reports[metric]
    with column:
        if "error" in r:
            st.markdown(f'<div class="cp-kpi"><div class="cp-kpi-label">{KPI_LABELS[metric]}</div></div>',
                        unsafe_allow_html=True)
            st.warning(r["error"])
            continue
        color, label = trust(r["data_quality"])
        d = definitions[metric]
        definition = " ".join(str(d.get("definition") or d.get("calculation", "")).split())
        st.markdown(
            f'<div class="cp-kpi" title="{escape(definition)}">'
            f'<div class="cp-kpi-label">{KPI_LABELS[metric]}<span class="cp-trust">{dot(color)}{label}</span></div>'
            f'<div class="cp-kpi-value">{value_html(r["kpi"])}</div>'
            f'<div class="cp-kpi-foot">n = {r["kpi"]["n"]:,} · <span class="cp-def">{escape(definition)}</span></div>'
            f'</div>', unsafe_allow_html=True)

st.write("")
left, right = st.columns([1.55, 1], gap="large")

# main chart
with left, st.container(border=True):
    title = st.empty()
    pick_metric, pick_dim = st.columns([1.35, 1])
    metric_label = pick_metric.segmented_control("Metric", list(CHART_METRICS), default="Completion",
                                                 label_visibility="collapsed", key="chart_metric") or "Completion"
    dim_label = pick_dim.segmented_control("Dimension", list(CHART_DIMENSIONS), default="By specialization",
                                           label_visibility="collapsed", key="chart_dim") or "By specialization"
    metric, dim = CHART_METRICS[metric_label], CHART_DIMENSIONS[dim_label]
    title.markdown(f'<div class="cp-panel-title">{KPI_LABELS[metric]} {dim_label.lower()}</div>',
                   unsafe_allow_html=True)
    only_large = st.toggle(f"Only groups with {MIN_GROUP_SIZE}+ enrollments", value=True,
                           disabled=metric == "total_enrollments")
    r = report(orch, metric, dim)
    if "error" in r:
        st.warning(r["error"])
    else:
        rows = pd.DataFrame(r["kpi"]["rows"]).rename(columns={dim: "group"})
        rows["group"] = rows["group"].fillna("(not in dim_course)").str.rsplit("/", n=1).str[-1]
        if only_large and metric != "total_enrollments":
            rows = rows[rows["n"] >= MIN_GROUP_SIZE]
        rows = rows.dropna(subset=["value"]).nlargest(TOP_GROUPS, "value")
        unit = "%" if r["kpi"]["unit"] == "%" else ""
        bars = alt.Chart(rows).mark_bar(color=ACCENT, cornerRadiusEnd=4, height=14).encode(
            x=alt.X("value:Q", title=f"{KPI_LABELS[metric]}{' (%)' if unit else ''}"),
            y=alt.Y("group:N", sort="-x", title=None, axis=alt.Axis(labelLimit=260)),
            tooltip=[alt.Tooltip("group:N", title=dim.capitalize()),
                     alt.Tooltip("value:Q", title=KPI_LABELS[metric]), alt.Tooltip("n:Q", title="Enrollments", format=",")],
        )
        layers = [bars]
        overall = kpi.get(metric, {}).get("value")
        if unit and overall is not None:        # overall KPI from the backend, drawn as a reference line
            layers.append(alt.Chart(pd.DataFrame({"value": [overall]})).mark_rule(
                color=INK_3, strokeDash=[4, 4]).encode(x="value:Q"))
        st.altair_chart(alt.layer(*layers).properties(height=max(160, 30 * len(rows))), width="stretch")
        notes = [f"Top {len(rows)} of {len(r['kpi']['rows']):,} groups"
                 + (" (backend returned the first 1,000 only)" if r["kpi"]["truncated"] else "")]
        if unit and overall is not None:
            notes.append(f"dashed line: overall {fmt(overall, '%')}%")
        st.caption(" · ".join(notes))
        for w in r["data_quality"]["warnings"]:
            st.markdown(warning_line(w), unsafe_allow_html=True)

# ask your data
with right, st.container(key="ask"):
    st.markdown("## Ask your data")
    with st.form("ask_form", clear_on_submit=True, border=False):
        q_col, b_col = st.columns([4, 1.3], vertical_alignment="bottom")
        typed = q_col.text_input("Question", placeholder="e.g. Which specializations complete most?",
                                 label_visibility="collapsed")
        submitted = b_col.form_submit_button("Ask", width="stretch")
    clicked = None
    with st.container(key="suggest", gap="small"):
        for suggestion in SUGGESTIONS:
            if st.button(suggestion, key=f"suggest_{suggestion}", width="stretch"):
                clicked = suggestion
    question = (typed.strip() if submitted else "") or clicked

    if question:
        log = st.session_state.tool_log
        log.clear()
        with st.status("Finding the metric, checking data quality, calculating…", expanded=False) as status:
            try:
                reply = orch.ask(question)
                message = {"q": question, "text": reply.text, "kpis": reply.kpi_results, "warnings": reply.warnings}
                status.update(label="Done", state="complete")
            except AgentError as error:
                message = {"q": question, "error": str(error)}
                status.update(label="The analyst is unavailable", state="error")
        message["tools"] = [line.strip() for line in log]
        st.session_state.messages.append(message)

    with st.container(height=480, border=False):
        if not st.session_state.messages:
            st.caption("Ask about enrollments, progress or completion, overall or by course or specialization. "
                       "Every number is calculated with SQL after a data-quality check.")
        for m in reversed(st.session_state.messages):
            st.markdown(f'<div class="cp-q">{escape(m["q"])}</div>', unsafe_allow_html=True)
            if "error" in m:
                busy = "429" in m["error"] or "503" in m["error"]
                st.error("Gemini is busy right now. Wait a minute and ask again." if busy else m["error"])
                continue
            st.markdown(m["text"])
            for k in m["kpis"]:
                if k["by"] is None:
                    st.markdown(f'<div class="cp-big">{value_html(k)}</div>', unsafe_allow_html=True)
                else:
                    table = pd.DataFrame(k["rows"]).rename(columns={k["by"]: k["by"].capitalize(), "value": KPI_LABELS[k["metric"]], "n": "Enrollments"})
                    st.dataframe(table, hide_index=True, height=220, width="stretch")
            for w in m["warnings"]:
                st.markdown(warning_line(w), unsafe_allow_html=True)
            if m["kpis"] or m["tools"]:
                with st.expander("How this was calculated"):
                    for k in m["kpis"]:
                        st.markdown(f"- **`{k['metric']}`** on `{k['table']}`"
                                    + (f" by `{k['by']}`" if k["by"] else "")
                                    + f": {k['definition'] or k['calculation']}")
                    if m["tools"]:
                        st.code("\n".join(m["tools"]), language=None)
                    st.caption("Values come from SQL via the KPI tools. The AI only explains them.")

# data health (secondary)
with st.container(border=True):
    score, lines, action = st.columns([1, 2.4, 1], vertical_alignment="center")
    total = passed + issues
    score.markdown(f'<span class="cp-eyebrow">Data health</span><div class="cp-health-score">{passed} / {total}</div>'
                   f'<span style="color:{INK_3};font-size:13px">checks passed · {", ".join(health)}</span>',
                   unsafe_allow_html=True)
    worst = [(BAD, c) for r in health.values() for c in r.confirmed] + \
            [(WARN, c) for r in health.values() for c in r.possible]
    lines.markdown("".join(
        f'<div class="cp-note">{dot(color)}<span><code>{escape(c["target"])}</code> {escape(c["rule"])}</span></div>'
        for color, c in worst[:3]) or f'<div class="cp-note">{dot(OK)}All checks passed.</div>',
        unsafe_allow_html=True)
    if action.button("View all checks", width="stretch"):
        show_health(health)
