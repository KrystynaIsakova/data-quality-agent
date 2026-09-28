"""Web interface: streamlit run app.py"""
import streamlit as st

from dq_agent.session import AgentError, StartupError, start_session

EXAMPLES = [
    "Перевір таблицю enrollments",
    "Чи є дублікати в weekly_activity?",
    "Які значення поза діапазоном у payments?",
    "Перевір пропуски в таблиці users",
]

st.set_page_config(page_title="Data Quality Agent", page_icon="🔎", layout="wide")


def get_session():
    """One agent session per browser tab, kept in st.session_state."""
    if "session" not in st.session_state:
        tool_log: list[str] = []
        try:
            with st.spinner("Підключення до бази та Gemini…"):
                session = start_session(on_tool_call=tool_log.append)
        except StartupError as error:
            st.error(str(error))
            st.info("Скопіюйте `.env.example` у `.env`, заповніть його і перезавантажте сторінку.")
            st.stop()
        st.session_state.session = session
        st.session_state.tool_log = tool_log
        st.session_state.messages = []
    return st.session_state.session


def render_message(message: dict) -> None:
    with st.chat_message(message["role"]):
        if message.get("error"):
            st.error(message["content"])
        else:
            st.markdown(message["content"])
        if message.get("questions"):
            st.markdown("**Питання до бізнесу**\n\n" + "\n".join(f"- {q}" for q in message["questions"]))
        if message.get("tool_calls"):
            with st.expander(f"Викликані тули ({len(message['tool_calls'])})"):
                st.code("\n".join(message["tool_calls"]), language=None)


def ask(session, request: str) -> dict:
    tool_log = st.session_state.tool_log
    tool_log.clear()
    try:
        reply = session.ask(request)
        message = {"role": "assistant", "content": reply.text, "questions": reply.questions}
    except AgentError as error:
        message = {"role": "assistant", "content": f"Помилка: {error}", "error": True}
    message["tool_calls"] = [line.strip().removeprefix("🔧").strip() for line in tool_log]
    return message


session = get_session()
prompt = st.chat_input("Опишіть, що перевірити, наприклад: перевір дублікати в enrollments")

st.title("🔎 Data Quality Agent")
st.caption("Gemini + PostgreSQL · лише агрегатні SELECT-запити · звіт у reports/data_quality_report.md")

chat_tab, report_tab = st.tabs(["Чат", "Звіт"])

with chat_tab:
    clicked = None
    if not st.session_state.messages:
        st.markdown("**Приклади запитів:**")
        for column, example in zip(st.columns(len(EXAMPLES)), EXAMPLES):
            if column.button(example, width="stretch"):
                clicked = example

    for message in st.session_state.messages:
        render_message(message)

    request = prompt or clicked
    if request:
        user_message = {"role": "user", "content": request}
        st.session_state.messages.append(user_message)
        render_message(user_message)
        with st.spinner("Агент перевіряє дані…"):
            answer = ask(session, request)
        st.session_state.messages.append(answer)
        render_message(answer)

with report_tab:
    if session.report_path and session.report_path.exists():
        st.markdown(session.report_path.read_text(encoding="utf-8"))
    else:
        st.info("Звіт з'явиться після першої перевірки.")

with st.sidebar:
    st.header("Сесія")
    store = session.store
    if len(store):
        st.metric("ALL PASS", "✅ Так" if store.all_pass else "❌ Ні")
        col1, col2 = st.columns(2)
        col1.metric("Підтверджені", len(store.confirmed()))
        col2.metric("Припущення", len(store.possible()))
        st.caption(f"Перевірок виконано: {len(store)}")
    else:
        st.caption("Перевірок ще не було.")

    if session.report_path and session.report_path.exists():
        st.download_button(
            "⬇️ Завантажити звіт",
            data=session.report_path.read_text(encoding="utf-8"),
            file_name=session.report_path.name,
            mime="text/markdown",
            width="stretch",
        )

    if st.button("🔄 Нова сесія", width="stretch"):
        session.close()
        for key in ("session", "tool_log", "messages"):
            st.session_state.pop(key, None)
        st.rerun()

    st.divider()
    st.markdown(f"**Правила:** {session.rules.label}")
    for problem in session.rule_problems:
        st.warning(problem)
    with st.expander(f"Таблиці ({len(session.schema.tables())})"):
        st.markdown("\n".join(f"- `{t}`" for t in session.schema.tables()))
