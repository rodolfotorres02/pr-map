import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { KIND_LABEL } from "../lib/format";
import type { GraphNode } from "../lib/types";
import { ChangeBadge } from "./common";

interface Props {
  reviewId: string;
  ready: boolean;
  onClose: () => void;
  onPick: (id: string) => void;
}

export function SymbolPalette({ reviewId, ready, onClose, onPick }: Props) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<GraphNode[]>([]);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => inputRef.current?.focus(), []);

  useEffect(() => {
    if (!ready || !query.trim()) {
      setResults([]);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      api
        .search(reviewId, query)
        .then((r) => {
          if (!cancelled) {
            setResults(r.results);
            setActive(0);
          }
        })
        .catch(() => undefined);
    }, 120);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [query, reviewId, ready]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") onClose();
    else if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => Math.min(results.length - 1, a + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(0, a - 1));
    } else if (e.key === "Enter" && results[active]) onPick(results[active].id);
  };

  return (
    <div className="palette-backdrop" onMouseDown={onClose}>
      <div className="palette" role="dialog" aria-label="Find a symbol" onMouseDown={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          className="palette-input"
          placeholder={ready ? "Function, class or method name" : "Still mapping the code base…"}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={!ready}
          spellCheck={false}
          aria-label="Symbol name"
        />
        <ul className="palette-results" role="listbox">
          {results.map((r, i) => (
            <li key={r.id} role="option" aria-selected={i === active}>
              <button className={`palette-item ${i === active ? "active" : ""}`} onMouseEnter={() => setActive(i)} onClick={() => onPick(r.id)}>
                <span className="swatch" style={{ background: `var(--cat-${r.category})` }} />
                <span className="palette-name">{r.label}</span>
                <span className="muted small">{KIND_LABEL[r.kind] ?? r.kind}</span>
                <ChangeBadge change={r.change} />
                <span className="palette-path muted small">
                  {r.path}:{r.line}
                </span>
              </button>
            </li>
          ))}
          {ready && query.trim() && results.length === 0 && <li className="palette-empty muted">No symbol by that name.</li>}
        </ul>
      </div>
    </div>
  );
}
