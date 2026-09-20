"""Persistence layer for feedback records, backed by Redis Stack's RediSearch module.

Every piece of feedback lives as a single Redis HASH under the key
`feedbackchatbot:<id>`, and a RediSearch index sits on top of that keyspace.
Fields you'd filter or sort on (vote, username, source, trace, route, model,
timestamp) are indexed as structured fields, while the free-text fields
(question/answer/comment) get full-text indexing.

This module is written to by two separate callers:
- the /feedback HTTP endpoint, hit by the thumb up/down controls and the
  sidebar feedback box;
- the `submit_feedback` action tool, triggered when a user asks to leave
  feedback mid-conversation.

If there's no Redis URL configured (e.g. running offline or locally), records
fall back to an in-memory list so nothing breaks in the absence of a broker.
Index creation is idempotent — safe to attempt on every write.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

FEEDBACK_INDEX = "feedbackchatbot"
FEEDBACK_PREFIX = "feedbackchatbot:"

# Complete set of hash fields this store can persist: the common feedback
# schema, extended with UI context plus the Phoenix trace identity used to join
# a rating back to the exact agent run that produced it.
_FIELDS = (
    "id",
    "input_query",
    "modified_query",
    "intent_catagory",
    "feedback",
    "query_filters",
    "response",
    "sources",
    "relevant_history",
    "username",
    "session_id",
    "source",
    "comment",
    "category",
    "trace_id",
    "route",
    "model",
    "timestamp",
    "response_timestamp",
)

# Used in place of Redis when CHATDEMO_REDIS_URL isn't set, so local/dev runs
# without a broker still function end to end.
_MEM: list[dict[str, Any]] = []


def _redis_url(url: str | None) -> str:
    return url if url is not None else os.environ.get("CHATDEMO_REDIS_URL", "")


def _sync_client(url: str):
    import redis

    return redis.Redis.from_url(url, decode_responses=True)


def _ensure_index(client) -> None:
    """Ensure the RediSearch index exists, creating it if necessary (safe to call
    repeatedly). This needs the Redis Stack module; on a plain Redis instance the
    resulting error is caught and ignored — hashes still get written and can be
    fetched by key, they just won't be searchable via FT."""
    from redis.commands.search.field import NumericField, TagField, TextField
    from redis.commands.search.index_definition import IndexDefinition, IndexType
    from redis.exceptions import RedisError

    try:
        client.ft(FEEDBACK_INDEX).info()
        return  # index is already there, nothing to do
    except RedisError as exc:
        logger.debug("feedback search index is unavailable; creating it: %s", exc)
    schema = (
        TextField("id"),
        TextField("input_query"),
        TextField("response"),
        TextField("comment"),
        TextField("sources"),
        TagField("feedback"),  # the vote itself: up | down | ""
        TagField("username"),
        TagField("source"),  # where it came from: thumb | sidebar | chat
        TagField("category"),
        TagField("trace_id"),  # exact Phoenix trace that produced the answer
        TagField("route"),
        TagField("model"),
        NumericField("timestamp"),
    )
    try:
        client.ft(FEEDBACK_INDEX).create_index(
            schema,
            definition=IndexDefinition(prefix=[FEEDBACK_PREFIX], index_type=IndexType.HASH),
        )
    except RedisError as exc:
        # No RediSearch is available, so degrade to storage-only hashes.
        logger.debug("feedback search index creation skipped: %s", exc)


def _build_record(data: dict[str, Any]) -> dict[str, str]:
    now = int(time.time())
    rec: dict[str, str] = {f: "" for f in _FIELDS}
    rec.update({k: ("" if v is None else str(v)) for k, v in data.items() if k in _FIELDS})
    rec["id"] = rec["id"] or uuid.uuid4().hex
    rec["timestamp"] = rec["timestamp"] or str(now)
    return rec


def record_feedback(data: dict[str, Any], url: str | None = None) -> dict[str, str]:
    """Write a single feedback record to the store and hand it back with its
    generated id/timestamp populated."""
    rec = _build_record(data)
    url = _redis_url(url)
    if not url:
        _MEM.append(rec)
        return rec
    client = _sync_client(url)
    _ensure_index(client)
    client.hset(FEEDBACK_PREFIX + rec["id"], mapping=rec)
    return rec


# FT.SEARCH's paging (offset + num) is capped by RediSearch's MAXSEARCHRESULTS
# setting, 10000 by default — so it can only ever return a bounded slice of
# results. Requests for "everything" therefore fall back to a SCAN-based path.
_FT_LIMIT_MAX = 10_000


def recent_feedback(limit: int | None = 50, url: str | None = None) -> list[dict[str, str]]:
    """Fetch the most recent feedback entries, newest first — used by both the
    test suite and the admin view.

    Passing `limit=None` fetches everything. When `limit` fits within the
    FT.SEARCH ceiling (<= 10000), the query is delegated to FT.SEARCH, which
    sorts on the server side. Beyond that ceiling — or when `limit` is None —
    we instead SCAN + HGETALL every hash and sort client-side; that path has no
    upper bound and also works against a plain Redis instance that lacks
    RediSearch."""
    url = _redis_url(url)
    if not url:
        items = list(reversed(_MEM))
        return items if limit is None else items[:limit]
    client = _sync_client(url)

    if limit is None or limit > _FT_LIMIT_MAX:
        # Walk every feedback hash via SCAN, pull them all back in one pipelined
        # round trip, then sort newest-first on our side.
        keys = list(client.scan_iter(match=FEEDBACK_PREFIX + "*", count=1000))
        pipe = client.pipeline()
        for k in keys:
            pipe.hgetall(k)
        recs = [{f: (row or {}).get(f, "") for f in _FIELDS} for row in pipe.execute() if row]
        recs.sort(key=lambda r: int(r.get("timestamp") or 0), reverse=True)
        return recs if limit is None else recs[:limit]

    from redis.commands.search.query import Query

    _ensure_index(client)
    q = Query("*").sort_by("timestamp", asc=False).paging(0, limit)
    res = client.ft(FEEDBACK_INDEX).search(q)
    return [{k: getattr(d, k, "") for k in _FIELDS} for d in res.docs]
