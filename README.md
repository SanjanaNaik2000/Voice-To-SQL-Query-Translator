# DBChatPro 2.0 (Python) — chat with a SQLite database, offline, no AI

Python/Flask re-engineering of the Blazor `dbchatprov2` workflow. No LLM, no neural network,
no network access: natural language is translated with TF-IDF vector-space maths (scikit-learn + numpy).

## Run
    python -m venv venv
    venv\Scripts\activate            (macOS/Linux: source venv/bin/activate)
    pip install -r requirements.txt
    python seed_demo_data.py         (optional demo tables: employees, products, orders)
    python app.py                    -> http://127.0.0.1:5000

Voice (optional, fully offline): `pip install sounddevice faster-whisper`. The Whisper model (`base.en`, ~150 MB) downloads itself the first time you press Speak, then works without internet. Choose a different size with the environment variable `DBCHAT_WHISPER_MODEL` (`tiny.en`, `base.en`, `small.en`). Vosk (`sprc download vosk`) is only used as a fallback. Test your microphone with `python mic_check.py`.

History and favorites are saved in `history.json` (delete single entries with the ✕ button).
Test:  `python test_pipeline.py`

The app works on `target_database.db` next to `app.py` (created empty if missing).
Open the same file in DB Browser for SQLite: INSERT / DELETE / CREATE are committed instantly.

## Blazor -> Python map
| Blazor                          | Python                                              |
|---------------------------------|-----------------------------------------------------|
| `Home.razor`, `MainLayout.razor`| `app.py` + `templates/index.html` + `static/style.css` |
| `OpenAIService.cs`              | `translator.py` (vector space, no AI)               |
| *(none)*                        | `validator.py` (pre-execution gate)                 |
| `DatabaseService.cs`            | `database_service.py`                               |
| `HistoryService.cs`             | `history_service.py`                                |
| *(speech: Whisper, Vosk fallback)* | `voice_service.py`                               |

## Pipeline
prompt -> `Translator` -> SQL -> `Validator` (halt = *Validation Exception*, DB untouched) -> `DatabaseService` -> grid.

## Example prompts (demo data)
    show all employees                          how many employees in engineering
    employees older than 30                     average salary per department
    staff paid over ninety thousand             count employees per department having more than 3
    show products cheaper than 100              products with price between 10 and 500
    -- ordering, ranking, filters
    top 3 highest paid employees                show 3 cheapest products
    employees sorted by age descending          the oldest employee
    employees in london or paris                employees not in london
    employees older than 40 or paid under 60000 employees with salary at least 90000
    unique departments                          how many distinct cities
    employees whose name starts with A          products whose name ends with r
    -- structure of a table (also on the in-app Help page)
    describe employees                          what columns does products have
    -- multi-table queries & joins
    show orders and products                    show customer and price from orders and products
    orders with products where price > 500      count orders by category from orders and products
    -- changing data (DML)
    create table pets with columns name, age, weight
    insert into pets name Rex age 4 weight 12.5
    update employees set salary = 75000, city = 'Berlin' where name is Bob
    increase salary by 5000 for employees in sales
    delete pets older than 10
    -- changing structure (DDL)
    alter table employees add column phone text
    alter table employees rename column city to town
    rename table orders to purchases
    -- irreversible (asks you to type the name to confirm)
    alter table employees drop column phone     drop table pets

## Safety rules
* Every statement (typed or generated) passes the validation layer before the database sees it.
* UPDATE and DELETE must carry a condition; otherwise they are halted.
* DROP TABLE and ALTER ... DROP COLUMN are parked behind a confirmation card: nothing runs
  until you type the table / column name. Cancel, or any new prompt, discards it.
* Only SELECT, INSERT, UPDATE, DELETE, CREATE TABLE, ALTER TABLE and DROP TABLE are allowed.

## Teaching it your vocabulary
`translator.py` -> `CONCEPTS`: neighbourhoods of related words. A column or table whose name lands
on a member inherits the rest ("older" finds a column called `age`). Add your own lines there.

## Notes & Capabilities
Supports multi-table detection with dynamic relational INNER JOINs (via live SQLite FK catalogs
and VSM semantic anchors), full DDL (ALTER ADD/DROP, DROP TABLE), full DML (multi-set UPDATE, INSERT, DELETE),
and table-qualified column references (`"table"."column"`) to prevent ambiguous reference faults.
Text filters ground against indexed column values or quoted literals. Ambiguous requests are reported,
not guessed.
