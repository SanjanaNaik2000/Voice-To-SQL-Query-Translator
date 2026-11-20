"""
End-to-end tests.  Run:  python test_pipeline.py
Uses a throw-away database file; your real target_database.db is never touched.
"""
import os
import sqlite3
import sys
import tempfile

from app import create_app
from seed_demo_data import seed

failed = 0


def check(name, cond, extra=""):
    global failed
    failed += not cond
    print(("PASS " if cond else "FAIL "), name, ("-> " + str(extra)) if (extra and not cond) else "")


tmp = os.path.join(tempfile.mkdtemp(), "t.db")
seed(tmp)
app = create_app(tmp)
ws = app.config["WORKSPACE"]
client = app.test_client()


def ask(prompt):
    client.post("/query", data={"action": "run", "prompt": prompt})
    return client.get("/").get_data(as_text=True)


def disk(sql, *args):
    """Read the physical file through an independent connection."""
    c = sqlite3.connect(tmp)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


# ---------------------------------------------------------------- 1. translation -> SQL
SQL_CASES = [
    ("show all employees", 'SELECT * FROM "employees";'),
    ("how many employees are there", 'SELECT COUNT(*) FROM "employees";'),
    ("average salary per department", 'SELECT "department", AVG("salary") FROM "employees" GROUP BY "department";'),
    ("employees older than 30", 'SELECT * FROM "employees" WHERE "age" > 30;'),
    ("show names of employees in London", 'SELECT "name" FROM "employees" WHERE "city" = \'London\';'),
    ("count employees per department having more than 3",
     'SELECT "department", COUNT(*) FROM "employees" GROUP BY "department" HAVING COUNT(*) > 3;'),
    ("average salary per department above 70000",
     'SELECT "department", AVG("salary") FROM "employees" GROUP BY "department" HAVING AVG("salary") > 70000;'),
    ("show products cheaper than 100", 'SELECT * FROM "products" WHERE "price" < 100;'),
    ("products with price between 10 and 500", 'SELECT * FROM "products" WHERE "price" BETWEEN 10 AND 500;'),
    ("staff paid over ninety thousand", 'SELECT * FROM "employees" WHERE "salary" > 90000;'),
    ("highest price item", None),   # 'item' -> products table: MAX(price)
    ("total salary of staff in engineering", 'SELECT SUM("salary") FROM "employees" WHERE "department" = \'Engineering\';'),
]
for prompt, expected in SQL_CASES:
    tr = ws.translator.translate(prompt)
    if expected is None:
        check(f"translate: {prompt!r}", tr.sql == 'SELECT MAX("price") FROM "products";', tr.sql or tr.error)
    else:
        check(f"translate: {prompt!r}", tr.sql == expected, tr.sql or tr.error)

# ---------------------------------------------------------------- 2. dashboard rendering
html = ask("average salary per department")
check("status pill rendered", "🟢 SQLite Connected" in html)
check("emerald SQL card shows generated SQL", 'class="card sql-card"' in html and "AVG(&#34;salary&#34;)" in html)
check("grid has headers from cursor metadata", "<th>department</th>" in html and "<th>AVG(&#34;salary&#34;)</th>" in html)
check("history card added to sidebar", 'value="average salary per department"' in html and "badge-select" in html)

# ---------------------------------------------------------------- 3. validation layer
html = ask("show names")                                   # ambiguous table -> translation exception, no DB hit
check("translation exception card", "Translation Exception" in html and "exc-translation" in html)

client.post("/sql", data={"sql": "SELECT FROM employees"})
html = client.get("/").get_data(as_text=True)
check("Validation Exception card (no attributes)", "Validation Exception" in html and "no attributes" in html)

client.post("/sql", data={"sql": "SELECT department, AVG(salary) FROM employees WHERE COUNT(*) > 2"})
html = client.get("/").get_data(as_text=True)
check("aggregate in WHERE -> HAVING + GROUP BY enforced",
      "GROUP BY department HAVING COUNT(*) &gt; 2" in html and "moved from WHERE to HAVING" in html, html[-200:])

client.post("/sql", data={"sql": "DELETE FROM employees"})
html = client.get("/").get_data(as_text=True)
check("unbounded DELETE halted, rows intact", "Validation Exception" in html
      and disk("SELECT COUNT(*) FROM employees")[0][0] == 12)

client.post("/sql", data={"sql": "DROP TABLE employees"})
check("DROP from the SQL Editor is parked, never run directly",
      "Confirm destructive action" in client.get("/").get_data(as_text=True)
      and disk("SELECT COUNT(*) FROM employees")[0][0] == 12)
client.post("/cancel")

# ---------------------------------------------------------------- 4. DDL / DML commit to disk
n0 = len(ws.history.get_queries("target_database.db"))
html = ask("create table pets with columns name, age, weight")
check("CREATE committed to the file", disk("SELECT sql FROM sqlite_master WHERE name='pets'") != [])
check("success card + empty grid preview", "Committed" in html and "<th>name</th>" in html)

html = ask("insert into pets name Rex age 4 weight 12.5")
check("INSERT committed", disk("SELECT name, age, weight FROM pets") == [("Rex", 4, 12.5)])
amb = ws.translator.translate("add a pet called Luna age 2")
check("genuinely ambiguous prompt is reported, not guessed", amb.sql is None and "Ambiguous table" in (amb.error or ""))
ask("insert into pets name Luna age 2")
check("2nd INSERT via new schema (vector space rebuilt)", len(disk("SELECT * FROM pets")) == 2, disk("SELECT * FROM pets"))

ask("delete pets older than 3")
check("DELETE committed", disk("SELECT name FROM pets") == [("Luna",)], disk("SELECT name FROM pets"))

ask("add a new employee called Max Power in Sales earning 70000")
check("INSERT with grounded values", disk("SELECT name, department, salary FROM employees WHERE name='Max Power'")
      == [("Max Power", "Sales", 70000.0)])
ask("how many employees in sales")
check("new row visible to value index", "<td class=\"num\">5</td>" in client.get("/").get_data(as_text=True))

# ---------------------------------------------------------------- 5. database exception (orange box)
client.post("/sql", data={"sql": "CREATE TABLE u (id INTEGER PRIMARY KEY, v TEXT)"})
client.post("/sql", data={"sql": "INSERT INTO u VALUES (1, 'a')"})
client.post("/sql", data={"sql": "INSERT INTO u VALUES (1, 'b')"})        # passes validation, fails at runtime
html = client.get("/").get_data(as_text=True)
check("Database System Exception box", "exc-database" in html and "UNIQUE" in html.upper())
check("failed statement left data unchanged", disk("SELECT COUNT(*) FROM u")[0][0] == 1)

# ---------------------------------------------------------------- 6. history / favorites / pager
hist = ws.history.get_queries("target_database.db")
check("history only records executed prompts", len(hist) > n0 and all(h.query for h in hist))
check("failed prompts not in history", "show names" not in [h.query for h in hist])
client.post("/query", data={"action": "favorite", "prompt": "average salary per department"})
check("favorite saved", len(ws.history.get_favorites("target_database.db")) == 1)

for i in range(30):
    ws.db.execute(f"INSERT INTO products VALUES ({100 + i}, 'bulk{i}', 'Bulk', {i}, {i})")
ask("show all products")
page2 = client.get("/?page=2").get_data(as_text=True)
check("pager works", "Page 2 of 2" in page2 and "bulk" in page2)

# ---------------------------------------------------------------- 7. HTML escaping
client.post("/sql", data={"sql": "SELECT '<script>alert(1)</script>' AS x"})
html = client.get("/").get_data(as_text=True)
check("cell values are HTML-escaped", "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html)

# ---------------------------------------------------------------- 8. UPDATE
def col(sql, *a):
    return [r[0] for r in disk(sql, *a)]


html = ask("update employees set salary to 70000 where name is Bob")
check("UPDATE (absolute) committed", disk("SELECT salary FROM employees WHERE name='Bob'") == [(70000.0,)])
check("UPDATE success card + row count", "row(s) updated" in html and "badge-update" in html)
check("UPDATE touched only the filtered row",
      disk("SELECT name FROM employees WHERE salary=70000 ORDER BY name") == [("Bob",), ("Max Power",)])   # Max Power was inserted earlier

before = dict(disk("SELECT name, salary FROM employees WHERE department='Sales'"))
ask("increase salary by 5000 for employees in sales")
after = dict(disk("SELECT name, salary FROM employees WHERE department='Sales'"))
check("UPDATE (relative) raised every Sales salary by 5000",
      all(after[k] == before[k] + 5000 for k in before) and len(before) >= 4, (before, after))

ask("give Bob a raise of 5000")
check("'give Bob a raise of 5000' -> UPDATE", disk("SELECT salary FROM employees WHERE name='Bob'") == [(75000.0,)])
ask("change the city of Alice to Paris")
check("UPDATE text value (value is not mistaken for a filter)", disk("SELECT city FROM employees WHERE name='Alice'") == [("Paris",)])
ask("update employees set department to Marketing where age over 50 and city is London")
check("UPDATE with two conditions", disk("SELECT department FROM employees WHERE name='Frank'") == [("Marketing",)])

client.post("/sql", data={"sql": "UPDATE employees SET salary = 1"})
html = client.get("/").get_data(as_text=True)
check("UPDATE without WHERE halted by the validator",
      "Validation Exception" in html and disk("SELECT COUNT(*) FROM employees WHERE salary=1")[0][0] == 0)

# ---------------------------------------------------------------- 9. ALTER (non-destructive)
html = ask("add column phone to employees")
check("ALTER ADD COLUMN committed", "phone" in col("SELECT name FROM pragma_table_info('employees')"))
check("schema tree refreshed with the new column", "<li>phone" in html and "<th>phone</th>" in html)

ask("rename column city to town in employees")
cols = col("SELECT name FROM pragma_table_info('employees')")
check("ALTER RENAME COLUMN committed", "town" in cols and "city" not in cols, cols)
ask("rename table orders to purchases")
check("ALTER RENAME TABLE committed", disk("SELECT 1 FROM sqlite_master WHERE name='purchases'") != []
      and disk("SELECT 1 FROM sqlite_master WHERE name='orders'") == [])
ask("how many purchases are there")
check("vector space rebuilt after the rename", "<td class=\"num\">8</td>" in client.get("/").get_data(as_text=True))

# ---------------------------------------------------------------- 10. DROP / DROP COLUMN need confirmation
n_hist = len(ws.history.get_queries("target_database.db"))
html = ask("drop table products")
check("DROP is parked, not executed", "Confirm destructive action" in html and "waiting for confirmation" in html
      and disk("SELECT 1 FROM sqlite_master WHERE name='products'") != [])
check("confirmation card names the table and row count", "permanently delete the table" in html and "row(s)" in html)
check("pending DROP is not in history yet", len(ws.history.get_queries("target_database.db")) == n_hist)

client.post("/confirm", data={"confirm": "wrong"})
html = client.get("/").get_data(as_text=True)
check("wrong confirmation text refused, table kept", "did not match" in html
      and disk("SELECT 1 FROM sqlite_master WHERE name='products'") != [] and "Confirm destructive action" in html)

client.post("/confirm", data={"confirm": "products"})
html = client.get("/").get_data(as_text=True)
check("DROP TABLE executed after confirmation", disk("SELECT 1 FROM sqlite_master WHERE name='products'") == []
      and "Table dropped" in html and "badge-drop" in html)
check("dropped table removed from the vector space", "products" not in ws.translator.tables)
check("confirmed DROP recorded in history", "drop table products" in [h.query for h in ws.history.get_queries("target_database.db")])

ask("drop table purchases")
client.post("/cancel")
html = client.get("/").get_data(as_text=True)
check("Cancel keeps the table", disk("SELECT 1 FROM sqlite_master WHERE name='purchases'") != [] and "Cancelled" in html)

ask("drop column phone from employees")
check("DROP COLUMN is parked too", "phone" in col("SELECT name FROM pragma_table_info('employees')")
      and "Confirm destructive action" in client.get("/").get_data(as_text=True))
client.post("/confirm", data={"confirm": "phone"})
check("DROP COLUMN executed after confirmation",
      "phone" not in col("SELECT name FROM pragma_table_info('employees')"))

client.post("/sql", data={"sql": "DROP TABLE u"})                          # SQL Editor goes through the same gate
check("SQL Editor DROP also needs confirmation", disk("SELECT 1 FROM sqlite_master WHERE name='u'") != []
      and "Confirm destructive action" in client.get("/").get_data(as_text=True))
client.post("/confirm", data={"confirm": "u"})
check("SQL Editor DROP executed after confirmation", disk("SELECT 1 FROM sqlite_master WHERE name='u'") == [])

client.post("/sql", data={"sql": "DROP TABLE ghost"})
check("DROP of a missing table halted", "no such table" in client.get("/").get_data(as_text=True))
client.post("/sql", data={"sql": "DROP INDEX x"})
check("DROP INDEX not permitted", "Only DROP TABLE is permitted" in client.get("/").get_data(as_text=True))

ask("drop table purchases")
ask("show all employees")
check("a new prompt discards a pending confirmation", "Confirm destructive action" not in client.get("/").get_data(as_text=True)
      and disk("SELECT 1 FROM sqlite_master WHERE name='purchases'") != [])

# ---------------------------------------------------------------- 11. snake_case names typed exactly (regression)
ask("create table temp_notes with columns note, priority")
ask("drop table temp_notes")
check("exact snake_case table name is matched in a multi-table schema",
      "Confirm destructive action" in client.get("/").get_data(as_text=True)
      and disk("SELECT 1 FROM sqlite_master WHERE name='temp_notes'") != [])
client.post("/cancel")
ask("insert into temp_notes note call_bob priority high")
check("snake_case table accepts INSERT", disk("SELECT note, priority FROM temp_notes") == [("call_bob", "high")],
      disk("SELECT * FROM temp_notes"))

print("\nAll passed" if not failed else f"\n{failed} FAILED")
sys.exit(failed)
