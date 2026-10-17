"""Persistence.

SQLite today, Postgres later. The point of this module is that the API layer
never learns which one it is talking to, so the swap is contained here.

Schema note: memory categories and conversation categories are DIFFERENT
vocabularies in the OMI contract and must not be conflated. See
docs/ROUTE-SURFACE.md.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id          TEXT PRIMARY KEY,
    content     TEXT NOT NULL,
    category    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    deleted_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_cat ON memories(category);

CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    category    TEXT,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    created_at  TEXT NOT NULL,
    deleted_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_conv_started ON conversations(started_at);

CREATE TABLE IF NOT EXISTS segments (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    idx             INTEGER NOT NULL,
    text            TEXT NOT NULL,
    speaker         TEXT,
    speaker_id      INTEGER,
    is_user         INTEGER NOT NULL DEFAULT 0,
    start_ts        REAL,
    end_ts          REAL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_seg_conv ON segments(conversation_id, idx);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------------- memories ----------------

    def create_memory(self, content: str, category: str) -> dict[str, Any]:
        mid = str(uuid.uuid4())
        now = _now()
        self._conn.execute(
            "INSERT INTO memories (id, content, category, created_at, updated_at) VALUES (?,?,?,?,?)",
            (mid, content, category, now, now),
        )
        self._conn.commit()
        return {"id": mid, "content": content, "category": category, "created_at": now}

    def list_memories(self, categories: Iterable[str] | None, limit: int, offset: int) -> list[dict]:
        cats = list(categories or [])
        sql = "SELECT id, content, category, created_at, updated_at FROM memories WHERE deleted_at IS NULL"
        args: list[Any] = []
        if cats:
            sql += " AND category IN (%s)" % ",".join("?" * len(cats))
            args += cats
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def get_memory(self, memory_id: str) -> dict | None:
        r = self._conn.execute(
            "SELECT id, content, category, created_at, updated_at FROM memories WHERE id=? AND deleted_at IS NULL",
            (memory_id,),
        ).fetchone()
        return dict(r) if r else None

    def edit_memory(self, memory_id: str, content: str) -> dict | None:
        if not self.get_memory(memory_id):
            return None
        self._conn.execute(
            "UPDATE memories SET content=?, updated_at=? WHERE id=?", (content, _now(), memory_id)
        )
        self._conn.commit()
        return self.get_memory(memory_id)

    def delete_memory(self, memory_id: str) -> bool:
        cur = self._conn.execute(
            "UPDATE memories SET deleted_at=? WHERE id=? AND deleted_at IS NULL", (_now(), memory_id)
        )
        self._conn.commit()
        return cur.rowcount > 0

    # ---------------- conversations ----------------

    def create_conversation(
        self, title: str | None, category: str | None, segments: list[dict] | None = None
    ) -> dict:
        cid = str(uuid.uuid4())
        now = _now()
        self._conn.execute(
            "INSERT INTO conversations (id, title, category, started_at, created_at) VALUES (?,?,?,?,?)",
            (cid, title, category, now, now),
        )
        for i, seg in enumerate(segments or []):
            # `seg.get("idx", i)` is wrong: the key EXISTS with value None when the
            # caller omits idx, so .get returns None and the positional default never
            # applies. Resolve it explicitly.
            seg_idx = seg.get("idx")
            if seg_idx is None:
                seg_idx = i
            self._conn.execute(
                "INSERT INTO segments (id, conversation_id, idx, text, speaker, speaker_id, is_user, start_ts, end_ts)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()), cid, seg_idx, seg.get("text", ""),
                    seg.get("speaker"), seg.get("speaker_id"), int(bool(seg.get("is_user", False))),
                    seg.get("start"), seg.get("end"),
                ),
            )
        self._conn.commit()
        return self.get_conversation(cid)  # type: ignore[return-value]

    def list_conversations(
        self, start_date: str | None, end_date: str | None, categories: Iterable[str] | None,
        limit: int, offset: int,
    ) -> list[dict]:
        cats = list(categories or [])
        sql = "SELECT id, title, category, started_at, created_at FROM conversations WHERE deleted_at IS NULL"
        args: list[Any] = []
        if start_date:
            sql += " AND date(started_at) >= date(?)"
            args.append(start_date)
        if end_date:
            sql += " AND date(started_at) <= date(?)"
            args.append(end_date)
        if cats:
            sql += " AND category IN (%s)" % ",".join("?" * len(cats))
            args += cats
        sql += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def get_conversation(self, conversation_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT id, title, category, started_at, created_at FROM conversations"
            " WHERE id=? AND deleted_at IS NULL",
            (conversation_id,),
        ).fetchone()
        if not row:
            return None
        out = dict(row)
        segs = self._conn.execute(
            "SELECT idx, text, speaker, speaker_id, is_user, start_ts, end_ts FROM segments"
            " WHERE conversation_id=? ORDER BY idx",
            (conversation_id,),
        ).fetchall()
        out["segments"] = [
            {
                "idx": s["idx"], "text": s["text"], "speaker": s["speaker"],
                "speaker_id": s["speaker_id"], "is_user": bool(s["is_user"]),
                "start": s["start_ts"], "end": s["end_ts"],
            }
            for s in segs
        ]
        return out

    # ---------------- search (naive; Qdrant later) ----------------

    def search_memories(self, query: str, limit: int) -> list[dict]:
        like = f"%{query}%"
        rows = self._conn.execute(
            "SELECT id, content, category, created_at FROM memories"
            " WHERE deleted_at IS NULL AND content LIKE ? ORDER BY created_at DESC LIMIT ?",
            (like, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def search_conversations(self, query: str, limit: int) -> list[dict]:
        like = f"%{query}%"
        rows = self._conn.execute(
            "SELECT DISTINCT c.id, c.title, c.category, c.started_at FROM conversations c"
            " JOIN segments s ON s.conversation_id = c.id"
            " WHERE c.deleted_at IS NULL AND s.text LIKE ? ORDER BY c.started_at DESC LIMIT ?",
            (like, limit),
        ).fetchall()
        return [dict(r) for r in rows]
