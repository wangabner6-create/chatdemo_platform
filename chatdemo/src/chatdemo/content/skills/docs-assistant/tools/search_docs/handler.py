"""Query-only lookup against this project's own documentation set, served from a
local corpus rather than any remote retrieval service."""

from __future__ import annotations

from pathlib import Path

from chatdemo.rag import make_search

run = make_search(Path(__file__).resolve().parents[2], tool_name="search_docs")
