# Course Pulse: аналітичний агент для даних курсу

Course Pulse відповідає на аналітичні питання про дані курсу (Coursera, PostgreSQL) і показує, чи можна довіряти цифрам. Він складається з трьох частин:

- **KPI-тули** рахують метрики SQL-запитами за затвердженими визначеннями з `semantic_layer.yaml`.
- **Data Quality Subagent** перевіряє дані, з яких рахуються KPI: пропуски, дублікати, значення поза межами.
- **Orchestrator** (Gemini) розуміє питання, обирає метрику й пояснює результат. Сам він цифр не рахує.

Головний інтерфейс — дашборд у Streamlit. Доступ до БД лише на читання.

![Як Course Pulse відповідає на питання про KPI: Orchestrator обирає метрику, get_kpi у Python бере визначення з semantic_layer.yaml, перевіряє дані через Data Quality Subagent і рахує KPI SQL-запитом через read-only доступ до БД](docs/architecture.svg)

Як це працює на прикладі питання «What is the completion rate by specialization?»:

1. **Orchestrator (Gemini)** розуміє питання й обирає метрику `completion_rate` і вимір `specialization`. Далі він лише викликає `get_kpi`.
2. **Визначення** береться з `semantic_layer.yaml`: завершеним вважається запис, у якому `completed_at IS NOT NULL`.
3. **Потрібні дані**: колонки `enrollments` і `dim_course`, з яких рахуватиметься KPI.
4. **Data Quality Subagent** перевіряє саме ці колонки: пропуски, дублікати, значення поза межами з `rules/*.yaml`.
5. **KPI-тули** рахують значення одним SQL-запитом.
6. Відповідь містить точні значення з SQL, попередження про якість даних і визначення метрики. Модель їх лише пояснює.

Усі запити проходять `sql_guard` (лише один SELECT), whitelist схеми й read-only сесію.

Повна специфікація: [specs/data-quality-agent.md](specs/data-quality-agent.md).

## Швидкий старт

### 1. Що потрібно

- Python 3.11 або новіший
- доступ до PostgreSQL-бази курсу (рядок підключення `postgresql://…`)
- ключ Gemini API з [Google AI Studio](https://aistudio.google.com/apikey); безкоштовного тарифу достатньо

### 2. Встановлення

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

### 3. Налаштування `.env`

| Змінна | Опис |
|---|---|
| `GEMINI_API_KEY` | ключ Google AI Studio |
| `GEMINI_MODEL` | необов'язково, за замовчуванням `gemini-2.5-flash` |
| `DATABASE_URL` | `postgresql://USER:PASSWORD@HOST:5432/DBNAME` |

`.env` ігнорується git. Пароль і `DATABASE_URL` не потрапляють у код, звіти чи повідомлення про помилки. Краще підключатися користувачем БД, який має лише права на читання.

### 4. Перевірка встановлення (необов'язково)

```bash
python -m pytest
```

Тести не потребують ні БД, ні Gemini. Якщо всі пройшли, код і залежності встановлено правильно.

### 5. Запуск дашборду

```bash
source .venv/bin/activate
streamlit run dashboard.py
```

Відкрийте http://localhost:8501. Перше завантаження триває кілька секунд: агент підключається до БД, перевіряє якість даних у `enrollments` і `dim_course` та рахує KPI. Далі результати кешуються для вкладки браузера. Зупинити сервер: `Ctrl+C` у терміналі.

### Що на екрані

| Блок | Що показує | Звідки дані |
|---|---|---|
| Головний інсайт | одне речення про стан бізнесу | значення KPI-тулів |
| Картки KPI | Total enrollments, Completion rate, Average progress; позначка «Verified» або кількість проблем з даними; визначення метрики | KPI-тули + Data Quality Subagent |
| Графік | метрика за спеціалізацією або курсом; перемикачі метрики й виміру; фільтр «500+ enrollments» | KPI-тули |
| Ask your data | відповідь агента, точні значення, попередження, «How this was calculated» | Orchestrator |
| Data health | скільки перевірок пройдено, головні проблеми, «View all checks» | Data Quality Subagent |

Кнопка **Refresh data** починає нову сесію: заново запускає перевірки й перераховує KPI.

Приклади питань для «Ask your data»:

- `Which specializations complete most?`
- `What is the average progress by course?`
- `Яка частка завершених курсів?`
- `Can I trust the completion rate?`

Агент відповідає лише про метрики з `semantic_layer.yaml`. На питання на кшталт «What is our revenue?» він відповість, що такої метрики немає, і нічого не вигадуватиме.

## Інші способи запуску

| Команда | Для чого |
|---|---|
| `streamlit run dashboard.py` | **дашборд Course Pulse** (основний продукт) |
| `python analyst.py` | аналітичні питання в терміналі: той самий Orchestrator, точні значення друкуються окремо від тексту моделі |
| `streamlit run app.py` | чат про якість даних (Data Quality Agent) зі звітом `reports/data_quality_report.md` |
| `python main.py` | той самий чат про якість даних у терміналі |

Приклади запитів для чату про якість даних:

- `перевір таблицю enrollments` — усі перевірки для таблиці
- `чи є дублікати в weekly_activity?`
- `які значення поза діапазоном у payments?`
- `перевір, чи quiz_score у weekly_activity між 0 і 10` — власні межі (вважаються припущенням)

У терміналі `exit` або `quit` завершує роботу. Звіт про якість даних перезаписується після кожної перевірки, зокрема й тих, які запускає дашборд.

## Якщо щось не працює

| Повідомлення | Що робити |
|---|---|
| `Configuration error: Missing environment variables` | Створіть `.env` з `.env.example` і заповніть `GEMINI_API_KEY` і `DATABASE_URL`, потім перезавантажте сторінку. |
| `Cannot connect to the database` | Перевірте `DATABASE_URL`, мережу чи VPN і те, що БД доступна з вашого комп'ютера. |
| `Gemini is busy right now` | Безкоштовний тариф Gemini дозволяє близько 5 запитів на хвилину, а одне питання використовує 2–3. Зачекайте хвилину. KPI і Data health працюють без Gemini. |
| `ModuleNotFoundError: No module named 'dq_agent'` під час тестів | Запускайте `python -m pytest`, а не просто `pytest`. |
| Порт 8501 зайнятий | `streamlit run dashboard.py --server.port 8502` |

## Використання з коду

KPI і перевірки без Gemini:

```python
from dq_agent.orchestrator import Orchestrator

orch = Orchestrator.create()
report = orch.kpi_report("completion_rate", by="specialization")
report["kpi"]["rows"]              # значення з SQL
report["data_quality"]["status"]   # PASS / WARN / FAIL / ERROR
orch.close()
```

Лише перевірки якості даних (Data Quality Subagent):

```python
from dq_agent.subagent import DataQualitySubagent

dq = DataQualitySubagent.create(llm=False)
result = dq.check_tables(["enrollments"])
result.to_dict()                   # JSON для іншого агента
dq.close()
```

Контракти описано в `specs/data-quality-agent.md`, розділи 3.1–3.4.

## Тули

| Тул | Що перевіряє |
|---|---|
| `describe_table` | колонки, типи, ключі, текстові колонки з датами, правила для таблиці |
| `count_rows` | точна кількість рядків |
| `check_missing_values` | NULL; для тексту також порожні рядки, сентинели (`n/a`, `unknown`, …) і пробіли на краях |
| `check_duplicates` | 4 зрізи: повний рядок, ID, рядок без ID, природний ключ |
| `check_out_of_range` | числові значення поза межами з `rules/ranges.yaml` або з запиту; показує, чи дефект систематичний |

Тули повертають тільки агрегати: жодних сирих рядків, ID людей чи вільного тексту.

## Правила (`rules/`)

- `rules/ranges.yaml` — допустимі діапазони числових колонок. `basis: rule` → порушення потрапляє в «Підтверджені проблеми»; `basis: assumption` → у «Можливі проблеми».
- `rules/keys.yaml` — ID-колонки та природні ключі для перевірки дублікатів.

Щоб додати правило, допишіть рядок у YAML, наприклад:

```yaml
- {table: payments, column: amount_usd, max: 10000, basis: assumption,
   note: "Платежі понад 10 000 USD підозрілі."}
```

Під час старту агент перевіряє, що всі колонки з правил існують і мають числовий тип, і попереджає про помилки.

## Безпека SQL

1. `dq_agent/sql_guard.py` пропускає лише одну інструкцію `SELECT` / `WITH … SELECT`, без DML/DDL і небезпечних функцій.
2. З'єднання працює в режимі `default_transaction_read_only=on` з `statement_timeout` 30 с і лімітом 1000 рядків.
3. Назви таблиць і колонок від LLM звіряються з `information_schema` і підставляються через `psycopg.sql.Identifier`; значення передаються як параметри.

Рекомендовано також підключатися користувачем БД, який має лише права на читання.

## Тести

```bash
python -m pytest                                   # без БД і без Gemini
python -m pytest tests/test_sql_guard.py -k blocks  # окремий набір
```
