import { useState } from "react";
import { Button } from "./ui/button";
import { Badge } from "./ui/badge";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "./ui/collapsible";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetTrigger } from "./ui/sheet";
import {
  Copy,
  Check,
  Download,
  ThumbsUp,
  ThumbsDown,
  X,
  ChevronDown,
  Loader2,
  Library,
  ExternalLink,
} from "lucide-react";
import MarkdownView from "./Markdown";
import { cn } from "../lib/utils";

// Builds a compact single-line label describing a backend trace event, whether it's
// a routing decision, a tool call, or a workflow step.
function traceLabel(ev) {
  const k = ev.kind;
  if (k === "handoff") return { tag: "route", text: `→ ${ev.to}`, dim: false };
  if (k === "agent") return { tag: "agent", text: `${ev.name} ${ev.event}`, dim: true };
  if (k === "tool")
    return {
      tag: "tool",
      text: `${ev.name} ${ev.event}${ev.result ? ` → ${ev.result}` : ""}`,
      dim: ev.event === "start",
    };
  if (k === "workflow")
    return { tag: "workflow", text: `${ev.id} ${ev.event}${ev.ms != null ? ` (${ev.ms}ms)` : ""}`, dim: false };
  if (k === "level") return { tag: "level", text: `${ev.index}: ${(ev.steps || []).join(", ")}`, dim: true };
  if (k === "step") {
    if (ev.event === "skip") return { tag: "step", text: `${ev.id} skipped (${ev.when})`, dim: true };
    if (ev.event === "end") return { tag: "step", text: `${ev.id} (${ev.ms}ms)`, dim: true };
    if (ev.event === "retry") return { tag: "retry", text: `${ev.id} attempt ${ev.attempt}`, dim: false };
    if (ev.event === "error") return { tag: "error", text: `${ev.id} error`, dim: false };
    return { tag: "step", text: `${ev.id}…`, dim: true };
  }
  if (k === "confirm") return { tag: "confirm", text: `awaiting confirm: ${ev.tool}`, dim: false };
  if (k === "resume") return { tag: "resume", text: `approved=${ev.approved}`, dim: true };
  return { tag: k, text: `${ev.event || ""}`.trim(), dim: true };
}

// An expandable/collapsible panel that lists out the backend's step-by-step trace for this turn.
function TracePanel({ trace, live }) {
  const [open, setOpen] = useState(live);
  if (!trace?.length) return null;
  return (
    <Collapsible open={open} onOpenChange={setOpen} className="mb-1">
      <CollapsibleTrigger asChild>
        <button
          type="button"
          className="flex items-center gap-1 text-xs font-medium text-muted-foreground hover:text-foreground"
        >
          <ChevronDown className={cn("size-3.5 transition-transform", !open && "-rotate-90")} />
          {live ? "Running…" : "Steps"} ({trace.length})
        </button>
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-1 flex flex-col gap-1 border-l-2 border-border pl-2">
        {trace.map((ev, i) => {
          const { tag, text, dim } = traceLabel(ev);
          return (
            <div
              key={i}
              className={cn(
                "flex items-baseline gap-2 font-mono text-xs whitespace-pre-wrap",
                dim ? "text-muted-foreground" : "text-foreground",
              )}
            >
              <Badge variant="outline" className="shrink-0 font-sans normal-case">
                {tag}
              </Badge>
              <span>{text}</span>
            </div>
          );
        })}
      </CollapsibleContent>
    </Collapsible>
  );
}

export default function Message({ msg, onRelated, onDelete, onFeedback }) {
  const [copied, setCopied] = useState(false);
  const [vote, setVote] = useState(null); // holds "up", "down", or null — the current vote for this reply

  function castVote(v) {
    const next = vote === v ? null : v; // clicking the same vote again resets it to null
    setVote(next);
    onFeedback?.(next || v); // report either the new vote, or fall back to the original value when clearing
  }
  const delBtn = (
    <button className="msg-del" onClick={onDelete} aria-label="Delete message" title="Delete">
      <X className="size-3.5" />
    </button>
  );

  function copyAnswer() {
    navigator.clipboard?.writeText(msg.text || "").then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    });
  }
  function downloadAnswer() {
    const blob = new Blob([msg.text || ""], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "chatdemo-answer.md";
    a.click();
    URL.revokeObjectURL(url);
  }

  // For a user turn, render a muted, right-aligned chat bubble without any strong accent color.
  if (msg.role === "user") {
    return (
      <div className="msg-wrap flex items-center justify-end gap-2">
        {delBtn}
        <div className="user-bubble">
          <p className="whitespace-pre-wrap text-sm">{msg.text}</p>
        </div>
      </div>
    );
  }

  // For an assistant turn, render the full-width layout: trace panel, markdown answer, sources, and related questions.
  const showSpinner = msg.pending && !msg.text && !msg.error;
  return (
    <div className="msg-wrap relative flex w-full flex-col gap-2">
      <div className="msg-del-slot">{delBtn}</div>
      <TracePanel trace={msg.trace} live={msg.pending} />

      {msg.error ? (
        <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {msg.error}
        </div>
      ) : showSpinner ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" aria-label="Thinking" />
          Thinking…
        </div>
      ) : (
        <MarkdownView text={msg.text + (msg.pending && msg.text ? " ▌" : "")} />
      )}

      {!msg.pending && !msg.error && msg.text && (
        <div className="flex flex-wrap items-center gap-1.5">
          <Button
            variant="ghost"
            size="icon"
            onClick={copyAnswer}
            aria-label="Copy answer"
            title={copied ? "Copied" : "Copy answer"}
          >
            {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
          </Button>
          <Button variant="ghost" size="icon" onClick={downloadAnswer} aria-label="Download answer" title="Download answer">
            <Download className="size-4" />
          </Button>
          <span className={"vote-btn vote-up" + (vote === "up" ? " on" : "")}>
            <Button variant="ghost" size="icon" onClick={() => castVote("up")} aria-label="Good answer" title="Good answer">
              <ThumbsUp className="size-4" />
            </Button>
          </span>
          <span className={"vote-btn vote-down" + (vote === "down" ? " on" : "")}>
            <Button variant="ghost" size="icon" onClick={() => castVote("down")} aria-label="Bad answer" title="Bad answer">
              <ThumbsDown className="size-4" />
            </Button>
          </span>
          {msg.traceId && (
            <Badge variant="outline" className="font-mono text-[10px]" title={msg.traceId}>
              trace {msg.traceId.slice(0, 8)}
            </Badge>
          )}
          {msg.sources?.length > 0 && (
            <Sheet>
              <SheetTrigger asChild>
                <Button variant="secondary" size="sm" className="gap-1.5">
                  <Library className="size-3.5" />
                  Sources ({msg.sources.length})
                </Button>
              </SheetTrigger>
              <SheetContent>
                <SheetHeader>
                  <SheetTitle>Sources ({msg.sources.length})</SheetTitle>
                </SheetHeader>
                <div className="flex flex-col gap-4 overflow-y-auto px-4 pb-4">
                  {msg.sources.map((s, i) => (
                    <div key={i} className="flex flex-col gap-1">
                      <a
                        href={s.url || "#"}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="flex items-start gap-1.5 text-sm font-medium text-primary hover:underline"
                      >
                        <span>
                          {i + 1}. {s.title || s.url}
                        </span>
                        <ExternalLink className="mt-0.5 size-3 shrink-0" />
                      </a>
                      {s.url && <span className="ellipsis text-xs text-muted-foreground">{s.url}</span>}
                    </div>
                  ))}
                </div>
              </SheetContent>
            </Sheet>
          )}
        </div>
      )}

      {msg.related?.length > 0 && (
        <div className="mt-1 flex flex-col gap-0.5">
          <span className="section-heading">Follow-ups</span>
          {msg.related.map((q, i) => (
            <button key={i} className="related-row" onClick={() => onRelated(q)}>
              <span className="ellipsis">{q}</span>
              <span className="related-plus">+</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
