"""NodeSync server 权威状态的 SQLite 存储。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from nodesync.shared.jsonio import dumps_json, loads_json
from nodesync.shared.models import ChatMessageEvent, ClientRegistration, Directive, SceneState
from nodesync.shared.time_utils import now_ts


class NodeSyncStorage:
    """基于 SQLite 的服务端权威状态存储。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """打开 SQLite 连接，并确保退出时提交、回滚和关闭。"""

        conn = sqlite3.connect(str(self.path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS clients (
                    bot_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    last_seen_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stream_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_stream_created
                    ON messages(stream_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS scenes (
                    session_id TEXT PRIMARY KEY,
                    stream_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_scenes_stream_status
                    ON scenes(stream_id, status);
                CREATE TABLE IF NOT EXISTS directives (
                    directive_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    target_bot_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    def upsert_client(self, client: ClientRegistration) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO clients(bot_id, payload_json, last_seen_at)
                VALUES (?, ?, ?)
                ON CONFLICT(bot_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    last_seen_at = excluded.last_seen_at
                """,
                (client.bot_id, dumps_json(client.to_dict()), now_ts()),
            )

    def list_clients(self) -> list[ClientRegistration]:
        with self.connect() as conn:
            rows = conn.execute("SELECT payload_json FROM clients ORDER BY bot_id").fetchall()
        return [ClientRegistration.from_dict(loads_json(str(row["payload_json"]))) for row in rows]

    def add_message(self, event: ChatMessageEvent) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO messages(stream_id, message_id, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (event.stream_id, event.message_id, dumps_json(event.to_dict()), event.timestamp),
            )

    def count_messages(self, stream_id: str) -> int:
        """统计指定 stream 已记录的消息数量。"""

        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM messages WHERE stream_id = ?",
                (stream_id,),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def get_recent_messages(self, stream_id: str, limit: int = 40) -> list[ChatMessageEvent]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json FROM messages
                WHERE stream_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (stream_id, limit),
            ).fetchall()
        messages = [ChatMessageEvent.from_dict(loads_json(str(row["payload_json"]))) for row in rows]
        messages.reverse()
        return messages

    def upsert_scene(self, scene: SceneState, status: str = "active") -> None:
        scene.updated_at = now_ts()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO scenes(session_id, stream_id, payload_json, status, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    stream_id = excluded.stream_id,
                    payload_json = excluded.payload_json,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (scene.session_id, scene.stream_id, dumps_json(scene.to_dict()), status, scene.updated_at),
            )

    def get_active_scene_for_stream(self, stream_id: str) -> SceneState | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT payload_json FROM scenes
                WHERE stream_id = ? AND status = 'active'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (stream_id,),
            ).fetchone()
        if row is None:
            return None
        return SceneState.from_dict(loads_json(str(row["payload_json"])))

    def get_scene(self, session_id: str) -> SceneState | None:
        with self.connect() as conn:
            row = conn.execute("SELECT payload_json FROM scenes WHERE session_id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        return SceneState.from_dict(loads_json(str(row["payload_json"])))

    def close_scene(self, session_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE scenes SET status = 'closed', updated_at = ? WHERE session_id = ?",
                (now_ts(), session_id),
            )

    def add_directive(self, directive: Directive, status: str = "queued") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO directives(directive_id, session_id, target_bot_id, payload_json, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(directive_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    directive.directive_id,
                    directive.session_id,
                    directive.target_bot_id,
                    dumps_json(directive.to_dict()),
                    status,
                    now_ts(),
                ),
            )

    def get_directive(self, directive_id: str) -> Directive | None:
        """按 ID 读取指令。"""

        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM directives WHERE directive_id = ?",
                (directive_id,),
            ).fetchone()
        if row is None:
            return None
        return Directive.from_dict(loads_json(str(row["payload_json"])))

    def find_open_directive_by_key(self, idempotency_key: str, now: float | None = None) -> Directive | None:
        """按幂等键查找 TTL 内仍应去重的指令。"""

        if not idempotency_key:
            return None
        current = now if now is not None else now_ts()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json, status FROM directives
                ORDER BY updated_at DESC
                """
            ).fetchall()
        for row in rows:
            if str(row["status"]) in {"expired", "exhausted", "cancelled"}:
                continue
            directive = Directive.from_dict(loads_json(str(row["payload_json"])))
            if directive.idempotency_key != idempotency_key:
                continue
            if directive.expires_at and directive.expires_at < current:
                continue
            return directive
        return None

    def list_retryable_directives(
        self,
        retry_statuses: Iterable[str],
        retry_after_seconds: int,
        max_attempts: int,
        now: float | None = None,
    ) -> list[Directive]:
        """返回达到重试条件的未终态指令。"""

        current = now if now is not None else now_ts()
        status_set = set(retry_statuses)
        cutoff = current - retry_after_seconds
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json, status, updated_at FROM directives
                WHERE updated_at <= ?
                ORDER BY updated_at ASC
                """,
                (cutoff,),
            ).fetchall()
        directives = []
        for row in rows:
            if str(row["status"]) not in status_set:
                continue
            directive = Directive.from_dict(loads_json(str(row["payload_json"])))
            if directive.expires_at and directive.expires_at < current:
                self.update_directive_status(directive.directive_id, "expired")
                continue
            if directive.attempts >= max_attempts:
                self.update_directive_status(directive.directive_id, "exhausted")
                continue
            directives.append(directive)
        return directives

    def increment_directive_attempt(self, directive_id: str, last_error: str = "") -> Directive | None:
        """增加指令发送尝试次数，并返回更新后的指令。"""

        directive = self.get_directive(directive_id)
        if directive is None:
            return None
        directive.attempts += 1
        directive.last_error = last_error
        self.add_directive(directive)
        return directive

    def update_directive_status(self, directive_id: str, status: str, extra: dict[str, Any] | None = None) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM directives WHERE directive_id = ?",
                (directive_id,),
            ).fetchone()
            payload: dict[str, Any] = {}
            if row is not None:
                payload = loads_json(str(row["payload_json"]))
            if extra:
                payload["result"] = dict(extra)
            conn.execute(
                """
                UPDATE directives
                SET status = ?, payload_json = ?, updated_at = ?
                WHERE directive_id = ?
                """,
                (status, dumps_json(payload), now_ts(), directive_id),
            )
