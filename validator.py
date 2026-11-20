"""
validator.py  --  Pre-Execution Validation Layer

Every statement (generated from natural language OR typed into the SQL Editor tab) is
intercepted here BEFORE it is handed to the database.  Nothing in this module touches
``target_database.db``:

    1. Statement scan     a character scanner splits the SQL string into tokens
                          (strings, quoted identifiers, words, symbols, parenthesis depth).
    2. Structural checks  - only SELECT / INSERT / DELETE / CREATE TABLE are allowed
                          - exactly one statement
                          - SELECT clause must list attributes
                          - FROM clause must point at a table token
                          - DELETE and UPDATE must carry a WHERE (otherwise every row is hit)
                          - DROP TABLE / ALTER TABLE must have a valid shape; irreversible ones
                            (DROP TABLE, ALTER ... DROP COLUMN) are flagged so the caller asks
                            the user to confirm before running them
    3. Aggregate rules    - an aggregate sitting in WHERE is moved to HAVING
                          - plain columns beside an aggregate are added to GROUP BY
    4. Engine dry-run     ``EXPLAIN <sql>`` on an IN-MEMORY mirror of the schema.  It catches
                          unknown tables/columns, syntax errors, wrong value counts and
                          duplicate tables without executing anything or touching the disk file.

On failure a ``ValidationException`` is raised; the caller halts and shows the
"Validation Exception" card.  On success a ``ValidatedSql`` is returned (the possibly
auto-corrected SQL plus human-readable notes about what was enforced).
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field

AGG_FUNCS = {"COUNT", "SUM", "AVG", "MIN", "MAX", "TOTAL"}
ALLOWED_KINDS = {"SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"}
_CLAUSE_RANK = {"SELECT": 0, "FROM": 1, "WHERE": 2, "GROUP": 3, "HAVING": 4, "ORDER": 5, "LIMIT": 6}


class ValidationException(Exception):
    """Raised when a statement must be halted before it reaches the database."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


@dataclass
class ValidatedSql:
    sql: str
    kind: str
    notes: list[str] = field(default_factory=list)
    table: str = ""          # table to preview after the statement (empty if it no longer exists)
    destructive: bool = False   # irreversible (DROP TABLE, ALTER ... DROP COLUMN): confirm first
    target: str = ""         # what the user must type to confirm (table or column name)


@dataclass
class SqlTok:
    kind: str      # WORD | STR | QID | NUM | SYM
    text: str
    start: int
    end: int
    depth: int     # parenthesis depth (a '(' carries the depth before it, a ')' the depth after it)


# ===================================================================== 1. THE SCANNER
def scan(sql: str) -> list[SqlTok]:
    toks: list[SqlTok] = []
    i, n, depth = 0, len(sql), 0
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
        elif c in ("'", '"'):                                     # 'string'  or  "quoted identifier"
            j = i + 1
            while True:
                if j >= n:
                    raise ValidationException("Unterminated quote in the statement.",
                                              "Check that every ' or \" has a matching closing quote.")
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:             # doubled quote = escaped quote
                        j += 2
                        continue
                    break
                j += 1
            toks.append(SqlTok("STR" if c == "'" else "QID", sql[i:j + 1], i, j + 1, depth))
            i = j + 1
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            toks.append(SqlTok("WORD", sql[i:j], i, j, depth))
            i = j
        elif c.isdigit():
            j = i
            while j < n and (sql[j].isdigit() or sql[j] == "."):
                j += 1
            toks.append(SqlTok("NUM", sql[i:j], i, j, depth))
            i = j
        else:
            two = sql[i:i + 2]
            if c == "(":
                toks.append(SqlTok("SYM", "(", i, i + 1, depth))
                depth += 1
                i += 1
            elif c == ")":
                depth -= 1
                if depth < 0:
                    raise ValidationException("Unbalanced parentheses: a ')' has no matching '('.")
                toks.append(SqlTok("SYM", ")", i, i + 1, depth))
                i += 1
            elif two in (">=", "<=", "!=", "<>", "||"):
                toks.append(SqlTok("SYM", two, i, i + 2, depth))
                i += 2
            else:
                toks.append(SqlTok("SYM", c, i, i + 1, depth))
                i += 1
    if depth != 0:
        raise ValidationException("Unbalanced parentheses: a '(' is never closed.")
    return toks


def _up(t: SqlTok) -> str:
    return t.text.upper() if t.kind == "WORD" else t.text


def _split_top(toks: list[SqlTok], separators: set[str]) -> list[list[SqlTok]]:
    """Split a token run at depth-0 separators (',' or the keyword AND)."""
    parts: list[list[SqlTok]] = [[]]
    pending_between = False
    for t in toks:
        u = _up(t)
        if t.depth == 0 and u == "BETWEEN":
            pending_between = True
        if t.depth == 0 and u in separators:
            if u == "AND" and pending_between:                    # AND that belongs to BETWEEN x AND y
                pending_between = False
                parts[-1].append(t)
                continue
            parts.append([])
            continue
        parts[-1].append(t)
    return [p for p in parts if p]


def _has_aggregate(toks: list[SqlTok]) -> bool:
    return any(t.kind == "WORD" and _up(t) in AGG_FUNCS and k + 1 < len(toks) and toks[k + 1].text == "("
               for k, t in enumerate(toks))


def _text(sql: str, toks: list[SqlTok]) -> str:
    return sql[toks[0].start:toks[-1].end] if toks else ""


def _norm(expr: str) -> str:
    """Normalise an expression for comparison (case, quotes, spaces)."""
    return expr.replace('"', "").replace(" ", "").lower()


# ===================================================================== 2. SHADOW SCHEMA
class ShadowSchema:
    """An in-memory SQLite mirror of the schema used for the dry run (no rows, no disk)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)

    def rebuild(self, ddl_statements: list[str]) -> None:
        with self._lock:
            self._conn.close()
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
            for ddl in ddl_statements:
                try:
                    self._conn.execute(ddl)
                except sqlite3.Error:
                    pass          # an exotic table definition must not break validation

    def dry_run(self, sql: str) -> None:
        with self._lock:
            try:
                self._conn.execute("EXPLAIN " + sql)              # compiles the statement, runs nothing
            except sqlite3.Error as exc:
                raise ValidationException(f"The SQL engine rejected the statement: {exc}",
                                          "Check table and column names against the Schema tab.") from exc


# ===================================================================== 3. THE VALIDATOR
class Validator:
    def __init__(self, shadow: ShadowSchema) -> None:
        self.shadow = shadow

    def validate(self, sql: str) -> ValidatedSql:
        sql = (sql or "").strip()
        if not sql:
            raise ValidationException("The statement is empty.")
        toks = scan(sql)

        # drop one trailing semicolon; any other ';' means multiple statements
        if toks and toks[-1].text == ";":
            toks = toks[:-1]
        if any(t.text == ";" for t in toks):
            raise ValidationException("Only one statement may be executed at a time.")
        if not toks:
            raise ValidationException("The statement is empty.")

        kind = _up(toks[0])
        if toks[0].kind != "WORD" or kind not in ALLOWED_KINDS:
            raise ValidationException(
                f"Statement type '{toks[0].text}' is not permitted.",
                "Allowed: SELECT, INSERT, UPDATE, DELETE, CREATE TABLE, ALTER TABLE and DROP TABLE.")

        notes: list[str] = []
        body = sql[:toks[-1].end]                                  # the statement without the ';'
        table, destructive, target = "", False, ""
        if kind == "SELECT":
            body = self._check_select(body, toks, notes)
        elif kind == "INSERT":
            self._check_insert(toks)
            table = toks[2].text
        elif kind == "DELETE":
            self._check_delete(toks)
            table = toks[2].text
        elif kind == "UPDATE":
            self._check_update(toks)
            table = toks[1].text
        elif kind == "DROP":
            target = self._check_drop(toks)
            destructive = True
            notes.append("DROP TABLE is irreversible: the table and all of its rows will be removed.")
        elif kind == "ALTER":
            table, destructive, target = self._check_alter(toks)
            if destructive:
                notes.append("Dropping a column is irreversible: its data will be removed.")
        else:
            self._check_create(toks)
            table = toks[5].text if len(toks) > 5 and _up(toks[2]) == "IF" else toks[2].text

        final = body + ";"
        self.shadow.dry_run(final)                                 # engine-level dry run (memory only)
        return ValidatedSql(final, kind, notes, table.strip('"'), destructive, target.strip('"'))

    # ------------------------------------------------------------------- SELECT
    def _check_select(self, sql: str, toks: list[SqlTok], notes: list[str]) -> str:
        # locate the top-level clause keywords
        marks: list[tuple[str, int, int]] = []                     # (clause, keyword start, keyword end)
        i = 0
        while i < len(toks):
            t = toks[i]
            if t.depth == 0 and t.kind == "WORD":
                u = _up(t)
                if u in ("SELECT", "FROM", "WHERE", "HAVING", "LIMIT"):
                    marks.append((u, t.start, t.end))
                elif u in ("GROUP", "ORDER") and i + 1 < len(toks) and _up(toks[i + 1]) == "BY":
                    marks.append((u, t.start, toks[i + 1].end))
                    i += 1
            i += 1
        names = [m[0] for m in marks]
        if len(set(names)) != len(names):
            raise ValidationException("A clause appears twice in the SELECT statement.")
        if [_CLAUSE_RANK[n] for n in names] != sorted(_CLAUSE_RANK[n] for n in names):
            raise ValidationException("SELECT clauses are out of order.",
                                      "The order must be SELECT, FROM, WHERE, GROUP BY, HAVING, ORDER BY, LIMIT.")
        clause: dict[str, tuple[int, int]] = {}
        for k, (name, _s, kw_end) in enumerate(marks):
            clause[name] = (kw_end, marks[k + 1][1] if k + 1 < len(marks) else len(sql))

        def run(name: str) -> list[SqlTok]:
            if name not in clause:
                return []
            a, b = clause[name]
            return [t for t in toks if t.start >= a and t.end <= b]

        # --- SELECT clause must have attributes
        sel = run("SELECT")
        prefix = ""
        if sel and sel[0].kind == "WORD" and _up(sel[0]) in ("DISTINCT", "ALL"):
            prefix, sel = sel[0].text + " ", sel[1:]
        items = _split_top(sel, {","})
        if not items:
            raise ValidationException("The SELECT clause has no attributes.",
                                      "List at least one column, an aggregate such as COUNT(*), or *.")

        # --- FROM clause must point at a table token
        frm = run("FROM")
        if not frm or frm[0].kind not in ("WORD", "QID") or (frm[0].kind == "WORD" and _up(frm[0]) in _CLAUSE_RANK):
            raise ValidationException("The FROM clause does not point to a table.",
                                      "Name the table after FROM, for example: FROM employees.")

        # --- aggregate / GROUP BY / HAVING enforcement
        item_text = [_text(sql, it) for it in items]
        is_agg = [_has_aggregate(it) for it in items]
        where_toks, having_toks, group_toks = run("WHERE"), run("HAVING"), run("GROUP")

        where_preds = _split_top(where_toks, {"AND"})
        has_or = any(t.depth == 0 and _up(t) == "OR" for t in where_toks)
        agg_preds = [p for p in where_preds if _has_aggregate(p)]
        if agg_preds and has_or:
            raise ValidationException("An aggregate inside WHERE is combined with OR, so it cannot be moved to HAVING.",
                                      "Rewrite the condition using AND, or put the aggregate test in HAVING yourself.")
        plain_preds = [_text(sql, p) for p in where_preds if not _has_aggregate(p)]
        having_extra = [_text(sql, p) for p in agg_preds]
        for p in having_extra:
            notes.append(f"Aggregate condition `{p}` was moved from WHERE to HAVING.")

        if any(is_agg):
            if any(it == "*" for it in item_text):
                raise ValidationException("'*' cannot be combined with an aggregate function.",
                                          "Select explicit columns, or remove the aggregate.")
        plain_items = [t for t, agg in zip(item_text, is_agg) if not agg]
        group_items = [_text(sql, g) for g in _split_top(group_toks, {","})]
        g_norms = {_norm(g) for g in group_items}
        missing = [p for p in plain_items if _norm(p) not in g_norms
                   and _norm(p).split(".")[-1] not in {g.split(".")[-1] for g in g_norms}]
        if any(is_agg) and missing:
            group_items += missing
            notes.append("GROUP BY " + ", ".join(missing) + " was enforced: non-aggregated columns "
                         "appear beside an aggregate function.")

        # --- rebuild the statement from its checked parts
        having_parts = ([f"({_text(sql, having_toks)})"] if having_toks and having_extra
                        else ([_text(sql, having_toks)] if having_toks else [])) + having_extra
        out = f"SELECT {prefix}{', '.join(item_text)} FROM {_text(sql, frm)}"
        if plain_preds:
            out += " WHERE " + " AND ".join(plain_preds)
        if group_items:
            out += " GROUP BY " + ", ".join(group_items)
        if having_parts:
            out += " HAVING " + " AND ".join(having_parts)
        for tail in ("ORDER", "LIMIT"):
            if tail in clause:
                kw = next(m for m in marks if m[0] == tail)
                out += " " + sql[kw[1]:clause[tail][1]].strip()
        return out

    # ----------------------------------------------------------- INSERT / DELETE / CREATE
    @staticmethod
    def _table_token(toks: list[SqlTok], pos: int, what: str) -> None:
        if len(toks) <= pos or toks[pos].kind not in ("WORD", "QID"):
            raise ValidationException(f"{what} does not point to a table.",
                                      "Name the table right after the keyword.")

    def _check_insert(self, toks: list[SqlTok]) -> None:
        if len(toks) < 2 or _up(toks[1]) != "INTO":
            raise ValidationException("INSERT must be followed by INTO.", "Example: INSERT INTO employees VALUES (...).")
        self._table_token(toks, 2, "INSERT INTO")

    def _check_delete(self, toks: list[SqlTok]) -> None:
        if len(toks) < 2 or _up(toks[1]) != "FROM":
            raise ValidationException("DELETE must be followed by FROM.", "Example: DELETE FROM employees WHERE id = 3.")
        self._table_token(toks, 2, "DELETE FROM")
        if not any(t.depth == 0 and _up(t) == "WHERE" for t in toks):
            raise ValidationException(
                "DELETE without a WHERE condition would remove every row of the table.",
                "Add a condition, for example: delete employees older than 60.")

    def _check_update(self, toks: list[SqlTok]) -> None:
        self._table_token(toks, 1, "UPDATE")
        if len(toks) < 4 or _up(toks[2]) != "SET":
            raise ValidationException("UPDATE must be followed by a table and SET.",
                                      "Example: UPDATE employees SET salary = 70000 WHERE id = 3.")
        if not any(t.depth == 0 and _up(t) == "WHERE" for t in toks):
            raise ValidationException(
                "UPDATE without a WHERE condition would change every row of the table.",
                "Add a condition, for example: change Bob's salary to 70000.")

    def _check_drop(self, toks: list[SqlTok]) -> str:
        if len(toks) < 3 or _up(toks[1]) != "TABLE":
            raise ValidationException("Only DROP TABLE is permitted.", "Example: DROP TABLE pets.")
        pos = 4 if len(toks) > 4 and _up(toks[2]) == "IF" else 2          # DROP TABLE IF EXISTS x
        self._table_token(toks, pos, "DROP TABLE")
        if len(toks) > pos + 1:
            raise ValidationException("Unexpected text after the table name in DROP TABLE.")
        return toks[pos].text

    def _check_alter(self, toks: list[SqlTok]) -> tuple[str, bool, str]:
        """Returns (table to preview afterwards, destructive?, text to type for confirmation)."""
        if len(toks) < 5 or _up(toks[1]) != "TABLE":
            raise ValidationException("Only ALTER TABLE is permitted.",
                                      "Example: ALTER TABLE employees ADD COLUMN phone TEXT.")
        self._table_token(toks, 2, "ALTER TABLE")
        table, action = toks[2].text, _up(toks[3])
        if action == "ADD":
            return table, False, ""
        if action == "RENAME":
            if _up(toks[4]) == "TO":                                       # RENAME TO newname
                self._table_token(toks, 5, "RENAME TO")
                return toks[5].text, False, ""
            return table, False, ""                                        # RENAME [COLUMN] a TO b
        if action == "DROP":
            pos = 5 if _up(toks[4]) == "COLUMN" else 4
            self._table_token(toks, pos, "DROP COLUMN")
            return table, True, toks[pos].text
        raise ValidationException(f"ALTER TABLE ... {toks[3].text} is not supported.",
                                  "Supported: ADD COLUMN, RENAME COLUMN, RENAME TO, DROP COLUMN.")

    def _check_create(self, toks: list[SqlTok]) -> None:
        if len(toks) < 2 or _up(toks[1]) != "TABLE":
            raise ValidationException("Only CREATE TABLE is permitted.")
        if not any(t.text == "(" for t in toks):
            raise ValidationException("CREATE TABLE needs a column list in parentheses.",
                                      "Example: CREATE TABLE pets (name TEXT, age INTEGER).")
