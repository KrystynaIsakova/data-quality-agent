# Специфікація: Data Quality Agent

| | |
|---|---|
| **Статус** | Реалізовано (v1), не перевірено на реальній БД |
| **Дата** | 2026-09-23 |
| **Код** | `dq_agent/`, `app.py`, `main.py` |
| **Джерела методології** | skill `validate-dataset`, `.claude/skills/db-quality-check/SKILL.md`, `lesson3_for_me/rules/enrollments_cleaning_rules.md` |

## 1. Мета

Дати аналітику інструмент, який звичайною мовою відповідає на питання «чи можна довіряти цим даним?» для PostgreSQL-бази курсу (Coursera). Агент сам обирає перевірки, виконує їх безпечно, лише читаючи дані, і зберігає звіт, у якому **підтверджені дефекти відокремлені від припущень і питань до бізнесу**.

### 1.1. Поза межами v1

- Очищення чи зміна даних у будь-якій формі.
- Довільний SQL від користувача чи LLM.
- Перевірки форматів дат у текстових колонках, логічних суперечностей між колонками (`completed_at < enrolled_at`) і міжтабличної цілісності (orphans). Див. §11.
- Багатокористувацький режим з автентифікацією.

## 2. Користувачі та сценарії

| Сценарій | Приклад запиту | Очікувана поведінка |
|---|---|---|
| Повний аудит таблиці | «Перевір таблицю enrollments» | Усі 5 тулів для таблиці, підсумок, звіт |
| Точкова перевірка | «Чи є дублікати в weekly_activity?» | Лише `check_duplicates` (і `describe_table`, якщо структура невідома) |
| Власні межі | «Чи quiz_score між 0 і 10?» | `check_out_of_range` з межами з запиту; порушення потрапляють у припущення |
| Уточнення | «А скільки з них систематичні?» | Відповідь з уже отриманих даних або повторний виклик тула |
| Невідомий об'єкт | «Перевір таблицю orders» | Тул повертає помилку зі списком наявних таблиць, агент пояснює |

## 3. Архітектура

```
app.py (Streamlit) ─┐                        інший агент (оркестратор)
                    ├─► session.py ──┐                  │ check_tables() / run()
main.py (CLI) ──────┘                ▼                  ▼
                        subagent.py (DataQualitySubagent) ─► agent.py (Gemini chat)
                             │                    │
                             │                    ▼
                             │               tools.py ─► schema.py (whitelist)
                             │                    │
                             │                    ▼
                             │               db.run_select ─► sql_guard ─► PostgreSQL (read-only)
                             │                    │
                             ▼                    ▼
                        report.py ◄──────── results.ResultStore
                             │
                             ▼
                reports/data_quality_report.md
```

| Модуль | Відповідальність |
|---|---|
| `config.py` | Читання `.env` при кожному старті сесії; `Settings.redact()` для секретів |
| `subagent.py` | `DataQualitySubagent`: старт, `check_tables()` (без LLM), `run()` (Gemini), структурований `QualityResult`, запис звіту |
| `session.py` | Тонка обгортка для обох UI: `Session.ask()` повертає текст і питання, повний результат у `last_result` |
| `agent.py` | Gemini-чат, system prompt, розбір блоку `QUESTIONS:` |
| `tools.py` | 5 тулів; побудова SQL; запис `CheckResult` |
| `schema.py` | Whitelist таблиць і колонок з `information_schema` |
| `rules.py` | Завантаження й валідація `rules/*.yaml` |
| `sql_guard.py` | Валідатор «лише один SELECT» |
| `db.py` | Read-only з'єднання, таймаут, ліміт рядків |
| `results.py` | `CheckResult`, `ResultStore`, логіка ALL PASS |
| `report.py` | Детермінований рендер Markdown-звіту |

### 3.1. Контракт сабагента (виклик з іншого агента)

`DataQualitySubagent` викликається in-process, доступ до БД лише read-only (ті самі три рівні захисту).

```python
dq = DataQualitySubagent.create(llm=False)          # llm=True відкриває ще й Gemini-чат
result = dq.check_tables(["enrollments"], checks=["count", "out_of_range"])
result = dq.run("перевір progress_pct")             # природна мова, потрібен llm=True
result.to_dict()                                     # JSON-серіалізований dict
```

- `check_tables(tables, checks=None)`: детерміновано викликає тули `describe`, `count`, `missing`, `duplicates`, `out_of_range` (за замовчуванням усі). Таблиці без правил у `ranges.yaml` потрапляють у `skipped`.
- `run(request)`: запит через Gemini. Помилки не піднімаються, а повертаються як `status = "ERROR"` (секрети замасковано).
- `QualityResult`: `status` (`PASS` / `FAIL` / `NO_CHECKS` / `ERROR`), `all_pass`, `tables`, `confirmed` / `possible` / `passed` / `info` (словники `CheckResult` + `target`, `section`), `facts`, `questions`, `skipped`, `errors`, `summary`, `report_path`.
- Результат містить лише перевірки **цього виклику** (`ResultStore.begin_run()`), а сховище і звіт залишаються накопичувальними.
- Класифікація та сама, що в розділі 7: `confirmed` лише для `basis == "rule"`.

### 3.2. KPI-тули (`dq_agent/kpis.py`)

Детерміновані функції для метрик із `semantic_layer.yaml`. Файл лише читається і змінюється тільки аналітиком. Модуль не залежить від агентів і Gemini: LLM KPI не рахує, лише викликає функції. Кожне значення рахує один агрегатний SELECT через `run_select`, тобто проходить `sql_guard` і read-only сесію.

| Функція | Метрика | Колонки | SQL (`f` = `enrollments`) |
|---|---|---|---|
| `total_enrollments(run_select, by=None)` | `total_enrollments` | `enrollment_id` | `COUNT(f.enrollment_id)` |
| `average_progress(run_select, by=None)` | `average_progress` | `progress_pct` | `AVG(f.progress_pct)`; `n` = кількість не-NULL |
| `completion_rate(run_select, by=None)` | `completion_rate` | `completed_at`, `enrollment_id` | `COUNT(CASE WHEN f.completed_at IS NOT NULL THEN 1 END) * 100.0 / NULLIF(COUNT(f.enrollment_id), 0)` |

- `by="course"` групує за `enrollments.course_url` без join.
- `by="specialization"` робить `LEFT JOIN` на `SELECT DISTINCT course_url, specialization_url FROM dim_course`, щоб дублікати в `dim_course` не множили рядки. Курс, що входить у кілька спеціалізацій, рахується в кожній. Курси, яких немає в `dim_course`, потрапляють у групу `None`.
- Результат: `metric`, `description`, `definition`, `calculation` (текст з YAML), `table`, `unit`, `by`, а також `value` і `n`, або з `by` натомість `rows` і `truncated`. Значення округлюються до 2 знаків.
- `calculation` з YAML не вставляється в SQL. SQL кожної метрики записаний у `kpis.METRICS`, а `tests/test_kpis.py` перевіряє, що агрегати з YAML є в SQL і що таблиці та колонки існують у схемі.
- `* 100.0` дає десяткове ділення: цілочисельне `COUNT / COUNT` у PostgreSQL обрізало б результат до 0. Це реалізація формули з YAML, а не зміна визначення.

### 3.3. Analytics Orchestrator (`dq_agent/orchestrator.py`, `analyst.py`)

Gemini-агент для аналітичних питань. LLM лише обирає метрику та вимір і формулює відповідь. KPI він не рахує.

Тули оркестратора:
- `list_kpis()`: метрики й виміри з `semantic_layer.yaml`. Каталог також вбудовано в system prompt.
- `get_kpi(metric, by=None)`: весь конвеєр у Python.
  1. Визначення метрики з `semantic_layer.yaml`.
  2. Потрібні таблиці й колонки: `kpis.METRICS[...]["columns"]`, для `by` ще `course_url`, для `specialization` ще `dim_course.course_url` і `specialization_url`.
  3. `DataQualitySubagent.check_tables(table, ["missing", "duplicates", "out_of_range"])`, кеш на сесію для кожної таблиці.
  4. Фільтр висновків до потрібних колонок (плюс табличні висновки, як-от дублікати повного рядка).
  5. `kpis.calculate_kpi`.

  Повертає `kpi`, `required_data` і `data_quality` (`status`: `PASS` / `WARN` / `FAIL` / `ERROR`, `warnings` із `severity` `confirmed` / `possible`).
- `check_data_quality(tables)`: для питань лише про якість даних.

Інше:
- `Orchestrator.ask(question)` повертає `OrchestratorReply(text, kpi_results, warnings)`. `kpi_results` — точні виходи KPI-функцій, і UI друкує їх поруч із текстом моделі.
- Для моделі розбивка обрізається до 30 рядків (`rows_total` / `rows_shown`), а повний результат лишається в `kpi_results`.
- Одне read-only з'єднання: сабагент створюється з `llm=False`, а KPI-запити йдуть через його `db.run_select`.
- Невідома метрика чи вимір, а також помилки БД повертаються моделі як `{"error": ...}` (секрети замасковано).

### 3.4. Дашборд (`dashboard.py`)

Лише шар представлення над Orchestrator: окремий екземпляр на кожну вкладку браузера, одне read-only з'єднання.

| Компонент | Джерело |
|---|---|
| Картки KPI, заголовок-інсайт | `Orchestrator.kpi_report(metric)`: перевірки DQ, потім SQL, без Gemini. Інсайт — шаблон зі значень тулів. |
| Позначка довіри на KPI | `kpi_report(...)["data_quality"]` (попередження, відфільтровані до колонок KPI) |
| Графік | `kpi_report(metric, by="specialization" \| "course")`. Дашборд лише сортує, обирає top-N і відсікає групи з менш ніж 500 записами. |
| Data health, діалог «View all checks» | `Orchestrator.data_health(tables)` з результатами Data Quality Subagent. Таблиці беруться з метрик і вимірів у `semantic_layer.yaml`. |
| Ask your data | `Orchestrator.ask()`: текст моделі, точні значення з `kpi_results`, попередження. Помилки 429 і 503 показуються як «Gemini is busy». |
| Визначення метрик | `orch.layer` (`semantic_layer.yaml`, лише читання) |

Тренд за місяцями, дельти «vs last year» і спарклайни з макета не реалізовано: у семантичному шарі немає часового виміру, а `enrolled_at` зберігається як text.

## 4. Функціональні вимоги

| ID | Вимога |
|---|---|
| FR-1 | Агент приймає запит природною мовою через веб-інтерфейс (`streamlit run app.py`) або термінал (`python main.py`). |
| FR-2 | LLM — Gemini через `google-genai`; модель задається `GEMINI_MODEL` (за замовчуванням `gemini-2.5-flash`). |
| FR-3 | Підключення до PostgreSQL лише через `DATABASE_URL` з `.env` або змінної оболонки. MCP застосунок не використовує. |
| FR-4 | Агент має 5 тулів (§5) і не має жодного способу виконати довільний SQL. |
| FR-5 | Кожен результат перевірки класифікується як PASS / FAIL / INFO з полем `basis` (§7). |
| FR-6 | Після кожної відповіді, якщо виконано хоча б одну перевірку, перезаписується `reports/data_quality_report.md` зі станом усієї сесії. |
| FR-7 | Для кожного дефекту агент вказує, систематичний він чи випадковий. |
| FR-8 | Питання до бізнесу агент виносить у блок `QUESTIONS:`, і вони потрапляють у розділ 3 звіту. |
| FR-9 | Повторна перевірка замінює попередній результат, а FAIL, що більше не відтворюється, видаляється. |
| FR-10 | Під час старту правила з `rules/` звіряються зі схемою, розбіжності показуються як попередження і в розділі «Зауваження до правил». |

## 5. Контракт тулів

Загальні правила для всіх тулів:
- назва таблиці нормалізується (`Public.Enrollments` → `enrollments`) і звіряється з whitelist, так само колонки;
- SQL будується через `psycopg.sql.Identifier`, значення передаються як параметри `%s`;
- результат містить **лише агрегати**; помилки повертаються як `{"error": "<текст>"}`, а не винятком;
- кожен виклик логується через `ToolContext.on_tool_call`.

| Тул | Вхід | Вихід (ключові поля) | Записує в `ResultStore` |
|---|---|---|---|
| `describe_table` | `table` | колонки (тип, nullable), constraints, `key_rules`, `date_like_text_columns`, `numeric_columns`, `range_rules` | факти: `columns`, `constraints`, `date_like_text` |
| `count_rows` | `table` | `row_count` | факт `rows` |
| `check_missing_values` | `table`, `columns?` | по колонках: `nulls`, `null_share`; для тексту `empty_strings`, `sentinel_values`, `edge_whitespace` | NULL в ID → `rule`; приховані пропуски → `heuristic`; зведення NULL → `INFO` |
| `check_duplicates` | `table`, `key_columns?` | `slices[]`: `full_row`, `id_column`, `row_without_id`, `natural_key` з `extra_rows`, `share`, `duplicate_groups`, `systematic`, `report_section`; `skipped[]` | по одному результату на зріз |
| `check_out_of_range` | `table`, `column?`, `min_value?`, `max_value?` | `checks[]`: `violations`, `share`, `min`, `max`, `top_violating_values` (≤ 5), `systematic`, `report_section` | по одному результату на правило |

### 5.1. Деталі перевірок

- **Сентинели:** `na`, `n/a`, `null`, `none`, `-`, `?`, `unknown`, `nan` (після `lower(btrim())`).
- **Дублікати:** «зайві рядки» = `count(*) - count(DISTINCT …)`. Для зрізів ID і природного ключа при FAIL додатково рахуються групи (`n_groups`, `min_size`, `max_size`).
- **Діапазони:** межі включні. Без `column` застосовуються всі правила таблиці. `min_value`/`max_value` без `column` дають помилку. Колонка має бути числовою.
- **Систематичність:**
  - діапазони: одне значення-порушник → «систематичний (заглушка)»; одне значення дає ≥ 80% порушень → «переважно систематичний»; інакше «різні значення»;
  - дублікати: усі групи однакового розміру → «систематичний (повторне завантаження)»; інакше «різний розмір груп».

## 6. Правила (`rules/`)

### 6.1. `rules/ranges.yaml`

```yaml
ranges:
  - {table: <table>, column: <numeric column>, min: <number>, max: <number>,
     basis: rule | assumption, note: "<підстава>"}
```

- Обов'язкова хоча б одна межа; `min ≤ max`; `basis` лише `rule` або `assumption`. Порушення формату → `RulesError`, старт зупиняється.
- `rule` означає доведене правило (порушення потрапляє в «Підтверджені»). `assumption` означає евристику з невідомою шкалою (порушення потрапляє в «Припущення»).

### 6.2. `rules/keys.yaml`

```yaml
keys:
  <table>: {id: <column>, natural: [<column>, ...]}
```

Потрібен хоча б `id` або `natural`.

### 6.3. Відсутні правила

Якщо файлів немає, у звіті пишеться «Правила: none», і працюють лише евристики. Правила не вигадуються: для колонки без правила агент просить межі в користувача.

## 7. Класифікація результатів

| `basis` | Джерело | FAIL потрапляє в |
|---|---|---|
| `rule` | `rules/*.yaml` з `basis: rule`, ID і природні ключі з `keys.yaml` | **1. Підтверджені проблеми** |
| `assumption` | `rules/ranges.yaml` з `basis: assumption` | **2. Можливі проблеми** |
| `heuristic` | евристики `validate-dataset` (повний рядок, рядок без ID, приховані пропуски) | **2. Можливі проблеми** |
| `user` | межі чи ключ із запиту користувача | **2. Можливі проблеми** |
| `info` | зведення NULL | лише розділи 0 і 4 |

- Класифікацію робить **код**, а не LLM. Модель не може перевести припущення в підтверджене.
- `ALL PASS = True`, якщо виконано хоча б одну перевірку і немає жодного FAIL.
- NULL самі по собі не є дефектом, крім NULL в ID-колонці.

## 8. Звіт `reports/data_quality_report.md`

Структура (як у `validate-dataset`), мова українська:

0. **Загальна картина:** таблиці (рядки, колонки, constraints, дати в text-колонках), NULL по колонках.
1. **Підтверджені проблеми:** умова, рядків і частка, факт, характер (систематичний чи ні), підстава, основа класифікації.
2. **Можливі проблеми (припущення):** та сама структура.
3. **Питання до бізнесу:** з блоку `QUESTIONS:`, без дублікатів.
4. **Результати перевірок:** таблиця `Check | Об'єкт | Правило | Очікувано | Факт | Статус`.
5. **Пріоритети:** Високий (підтверджене), Середній (припущення ≥ 1% рядків), Низький (інше); систематичні дефекти позначаються як відновлювані.
6. **Висновки агента:** запит → відповідь, для кожного запиту сесії.
7. **Зауваження до правил.**

У звіті немає рядка підключення, паролів, ID людей і вільного тексту.

## 9. Нефункціональні вимоги

### 9.1. Безпека SQL (усі три рівні обов'язкові)

| Рівень | Реалізація |
|---|---|
| 1. Валідатор | `sql_guard.assert_safe_select`: одна інструкція; починається з `SELECT`/`WITH`; заборонені DML/DDL/транзакційні ключові слова, `INTO`, `FOR UPDATE`, небезпечні функції (`pg_sleep`, `set_config`, `dblink`, `lo_*`, …). Коментарі й літерали прибираються перед перевіркою. |
| 2. З'єднання | `default_transaction_read_only=on`, `conn.read_only = True`, `statement_timeout = 30 s`, `connect_timeout = 10 s`, `fetchmany(1000)`, `rollback()` після кожного запиту. |
| 3. Ідентифікатори | Whitelist зі `schema.py` + `sql.Identifier`; значення лише через параметри. |

Рекомендовано підключатися користувачем БД, який має лише права на читання.

### 9.2. Секрети

- `.env` у `.gitignore`; у репозиторії тільки `.env.example` з плейсхолдерами.
- `.env` читається при кожному старті сесії й **не записується** в `os.environ`, тож виправлення `.env` підхоплюється без перезапуску. Змінні, експортовані в оболонці, мають пріоритет.
- Усі повідомлення про помилки проходять через `Settings.redact()`: прибираються API-ключ, `DATABASE_URL` і пароль.

### 9.3. Приватність

Тули повертають лише агрегати. Показувати значення дозволено лише для числових показників (`top_violating_values`) і фіксованого списку сентинелів.

### 9.4. Надійність

- Ліміт автоматичних викликів тулів — 25 на запит (`MAX_TOOL_CALLS`).
- Помилки Gemini чи БД під час запиту не завершують сесію: користувач бачить повідомлення й може продовжити.
- У Streamlit кожна вкладка браузера має власну сесію. Запити серіалізуються `tools.use(ctx)` (реентерабельний `RLock`, відновлює попередній контекст), бо контекст тулів — глобальний для модуля. Тому оркестратор може викликати сабагент зсередини свого тула в тому ж потоці.

## 10. Критерії приймання

| # | Критерій | Як перевіряється |
|---|---|---|
| AC-1 | Будь-який не-SELECT блокується | `tests/test_sql_guard.py` |
| AC-2 | Невідомі таблиці й колонки відхиляються до виконання SQL | `tests/test_tools.py` |
| AC-3 | Межі передаються параметрами, ідентифікатори — в лапках | `tests/test_tools.py` |
| AC-4 | Класифікація 1 / 2 іде за `basis`; ALL PASS коректний | `tests/test_report.py`, `tests/test_tools.py` |
| AC-5 | Правила з репозиторію валідні й відповідають схемі | `tests/test_rules.py` |
| AC-6 | Тули мають коректні Gemini-декларації | `test_tools_have_gemini_declarations` |
| AC-7 | Секрети не потрапляють у звіт і помилки; зміни `.env` підхоплюються | `tests/test_report.py`, `tests/test_session.py`, `tests/test_config.py` |
| AC-8 | Веб-інтерфейс: запит → відповідь, тули, звіт, помилки | `tests/test_app.py` (AppTest з фейковою сесією) |
| AC-9 | Згенерований SQL синтаксично валідний для PostgreSQL | разова перевірка парсером `pglast` (81 запит) |
| AC-11 | Сабагент повертає JSON-сумісний результат лише поточного виклику; помилки не піднімаються | `tests/test_subagent.py` |
| AC-12 | KPI рахуються SQL за визначеннями з `semantic_layer.yaml`; розбіжність коду і YAML ламає тест | `tests/test_kpis.py` |
| AC-13 | Оркестратор перевіряє якість потрібних даних до розрахунку KPI, фільтрує попередження до колонок KPI і повертає точні значення тулів | `tests/test_orchestrator.py` |
| AC-14 | Дашборд не рахує KPI і не має власної DQ-логіки: імпортує лише `dq_agent.orchestrator` / `dq_agent.subagent` | `tests/test_dashboard.py` |
| AC-10 | Наскрізний запуск на реальній БД і Gemini створює звіт без секретів і ID | **ручна перевірка користувачем**, ще не виконана |

Команда для автоматичних тестів: `pytest` (без БД і Gemini).

## 11. Обмеження та наступні кроки

| Пріоритет | Що | Джерело рецепта |
|---|---|---|
| 1 | Тул `check_date_formats`: класифікація форматів у text-датах (`enrolled_at`, `completed_at`, `review_date`); приведення типу лише в межах свого класу | `db-quality-check` Step 1 «Formats», lesson3 R2 |
| 2 | Тул `check_logical_rules`: `completed_at < enrolled_at`, `last_week_reached > n_weeks`, `progress_pct` ≠ формула, `is_certified` ⟂ `funnel_state` | `db-quality-check` «Logical contradictions», lesson3 R3–R6 |
| 3 | Тул `check_references`: orphans між `enrollments`, `users`, `dim_course`, `payments`, `weekly_activity`, `review_link`, `reviews` | `db-quality-check` «Cross-table integrity» |
| 4 | Перевірка перекриття дефектів через перетин множин ключів | `validate-dataset` «Перекриття дефектів» |
| 5 | Об'єднання демографічних груп < 10 в `other (<10)` для розподілів у `users` | `db-quality-check` Safety contract |

Відомі обмеження v1:
- Звіт один на сесію й перезаписується; історії запусків немає.
- Контекст тулів — глобальний для модуля. Запити з кількох вкладок виконуються послідовно.
- Шкали `quiz_score`, `pct_low`, `mean_stars`, `review_score` не задокументовані, тому їхні правила мають статус `assumption` до відповіді бізнесу.
