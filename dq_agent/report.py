"""Render reports/data_quality_report.md from the session's check results.

Section structure follows the validate-dataset skill. Placement into
"confirmed" vs "possible" comes from each result's basis, not from the LLM.
"""
import re
from datetime import date
from pathlib import Path

from dq_agent.results import FAIL, CheckResult, ResultStore

QUESTIONS_HEADER = re.compile(r"^\s*[#*_ ]*(QUESTIONS|ПИТАННЯ)\b[^\n]*$", re.I | re.M)
LIST_ITEM = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.+?)\s*$")


def split_questions(answer: str) -> tuple[str, list[str]]:
    """Split the agent answer into (text, business questions from a QUESTIONS: block)."""
    match = QUESTIONS_HEADER.search(answer or "")
    if not match:
        return (answer or "").strip(), []
    text = answer[:match.start()].rstrip()
    questions = []
    for line in answer[match.end():].splitlines():
        item = LIST_ITEM.match(line)
        if item:
            questions.append(item.group(1).strip())
        elif line.strip() and questions:
            break
    return text, questions


def render(store: ResultStore, rules_label: str, rule_problems: list[str] | None = None,
           today: date | None = None) -> str:
    today = today or date.today()
    confirmed = sorted(store.confirmed(), key=lambda r: -r.rows)
    possible = sorted(store.possible(), key=lambda r: -r.rows)

    lines = [
        "# Data quality report",
        "",
        f"Джерело: PostgreSQL (read-only, лише SELECT) · Правила: {rules_label} · "
        f"Дата: {today.isoformat()}",
        "",
        f"Source data не змінювались. **ALL PASS: {store.all_pass}**",
        "",
        "## 0. Загальна картина",
        "",
    ]
    lines += _overview(store)

    lines += ["", "## 1. Підтверджені проблеми", ""]
    lines += _issues(confirmed) if confirmed else ["Не виявлено."]

    lines += ["", "## 2. Можливі проблеми (припущення)", ""]
    lines += _issues(possible) if possible else ["Не виявлено."]

    lines += ["", "## 3. Питання до бізнесу", ""]
    lines += [f"- {q}" for q in store.questions] or ["Немає."]

    lines += ["", "## 4. Результати перевірок", ""]
    lines += _checks_table(store.all())

    lines += ["", "## 5. Пріоритети", ""]
    lines += _priorities(confirmed + possible)

    lines += ["", "## 6. Висновки агента", ""]
    if store.exchanges:
        for ex in store.exchanges:
            lines += [f"### Запит: {_inline(ex.request)}", "", ex.answer.strip() or "_(порожня відповідь)_", ""]
    else:
        lines.append("Немає.")

    lines += ["", "## Зауваження до правил", ""]
    lines += [f"- {p}" for p in rule_problems or []] or ["Немає."]
    return "\n".join(lines).rstrip() + "\n"


def write(store: ResultStore, path: Path, rules_label: str,
          rule_problems: list[str] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(store, rules_label, rule_problems), encoding="utf-8")
    return path


# ---------------------------------------------------------------- sections

def _overview(store: ResultStore) -> list[str]:
    if not store.tables:
        return ["Перевірок ще не виконано."]
    lines = ["| Таблиця | Рядків | Колонок | Ключі (constraints) | Дати в text-колонках |",
             "|---|---|---|---|---|"]
    for table, facts in sorted(store.tables.items()):
        constraints = facts.get("constraints")
        constraints_text = (
            "; ".join(f"{k}: {', '.join(v)}" for k, v in constraints.items())
            if constraints else ("немає" if constraints is not None else "—")
        )
        lines.append("| {} | {} | {} | {} | {} |".format(
            table,
            _int(facts.get("rows")),
            facts.get("columns", "—"),
            _cell(constraints_text),
            _cell(", ".join(facts.get("date_like_text", [])) or "—"),
        ))

    nulls = [(t, c, n, f.get("rows")) for t, f in sorted(store.tables.items())
             for c, n in f.get("nulls", {}).items()]
    if nulls:
        lines += ["", "**NULL-значення**", "", "| Таблиця | Колонка | NULL | Частка |", "|---|---|---|---|"]
        for table, column, n, rows in nulls:
            lines.append(f"| {table} | {column} | {_int(n)} | {_pct(n / rows if rows else None)} |")
    return lines


def _issues(results: list[CheckResult]) -> list[str]:
    lines = []
    for r in results:
        lines += [
            f"### {r.target} — {r.rule}",
            "",
            f"- **Умова:** `{r.condition or r.rule}`",
            f"- **Рядків:** {_int(r.rows)} ({_pct(r.share)})",
            f"- **Факт:** {r.actual}",
        ]
        if r.systematic:
            lines.append(f"- **Характер:** {r.systematic}")
        if r.note:
            lines.append(f"- **Підстава / контекст:** {r.note}")
        lines.append(f"- **Основа класифікації:** {_basis(r.basis)}")
        lines.append("")
    return lines


def _checks_table(results: list[CheckResult]) -> list[str]:
    if not results:
        return ["Перевірок ще не виконано."]
    lines = ["| Check | Об'єкт | Правило | Очікувано | Факт | Статус |",
             "|---|---|---|---|---|---|"]
    for r in results:
        lines.append("| {} | {} | {} | {} | {} | {} |".format(
            r.check, _cell(r.target), _cell(r.rule), _cell(r.expected), _cell(r.actual),
            f"**{r.status}**" if r.status == FAIL else r.status,
        ))
    return lines


def _priorities(failures: list[CheckResult]) -> list[str]:
    if not failures:
        return ["Немає проблем для пріоритизації."]

    def priority(r: CheckResult) -> tuple[int, str, str]:
        if r.is_confirmed:
            return 0, "Високий", "підтверджено правилом"
        if (r.share or 0) >= 0.01:
            return 1, "Середній", "припущення, ≥ 1% рядків"
        return 2, "Низький", "припущення, < 1% рядків"

    ranked = sorted(failures, key=lambda r: (priority(r)[0], -r.rows))
    lines = ["| # | Проблема | Рядків | Пріоритет | Чому |", "|---|---|---|---|---|"]
    for i, r in enumerate(ranked, 1):
        _, level, why = priority(r)
        if r.systematic.startswith(("систематичний", "переважно")):
            why += "; систематичний — можна відновити"
        lines.append(f"| {i} | {_cell(r.target + ': ' + r.rule)} | {_int(r.rows)} | {level} | {why} |")
    return lines


# ---------------------------------------------------------------- formatting

def _basis(basis: str) -> str:
    return {
        "rule": "записане правило (rules/)",
        "assumption": "припущення з rules/ (потребує підтвердження)",
        "heuristic": "евристика validate-dataset",
        "user": "межі / ключ із запиту користувача",
    }.get(basis, basis)


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _inline(text: str) -> str:
    return " ".join(str(text).split())


def _int(value) -> str:
    return f"{value:,}".replace(",", " ") if isinstance(value, int) else "—" if value is None else str(value)


def _pct(share: float | None) -> str:
    return "—" if share is None else f"{share:.2%}"
