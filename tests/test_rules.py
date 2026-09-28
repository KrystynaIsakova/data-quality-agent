import pytest

from dq_agent.rules import Rules, RulesError


def test_repo_rules_match_schema(rules, schema):
    assert rules.ranges and rules.keys
    assert rules.validate_against(schema) == []


def test_every_range_has_known_basis(rules):
    assert {r.basis for r in rules.ranges} <= {"rule", "assumption"}


def test_missing_rules_dir_means_none(tmp_path):
    rules = Rules.load(tmp_path)
    assert rules.label == "none"
    assert rules.ranges == [] and rules.keys == {}


@pytest.mark.parametrize("body, message", [
    ("ranges:\n  - {table: t, column: c, basis: rule}\n", "min / max"),
    ("ranges:\n  - {table: t, column: c, min: 5, max: 1, basis: rule}\n", "min > max"),
    ("ranges:\n  - {table: t, column: c, min: 0, basis: maybe}\n", "basis"),
])
def test_invalid_range_rules_rejected(tmp_path, body, message):
    (tmp_path / "ranges.yaml").write_text(body)
    with pytest.raises(RulesError, match=message):
        Rules.load(tmp_path)


def test_rule_pointing_to_unknown_column_is_reported(tmp_path, schema):
    (tmp_path / "ranges.yaml").write_text(
        "ranges:\n  - {table: enrollments, column: nope, min: 0, basis: rule}\n"
        "  - {table: enrollments, column: funnel_state, min: 0, basis: rule}\n")
    problems = Rules.load(tmp_path).validate_against(schema)
    assert any("does not exist" in p for p in problems)
    assert any("not numeric" in p for p in problems)
