"""Common local-corpus search logic reused by every content-authored RAG skill.

Each topic skill owns a folder containing an `index/` directory that gets
built offline by a separate ingestion script (see `scripts/ingest_docs.py` for
the canonical example). This module wraps that on-disk index into the
`run(arguments, user)` callable that a skill mounts from its
`tools/<name>/handler.py`. The implementation lives here once and every topic
reuses it — a topic's handler.py ends up being a tiny pass-through, roughly:

    from chatdemo.rag import make_search
    run = make_search(Path(__file__).resolve().parents[2])

Retrieval happens in two passes: a cheap embedding pass gets broad recall
across the full corpus, then a cross-encoder reranker sharpens precision over
that shortlist.

    embed(query) -> cosine top-N (N=50) -> rerank -> top-k (k=8)

Both the embedding and rerank calls follow the OpenAI-compatible wire format
and are sent to CHATDEMO_BASE_URL.

On failures, this follows the same policy as the rest of the platform's
retrievers: nothing here should propagate an exception up into the
orchestration loop. Whether the index is missing, the endpoint is unreachable,
or the request times out, the function instead returns a plain-text sentence
the model can pass along — so a retrieval outage shows up to the user as "I
can't search right now" rather than a hard 500 error.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from .citations import dedup_and_format
from .observability import trace_step

_TIMEOUT = float(os.environ.get("CHATDEMO_RAG_TIMEOUT", "30"))
_RECALL = int(
    os.environ.get("CHATDEMO_RAG_RECALL", "50")
)  # size of the vector-similarity shortlist
_TOP_K = int(os.environ.get("CHATDEMO_RAG_TOP_K", "8"))  # how many survive the rerank step
_SNIPPET_MAX = 900
_DIGEST_MAX = 900  # character budget per passage sent to the model


class _Index:
    """Wraps corpus.json and vectors.npy; loaded lazily, exactly once per
    process, the first time a search runs.

    Vectors on disk are float32 and already L2-normalised, so cosine
    similarity between two vectors reduces to a plain dot product.
    """

    def __init__(self, folder: Path) -> None:
        self._dir = folder / "index"
        self.chunks: list[dict] = []
        self.vectors: np.ndarray | None = None
        self.meta: dict = {}
        self.error = ""
        try:
            self.chunks = json.loads((self._dir / "corpus.json").read_text())
            self.vectors = np.load(self._dir / "vectors.npy").astype(np.float32)
            mp = self._dir / "meta.json"
            self.meta = json.loads(mp.read_text()) if mp.exists() else {}
        except FileNotFoundError:
            self.error = (
                f"no index at {self._dir} — run `uv run python scripts/ingest_docs.py` to build it"
            )
            return
        except Exception as exc:  # noqa: BLE001 — a corrupted index file shouldn't take the whole turn down with it
            self.error = f"index at {self._dir} is unreadable ({type(exc).__name__}: {exc})"
            return
        if self.vectors is not None and len(self.chunks) != len(self.vectors):
            self.error = (
                f"index at {self._dir} is inconsistent: {len(self.chunks)} chunks vs "
                f"{len(self.vectors)} vectors — rebuild it"
            )


def _client() -> tuple[httpx.AsyncClient, str] | None:
    base = (os.environ.get("CHATDEMO_BASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("CHATDEMO_API_KEY") or "").strip()
    if not base:
        return None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return httpx.AsyncClient(timeout=_TIMEOUT, headers=headers), base


async def _embed_query(client: httpx.AsyncClient, base: str, model: str, text: str) -> np.ndarray:
    """Turns the query into an embedding vector. Passing `input_type: query`
    matters for asymmetric embedding models, since the corpus passages were
    embedded with `input_type: passage` — using the wrong one for either side
    measurably hurts recall. Symmetric models simply ignore the field, so it's
    safe either way."""
    with trace_step(
        "retrieval.embed_query",
        "llm",
        attributes={"llm.model_name": model, "retrieval.query.characters": len(text)},
    ) as span:
        r = await client.post(
            f"{base}/embeddings",
            json={"model": model, "input": [text], "input_type": "query"},
        )
        r.raise_for_status()
        body = r.json()
        vec = np.asarray(body["data"][0]["embedding"], dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        span.set_tokens((body.get("usage") or {}).get("total_tokens"))
        span.set_output({"dimensions": len(vec)})
        return vec / norm if norm else vec


async def _rerank(
    client: httpx.AsyncClient, base: str, model: str, query: str, docs: list[str]
) -> list[tuple[int, float]]:
    """Runs a rerank call shaped like Cohere's API. Returns a best-first list of
    (index_into_docs, score) pairs."""
    with trace_step(
        "retrieval.rerank",
        "llm",
        attributes={"llm.model_name": model, "retrieval.documents": len(docs)},
    ) as span:
        r = await client.post(
            f"{base}/rerank",
            json={"model": model, "query": query, "documents": docs, "top_n": len(docs)},
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        span.set_output({"ranked_documents": len(results)})
        return [(int(x["index"]), float(x.get("relevance_score", 0.0))) for x in results]


def _passage(chunk: dict) -> str:
    """Builds the text that both the reranker scores and the model eventually
    reads. Prepending the title is deliberate — a chunk taken from the middle
    of a document tends to lose its section heading, and that context matters
    to the cross-encoder as much as it does to the model."""
    head = chunk.get("title") or ""
    body = (chunk.get("text") or "").strip()
    return f"{head}\n{body}" if head else body


def make_search(folder: Path, tool_name: str = "search") -> Any:
    """Constructs the `run(arguments, user)` callable a topic skill's search tool executes.

    `folder` should be the skill's own directory — the one that holds SKILL.md
    alongside index/. Loading of the index is deferred to the first actual
    call, which keeps importing a skill's handler from touching disk while the
    registry is just being scanned.
    """
    state: dict[str, _Index] = {}

    async def run(arguments: dict[str, Any], user: Any) -> str:
        query = (arguments.get("query") or "").strip()
        if not query:
            return f"{tool_name}: `query` is required."

        index = state.get("index")
        if index is None:
            index = state["index"] = _Index(folder)
        if index.error:
            return f"{tool_name} unavailable: {index.error}. Tell the user search is down."

        top_k = min(int(arguments.get("k") or _TOP_K), 20)

        made = _client()
        if made is None:
            return (
                f"{tool_name} unavailable: CHATDEMO_BASE_URL is not set, so the query "
                "cannot be embedded. Tell the user search is not configured."
            )
        client, base = made
        # meta.json values take priority over the env vars — if a query goes out with a
        # different embedding model than the one the corpus was built against, you get
        # confidently wrong results instead of a visible failure.
        embed_model = index.meta.get("embed_model") or os.environ.get(
            "CHATDEMO_EMBED_MODEL", "text-embedding-3-small"
        )
        rerank_model = index.meta.get("rerank_model") or os.environ.get(
            "CHATDEMO_RERANK_MODEL", "rerank-v1"
        )

        try:
            async with client:
                qvec = await _embed_query(client, base, embed_model, query)
                assert index.vectors is not None
                with trace_step(
                    "retrieval.vector_search",
                    attributes={
                        "retrieval.corpus_chunks": len(index.chunks),
                        "retrieval.recall": min(_RECALL, len(index.chunks)),
                    },
                ) as span:
                    sims = index.vectors @ qvec
                    shortlist = list(np.argsort(-sims)[:_RECALL])
                    span.set_output(
                        {
                            "shortlist_size": len(shortlist),
                            "top_score": float(sims[shortlist[0]]) if shortlist else None,
                        }
                    )

                # Second pass: reranking. If it fails, that's not fatal — falling back to
                # plain vector order still gives usable results, so we degrade gracefully
                # rather than return an empty set.
                order = list(range(len(shortlist)))
                if rerank_model.strip().lower() not in ("", "off", "disabled", "none"):
                    try:
                        docs = [_passage(index.chunks[i])[:_DIGEST_MAX] for i in shortlist]
                        ranked = await _rerank(client, base, rerank_model, query, docs)
                        if ranked:
                            order = [i for i, _ in ranked]
                    except Exception:  # noqa: BLE001, S110 — stick with vector order
                        pass
                picked = [shortlist[i] for i in order[:top_k]]
        except httpx.HTTPStatusError as exc:
            return (
                f"{tool_name} failed (HTTP {exc.response.status_code} from the model "
                "endpoint); tell the user retrieval is unavailable."
            )
        except Exception as exc:  # noqa: BLE001 — turn whatever happened into a message the caller can act on
            return (
                f"{tool_name} failed ({type(exc).__name__}: {exc}); tell the user "
                "retrieval is unavailable."
            )

        sources = [
            {
                "title": index.chunks[i].get("title"),
                "url": index.chunks[i].get("url"),
                "snippet": (index.chunks[i].get("text") or "").strip()[:_SNIPPET_MAX] or None,
                "metadata": {"section": index.chunks[i].get("section")},
            }
            for i in picked
        ]
        return dedup_and_format(sources, user, query)

    return run
