#!/usr/bin/env python
"""Exploratory script for hitting the Responses API with the web-search tool
enabled, purely to observe the shape of returned results and citations.

Point this at your own endpoint and share back the "raw response" section
so ResponseRunner's source-extraction logic can be adapted to match reality.

Usage:
    export INFERENCE_API_KEY=...            # or CHATDEMO_API_KEY
    # optional overrides:
    export CHATDEMO_BASE_URL=https://api.your-provider.com/v1
    export CHATDEMO_MODEL="your-model-id"
    export CHATDEMO_WEBSEARCH_TOOL=web_search   # or web_search_preview
    uv run python scripts/test_websearch.py "What's the latest news on renewable energy? Cite sources."
"""

from __future__ import annotations

import json
import os
import sys

from openai import OpenAI

BASE_URL = os.environ.get("CHATDEMO_BASE_URL", "https://api.your-provider.com/v1")
API_KEY = os.environ.get("CHATDEMO_API_KEY") or os.environ.get("INFERENCE_API_KEY")
MODEL = os.environ.get("CHATDEMO_MODEL", "your-model-id")
TOOL_TYPE = os.environ.get("CHATDEMO_WEBSEARCH_TOOL", "web_search")

QUERY = (
    sys.argv[1]
    if len(sys.argv) > 1
    else "What's the latest news on renewable energy? Cite your sources."
)


def main() -> None:
    if not API_KEY:
        sys.exit("Set INFERENCE_API_KEY (or CHATDEMO_API_KEY) in the environment.")

    client = OpenAI(base_url=BASE_URL, api_key=API_KEY)
    print(f"base_url={BASE_URL}\nmodel={MODEL}\ntool={{'type': '{TOOL_TYPE}'}}\nquery={QUERY!r}\n")

    try:
        resp = client.responses.create(
            model=MODEL,
            input=[{"role": "user", "content": QUERY}],
            tools=[{"type": TOOL_TYPE}],
            max_output_tokens=1024,
        )
    except Exception as exc:  # noqa: BLE001 - intentionally broad so the underlying API error is visible
        print("!! request failed:", type(exc).__name__, exc)
        print("\nIf the tool type is rejected, retry with:")
        print("   CHATDEMO_WEBSEARCH_TOOL=web_search_preview  (or the type your endpoint supports)")
        sys.exit(1)

    print("=== output_text ===")
    print(getattr(resp, "output_text", "") or "(empty)")

    data = resp.model_dump()

    print("\n=== output item types (where results/citations live) ===")
    for i, item in enumerate(data.get("output", []) or []):
        print(f"[{i}] type={item.get('type')} keys={list(item.keys())}")

    print("\n=== extracted url citations ===")
    sources = _extract_sources(data)
    if sources:
        for s in sources:
            print(f" - {s.get('title')}  {s.get('url')}")
    else:
        print("(none found via annotations — inspect the raw dump below to locate results)")

    print("\n=== raw response (truncated to 8000 chars) ===")
    print(json.dumps(data, indent=2, default=str)[:8000])


def _extract_sources(data: dict) -> list[dict]:
    sources: list[dict] = []
    for item in data.get("output", []) or []:
        # look for citations nested inside the message's text content blocks
        for block in item.get("content", []) or []:
            for ann in block.get("annotations", []) or []:
                if ann.get("type") in ("url_citation", "citation") and ann.get("url"):
                    sources.append({"title": ann.get("title"), "url": ann.get("url")})
        # alternatively, certain providers put results straight on a web_search_call item
        for key in ("results", "search_results", "sources"):
            for r in item.get(key, []) or []:
                if isinstance(r, dict) and r.get("url"):
                    sources.append({"title": r.get("title"), "url": r.get("url")})
    return sources


if __name__ == "__main__":
    main()
