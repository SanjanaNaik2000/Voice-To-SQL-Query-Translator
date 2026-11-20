"""
test_advanced_relational.py -- Tests for the 5 advanced relational capabilities:
1. Multi-Table Mapping & Detection
2. Multi-Table Relational JOIN Compilations (dynamic FK catalog scanning & structural anchors)
3. Full DDL Implementation (ALTER TABLE ADD/DROP COLUMN, DROP TABLE)
4. Full DML Mutation Parsing (Advanced UPDATE with isolated sets/conditions, INSERT, DELETE)
5. Unified Syntax Validation Integration (table.column prefixing, no ambiguity faults)
"""
import os
import sqlite3
import sys
import tempfile

from database_service import DatabaseService
from translator import Translator
from validator import ShadowSchema, Validator, ValidationException
from seed_demo_data import seed

failed = 0

def check(name, cond, extra=""):
    global failed
    failed += not cond
    print(("PASS " if cond else "FAIL "), name, ("-> " + str(extra)) if (extra and not cond) else "")

# Setup temporary test database
tmp_dir = tempfile.mkdtemp()
db_path = os.path.join(tmp_dir, "test_relational.db")
seed(db_path)

db_service = DatabaseService(db_path)
schema = db_service.get_schema()
values = db_service.get_text_values(schema)
shadow = ShadowSchema()
shadow.rebuild(db_service.get_ddl())
validator = Validator(shadow)
translator = Translator(schema, values, db_service=db_service)

# ---------------------------------------------------------------- 1. Multi-Table Detection
print("\n--- 1. Multi-Table Detection ---")
tr = translator.translate("show orders and products")
check("multi-table detection: orders and products", set(tr.tables) == {"orders", "products"}, tr.tables)
check("multi-table SELECT generated", tr.sql is not None and "INNER JOIN" in tr.sql, tr.sql)

tr2 = translator.translate("show customer and price from orders and products")
check("multi-table detection with columns", set(tr2.tables) == {"orders", "products"}, tr2.tables)
check("columns qualified with table names", '"orders"."customer"' in tr2.sql and '"products"."price"' in tr2.sql, tr2.sql)

# ---------------------------------------------------------------- 2. Multi-Table JOIN & Foreign Key Scanning
print("\n--- 2. Multi-Table Relational JOIN Compilations ---")
# Join between orders and products via structural anchor: orders.product = products.product_name
check("INNER JOIN syntax generated", 'INNER JOIN "products" ON "orders"."product" = "products"."product_name"' in tr.sql, tr.sql)

# Test execution on live SQLite database
res = db_service.execute(tr.sql)
check("multi-table query executes on SQLite", res.is_query and len(res.rows) > 0, len(res.rows))
check("no ambiguous reference error in SQLite", len(res.columns) > 0, res.columns)

# Test with WHERE filter across joined tables
tr_where = translator.translate("orders with products where price > 500")
check("multi-table WHERE filter", '"products"."price" > 500' in tr_where.sql and "INNER JOIN" in tr_where.sql, tr_where.sql)
res_where = db_service.execute(tr_where.sql)
check("filtered multi-table query executes", res_where.is_query and all(r[res_where.columns.index("price")] > 500 for r in res_where.rows))

# Test dynamic catalog foreign key scanning with newly created table having explicit FK constraint
conn = sqlite3.connect(db_path)
conn.execute("""
    CREATE TABLE line_items (
        item_id INTEGER PRIMARY KEY,
        order_ref INTEGER,
        product_ref INTEGER,
        FOREIGN KEY (order_ref) REFERENCES orders(order_id),
        FOREIGN KEY (product_ref) REFERENCES products(id)
    );
""")
conn.execute("INSERT INTO line_items VALUES (1, 1, 1);")
conn.execute("INSERT INTO line_items VALUES (2, 2, 2);")
conn.commit()
conn.close()

# Refresh schema and translator
schema_updated = db_service.get_schema()
values_updated = db_service.get_text_values(schema_updated)
shadow.rebuild(db_service.get_ddl())
translator_updated = Translator(schema_updated, values_updated, db_service=db_service)

# Verify get_foreign_keys scanned live catalog
fks = db_service.get_foreign_keys("line_items")
check("dynamic foreign key catalog scanning", len(fks) == 2, fks)
check("FK references orders", any(fk.to_table == "orders" and fk.from_column == "order_ref" for fk in fks))
check("FK references products", any(fk.to_table == "products" and fk.from_column == "product_ref" for fk in fks))

# Translate join between line_items and orders
tr_fk = translator_updated.translate("show line_items and orders")
check("FK-based join compiled dynamically", 'ON "line_items"."order_ref" = "orders"."order_id"' in tr_fk.sql or 'ON "orders"."order_id" = "line_items"."order_ref"' in tr_fk.sql, tr_fk.sql)
res_fk = db_service.execute(tr_fk.sql)
check("FK join query executed successfully", res_fk.is_query and len(res_fk.rows) == 2)

# ---------------------------------------------------------------- 3. Full DDL Implementation
print("\n--- 3. Full DDL Implementation ---")
# ALTER TABLE ADD COLUMN
tr_add = translator_updated.translate("alter table employees add column phone text")
check("ALTER TABLE ADD COLUMN explicit text", tr_add.sql == 'ALTER TABLE "employees" ADD COLUMN "phone" TEXT;', tr_add.sql)
val_add = validator.validate(tr_add.sql)
check("ALTER ADD validated", val_add.kind == "ALTER" and not val_add.destructive)
db_service.execute(tr_add.sql)
shadow.rebuild(db_service.get_ddl())

# ALTER TABLE ADD COLUMN with integer type
tr_add2 = translator_updated.translate("alter table employees add column rank integer")
check("ALTER TABLE ADD COLUMN explicit integer", tr_add2.sql == 'ALTER TABLE "employees" ADD COLUMN "rank" INTEGER;', tr_add2.sql)
db_service.execute(tr_add2.sql)
shadow.rebuild(db_service.get_ddl())

# ALTER TABLE DROP COLUMN
tr_drop_col = translator_updated.translate("alter table employees drop column phone")
check("ALTER TABLE DROP COLUMN syntax", tr_drop_col.sql == 'ALTER TABLE "employees" DROP COLUMN "phone";', tr_drop_col.sql)
val_drop_col = validator.validate(tr_drop_col.sql)
check("ALTER DROP COLUMN flagged as destructive", val_drop_col.destructive and val_drop_col.target == "phone")
db_service.execute(tr_drop_col.sql)
shadow.rebuild(db_service.get_ddl())

# DROP TABLE
tr_drop_tbl = translator_updated.translate("drop table line_items")
check("DROP TABLE syntax", tr_drop_tbl.sql == 'DROP TABLE "line_items";', tr_drop_tbl.sql)
val_drop_tbl = validator.validate(tr_drop_tbl.sql)
check("DROP TABLE flagged as destructive", val_drop_tbl.destructive and val_drop_tbl.target == "line_items")
db_service.execute(tr_drop_tbl.sql)
shadow.rebuild(db_service.get_ddl())

# ---------------------------------------------------------------- 4. Full DML Mutation Parsing
print("\n--- 4. Full DML Mutation Parsing ---")
# UPDATE: Multiple assignments + condition
tr_up_multi = translator_updated.translate("update employees set salary = 99999, city = 'Berlin' where name is Alice")
check("UPDATE multiple isolated sets", '"salary" = 99999' in tr_up_multi.sql and '"city" = \'Berlin\'' in tr_up_multi.sql, tr_up_multi.sql)
check("UPDATE isolated where condition", '"name" = \'Alice\'' in tr_up_multi.sql, tr_up_multi.sql)
db_service.execute(tr_up_multi.sql)
c = sqlite3.connect(db_path)
row = c.execute("SELECT salary, city FROM employees WHERE name='Alice'").fetchone()
c.close()
check("multi-set UPDATE executed on disk", row == (99999.0, "Berlin"), row)

# Relative UPDATE
tr_up_rel = translator_updated.translate("increase salary by 4000 for employees in London")
check("relative UPDATE syntax", '"salary" = "salary" + 4000' in tr_up_rel.sql and '"city" = \'London\'' in tr_up_rel.sql, tr_up_rel.sql)
db_service.execute(tr_up_rel.sql)

# Comprehensive INSERT
tr_ins = translator_updated.translate("insert into products product_name Tablet category Electronics price 499.9 stock 30")
check("INSERT syntax", 'INSERT INTO "products"' in tr_ins.sql and 'Tablet' in tr_ins.sql, tr_ins.sql)
db_service.execute(tr_ins.sql)

# Comprehensive DELETE
tr_del = translator_updated.translate("delete products where price > 1000")
check("DELETE with condition", tr_del.sql == 'DELETE FROM "products" WHERE "price" > 1000;', tr_del.sql)
db_service.execute(tr_del.sql)

# ---------------------------------------------------------------- 5. Unified Syntax Validation Integration
print("\n--- 5. Unified Syntax Validation Integration ---")
# Verify validator passes multi-table joins and table-qualified columns
val_join = validator.validate(tr2.sql)
check("validator accepts qualified multi-table SQL", val_join.kind == "SELECT", val_join.sql)

# Verify validator catches ambiguous raw column references when multiple tables are present in hand-edited SQL
try:
    # 'id' is ambiguous in SQLite when employees and products are joined without qualification
    validator.validate('SELECT id FROM "employees" INNER JOIN "products" ON "employees"."id" = "products"."id";')
    amb_caught = False
except ValidationException as e:
    amb_caught = True
check("validator dry-run detects ambiguous column reference", amb_caught)

print(f"\nAdvanced Relational Tests: {'ALL PASSED' if not failed else f'{failed} FAILED'}")
sys.exit(failed)
