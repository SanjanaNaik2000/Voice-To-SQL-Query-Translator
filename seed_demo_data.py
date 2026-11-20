"""
seed_demo_data.py -- optional: fill target_database.db with demo tables so you can try the app.

    python seed_demo_data.py          # creates the tables only if they do not exist
    python seed_demo_data.py --reset  # drops and recreates the demo tables

The app itself works with ANY SQLite file; this is only sample data.
"""
import sqlite3
import sys

from database_service import DatabaseService

EMPLOYEES = [
    (1, "Alice", "Engineering", 95000, 34, "London"),
    (2, "Bob", "Engineering", 88000, 29, "Paris"),
    (3, "Carol", "Sales", 62000, 41, "London"),
    (4, "David", "Sales", 58000, 25, "Berlin"),
    (5, "Eve", "Marketing", 71000, 38, "Paris"),
    (6, "Frank", "Marketing", 64000, 52, "London"),
    (7, "Grace", "Engineering", 105000, 45, "Berlin"),
    (8, "Heidi", "HR", 54000, 31, "Paris"),
    (9, "Ivan", "Sales", 66000, 36, "London"),
    (10, "Judy", "HR", 57000, 28, "Berlin"),
    (11, "Mallory", "Engineering", 99000, 40, "London"),
    (12, "Niaj", "Sales", 61000, 33, "Paris"),
]
PRODUCTS = [
    (1, "Laptop", "Electronics", 1200.0, 15),
    (2, "Phone", "Electronics", 800.0, 40),
    (3, "Desk", "Furniture", 350.0, 12),
    (4, "Chair", "Furniture", 150.0, 60),
    (5, "Notebook", "Stationery", 4.5, 500),
    (6, "Pen", "Stationery", 1.2, 900),
    (7, "Monitor", "Electronics", 300.0, 25),
    (8, "Lamp", "Furniture", 45.0, 80),
]
ORDERS = [
    (1, "Acme", "Laptop", 3, 3600.0),
    (2, "Globex", "Phone", 10, 8000.0),
    (3, "Acme", "Desk", 2, 700.0),
    (4, "Initech", "Chair", 20, 3000.0),
    (5, "Globex", "Pen", 500, 600.0),
    (6, "Initech", "Monitor", 5, 1500.0),
    (7, "Acme", "Notebook", 100, 450.0),
    (8, "Umbrella", "Lamp", 8, 360.0),
]


def seed(path=None, reset=False):
    db = DatabaseService(path)
    conn = sqlite3.connect(db.path)
    if reset:
        for t in ("employees", "products", "orders"):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS employees (id INTEGER, name TEXT, department TEXT, salary REAL, age INTEGER, city TEXT);
        CREATE TABLE IF NOT EXISTS products  (id INTEGER, product_name TEXT, category TEXT, price REAL, stock INTEGER);
        CREATE TABLE IF NOT EXISTS orders    (order_id INTEGER, customer TEXT, product TEXT, quantity INTEGER, total REAL);
    """)
    for table, rows in (("employees", EMPLOYEES), ("products", PRODUCTS), ("orders", ORDERS)):
        if conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0:
            marks = ",".join("?" * len(rows[0]))
            conn.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)
    conn.commit()
    conn.close()
    return db.path


if __name__ == "__main__":
    print("Seeded:", seed(reset="--reset" in sys.argv))
