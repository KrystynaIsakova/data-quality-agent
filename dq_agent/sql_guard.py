"""Allow a single read-only SELECT only.

Ported from .claude/hooks/select_only.py. This is the first of three safety
layers; the others are the read-only transaction and statement_timeout in db.py.
"""
import re

FORBIDDEN_KEYWORDS = (
    "insert", "update", "delete", "merge", "upsert", "alter", "drop", "create",
    "truncate", "grant", "revoke", "copy", "vacuum", "analyze", "cluster",
    "reindex", "call", "do", "lock", "into", "set", "reset", "comment",
    "refresh", "listen", "notify", "prepare", "execute", "discard", "begin",
    "commit", "rollback", "savepoint",
)
FORBIDDEN_FUNCTIONS = (
    r"pg_sleep\w*", r"pg_terminate_backend", r"pg_cancel_backend",
    r"pg_read_file", r"pg_read_binary_file", r"pg_ls_dir", r"pg_stat_file",
    r"lo_\w+", r"dblink\w*", r"set_config", r"pg_reload_conf",
    r"pg_advisory\w*", r"nextval", r"setval", r"txid_current",
)


class UnsafeSQLError(ValueError):
    pass


def normalise(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)          # block comments
    sql = re.sub(r"--[^\n]*", " ", sql)                        # line comments
    sql = re.sub(r"\$(\w*)\$.*?\$\1\$", "''", sql, flags=re.S)  # dollar-quoted
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)                 # string literals
    sql = re.sub(r'"(?:[^"]|"")*"', "ident", sql)              # quoted identifiers
    return sql.strip().lower()


def assert_safe_select(sql: str) -> None:
    """Raise UnsafeSQLError unless `sql` is one SELECT / WITH ... SELECT."""
    if not isinstance(sql, str) or not sql.strip():
        raise UnsafeSQLError("empty query")

    text = normalise(sql).rstrip(";").strip()
    if ";" in text:
        raise UnsafeSQLError("only a single statement is allowed")
    if not re.match(r"^(select|with)\b", text):
        raise UnsafeSQLError("only SELECT / WITH ... SELECT statements are allowed")
    kw = re.search(r"\b(" + "|".join(FORBIDDEN_KEYWORDS) + r")\b", text)
    if kw:
        raise UnsafeSQLError(f"forbidden keyword '{kw.group(1).upper()}'")
    fn = re.search(r"\b(" + "|".join(FORBIDDEN_FUNCTIONS) + r")\s*\(", text)
    if fn:
        raise UnsafeSQLError(f"forbidden function '{fn.group(1)}'")
