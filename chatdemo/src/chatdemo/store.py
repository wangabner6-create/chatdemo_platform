"""Persistence for conversation history and pending approvals in the agent orchestration runtime.

What gets persisted is limited to plain, provider-neutral conversation messages
— tool calls, reasoning blocks, and hosted-tool artifacts live only for the
duration of a turn and are never written into shared history. Redis backs this
when it's configured; when it isn't, a lightweight SQLite store gives local
development durable persistence without pulling in a separate agent SDK.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, Protocol


class Session(Protocol):
    session_id: str

    async def get_items(self, limit: int | None = None) -> list[Any]: ...

    async def add_items(self, items: list[Any]) -> None: ...

    async def pop_item(self) -> Any | None: ...

    async def clear_session(self) -> None: ...


_KEEP_ROLES = frozenset({"user", "assistant", "system", "developer"})
_KEEP_TYPES = frozenset({"message"})


def _keep_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("role") in _KEEP_ROLES:
        return True
    return item.get("type") in _KEEP_TYPES


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif isinstance(part.get("refusal"), str):
                    parts.append(part["refusal"])
        return "".join(parts)
    return "" if content is None else str(content)


def _normalize_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    role = item.get("role")
    if role in _KEEP_ROLES and item.get("type") in (None, "message"):
        return {"role": role, "content": _text_from_content(item.get("content"))}
    return item


class ChatSafeSession:
    """Filters the underlying session down to plain, provider-neutral conversation messages only."""

    def __init__(self, inner: Session) -> None:
        self._inner = inner
        self.session_id = inner.session_id

    async def get_items(self, limit: int | None = None) -> list[Any]:
        items = [
            _normalize_item(item)
            for item in await self._inner.get_items(None)
            if _keep_item(item)
        ]
        return items[-limit:] if limit else items

    async def add_items(self, items: list[Any]) -> None:
        await self._inner.add_items(
            [_normalize_item(item) for item in items if _keep_item(item)]
        )

    async def pop_item(self) -> Any | None:
        return await self._inner.pop_item()

    async def clear_session(self) -> None:
        await self._inner.clear_session()


class SQLiteSession:
    """Session backend built on SQLite, used as the fallback when no Redis URL is configured."""

    def __init__(self, session_id: str, db_path: str) -> None:
        self.session_id = session_id
        self._path = Path(db_path)
        self._lock = asyncio.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS chatdemo_sessions "
                "(session_id TEXT PRIMARY KEY, items_json TEXT NOT NULL)"
            )

    def _read(self) -> list[Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT items_json FROM chatdemo_sessions WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()
        return json.loads(row[0]) if row else []

    def _write(self, items: list[Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO chatdemo_sessions(session_id, items_json) VALUES(?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET items_json = excluded.items_json",
                (self.session_id, json.dumps(items)),
            )

    async def get_items(self, limit: int | None = None) -> list[Any]:
        async with self._lock:
            items = await asyncio.to_thread(self._read)
        return items[-limit:] if limit else items

    async def add_items(self, items: list[Any]) -> None:
        async with self._lock:
            current = await asyncio.to_thread(self._read)
            current.extend(items)
            await asyncio.to_thread(self._write, current)

    async def pop_item(self) -> Any | None:
        async with self._lock:
            current = await asyncio.to_thread(self._read)
            if not current:
                return None
            item = current.pop()
            await asyncio.to_thread(self._write, current)
            return item

    async def clear_session(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write, [])


class RedisSession:
    """Session backend built on Redis, supporting an optional sliding-window TTL."""

    def __init__(
        self, session_id: str, url: str, ttl_seconds: int = 0, prefix: str = "chatdemo:sess:"
    ) -> None:
        import redis.asyncio as aioredis

        self.session_id = session_id
        self._redis = aioredis.from_url(url, decode_responses=True)
        self._key = f"{prefix}{session_id}"
        self._ttl = ttl_seconds or None

    async def get_items(self, limit: int | None = None) -> list[Any]:
        raw = await self._redis.get(self._key)
        if raw and self._ttl:
            await self._redis.expire(self._key, self._ttl)
        items = json.loads(raw) if raw else []
        return items[-limit:] if limit else items

    async def add_items(self, items: list[Any]) -> None:
        current = await self.get_items()
        current.extend(items)
        await self._redis.set(self._key, json.dumps(current), ex=self._ttl)

    async def pop_item(self) -> Any | None:
        current = await self.get_items()
        if not current:
            return None
        item = current.pop()
        await self._redis.set(self._key, json.dumps(current), ex=self._ttl)
        return item

    async def clear_session(self) -> None:
        await self._redis.delete(self._key)


class MemoryPending:
    """Keeps pending approvals in process memory — suitable only for single-process, local development."""

    def __init__(self) -> None:
        self._items: dict[str, str] = {}

    async def pop(self, session_id: str) -> str | None:
        return self._items.pop(session_id, None)

    async def put(self, session_id: str, state: str) -> None:
        self._items[session_id] = state


class RedisPending:
    """Stores pending approvals in Redis so they're visible across all running replicas."""

    def __init__(self, url: str, prefix: str = "chatdemo:pending:", ttl_seconds: int = 3600) -> None:
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(url, decode_responses=True)
        self._prefix = prefix
        self._ttl = ttl_seconds

    async def pop(self, session_id: str) -> str | None:
        key = f"{self._prefix}{session_id}"
        value = await self._redis.get(key)
        if value is not None:
            await self._redis.delete(key)
        return value

    async def put(self, session_id: str, state: str) -> None:
        await self._redis.set(f"{self._prefix}{session_id}", state, ex=self._ttl)
