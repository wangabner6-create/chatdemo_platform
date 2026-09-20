"""Common per-turn logic for deduplicating and formatting citations used by search-tool handlers."""

from __future__ import annotations

from typing import Any


def dedup_and_format(sources: list[dict[str, Any]], user: Any, query: str) -> str:
    sink = getattr(user, "sources", None)
    seen: set[str] = {s.get("url") or "" for s in sink} if sink is not None else set()
    deduped = []
    for s in sources:
        url = s.get("url") or ""
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(s)

    citation_start = len(sink) + 1 if sink is not None else 1
    if sink is not None:
        sink.extend(deduped)

    if not deduped:
        if sources:
            return (
                f"All results for {query!r} were already listed by an earlier "
                "search this turn; reuse their existing [n] identifiers."
            )
        return f"No results for {query!r}. Tell the user you don't have that."

    lines = [
        f"[{n}] {s['title']}\n{s['url']}\n{s.get('snippet') or ''}"
        for n, s in enumerate(deduped, citation_start)
    ]
    return (
        "SEARCH RESULTS (ground your answer on these; preserve each exact [n] "
        "citation identifier):\n\n" + "\n\n".join(lines)
    )
