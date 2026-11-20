"""
database_service.py  --  Python port of DatabaseService.cs / TableSchema.cs

Responsibilities
    * hold the connection settings for the physical file ``target_database.db``
    * introspect the schema (tables / columns)  -> feeds the vector space + sidebar tree
    * sample distinct text values per column     -> lets filters like "in London" be grounded
    * execute a statement:
          SELECT   -> column headers come from ``cursor.description`` + the fetched rows
          DDL/DML  -> execute, then ``conn.commit()`` so the change is on disk immediately
            (visible at once in DB Browser for SQLite)

Only the standard-library ``sqlite3`` engine is used.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "target_database.db")
CONNECTIONS_FILE = os.path.join(BASE_DIR, "connections.json")
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")      # saved prompts / favorites


def load_connections() -> list[dict]:
    """
    Load the list of known SQLite connections [{name, path}].

    The list is cleaned on every load so a copied / moved project keeps working:
      * relative paths are resolved against the project folder,
      * entries whose file no longer exists on this machine are dropped
        (e.g. a Windows path saved on another computer, or a deleted temp test DB),
      * duplicate names are removed (the first one wins),
      * the bundled default database is always present.
    """
    cleaned: list[dict] = []
    if os.path.exists(CONNECTIONS_FILE):
        try:
            with open(CONNECTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                seen: set[str] = set()
                for c in data:
                    if not isinstance(c, dict) or not c.get("name") or not c.get("path"):
                        continue
                    path = c["path"] if os.path.isabs(c["path"]) else os.path.join(BASE_DIR, c["path"])
                    if not os.path.exists(path) or c["name"].lower() in seen:
                        continue
                    seen.add(c["name"].lower())
                    cleaned.append({"name": c["name"], "path": path})
        except Exception:
            pass
    if not cleaned:
        cleaned = [{"name": "target_database.db", "path": DEFAULT_DB_PATH}]
    save_connections(cleaned)
    return cleaned


def save_connections(conns: list[dict]) -> None:
    """
    Save the connections list to connections.json (counterpart to AIConnections.txt).
    Databases that live inside the project folder are stored with a RELATIVE path so the
    project keeps working when it is copied to another folder or computer.
    """
    out = []
    for c in conns:
        path = c["path"]
        try:
            if os.path.commonpath([os.path.abspath(path), BASE_DIR]) == BASE_DIR:
                path = os.path.relpath(os.path.abspath(path), BASE_DIR)
        except ValueError:          # different drive on Windows: keep the absolute path
            pass
        out.append({"name": c["name"], "path": path})
    try:
        with open(CONNECTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
    except Exception:
        pass


MAX_ROWS = 1000            # cap on rows pulled into the grid (the Blazor prompt used "limit 100")
MAX_VALUES_PER_COLUMN = 200  # distinct text values indexed per column
MAX_VALUE_LENGTH = 40

# A declared column type counts as numeric if it contains one of these fragments
# (this is SQLite's own type-affinity idea, not a language rule).
_NUMERIC_AFFINITY = ("INT", "REAL", "FLOA", "DOUB", "DEC", "NUM")


@dataclass
class ColumnInfo:
    name: str
    decl_type: str
    numeric: bool
    pk: bool = False


@dataclass
class ForeignKeyConstraint:
    from_table: str
    from_column: str
    to_table: str
    to_column: str


@dataclass
class TableSchema:
    """Mirror of TableSchema.cs: a table name plus its columns, PKs, and FKs."""
    table_name: str
    columns: list[ColumnInfo] = field(default_factory=list)
    foreign_keys: list[ForeignKeyConstraint] = field(default_factory=list)
    primary_keys: list[str] = field(default_factory=list)


@dataclass
class ExecutionResult:
    is_query: bool                       # True for SELECT-like statements
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    rowcount: int = 0                    # rows affected for DML
    truncated: bool = False              # True if more than MAX_ROWS rows existed


def quote_identifier(name: str) -> str:
    """Quote an identifier for safe use inside generated SQL."""
    return '"' + name.replace('"', '""') + '"'


class DatabaseService:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.environ.get("TARGET_DB_PATH") or DEFAULT_DB_PATH
        # Connecting creates the physical file when it does not exist yet.
        sqlite3.connect(self.path).close()

    # ------------------------------------------------------------------ schema
    def get_schema(self) -> list[TableSchema]:
        """Read every user table, its columns, PKs and foreign keys via PRAGMAs."""
        conn = sqlite3.connect(self.path)
        try:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            schema: list[TableSchema] = []
            for table in names:
                cols = []
                pks = []
                for _cid, cname, ctype, _notnull, _dflt, pk in conn.execute(
                    f"PRAGMA table_info({quote_identifier(table)})"
                ):
                    decl = (ctype or "").upper()
                    is_num = any(f in decl for f in _NUMERIC_AFFINITY)
                    is_pk = bool(pk)
                    cols.append(ColumnInfo(cname, decl, is_num, pk=is_pk))
                    if is_pk:
                        pks.append(cname)

                fks = []
                for _id, _seq, to_tbl, from_col, to_col, *_rest in conn.execute(
                    f"PRAGMA foreign_key_list({quote_identifier(table)})"
                ):
                    resolved_to = to_col or ""
                    if not resolved_to:
                        parent_cols = conn.execute(
                            f"PRAGMA table_info({quote_identifier(to_tbl)})"
                        ).fetchall()
                        parent_pks = [r[1] for r in parent_cols if r[5] > 0]
                        resolved_to = parent_pks[0] if parent_pks else "id"
                    fks.append(ForeignKeyConstraint(table, from_col, to_tbl, resolved_to))

                schema.append(TableSchema(table, cols, foreign_keys=fks, primary_keys=pks))
            return schema
        finally:
            conn.close()

    def get_foreign_keys(self, table: str | None = None) -> list[ForeignKeyConstraint]:
        """
        Query the live foreign key constraints from the SQLite schema dynamically
        by scanning system database catalogs via PRAGMA foreign_key_list.
        """
        conn = sqlite3.connect(self.path)
        try:
            tables = [table] if table else [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            fks: list[ForeignKeyConstraint] = []
            for t in tables:
                for _id, _seq, to_tbl, from_col, to_col, *_rest in conn.execute(
                    f"PRAGMA foreign_key_list({quote_identifier(t)})"
                ):
                    resolved_to = to_col or ""
                    if not resolved_to:
                        parent_cols = conn.execute(
                            f"PRAGMA table_info({quote_identifier(to_tbl)})"
                        ).fetchall()
                        parent_pks = [r[1] for r in parent_cols if r[5] > 0]
                        resolved_to = parent_pks[0] if parent_pks else "id"
                    fks.append(ForeignKeyConstraint(t, from_col, to_tbl, resolved_to))
            return fks
        finally:
            conn.close()

    def get_ddl(self) -> list[str]:
        """CREATE TABLE statements, used to mirror the schema in the in-memory validator."""
        conn = sqlite3.connect(self.path)
        try:
            return [r[0] for r in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL")]
        finally:
            conn.close()

    def get_text_values(self, schema: list[TableSchema]) -> dict[tuple[str, str], list[str]]:
        """Distinct cell values of every non-numeric column (the 'value index')."""
        out: dict[tuple[str, str], list[str]] = {}
        conn = sqlite3.connect(self.path)
        try:
            for t in schema:
                for c in t.columns:
                    if c.numeric:
                        continue
                    q = (f"SELECT DISTINCT {quote_identifier(c.name)} FROM {quote_identifier(t.table_name)} "
                         f"WHERE {quote_identifier(c.name)} IS NOT NULL LIMIT {MAX_VALUES_PER_COLUMN}")
                    vals = [str(r[0]) for r in conn.execute(q) if r[0] is not None]
                    vals = [v for v in vals if 0 < len(v) <= MAX_VALUE_LENGTH]
                    if vals:
                        out[(t.table_name, c.name)] = vals
            return out
        finally:
            conn.close()

    # --------------------------------------------------------------- execution
    def execute(self, sql: str) -> ExecutionResult:
        """
        Run ONE validated statement against the physical database file.

        SELECT : headers are read from the cursor metadata description loop.
        DDL/DML: executed, then committed so the file on disk changes instantly.
        """
        conn = sqlite3.connect(self.path)
        try:
            cur = conn.cursor()
            cur.execute(sql)
            if cur.description is not None:                     # a result set exists
                columns = [col[0] for col in cur.description]   # metadata description loop
                rows = cur.fetchmany(MAX_ROWS + 1)
                truncated = len(rows) > MAX_ROWS
                conn.commit()
                return ExecutionResult(True, columns, rows[:MAX_ROWS], truncated=truncated)
            conn.commit()                                       # persist DDL / DML now
            return ExecutionResult(False, rowcount=cur.rowcount)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
