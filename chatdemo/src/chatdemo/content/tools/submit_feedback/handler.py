"""Handler function backing the submit_feedback tool.

Persists the feedback entry into the same shared Redis-backed store that the
UI's thumbs-up/thumbs-down control and sidebar feedback box also write to.
This path is triggered from chat: the agent derives `summary`/`category`/`vote`
from the conversation itself and invokes this tool (a confirmation step happens
first at the platform level, because side_effecting=true).
"""

from __future__ import annotations

from typing import Any

from chatdemo.feedback_store import record_feedback


def run(arguments: dict[str, Any], user: Any) -> dict[str, Any]:
    rec = record_feedback(
        {
            "input_query": arguments.get("summary"),
            "category": arguments.get("category"),
            "feedback": arguments.get("vote"),  # accepts up, down, or empty/omitted
            "comment": arguments.get("summary"),
            "source": "chat",
            "username": getattr(user, "id", None),
            "session_id": getattr(user, "session_id", None),
        }
    )
    return {
        "ticket_id": rec["id"],
        "summary": arguments.get("summary"),
        "category": arguments.get("category"),
        "vote": arguments.get("vote") or None,
        "submitted_by": rec.get("username") or None,
    }
