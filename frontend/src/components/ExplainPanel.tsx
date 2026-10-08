import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { renderMarkdown } from "../lib/markdown";
import { Spinner } from "./common";

export interface ExplainTarget {
  id: string;
  label: string;
}

interface Props {
  reviewId: string;
  target: ExplainTarget;
  onClose: () => void;
}

type Phase = "streaming" | "done" | "error";

export function ExplainPanel({ reviewId, target, onClose }: Props) {
  const [text, setText] = useState("");
  const [phase, setPhase] = useState<Phase>("streaming");
  const [error, setError] = useState<string | null>(null);
  const [meta, setMeta] = useState<{ model: string | null; cost: number | null; ms: number | null; cached: boolean }>({
    model: null,
    cost: null,
    ms: null,
    cached: false,
  });
  const [run, setRun] = useState(0); // bump to regenerate
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setText("");
    setError(null);
    setPhase("streaming");
    setMeta({ model: null, cost: null, ms: null, cached: false });
    (async () => {
      try {
        for await (const event of api.explain(reviewId, target.id, run > 0, controller.signal)) {
          if (event.type === "meta") setMeta((m) => ({ ...m, model: event.model, cached: !!event.cached }));
          else if (event.type === "text") setText((t) => t + event.text);
          else if (event.type === "done") {
            setMeta((m) => ({ ...m, cost: event.cost_usd, ms: event.duration_ms, cached: !!event.cached }));
            setPhase("done");
          } else if (event.type === "error") {
            setError(event.message);
            setPhase("error");
          }
        }
        setPhase((p) => (p === "streaming" ? "done" : p));
      } catch (err) {
        if (!controller.signal.aborted) {
          setError(err instanceof Error ? err.message : String(err));
          setPhase("error");
        }
      }
    })();
    return () => controller.abort(); // closing the panel stops the claude process
  }, [reviewId, target.id, run]);

  const html = useMemo(() => renderMarkdown(text), [text]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable */
    }
  };

  return (
    <section className="explain" aria-live="polite" aria-busy={phase === "streaming"}>
      <header className="explain-head">
        <div className="explain-title">
          <span className="explain-kicker">Explained by Claude</span>
          <span className="explain-symbol" title={target.label}>
            {target.label}
          </span>
        </div>
        <button className="btn btn-ghost btn-small btn-icon" onClick={onClose} aria-label="Close explanation" title="Close (stops Claude if it's still writing)">
          ×
        </button>
      </header>

      {phase === "streaming" && !text && (
        <p className="muted small explain-wait">
          <Spinner /> Asking Claude about this change and its callers…
        </p>
      )}
      {text && <div className={`explain-body ${phase === "streaming" ? "is-streaming" : ""}`} dangerouslySetInnerHTML={{ __html: html }} />}
      {error && <p className="error-text small explain-error">{error}</p>}

      <footer className="explain-foot">
        <span className="muted small explain-meta">
          {meta.model ? meta.model.replace(/\[.*\]$/, "") : "Claude Code"}
          {meta.cached ? ", saved answer" : ""}
          {meta.cost != null && !meta.cached ? `, $${meta.cost.toFixed(3)}` : ""}
          {meta.ms != null && !meta.cached ? `, ${(meta.ms / 1000).toFixed(1)}s` : ""}
        </span>
        {phase !== "streaming" && text && (
          <button className="btn btn-small" onClick={copy}>
            {copied ? "Copied" : "Copy"}
          </button>
        )}
        {phase !== "streaming" && (
          <button className="btn btn-small" onClick={() => setRun((r) => r + 1)} title="Ask Claude again">
            Regenerate
          </button>
        )}
      </footer>
    </section>
  );
}
