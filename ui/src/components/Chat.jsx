import { useEffect, useRef, useState } from "react";
import { Input } from "./ui/input";
import { Button } from "./ui/button";
import { PanelLeftOpen, SendHorizontal, MessageSquareText } from "lucide-react";
import Message from "./Message";
import PianoNav from "./PianoNav";

export default function Chat({ session, onSend, onDeleteMessage, onFeedback, sidebarOpen, onShowSidebar }) {
  const [input, setInput] = useState("");
  const [activeQ, setActiveQ] = useState(null);
  const endRef = useRef(null);
  const scrollRef = useRef(null);
  const msgRefs = useRef(new Map());
  const prevLen = useRef(0);
  const prevId = useRef(null);

  // Pull out just this session's user questions, keeping each one's message index
  // attached so the piano nav can jump directly to it.
  const questions = (session?.messages || [])
    .map((m, i) => ({ index: i, role: m.role, text: m.text }))
    .filter((m) => m.role === "user" && m.text);

  function jumpTo(index) {
    const el = msgRefs.current.get(index);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    setActiveQ(index);
  }

  // Keeps the correct piano key lit up for the question currently in the viewport.
  // Maintains a running set of which messages are visible and activates the topmost visible question.
  useEffect(() => {
    const root = scrollRef.current;
    if (!root) return;
    const visible = new Set();
    const qIndices = new Set(questions.map((q) => q.index));
    const obs = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          const idx = Number(e.target.dataset.msgIndex);
          if (e.isIntersecting) visible.add(idx);
          else visible.delete(idx);
        }
        const onScreen = [...visible].filter((i) => qIndices.has(i));
        if (onScreen.length) setActiveQ(Math.min(...onScreen));
      },
      { root, threshold: 0 },
    );
    msgRefs.current.forEach((el) => el && obs.observe(el));
    return () => obs.disconnect();
  }, [session?.id, session?.messages?.length]);

  // Only jump to the bottom when a message gets appended or the session changes.
  // If a message is removed instead (the list gets shorter), leave the scroll position alone.
  useEffect(() => {
    const id = session?.id ?? null;
    const len = session?.messages?.length ?? 0;
    const switched = id !== prevId.current;
    if (switched || len > prevLen.current) {
      endRef.current?.scrollIntoView({ behavior: switched ? "auto" : "smooth" });
    }
    prevId.current = id;
    prevLen.current = len;
  }, [session?.id, session?.messages?.length]);

  function submit(e) {
    e.preventDefault();
    const text = input.trim();
    if (!text) return;
    setInput("");
    onSend(text);
  }

  return (
    <div className="flex h-full min-w-0 flex-1 flex-col bg-background">
      {!sidebarOpen && (
        <div className="border-b border-border px-3 py-2">
          <Button variant="ghost" size="sm" onClick={onShowSidebar} className="gap-2">
            <PanelLeftOpen className="size-4" /> Menu
          </Button>
        </div>
      )}
      <div className="flex min-h-0 flex-1">
        <PianoNav questions={questions} activeIndex={activeQ} onJump={jumpTo} />
        <div ref={scrollRef} className="flex-1 overflow-y-auto">
          <div className="chat-col">
            {!session || session.messages.length === 0 ? (
              <div className="flex h-[60vh] flex-col items-center justify-center gap-3 text-center">
                <div className="flex size-12 items-center justify-center rounded-full bg-muted text-muted-foreground">
                  <MessageSquareText className="size-6" />
                </div>
                <p className="text-lg font-medium">Ask ChatDemo anything</p>
              </div>
            ) : (
              <div className="flex flex-col gap-6">
                {session.messages.map((m, i) => (
                  <div
                    key={i}
                    data-msg-index={i}
                    ref={(el) => {
                      if (el) msgRefs.current.set(i, el);
                      else msgRefs.current.delete(i);
                    }}
                  >
                    <Message
                      msg={m}
                      onRelated={(q) => onSend(q)}
                      onDelete={() => onDeleteMessage(i)}
                      onFeedback={(vote) =>
                        onFeedback?.({
                          question: session.messages[i - 1]?.text || "",
                          answer: m.text || "",
                          vote,
                          traceId: m.traceId,
                          route: m.route,
                          model: m.model,
                        })
                      }
                    />
                  </div>
                ))}
              </div>
            )}
            <div ref={endRef} />
          </div>
        </div>
      </div>

      <div className="border-t border-border">
        <form onSubmit={submit} className="chat-col !py-3">
          <div className="flex items-center gap-2">
            <Input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Ask something…"
              className="flex-1"
            />
            <Button type="submit" size="icon" aria-label="Send">
              <SendHorizontal className="size-4" />
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
