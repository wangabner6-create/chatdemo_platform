// A lightweight store on top of localStorage, plus the functions that call the backend.
// This follows the same split as the original vanilla UI: session and message state is
// kept entirely on the client, while the backend is the source of truth for multi-turn
// conversation history, keyed by session_id.
const LS = {
  get(k, d) {
    try {
      return JSON.parse(localStorage.getItem(k)) ?? d;
    } catch {
      return d;
    }
  },
  set: (k, v) => localStorage.setItem(k, JSON.stringify(v)),
  del: (k) => localStorage.removeItem(k),
};

export const uid = () =>
  crypto.randomUUID
    ? crypto.randomUUID()
    : "id-" + Date.now() + "-" + Math.random().toString(16).slice(2);

export const load = () => ({
  user: LS.get("chatdemo.user", null), // shape: { id, name }
  backend: LS.get("chatdemo.backend", ""), // empty string means same-origin, since nginx proxies /chat
  sessions: LS.get("chatdemo.sessions", []), // array of { id, title, messages }
  current: LS.get("chatdemo.current", null),
  theme: LS.get("chatdemo.theme", "dark"), // either "light" or "dark"
  sidebarOpen: LS.get("chatdemo.sidebarOpen", true),
});

export const persist = {
  user: (v) => (v ? LS.set("chatdemo.user", v) : LS.del("chatdemo.user")),
  backend: (v) => LS.set("chatdemo.backend", v),
  sessions: (v) => LS.set("chatdemo.sessions", v),
  current: (v) => LS.set("chatdemo.current", v),
  theme: (v) => LS.set("chatdemo.theme", v),
  sidebarOpen: (v) => LS.set("chatdemo.sidebarOpen", v),
};

export function userFromName(name) {
  return { id: "user-" + name.toLowerCase().replace(/\s+/g, "-"), name };
}

// Sends a single turn to the backend as a normal (non-streaming) POST request.
// Resolves to { text, sources, related }.
export async function sendChat({ backend, query, sessionId, user }) {
  const res = await fetch(backend.replace(/\/$/, "") + "/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, session_id: sessionId, user }),
  });
  if (!res.ok) throw new Error("HTTP " + res.status);
  const data = await res.json();
  let text = data.answer || data.clarify_question || "(no answer)";
  if (data.state === "confirm") text += "  —  reply 'yes' to confirm or 'no' to cancel.";
  return { text, sources: data.sources || [], related: data.related_questions || [] };
}

// Overwrites the backend's stored history for this session with the given messages.
// Used to keep the model's multi-turn memory consistent with the UI after a deletion; not guaranteed to succeed.
export async function replaceSession({ backend, sessionId, messages }) {
  const items = messages
    .filter((m) => (m.role === "user" || m.role === "assistant") && m.text && !m.error)
    .map((m) => ({ role: m.role, content: m.text }));
  await fetch(backend.replace(/\/$/, "") + "/session/" + encodeURIComponent(sessionId) + "/replace", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages: items }),
  });
}

// Forwards feedback — either a thumb up/down on a specific reply or free text typed
// into the sidebar box — to the backend, which persists it directly to Redis. Not
// guaranteed to succeed; resolves with the generated id.
// `payload` shape: { session_id, question, answer, vote, comment, source, user,
// trace_id, route, model }.
export async function sendFeedback({ backend, payload }) {
  const res = await fetch(backend.replace(/\/$/, "") + "/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

// Streams a turn from the backend using server-sent events, invoking `onEvent(ev)` for
// every event received — trace entries (routing, tool, or workflow steps), meta, delta
// (incremental answer tokens), sources, related questions, state, error, and done. The
// returned promise settles once the stream finishes.
export async function streamChat({ backend, query, sessionId, user, onEvent, signal }) {
  const res = await fetch(backend.replace(/\/$/, "") + "/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, session_id: sessionId, user }),
    signal,
  });
  if (!res.ok || !res.body) throw new Error("HTTP " + res.status);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let sep;
    // A blank line marks the boundary between consecutive SSE frames
    while ((sep = buf.indexOf("\n\n")) !== -1) {
      const frame = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      for (const line of frame.split("\n")) {
        if (!line.startsWith("data:")) continue;
        const json = line.slice(5).trim();
        if (!json) continue;
        try {
          onEvent(JSON.parse(json));
        } catch {
          /* swallow any frame that fails to parse */
        }
      }
    }
  }
}
