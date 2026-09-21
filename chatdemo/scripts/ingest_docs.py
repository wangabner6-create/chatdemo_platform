#!/usr/bin/env python3
"""Generates the local RAG index that powers the `docs-assistant` skill,
sourcing content exclusively from this project's own markdown files — nothing
is pulled from the network or from outside repos.

Usage:
    cd chatdemo
    export CHATDEMO_BASE_URL=<openai-compatible endpoint>
    export CHATDEMO_API_KEY=<key>
    export CHATDEMO_EMBED_MODEL=<embedding model id>
    uv run python scripts/ingest_docs.py

Output lands at src/chatdemo/content/skills/docs-assistant/index/{corpus.json,vectors.npy,meta.json}.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parents[2]  # top-level of the repo
SKILL_DIR = Path(__file__).resolve().parents[1] / "src/chatdemo/content/skills/docs-assistant"
INDEX_DIR = SKILL_DIR / "index"

# Each tuple is (display title, path relative to repo root); together they define
# the entire indexed knowledge base. Append a new tuple to include another doc.
SOURCES = [
    ("Project overview", "README.md"),
    ("System design", "SystemDesign.md"),
    ("Backend guide", "chatdemo/README.md"),
    ("UI guide", "ui/README.md"),
    ("Authoring a workflow", "skills/authoring-chatdemo-workflows/SKILL.md"),
    ("Workflow manifest & DAG reference", "skills/authoring-chatdemo-workflows/references/manifest-and-dag.md"),
]

CHUNK_CHARS = 3200


def chunk(text: str) -> list[str]:
    """Break the input at blank lines, treating each gap as a paragraph or
    section boundary, then pack consecutive paragraphs into pieces roughly
    CHUNK_CHARS long, never cutting a single paragraph across two chunks."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if buf and len(buf) + len(p) + 2 > CHUNK_CHARS:
            chunks.append(buf)
            buf = p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        chunks.append(buf)
    return chunks


def build_corpus() -> list[dict]:
    corpus: list[dict] = []
    for title, rel_path in SOURCES:
        path = ROOT / rel_path
        if not path.exists():
            print(f"skip (missing): {rel_path}", file=sys.stderr)
            continue
        text = path.read_text(encoding="utf-8")
        for i, piece in enumerate(chunk(text)):
            corpus.append(
                {
                    "url": f"repo://{rel_path}",
                    "title": title,
                    "section": f"part {i + 1}" if i else None,
                    "chunk": i,
                    "text": piece,
                }
            )
    return corpus


async def embed_all(chunks: list[dict], base: str, key: str, model: str) -> np.ndarray:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    vectors = []
    async with httpx.AsyncClient(timeout=60, headers=headers) as client:
        for i, c in enumerate(chunks):
            text = f"{c['title']}\n{c['text']}"
            r = await client.post(
                f"{base}/embeddings",
                json={"model": model, "input": [text], "input_type": "passage"},
            )
            r.raise_for_status()
            vec = np.asarray(r.json()["data"][0]["embedding"], dtype=np.float32)
            norm = float(np.linalg.norm(vec))
            vectors.append(vec / norm if norm else vec)
            print(f"embedded {i + 1}/{len(chunks)}: {c['title']} ({c.get('section') or 'part 1'})")
    return np.stack(vectors)


def main() -> None:
    import asyncio

    base = (os.environ.get("CHATDEMO_BASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("CHATDEMO_API_KEY") or "").strip()
    embed_model = os.environ.get("CHATDEMO_EMBED_MODEL", "text-embedding-3-small")
    rerank_model = os.environ.get("CHATDEMO_RERANK_MODEL", "rerank-v1")
    if not base:
        sys.exit("CHATDEMO_BASE_URL is not set — point it at an OpenAI-compatible endpoint.")

    corpus = build_corpus()
    if not corpus:
        sys.exit("No source docs found — check the SOURCES list.")
    print(f"{len(corpus)} chunks from {len(SOURCES)} documents")

    vectors = asyncio.run(embed_all(corpus, base, key, embed_model))

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    (INDEX_DIR / "corpus.json").write_text(json.dumps(corpus, indent=2, ensure_ascii=False))
    np.save(INDEX_DIR / "vectors.npy", vectors)
    (INDEX_DIR / "meta.json").write_text(
        json.dumps(
            {
                "topic": "docs-assistant",
                "documents": len(SOURCES),
                "chunks": len(corpus),
                "embed_model": embed_model,
                "rerank_model": rerank_model,
                "dims": int(vectors.shape[1]),
            },
            indent=2,
        )
    )
    print(f"wrote index: {len(corpus)} chunks, {vectors.shape[1]} dims -> {INDEX_DIR}")


if __name__ == "__main__":
    main()
