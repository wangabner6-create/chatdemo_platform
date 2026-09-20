"""HTTP surface for chat, layered on top of the agent orchestration runtime."""

from __future__ import annotations

import hmac
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .config import Settings
from .contracts import Response, User
from .feedback_store import recent_feedback, record_feedback
from .nemo_runtime import replace_session, run_agent, run_agent_streamed, shutdown, warmup
from .observability import configure_logging, emit
from .streaming import sse_stream


@asynccontextmanager
async def _lifespan(app: FastAPI):
    configure_logging()  # turn on human-readable, per-step log output (bump verbosity with CHATDEMO_LOG_LEVEL=DEBUG)
    await (
        warmup()
    )  # pre-build the NeMo workflow now, rather than paying that cost on the first incoming request
    try:
        yield
    finally:
        await shutdown()


class ChatRequest(BaseModel):
    query: str
    session_id: (
        str  # key used to look up multi-turn history, whether it lives in Redis or local SQLite
    )
    user: User | None = (
        None  # carried along for UI display and for attribution features not yet built
    )


class SessionMessage(BaseModel):
    role: str
    content: str


class ReplaceRequest(BaseModel):
    messages: list[SessionMessage] = []


class FeedbackRequest(BaseModel):
    # Payload for raw feedback submitted from the UI, either a thumbs 👍/👎 on a
    # reply or free text typed into the sidebar box. This goes straight to Redis
    # with no LLM involvement and no confirmation step. `vote` holds "up", "down",
    # or "" ; `comment` carries any free-form text; `question`/`answer` are only
    # populated when the feedback is a thumb on one particular exchange.
    session_id: str = ""
    question: str = ""  # user's turn that the thumb rating applies to
    answer: str = ""  # assistant's reply that the thumb rating applies to
    vote: str = ""  # one of up / down / "" — sidebar submissions may leave this blank
    comment: str = ""  # free-text note from the sidebar box
    source: str = "ui"  # where it came from: thumb or sidebar
    user: User | None = None
    trace_id: str = ""  # links this rating back to its Phoenix trace
    route: str = ""  # specialist that generated the rated answer
    model: str = ""  # model recorded in the answer metadata


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    """FastAPI dependency that locks the read-only admin routes behind CHATDEMO_ADMIN_TOKEN.

    If no token was configured, the route responds 403 — a deployment that never
    opted in to the admin endpoints can't accidentally expose usernames/queries.
    When a token is set, the request must present a matching `X-Admin-Token`
    header, compared with a constant-time check to avoid leaking timing info.
    """
    expected = Settings.from_env().admin_token
    if not expected:
        raise HTTPException(
            status_code=403, detail="admin endpoint disabled (CHATDEMO_ADMIN_TOKEN unset)"
        )
    if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=401, detail="invalid admin token")


def create_app() -> FastAPI:
    app = FastAPI(title="ChatDemo Platform", lifespan=_lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/chat", response_model=Response)
    async def chat(req: ChatRequest) -> Response:
        return await run_agent(req.query, session_id=req.session_id, user=req.user)

    @app.post("/session/{session_id}/replace")
    async def replace(session_id: str, req: ReplaceRequest) -> dict[str, object]:
        # Overwrite the stored session history — triggered when the UI deletes a
        # message — so what the model remembers stays in sync with what's on screen
        n = await replace_session(session_id, [m.model_dump() for m in req.messages])
        return {"ok": True, "count": n}

    @app.post("/feedback")
    def feedback(req: FeedbackRequest) -> dict[str, object]:
        # Declared as a plain (non-async) function so FastAPI dispatches it to a
        # worker thread; that way the synchronous, blocking Redis write can't stall
        # the event loop.
        rec = record_feedback(
            {
                "input_query": req.question,
                "response": req.answer,
                "feedback": req.vote,
                "comment": req.comment,
                "source": req.source,
                "session_id": req.session_id,
                "username": req.user.id if req.user else None,
                "trace_id": req.trace_id,
                "route": req.route,
                "model": req.model,
            },
            url=Settings.from_env().redis_url,
        )
        emit("feedback", source=req.source, vote=req.vote or "none", id=rec["id"])
        return {"ok": True, "id": rec["id"]}

    @app.get("/feedback", dependencies=[Depends(require_admin)])
    def list_feedback(limit: int | None = None) -> dict[str, object]:
        # Read-only listing of all captured feedback (thumb, sidebar, and chat
        # sources); access is enforced upstream by the require_admin dependency's
        # CHATDEMO_ADMIN_TOKEN check. Leave `limit` unset to get everything, newest
        # entries first.
        items = recent_feedback(limit=limit, url=Settings.from_env().redis_url)
        return {"count": len(items), "items": items}

    @app.post("/chat/stream")
    async def chat_stream(req: ChatRequest) -> StreamingResponse:
        # Genuine streaming — trace events and token deltas are pushed out while the turn is still executing
        events = run_agent_streamed(req.query, session_id=req.session_id, user=req.user)
        return StreamingResponse(
            sse_stream(events),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run("chatdemo.api:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
