"""
test_history.py -- history / favorites are saved to disk, survive a restart, and can be deleted.
Run:  python test_history.py     (uses temporary files; your real history.json is never touched)
"""
import json
import os
import shutil
import tempfile

import app as app_module
import database_service
from history_service import HistoryService

failed = 0


def check(name, ok):
    global failed
    failed += not ok
    print("PASS " if ok else "FAIL ", name)


tmp = tempfile.mkdtemp()
path = os.path.join(tmp, "history.json")

# ---- 1. the service on its own
h = HistoryService(path)
h.save_query("show all employees", "db1", "SELECT")
h.save_query("how many orders", "db1", "SELECT")
h.save_query("show products", "db2", "SELECT")
h.save_favorite("show all employees", "db1", "SELECT")
h.save_favorite("show all employees", "db1", "SELECT")                 # duplicate is ignored
check("file is written", os.path.exists(path))
check("no temp file left behind", not os.path.exists(path + ".tmp"))

h2 = HistoryService(path)                                              # "restart"
check("history survives a restart", [q.query for q in h2.get_queries("db1")] == ["how many orders", "show all employees"])
check("history stays per connection", [q.query for q in h2.get_queries("db2")] == ["show products"])
check("favorites survive a restart, no duplicate", [f.query for f in h2.get_favorites("db1")] == ["show all employees"])
h2.save_query("new one", "db1", "SELECT")
ids = [q.id for q in h2.get_queries("db1")]
check("ids keep increasing after a restart", len(set(ids)) == len(ids) and max(ids) > 3)

# ---- 2. delete one entry
victim = next(q for q in h2.get_queries("db1") if q.query == "how many orders")
check("delete a single entry", h2.delete_query(victim.id, "db1") is True)
check("other entries stay", [q.query for q in HistoryService(path).get_queries("db1")] == ["new one", "show all employees"])
check("cannot delete another connection's entry", h2.delete_query(next(iter(h2.get_queries("db2"))).id, "db1") is False)
check("deleting a missing id is harmless", h2.delete_query(9999, "db1") is False)
fav = h2.get_favorites("db1")[0]
check("delete a favorite", h2.delete_favorite(fav.id, "db1") and not HistoryService(path).get_favorites("db1"))

# ---- 3. clear
h2.clear_queries("db1")
after = HistoryService(path)
check("clear removes only that connection", not after.get_queries("db1") and len(after.get_queries("db2")) == 1)

# ---- 4. damaged / missing file never stops the app
with open(path, "w") as f:
    f.write("{ this is not json")
check("a damaged file is ignored", HistoryService(path).get_queries("db1") == [])
check("in-memory mode writes nothing", HistoryService().save_query("x", "c", "") is None)

# ---- 5. through the real Flask app (restart = create a new app)
real_hist = os.path.join(tmp, "app_history.json")
conns = os.path.join(tmp, "connections.json")
shutil.copy(database_service.CONNECTIONS_FILE, conns)
app_module.HISTORY_FILE = real_hist
database_service.CONNECTIONS_FILE = conns

a1 = app_module.create_app(); c1 = a1.test_client()
c1.post("/query", data={"action": "run", "prompt": "show all employees"})
c1.post("/query", data={"action": "run", "prompt": "how many employees in engineering"})
c1.post("/query", data={"action": "favorite", "prompt": "show all employees"})
a2 = app_module.create_app(); c2 = a2.test_client()                    # "close and reopen the project"
html = c2.get("/").get_data(as_text=True)
check("after reopening, history is shown", 'value="how many employees in engineering"' in html and "show all employees" in html)
check("after reopening, favorites are shown", "★ show all employees" in html)
check("delete buttons are on the page", "Delete this entry" in html and "Remove this favorite" in html)

ws2 = a2.config["WORKSPACE"]
before = ws2.history.get_queries(ws2.connection_name)
one = before[0]
r = c2.post("/history/delete", data={"id": str(one.id)})
check("delete route redirects", r.status_code == 303)
left = ws2.history.get_queries(ws2.connection_name)
check("exactly one entry removed", len(left) == len(before) - 1 and one.id not in [q.id for q in left])
check("deleted entry is gone from the page", f'name="id" value="{one.id}"' not in c2.get("/").get_data(as_text=True))
left = [q.query for q in left]
check("delete is saved to disk", len(json.load(open(real_hist))["queries"]) == len(left))
c2.post("/history/delete", data={"id": "abc"})                         # bad input must not crash
check("a bad id is ignored", c2.get("/").status_code == 200)
c2.post("/history/clear")
c2.post("/favorites/clear")
data = json.load(open(real_hist))
check("clear history + clear favorites", data["queries"] == [] and data["favorites"] == [])

print("\nAll passed" if not failed else f"\n{failed} FAILED")
raise SystemExit(1 if failed else 0)
