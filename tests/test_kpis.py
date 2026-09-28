"""KPI calculations from semantic_layer.yaml (no DB, no Gemini)."""
import re
from decimal import Decimal

import pytest

from dq_agent import kpis

from conftest import FakeDB


@pytest.fixture
def layer():
    return kpis.load_semantic_layer()


def test_repo_semantic_layer_matches_code_and_schema(layer, schema):
    assert kpis.validate_semantic_layer(layer, schema) == []
    assert set(layer["metrics"]) == set(kpis.METRICS)


def test_sql_matches_yaml_calculation(layer):
    """Every aggregate in the YAML calculation appears in the implemented SQL."""
    for name, metric in layer["metrics"].items():
        yaml_calc = " ".join(metric["calculation"].split())
        impl = kpis.METRICS[name]["value"].replace("f.", "")
        for aggregate in re.findall(r"(?:COUNT|AVG|SUM)\((?:[^()]|\([^()]*\))*\)", yaml_calc):
            assert aggregate in impl, f"{name}: {aggregate} not in {impl}"


def test_total_enrollments(layer):
    db = FakeDB(lambda q, p: [{"value": 1234, "n": 1234}])
    result = kpis.total_enrollments(db.run_select)
    assert result["value"] == 1234 and result["unit"] == "count" and result["by"] is None
    assert result["description"] == layer["metrics"]["total_enrollments"]["description"]
    query = db.queries[0][0]
    assert 'COUNT(f.enrollment_id) AS value' in query
    assert 'FROM "public"."enrollments" AS f' in query


def test_average_progress_rounds_and_reports_non_null_count():
    db = FakeDB(lambda q, p: [{"value": Decimal("47.123456"), "n": 900}])
    result = kpis.average_progress(db.run_select)
    assert result == {**result, "value": 47.12, "n": 900, "unit": "%"}
    assert "AVG(f.progress_pct)" in db.queries[0][0]


def test_completion_rate_uses_decimal_division_and_definition():
    db = FakeDB(lambda q, p: [{"value": Decimal("31.4159"), "n": 1000}])
    result = kpis.completion_rate(db.run_select)
    assert result["value"] == 31.42
    assert "completed_at IS NOT NULL" in result["definition"]
    query = db.queries[0][0]
    assert "* 100.0" in query and "NULLIF(COUNT(f.enrollment_id), 0)" in query


def test_empty_table_gives_none_not_error():
    db = FakeDB(lambda q, p: [{"value": None, "n": 0}])
    assert kpis.completion_rate(db.run_select)["value"] is None


def test_by_course_groups_fact_table_without_join():
    db = FakeDB(lambda q, p: [{"dim": "c1", "value": Decimal("50.0"), "n": 4},
                              {"dim": "c2", "value": Decimal("0"), "n": 2}])
    result = kpis.completion_rate(db.run_select, by="course")
    assert result["rows"] == [{"course": "c1", "value": 50.0, "n": 4},
                              {"course": "c2", "value": 0.0, "n": 2}]
    assert result["truncated"] is False and "value" not in result
    query = db.queries[0][0]
    assert "JOIN" not in query and 'GROUP BY f."course_url"' in query


def test_by_specialization_joins_distinct_dim_course():
    db = FakeDB(lambda q, p: [{"dim": "s1", "value": 10, "n": 10},
                              {"dim": None, "value": 3, "n": 3}])   # course not in dim_course
    result = kpis.total_enrollments(db.run_select, by="specialization")
    assert result["rows"][1] == {"specialization": None, "value": 3, "n": 3}
    query = db.queries[0][0]
    assert ('LEFT JOIN (SELECT DISTINCT "course_url", "specialization_url" '
            'FROM "public"."dim_course") AS d ON d."course_url" = f."course_url"') in query
    assert 'GROUP BY d."specialization_url"' in query


def test_unknown_metric_or_dimension_is_rejected_without_query():
    db = FakeDB(lambda q, p: [])
    with pytest.raises(ValueError, match="Unknown metric"):
        kpis.calculate_kpi(db.run_select, "revenue")
    with pytest.raises(ValueError, match="Unknown dimension"):
        kpis.calculate_kpi(db.run_select, "total_enrollments", by="country; DROP TABLE x")
    assert db.queries == []


def test_validation_reports_drift(schema):
    layer = {
        "metrics": {"revenue": {"table": "payments"},
                    "total_enrollments": {"table": "orders"}},
        "dimensions": {"plan": {"table": "users", "key": "plan"}},
    }
    problems = kpis.validate_semantic_layer(layer, schema)
    assert any("'revenue' has no implementation" in p for p in problems)
    assert any("unknown table 'orders'" in p for p in problems)
    assert any("has no 'course_url'" in p for p in problems)


def test_list_kpis(layer):
    listing = kpis.list_kpis(layer)
    assert [m["name"] for m in listing["metrics"]] == list(layer["metrics"])
    assert {d["name"] for d in listing["dimensions"]} == {"course", "specialization"}
