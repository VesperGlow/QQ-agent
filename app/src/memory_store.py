from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, TypeVar

import numpy as np

from .config import Settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def clean_relation(value: str) -> str:
    cleaned = re.sub(r"[^\w\-一-鿿]", "_", value.strip().lower())
    return cleaned[:80] or "related"


def level_expiry(level: int, ttl_days: list[float], now: str | None = None) -> str | None:
    """按记忆等级计算过期时间。等级 10 永久（返回 None）；1..9 按 ttl_days 梯度。

    记忆被再次提及时应以当下时间重算，相当于续期：反复出现的记忆越活越久。
    """
    if level >= 10:
        return None
    index = min(max(level, 1), len(ttl_days)) - 1
    base = datetime.fromisoformat(now) if now else datetime.now(UTC)
    return (base + timedelta(days=ttl_days[index])).isoformat()


def _keyword_tokens(query: str, max_tokens: int = 8) -> list[str]:
    """从查询里取值得做字面匹配的词：连续 CJK 段或 3+ 字符的字母数字词。"""
    tokens = re.findall(r"[一-鿿]{2,}|[A-Za-z0-9_]{3,}", query)
    seen: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.append(token)
        if len(seen) >= max_tokens:
            break
    return seen


def _vec_to_blob(vector: list[float]) -> bytes:
    return np.asarray(vector, dtype=np.float16).tobytes()


def _blob_to_vec(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float16).astype(np.float32)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  message_count INTEGER NOT NULL DEFAULT 0,
  summary TEXT NOT NULL DEFAULT '',
  summary_upto_seq INTEGER NOT NULL DEFAULT 0,
  summary_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  seq INTEGER NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (conversation_id, seq)
);
CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  text TEXT NOT NULL,
  kind TEXT NOT NULL,
  level INTEGER NOT NULL,
  subject TEXT NOT NULL DEFAULT 'user',
  embedding BLOB NOT NULL,
  fingerprint TEXT NOT NULL,
  source TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  repetitions INTEGER NOT NULL DEFAULT 1,
  access_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  last_accessed_at TEXT,
  expires_at TEXT,
  forgotten_at TEXT,
  superseded_by TEXT,
  superseded_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id, active);
CREATE INDEX IF NOT EXISTS idx_memories_fingerprint ON memories(user_id, fingerprint);
CREATE INDEX IF NOT EXISTS idx_memories_expires ON memories(expires_at);
CREATE INDEX IF NOT EXISTS idx_memories_superseded_by ON memories(superseded_by);
CREATE TABLE IF NOT EXISTS entities (
  key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_entities (
  memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  entity_key TEXT NOT NULL REFERENCES entities(key),
  PRIMARY KEY (memory_id, entity_key)
);
CREATE TABLE IF NOT EXISTS memory_links (
  from_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  to_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (from_id, to_id)
);
CREATE TABLE IF NOT EXISTS moods (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  label TEXT NOT NULL,
  valence INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_moods_user ON moods(user_id, created_at);
"""

# 过期记忆先按 expires_at 从检索里消失，再宽限这么多天才真正物理删除，留出反悔窗口。
_PURGE_GRACE_DAYS = 7


class MemoryStore:
    """SQLite 存储：向量（float16 BLOB）暴力余弦 + 等级/新近度/关键词加权检索。

    单写连接 + 线程锁串行化写入，所有调用经 asyncio.to_thread 下沉，
    对上层保持与旧图数据库版一致的 async 接口。
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    async def _run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def call() -> T:
            assert self._conn is not None, "MemoryStore 尚未 connect()"
            with self._lock:
                return fn(self._conn)

        return await asyncio.to_thread(call)

    async def connect(self, attempts: int = 1) -> None:
        def open_db() -> sqlite3.Connection:
            path = Path(self.settings.db_path)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(path, check_same_thread=False)
            except (OSError, sqlite3.OperationalError) as exc:
                raise RuntimeError(
                    f"无法打开数据库 {path}：{exc}。若挂载了旧版本（Neo4j 时代）的数据卷，"
                    "其属主不是本容器的 appuser(uid 10001)，请删除旧卷重建，"
                    "或 podman unshare chown -R 10001:10001 <卷挂载点>。"
                ) from exc
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA)
            conn.commit()
            return conn

        self._conn = await asyncio.to_thread(open_db)
        await self._run(self._purge_expired)

    async def close(self) -> None:
        if self._conn is not None:
            conn = self._conn
            self._conn = None
            await asyncio.to_thread(conn.close)

    async def ping(self) -> bool:
        try:
            await self._run(lambda conn: conn.execute("SELECT 1").fetchone())
            return True
        except Exception:
            return False

    # ---------- 对话历史 ----------

    async def save_message(
        self, user_id: str, conversation_id: str, role: str, content: str
    ) -> str:
        message_id = str(uuid.uuid4())
        now = utc_now()

        def write(conn: sqlite3.Connection) -> str:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO users (id, created_at) VALUES (?, ?)",
                    (user_id, now),
                )
                row = conn.execute(
                    "SELECT user_id FROM conversations WHERE id = ?", (conversation_id,)
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO conversations (id, user_id, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?)",
                        (conversation_id, user_id, now, now),
                    )
                elif row["user_id"] != user_id:
                    raise ValueError("conversation_id 已属于其他用户")
                seq = conn.execute(
                    "UPDATE conversations SET message_count = message_count + 1,"
                    " updated_at = ? WHERE id = ? RETURNING message_count",
                    (now, conversation_id),
                ).fetchone()["message_count"]
                conn.execute(
                    "INSERT INTO messages (id, conversation_id, seq, role, content, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (message_id, conversation_id, seq, role, content, now),
                )
            return message_id

        return await self._run(write)

    async def get_history(
        self, user_id: str, conversation_id: str, limit: int
    ) -> list[dict[str, str]]:
        if limit <= 0:
            return []

        def read(conn: sqlite3.Connection) -> list[dict[str, str]]:
            rows = conn.execute(
                "SELECT m.role, m.content FROM messages m"
                " JOIN conversations c ON c.id = m.conversation_id"
                " WHERE c.id = ? AND c.user_id = ? AND m.role IN ('user', 'assistant')"
                " ORDER BY m.seq DESC LIMIT ?",
                (conversation_id, user_id, limit),
            ).fetchall()
            return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

        return await self._run(read)

    async def get_last_message_at(self, user_id: str, conversation_id: str) -> str | None:
        def read(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(
                "SELECT m.created_at FROM messages m"
                " JOIN conversations c ON c.id = m.conversation_id"
                " WHERE c.id = ? AND c.user_id = ? ORDER BY m.seq DESC LIMIT 1",
                (conversation_id, user_id),
            ).fetchone()
            return row["created_at"] if row else None

        return await self._run(read)

    async def get_conversation_summary(self, user_id: str, conversation_id: str) -> str:
        def read(conn: sqlite3.Connection) -> str:
            row = conn.execute(
                "SELECT summary FROM conversations WHERE id = ? AND user_id = ?",
                (conversation_id, user_id),
            ).fetchone()
            return row["summary"] if row else ""

        return await self._run(read)

    async def messages_to_summarize(
        self, user_id: str, conversation_id: str, window: int, limit: int = 200
    ) -> dict[str, Any] | None:
        """取已滑出短期窗口（seq <= total-window）且尚未摘要（seq > summary_upto_seq）的旧消息。"""

        def read(conn: sqlite3.Connection) -> dict[str, Any] | None:
            convo = conn.execute(
                "SELECT summary, summary_upto_seq, message_count FROM conversations"
                " WHERE id = ? AND user_id = ?",
                (conversation_id, user_id),
            ).fetchone()
            if convo is None:
                return None
            rows = conn.execute(
                "SELECT role, content, seq FROM messages"
                " WHERE conversation_id = ? AND seq > ? AND seq <= ?"
                " AND role IN ('user', 'assistant') ORDER BY seq ASC LIMIT ?",
                (
                    conversation_id,
                    convo["summary_upto_seq"],
                    convo["message_count"] - max(window, 0),
                    min(max(limit, 1), 1000),
                ),
            ).fetchall()
            if not rows:
                return None
            return {
                "summary": convo["summary"],
                "messages": [{"role": r["role"], "content": r["content"]} for r in rows],
                "max_seq": rows[-1]["seq"],
            }

        return await self._run(read)

    async def update_conversation_summary(
        self, user_id: str, conversation_id: str, summary: str, upto_seq: int
    ) -> None:
        def write(conn: sqlite3.Connection) -> None:
            with conn:
                conn.execute(
                    "UPDATE conversations SET summary = ?, summary_upto_seq = ?, summary_at = ?"
                    " WHERE id = ? AND user_id = ?",
                    (summary, upto_seq, utc_now(), conversation_id, user_id),
                )

        await self._run(write)

    # ---------- 长期记忆 ----------

    def _memory_view(
        self, conn: sqlite3.Connection, row: sqlite3.Row, score: float | None = None
    ) -> dict[str, Any]:
        entities = [
            {"name": r["name"], "type": r["type"]}
            for r in conn.execute(
                "SELECT e.name, e.type FROM memory_entities me"
                " JOIN entities e ON e.key = me.entity_key WHERE me.memory_id = ?",
                (row["id"],),
            )
        ]
        view: dict[str, Any] = {
            "id": row["id"],
            "text": row["text"],
            "kind": row["kind"],
            "level": row["level"],
            "subject": row["subject"],
            "created_at": row["created_at"],
            "entities": entities,
        }
        if score is not None:
            view["score"] = round(float(score), 6)
        return view

    async def search_memories(
        self,
        user_id: str,
        embedding: list[float],
        limit: int | None = None,
        min_score: float | None = None,
        temporal_ranking: bool = True,
        query_text: str = "",
    ) -> list[dict[str, Any]]:
        limit = limit or self.settings.memory_search_limit
        min_score = self.settings.memory_min_score if min_score is None else min_score
        now = utc_now()
        query = np.asarray(embedding, dtype=np.float32)
        settings = self.settings
        tokens = _keyword_tokens(query_text) if query_text else []

        def read(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT id, text, kind, level, subject, created_at, last_seen_at, embedding"
                " FROM memories WHERE user_id = ? AND active = 1"
                " AND (expires_at IS NULL OR expires_at > ?)",
                (user_id, now),
            ).fetchall()
            if not rows:
                return []
            matrix = np.stack([_blob_to_vec(r["embedding"]) for r in rows])
            # 向量在入库前已 L2 归一化，点积即余弦相似度。
            similarity = matrix @ query

            recency_weight = settings.memory_recency_weight if temporal_ranking else 0.0
            level_weight = settings.memory_importance_weight if temporal_ranking else 0.0
            keyword_weight = settings.memory_keyword_weight if temporal_ranking else 0.0
            now_dt = datetime.fromisoformat(now)

            scored: list[tuple[float, float, sqlite3.Row]] = []
            for row, sim in zip(rows, similarity, strict=True):
                sim = float(sim)
                if sim < min_score:
                    continue
                final = sim * settings.memory_similarity_weight
                if recency_weight:
                    age_days = max(
                        (now_dt - datetime.fromisoformat(row["last_seen_at"])).total_seconds()
                        / 86400.0,
                        0.0,
                    )
                    final += recency_weight / (
                        1.0 + age_days / settings.memory_recency_halflife_days
                    )
                if level_weight:
                    final += level_weight * (row["level"] / 10.0)
                if keyword_weight and tokens:
                    text = row["text"]
                    if any(token in text for token in tokens):
                        final += keyword_weight
                scored.append((final, sim, row))
            scored.sort(key=lambda item: item[0], reverse=True)
            top = scored[:limit]
            if top:
                with conn:
                    conn.executemany(
                        "UPDATE memories SET access_count = access_count + 1,"
                        " last_accessed_at = ? WHERE id = ?",
                        [(now, row["id"]) for _, _, row in top],
                    )
            return [self._memory_view(conn, row, score=sim) for _, sim, row in top]

        return await self._run(read)

    async def recent_memories(self, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        now = utc_now()

        def read(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT * FROM memories WHERE user_id = ? AND active = 1"
                " AND (expires_at IS NULL OR expires_at > ?)"
                " ORDER BY last_seen_at DESC LIMIT ?",
                (user_id, now, min(max(limit, 1), 100)),
            ).fetchall()
            return [self._memory_view(conn, row) for row in rows]

        return await self._run(read)

    def _purge_expired(self, conn: sqlite3.Connection) -> None:
        deadline = (
            datetime.now(UTC) - timedelta(days=_PURGE_GRACE_DAYS)
        ).isoformat()
        with conn:
            deleted = conn.execute(
                "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at < ?"
                " AND superseded_by IS NULL",
                (deadline,),
            ).rowcount
        if deleted:
            logger.info("已清理 %d 条过期记忆", deleted)

    def _touch_memory(
        self, conn: sqlite3.Connection, row: sqlite3.Row, level: int, now: str
    ) -> dict[str, Any]:
        """同一记忆再次出现：续期、升级（取更高等级）、计数。"""
        new_level = max(row["level"], level)
        with conn:
            conn.execute(
                "UPDATE memories SET last_seen_at = ?, repetitions = repetitions + 1,"
                " level = ?, expires_at = ? WHERE id = ?",
                (
                    now,
                    new_level,
                    level_expiry(new_level, self.settings.memory_level_ttls, now),
                    row["id"],
                ),
            )
        refreshed = conn.execute(
            "SELECT * FROM memories WHERE id = ?", (row["id"],)
        ).fetchone()
        view = self._memory_view(conn, refreshed)
        view["deduplicated"] = True
        return view

    async def create_memory(
        self,
        *,
        user_id: str,
        text: str,
        kind: str,
        level: int,
        entities: list[dict[str, str]],
        embedding: list[float],
        source: str,
        subject: str = "user",
    ) -> dict[str, Any]:
        subject = subject if subject in {"user", "assistant"} else "user"
        level = min(max(int(level), 1), 10)
        text = text.strip()
        fingerprint = hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()
        now = utc_now()
        query = np.asarray(embedding, dtype=np.float32)
        settings = self.settings

        def write(conn: sqlite3.Connection) -> dict[str, Any]:
            self._purge_expired(conn)
            # 去重按主体隔离：同样文本但主体不同（关于用户 vs 关于助手）不应合并。
            existing = conn.execute(
                "SELECT * FROM memories WHERE user_id = ? AND fingerprint = ?"
                " AND active = 1 AND subject = ? LIMIT 1",
                (user_id, fingerprint, subject),
            ).fetchone()
            if existing is not None:
                return self._touch_memory(conn, existing, level, now)

            # 近乎完全相同的规范化表述用极高阈值合并（默认 0.995），
            # 避免把“喜欢 X”和“不喜欢 X”误合并。
            candidates = conn.execute(
                "SELECT * FROM memories WHERE user_id = ? AND active = 1"
                " AND subject = ? AND kind = ?"
                " AND (expires_at IS NULL OR expires_at > ?)",
                (user_id, subject, kind, now),
            ).fetchall()
            for row in candidates:
                if float(_blob_to_vec(row["embedding"]) @ query) >= settings.memory_duplicate_threshold:
                    return self._touch_memory(conn, row, level, now)

            memory_id = str(uuid.uuid4())
            safe_entities = []
            for entity in entities[:30]:
                if not isinstance(entity, dict):
                    continue
                name = str(entity.get("name", "")).strip()[:200]
                entity_type = str(entity.get("type", "entity")).strip()[:80] or "entity"
                if name:
                    safe_entities.append(
                        {
                            "name": name,
                            "type": entity_type,
                            "key": f"{entity_type.casefold()}:{name.casefold()}",
                        }
                    )
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO users (id, created_at) VALUES (?, ?)",
                    (user_id, now),
                )
                conn.execute(
                    "INSERT INTO memories (id, user_id, text, kind, level, subject,"
                    " embedding, fingerprint, source, created_at, last_seen_at, expires_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        memory_id,
                        user_id,
                        text,
                        kind,
                        level,
                        subject,
                        _vec_to_blob(embedding),
                        fingerprint,
                        source,
                        now,
                        now,
                        level_expiry(level, settings.memory_level_ttls, now),
                    ),
                )
                for entity in safe_entities:
                    conn.execute(
                        "INSERT OR IGNORE INTO entities (key, name, type, created_at)"
                        " VALUES (?, ?, ?, ?)",
                        (entity["key"], entity["name"], entity["type"], now),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO memory_entities (memory_id, entity_key)"
                        " VALUES (?, ?)",
                        (memory_id, entity["key"]),
                    )
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            view = self._memory_view(conn, row)
            view["deduplicated"] = False
            return view

        return await self._run(write)

    async def forget_memory(self, user_id: str, memory_id: str) -> bool:
        def write(conn: sqlite3.Connection) -> bool:
            with conn:
                changed = conn.execute(
                    "UPDATE memories SET active = 0, forgotten_at = ?"
                    " WHERE id = ? AND user_id = ? AND active = 1",
                    (utc_now(), memory_id, user_id),
                ).rowcount
            return bool(changed)

        return await self._run(write)

    async def supersede_memory(
        self,
        *,
        user_id: str,
        old_memory_id: str,
        text: str,
        kind: str,
        level: int,
        entities: list[dict[str, str]],
        embedding: list[float],
        subject: str = "user",
    ) -> dict[str, Any]:
        """用新内容取代一条旧记忆：新建（或复用）新记忆并软停用旧记忆。

        旧记忆保留（active=0 + superseded_by/at），可经 memory_history 回溯时间线。
        """
        created = await self.create_memory(
            user_id=user_id,
            text=text,
            kind=kind,
            level=level,
            entities=entities,
            embedding=embedding,
            source="memory_update",
            subject=subject,
        )

        def link(conn: sqlite3.Connection) -> bool:
            if created["id"] == old_memory_id:
                return False
            with conn:
                changed = conn.execute(
                    "UPDATE memories SET active = 0, superseded_by = ?, superseded_at = ?"
                    " WHERE id = ? AND user_id = ?",
                    (created["id"], utc_now(), old_memory_id, user_id),
                ).rowcount
            return bool(changed)

        superseded = await self._run(link)
        created["superseded"] = superseded
        created["superseded_memory_id"] = old_memory_id if superseded else None
        return created

    async def memory_history(self, user_id: str, memory_id: str) -> list[dict[str, Any]]:
        """沿取代链返回一条记忆的完整演变时间线（含已停用的历史版本）。"""

        def read(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            anchor = conn.execute(
                "SELECT id FROM memories WHERE id = ? AND user_id = ?",
                (memory_id, user_id),
            ).fetchone()
            if anchor is None:
                return []
            rows = conn.execute(
                """
                WITH RECURSIVE newer(id) AS (
                  SELECT superseded_by FROM memories WHERE id = :mid AND superseded_by IS NOT NULL
                  UNION
                  SELECT m.superseded_by FROM memories m JOIN newer n ON m.id = n.id
                  WHERE m.superseded_by IS NOT NULL
                ), older(id) AS (
                  SELECT id FROM memories WHERE superseded_by = :mid
                  UNION
                  SELECT m.id FROM memories m JOIN older o ON m.superseded_by = o.id
                )
                SELECT * FROM memories
                WHERE user_id = :uid
                  AND (id = :mid OR id IN (SELECT id FROM newer) OR id IN (SELECT id FROM older))
                ORDER BY created_at
                """,
                {"mid": memory_id, "uid": user_id},
            ).fetchall()
            result = []
            for row in rows:
                view = self._memory_view(conn, row)
                view["active"] = bool(row["active"])
                view["superseded_at"] = row["superseded_at"]
                result.append(view)
            return result

        return await self._run(read)

    async def link_memories(
        self, user_id: str, from_id: str, to_id: str, relation: str
    ) -> bool:
        def write(conn: sqlite3.Connection) -> bool:
            owned = conn.execute(
                "SELECT count(*) AS n FROM memories WHERE user_id = ? AND id IN (?, ?)",
                (user_id, from_id, to_id),
            ).fetchone()["n"]
            if owned != 2 or from_id == to_id:
                return False
            with conn:
                conn.execute(
                    "INSERT INTO memory_links (from_id, to_id, kind, updated_at)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT (from_id, to_id) DO UPDATE SET kind = excluded.kind,"
                    " updated_at = excluded.updated_at",
                    (from_id, to_id, clean_relation(relation), utc_now()),
                )
            return True

        return await self._run(write)

    async def graph_snapshot(self, user_id: str, limit: int = 100) -> dict[str, Any]:
        def read(conn: sqlite3.Connection) -> dict[str, Any]:
            rows = conn.execute(
                "SELECT id, text, kind FROM memories WHERE user_id = ? AND active = 1"
                " ORDER BY created_at DESC LIMIT ?",
                (user_id, min(max(limit, 1), 500)),
            ).fetchall()
            memory_ids = [row["id"] for row in rows]
            nodes = [
                {"id": row["id"], "label": row["text"], "type": "memory", "kind": row["kind"]}
                for row in rows
            ]
            edges: list[dict[str, str]] = []
            if memory_ids:
                marks = ",".join("?" * len(memory_ids))
                entity_rows = conn.execute(
                    f"SELECT me.memory_id, e.key, e.name, e.type FROM memory_entities me"
                    f" JOIN entities e ON e.key = me.entity_key"
                    f" WHERE me.memory_id IN ({marks})",
                    memory_ids,
                ).fetchall()
                seen_entities: set[str] = set()
                for row in entity_rows:
                    if row["key"] not in seen_entities:
                        seen_entities.add(row["key"])
                        nodes.append(
                            {
                                "id": row["key"],
                                "label": row["name"],
                                "type": "entity",
                                "kind": row["type"],
                            }
                        )
                    edges.append(
                        {"source": row["memory_id"], "target": row["key"], "relation": "mentions"}
                    )
                link_rows = conn.execute(
                    f"SELECT from_id, to_id, kind FROM memory_links"
                    f" WHERE from_id IN ({marks})",
                    memory_ids,
                ).fetchall()
                edges += [
                    {"source": row["from_id"], "target": row["to_id"], "relation": row["kind"]}
                    for row in link_rows
                ]
            return {"nodes": nodes, "edges": edges}

        return await self._run(read)

    # ---------- 情绪时间线 ----------

    async def record_mood(
        self, user_id: str, label: str, valence: int, note: str = ""
    ) -> dict[str, Any]:
        mood_id = str(uuid.uuid4())
        now = utc_now()

        def write(conn: sqlite3.Connection) -> dict[str, Any]:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO users (id, created_at) VALUES (?, ?)",
                    (user_id, now),
                )
                conn.execute(
                    "INSERT INTO moods (id, user_id, label, valence, note, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (mood_id, user_id, label, valence, note, now),
                )
            return {
                "id": mood_id,
                "label": label,
                "valence": valence,
                "note": note,
                "created_at": now,
            }

        return await self._run(write)

    async def mood_trend(self, user_id: str, days: int) -> dict[str, Any]:
        """近 days 天的情绪聚合：条数、valence 均值、标签分布与最近一次。"""
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()

        def read(conn: sqlite3.Connection) -> dict[str, Any]:
            rows = conn.execute(
                "SELECT label, valence, created_at FROM moods"
                " WHERE user_id = ? AND created_at >= ? ORDER BY created_at DESC",
                (user_id, since),
            ).fetchall()
            if not rows:
                return {"count": 0, "days": days}
            distribution: dict[str, int] = {}
            for row in rows:
                distribution[row["label"]] = distribution.get(row["label"], 0) + 1
            return {
                "count": len(rows),
                "days": days,
                "avg_valence": sum(row["valence"] for row in rows) / len(rows),
                "latest_label": rows[0]["label"],
                "latest_at": rows[0]["created_at"],
                "distribution": distribution,
            }

        return await self._run(read)

    async def recent_moods(self, user_id: str, limit: int) -> list[dict[str, Any]]:
        def read(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT id, label, valence, note, created_at FROM moods"
                " WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, min(max(limit, 1), 500)),
            ).fetchall()
            return [dict(row) for row in rows]

        return await self._run(read)
