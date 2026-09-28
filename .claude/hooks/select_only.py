#!/usr/bin/env python3
"""PreToolUse hook for mcp__course-db__query: allow a single read-only SELECT only.

Defence in depth on top of the read-only DB role. Exit 0 = allow, exit 2 = block
(stderr is shown to Claude as the reason).
"""
import json
import re
import sys

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


def block(reason: str) -> None:
    print(f"course-db query blocked: {reason}", file=sys.stderr)
    sys.exit(2)


def normalise(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)          # block comments
    sql = re.sub(r"--[^\n]*", " ", sql)                        # line comments
    sql = re.sub(r"\$(\w*)\$.*?\$\1\$", "''", sql, flags=re.S)  # dollar-quoted
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)                 # string literals
    sql = re.sub(r'"(?:[^"]|"")*"', "ident", sql)              # quoted identifiers
    return sql.strip().lower()


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        block("hook could not parse tool input")
    sql = (payload.get("tool_input") or {}).get("sql")
    if not isinstance(sql, str) or not sql.strip():
        block("empty query")

    text = normalise(sql).rstrip(";").strip()
    if ";" in text:
        block("only a single statement is allowed")
    if not re.match(r"^(select|with)\b", text):
        block("only SELECT / WITH ... SELECT statements are allowed")
    kw = re.search(r"\b(" + "|".join(FORBIDDEN_KEYWORDS) + r")\b", text)
    if kw:
        block(f"forbidden keyword '{kw.group(1).upper()}'")
    fn = re.search(r"\b(" + "|".join(FORBIDDEN_FUNCTIONS) + r")\s*\(", text)
    if fn:
        block(f"forbidden function '{fn.group(1)}'")
    sys.exit(0)


if __name__ == "__main__":
    main()
