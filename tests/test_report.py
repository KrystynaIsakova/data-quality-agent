from datetime import date

from dq_agent import report
from dq_agent.config import Settings
from dq_agent.results import FAIL, PASS, CheckResult, ResultStore


def _result(**kw):
    base = dict(check="out_of_range", table="enrollments", column="progress_pct",
                rule="progress_pct in [0, 100]", expected="0", actual="0",
                status=PASS, basis="rule")
    base.update(kw)
    return CheckResult(**base)


def test_sections_follow_basis():
    store = ResultStore()
    store.add(_result(status=FAIL, rows=277, share=0.0029, systematic="систематичний: 104"))
    store.add(_result(column="quiz_score", table="weekly_activity", rule="quiz_score in [0, 100]",
                      status=FAIL, basis="assumption", rows=5, share=0.5))
    text = report.render(store, "rules/ranges.yaml", today=date(2026, 9, 23))

    confirmed = text.split("## 1.")[1].split("## 2.")[0]
    possible = text.split("## 2.")[1].split("## 3.")[0]
    assert "enrollments.progress_pct" in confirmed and "quiz_score" not in confirmed
    assert "weekly_activity.quiz_score" in possible and "progress_pct" not in possible
    assert "**ALL PASS: False**" in text
    assert "Дата: 2026-09-23" in text
    priorities = text.split("## 5.")[1]
    assert priorities.index("progress_pct") < priorities.index("quiz_score")


def test_all_pass_requires_checks_and_no_failures():
    store = ResultStore()
    assert not store.all_pass
    store.add(_result())
    assert store.all_pass
    assert "**ALL PASS: True**" in report.render(store, "none")
    store.add(_result(column="x", rule="x", status=FAIL, basis="heuristic"))
    assert not store.all_pass


def test_report_has_no_connection_details(tmp_path):
    store = ResultStore()
    store.add(_result())
    store.add_exchange("перевір enrollments", "Усе добре.", ["Яка шкала quiz_score?"])
    path = report.write(store, tmp_path / "r.md", "rules/ranges.yaml")
    text = path.read_text()
    assert "postgresql://" not in text
    assert "- Яка шкала quiz_score?" in text
    assert "### Запит: перевір enrollments" in text


def test_split_questions():
    text, questions = report.split_questions(
        "Знайдено 277 порушень.\n\nQUESTIONS:\n- Яка шкала quiz_score?\n2. Чи можливий курс без тижнів?\n")
    assert text == "Знайдено 277 порушень."
    assert questions == ["Яка шкала quiz_score?", "Чи можливий курс без тижнів?"]
    assert report.split_questions("Без питань.") == ("Без питань.", [])


def test_settings_redact_hides_secrets():
    s = Settings(gemini_api_key="key123", database_url="postgresql://u:s3cret@h:5432/db")
    assert "s3cret" not in repr(s) and "key123" not in repr(s)
    msg = s.redact("failed for postgresql://u:s3cret@h:5432/db, password s3cret, key key123")
    assert "s3cret" not in msg and "key123" not in msg
