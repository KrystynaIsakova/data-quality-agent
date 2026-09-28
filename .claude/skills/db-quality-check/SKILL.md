---
name: db-quality-check
description: Reference spec only — SQL check methodology to port into the Python Data Quality Agent. Do not run it to check data from Claude Code.
disable-model-invocation: true
argument-hint: "[table ...|all]"
---

# db-quality-check

SQL adaptation of the user-level `validate-dataset` skill (profiling → rules → validation report) for the live `course-db` Postgres DB. Methodology and report structure follow that skill; only the mechanics change from pandas to SQL.

## Input

`$ARGUMENTS` — space-separated table names in schema `public`.

- empty → core set: `enrollments users payments weekly_activity dim_course reviews review_link`
- `all` → every base table from `information_schema.tables` (`table_type = 'BASE TABLE'`, schema `public`)

## Safety contract (read before the first query)

1. The only data access is `mcp__course-db__query`. Only one `SELECT` / `WITH … SELECT` per call. A hook (`.claude/hooks/select_only.py`) blocks anything else — if it blocks a query, rewrite it as a plain SELECT; never try to work around it.
2. **Aggregate first.** Answer questions with `count`, `count(DISTINCT)`, `min/max`, `avg`, percentiles, `GROUP BY` of *values*. Never `SELECT *` from a table without `LIMIT 5`, and never put raw rows in a report.
3. **Sensitive columns** — output only aggregates, never example values:
   - identifiers of people or their records: `user_id`, `enrollment_id`, `payment_id`, `review_id` → counts, null rates, uniqueness, format pattern shapes (e.g. `length()`, regex class), never the IDs themselves;
   - quasi-identifiers in `users` (`country`, `age_band`, `persona`, `device_primary`, `signup_date`) → distributions only; merge groups smaller than 10 into `other (<10)`; never cross-tab more than one of them at a time;
   - free text (`reviews.review_content`, `*_description`, `details`, `specialization_instructors`, `skills`) → null/empty/whitespace counts and length stats only; never quote content.
   Course/specialization names, URLs and category values (`level`, `plan`, `funnel_state`) are non-personal and may be shown.
4. No credentials, connection strings or environment details in reports or chat.
5. Write only inside `reports/`. The DB is read-only, so there is no cleaning step.

## Step 0. Discover

For each target table: columns + types + nullability (`information_schema.columns`), constraints (`information_schema.table_constraints` / `key_column_usage`), exact `count(*)`. Note text columns that hold dates or numbers — those are the prime suspects.

## Step 1. Profiling (SQL recipes)

Build one aggregate query per table where possible (many `count(*) FILTER (WHERE …)` expressions in a single SELECT) to keep round-trips low.

- **Shape**: rows, columns, date coverage (`min/max` of each date column; for text dates only over values matching the dominant format).
- **Columns**: `count(DISTINCT col)`, min/max, value length distribution for text (`length(col), count(*) … GROUP BY 1`) — equal lengths with different shapes reveal hidden formats.
- **Key & granularity**: what one row means; test candidate keys with `count(*) - count(DISTINCT key)`; composite natural keys with `count(DISTINCT (a, b))`.
- **Missing values**: `col IS NULL`, plus for text: `btrim(col) = ''`, `lower(btrim(col)) IN ('na','n/a','null','none','-','?','unknown','nan')`, and `col <> btrim(col)` (edge whitespace).
- **Duplicates — at least 4 slices**:
  1. full row: `SELECT count(*) - count(DISTINCT t.*) FROM t`;
  2. ID column: `count(id) - count(DISTINCT id)`;
  3. row without ID: `count(DISTINCT (all non-id columns))` vs `count(*)`;
  4. natural key (e.g. `(user_id, course_id)` in enrollments, `(enrollment_id, week_number)` in weekly_activity).
- **Formats** (text dates/codes): classify with `CASE WHEN col ~ '^\d{4}-\d{2}-\d{2}$' THEN 'YYYY-MM-DD' WHEN col ~ '^\d{2}\.\d{2}\.\d{4}$' THEN 'DD.MM.YYYY' … ELSE 'other' END` and count per class. Cast to date **only** inside the matching class (`to_date(col, 'YYYY-MM-DD')`), never with a blind `::date` on the whole column.
- **Ranges**: negatives where impossible (minutes, amounts, counts), percentages outside `[0, 100]`, stars outside `1–5`, dates in the future (`> current_date`) or before plausible start, weeks `< 1`.
- **Logical contradictions** (examples for this DB — verify each exists before using):
  - `completed_at < enrolled_at`; `completed_at` set but `funnel_state` not completed (and vice versa);
  - `is_certified = 1` without completion; `last_week_reached > n_weeks`;
  - `progress_pct` vs `100.0 * last_week_reached / n_weeks`;
  - `enrollments.n_weeks` ≠ `dim_course.n_weeks` for the same course;
  - `dim_course.total_minutes` vs `n_weeks * minutes_per_week`, `video_min + practice_min`;
  - `payments.is_refunded` with `amount_usd <= 0`; `weekly_activity.active_date < enrolled_at`.
- **Reference consistency**: one id → one attribute (`course_id` → `course_url`, `course_no`; `spec_id` → `specialization_name`): `GROUP BY id HAVING count(DISTINCT attr) > 1`.

### Systematic or random?

For every defect, `GROUP BY` the offending value (or the delta, e.g. `completed_at - enrolled_at`). All identical (one constant, one offset) → technical artefact (stub, ETL/timezone bug), recoverable. Varied → needs a human. Report which it is.

### Overlapping defects

Before reporting two defects separately, compare their key sets: `SELECT count(*) FROM (<keys of defect A> INTERSECT <keys of defect B>) x`. If they coincide, they are one defect with two symptoms — say so.

### Cross-table integrity (when ≥ 2 related tables are in scope)

Orphan counts with `LEFT JOIN … WHERE parent.key IS NULL` (report count and share, no IDs):

- `enrollments.user_id → users.user_id`, `enrollments.course_id → dim_course.course_id`
- `payments.user_id → users.user_id`
- `weekly_activity.enrollment_id → enrollments.enrollment_id`
- `review_link.enrollment_id → enrollments`, `review_link.review_id → reviews`
- `reviews.course_url → dim_course.course_url`
- `weekly_activity.week_number` beyond the enrollment's `n_weeks`

## Step 2. Rules from `rules/`

If `rules/` exists in the project root, read every `.md`/`.yaml`/`.py`/`.sql` file; rules take priority over Step 1 heuristics and are applied in their documented order. For each rule extract: condition → action on violation → affected columns → validation check. A rule without a validation check is itself a finding.

If `rules/` is missing or empty, say so ("Rules: none") and use Step 1 only. Never invent rules retroactively.

A rule with zero hits is a result — show it and explain why (often overlap with another rule).

## Step 3. Report

One file per table: `reports/validation_<table>.md`. When more than one table is checked, also `reports/validation_summary.md` with the cross-table section, a per-table ALL PASS list and the top priorities overall. Overwrite files from earlier runs; the run date in the header distinguishes them. Report language = the user's language.

```markdown
# Validation report: public.<table>
Source: course-db (PostgreSQL, read-only) · Rules: <rules/ or "none"> · Date: <YYYY-MM-DD>
Source data not modified (SELECT-only). ALL PASS: <True/False>

## 0. Overview
<rows, columns, key & granularity, coverage period, nulls/sentinels, duplicates (4 slices) — as tables>

## 1. Confirmed issues
<data contradicts itself or violates a rule from rules/>
Per issue: condition, rows, share, systematic vs random, why it is a defect, what to do. Include the SQL condition used.

## 2. Possible issues (assumptions)
<technically consistent, but easy to analyse wrongly>
Per issue: what we see, why it may be normal, which metric it would distort.

## 3. Questions for the business
<answers are not in the data>

## 4. Check results
| Check | Rule | Expected | Actual | Status |

## 5. Priorities
| # | Issue | Rows | Priority | Why |

## Known residual issues
<deliberate consequences of rules that look like errors; "none" if none>
```

### Boundary between sections 1, 2 and 3 — the core of this skill

| Section | Criterion | Example |
|---|---|---|
| **1. Confirmed** | Proven by the data itself or violates a written rule | `completed_at < enrolled_at` |
| **2. Assumption** | Looks odd but may be normal | 95% missing completion dates |
| **3. Question** | The answer is not in the data | Can a course be completed without a certificate? |

Never promote an assumption to confirmed because it is phrased confidently. In doubt → section 2.

`ALL PASS` is `True` only if section 1 is empty and every check in section 4 passed. Never report "done" if checks did not run.

Before saving, re-read the report and remove anything that violates the safety contract (IDs, free text, small-group demographics, credentials).

## Step 4. Summary in chat

3–5 most important findings, ALL PASS per table, paths to the reports. Do not retell the report.
