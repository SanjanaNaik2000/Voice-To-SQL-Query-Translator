"""
app.py  --  Flask application (Python counterpart of Home.razor + MainLayout.razor)

End-to-end workflow, replicated from the Blazor repository:

    prompt  --Translator-->  SQL  --Validator-->  checked SQL  --DatabaseService-->  grid / message
       |                                |                                  |
       |                                +-- ValidationException: halt, DB untouched
       +-- HistoryService.save_query(prompt) after a successful execution

Run:   python app.py        then open  http://127.0.0.1:5000
"""
from __future__ import annotations

import functools
import html
import math
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field

from flask import Flask, abort, flash, redirect, render_template, request, url_for
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)

import auth_db
from auth_db import (
    User,
    create_default_admin,
    create_user,
    delete_user,
    get_activity_log,
    get_all_users,
    get_user_by_id,
    init_auth_db,
    log_activity,
    update_user_status,
    verify_password,
)

import voice_service
from database_service import (
    DEFAULT_DB_PATH,
    DatabaseService,
    ExecutionResult,
    load_connections,
    quote_identifier,
    save_connections,
    HISTORY_FILE,
)
from history_service import HistoryService
from translator import Translator, lex
from validator import ShadowSchema, ValidationException, Validator

CONNECTION_NAME = "target_database.db"
PAGE_SIZE = 20                       # rows per page in the data grid (the MudTablePager)

COMMON_WORDS = {
    "show", "list", "display", "get", "fetch", "find", "select", "view", "see", "give", "tell",
    "all", "any", "some", "each", "every", "per", "a", "an", "the", "of", "in", "at", "to", "for",
    "with", "from", "into", "by", "on", "as", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "having", "do", "does", "did", "doing", "can", "could", "would", "should", "will", "shall",
    "what", "which", "who", "whom", "whose", "where", "when", "why", "how",
    "than", "then", "if", "and", "or", "not", "no", "yes", "but", "so", "only", "just", "also",
    "greater", "less", "more", "fewer", "above", "below", "under", "over", "between", "equal", "equals", "exactly",
    "count", "many", "number", "sum", "total", "average", "avg", "mean", "min", "minimum", "max", "maximum",
    "highest", "lowest", "top", "bottom", "first", "last", "sort", "sorted", "order", "ordered", "ranking", "ranked",
    "asc", "ascending", "desc", "descending", "group", "grouped", "like", "contains", "containing", "starts", "starting", "ends", "ending",
    "delete", "remove", "update", "set", "insert", "add", "create", "drop", "alter", "modify", "change",
    "table", "tables", "column", "columns", "field", "fields", "records", "rows", "details", "data", "info", "information",
    "distinct", "unique", "different", "describe", "structure", "schema", "definition",
    "please", "me", "my", "us", "we", "you", "they", "them", "their", "there", "this", "that", "these", "those",
    "user", "users", "employee", "employees", "order", "orders", "product", "products", "customer", "customers",
    "item", "items", "id", "name", "price", "salary", "cost", "amount", "quantity", "stock", "department", "date", "age",
    "status", "active", "type", "value", "values", "category", "city", "country", "email", "phone", "address"
}


@dataclass
class DashboardState:
    """Everything the single page needs to render (the @code block of Home.razor)."""
    prompt: str = ""
    prompt_display: str = ""                         # user input in text format with underline on typos
    typos: list[str] = field(default_factory=list)   # words identified as spelling mistakes
    sql: str = ""                                   # SQL shown in the emerald card / SQL editor
    kind: str = ""
    notes: list[str] = field(default_factory=list)   # what the validation layer auto-enforced
    trace: list[str] = field(default_factory=list)   # vector evidence ("Insights" tab)
    exception: dict | None = None                    # {kind, title, message, hint}
    message: str = ""                                # success banner for DDL / DML
    message_title: str = "Committed"
    pending: dict | None = None                      # destructive statement waiting for confirmation
    result: ExecutionResult | None = None
    voice_device: int | None = None


class Workspace:
    """Services + state for one running dashboard (single local user)."""

    def __init__(self, db_path: str | None = None, connection_name: str | None = None) -> None:
        self.connections = load_connections()
        if db_path is not None:
            self.db_path = os.path.abspath(db_path)
            self.connection_name = connection_name or "target_database.db"
            # An explicit path (tests / embedding) runs on its own, in memory only: it is NOT written
            # to connections.json, so throw-away databases never pile up in the saved list and can
            # never shadow a saved connection that has the same name.
            self.connections = [{"name": self.connection_name, "path": self.db_path}]
        else:
            first = self.connections[0] if self.connections else {"name": "target_database.db", "path": DEFAULT_DB_PATH}
            self.connection_name = first["name"]
            self.db_path = first["path"]

        self.db = DatabaseService(self.db_path)
        # Saved to history.json so it survives a restart.  An explicit db_path (tests / embedding)
        # keeps history in memory only, so tests never touch your real history file.
        self.history = HistoryService(HISTORY_FILE if db_path is None else None)
        self.shadow = ShadowSchema()
        self.validator = Validator(self.shadow)
        self.state = DashboardState()
        self.lock = threading.RLock()
        self.devices: list[dict] | None = None
        self.schema = []
        self.translator: Translator
        self.refresh_schema()

    def switch_connection(self, name_or_path: str) -> bool:
        """Switch active database to a saved connection by name or path."""
        target = next((c for c in self.connections if c["name"].lower() == name_or_path.lower()
                       or os.path.abspath(c["path"]) == os.path.abspath(name_or_path)), None)
        if not target:
            return False
        self.connection_name = target["name"]
        self.db_path = target["path"]
        self.db = DatabaseService(self.db_path)
        self.refresh_schema()
        self._reset_outputs()
        return True

    def add_connection(self, name: str, path: str) -> None:
        """Add a new SQLite database connection and switch to it."""
        clean_path = path.strip().strip('"').strip("'")
        abs_path = os.path.abspath(clean_path)
        clean_name = name.strip() or os.path.basename(abs_path) or "Database"

        # Validate that the database can be introspected
        test_db = DatabaseService(abs_path)
        _ = test_db.get_schema()

        existing = next((c for c in self.connections if c["name"].lower() == clean_name.lower()), None)
        if existing:
            existing["path"] = abs_path
        else:
            self.connections.append({"name": clean_name, "path": abs_path})
        save_connections(self.connections)
        self.switch_connection(clean_name)

    def delete_connection(self, name: str) -> bool:
        """Remove a saved connection. Cannot remove if it is the only one."""
        if len(self.connections) <= 1:
            return False
        self.connections = [c for c in self.connections if c["name"] != name]
        save_connections(self.connections)
        if self.connection_name == name:
            self.switch_connection(self.connections[0]["name"])
        return True

    # ---------------------------------------------------------------- schema refresh
    def refresh_schema(self) -> None:
        """Re-read the schema and values; rebuild the vector space and the validator's mirror."""
        self.schema = self.db.get_schema()
        self.translator = Translator(self.schema, self.db.get_text_values(self.schema), db_service=self.db)
        self.shadow.rebuild(self.db.get_ddl())

    # ------------------------------------------------------------------- pipeline
    def _reset_outputs(self) -> None:
        s = self.state
        s.sql, s.kind, s.notes, s.trace = "", "", [], []
        s.exception, s.message, s.result = None, "", None
        s.message_title, s.pending = "Committed", None
        s.prompt_display, s.typos = "", []

    def _format_prompt_display(self, prompt: str) -> tuple[str, list[str]]:
        if not prompt:
            return "", []
        import html
        vocab = set(self.translator.space.index.keys()) if hasattr(self.translator, "space") and hasattr(self.translator.space, "index") else set()
        all_valid = vocab | COMMON_WORDS
        toks = lex(prompt)
        typos: list[str] = []
        for t in toks:
            if t.kind == "WORD":
                w = t.text.lower()
                if not w.isdigit() and w not in all_valid:
                    typos.append(t.raw)

        typo_set = set(typos)
        tok_regex = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)|([^A-Za-z_]+)")
        parts: list[str] = []
        for m in tok_regex.finditer(prompt):
            word, other = m.group(1), m.group(2)
            if word:
                if word in typo_set:
                    parts.append(f'<u class="spell-mistake">{html.escape(word)}</u>')
                else:
                    parts.append(html.escape(word))
            elif other:
                parts.append(html.escape(other))
        return "".join(parts), typos

    def _raise(self, kind: str, title: str, message: str, hint: str = "") -> None:
        self.state.exception = {"kind": kind, "title": title, "message": message, "hint": hint}

    def run_prompt(self, prompt: str) -> None:
        """Natural language -> SQL -> validation -> database (RunDataChat in Home.razor)."""
        s = self.state
        self._reset_outputs()
        s.prompt = prompt
        s.prompt_display, s.typos = self._format_prompt_display(prompt)
        tr = self.translator.translate(prompt)
        s.trace, s.kind = tr.trace, tr.kind
        if tr.error:
            self._raise("translation", "Translation Exception", tr.error,
                        "Rephrase the request or name the table / column explicitly.")
            return
        if self._execute(tr.sql, prompt) == "ok":
            self.history.save_query(prompt, self.connection_name, s.kind)

    def run_sql(self, sql: str) -> None:
        """Execute hand-edited SQL from the SQL Editor tab (same validation gate)."""
        trace = self.state.trace
        self._reset_outputs()
        self.state.trace = trace
        self._execute(sql)

    def _execute(self, sql: str, prompt: str = "", confirmed: bool = False) -> str:
        """Returns "ok", "fail" or "pending" (destructive statement awaiting confirmation)."""
        s = self.state
        # 1) validation layer: intercept the string BEFORE any database access
        try:
            checked = self.validator.validate(sql)
        except ValidationException as exc:
            s.sql = sql
            self._raise("validation", "Validation Exception", exc.message,
                        (exc.hint + " " if exc.hint else "") + "Transaction halted: the database was not touched.")
            return "fail"
        s.sql, s.notes, s.kind = checked.sql, checked.notes, checked.kind

        # 1b) irreversible statements (DROP TABLE, DROP COLUMN) wait for an explicit confirmation
        if checked.destructive and not confirmed:
            s.pending = {"sql": checked.sql, "prompt": prompt, "target": checked.target,
                         "detail": self._destructive_detail(checked)}
            return "pending"

        # 2) database action layer
        try:
            res = self.db.execute(checked.sql)
        except sqlite3.Error as exc:
            self._raise("database", "Database System Exception", str(exc),
                        "The statement was rolled back; nothing was changed.")
            return "fail"

        if checked.kind == "SELECT":
            s.result = res
            return "ok"

        # DDL / DML: already committed to disk by DatabaseService.execute
        s.message = {
            "INSERT": f"{res.rowcount} row inserted and committed to the database file.",
            "UPDATE": f"{res.rowcount} row(s) updated and committed to the database file.",
            "DELETE": f"{res.rowcount} row(s) deleted and committed to the database file.",
            "CREATE": "Table created and committed to the database file.",
            "ALTER": "Table structure changed and committed to the database file.",
            "DROP": "Table dropped and committed to the database file.",
        }[checked.kind]
        self.refresh_schema()                                   # new tables / values -> new vector space
        if checked.table:                                       # show the effect straight away
            try:
                preview = self.validator.validate(f"SELECT * FROM {quote_identifier(checked.table)}")
                s.result = self.db.execute(preview.sql)
            except (ValidationException, sqlite3.Error):
                pass
        return "ok"

    def _destructive_detail(self, checked) -> str:
        if checked.kind == "DROP":
            try:
                n = self.db.execute(f"SELECT COUNT(*) FROM {quote_identifier(checked.target)}").rows[0][0]
                return f"This will permanently delete the table '{checked.target}' and its {n} row(s). It cannot be undone."
            except sqlite3.Error:
                return f"This will permanently delete the table '{checked.target}'. It cannot be undone."
        return f"This will permanently delete the column '{checked.target}' and all of its data. It cannot be undone."

    def confirm(self, typed: str) -> None:
        """Run the parked destructive statement if the user typed the exact target name."""
        s = self.state
        p = s.pending
        if not p:
            return
        if typed.strip() != p["target"]:
            s.exception = {"kind": "validation", "title": "Validation Exception",
                           "message": f"Confirmation text did not match. Type exactly: {p['target']}",
                           "hint": "Nothing was changed."}
            return
        sql, prompt = p["sql"], p["prompt"]
        self._reset_outputs()
        s.prompt = prompt
        if self._execute(sql, prompt, confirmed=True) == "ok" and prompt:
            self.history.save_query(prompt, self.connection_name, s.kind)

    def cancel(self) -> None:
        self._reset_outputs()
        self.state.message, self.state.message_title = "Cancelled: nothing was changed.", "Cancelled"

    def _voice_hint(self) -> str:
        """Table and column names, given to Whisper so it spells them correctly ("orders", not "odors")."""
        words: list[str] = []
        for t in self.schema:
            words.append(t.table_name.replace("_", " "))
            words += [c.name.replace("_", " ") for c in t.columns]
        names = ", ".join(list(dict.fromkeys(words))[:60])
        return f"Database commands: show all, how many, average, per, top 3, sorted by. {names}."

    def run_voice(self) -> None:
        self._reset_outputs()
        try:
            text = voice_service.listen(self.state.voice_device, hint=self._voice_hint())
        except voice_service.VoiceError as exc:
            self._raise("database", "Voice Input Exception", str(exc))
            return
        except Exception as exc:                                # never let a driver problem crash the page
            self._raise("database", "Voice Input Exception", f"Unexpected audio problem: {exc}")
            return
        self.run_prompt(text)
        exc = self.state.exception
        if exc and exc.get("kind") == "translation":
            # Say what the microphone actually understood: a mis-heard word looks exactly like a bad
            # sentence, and the person cannot tell the two apart without seeing the transcript.
            exc["message"] += f' Voice input heard: "{text}".'
            exc["hint"] = ("If that is not what you said, the speech model mis-heard you: say it again slowly "
                           "and clearly, or type the sentence in the prompt box.")


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    ws = Workspace(db_path)
    app.config["WORKSPACE"] = ws
    if db_path is None:
        voice_service.prewarm()

    # ------------------------------------------------------------------ auth setup
    app.secret_key = b'dbchatpro-secret-key-2024-xk9m'

    login_manager = LoginManager(app)
    login_manager.login_view = "login_page"  # type: ignore[assignment]

    @login_manager.user_loader
    def load_user(user_id: str):
        user_dict = get_user_by_id(user_id)
        if user_dict is None:
            return None
        return User(user_dict)

    # Initialise the auth database + ensure a default admin exists
    init_auth_db()
    create_default_admin()

    # ------------------------------------------------------------------ admin guard
    def admin_required(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated or current_user.role != "admin":
                abort(403)
            return fn(*args, **kwargs)
        return login_required(wrapper)

    # ================================================================== AUTH ROUTES

    @app.get("/login")
    def login_page():
        return render_template("login.html")

    @app.post("/login")
    def login_post():
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user_dict = verify_password(username, password)
        if user_dict is None:
            flash("Invalid username or password.", "error")
            return redirect(url_for("login_page"))
        if not user_dict.get("is_active", 1):
            flash("Your account has been deactivated. Contact an administrator.", "error")
            return redirect(url_for("login_page"))
        user_obj = User(user_dict)
        login_user(user_obj)
        log_activity(user_dict["id"], username, "LOGIN", "Logged in successfully")
        if user_dict["role"] == "admin":
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("index"))

    @app.get("/logout")
    @login_required
    def logout():
        log_activity(current_user.id, current_user.username, "LOGOUT", "Logged out")
        logout_user()
        return redirect(url_for("login_page"))

    @app.get("/register")
    def register_page():
        return render_template("register.html")

    @app.post("/register")
    def register_post():
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")
        if not username or not password:
            flash("Username and password are required.", "error")
            return redirect(url_for("register_page"))
        if len(username) < 3:
            flash("Username must be at least 3 characters.", "error")
            return redirect(url_for("register_page"))
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return redirect(url_for("register_page"))
        if password != confirm:
            flash("Passwords do not match.", "error")
            return redirect(url_for("register_page"))
        try:
            user_dict = create_user(username, password, role="user")
            log_activity(user_dict["id"], username, "REGISTER", "New account created")
            flash("Account created! You can now sign in.", "success")
            return redirect(url_for("login_page"))
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("register_page"))

    @app.get("/admin")
    @admin_required
    def admin_dashboard():
        users = get_all_users()
        activity = get_activity_log(100)
        total_users    = len(users)
        active_users   = sum(1 for u in users if u.get("is_active", 1))
        admin_count    = sum(1 for u in users if u.get("role") == "admin")
        recent_count   = len(activity)
        return render_template(
            "admin.html",
            users=users,
            activity=activity,
            total_users=total_users,
            active_users=active_users,
            admin_count=admin_count,
            recent_count=recent_count,
        )

    @app.post("/admin/toggle-user")
    @admin_required
    def admin_toggle_user():
        user_id   = request.form.get("user_id", "")
        is_active = request.form.get("is_active", "1")
        if user_id.isdigit():
            update_user_status(int(user_id), int(is_active))
            log_activity(
                current_user.id, current_user.username,
                "TOGGLE_USER",
                f"user_id={user_id} set is_active={is_active}",
            )
        return redirect(url_for("admin_dashboard"))

    @app.post("/admin/delete-user")
    @admin_required
    def admin_delete_user():
        user_id = request.form.get("user_id", "")
        if user_id.isdigit():
            delete_user(int(user_id))
            log_activity(
                current_user.id, current_user.username,
                "DELETE_USER",
                f"user_id={user_id} deleted",
            )
        return redirect(url_for("admin_dashboard"))

    # ================================================================== EXISTING ROUTES (protected)

    @app.get("/")
    @login_required
    def index():
        with ws.lock:
            s = ws.state
            if ws.devices is None:
                ws.devices = voice_service.list_input_devices()
            rows = s.result.rows if s.result else []
            pages = max(1, math.ceil(len(rows) / PAGE_SIZE))
            try:
                page = min(max(int(request.args.get("page", 1)), 1), pages)
            except ValueError:
                page = 1
            return render_template(
                "index.html",
                s=s,
                connection=ws.connection_name,
                active_path=ws.db_path,
                connections=ws.connections,
                schema=ws.schema,
                history=ws.history.get_queries(ws.connection_name),
                favorites=ws.history.get_favorites(ws.connection_name),
                devices=ws.devices,
                page=page, pages=pages, total=len(rows), page_size=PAGE_SIZE,
                page_rows=rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE],
                vocab_words=list(ws.translator.space.index.keys()) if hasattr(ws.translator, "space") and hasattr(ws.translator.space, "index") else [],
            )

    @app.get("/help")
    @login_required
    def help_page():
        with ws.lock:
            return render_template("help.html", connection=ws.connection_name, schema=ws.schema)

    @app.post("/switch-db")
    @login_required
    def switch_db():
        name = request.form.get("name", "").strip()
        with ws.lock:
            if not ws.switch_connection(name):
                ws._raise("database", "Connection Error", f"Could not find connection '{name}'.")
        return redirect(url_for("index"), code=303)

    @app.post("/connect-db")
    @login_required
    def connect_db():
        name = request.form.get("name", "").strip()
        path = request.form.get("path", "").strip()
        if not path:
            with ws.lock:
                ws._raise("validation", "Validation Exception", "Please provide a file path to the SQLite database.",
                          "Example: E:\\Sanjana\\project2\\dbchat_flask\\target_database.db")
            return redirect(url_for("index"), code=303)
        try:
            with ws.lock:
                ws.add_connection(name, path)
        except Exception as exc:
            with ws.lock:
                ws._raise("database", "Database Connection Exception", f"Could not connect to '{path}': {exc}",
                          "Verify the file is a valid SQLite database or check file permissions.")
        return redirect(url_for("index"), code=303)

    @app.post("/delete-db")
    @login_required
    def delete_db():
        name = request.form.get("name", "").strip()
        with ws.lock:
            if not ws.delete_connection(name):
                ws._raise("validation", "Validation Exception", "Cannot delete the only remaining connection.")
        return redirect(url_for("index"), code=303)

    @app.post("/query")
    @login_required
    def query():
        """Handles Submit, Speak, Save-favorite and history / favorite replays."""
        action = request.form.get("action", "run")
        prompt = request.form.get("prompt", "").strip()
        with ws.lock:
            device = request.form.get("device", "")
            ws.state.voice_device = int(device) if device.isdigit() else None
            if action == "voice":
                ws.run_voice()
            elif action == "favorite":
                if prompt:
                    ws.history.save_favorite(prompt, ws.connection_name, ws.state.kind)
                ws.state.prompt = prompt
            elif prompt:
                ws.run_prompt(prompt)
            else:
                ws._reset_outputs()
                ws._raise("validation", "Validation Exception", "Please type a request first.")
        return redirect(url_for("index"), code=303)

    @app.post("/sql")
    @login_required
    def sql_editor():
        with ws.lock:
            ws.run_sql(request.form.get("sql", ""))
        return redirect(url_for("index"), code=303)

    @app.post("/confirm")
    @login_required
    def confirm():
        with ws.lock:
            ws.confirm(request.form.get("confirm", ""))
        return redirect(url_for("index"), code=303)

    @app.post("/cancel")
    @login_required
    def cancel():
        with ws.lock:
            ws.cancel()
        return redirect(url_for("index"), code=303)

    @app.post("/history/delete")
    @login_required
    def delete_history_item():
        item = request.form.get("id", "")
        with ws.lock:
            if item.isdigit():
                ws.history.delete_query(int(item), ws.connection_name)
        return redirect(url_for("index"), code=303)

    @app.post("/favorites/delete")
    @login_required
    def delete_favorite_item():
        item = request.form.get("id", "")
        with ws.lock:
            if item.isdigit():
                ws.history.delete_favorite(int(item), ws.connection_name)
        return redirect(url_for("index"), code=303)

    @app.post("/favorites/clear")
    @login_required
    def clear_favorites():
        with ws.lock:
            ws.history.clear_favorites(ws.connection_name)
        return redirect(url_for("index"), code=303)

    @app.post("/history/clear")
    @login_required
    def clear_history():
        with ws.lock:
            ws.history.clear_queries(ws.connection_name)
        return redirect(url_for("index"), code=303)

    return app


if __name__ == "__main__":
    application = create_app()
    print("\nDBChatPro (Python) -> http://127.0.0.1:5000\n")
    application.run(host="127.0.0.1", port=5000, debug=False)
