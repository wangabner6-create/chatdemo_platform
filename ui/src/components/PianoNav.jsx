import { useState } from "react";

// A vertical strip resembling a piano keyboard, used as an index into the user's
// questions for the active chat. Each question maps to one key, alternating black/white
// in the same pattern as a real piano octave. Hover to preview the question text in a
// floating tooltip; click a key to jump to that question.
const BLACK = new Set([1, 3, 6, 8, 10]); // the semitone positions within an octave that render as black keys

export default function PianoNav({ questions, activeIndex, onJump }) {
  const [hover, setHover] = useState(null); // shape: { text, x, y }

  if (!questions.length) return null;

  const enter = (e, text) => {
    const r = e.currentTarget.getBoundingClientRect();
    setHover({ text, x: r.right + 8, y: r.top + r.height / 2 });
  };

  return (
    <nav className="piano-nav" aria-label="Questions in this chat">
      {questions.map((q, n) => {
        const black = BLACK.has(n % 12);
        const active = q.index === activeIndex;
        return (
          <button
            key={q.index}
            type="button"
            className={`piano-key ${black ? "black" : "white"}${active ? " active" : ""}`}
            onClick={() => onJump(q.index)}
            onMouseEnter={(e) => enter(e, q.text)}
            onMouseLeave={() => setHover(null)}
            aria-label={`Question ${n + 1}: ${q.text}`}
            aria-current={active ? "true" : undefined}
          >
            <span className="piano-key-num">{n + 1}</span>
          </button>
        );
      })}

      {hover && (
        <div className="piano-flyout" style={{ left: hover.x, top: hover.y }}>
          {hover.text}
        </div>
      )}
    </nav>
  );
}
