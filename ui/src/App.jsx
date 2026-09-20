import { useEffect, useState } from "react";
import { TooltipProvider } from "./components/ui/tooltip";
import Login from "./components/Login";
import Sidebar from "./components/Sidebar";
import Chat from "./components/Chat";
import { load, persist, uid, userFromName, streamChat, replaceSession, sendFeedback } from "./lib/store";

export default function App() {
  const init = load();
  const [user, setUser] = useState(init.user);
  const [backend, setBackend] = useState(init.backend);
  const [sessions, setSessions] = useState(init.sessions);
  const [current, setCurrent] = useState(init.current);
  const [theme, setTheme] = useState(init.theme || "dark");
  const [sidebarOpen, setSidebarOpen] = useState(init.sidebarOpen ?? true);

  const currentSession = sessions.find((s) => s.id === current) || null;

  // Keep the Tailwind `dark` class in sync with whatever the theme state currently is.
  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
  }, [theme]);

  function toggleTheme() {
    setTheme((t) => {
      const next = t === "dark" ? "light" : "dark";
      persist.theme(next);
      return next;
    });
  }

  function toggleSidebar() {
    setSidebarOpen((v) => {
      persist.sidebarOpen(!v);
      return !v;
    });
  }

  function deleteSession(id) {
    const next = sessions.filter((x) => x.id !== id);
    saveSessions(next);
    if (current === id) {
      const nextId = next[0]?.id || null;
      setCurrent(nextId);
      persist.current(nextId);
    }
  }

  // Drops one message (either a question or a reply) out of the active session, then
  // tries to push the same removal to the backend so the model's memory matches (no guarantees if it fails).
  function deleteMessage(index) {
    const s = sessions.find((x) => x.id === current);
    if (!s) return;
    const remaining = s.messages.filter((_, i) => i !== index);
    const updated = sessions.map((x) => (x.id === current ? { ...x, messages: remaining } : x));
    saveSessions(updated);
    replaceSession({ backend, sessionId: current, messages: remaining }).catch(() => {});
  }

  function saveSessions(next) {
    setSessions(next);
    persist.sessions(next);
  }

  // Handles a thumbs up/down on an assistant reply. Chat hands us the preceding user
  // turn, the answer text, and the vote value, which we forward directly to the backend — no LLM call, no confirmation step.
  function feedbackOnReply({ question, answer, vote, traceId, route, model }) {
    sendFeedback({
      backend,
      payload: {
        session_id: current,
        question,
        answer,
        vote,
        source: "thumb",
        user,
        trace_id: traceId || "",
        route: route || "",
        model: model || "",
      },
    }).catch(() => {});
  }

  // Handles the sidebar's free-text feedback form; the vote is optional and can be
  // left empty. The returned promise lets the sidebar render a sent/failed status.
  function submitSidebarFeedback({ comment, vote }) {
    return sendFeedback({
      backend,
      payload: { session_id: current || "", comment, vote: vote || "", source: "sidebar", user },
    });
  }

  function newSession() {
    const s = { id: uid(), title: "New session", messages: [] };
    const next = [s, ...sessions];
    saveSessions(next);
    setCurrent(s.id);
    persist.current(s.id);
    return s.id;
  }

  function login(name, backendUrl) {
    const u = userFromName(name);
    setUser(u);
    persist.user(u);
    setBackend(backendUrl);
    persist.backend(backendUrl);
    if (sessions.length === 0) newSession();
  }

  function logout() {
    setUser(null);
    persist.user(null);
  }

  function selectSession(id) {
    setCurrent(id);
    persist.current(id);
  }

  async function send(text) {
    const s = sessions.find((x) => x.id === current);
    if (!text.trim() || !s) return;

    const userMsg = { role: "user", text };
    const pending = { role: "assistant", text: "", pending: true, trace: [], sources: [], related: [] };
    const title = s.title === "New session" ? text.slice(0, 40) : s.title;
    const next = sessions.map((x) =>
      x.id === s.id ? { ...x, title, messages: [...x.messages, userMsg, pending] } : x,
    );
    saveSessions(next);

    // Holds the in-progress assistant message as stream chunks arrive
    const acc = { role: "assistant", text: "", pending: true, trace: [], sources: [], related: [] };

    // Pushes whatever is currently in the accumulator into the session's final message slot
    const flush = (persistToo) =>
      setSessions((cur) => {
        const updated = cur.map((x) => {
          if (x.id !== s.id) return x;
          const msgs = [...x.messages];
          msgs[msgs.length - 1] = { ...acc, trace: [...acc.trace], sources: [...acc.sources], related: [...acc.related] };
          return { ...x, messages: msgs };
        });
        // Strip the trace (which can get large) and the pending flag before persisting, to keep localStorage small
        if (persistToo)
          persist.sessions(
            updated.map((x) =>
              x.id === s.id
                ? { ...x, messages: x.messages.map((m) => ({ ...m, trace: undefined, pending: false })) }
                : x,
            ),
          );
        return updated;
      });

    const onEvent = (ev) => {
      switch (ev.type) {
        case "trace":
          acc.trace.push(ev);
          break;
        case "meta":
          acc.route = ev.route;
          acc.model = ev.model;
          acc.traceId = ev.trace_id;
          break;
        case "delta":
          acc.text += ev.text;
          break;
        case "sources":
          acc.sources = ev.sources || [];
          break;
        case "related":
          acc.related = ev.related_questions || [];
          break;
        case "state":
          if (ev.state === "confirm")
            acc.text += "  —  reply 'yes' to confirm or 'no' to cancel.";
          else if (ev.state === "clarify" && !acc.text) acc.text = ev.clarify_question || "";
          break;
        case "error":
          acc.error = ev.message;
          break;
        case "done":
          acc.pending = false;
          break;
        default:
          break;
      }
      flush(ev.type === "done");
    };

    try {
      await streamChat({ backend, query: text, sessionId: s.id, user, onEvent });
    } catch (err) {
      acc.error = `${err.message} (is the backend reachable at "${backend || "same origin"}"?)`;
    } finally {
      acc.pending = false;
      flush(true);
    }
  }

  const body = !user ? (
    <Login onLogin={login} />
  ) : (
    <div className="flex h-full">
      {sidebarOpen && (
        <Sidebar
          user={user}
          sessions={sessions}
          current={current}
          theme={theme}
          onNew={newSession}
          onSelect={selectSession}
          onDelete={deleteSession}
          onToggleTheme={toggleTheme}
          onCollapse={toggleSidebar}
          onLogout={logout}
          onSubmitFeedback={submitSidebarFeedback}
        />
      )}
      <Chat
        session={currentSession}
        onSend={send}
        onDeleteMessage={deleteMessage}
        onFeedback={feedbackOnReply}
        sidebarOpen={sidebarOpen}
        onShowSidebar={toggleSidebar}
      />
    </div>
  );

  return <TooltipProvider>{body}</TooltipProvider>;
}
