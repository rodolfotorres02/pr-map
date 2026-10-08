import { Fragment, useMemo } from "react";
import { CATEGORY_LABEL, KIND_LABEL, basename, dirname } from "../lib/format";
import { highlightLine } from "../lib/highlight";
import type { FileDetail, FileSymbol, Hunk } from "../lib/types";
import { ChangeBadge, Spinner } from "./common";
import type { ExplainTarget } from "./ExplainPanel";

interface Props {
  detail: FileDetail | null;
  error: string | null;
  reviewed: boolean;
  onToggleReviewed: () => void;
  fullFile: boolean;
  onFullFile: (full: boolean) => void;
  onFocusSymbol: (id: string) => void;
  onExplain: ((target: ExplainTarget) => void) | null;
  indexReady: boolean;
}

export function DiffView({ detail, error, reviewed, onToggleReviewed, fullFile, onFullFile, onFocusSymbol, onExplain, indexReady }: Props) {
  if (error) return <div className="stage-message error-text">{error}</div>;
  if (!detail) {
    return (
      <div className="stage-message">
        <Spinner /> Loading diff…
      </div>
    );
  }
  const { file } = detail;
  const dir = dirname(file.path);

  return (
    <div className="diff">
      <header className="diff-head">
        <div className="diff-title">
          <h2 className="diff-path" title={file.path}>
            {dir && <span className="muted">{dir}/</span>}
            {basename(file.path)}
          </h2>
          <div className="diff-meta">
            <span className={`status-word status-${file.status}`}>{file.status}</span>
            {file.old_path && (
              <span className="muted">
                from <code>{file.old_path}</code>
              </span>
            )}
            <span className="cat-label">
              <span className="swatch" style={{ background: `var(--cat-${file.category})` }} />
              {CATEGORY_LABEL[file.category]}
            </span>
            {!file.binary && (
              <span>
                <span className="add">+{file.additions}</span> <span className="del">−{file.deletions}</span>
              </span>
            )}
          </div>
        </div>
        <div className="diff-actions">
          <div className="segmented segmented-small" role="group" aria-label="Context">
            <button className={!fullFile ? "on" : ""} onClick={() => onFullFile(false)}>
              Changes
            </button>
            <button className={fullFile ? "on" : ""} onClick={() => onFullFile(true)}>
              Full file
            </button>
          </div>
          <label className="check check-small reviewed-toggle">
            <input type="checkbox" checked={reviewed} onChange={onToggleReviewed} />
            Reviewed
          </label>
        </div>
      </header>

      {file.analyzable && !indexReady && (
        <div className="notice notice-quiet">
          <Spinner /> Mapping the code base. Function markers and connection maps appear when it's done.
        </div>
      )}

      {detail.diff.binary || file.binary ? (
        <div className="stage-message muted">Binary file. Nothing to show line by line.</div>
      ) : detail.diff.hunks.length === 0 ? (
        <div className="stage-message muted">
          {file.status === "renamed" ? "Renamed without content changes." : "No textual changes."}
        </div>
      ) : (
        <div className="hunks">
          {detail.diff.hunks.map((hunk, i) => (
            <HunkView
              key={`${hunk.header}-${i}`}
              hunk={hunk}
              language={file.language}
              symbols={detail.symbols}
              onFocusSymbol={onFocusSymbol}
              onExplain={onExplain}
              showHeader={!fullFile || detail.diff.hunks.length > 1}
            />
          ))}
          {detail.truncated && <div className="stage-message muted">Diff truncated: this file has too many changed lines to show.</div>}
        </div>
      )}
    </div>
  );
}

function HunkView({
  hunk,
  language,
  symbols,
  onFocusSymbol,
  onExplain,
  showHeader,
}: {
  hunk: Hunk;
  language: string | null;
  symbols: FileSymbol[];
  onFocusSymbol: (id: string) => void;
  onExplain: ((target: ExplainTarget) => void) | null;
  showHeader: boolean;
}) {
  const newEnd = hunk.new_start + Math.max(hunk.new_lines, 1) - 1;
  const touched = symbols.filter(
    (s) => s.change && s.change !== "deleted" && s.kind !== "module" && s.start <= newEnd && s.end >= hunk.new_start,
  );
  // Innermost-first so a changed method is listed before its class.
  touched.sort((a, b) => a.end - a.start - (b.end - b.start));

  const starts = useMemo(() => {
    const map = new Map<number, FileSymbol[]>();
    for (const s of symbols) {
      if (s.kind === "module" || s.change === "deleted") continue;
      map.set(s.start, [...(map.get(s.start) ?? []), s]);
    }
    return map;
  }, [symbols]);

  const rendered = useMemo(() => hunk.lines.map((l) => highlightLine(l.text, language)), [hunk, language]);

  return (
    <section className="hunk">
      {showHeader && (
        <div className="hunk-head">
          <code className="hunk-range">
            −{hunk.old_start},{hunk.old_lines} +{hunk.new_start},{hunk.new_lines}
          </code>
          {hunk.section && <code className="hunk-section">{hunk.section}</code>}
          <span className="hunk-symbols">
            {touched.slice(0, 4).map((s) => (
              <button key={s.id} className="sym-chip" onClick={() => onFocusSymbol(s.id)} title="Show what this touches and what touches it">
                <span className={`dot dot-${s.change}`} />
                {s.qual}
              </button>
            ))}
          </span>
        </div>
      )}
      <table className="code">
        <tbody>
          {hunk.lines.map((line, i) => {
            const markers = line.new !== null && line.type !== "del" ? starts.get(line.new) : undefined;
            return (
              <Fragment key={i}>
                {markers?.map((s) => <SymbolMarker key={s.id} symbol={s} onFocus={onFocusSymbol} onExplain={onExplain} />)}
                <tr className={`ln ln-${line.type}`} id={line.new !== null ? `line-${line.new}` : undefined}>
                  <td className="ln-no">{line.old ?? ""}</td>
                  <td className="ln-no">{line.new ?? ""}</td>
                  <td className="ln-sign">{line.type === "add" ? "+" : line.type === "del" ? "−" : ""}</td>
                  <td className="ln-code">
                    <code dangerouslySetInnerHTML={{ __html: rendered[i] || " " }} />
                  </td>
                </tr>
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

function SymbolMarker({
  symbol,
  onFocus,
  onExplain,
}: {
  symbol: FileSymbol;
  onFocus: (id: string) => void;
  onExplain: ((target: ExplainTarget) => void) | null;
}) {
  return (
    <tr className={`sym-row ${symbol.change ? `sym-${symbol.change}` : ""}`}>
      <td colSpan={3} />
      <td>
        <div className="sym-marker">
          <span className="sym-kind">{KIND_LABEL[symbol.kind] ?? symbol.kind}</span>
          <span className="sym-name">{symbol.qual}</span>
          <ChangeBadge change={symbol.change} />
          <span className="muted small">
            {symbol.callers} caller{symbol.callers === 1 ? "" : "s"}, {symbol.callees} callee{symbol.callees === 1 ? "" : "s"}
          </span>
          <button className="btn btn-link small" onClick={() => onFocus(symbol.id)}>
            Map connections
          </button>
          {onExplain && (
            <button
              className="btn btn-link small btn-ai"
              onClick={() => onExplain({ id: symbol.id, label: symbol.qual })}
              title="Ask Claude (through your local Claude Code) to explain this change and its impact"
            >
              Explain with AI
            </button>
          )}
        </div>
      </td>
    </tr>
  );
}
