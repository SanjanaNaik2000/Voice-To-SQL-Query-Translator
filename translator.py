"""
translator.py  --  Non-AI natural language -> SQL compiler.

No LLM, no neural network, no network access.  Two classical techniques are combined:

1. VECTOR SPACE MODELLING (scikit-learn + numpy)
   Every schema element is a *cluster of points* in one shared TF-IDF space:
       tables, columns, cell values (read live from the database, so ANY schema works)
       + the SQL "slots" (intent, aggregate, comparison, group-by, ...)
   A query word's affinity to an element is the cosine similarity to the nearest point of
   that cluster.  All semantic decisions are argmax / sum operations on those matrices.

2. LEXICAL POSITION SEQUENCING
   Position is used only to *segment* the sentence:
       - the words right after a preposition anchor ("from", "into") boost table evidence
       - the words right after an aggregate cue are that aggregate's column
       - the words right after a group cue ("per", "by") are the GROUP BY column
       - numbers / quoted text bind to the words just before them (the comparison window)
       - after the table in an INSERT, "column value value column value" is read in sequence
   Which words ARE anchors / cues is itself decided by cosine similarity, never by
   comparing strings to keyword lists.

Regular expressions are used for exactly two lexical jobs (as the brief allows):
   tokenising, and extracting numeric / quoted literals.  There are no regex templates
   that map phrasings to SQL.

Ordinary Python conditionals appear only as program flow and numeric comparisons
(for example "is the best cosine above the threshold?").
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from database_service import TableSchema, quote_identifier

# --------------------------------------------------------------------------- constants
TAU = 0.75            # min nearest-member cosine for a word to count as evidence
TAU_STRICT = 0.99     # exact member of a cluster (used when a word must be *skipped* as structural)
TAU_VALUE = 0.90      # cell-value grounding needs near-exact matches
CONTEXT_LEFT = 3      # words left of a literal that describe its comparison
CONTEXT_RIGHT = 1     # words right of a literal (units like "dollars", "years")
AGG_WINDOW = 3        # words after an aggregate cue searched for its column
GROUP_WINDOW = 2      # words after a group cue searched for its column
KEEP_RATIO = 0.5      # SELECT columns kept if they score >= this fraction of the best
PREP_BOOST = 1.25     # table evidence multiplier for the word right after a preposition


# ============================================================================ 1. LEXER
_LEXER = re.compile(
    r"""'(?P<sq>[^']*)'"""                       # 'quoted text'
    r"""|"(?P<dq>[^"]*)\""""                     # "quoted text"
    r"""|(?P<sym>>=|<=|!=|<>|=|<|>)"""           # comparison symbols typed literally
    r"""|(?P<num>-?\d+(?:\.\d+)?)"""             # numeric literal
    r"""|(?P<word>[A-Za-z_][A-Za-z0-9_]*)"""     # word / identifier
    r"""|(?P<comma>,)"""
)


@dataclass(frozen=True)
class Token:
    kind: str                       # WORD | NUM | STR | SYM | COMMA
    text: str                       # normalised (lower-case for words)
    raw: str                        # original spelling (kept for inserted values)
    value: Optional[float] = None   # numeric value for NUM tokens


def _num(text: str) -> float | int:
    v = float(text)
    return int(v) if v.is_integer() else v


def lex(text: str) -> list[Token]:
    out: list[Token] = []
    for m in _LEXER.finditer(text):
        if m.group("sq") is not None or m.group("dq") is not None:
            s = m.group("sq") if m.group("sq") is not None else m.group("dq")
            out.append(Token("STR", s.lower(), s))
        elif m.group("sym"):
            out.append(Token("SYM", "!=" if m.group("sym") == "<>" else m.group("sym"), m.group("sym")))
        elif m.group("num"):
            out.append(Token("NUM", m.group("num"), m.group("num"), _num(m.group("num"))))
        elif m.group("word"):
            out.append(Token("WORD", m.group("word").lower(), m.group("word")))
        else:
            out.append(Token("COMMA", ",", ","))
    return out


# Spoken numbers: Vosk writes "fifty thousand", not "50000".  A value cannot be produced by
# cosine similarity, so spoken numerals are folded into numeric tokens by positional arithmetic.
_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate(
    "x x twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "x"}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000}
_NUMERALS = set(_UNITS) | set(_TENS) | set(_SCALES)


def _numeral_value(words: list[str]) -> int:
    total = current = 0
    for w in words:
        if w in _UNITS:
            current += _UNITS[w]
        elif w in _TENS:
            current += _TENS[w]
        elif w == "hundred":
            current = max(current, 1) * 100
        else:
            total += max(current, 1) * _SCALES[w]
            current = 0
    return total + current


def fold_numerals(tokens: list[Token]) -> list[Token]:
    """'older than twenty one' -> 'older than 21'.  A lone 'one' is left as a word."""
    out: list[Token] = []
    group: list[Token] = []

    def flush() -> None:
        if group and [g.text for g in group] != ["one"]:
            v = _numeral_value([g.text for g in group])
            out.append(Token("NUM", str(v), " ".join(g.raw for g in group), v))
        else:
            out.extend(group)
        group.clear()

    for i, t in enumerate(tokens):
        nxt = tokens[i + 1].text if i + 1 < len(tokens) else ""
        glue = (t.kind == "WORD" and t.text == "and" and group
                and group[-1].text in _SCALES and nxt in _NUMERALS)
        if t.kind == "WORD" and t.text in _NUMERALS:
            group.append(t)
        elif glue:
            continue                       # "one hundred AND five": 'and' is only glue here
        else:
            flush()
            out.append(t)
    flush()
    return out


# ============================================================ 2. THE SEMANTIC CLUSTERS
# Static clusters describe the SQL *slots*.  They are data that places each slot in the
# vector space; decisions are made by cosine similarity against them.
ROLE_CLUSTERS: dict[str, dict[str, str]] = {
    "INTENT": {
        "SELECT": "show list display get fetch find select give view see what which who tell",
        "INSERT": "insert add append put store register enroll",
        "DELETE": "delete remove erase discard purge",
        "CREATE": "create make build define",
        "UPDATE": "update change modify edit revise set increase raise boost decrease reduce",
        "DROP": "drop",
        "ALTER": "alter",
    },
    "RENAME": {"RENAME": "rename"},
    "ASSIGN": {"ASSIGN": "to as becomes"},                 # "set salary TO 70000", "rename a TO b"
    "COND": {"COND": "where whose when if"},               # starts the filter part of a sentence
    "COLWORD": {"COLWORD": "column field"},                # the word "column" (singular) as an object
    "DELTA": {"INC": "increase raise boost grow", "DEC": "decrease reduce"},   # relative updates
    "AGG": {
        "COUNT": "count many number",
        "SUM": "sum total altogether combined overall",
        "AVG": "average mean avg typical",
        "MIN": "minimum lowest min smallest fewest",
        "MAX": "maximum highest max largest biggest",
    },
    "OP": {
        "<": "under below less lower fewer smaller younger earlier before cheaper",
        ">": "over above greater more higher older later after exceeding exceeds",
        "=": "equal equals exactly",
        "BETWEEN": "between range",
    },
    "GROUP": {"GROUP": "per each by grouped group"},
    # ---- ordering / ranking / limiting
    "SORT": {"SORT": "sort sorted sorting order ordered ranked arrange arranged"},
    "DIR": {"ASC": "ascending asc", "DESC": "descending desc"},
    # exact superlative words only: "cheapest" must not be confused with the comparison "cheaper"
    "SUPER": {
        "HIGH": "highest largest biggest most greatest priciest costliest richest oldest latest newest",
        "LOW": "lowest smallest least cheapest poorest youngest earliest fewest bottom",
    },
    "TOPN": {"TOP": "top", "FIRST": "first limit", "BOTTOM": "bottom"},
    # ---- negation, ">= / <=" helpers, pattern matching, duplicates
    "NEG": {"NEG": "not except excluding without besides"},
    "ATLEAST": {"GE": "least", "LE": "most"},                 # only meaningful right after "at"
    "LIKEOP": {
        "STARTS": "starts start starting begins begin beginning",
        "ENDS": "ends end ending",
        "CONTAINS": "contains contain containing includes include including",
    },
    "DISTINCT": {"DISTINCT": "distinct unique different"},
    "DESCRIBE": {"DESCRIBE": "describe describes structure schema definition"},
    "COLSPLURAL": {"COLS": "columns fields attributes"},      # "what columns does X have"
    "OR": {"OR": "or"},
    "HAVING": {"HAVING": "having"},
    "PREP": {"PREP": "from into in of to on for at"},
    "BINDER": {"BINDER": "columns column fields field attributes values value set called named titled new "
                       "letter letters character characters"},
    "TABLEWORD": {"TABLEWORD": "table tables"},
    # Function words: never data.  (Deliberately tiny: content words such as "name" or
    # "amount" must stay usable as column names.)
    "FILLER": {"FILLER": "a an the and or is are was were be me my us we you i please with that this these "
                         "those there it its all any some also then than just only do does did can could "
                         "would should will as if so but not no have has had whom how row rows record records"},
    "TYPE_EXPLICIT": {
        "INTEGER": "integer int whole",
        "REAL": "real float double decimal numeric",
        "TEXT": "text string varchar char",
    },
    # Used when a column is created without an explicit type: the *name* decides the type.
    "TYPE_HINT": {
        "INTEGER": "id age count quantity qty year number stock rank level",
        "REAL": "price salary amount cost total rate weight height balance revenue fee discount",
    },
}


# Concept neighbourhoods.  A table / column whose NAME lands on a member of a concept
# (cosine >= TAU_VALUE) inherits the whole neighbourhood, so "older" finds a column called
# `age` and "staff" finds a table called `employees` without any per-database authoring.
# Add your own neighbourhoods here; the mechanism stays pure cosine similarity.
# Each concept applies to one scope: attribute concepts enrich COLUMNS, entity concepts enrich TABLES.
CONCEPTS: dict[str, tuple[str, str]] = {
    "NAME": ("COLUMN", "name names named called titled title"),
    "AGE": ("COLUMN", "age ages old older oldest younger youngest years aged"),
    "PRICE": ("COLUMN", "price prices cost costs costing cheap cheaper cheapest expensive pricey priciest costliest priced dollars rupees"),
    "PAY": ("COLUMN", "salary salaries pay wage wages income earn earning earnings paid richest poorest compensation raise"),
    "AMOUNT": ("COLUMN", "amount amounts total value spent revenue worth"),
    "QUANTITY": ("COLUMN", "quantity quantities units pieces"),
    "STOCK": ("COLUMN", "stock stocked available"),
    "PEOPLE": ("TABLE", "users user customers customer clients client people persons members employees "
                        "employee staff workers worker accounts"),
    "GOODS": ("TABLE", "products product items item goods inventory catalog merchandise"),
    "SALES": ("TABLE", "orders order sales sale purchases purchase transactions transaction billing payments"),
}


@dataclass(frozen=True)
class Prototype:
    kind: str            # a ROLE_CLUSTERS key, or TABLE / COLUMN
    label: str
    table: str
    column: str
    numeric: bool
    members: tuple[str, ...]


@dataclass(frozen=True)
class ValueProto:
    table: str
    column: str
    value: str
    words: tuple[str, ...]


@dataclass
class ValueMatch:
    proto: ValueProto
    token_idx: list[int]
    score: float


@dataclass
class Analysis:
    toks: list[Token]
    S: np.ndarray                    # (tokens x static prototypes) evidence matrix
    values: list[ValueMatch]
    prep_follow: set[int]            # token indices that directly follow a preposition


def subwords(identifier: str) -> tuple[str, ...]:
    """'hire_date' / 'HireDate' -> ('hire', 'date', 'hiredate'): lexical normalisation of a name."""
    parts = [p.lower() for p in re.split(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])", identifier) if p]
    words = [p for p in parts if len(p) >= 2]
    joined = "".join(parts)
    if len(parts) > 1 and joined not in words:
        words.append(joined)
    if len(parts) > 1:                                   # the identifier exactly as typed: "temp_notes"
        words.append(identifier.lower())
    for w in list(words):                                    # city <-> cities, category <-> categories
        if w.endswith("y") and len(w) > 2 and w[-2] not in "aeiou":
            words.append(w[:-1] + "ies")
        elif w.endswith("ies") and len(w) > 4:
            words.append(w[:-3] + "y")
    return tuple(dict.fromkeys(words or [joined or identifier.lower()]))


class VectorSpace:
    """
    Every schema element is a cluster of TF-IDF points (one point per member word).
    Character n-grams make the space tolerant to plurals, inflections and the small
    spelling variations a speech recogniser produces (cheap ~ cheaper, order ~ orders).
    Scoring by the NEAREST member keeps big and small clusters comparable.
    """

    def __init__(self, tables: list[TableSchema],
                 values: dict[tuple[str, str], list[str]]) -> None:
        protos: list[Prototype] = []
        for kind, clusters in ROLE_CLUSTERS.items():
            for label, text in clusters.items():
                protos.append(Prototype(kind, label, "", "", False, tuple(text.split())))
        self.exact_table_members: dict[str, tuple[str, ...]] = {}
        for t in tables:
            sw = subwords(t.table_name)
            self.exact_table_members[t.table_name] = sw
            protos.append(Prototype("TABLE", t.table_name, t.table_name, "", False, sw))
            for c in t.columns:
                protos.append(Prototype("COLUMN", c.name, t.table_name, c.name, c.numeric, subwords(c.name)))

        vprotos: list[ValueProto] = []
        for (table, col), vals in values.items():
            for v in vals:
                words = tuple(dict.fromkeys(w for w in re.findall(r"[a-z0-9]+", v.lower()) if len(w) >= 3))
                if words:
                    vprotos.append(ValueProto(table, col, v, words))

        concept_words = {c: (scope, text.split()) for c, (scope, text) in CONCEPTS.items()}
        vocab = sorted({w for p in protos for w in p.members} | {w for v in vprotos for w in v.words}
                       | {w for _sc, ws in concept_words.values() for w in ws})
        self.vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True)
        self.points = self.vectorizer.fit_transform(vocab)
        self.index = {w: i for i, w in enumerate(vocab)}

        # Enrich table / column clusters: inherit every concept whose members the name lands on.
        for k, p in enumerate(protos):
            if p.kind not in ("TABLE", "COLUMN"):
                continue
            own = self.points[[self.index[w] for w in p.members]]
            extra: list[str] = []
            for scope, words in concept_words.values():
                if scope != p.kind:
                    continue
                sim = cosine_similarity(own, self.points[[self.index[w] for w in words]])
                if sim.max() >= TAU_VALUE:
                    extra += [w for w in words if w not in p.members and w not in extra]
            if extra:
                protos[k] = Prototype(p.kind, p.label, p.table, p.column, p.numeric, p.members + tuple(extra))

        self.protos = protos
        self.vprotos = vprotos
        self.members = [np.array([self.index[w] for w in p.members]) for p in protos]
        self.kind = np.array([p.kind for p in protos])
        self.label = np.array([p.label for p in protos])
        self.table = np.array([p.table for p in protos])
        self.numeric = np.array([p.numeric for p in protos])

    def mask(self, kind: str, table: str | list[str] | set[str] | tuple[str, ...] | None = None,
             numeric: bool | None = None) -> np.ndarray:
        m = self.kind == kind
        if table is not None:
            if isinstance(table, (list, tuple, set)):
                m &= np.isin(self.table, list(table))
            else:
                m &= self.table == table
        if numeric is not None:
            m &= self.numeric == numeric
        return m

    def analyse(self, toks: list[Token]) -> Analysis:
        """Project the sentence into the space: build the evidence matrices."""
        word_pos = [i for i, t in enumerate(toks) if t.kind == "WORD"]
        S = np.zeros((len(toks), len(self.protos)))
        values: list[ValueMatch] = []
        if word_pos:
            cos = cosine_similarity(self.vectorizer.transform([toks[i].text for i in word_pos]), self.points)
            for p, m in enumerate(self.members):
                S[word_pos, p] = cos[:, m].max(axis=1)
            S[S < TAU] = 0.0

            # A word better explained as a table name ("orders") is not also a column word
            # (column `order_id`): drop its column evidence.
            tab_m, col_m = self.mask("TABLE"), self.mask("COLUMN")
            tab_ev, col_ev = S[:, tab_m].max(axis=1, initial=0.0), S[:, col_m].max(axis=1, initial=0.0)
            S[np.ix_(tab_ev > col_ev, col_m)] = 0.0

            # Cell-value grounding: a value matches only if EVERY one of its words is
            # matched near-exactly by some query word.
            for vp in self.vprotos:
                best_idx, best_sc = [], []
                for w in vp.words:
                    col = cos[:, self.index[w]]
                    k = int(col.argmax())
                    best_idx.append(word_pos[k])
                    best_sc.append(float(col[k]))
                if min(best_sc) >= TAU_VALUE:
                    values.append(ValueMatch(vp, best_idx, min(best_sc)))

        prep_mask = self.mask("PREP")
        follow: set[int] = set()
        for i in word_pos:
            if S[i, prep_mask].max() > 0:
                nxt = next((j for j in word_pos if j > i), None)
                if nxt is not None:
                    follow.add(nxt)
        return Analysis(toks, S, values, follow)


# ======================================================================= 3. TRANSLATOR
class TranslationError(Exception):
    """The sentence could not be resolved to a safe statement."""


@dataclass
class Translation:
    sql: Optional[str] = None
    error: Optional[str] = None
    kind: str = ""                       # SELECT | INSERT | DELETE | CREATE
    table: str = ""                      # primary table touched
    tables: list[str] = field(default_factory=list)   # all detected tables
    trace: list[str] = field(default_factory=list)   # human-readable vector evidence


@dataclass
class OrderSpec:
    """ORDER BY / LIMIT slot read from the sentence."""
    keys: list[tuple[str, str, str]] = field(default_factory=list)   # (table, column, ASC|DESC)
    limit: Optional[int] = None
    consumed: set[int] = field(default_factory=set)                   # tokens used by this slot


@dataclass
class Agg:
    label: str
    column: Optional[str]
    cue: int
    expr: str
    table: str = ""


def _fmt(v: float | int) -> str:
    return str(v)


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _best(scores: np.ndarray, labels: np.ndarray) -> Optional[str]:
    if scores.size == 0 or scores.max() <= 0:
        return None
    return str(labels[int(np.argmax(scores))])


def _col_expr(tbl: str, col: str, multi: bool) -> str:
    """Format a column reference: table.column when multi-table, else bare column."""
    if multi:
        return f'{quote_identifier(tbl)}.{quote_identifier(col)}'
    return quote_identifier(col)


class Translator:
    def __init__(self, tables: list[TableSchema], values: dict[tuple[str, str], list[str]],
                 db_service: Optional[object] = None) -> None:
        self.tables = {t.table_name: t for t in tables}
        self.space = VectorSpace(tables, values)
        self.db_service = db_service

    # ------------------------------------------------------------------ entry point
    def translate(self, text: str) -> Translation:
        tr = Translation()
        toks = fold_numerals(lex(text))
        if not any(t.kind == "WORD" for t in toks):
            tr.error = "Nothing to translate: please type a sentence."
            return tr
        try:
            a = self.space.analyse(toks)
            base = self._intent(a, tr)
            tr.kind, sub = self._resolve_kind(a, base, tr)
            if tr.kind == "CREATE":
                self._create(a, tr)
            else:
                if not self.tables:
                    raise TranslationError("The database has no tables yet. Try: "
                                           "'create table pets with columns name, age'.")
                tables = self._pick_table(a, tr)
                tr.tables = tables
                tr.table = tables[0]
                if tr.kind == "SELECT" and self._is_describe(a):
                    self._describe(tables[0], tr)
                elif tr.kind == "SELECT":
                    self._select(a, tables, tr)
                elif tr.kind == "DELETE":
                    self._delete(a, tables[0], tr)
                elif tr.kind == "INSERT":
                    self._insert(a, tables[0], tr)
                elif tr.kind == "UPDATE":
                    self._update(a, tables[0], tr)
                elif tr.kind == "DROP":
                    tr.sql = f"DROP TABLE {quote_identifier(tables[0])};"
                    tr.trace.append(f"DROP    <- table '{tables[0]}' (needs confirmation before it runs)")
                else:
                    self._alter(a, tables[0], sub, tr)
        except TranslationError as e:
            tr.sql, tr.error = None, str(e)
        return tr

    # ----------------------------------------------------------------- shared slots
    def _is_describe(self, a: Analysis) -> bool:
        """'describe employees', 'structure of employees', or 'columns of employees' (no other column named)."""
        if self._cue(a, "DESCRIBE") is not None:
            return True
        colw = self._cue(a, "COLSPLURAL")
        if colw is None or any(t.kind in ("NUM", "STR") for t in a.toks):
            return False
        col_m = self.space.mask("COLUMN")
        # only a describe request when no particular column is named ("show columns name and age" is a SELECT)
        return not any(i != colw and a.toks[i].kind == "WORD" and a.S[i, col_m].max() > 0
                       for i in range(len(a.toks)))

    def _describe(self, table: str, tr: Translation) -> None:
        """Structure of a table: one row per column (name, type, NOT NULL, primary key)."""
        lit = "'" + table.replace("'", "''") + "'"
        tr.sql = (f'SELECT name AS "column", type AS "type", "notnull" AS "not_null", pk AS "primary_key" '
                  f"FROM pragma_table_info({lit});")
        tr.trace.append(f"DESCRIBE <- structure cue => column list of table '{table}' (read-only, no data rows)")

    def _intent(self, a: Analysis, tr: Translation) -> str:
        """Which statement kind?  Argmax of summed evidence over the intent clusters."""
        sp = self.space
        m = sp.mask("INTENT")
        totals = a.S[:, m].sum(axis=0)
        label = _best(totals, sp.label[m]) or "SELECT"        # a bare question is a query
        cue = int(np.argmax(a.S[:, m][:, int(np.argmax(totals))])) if totals.max() > 0 else None
        word = f"'{a.toks[cue].text}'" if cue is not None else "no cue word (default)"
        tr.trace.append(f"INTENT  <- {word}  => {label}")
        return label

    def _cue(self, a: Analysis, kind: str, strict: bool = True) -> Optional[int]:
        """Index of the first word with (exact, if strict) evidence for any cluster of `kind`."""
        m, thr = self.space.mask(kind), (TAU_STRICT if strict else TAU)
        return next((i for i, t in enumerate(a.toks) if t.kind == "WORD" and a.S[i, m].max() >= thr), None)

    def _resolve_kind(self, a: Analysis, base: str, tr: Translation) -> tuple[str, str]:
        """
        The intent cluster gives a first guess; structural cue words refine it:
            remove/delete + 'table'   -> DROP TABLE        add + 'column'     -> ALTER ... ADD COLUMN
            remove/drop   + 'column'  -> ALTER ... DROP COLUMN       'rename' -> ALTER ... RENAME
        (each cue is itself a vector-space cluster, so this is still cosine evidence).
        """
        colw, tabw = self._cue(a, "COLWORD"), self._cue(a, "TABLEWORD")
        if self._cue(a, "RENAME", strict=False) is not None:
            kind, sub = "ALTER", "RENAME"
        elif base == "ALTER":
            int_m = self.space.mask("INTENT")
            has_drop = any(
                a.S[i, int_m].max() >= TAU and str(self.space.label[int_m][int(a.S[i, int_m].argmax())]) in ("DELETE", "DROP")
                for i, t in enumerate(a.toks) if t.kind == "WORD"
            )
            has_add = any(
                a.S[i, int_m].max() >= TAU and str(self.space.label[int_m][int(a.S[i, int_m].argmax())]) == "INSERT"
                for i, t in enumerate(a.toks) if t.kind == "WORD"
            )
            if has_drop or (colw is not None and any(t.text in ("drop", "delete", "remove") for t in a.toks)):
                kind, sub = "ALTER", "DROPCOL"
            elif has_add or colw is not None:
                kind, sub = "ALTER", "ADD"
            elif tabw is not None:
                kind, sub = "DROP", ""
            else:
                raise TranslationError("Say what to alter: 'add column phone to employees', "
                                       "'rename column city to town', or 'drop column phone from employees'.")
        elif base in ("INSERT", "CREATE") and colw is not None:
            kind, sub = "ALTER", "ADD"
        elif base in ("DELETE", "DROP") and colw is not None:
            kind, sub = "ALTER", "DROPCOL"
        elif base in ("DELETE", "DROP") and tabw is not None:
            kind, sub = "DROP", ""
        elif base == "DROP":
            kind, sub = "DROP", ""
        elif base == "SELECT" and self._cue(a, "DELTA") is not None:
            kind, sub = "UPDATE", ""                      # "give Bob a raise of 5000"
        else:
            kind, sub = base, ""
        if (kind, sub) != (base, ""):
            tr.trace.append(f"INTENT  <- structural cue words refine {base} => {kind}{' ' + sub if sub else ''}")
        return kind, sub

    def _pick_table(self, a: Analysis, tr: Translation) -> list[str]:
        """
        FROM slot: summed nearest-member evidence per table (+ preposition-position boost).
        Eliminates the single argmax operation constraint. Retains all table coordinates
        whose cumulative cosine similarity score crosses the threshold parameter.
        """
        sp = self.space
        boost = np.ones(len(a.toks))
        for j in a.prep_follow:
            boost[j] = PREP_BOOST

        # Grounded tokens belong exclusively to the grounded table
        grounded_tokens: dict[int, str] = {}
        for vm in a.values:
            for idx in vm.token_idx:
                grounded_tokens[idx] = vm.proto.table

        agg_m = sp.mask("AGG")

        ev: dict[str, float] = {}
        tbl_tokens: dict[str, set[int]] = {t: set() for t in self.tables}
        tbl_name_hits: dict[str, set[int]] = {t: set() for t in self.tables}

        for t in self.tables:
            t_mask = sp.mask("TABLE", t)
            c_mask = sp.mask("COLUMN", t)
            cols = t_mask | c_mask

            # Check direct table name matches
            exact_words = getattr(sp, "exact_table_members", {}).get(t, subwords(t))
            for i, tok in enumerate(a.toks):
                if tok.kind == "WORD" and tok.text in exact_words:
                    tbl_name_hits[t].add(i)

            tok_ev = a.S[:, cols].max(axis=1) * boost
            # Tokens grounded to other tables do not contribute to table t
            for i in range(len(a.toks)):
                if i in grounded_tokens and grounded_tokens[i] != t:
                    tok_ev[i] = 0.0
                # A token that is an aggregate cue for another table's column should not count as table evidence
                elif a.S[i, agg_m].max() >= TAU and not bool(tbl_name_hits[t] & {i}):
                    if i + 1 < len(a.toks) and a.S[i + 1, sp.mask("COLUMN")].max() >= TAU:
                        other_cols = sp.mask("COLUMN") & ~sp.mask("COLUMN", t)
                        if a.S[i + 1, other_cols].max() > a.S[i + 1, c_mask].max():
                            tok_ev[i] = 0.0

            ev[t] = float(tok_ev.sum())

            for i, sc in enumerate(tok_ev):
                if sc >= TAU:
                    tbl_tokens[t].add(i)

            val_ev = sum(vm.score for vm in a.values if vm.proto.table == t)
            ev[t] += val_ev
            if val_ev > 0:
                for vm in a.values:
                    if vm.proto.table == t:
                        tbl_tokens[t].update(vm.token_idx)

        ranked = sorted(ev.items(), key=lambda kv: kv[1], reverse=True)
        tr.trace.append("FROM    <- table evidence " + ", ".join(f"{k}={v:.2f}" for k, v in ranked))

        if not ranked or ranked[0][1] <= 0:
            raise TranslationError("I couldn't match your words to any table. Known tables: "
                                   + ", ".join(self.tables) + ".")

        # If user explicitly named tables (e.g. "orders and products", "from orders"),
        # select the explicitly named tables!
        direct_named_tables = [t for t in self.tables if tbl_name_hits[t]]
        if direct_named_tables:
            return sorted(direct_named_tables, key=lambda t: ev[t], reverse=True)

        # Ambiguity check: if top tables have identical scores and their token evidence
        # is completely overlapping (e.g. neither table was named and both matched the same single column word)
        if len(ranked) > 1 and np.isclose(ranked[0][1], ranked[1][1]):
            t0, t1 = ranked[0][0], ranked[1][0]
            if not tbl_name_hits[t0] and not tbl_name_hits[t1] and tbl_tokens[t0] == tbl_tokens[t1]:
                raise TranslationError(f"Ambiguous table: '{t0}' and '{t1}' match equally. "
                                       "Please name the table.")

        best_score = ranked[0][1]
        threshold = TAU # 0.75 threshold parameter
        selected: list[str] = [ranked[0][0]]
        claimed_tokens = set(tbl_tokens[ranked[0][0]])

        for t, score in ranked[1:]:
            if score < threshold:
                continue
            if score < KEEP_RATIO * best_score:
                continue
            has_direct_name = bool(tbl_name_hits[t])
            has_unique_tokens = bool(tbl_tokens[t] - claimed_tokens)
            if has_direct_name or has_unique_tokens:
                selected.append(t)
                claimed_tokens.update(tbl_tokens[t])

        return selected

    def _table_position(self, a: Analysis, table: str) -> int:
        """Index of the token that names the table (-1 if it was matched only through columns)."""
        m = self.space.mask("TABLE", table)
        col = a.S[:, m].max(axis=1)
        return int(col.argmax()) if col.max() > 0 else -1

    def _find_join_anchor(self, t1: str, t2: str) -> str:
        """
        Dynamically map matching primary-to-foreign key structural anchors on the fly
        based on live scanned schema metadata (from SQLite system catalogs) or VSM column alignment.
        """
        # 1. Live foreign key constraints from SQLite schema dynamically scanned
        fks: list = []
        if self.db_service is not None and hasattr(self.db_service, "get_foreign_keys"):
            fks = self.db_service.get_foreign_keys(t1) + self.db_service.get_foreign_keys(t2)
        else:
            if t1 in self.tables:
                fks += getattr(self.tables[t1], "foreign_keys", [])
            if t2 in self.tables:
                fks += getattr(self.tables[t2], "foreign_keys", [])

        for fk in fks:
            if fk.from_table.lower() == t1.lower() and fk.to_table.lower() == t2.lower():
                return f"{quote_identifier(t1)}.{quote_identifier(fk.from_column)} = {quote_identifier(t2)}.{quote_identifier(fk.to_column)}"
            if fk.from_table.lower() == t2.lower() and fk.to_table.lower() == t1.lower():
                return f"{quote_identifier(t1)}.{quote_identifier(fk.to_column)} = {quote_identifier(t2)}.{quote_identifier(fk.from_column)}"

        cols1 = [c.name for c in self.tables[t1].columns] if t1 in self.tables else []
        cols2 = [c.name for c in self.tables[t2].columns] if t2 in self.tables else []
        pks1 = getattr(self.tables[t1], "primary_keys", []) if t1 in self.tables else []
        pks2 = getattr(self.tables[t2], "primary_keys", []) if t2 in self.tables else []

        # 2. Check for matching column names between t1 and t2 (excluding generic primary keys)
        for c1 in cols1:
            for c2 in cols2:
                if c1.lower() == c2.lower() and c1.lower() not in ("id", "pk"):
                    return f"{quote_identifier(t1)}.{quote_identifier(c1)} = {quote_identifier(t2)}.{quote_identifier(c2)}"

        # 3. Vector Space Model column similarity (cosine similarity between column vectors)
        t1_stem = t1.rstrip("s").lower()
        t2_stem = t2.rstrip("s").lower()
        sp = self.space
        m1 = np.where((sp.kind == "COLUMN") & (sp.table == t1))[0]
        m2 = np.where((sp.kind == "COLUMN") & (sp.table == t2))[0]
        best_pair = None
        best_sim = -1.0
        if len(m1) > 0 and len(m2) > 0:
            for idx1 in m1:
                p1 = sp.protos[idx1]
                pts1 = sp.points[[sp.index[w] for w in p1.members]]
                for idx2 in m2:
                    p2 = sp.protos[idx2]
                    pts2 = sp.points[[sp.index[w] for w in p2.members]]
                    sim = float(cosine_similarity(pts1, pts2).max())
                    if p1.numeric == p2.numeric:
                        sim += 0.1
                    if t2_stem in p1.column.lower() or t1_stem in p2.column.lower():
                        sim += 0.5
                    if p1.column in pks1 and p2.column in pks2:
                        sim -= 0.6
                    if sim > best_sim:
                        best_sim = sim
                        best_pair = (p1.column, p2.column)

        if best_pair and best_sim >= 0.5:
            return f"{quote_identifier(t1)}.{quote_identifier(best_pair[0])} = {quote_identifier(t2)}.{quote_identifier(best_pair[1])}"

        # 4. Check FK naming patterns
        t1_sing = t1[:-1] if t1.endswith("s") else t1
        t2_sing = t2[:-1] if t2.endswith("s") else t2

        for c1 in cols1:
            c1_l = c1.lower()
            if c1_l in (f"{t2_sing}_id", f"{t2}_id"):
                target = pks2[0] if pks2 else ("id" if "id" in [c.lower() for c in cols2] else cols2[0])
                for c2 in cols2:
                    if c2.lower() == target.lower():
                        return f"{quote_identifier(t1)}.{quote_identifier(c1)} = {quote_identifier(t2)}.{quote_identifier(c2)}"

        for c2 in cols2:
            c2_l = c2.lower()
            if c2_l in (f"{t1_sing}_id", f"{t1}_id"):
                target = pks1[0] if pks1 else ("id" if "id" in [c.lower() for c in cols1] else cols1[0])
                for c1 in cols1:
                    if c1.lower() == target.lower():
                        return f"{quote_identifier(t1)}.{quote_identifier(c1)} = {quote_identifier(t2)}.{quote_identifier(c2)}"

        # Fallback
        c1_fallback = pks1[0] if pks1 else (cols1[0] if cols1 else "id")
        c2_fallback = pks2[0] if pks2 else (cols2[0] if cols2 else "id")
        return f"{quote_identifier(t1)}.{quote_identifier(c1_fallback)} = {quote_identifier(t2)}.{quote_identifier(c2_fallback)}"

    def _build_from_clause(self, tables: list[str]) -> str:
        """Dynamically append relational INNER JOIN syntax onto the output SQL string compiler."""
        if len(tables) <= 1:
            return quote_identifier(tables[0])
        clause = quote_identifier(tables[0])
        joined = [tables[0]]
        for t in tables[1:]:
            anchor = None
            if self.db_service is not None and hasattr(self.db_service, "get_foreign_keys"):
                for prev in joined:
                    fks = self.db_service.get_foreign_keys(prev) + self.db_service.get_foreign_keys(t)
                    for fk in fks:
                        if (fk.from_table.lower() == prev.lower() and fk.to_table.lower() == t.lower()) or \
                           (fk.from_table.lower() == t.lower() and fk.to_table.lower() == prev.lower()):
                            anchor = self._find_join_anchor(prev, t)
                            break
                    if anchor:
                        break
            if not anchor:
                anchor = self._find_join_anchor(joined[0], t)
            clause += f" INNER JOIN {quote_identifier(t)} ON {anchor}"
            joined.append(t)
        return clause

    def _best_proto(self, scores: np.ndarray, mask: np.ndarray) -> Optional[Prototype]:
        if scores.size == 0 or scores.max() <= 0:
            return None
        indices = np.where(mask)[0]
        return self.space.protos[indices[int(np.argmax(scores))]]

    # ------------------------------------------------------------ small shared helpers
    def _label(self, a: Analysis, i: int, kind: str, thr: float = TAU_STRICT) -> Optional[str]:
        """Label of the best cluster of `kind` that word i belongs to EXACTLY (cosine >= thr)."""
        if a.toks[i].kind != "WORD":
            return None
        m = self.space.mask(kind)
        ev = a.S[i, m]
        if ev.size == 0 or ev.max() < thr:
            return None
        return str(self.space.label[m][int(ev.argmax())])

    @staticmethod
    def _mask(a: Analysis, idx: set[int]) -> Analysis:
        """Copy of the analysis with the given tokens removed from every later slot."""
        toks = [Token("SKIP", "", "") if i in idx else t for i, t in enumerate(a.toks)]
        S = a.S.copy()
        S[sorted(idx)] = 0.0
        vals = [vm for vm in a.values if not (set(vm.token_idx) & idx)]
        return Analysis(toks, S, vals, a.prep_follow)

    # ----------------------------------------------------------- ORDER BY / LIMIT slot
    def _ordering(self, a: Analysis, tables: list[str], tr: Translation) -> OrderSpec:
        """
        sort cue ("sorted by", "ordered by") or superlative + number ("top 3 highest paid",
        "5 cheapest", "first 10")  ->  ORDER BY <column> ASC|DESC  [LIMIT n].
        Which word is a cue is decided by exact cluster membership; the column by cosine evidence.
        """
        sp, toks = self.space, a.toks
        spec = OrderSpec()
        words = [i for i, t in enumerate(toks) if t.kind == "WORD"]
        if not words:
            return spec
        pos = {w: k for k, w in enumerate(words)}
        agg_m, tab_m = sp.mask("AGG"), sp.mask("TABLE")
        col_all, col_num = sp.mask("COLUMN", tables), sp.mask("COLUMN", tables, True)
        exact_tables = {w for ws in sp.exact_table_members.values() for w in ws}

        # 1) sort cue.  A word that names a table ("orders by category") is not a sort cue.
        sort_cue = None
        for i in words:
            if self._label(a, i, "SORT") is None or toks[i].text in exact_tables:
                continue
            k = pos[i]
            by_next = k + 1 < len(words) and self._label(a, words[k + 1], "GROUP") is not None
            if a.S[i, tab_m].max() >= TAU and not by_next:
                continue
            sort_cue = i
            break

        # 2) superlatives ("highest", "cheapest", "most") and explicit asc / desc
        sups: list[tuple[int, str, bool]] = []            # (token, ASC|DESC, also an aggregate word?)
        for i in words:
            pol = self._label(a, i, "SUPER")
            k = pos[i]
            if pol is None or (k and toks[words[k - 1]].text == "at"):    # "at least" is a comparison
                continue
            sups.append((i, "DESC" if pol == "HIGH" else "ASC", bool(a.S[i, agg_m].max() >= TAU_STRICT)))
        dirs = [(i, str(self._label(a, i, "DIR"))) for i in words if self._label(a, i, "DIR")]

        # 3) limit: "top 3", "first 5", "limit 10"  or  "3 cheapest"
        limit_cue: Optional[tuple[int, str]] = None
        for i in words:
            lbl = self._label(a, i, "TOPN")
            if lbl is None:
                continue
            j = next((j for j in range(i + 1, min(len(toks), i + 3)) if toks[j].kind == "NUM"), None)
            if j is not None and float(toks[j].value) >= 1 and float(toks[j].value).is_integer():
                spec.limit = int(toks[j].value)
                limit_cue = (i, lbl)
                spec.consumed |= {i, j}
                break
        if spec.limit is None:
            for i, _pol, _agg in sups:
                if i > 0 and toks[i - 1].kind == "NUM" and float(toks[i - 1].value) >= 1 \
                        and float(toks[i - 1].value).is_integer():
                    spec.limit = int(toks[i - 1].value)
                    spec.consumed.add(i - 1)
                    break

        # superlatives that are ALSO aggregate words ("highest salary" = MAX) only rank rows
        # when a sort cue or a limit is present; otherwise the aggregate path keeps them.
        ranking = [x for x in sups if (not x[2]) or sort_cue is not None or spec.limit is not None]
        if sort_cue is None and spec.limit is None and not ranking:
            return OrderSpec()

        # 4) direction: an explicit asc/desc wins, then the first superlative, then the default
        first = min([(i, d) for i, d in dirs] + [(i, d) for i, d, _ in ranking], default=None)
        direction = first[1] if first else ("DESC" if limit_cue and limit_cue[1] == "TOP" else "ASC")
        if dirs:
            direction = min(dirs)[1]

        # 5) the column(s) to order by
        candidates: list[int] = []
        by_word: Optional[int] = None
        if sort_cue is not None:
            for j in range(sort_cue + 1, min(len(toks), sort_cue + 8)):
                if toks[j].kind in ("NUM", "STR"):
                    break
                if toks[j].kind == "WORD":
                    candidates.append(j)
        else:
            for i, _pol, _agg in ranking:
                candidates += [j for j in range(max(0, i - 3), min(len(toks), i + 4)) if toks[j].kind == "WORD"]
            if limit_cue is not None:                          # "top 5 products BY price"
                by_word = next((j for j in words if j > limit_cue[0] and self._label(a, j, "GROUP")), None)
                if by_word is not None:
                    candidates += [j for j in words if by_word < j <= by_word + 4]
        candidates = list(dict.fromkeys(candidates))
        skip_tok = {i for i, _ in dirs} | {i for i, _, _ in ranking}

        idxs = np.where(col_all)[0]
        used: list[int] = []
        found: list[tuple[str, str]] = []
        if sort_cue is not None:
            for j in candidates:
                if j in skip_tok or a.S[j, col_all].max() <= 0:
                    continue
                pr = sp.protos[idxs[int(a.S[j, col_all].argmax())]]
                if (pr.table, pr.column) not in found and len(found) < 3:
                    found.append((pr.table, pr.column))
                    used.append(j)
        elif candidates:
            for mask in (col_num, col_all):                    # a ranking column is usually numeric
                scores = a.S[candidates][:, mask].sum(axis=0)
                best = self._best_proto(scores, mask)
                if best is not None:
                    found.append((best.table, best.column))
                    mi = np.where(mask)[0]
                    k = mi[int(np.argmax(scores))]
                    used = [j for j in candidates if a.S[j, k] > 0 and j not in skip_tok]
                    break

        if not found:
            if sort_cue is not None:
                raise TranslationError("Sort by which column? Columns: "
                                       + ", ".join(sp.label[col_all]) + ".")
            if ranking or by_word is not None:
                if spec.limit is not None:
                    raise TranslationError("Which column should I rank by? Columns: "
                                           + ", ".join(sp.label[col_num]) + ".")
                return OrderSpec()                             # nothing to rank by: old behaviour
            # "first 5 employees": just a limit
            spec.keys = []
        else:
            spec.keys = [(t, c, direction) for t, c in found]

        # a lone singular superlative ("the cheapest product") means one row
        if spec.limit is None and sort_cue is None and spec.keys and len(tables) == 1:
            tm = sp.mask("TABLE", tables[0])
            holder = int(a.S[:, tm].max(axis=1).argmax()) if a.S[:, tm].max() > 0 else -1
            if holder >= 0 and not toks[holder].text.endswith("s"):
                spec.limit = 1

        spec.consumed |= skip_tok | set(used)
        if sort_cue is not None:
            spec.consumed.add(sort_cue)
            nb = next((j for j in candidates if self._label(a, j, "GROUP")), None)
            if nb is not None:
                spec.consumed.add(nb)
        if by_word is not None:
            spec.consumed.add(by_word)
        if spec.keys:
            tr.trace.append("ORDER BY <- " + ", ".join(f"'{c}' {d}" for _t, c, d in spec.keys)
                            + (" (sort / superlative cue)" if sort_cue is None else " (sort cue)"))
        if spec.limit is not None:
            tr.trace.append(f"LIMIT   <- {spec.limit}")
        return spec

    # ------------------------------------------------------------------- aggregates
    def _aggregates(self, a: Analysis, tables: list[str] | str, tr: Translation) -> list[Agg]:
        if isinstance(tables, str):
            tables = [tables]
        sp = self.space
        multi = len(tables) > 1
        agg_m = sp.mask("AGG")
        col_all = sp.mask("COLUMN", tables)
        col_num = sp.mask("COLUMN", tables, True)
        cues = []
        for i, t in enumerate(a.toks):
            if t.kind != "WORD":
                continue
            ev = a.S[i, agg_m]
            if ev.max() <= 0:
                continue
            # A word that is also a real column of this table belongs to the schema, not to the aggregate slot.
            if a.S[i, col_all].max() >= ev.max():
                continue
            cues.append((i, str(sp.label[agg_m][int(ev.argmax())])))
        aggs: list[Agg] = []
        for k, (i, label) in enumerate(cues):
            if label == "COUNT":
                aggs.append(Agg(label, None, i, "COUNT(*)"))
                tr.trace.append(f"SELECT  <- '{a.toks[i].text}' => COUNT(*)")
                continue
            stop = cues[k + 1][0] if k + 1 < len(cues) else len(a.toks)
            window = [j for j in range(i + 1, min(stop, i + 1 + AGG_WINDOW)) if a.toks[j].kind == "WORD"]
            scores = a.S[window][:, col_num].sum(axis=0) if window else np.array([])
            col_proto = self._best_proto(scores, col_num)
            if col_proto is None:
                opts = ", ".join(sp.label[col_num]) or "(no numeric columns)"
                t_names = ", ".join(tables)
                raise TranslationError(f"Which column should I compute the {label.lower()} of? "
                                       f"Numeric columns of '{t_names}': {opts}.")
            expr = f"{label}({_col_expr(col_proto.table, col_proto.column, multi)})"
            aggs.append(Agg(label, col_proto.column, i, expr, table=col_proto.table))
            tr.trace.append(f"SELECT  <- '{a.toks[i].text}' => {label}; column '{col_proto.column}' follows the cue")
        return aggs

    def _groups(self, a: Analysis, tables: list[str] | str, tr: Translation) -> list[tuple[str, str, int, int]]:
        """GROUP BY slot: returns list of (table, column, cue_idx, tok_idx)."""
        if isinstance(tables, str):
            tables = [tables]
        sp = self.space
        grp_m = sp.mask("GROUP")
        col_all = sp.mask("COLUMN", tables)
        indices = np.where(col_all)[0]
        out: list[tuple[str, str, int, int]] = []
        for i, t in enumerate(a.toks):
            if t.kind != "WORD" or a.S[i, grp_m].max() <= 0:
                continue
            window = [j for j in range(i + 1, min(len(a.toks), i + 1 + GROUP_WINDOW)) if a.toks[j].kind == "WORD"]
            best_j, best_proto, best_s = None, None, 0.0
            for j in window:
                s = a.S[j, col_all]
                if s.max() > best_s:
                    best_j = j
                    best_proto = sp.protos[indices[int(s.argmax())]]
                    best_s = float(s.max())
            if best_proto is not None and all((best_proto.table, best_proto.column) != (g[0], g[1]) for g in out):
                out.append((best_proto.table, best_proto.column, i, best_j))
                tr.trace.append(f"GROUP BY <- '{t.text}' + next word '{a.toks[best_j].text}' => {best_proto.column}")
        return out

    # ------------------------------------------------------------------- WHERE slot
    def _conditions(self, a: Analysis, tables: list[str] | str, aggs: list[Agg], has_groups: bool,
                    allow_having: bool, tr: Translation) -> tuple[list[str], list[str], set[int]]:
        if isinstance(tables, str):
            tables = [tables]
        sp = self.space
        multi = len(tables) > 1
        op_m, having_m = sp.mask("OP"), sp.mask("HAVING")
        col_all = sp.mask("COLUMN", tables)
        col_num = sp.mask("COLUMN", tables, True)
        col_txt = sp.mask("COLUMN", tables, False)
        toks = a.toks
        lit_idx = [i for i, t in enumerate(toks) if t.kind in ("NUM", "STR")]
        entries: list[dict] = []          # every WHERE condition with the token span it came from
        having: list[str] = []
        consumed: set[int] = set()
        skip: set[int] = set()
        like_used: set[int] = set()
        invert = {"=": "!=", "!=": "=", ">": "<=", "<": ">=", ">=": "<", "<=": ">"}

        def add(text: str, lo: int, hi: int, col: str = "", op: str = "", rhs: str = "") -> None:
            entries.append({"text": text, "lo": lo, "hi": hi, "col": col, "op": op, "rhs": rhs})

        def compose(lhs: str, op: str, v1: str, v2: str | None, neg: bool = False) -> str:
            if op == "BETWEEN":
                return f"{lhs} {'NOT ' if neg else ''}BETWEEN {v1} AND {v2}"
            return f"{lhs} {invert.get(op, op) if neg else op} {v1}"

        def negated(first: int) -> bool:
            """A negation word ('not', 'except', ...) among the 3 words right before token `first`."""
            seen = 0
            for j in range(first - 1, -1, -1):
                if toks[j].kind == "WORD":
                    if self._label(a, j, "NEG"):
                        return True
                    seen += 1
                    if seen == CONTEXT_LEFT:
                        break
                elif toks[j].kind in ("NUM", "STR"):
                    break
            return False

        # ---- pattern matching: "name starts with A", "city contains lon", "ends with son"
        for i, t in enumerate(toks):
            kind = self._label(a, i, "LIKEOP")
            if kind is None or a.S[i, col_all].max() >= TAU_STRICT:      # 'start_date' is a column, not a cue
                continue
            rest = list(range(i + 1, len(toks)))
            func = [k for k in rest if toks[k].kind == "WORD" and (self._label(a, k, "FILLER") or self._label(a, k, "BINDER"))]
            j = next((k for k in rest if k not in func), None)
            if j is None and func:                       # "starts with A" / "contains an": the value IS a function word
                j = func[-1]
            if j is None or toks[j].kind not in ("WORD", "STR", "NUM"):
                continue
            left_w = [k for k in range(i - 1, max(-1, i - 5), -1) if toks[k].kind == "WORD"]
            sc = a.S[left_w][:, col_txt].sum(axis=0) if left_w else np.array([])
            proto = self._best_proto(sc, col_txt) or self._best_proto(
                a.S[left_w][:, col_all].sum(axis=0) if left_w else np.array([]), col_all) \
                or self._best_proto(a.S[:, col_txt].sum(axis=0), col_txt)
            if proto is None:
                raise TranslationError(f"I found a '{t.text}' pattern ('{toks[j].raw}') but can't tell which "
                                       "column it applies to. Please name the column.")
            text = toks[j].raw
            pat = {"STARTS": f"{text}%", "ENDS": f"%{text}", "CONTAINS": f"%{text}%"}[kind]
            neg = negated(i)
            lhs = _col_expr(proto.table, proto.column, multi)
            add(f"{lhs} {'NOT ' if neg else ''}LIKE {_lit(pat)}", i, j)
            tr.trace.append(f"WHERE   <- '{t.text}' => {'NOT ' if neg else ''}LIKE {pat!r} on column '{proto.column}'")
            consumed |= {i, j}
            consumed.update(k for k in left_w if a.S[k, col_all].max() > 0)
            like_used |= {i, j}
            if toks[j].kind in ("NUM", "STR"):
                skip.add(j)

        for n, i in enumerate(lit_idx):
            if i in skip:
                continue
            boundary = lit_idx[n - 1] if n else -1
            left, words = [], 0
            for j in range(i - 1, boundary, -1):                 # nearest word first
                if toks[j].kind == "WORD":
                    if words == CONTEXT_LEFT:
                        break
                    words += 1
                    left.append(j)
                elif toks[j].kind == "SYM":
                    left.append(j)
            lw = [j for j in left if toks[j].kind == "WORD"]
            right = [j for j in range(i + 1, min(len(toks), i + 1 + CONTEXT_RIGHT)) if toks[j].kind == "WORD"]
            ctx = lw + right
            consumed.update(ctx)
            consumed.update(j for j in left if toks[j].kind == "SYM")

            # comparison operator: typed symbol wins, else cosine evidence in the window
            sym = next((toks[j].text for j in left if toks[j].kind == "SYM"), None)
            op = sym or _best(a.S[lw][:, op_m].sum(axis=0) if lw else np.array([]), sp.label[op_m]) or "="
            # "at least 90000" / "at most 50": the word after 'at' decides
            for j in lw:
                kind = self._label(a, j, "ATLEAST")
                prev = next((k for k in range(j - 1, -1, -1) if toks[k].kind == "WORD"), None)
                if kind and prev is not None and toks[prev].text == "at":
                    op = ">=" if kind == "GE" else "<="
                    consumed.add(prev)
            for j in lw:                                          # "no more than 60000" -> <=, "no less than" -> >=
                if toks[j].text == "no":
                    nxt_w = next((k for k in range(j + 1, i) if toks[k].kind == "WORD"), None)
                    if nxt_w is not None and op in (">", "<") and a.S[nxt_w, op_m].max() > 0:
                        op = "<=" if op == ">" else ">="
                        consumed.add(j)
                        before = next((k for k in range(min(lw) - 1, boundary, -1) if toks[k].kind == "WORD"), None)
                        if before is not None and before not in ctx:      # the column word just before "no ..."
                            ctx.append(before)
                            consumed.add(before)
            neg = any(self._label(a, j, "NEG") for j in lw)       # "not older than 30"

            is_num = toks[i].kind == "NUM"
            v1 = _fmt(toks[i].value) if is_num else _lit(toks[i].raw)
            v2 = None
            hi_idx = i
            if op == "BETWEEN":
                nxt = next((j for j in lit_idx if j > i and toks[j].kind == "NUM"), None)
                if not is_num or nxt is None:
                    raise TranslationError("'between' needs two numbers, e.g. 'between 10 and 50'.")
                lo, hi = sorted((toks[i].value, toks[nxt].value))
                v1, v2 = _fmt(lo), _fmt(hi)
                skip.add(nxt)
                hi_idx = nxt
                tail = []                                         # units after the 2nd number: "years old"
                for j in range(nxt + 1, min(len(toks), nxt + 3)):
                    if toks[j].kind != "WORD" or self._label(a, j, "GROUP"):
                        break
                    tail.append(j)
                consumed.update(tail)
                ctx = ctx + tail

            if is_num:
                col_scores = a.S[ctx][:, col_num].sum(axis=0) if ctx else np.array([])
                col_proto = self._best_proto(col_scores, col_num)
                having_cue = bool(lw) and a.S[lw][:, having_m].max() > 0
                agg_hit = [g for g in aggs if g.cue in ctx]

                # A comparison that targets an aggregate belongs in HAVING.
                if allow_having and (agg_hit or having_cue or (col_proto is None and aggs and has_groups)):
                    chosen = agg_hit[0] if agg_hit else (aggs[0] if aggs else None)
                    expr = chosen.expr if chosen else "COUNT(*)"
                    having.append(compose(expr, op, v1, v2, neg))
                    tr.trace.append(f"HAVING  <- number {toks[i].raw} (regex literal), operator '{op}' => {expr}")
                    continue
                if col_proto is None:                                   # fall back to the whole sentence
                    col_proto = self._best_proto(a.S[:, col_num].sum(axis=0), col_num)
                if col_proto is None:
                    opts = ", ".join(sp.label[col_num]) or "(no numeric columns)"
                    t_names = ", ".join(tables)
                    raise TranslationError(f"I found the number {toks[i].raw} but can't tell which column it "
                                           f"filters. Numeric columns of '{t_names}': {opts}. Please name one.")
            else:
                col_scores = a.S[ctx][:, col_all].sum(axis=0) if ctx else np.array([])
                col_proto = self._best_proto(col_scores, col_all) or self._best_proto(a.S[:, col_txt].sum(axis=0), col_txt)
                if col_proto is None:
                    raise TranslationError(f"I found the text '{toks[i].raw}' but can't tell which column it "
                                           "filters. Please name the column.")
            col_target = _col_expr(col_proto.table, col_proto.column, multi)
            eff = invert.get(op, op) if neg and op != "BETWEEN" else op
            add(compose(col_target, op, v1, v2, neg), i, hi_idx, col_target, eff, v1)
            tr.trace.append(f"WHERE   <- literal {toks[i].raw!r} (regex), operator '{eff}', column '{col_proto.column}'")

        # Cell-value grounding: words that match an indexed value of these tables' columns.
        last_neg: dict[str, tuple[bool, int]] = {}
        for vm in sorted(a.values, key=lambda v: min(v.token_idx)):
            if vm.proto.table not in tables or set(vm.token_idx) & like_used:
                continue
            col_target = _col_expr(vm.proto.table, vm.proto.column, multi)
            lo, hi = min(vm.token_idx), max(vm.token_idx)
            neg = negated(lo)
            prev = last_neg.get(col_target)
            if not neg and prev and prev[0]:       # "not in london or paris": the negation carries over
                between = range(prev[1] + 1, lo)
                neg = all(toks[j].kind in ("COMMA", "SKIP") or (toks[j].kind == "WORD" and (
                    self._label(a, j, "OR") or self._label(a, j, "FILLER") or self._label(a, j, "PREP")))
                    for j in between)
            last_neg[col_target] = (neg, hi)
            op = "!=" if neg else "="
            cond = f"{col_target} {op} {_lit(vm.proto.value)}"
            if not any(e["text"] == cond for e in entries):
                add(cond, lo, hi, col_target, op, _lit(vm.proto.value))
                tr.trace.append(f"WHERE   <- word(s) {[toks[j].text for j in vm.token_idx]} match the stored "
                                f"value '{vm.proto.value}' of column '{vm.proto.column}' (cosine {vm.score:.2f})")
            consumed.update(vm.token_idx)
        return self._finalize_where(a, entries), having, consumed

    def _finalize_where(self, a: Analysis, entries: list[dict]) -> list[str]:
        """
        1. Several equalities on ONE column mean "any of them":  city IN ('London', 'Paris')
           (several inequalities mean "none of them":            city NOT IN (...)).
        2. An 'or' word between two conditions joins them with OR; otherwise they are ANDed.
           AND binds tighter than OR, exactly as in SQL, so each OR-branch is parenthesised.
        """
        groups: dict[tuple[str, str], list[dict]] = {}
        for e in entries:
            if e["op"] in ("=", "!=") and e["col"]:
                groups.setdefault((e["col"], e["op"]), []).append(e)
        merged: list[dict] = []
        done: set[tuple[str, str]] = set()
        for e in entries:
            key = (e["col"], e["op"])
            if e["op"] in ("=", "!=") and e["col"] and len(groups[key]) > 1:
                if key in done:
                    continue
                done.add(key)
                g = groups[key]
                vals = ", ".join(dict.fromkeys(x["rhs"] for x in g))
                text = f"{e['col']} {'IN' if e['op'] == '=' else 'NOT IN'} ({vals})"
                merged.append({"text": text, "lo": min(x["lo"] for x in g), "hi": max(x["hi"] for x in g)})
            else:
                merged.append(e)
        if len(merged) < 2:
            return [e["text"] for e in merged]

        def or_between(x: dict, y: dict) -> bool:
            if x["hi"] < y["lo"]:
                rng = range(x["hi"] + 1, y["lo"])
            elif y["hi"] < x["lo"]:
                rng = range(y["hi"] + 1, x["lo"])
            else:
                return False
            return any(self._label(a, j, "OR") for j in rng)

        ordered = sorted(merged, key=lambda e: e["lo"])
        links = [or_between(ordered[k - 1], ordered[k]) for k in range(1, len(ordered))]
        if not any(links):
            return [e["text"] for e in merged]                    # plain AND: keep the original order
        branches: list[list[str]] = [[ordered[0]["text"]]]
        for k, is_or in enumerate(links, start=1):
            if is_or:
                branches.append([ordered[k]["text"]])
            else:
                branches[-1].append(ordered[k]["text"])
        return [" OR ".join(f"({' AND '.join(b)})" if len(b) > 1 else b[0] for b in branches)]

    # ----------------------------------------------------------------------- SELECT
    def _select(self, a: Analysis, tables: list[str] | str, tr: Translation) -> None:
        if isinstance(tables, str):
            tables = [tables]
        sp = self.space
        multi = len(tables) > 1
        order = self._ordering(a, tables, tr)               # ORDER BY / LIMIT slot
        if order.consumed:
            a = self._mask(a, order.consumed)
        dist_idx = {i for i, t in enumerate(a.toks) if t.kind == "WORD" and self._label(a, i, "DISTINCT")}
        if dist_idx:
            a = self._mask(a, dist_idx)
        aggs = self._aggregates(a, tables, tr)
        groups = self._groups(a, tables, tr)
        where, having, consumed = self._conditions(a, tables, aggs, bool(groups), True, tr)
        consumed.update(g.cue for g in aggs)
        for g in aggs:                                           # every word of the aggregate's column name
            if g.column:                                         # ("dropout rate" is ONE column: both words)
                match_indices = np.where((sp.kind == "COLUMN") & (sp.label == g.column))[0]
                if len(match_indices) > 0:
                    k = int(match_indices[0])
                    consumed.update(j for j in range(g.cue + 1, min(len(a.toks), g.cue + 1 + AGG_WINDOW))
                                    if a.toks[j].kind == "WORD" and a.S[j, k] > 0)
        for _tbl, _col, cue, tok in groups:
            consumed.update((cue, tok))

        # plain columns: evidence in the words not already used by another slot
        col_all = sp.mask("COLUMN", tables)
        free = [i for i, t in enumerate(a.toks) if t.kind == "WORD" and i not in consumed]
        scores = a.S[free][:, col_all].sum(axis=0) if free else np.zeros(int(col_all.sum()))
        indices = np.where(col_all)[0]
        plain: list[tuple[str, str]] = []
        if scores.size and scores.max() > 0:
            for k in np.where(scores >= KEEP_RATIO * scores.max())[0]:
                p = sp.protos[indices[k]]
                plain.append((p.table, p.column))
            tr.trace.append("SELECT  <- column evidence " + ", ".join(
                f"{sp.protos[indices[k]].column}={scores[k]:.2f}" for k in np.where(scores > 0)[0]))

        plain = [pc for pc in plain if pc not in [(g[0], g[1]) for g in groups]]
        if aggs and not groups and not dist_idx:
            # "how many products are in stock": without a 'per / by' cue a stray column word is not
            # a grouping request, so it must not be listed beside the aggregate (that would force
            # a meaningless GROUP BY on it).
            plain = []
        prefix = ""
        if dist_idx:                                        # "unique departments", "how many distinct cities"
            counter = next((g for g in aggs if g.label == "COUNT" and g.column is None), None)
            if counter is not None and plain:
                tbl, col = plain.pop(0)
                counter.expr = f"COUNT(DISTINCT {_col_expr(tbl, col, multi)})"
                tr.trace.append(f"SELECT  <- distinct cue => COUNT(DISTINCT {col})")
            elif not aggs:
                prefix = "DISTINCT "
                tr.trace.append("SELECT  <- distinct cue => SELECT DISTINCT")
        items = [_col_expr(g[0], g[1], multi) for g in groups]
        items += [_col_expr(tbl, col, multi) for (tbl, col) in plain]
        items += [g.expr for g in aggs]
        from_clause = self._build_from_clause(tables)
        sql = f"SELECT {prefix}{', '.join(items) if items else '*'} FROM {from_clause}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        if groups:
            sql += " GROUP BY " + ", ".join(_col_expr(g[0], g[1], multi) for g in groups)
        if having:
            sql += " HAVING " + " AND ".join(having)
        if order.keys:
            sql += " ORDER BY " + ", ".join(f"{_col_expr(t, c, multi)} {d}" for t, c, d in order.keys)
        if order.limit is not None:
            sql += f" LIMIT {order.limit}"
        tr.sql = sql + ";"

    # ----------------------------------------------------------------------- DELETE
    def _delete(self, a: Analysis, table: str, tr: Translation) -> None:
        where, _h, _c = self._conditions(a, [table], [], False, False, tr)
        sql = f"DELETE FROM {quote_identifier(table)}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        tr.sql = sql + ";"

    # ----------------------------------------------------------------------- INSERT
    def _insert(self, a: Analysis, table: str, tr: Translation) -> None:
        """
        Positional reading after the table word:
            <column> <value...> <column> <value...>      (marker mode)
            <value> <value> <value>                      (values for all columns, in order)
        A word is a column marker if it has column evidence for this table.  Everything else
        that is not a function word / structural word is a value.
        """
        sp, toks = self.space, a.toks
        col_m = sp.mask("COLUMN", table)
        skip_roles = np.zeros(len(sp.protos), dtype=bool)
        for k in ("BINDER", "PREP", "INTENT", "TABLEWORD", "FILLER"):
            skip_roles |= sp.mask(k)
        start = self._table_position(a, table) + 1

        markers: dict[str, list[Token]] = {}
        order: list[str] = []
        loose: list[list[Token]] = [[]]
        current: Optional[str] = None
        # words that match a stored cell value of one of this table's columns bind to THAT column
        # (only when the sentence uses explicit column markers; otherwise values are positional)
        has_markers = any(toks[i].kind == "WORD" and a.S[i, col_m].max() > 0 for i in range(start, len(toks)))
        grounded = ({j: vm for vm in a.values if vm.proto.table == table for j in vm.token_idx}
                    if has_markers else {})
        done: set[int] = set()
        for i in range(start, len(toks)):
            t = toks[i]
            if i in done:
                continue
            if i in grounded and grounded[i].proto.column not in markers:
                vm = grounded[i]
                markers[vm.proto.column] = [Token("STR", vm.proto.value.lower(), vm.proto.value)]
                order.append(vm.proto.column)
                done.update(vm.token_idx)
                tr.trace.append(f"INSERT  <- '{t.text}' matches stored value '{vm.proto.value}' "
                                f"=> column '{vm.proto.column}'")
                continue
            if t.kind == "COMMA":
                loose.append([])
                continue
            if t.kind == "SYM":
                continue
            if t.kind == "WORD":
                ev = a.S[i, col_m]
                if ev.max() > 0:                                   # a column marker
                    current = str(sp.label[col_m][int(ev.argmax())])
                    if current not in markers:
                        markers[current] = []
                        order.append(current)
                    continue
                if a.S[i, skip_roles].max() >= TAU_STRICT:
                    continue
            if current is not None:
                markers[current].append(t)
            loose[-1].append(t)

        def render(parts: list[Token]) -> str:
            if len(parts) == 1 and parts[0].kind == "NUM":
                return _fmt(parts[0].value)
            return _lit(" ".join(p.raw for p in parts))

        tname = quote_identifier(table)
        if markers:
            empty = [c for c in order if not markers[c]]
            if empty:
                raise TranslationError(f"No value found for column '{empty[0]}'.")
            cols = ", ".join(quote_identifier(c) for c in order)
            vals = ", ".join(render(markers[c]) for c in order)
            tr.trace.append("INSERT  <- column markers followed by their values: " + ", ".join(order))
            tr.sql = f"INSERT INTO {tname} ({cols}) VALUES ({vals});"
            return
        groups = [g for g in loose if g]
        if len(groups) == 1 and not any(t.kind == "COMMA" for t in toks):
            groups = [[t] for t in groups[0]]                      # no commas: one token = one value
        if not groups:
            raise TranslationError("I found no values to insert.")
        tr.trace.append(f"INSERT  <- {len(groups)} positional value(s) for all columns of '{table}'")
        tr.sql = f"INSERT INTO {tname} VALUES ({', '.join(render(g) for g in groups)});"

    # ----------------------------------------------------------------------- CREATE
    def _create(self, a: Analysis, tr: Translation) -> None:
        """
        New names cannot be matched against the schema, so they are read by position:
        the first identifier (after the word 'table' if present) is the table, the rest are
        columns; a word that matches an explicit-type cluster right after a name is its type;
        otherwise the type is inferred from how close the NAME is to the type-hint clusters.
        """
        sp, toks = self.space, a.toks
        block = np.zeros(len(sp.protos), dtype=bool)
        for k in ("INTENT", "BINDER", "TABLEWORD", "TYPE_EXPLICIT", "FILLER"):
            block |= sp.mask(k)
        tw = sp.mask("TABLEWORD")
        after = next((i for i, t in enumerate(toks) if t.kind == "WORD" and a.S[i, tw].max() >= TAU_STRICT), -1)

        idents: list[tuple[str, str]] = []                          # (name, type or "")
        for i in range(after + 1, len(toks)):
            t = toks[i]
            if t.kind != "WORD":
                continue
            if a.S[i, sp.mask("TYPE_EXPLICIT")].max() >= TAU_STRICT:
                if idents and not idents[-1][1]:
                    tm = sp.mask("TYPE_EXPLICIT")
                    idents[-1] = (idents[-1][0], str(sp.label[tm][int(a.S[i, tm].argmax())]))
                continue
            if a.S[i, block].max() >= TAU_STRICT:
                continue
            idents.append((t.text, ""))
        if len(idents) < 2:
            raise TranslationError("To create a table I need a name and at least one column, e.g. "
                                   "'create table pets with columns name, age, weight'.")
        table, cols = idents[0][0], idents[1:]
        defs = []
        hint_m = sp.mask("TYPE_HINT")
        for name, ctype in cols:
            if not ctype:
                i = next(k for k, t in enumerate(toks) if t.kind == "WORD" and t.text == name)
                ctype = _best(a.S[i, hint_m], sp.label[hint_m]) or "TEXT"
                tr.trace.append(f"TYPE    <- column '{name}' sits closest to the {ctype} cluster")
            defs.append(f"{quote_identifier(name)} {ctype}")
        tr.trace.append(f"CREATE  <- table '{table}' is the first free identifier; columns follow in order")
        tr.table = table
        tr.sql = f"CREATE TABLE {quote_identifier(table)} ({', '.join(defs)});"

    # ----------------------------------------------------------------------- UPDATE
    def _update(self, a: Analysis, table: str, tr: Translation) -> None:
        """
        Two shapes, both read by position:
            absolute  "<column> ... TO <value>"          set salary to 70000 where name is Bob
            relative  "increase/decrease <column> BY n"  increase salary by 5000 for staff in sales
        Everything that belongs to the assignment is removed from the evidence before the
        filter (WHERE) is built, so a new value is never mistaken for a condition.
        """
        sp, toks = self.space, a.toks
        col_all, col_num = sp.mask("COLUMN", table), sp.mask("COLUMN", table, True)
        labels = list(sp.label[col_all])
        stop = np.zeros(len(sp.protos), dtype=bool)
        for k in ("PREP", "COND", "FILLER", "ASSIGN", "INTENT"):
            stop |= sp.mask(k)
        filler_m, cond_m, dm = sp.mask("FILLER"), sp.mask("COND"), sp.mask("DELTA")
        cond_pos = next((i for i, t in enumerate(toks) if t.kind == "WORD" and a.S[i, cond_m].max() >= TAU_STRICT),
                        len(toks))
        excl: set[int] = set()
        sets: list[str] = []

        def best_token(col: str, among: list[int]) -> int:
            k = labels.index(col)
            return max(among, key=lambda j: a.S[j, col_all][k]) if among else -1

        cue = next((i for i, t in enumerate(toks) if t.kind == "WORD" and a.S[i, dm].max() >= TAU), None)
        if cue is not None:                                           # relative change
            sign = "+" if str(sp.label[dm][int(a.S[cue, dm].argmax())]) == "INC" else "-"
            amt = next((j for j in range(cue + 1, min(len(toks), cue + 5)) if toks[j].kind == "NUM"), None)
            if amt is None:
                raise TranslationError("By how much? Example: 'increase salary by 5000 for employees in sales'.")
            win = [j for j in range(cue + 1, amt) if toks[j].kind == "WORD"]
            col = (_best(a.S[win][:, col_num].sum(axis=0), sp.label[col_num]) if win else None) \
                or _best(a.S[:, col_num].sum(axis=0), sp.label[col_num])
            if col is None:
                raise TranslationError("Which numeric column should change? Options: "
                                       + ", ".join(sp.label[col_num]) + ".")
            q = quote_identifier(col)
            sets.append(f"{q} = {q} {sign} {toks[amt].value}")
            holder = best_token(col, [j for j, t in enumerate(toks) if t.kind == "WORD"])
            excl |= {cue, amt} | ({holder} if holder >= 0 else set())
            tr.trace.append(f"UPDATE  <- '{toks[cue].text}' is a relative change of {sign}{toks[amt].value} "
                            f"on column '{col}'")
        else:                                                         # absolute assignment(s)
            anchors = [i for i, t in enumerate(toks) if i < cond_pos and (
                (t.kind == "WORD" and a.S[i, sp.mask("ASSIGN")].max() >= TAU_STRICT)
                or (t.kind == "SYM" and t.text == "="))]
            if not anchors:
                raise TranslationError("What is the new value? Example: "
                                       "'change salary to 70000 where name is Bob'.")
            prev = -1
            for anc in anchors:
                if anc <= prev:
                    continue
                before = [j for j in range(anc - 1, prev, -1) if toks[j].kind == "WORD"][:4]
                col = _best(a.S[before][:, col_all].sum(axis=0), sp.label[col_all]) if before else None
                if col is None:
                    raise TranslationError(f"Which column should be changed? Columns of '{table}': "
                                           + ", ".join(labels) + ".")
                j = anc + 1
                while j < len(toks) and toks[j].kind == "WORD" and a.S[j, filler_m].max() >= TAU_STRICT:
                    j += 1                                          # skip "the"
                vals: list[int] = []
                while j < len(toks):
                    t = toks[j]
                    if t.kind in ("COMMA", "SYM") or (t.kind == "WORD" and a.S[j, stop].max() >= TAU_STRICT):
                        break
                    vals.append(j)
                    j += 1
                if not vals:
                    raise TranslationError(f"I found no new value for '{col}'.")
                one = len(vals) == 1 and toks[vals[0]].kind == "NUM"
                value = _fmt(toks[vals[0]].value) if one else _lit(" ".join(toks[v].raw for v in vals))
                sets.append(f"{quote_identifier(col)} = {value}")
                excl |= {anc, best_token(col, before)} | set(vals)
                tr.trace.append(f"UPDATE  <- column '{col}' (words before 'to') gets the value after 'to': {value}")
                prev = j

        # remove the assignment's words from the evidence, then read the filter from what is left
        toks2 = [Token("SKIP", "", "") if i in excl else t for i, t in enumerate(toks)]
        S2 = a.S.copy()
        S2[sorted(excl)] = 0.0
        vals2 = [vm for vm in a.values if not (set(vm.token_idx) & excl)]
        where, _h, _c = self._conditions(Analysis(toks2, S2, vals2, a.prep_follow), [table], [], False, False, tr)
        sql = f"UPDATE {quote_identifier(table)} SET {', '.join(sets)}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        tr.sql = sql + ";"

    # ------------------------------------------------------------------------ ALTER
    def _identifiers(self, a: Analysis, start: int, table: str) -> list[int]:
        """Token indices of NEW names: words that are neither structure words nor THE word naming the table."""
        sp = self.space
        block = np.zeros(len(sp.protos), dtype=bool)
        for k in ("INTENT", "BINDER", "TABLEWORD", "TYPE_EXPLICIT", "FILLER", "PREP", "COLWORD",
                  "ASSIGN", "RENAME", "COND"):
            block |= sp.mask(k)
        table_word = self._table_position(a, table)        # only this one token names the old table
        return [i for i in range(start, len(a.toks))
                if a.toks[i].kind == "WORD" and i != table_word and a.S[i, block].max() < TAU_STRICT]

    def _alter(self, a: Analysis, table: str, sub: str, tr: Translation) -> None:
        sp, toks = self.space, a.toks
        col_all = sp.mask("COLUMN", table)
        qt = quote_identifier(table)

        if sub == "ADD":
            ids = self._identifiers(a, 0, table)           # the name may come before or after the word "column"
            if not ids:
                raise TranslationError("What should the new column be called? "
                                       "Example: 'add column phone to employees'.")
            name = toks[ids[0]].text
            tm, hm = sp.mask("TYPE_EXPLICIT"), sp.mask("TYPE_HINT")
            ctype = next((str(sp.label[tm][int(a.S[j, tm].argmax())]) for j in range(ids[0] + 1, min(len(toks), ids[0] + 4))
                          if toks[j].kind == "WORD" and a.S[j, tm].max() >= TAU_STRICT), None)
            ctype = ctype or _best(a.S[ids[0], hm], sp.label[hm]) or "TEXT"
            tr.trace.append(f"ALTER   <- new column '{name}' ({ctype}) is the first free identifier")
            tr.sql = f"ALTER TABLE {qt} ADD COLUMN {quote_identifier(name)} {ctype};"
            return

        if sub == "RENAME":
            asg = sp.mask("ASSIGN")
            anc = next((i for i, t in enumerate(toks) if t.kind == "WORD" and a.S[i, asg].max() >= TAU_STRICT), None)
            if anc is None:
                raise TranslationError("Rename to what? Example: 'rename column city to town in employees'.")
            before = [j for j in range(anc) if toks[j].kind == "WORD"]
            old = _best(a.S[before][:, col_all].sum(axis=0), sp.label[col_all]) if before else None
            new_ids = self._identifiers(a, anc + 1, table)
            if not new_ids:
                raise TranslationError("What is the new name? Example: 'rename column city to town'.")
            new = toks[new_ids[0]].text
            is_column = self._cue(a, "COLWORD") is not None or (old is not None and self._cue(a, "TABLEWORD") is None)
            if is_column:
                if old is None:
                    raise TranslationError(f"Which column should be renamed? Columns of '{table}': "
                                           + ", ".join(sp.label[col_all]) + ".")
                tr.trace.append(f"ALTER   <- rename column '{old}' to '{new}' (new name follows 'to')")
                tr.sql = f"ALTER TABLE {qt} RENAME COLUMN {quote_identifier(old)} TO {quote_identifier(new)};"
            else:
                tr.trace.append(f"ALTER   <- rename table '{table}' to '{new}'")
                tr.sql = f"ALTER TABLE {qt} RENAME TO {quote_identifier(new)};"
            return

        # DROPCOL
        scores = a.S[:, col_all].sum(axis=0)
        col = _best(scores, sp.label[col_all])
        if col is None:
            ids = self._identifiers(a, 0, table)
            if ids:
                col = toks[ids[0]].text
        if col is None:
            raise TranslationError(f"Which column should be dropped? Columns of '{table}': "
                                   + ", ".join(sp.label[col_all]) + ".")
        tr.trace.append(f"ALTER   <- drop column '{col}' (needs confirmation before it runs)")
        tr.sql = f"ALTER TABLE {qt} DROP COLUMN {quote_identifier(col)};"

