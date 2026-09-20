import { useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Check, Copy } from "lucide-react";

// Renders a fenced code block with a Copy button that appears on hover; overflows into
// a scrollable area for long snippets (see global.css for the relevant styling).
function CodeBlock({ code }) {
  const [copied, setCopied] = useState(false);
  function copy() {
    navigator.clipboard?.writeText(code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    });
  }
  return (
    <div className="code-block">
      <button type="button" className="code-copy inline-flex items-center gap-1" onClick={copy}>
        {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
        {copied ? "Copied" : "Copy"}
      </button>
      <pre>
        <code>{code}</code>
      </pre>
    </div>
  );
}

function preToCode(children) {
  const child = Array.isArray(children) ? children[0] : children;
  const inner = child?.props?.children ?? "";
  const text = Array.isArray(inner) ? inner.join("") : String(inner);
  return text.replace(/\n$/, "");
}

// Turns an assistant's answer into GitHub-flavored markdown output. The `.md`
// class in global.css supplies all typography and spacing for it.
export default function MarkdownView({ text }) {
  return (
    <div className="md">
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" />,
          pre: ({ children }) => <CodeBlock code={preToCode(children)} />,
        }}
      >
        {text || ""}
      </Markdown>
    </div>
  );
}
