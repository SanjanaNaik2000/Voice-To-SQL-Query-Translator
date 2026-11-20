"""
test_nl_features.py -- tests for ORDER BY / LIMIT / DISTINCT / OR / NOT / LIKE / >= / <= phrasings.
Run:  python test_nl_features.py     (uses a throw-away database; nothing of yours is touched)
"""
import os
import tempfile

from database_service import DatabaseService
from seed_demo_data import seed
from translator import Translator
from validator import ShadowSchema, Validator

failed = 0
db_path = os.path.join(tempfile.mkdtemp(), "nl.db")
seed(db_path)
db = DatabaseService(db_path)
schema = db.get_schema()
translator = Translator(schema, db.get_text_values(schema), db_service=db)
shadow = ShadowSchema()
shadow.rebuild(db.get_ddl())
validator = Validator(shadow)


def check(prompt, expected_sql, expected_rows=None):
    """The generated SQL must match, pass the validator and (optionally) return N rows."""
    global failed
    tr = translator.translate(prompt)
    ok = tr.sql == expected_sql
    rows = None
    if ok:
        res = db.execute(validator.validate(tr.sql).sql)
        rows = len(res.rows)
        ok = expected_rows is None or rows == expected_rows
    failed += not ok
    print("PASS " if ok else "FAIL ", prompt, "" if ok else f"\n      got      {tr.sql or tr.error}  ({rows} rows)\n      expected {expected_sql}  ({expected_rows} rows)")


E, P = '"employees"', '"products"'
# ---- ORDER BY / LIMIT
check("top 3 highest paid employees", f'SELECT * FROM {E} ORDER BY "salary" DESC LIMIT 3;', 3)
check("top 5 most expensive products", f'SELECT * FROM {P} ORDER BY "price" DESC LIMIT 5;', 5)
check("show 3 cheapest products", f'SELECT * FROM {P} ORDER BY "price" ASC LIMIT 3;', 3)
check("bottom 3 products by price", f'SELECT * FROM {P} ORDER BY "price" ASC LIMIT 3;', 3)
check("top 3 products by stock", f'SELECT * FROM {P} ORDER BY "stock" DESC LIMIT 3;', 3)
check("first 5 employees", f'SELECT * FROM {E} LIMIT 5;', 5)
check("the oldest employee", f'SELECT * FROM {E} ORDER BY "age" DESC LIMIT 1;', 1)
check("youngest employee", f'SELECT * FROM {E} ORDER BY "age" ASC LIMIT 1;', 1)
check("list employees sorted by salary", f'SELECT * FROM {E} ORDER BY "salary" ASC;', 12)
check("employees sorted by age descending", f'SELECT * FROM {E} ORDER BY "age" DESC;', 12)
check("show products ordered by price from highest to lowest", f'SELECT * FROM {P} ORDER BY "price" DESC;', 8)
check("show name and salary of employees sorted by salary",
      f'SELECT "name", "salary" FROM {E} ORDER BY "salary" ASC;', 12)
check("employees older than 30 sorted by salary descending",
      f'SELECT * FROM {E} WHERE "age" > 30 ORDER BY "salary" DESC;', 9)
check("top 3 employees in sales by salary",
      f'SELECT * FROM {E} WHERE "department" = \'Sales\' ORDER BY "salary" DESC LIMIT 3;', 3)
check("average salary per department sorted by department",
      f'SELECT "department", AVG("salary") FROM {E} GROUP BY "department" ORDER BY "department" ASC;', 4)
check("highest price item", f'SELECT MAX("price") FROM {P};', 1)           # still an aggregate
# ---- DISTINCT
check("unique departments", f'SELECT DISTINCT "department" FROM {E};', 4)
check("distinct cities of employees", f'SELECT DISTINCT "city" FROM {E};', 3)
check("how many unique departments", f'SELECT COUNT(DISTINCT "department") FROM {E};', 1)
# ---- OR / NOT / comparisons
check("show employees in london or paris", f'SELECT * FROM {E} WHERE "city" IN (\'London\', \'Paris\');', 9)
check("employees older than 40 or paid under 60000",
      f'SELECT * FROM {E} WHERE "age" > 40 OR "salary" < 60000;', 6)
check("employees not in london", f'SELECT * FROM {E} WHERE "city" != \'London\';', 7)
check("employees not in london or paris", f'SELECT * FROM {E} WHERE "city" NOT IN (\'London\', \'Paris\');', 3)
check("employees with salary at least 90000", f'SELECT * FROM {E} WHERE "salary" >= 90000;', 3)
check("employees with age at most 30", f'SELECT * FROM {E} WHERE "age" <= 30;', 3)
check("employees with salary no more than 60000", f'SELECT * FROM {E} WHERE "salary" <= 60000;', 3)
check("employees between 30 and 40 years old", f'SELECT * FROM {E} WHERE "age" BETWEEN 30 AND 40;', 6)
# ---- pattern matching
check("employees whose name starts with A", f'SELECT * FROM {E} WHERE "name" LIKE \'A%\';', 1)
check("employees whose name contains an", f'SELECT * FROM {E} WHERE "name" LIKE \'%an%\';', 2)
check("products whose product name ends with r", f'SELECT * FROM {P} WHERE "product_name" LIKE \'%r\';', 2)
check("employees with name not starting with a", f'SELECT * FROM {E} WHERE "name" NOT LIKE \'a%\';', 11)
# ---- describe / structure of a table
DESC = 'SELECT name AS "column", type AS "type", "notnull" AS "not_null", pk AS "primary_key" FROM pragma_table_info(\'%s\');'
check("describe employees", DESC % "employees", 6)
check("show structure of products", DESC % "products", 5)
check("what columns does orders have", DESC % "orders", 5)
check("show columns name and age of employees", f'SELECT "name", "age" FROM {E};', 12)     # still a normal SELECT
# ---- unchanged behaviour
check("how many products are in stock", f'SELECT COUNT(*) FROM {P};', 1)
check("show products cheaper than 100", f'SELECT * FROM {P} WHERE "price" < 100;', 3)
# ---- runs last: it really deletes rows from the throw-away database
check("delete employees older than 50 or younger than 26", f'DELETE FROM {E} WHERE "age" > 50 OR "age" < 26;')

print("\nAll passed" if not failed else f"\n{failed} FAILED")
raise SystemExit(1 if failed else 0)
