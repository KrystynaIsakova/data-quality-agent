import pytest

from dq_agent.sql_guard import UnsafeSQLError, assert_safe_select


@pytest.mark.parametrize("query", [
    "SELECT count(*) FROM enrollments",
    "select 1;",
    "WITH x AS (SELECT 1 AS n) SELECT n FROM x",
    "SELECT count(*) FILTER (WHERE lower(btrim(\"plan\")) = ANY(%s)) FROM users",
    "SELECT 'drop table users; delete' AS text_literal",
    'SELECT "update" FROM t',
    "SELECT 1 -- ; DROP TABLE users",
])
def test_allows_single_select(query):
    assert_safe_select(query)


@pytest.mark.parametrize("query", [
    "",
    "   ",
    "DELETE FROM users",
    "UPDATE users SET plan = 'x'",
    "DROP TABLE users",
    "SELECT 1; DROP TABLE users",
    "SELECT * INTO backup FROM users",
    "WITH d AS (DELETE FROM users RETURNING *) SELECT * FROM d",
    "SELECT pg_sleep(10)",
    "SELECT set_config('x', 'y', false)",
    "SELECT * FROM users FOR UPDATE",
    "/* SELECT */ INSERT INTO users VALUES (1)",
    "EXPLAIN ANALYZE SELECT 1",
    "COPY users TO '/tmp/x'",
    "SELECT $$x$$; DROP TABLE users",
])
def test_blocks_everything_else(query):
    with pytest.raises(UnsafeSQLError):
        assert_safe_select(query)
