import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { CATEGORY_LABEL, KIND_LABEL, basename } from "../lib/format";
import { highlightLine } from "../lib/highlight";
import type { FileDetail, GraphEdge, GraphNode, IndexStatus, Review, SourceSlice, SymbolChange, SymbolRisk } from "../lib/types";
import { ChangeBadge, Spinner } from "./common";
import type { ExplainTarget } from "./ExplainPanel";
import type { View } from "./ReviewScreen";

interface Props {
  review: Review;
  view: View;
  detail: FileDetail | null;
  status: IndexStatus | null;
  selectedNode: GraphNode | null;
  risk: Record<string, SymbolRisk> | null;
  changes: Map<string, SymbolChange>;
  scope: { title: string; symbols: string[] } | null;
  onExplain: ((target: ExplainTarget) => void) | null;
  onFocus: (id: string) => void;
  onOpenFile: (path: string, line?: number) => void;
  onSelectNode: (node: GraphNode | null) => void;
}

const EDGE_VERB: Record<string, string> = {
  calls: "calls",
  instantiates: "creates",
  renders: "renders",
  inherits: "inherits",
  references: "uses",
};

export function Inspector(props: Props) {
  const { view, selectedNode } = props;
  if (view === "map" && selectedNode) return <NodeDetail {...props} node={selectedNode} />;
  if (view === "map") return <ChangedSymbols {...props} />;
  return <FileSymbols {...props} />;
}

function FileSymbols({ review, detail, status, risk, onFocus, onOpenFile, onExplain }: Props) {
  const [showBody, setShowBody] = useState(false);
  const symbols = detail?.symbols ?? [];
  const changed = symbols.filter((s) => s.change);
  const unchanged = symbols.filter((s) => !s.change && s.kind !== "module");
  const ready = status?.state === "ready";

  return (
    <div className="insp">
      {review.pr?.body && (
        <section className="insp-section">
          <button className="insp-disclosure" onClick={() => setShowBody((v) => !v)} aria-expanded={showBody}>
            Pull request description
          </button>
          {showBody && <pre className="pr-body">{review.pr.body}</pre>}
        </section>
      )}

      <section className="insp-section">
        <h3 className="insp-title">Changed in this file</h3>
        {!detail && <p className="muted small">Select a file.</p>}
        {detail && !detail.file.analyzable && (
          <p className="muted small">
            prmap maps functions in Python, JavaScript and TypeScript. This {detail.file.language ?? "file"} file only has a
            diff.
          </p>
        )}
        {detail?.file.analyzable && !ready && (
          <p className="muted small">
            <Spinner /> Finding functions…
          </p>
        )}
        {detail?.file.analyzable && ready && changed.length === 0 && (
          <p className="muted small">No function or class bodies changed here (imports or module-level code only).</p>
        )}
        <ul className="sym-list">
          {changed.map((s) => (
            <li key={s.id}>
              <div className="sym-item">
                <button
                  className="sym-main"
                  onClick={() => s.change !== "deleted" && onOpenFile(detail!.file.path, s.start)}
                  title={s.change === "deleted" ? "Removed in this change" : `Jump to line ${s.start}`}
                >
                  <span className={`dot dot-${s.change}`} />
                  <span className={`sym-qual ${s.change === "deleted" ? "struck" : ""}`}>{s.kind === "module" ? "Module-level code" : s.qual}</span>
                  {risk?.[s.id] && risk[s.id].external_callers >= 3 && (
                    <span className="risk-chip" title="Callers in files outside this change">
                      {risk[s.id].external_callers} out
                    </span>
                  )}
                  {risk?.[s.id] && !risk[s.id].tested && detail?.file.category === "source" && s.kind !== "module" && (
                    <span className="risk-chip risk-untested" title="No test calls this within two hops">
                      untested
                    </span>
                  )}
                </button>
                {onExplain && (
                  <button
                    className="btn btn-small btn-ai"
                    onClick={() => onExplain({ id: s.id, label: s.kind === "module" ? "Module-level code" : s.qual })}
                    title="Explain with AI"
                    aria-label={`Explain ${s.qual} with AI`}
                  >
                    AI
                  </button>
                )}
                {s.change !== "deleted" && (
                  <button className="btn btn-small" onClick={() => onFocus(s.id)} title="Show what this touches and what touches it">
                    Map
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      </section>

      {unchanged.length > 0 && (
        <section className="insp-section">
          <h3 className="insp-title">Also in this file</h3>
          <ul className="sym-list sym-list-quiet">
            {unchanged.map((s) => (
              <li key={s.id}>
                <div className="sym-item">
                  <button className="sym-main" onClick={() => onFocus(s.id)} title="Map connections">
                    <span className="dot" />
                    <span className="sym-qual">{s.qual}</span>
                    <span className="muted small">{s.callers} in</span>
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function ChangedSymbols({ review, status, scope, onFocus, onOpenFile }: Props) {
  const [changes, setChanges] = useState<SymbolChange[] | null>(null);
  const ready = status?.state === "ready";
  useEffect(() => {
    if (ready) api.changes(review.id).then((r) => setChanges(r.changes)).catch(() => setChanges([]));
  }, [ready, review.id]);

  const byFile = useMemo(() => {
    const groups = new Map<string, SymbolChange[]>();
    const inScope = scope ? new Set(scope.symbols) : null;
    for (const c of changes ?? []) {
      if (inScope && !inScope.has(c.id)) continue;
      groups.set(c.path, [...(groups.get(c.path) ?? []), c]);
    }
    return groups;
  }, [changes, scope]);

  return (
    <div className="insp">
      <section className="insp-section">
        <h3 className="insp-title">{scope ? `Changed in this group (${scope.symbols.length})` : "Changed symbols"}</h3>
        <p className="muted small">Click a box on the map for its callers, callees and code. Pick one here to center the map on it.</p>
        {!changes && ready && <Spinner />}
        {[...byFile.entries()].map(([path, items]) => (
          <div key={path} className="change-group">
            <button className="change-file" onClick={() => onOpenFile(path)} title="Open diff">
              <span className="swatch" style={{ background: `var(--cat-${items[0].category})` }} />
              <span className="change-file-path">{path}</span>
            </button>
            <ul className="sym-list">
              {items.map((c) => (
                <li key={c.id}>
                  <button className="sym-main" disabled={c.status === "deleted"} onClick={() => onFocus(c.id)}>
                    <span className={`dot dot-${c.status}`} />
                    <span className={`sym-qual ${c.status === "deleted" ? "struck" : ""}`}>{c.kind === "module" ? "Module-level code" : c.qual}</span>
                    <span className="small">
                      {c.additions > 0 && <span className="add">+{c.additions}</span>} {c.deletions > 0 && <span className="del">−{c.deletions}</span>}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </section>
    </div>
  );
}

function NodeDetail({ review, node, risk, changes, onFocus, onOpenFile, onSelectNode, onExplain }: Props & { node: GraphNode }) {
  const [links, setLinks] = useState<{ nodes: GraphNode[]; edges: GraphEdge[] } | null>(null);
  const [source, setSource] = useState<SourceSlice | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLinks(null);
    setSource(null);
    api
      .neighborhood(review.id, {
        seeds: [node.id],
        depth_in: 1,
        depth_out: 1,
        fuzzy: true,
        edge_kinds: ["calls", "instantiates", "renders", "inherits", "references"],
        exclude_categories: [],
        max_nodes: 400,
      })
      .then((g) => !cancelled && setLinks(g))
      .catch(() => !cancelled && setLinks({ nodes: [], edges: [] }));
    const end = node.kind === "module" ? node.line + 40 : Math.min(node.end, node.line + 80);
    api
      .source(review.id, node.path, node.line, end)
      .then((s) => !cancelled && setSource(s))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [review.id, node.id, node.path, node.line, node.end, node.kind]);

  const byId = new Map(links?.nodes.map((n) => [n.id, n]) ?? []);
  const members = new Set(links?.nodes.filter((n) => n.role === "focus").map((n) => n.id) ?? [node.id]);
  const incoming = links?.edges.filter((e) => members.has(e.target) && !members.has(e.source)) ?? [];
  const outgoing = links?.edges.filter((e) => members.has(e.source) && !members.has(e.target)) ?? [];

  const renderLinks = (edges: GraphEdge[], side: "source" | "target") => {
    const seen = new Set<string>();
    return edges
      .filter((e) => !seen.has(e[side]) && seen.add(e[side]))
      .map((e) => {
        const other = byId.get(e[side]);
        if (!other) return null;
        return (
          <li key={e.id}>
            <div className="sym-item">
              <button className="sym-main" onClick={() => onSelectNode(other)} title={`${other.path}:${other.line}`}>
                <span className={`dot ${other.change ? `dot-${other.change}` : ""}`} />
                <span className="sym-qual">{other.kind === "module" ? `${basename(other.path)} module code` : other.label}</span>
                <span className="muted small">
                  {EDGE_VERB[e.kind]}
                  {e.fuzzy ? ", guess" : ""}
                </span>
              </button>
              <button className="btn btn-small btn-icon" onClick={() => onFocus(other.id)} title="Center the map here" aria-label={`Center the map on ${other.label}`}>
                ⌖
              </button>
            </div>
            <div className="sym-path muted small">
              {other.path}:{other.line}
            </div>
          </li>
        );
      });
  };

  return (
    <div className="insp">
      <section className="insp-section">
        <div className="node-kind muted small">
          {KIND_LABEL[node.kind] ?? node.kind} in <span className="swatch" style={{ background: `var(--cat-${node.category})` }} /> {CATEGORY_LABEL[node.category]}
        </div>
        <h3 className="node-name">{node.kind === "module" ? `${basename(node.path)} module code` : node.label}</h3>
        <div className="node-path">
          {node.in_pr ? (
            <button className="btn btn-link" onClick={() => onOpenFile(node.path, node.line)}>
              {node.path}:{node.line}
            </button>
          ) : (
            <span className="muted">
              {node.path}:{node.line}
            </span>
          )}
          <ChangeBadge change={node.change} />
          {!node.in_pr && <span className="tag">not in this change</span>}
        </div>
        <RiskLine risk={risk?.[node.id]} category={node.category} kind={node.kind} labelOf={(id) => changes.get(id)?.qual ?? id.split("::").pop()!} />
        <div className="row gap-s">
          <button className="btn btn-small btn-primary" onClick={() => onFocus(node.id)}>
            Center map here
          </button>
          {node.in_pr && (
            <button className="btn btn-small" onClick={() => onOpenFile(node.path, node.line)}>
              Open diff
            </button>
          )}
          {onExplain && (
            <button
              className="btn btn-small btn-ai-solid"
              onClick={() => onExplain({ id: node.id, label: node.kind === "module" ? `${basename(node.path)} module code` : node.label })}
              title="Ask Claude (through your local Claude Code) to explain this symbol and its impact"
            >
              Explain with AI
            </button>
          )}
        </div>
      </section>

      <section className="insp-section">
        <h3 className="insp-title">Touched by {links ? `(${new Set(incoming.map((e) => e.source)).size})` : ""}</h3>
        {!links && <Spinner />}
        {links && incoming.length === 0 && <p className="muted small">Nothing in the repo calls or uses this.</p>}
        <ul className="sym-list">{renderLinks(incoming, "source")}</ul>
      </section>

      <section className="insp-section">
        <h3 className="insp-title">Touches {links ? `(${new Set(outgoing.map((e) => e.target)).size})` : ""}</h3>
        {links && outgoing.length === 0 && <p className="muted small">Doesn't call anything else in the repo.</p>}
        <ul className="sym-list">{renderLinks(outgoing, "target")}</ul>
      </section>

      {source && (
        <section className="insp-section">
          <h3 className="insp-title">Code</h3>
          <pre className="snippet">
            {source.lines.map((line, i) => (
              <div key={i} className="snippet-line">
                <span className="snippet-no">{source.start + i}</span>
                <code dangerouslySetInnerHTML={{ __html: highlightLine(line, node.language) || " " }} />
              </div>
            ))}
            {node.end > source.end && <div className="snippet-more muted">… {node.end - source.end} more lines</div>}
          </pre>
        </section>
      )}
    </div>
  );
}

function RiskLine({
  risk,
  category,
  kind,
  labelOf,
}: {
  risk: SymbolRisk | undefined;
  category: string;
  kind: string;
  labelOf: (id: string) => string;
}) {
  if (!risk) return null;
  return (
    <ul className="risk-line">
      <li>
        <strong>{risk.callers}</strong> caller{risk.callers === 1 ? "" : "s"}
        {risk.external_callers > 0 && (
          <>
            , <strong className={risk.external_callers >= 3 ? "warn-text" : ""}>{risk.external_callers}</strong> outside this change
          </>
        )}
      </li>
      {category === "source" && kind !== "module" && (
        <li>
          {risk.tested ? (
            <>Reached by {risk.test_callers.slice(0, 2).map(labelOf).join(", ")}{risk.test_callers.length > 2 ? " and more" : ""}</>
          ) : (
            <span className="warn-text">No test reaches this</span>
          )}
        </li>
      )}
    </ul>
  );
}
