import { useState } from "react";
import { Button } from "./ui/button";
import { Avatar, AvatarFallback } from "./ui/avatar";
import { Textarea } from "./ui/textarea";
import { Separator } from "./ui/separator";
import { MessageSquareText, Plus, Sun, Moon, PanelLeftClose, X, ThumbsUp, ThumbsDown, MessageCircle } from "lucide-react";

export default function Sidebar({
  user,
  sessions,
  current,
  theme,
  onNew,
  onSelect,
  onDelete,
  onToggleTheme,
  onCollapse,
  onLogout,
  onSubmitFeedback,
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [vote, setVote] = useState(null); // optional value: "up", "down", or null
  const [status, setStatus] = useState(null); // one of "sending", "sent", "error", or unset

  async function submit() {
    if (!text.trim() && !vote) return;
    setStatus("sending");
    try {
      await onSubmitFeedback?.({ comment: text.trim(), vote });
      setStatus("sent");
      setText("");
      setVote(null);
      setTimeout(() => {
        setStatus(null);
        setOpen(false);
      }, 900);
    } catch {
      setStatus("error");
    }
  }

  return (
    <div className="flex h-full w-64 shrink-0 flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground box-border">
      {/* logo/name plus the light-dark theme switcher */}
      <div className="flex items-center justify-between px-4 py-3">
        <div className="flex items-center gap-2">
          <div className="flex size-6 items-center justify-center rounded-md bg-primary text-primary-foreground">
            <MessageSquareText className="size-3.5" />
          </div>
          <span className="font-semibold">ChatDemo</span>
        </div>
        <div className="flex items-center gap-1">
          <Button variant="ghost" size="icon" onClick={onToggleTheme} aria-label="Toggle color theme">
            {theme === "dark" ? <Sun className="size-4" /> : <Moon className="size-4" />}
          </Button>
          <Button variant="ghost" size="icon" onClick={onCollapse} aria-label="Hide sidebar">
            <PanelLeftClose className="size-4" />
          </Button>
        </div>
      </div>

      <div className="px-3 pb-3">
        <Button variant="secondary" onClick={onNew} className="w-full justify-start gap-2">
          <Plus className="size-4" /> New chat
        </Button>
      </div>

      {/* scrollable list of chat sessions */}
      <div className="flex-1 overflow-y-auto px-2 flex flex-col gap-0.5">
        {sessions.map((s) => (
          <div
            key={s.id}
            className={"session-row" + (s.id === current ? " active" : "")}
            onClick={() => onSelect(s.id)}
          >
            <span className="session-title">{s.title}</span>
            <button
              className="session-del"
              aria-label="Delete chat"
              onClick={(e) => {
                e.stopPropagation();
                onDelete(s.id);
              }}
            >
              <X className="size-3.5" />
            </button>
          </div>
        ))}
      </div>

      {/* free-text feedback widget, positioned directly above the user info footer */}
      <div className="fb-box">
        {open ? (
          <div className="flex flex-col gap-2">
            <Textarea
              className="fb-text"
              rows={3}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Tell us what worked or what didn't…"
              autoFocus
            />
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-1">
                <button
                  className={"fb-vote" + (vote === "up" ? " on" : "")}
                  onClick={() => setVote(vote === "up" ? null : "up")}
                  aria-label="Thumbs up"
                  title="Thumbs up"
                >
                  <ThumbsUp className="size-3.5" />
                </button>
                <button
                  className={"fb-vote" + (vote === "down" ? " on" : "")}
                  onClick={() => setVote(vote === "down" ? null : "down")}
                  aria-label="Thumbs down"
                  title="Thumbs down"
                >
                  <ThumbsDown className="size-3.5" />
                </button>
              </div>
              <div className="flex items-center gap-1">
                <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
                  Cancel
                </Button>
                <Button size="sm" onClick={submit} disabled={status === "sending" || (!text.trim() && !vote)}>
                  {status === "sending" ? "Sending…" : status === "sent" ? "Sent ✓" : "Send"}
                </Button>
              </div>
            </div>
            {status === "error" && (
              <p className="text-sm text-destructive">Couldn't send — try again.</p>
            )}
          </div>
        ) : (
          <Button variant="secondary" size="sm" onClick={() => setOpen(true)} className="w-full gap-2">
            <MessageCircle className="size-3.5" /> Send feedback
          </Button>
        )}
      </div>

      <Separator className="bg-sidebar-border" />

      {/* footer showing the signed-in user and logout control */}
      <div className="flex items-center justify-between gap-2 px-4 py-3">
        <div className="flex min-w-0 items-center gap-2">
          <Avatar className="size-7">
            <AvatarFallback>{initials(user.name)}</AvatarFallback>
          </Avatar>
          <span className="ellipsis text-sm font-medium">{user.name}</span>
        </div>
        <Button variant="ghost" size="sm" onClick={onLogout}>
          Log out
        </Button>
      </div>
    </div>
  );
}

function initials(name) {
  return name
    .split(/\s+/)
    .map((w) => w[0])
    .slice(0, 2)
    .join("")
    .toUpperCase();
}
