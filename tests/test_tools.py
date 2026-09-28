from decimal import Decimal

from dq_agent import tools
from dq_agent.results import FAIL, INFO, PASS


def test_unknown_table_is_rejected_without_querying(setup_tools):
    db, _ = setup_tools(lambda q, p: [])
    result = tools.count_rows("users; DROP TABLE users")
    assert "Unknown table" in result["error"]
    assert db.queries == []


def test_unknown_column_is_rejected(setup_tools):
    db, _ = setup_tools(lambda q, p: [])
    result = tools.check_missing_values("enrollments", columns=['x" OR 1=1 --'])
    assert "Unknown column" in result["error"]
    assert db.queries == []


def test_count_rows_quotes_identifiers(setup_tools):
    db, store = setup_tools(lambda q, p: [{"n": 42}])
    assert tools.count_rows("Public.Enrollments") == {"table": "enrollments", "row_count": 42}
    assert '"public"."enrollments"' in db.queries[0][0]
    assert store.tables["enrollments"]["rows"] == 42


def test_out_of_range_systematic_rule_violation(setup_tools):
    def responder(query, params):
        if "GROUP BY" in query:
            return [{"value": Decimal("104.0"), "n": 277}]
        return [{"total": 1000, "non_null": 1000, "violations": 277,
                 "distinct_violating": 1, "min_value": Decimal("0"), "max_value": Decimal("104")}]

    db, store = setup_tools(responder)
    result = tools.check_out_of_range("enrollments", "progress_pct")
    check = result["checks"][0]
    assert check["violations"] == 277
    assert check["report_section"] == "confirmed"
    assert check["top_violating_values"] == [{"value": 104, "count": 277}]
    assert check["systematic"].startswith("систематичний")
    assert db.queries[0][1] == [0.0, 100.0, 0.0, 100.0]   # bounds are parameters
    assert "104" not in db.queries[0][0]
    [stored] = store.confirmed()
    assert stored.status == FAIL and stored.rows == 277


def test_out_of_range_user_bounds_are_assumptions(setup_tools):
    _, store = setup_tools(lambda q, p: [{"value": 7, "n": 1}] if "GROUP BY" in q else
                           [{"total": 10, "non_null": 10, "violations": 1,
                             "distinct_violating": 1, "min_value": 0, "max_value": 7}])
    result = tools.check_out_of_range("weekly_activity", "quiz_attempts", max_value=5)
    assert result["checks"][0]["report_section"] == "possible (assumption)"
    assert store.confirmed() == [] and len(store.possible()) == 1


def test_out_of_range_rejects_text_column_and_missing_rule(setup_tools):
    setup_tools(lambda q, p: [])
    assert "not numeric" in tools.check_out_of_range("enrollments", "funnel_state")["error"]
    assert "No range rule" in tools.check_out_of_range("dim_course", "spec_size")["error"]
    assert "No range rules" in tools.check_out_of_range("users")["error"]


def test_out_of_range_all_table_rules(setup_tools, rules):
    _, store = setup_tools(lambda q, p: [{"total": 5, "non_null": 5, "violations": 0,
                                          "distinct_violating": 0, "min_value": 1, "max_value": 2}])
    result = tools.check_out_of_range("payments")
    assert len(result["checks"]) == len(rules.ranges_for("payments"))
    assert store.all_pass


def test_duplicates_four_slices(setup_tools):
    def responder(query, params):
        if "HAVING" in query:
            return [{"n_groups": 3, "min_size": 2, "max_size": 2}]
        return [{"total": 100, "full_row": 0, "id_dup": 3, "no_id_dup": 3, "natural_dup": 0}]

    db, store = setup_tools(responder)
    result = tools.check_duplicates("enrollments")
    slices = {s["slice"]: s for s in result["slices"]}
    assert set(slices) == {"full_row", "id_column", "row_without_id", "natural_key"}
    assert slices["id_column"]["report_section"] == "confirmed"
    assert slices["id_column"]["systematic"].startswith("систематичний")
    assert slices["row_without_id"]["report_section"] == "possible (assumption)"
    assert 'count(DISTINCT ROW("user_id", "course_id"))' in db.queries[0][0]
    assert [r.column for r in store.confirmed()] == ["enrollment_id"]


def test_duplicates_without_known_key_skips_slices(setup_tools):
    setup_tools(lambda q, p: [{"total": 10, "full_row": 0, "id_dup": 0, "no_id_dup": 0}])
    result = tools.check_duplicates("users")
    assert [s["slice"] for s in result["slices"]] == ["full_row", "id_column", "row_without_id"]
    assert any("natural_key" in s for s in result["skipped"])


def test_missing_values_classification(setup_tools):
    def responder(query, params):
        assert all(p == tools.SENTINELS for p in params)
        row = {"total": 10}
        for i in range(7):
            row.update({f"c{i}_null": 0, f"c{i}_empty": 0, f"c{i}_sentinel": 0, f"c{i}_space": 0})
        row["c0_null"] = 1        # user_id (ID column) has a NULL
        row["c2_null"] = 4        # country: NULLs are informational
        row["c3_sentinel"] = 2    # plan: sentinel values
        return [row]

    _, store = setup_tools(responder)
    result = tools.check_missing_values("users")
    assert result["total_rows"] == 10
    by_rule = {(r.column, r.status) for r in store.all()}
    assert ("user_id", FAIL) in by_rule
    assert ("plan", FAIL) in by_rule
    assert [r.column for r in store.confirmed()] == ["user_id"]
    assert [r.column for r in store.possible()] == ["plan"]
    info = [r for r in store.all() if r.status == INFO]
    assert info and "country: 4" in info[0].actual
    assert store.tables["users"]["nulls"] == {"user_id": 1, "country": 4}


def test_rerun_clears_stale_hidden_missing_failure(setup_tools):
    state = {"sentinel": 2}

    def responder(query, params):
        return [{"total": 10, "c0_null": 0, "c0_empty": 0,
                 "c0_sentinel": state["sentinel"], "c0_space": 0}]

    _, store = setup_tools(responder)
    tools.check_missing_values("users", ["plan"])
    assert store.possible()
    state["sentinel"] = 0
    tools.check_missing_values("users", ["plan"])
    assert store.possible() == []
    assert any(r.status == PASS for r in store.all())


def test_tools_have_gemini_declarations():
    from google.genai import types
    for func in tools.TOOLS:
        decl = types.FunctionDeclaration.from_callable_with_api_option(callable=func)
        assert decl.name == func.__name__
        assert decl.description
        assert "table" in decl.parameters.required
