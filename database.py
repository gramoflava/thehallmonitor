"""
database.py — SQLite persistence layer for thehallmonitor.

Schema:
  tokens       — one row per (token, token_type) unique pair
  metadata     — key/value store for updater bookkeeping
  known_chats  — groups the bot has been added to
  chat_config  — per-group configuration (mode, warning text, reaction emoji)
  violations   — enforcement event log for /stats
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

DB_PATH = "data/forbidden.db"

_DDL = """
CREATE TABLE IF NOT EXISTS tokens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_text    TEXT NOT NULL,
    token       TEXT NOT NULL,
    token_type  TEXT NOT NULL
                CHECK(token_type IN ('url', 'domain', 'handle', 'text')),
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(token, token_type)
);

CREATE INDEX IF NOT EXISTS idx_tokens_lookup
    ON tokens(token_type, token);

CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS known_chats (
    chat_id   INTEGER PRIMARY KEY,
    title     TEXT,
    username  TEXT,
    added_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_config (
    chat_id         INTEGER PRIMARY KEY REFERENCES known_chats(chat_id),
    mode            INTEGER NOT NULL DEFAULT 2,
    warning_text    TEXT,
    reaction_emoji  TEXT NOT NULL DEFAULT '😡',
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS violations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
    token_type  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_violations_chat_time
    ON violations(chat_id, occurred_at);
"""


class Database:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self._conn: sqlite3.Connection | None = None

    # ── Context manager ───────────────────────────────────────────────────────

    def connect(self) -> "Database":
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        return self

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Database":
        return self.connect()

    def __exit__(self, *_) -> None:
        self.close()

    # ── Schema ────────────────────────────────────────────────────────────────

    def create_schema(self) -> None:
        assert self._conn, "Not connected"
        self._conn.executescript(_DDL)
        self._conn.commit()

    # ── Token operations ──────────────────────────────────────────────────────

    def replace_all_tokens(self, rows: list[tuple[str, str, str]]) -> None:
        """
        Atomically replace the entire tokens table.
        rows: list of (raw_text, token, token_type)
        """
        assert self._conn, "Not connected"
        with self._conn:
            self._conn.execute("DELETE FROM tokens")
            self._conn.executemany(
                "INSERT OR IGNORE INTO tokens (raw_text, token, token_type) "
                "VALUES (?, ?, ?)",
                rows,
            )
        logger.info("Token table replaced: %d rows inserted", len(rows))

    def get_all_tokens_by_type(self) -> dict[str, set[str]]:
        """
        Load all tokens into memory grouped by type.
        Called once at bot startup (and after each daily update).
        """
        assert self._conn, "Not connected"
        result: dict[str, set[str]] = {
            "url": set(),
            "domain": set(),
            "handle": set(),
            "text": set(),
        }
        for row in self._conn.execute("SELECT token, token_type FROM tokens"):
            result[row["token_type"]].add(row["token"])
        return result

    def count_tokens(self) -> dict[str, int]:
        assert self._conn, "Not connected"
        counts: dict[str, int] = {}
        for row in self._conn.execute(
            "SELECT token_type, COUNT(*) AS cnt FROM tokens GROUP BY token_type"
        ):
            counts[row["token_type"]] = row["cnt"]
        return counts

    # ── Metadata ──────────────────────────────────────────────────────────────

    def set_metadata(self, key: str, value: str) -> None:
        assert self._conn, "Not connected"
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                (key, value),
            )

    def get_metadata(self, key: str) -> str | None:
        assert self._conn, "Not connected"
        row = self._conn.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    # ── Known chats ───────────────────────────────────────────────────────────

    def upsert_known_chat(
        self, chat_id: int, title: str | None, username: str | None
    ) -> None:
        """Record or update a group the bot is active in."""
        assert self._conn, "Not connected"
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO known_chats (chat_id, title, username)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title    = excluded.title,
                    username = excluded.username
                """,
                (chat_id, title, username),
            )

    def get_known_chats(self) -> list[dict]:
        """Return all known groups as a list of dicts."""
        assert self._conn, "Not connected"
        return [
            dict(row)
            for row in self._conn.execute(
                "SELECT chat_id, title, username FROM known_chats"
            )
        ]

    # ── Per-group config ──────────────────────────────────────────────────────

    def get_chat_config(self, chat_id: int) -> dict:
        """
        Return config for a group as a dict.
        If no row exists, returns defaults.
        """
        assert self._conn, "Not connected"
        row = self._conn.execute(
            "SELECT * FROM chat_config WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row:
            return dict(row)
        # Return defaults so callers don't have to special-case missing rows
        return {
            "chat_id": chat_id,
            "mode": 2,
            "warning_text": None,
            "reaction_emoji": "😡",
        }

    def set_chat_config(self, chat_id: int, **kwargs) -> None:
        """
        Upsert one or more config fields for a group.
        Ensures the group exists in known_chats first (no-op if already there).
        Allowed kwargs: mode, warning_text, reaction_emoji
        """
        assert self._conn, "Not connected"
        allowed = {"mode", "warning_text", "reaction_emoji"}
        kwargs = {k: v for k, v in kwargs.items() if k in allowed}
        if not kwargs:
            return

        with self._conn:
            # Ensure parent row exists
            self._conn.execute(
                "INSERT OR IGNORE INTO known_chats (chat_id) VALUES (?)",
                (chat_id,),
            )
            # Build upsert
            cols = ", ".join(kwargs.keys())
            placeholders = ", ".join("?" * len(kwargs))
            update_clause = ", ".join(
                f"{k} = excluded.{k}" for k in kwargs
            )
            self._conn.execute(
                f"""
                INSERT INTO chat_config (chat_id, {cols}, updated_at)
                VALUES (?, {placeholders}, datetime('now'))
                ON CONFLICT(chat_id) DO UPDATE SET
                    {update_clause},
                    updated_at = datetime('now')
                """,
                (chat_id, *kwargs.values()),
            )

    # ── Violations ────────────────────────────────────────────────────────────

    def record_violation(self, chat_id: int, token_types: list[str]) -> None:
        """
        Record one enforcement event (one per message, not per token).
        token_types: the distinct token types that triggered (e.g. ['url', 'handle'])
        """
        assert self._conn, "Not connected"
        # Record the "most significant" type in this priority order
        priority = ["handle", "url", "domain", "text"]
        token_type = next(
            (t for t in priority if t in token_types),
            token_types[0] if token_types else "text",
        )
        with self._conn:
            self._conn.execute(
                "INSERT INTO violations (chat_id, token_type) VALUES (?, ?)",
                (chat_id, token_type),
            )

    def get_violation_stats(self, chat_id: int, days: int = 30) -> dict:
        """
        Return violation stats for a group over the last N days.
        Returns: {total, last_occurred_at, by_type: {type: count}}
        """
        assert self._conn, "Not connected"
        rows = self._conn.execute(
            """
            SELECT token_type, COUNT(*) AS cnt,
                   MAX(occurred_at) AS last_at
            FROM violations
            WHERE chat_id = ?
              AND occurred_at >= datetime('now', ? || ' days')
            GROUP BY token_type
            """,
            (chat_id, f"-{days}"),
        ).fetchall()

        total = sum(r["cnt"] for r in rows)
        last_at = max((r["last_at"] for r in rows), default=None)
        by_type = {r["token_type"]: r["cnt"] for r in rows}
        return {"total": total, "last_occurred_at": last_at, "by_type": by_type}
