import pytest
from psycopg import sql

from dq_agent import tools
from dq_agent.config import RULES_DIR
from dq_agent.results import ResultStore
from dq_agent.rules import Rules
from dq_agent.schema import Column, Schema
from dq_agent.sql_guard import assert_safe_select

# Snapshot of information_schema.columns (schema public), taken via the
# course-db MCP server during development. Column types only, no data.
SCHEMA_SNAPSHOT = {
    "dim_course": "course_id:character varying, course_url:text, specialization_url:text, "
                  "course_no:integer, course:text, n_weeks:integer, total_minutes:numeric, "
                  "minutes_per_week:numeric, video_min:numeric, practice_min:numeric, "
                  "spec_size:integer, specialization_name:text, partner:text, "
                  "level:character varying, domain:text, specialization_enrolled:numeric, "
                  "review_score:numeric, spec_id:character varying, n_reviews:numeric, "
                  "mean_stars:numeric, pct_low:numeric",
    "enrollments": "enrollment_id:character varying, user_id:character varying, "
                   "course_id:character varying, course_url:text, "
                   "specialization_id:character varying, course_no:integer, enrolled_at:text, "
                   "completed_at:text, last_week_reached:integer, n_weeks:integer, "
                   "progress_pct:numeric, funnel_state:character varying, is_certified:integer",
    "payments": "payment_id:character varying, user_id:character varying, paid_at:date, "
                "amount_usd:numeric, plan:character varying, is_refunded:integer",
    "review_link": "enrollment_id:character varying, review_id:character varying",
    "reviews": "review_id:character varying, course_url:text, review_date:text, "
               "review_content:text, stars:integer",
    "users": "user_id:character varying, signup_date:date, country:character varying, "
             "plan:character varying, persona:character varying, age_band:character varying, "
             "device_primary:character varying",
    "weekly_activity": "enrollment_id:character varying, week_number:integer, "
                       "active_date:date, minutes_watched:numeric, quiz_attempts:integer, "
                       "quiz_score:numeric",
}


def make_schema() -> Schema:
    tables = {}
    for table, spec in SCHEMA_SNAPSHOT.items():
        cols = {}
        for item in spec.split(", "):
            name, data_type = item.split(":")
            cols[name] = Column(name, data_type, nullable=True)
        tables[table] = cols
    return Schema(tables)


class FakeDB:
    """Records queries (rendered to text and passed through sql_guard)."""

    def __init__(self, responder):
        self.responder = responder
        self.queries: list[tuple[str, list]] = []

    def run_select(self, query, params=None):
        text = query.as_string() if isinstance(query, sql.Composable) else query
        assert_safe_select(text)
        self.queries.append((text, list(params or [])))
        return self.responder(text, list(params or []))


@pytest.fixture
def schema():
    return make_schema()


@pytest.fixture
def rules():
    return Rules.load(RULES_DIR)


@pytest.fixture
def setup_tools(schema, rules):
    """Return a factory: setup_tools(responder) -> (fake_db, store)."""
    def factory(responder):
        db = FakeDB(responder)
        store = ResultStore()
        tools.configure(tools.ToolContext(db.run_select, schema, rules, store))
        return db, store
    yield factory
    tools.configure(None)
