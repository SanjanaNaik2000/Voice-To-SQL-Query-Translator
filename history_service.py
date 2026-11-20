"""
history_service.py  --  query history and favorites, saved to disk.

History and favorites are kept per connection name, newest first, and are written to
`history.json` next to the application, so they survive a restart / reopening the project.
Individual entries can be deleted, and a whole connection's history or favorites can be cleared.

    HistoryService()            -> in memory only (used by the tests, nothing is written)
    HistoryService(path)        -> loads `path` on start and saves it after every change

Writes are atomic (temp file + replace), so a crash can never leave a half-written file, and a
damaged file is ignored instead of stopping the app.  A lock keeps it safe under Flask's
threaded dev server.  Only the natural-language prompt, its statement type and a time are
stored; no result rows.
"""
from __future__ import annotations

import itertools
import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime

MAX_ITEMS = 1000          # oldest entries are dropped beyond this so the file cannot grow forever


@dataclass
class HistoryItem:
    id: int
    query: str            # the natural-language prompt (what is replayed on click)
    name: str             # label shown on the sidebar card
    connection_name: str
    created: str          # ISO date-time the entry was saved
    kind: str = ""        # SELECT / INSERT / DELETE / CREATE badge

    @property
    def timestamp(self) -> str:
        """Shown on the card: just the time for today, the date as well for older entries."""
        try:
            when = datetime.fromisoformat(self.created)
        except ValueError:
            return ""
        return when.strftime("%H:%M:%S") if when.date() == datetime.now().date() else when.strftime("%d %b %H:%M")


class HistoryService:
    def __init__(self, path: str | None = None) -> None:
        self._path = path
        self._queries: list[HistoryItem] = []
        self._favorites: list[HistoryItem] = []
        self._lock = threading.Lock()
        self._load()
        used = [i.id for i in self._queries + self._favorites]
        self._ids = itertools.count(max(used, default=0) + 1)

    # ------------------------------------------------------------------ disk
    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._queries = [HistoryItem(**d) for d in data.get("queries", [])]
            self._favorites = [HistoryItem(**d) for d in data.get("favorites", [])]
        except Exception:                                   # damaged / unreadable file: start empty
            self._queries, self._favorites = [], []

    def _save(self) -> None:
        """Caller holds the lock."""
        if not self._path:
            return
        data = {"queries": [asdict(q) for q in self._queries[-MAX_ITEMS:]],
                "favorites": [asdict(f) for f in self._favorites]}
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self._path)
        except OSError:
            pass                                            # read-only folder etc.: keep working in memory

    # --------------------------------------------------------------- changes
    def _new(self, prompt: str, connection: str, kind: str) -> HistoryItem:
        return HistoryItem(next(self._ids), prompt, prompt, connection,
                           datetime.now().isoformat(timespec="seconds"), kind)

    def save_query(self, prompt: str, connection: str, kind: str = "") -> None:
        with self._lock:
            self._queries.append(self._new(prompt, connection, kind))
            self._queries = self._queries[-MAX_ITEMS:]
            self._save()

    def save_favorite(self, prompt: str, connection: str, kind: str = "") -> None:
        with self._lock:
            if not any(f.query == prompt and f.connection_name == connection for f in self._favorites):
                self._favorites.append(self._new(prompt, connection, kind))
                self._save()

    def delete_query(self, item_id: int, connection: str) -> bool:
        """Delete ONE history entry (only of the given connection). Returns whether it existed."""
        with self._lock:
            keep = [q for q in self._queries if not (q.id == item_id and q.connection_name == connection)]
            changed = len(keep) != len(self._queries)
            self._queries = keep
            if changed:
                self._save()
            return changed

    def delete_favorite(self, item_id: int, connection: str) -> bool:
        with self._lock:
            keep = [f for f in self._favorites if not (f.id == item_id and f.connection_name == connection)]
            changed = len(keep) != len(self._favorites)
            self._favorites = keep
            if changed:
                self._save()
            return changed

    def clear_queries(self, connection: str) -> None:
        with self._lock:
            self._queries = [q for q in self._queries if q.connection_name != connection]
            self._save()

    def clear_favorites(self, connection: str) -> None:
        with self._lock:
            self._favorites = [f for f in self._favorites if f.connection_name != connection]
            self._save()

    # ----------------------------------------------------------------- reads
    def get_queries(self, connection: str) -> list[HistoryItem]:
        """Newest first, as a log should be read."""
        with self._lock:
            return [q for q in reversed(self._queries) if q.connection_name == connection]

    def get_favorites(self, connection: str) -> list[HistoryItem]:
        with self._lock:
            return [f for f in self._favorites if f.connection_name == connection]
