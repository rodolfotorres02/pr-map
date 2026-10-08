import { useState } from "react";
import { CATEGORY_LABEL, basename } from "../lib/format";
import type { Category, ChangedFile, PlanCluster, ReviewPlan, SymbolChange, SymbolRisk } from "../lib/types";
import { Spinner } from "./common";
import type { ExplainTarget } from "./ExplainPanel";

interface Props {
  plan: ReviewPlan | null;
  error: string | null;
  indexReady: boolean;
  changes: Map<string, SymbolChange>;
  files: ChangedFile[];
  reviewed: Set<string>;
  activeCluster: string | null;
  onActivate: (id: string | null) => void;
  onOpenFile: (path: string, line?: number) => void;
  onFocusSymbol: (id: string) => void;
  onExplain: ((target: ExplainTarget) => void) | null;
  onMapCluster: (id: string) => void;
}

const WIDE_REACH_MIN = 3;

export function PlanPanel(props: Props) {
  const { plan, error, indexReady, changes, files, reviewed, activeCluster, onActivate, onOpenFile } = props;
  const [showUnmapped, setShowUnmapped] = useState(false);

  if (!indexReady) {
    return (
      <div className="plan-empty muted">
        <Spinner /> Mapping the code base to plan this review…
      </div>
    );
  }
  if (error) return <div className="plan-empty error-text">{error}</div>;
  if (!plan) {
    return (
      <div className="plan-empty muted">
        <Spinner /> Grouping changes…
      </div>
    );
  }

  const fileByPath = new Map(files.map((f) => [f.path, f]));
  const reviewedIn = (paths: string[]) => paths.filter((p) => reviewed.has(p)).length;
  const label = (id: string) => {
    const c = changes.get(id);
    if (!c) return id.split("::").pop() ?? id;
    return c.kind === "module" ? `${basename(c.path)} module code` : c.qual;
  };

  const wideReach = Object.entries(plan.risk)
    .filter(([id, r]) => r.external_callers >= WIDE_REACH_MIN && changes.has(id))
    .sort((a, b) => b[1].external_callers - a[1].external_callers)
    .slice(0, 6);
  const untested = Object.entries(plan.risk).filter(([id, r]) => {
    const c = changes.get(id);
    return !r.tested && c && c.category === "source" && c.kind !== "module" && c.status !== "deleted";
  });

  return (
    <div className="plan">
      <p className="plan-summary">
        {plan.clusters.length === 0
          ? "No connected code changes to group. Use the file list below."
          : `${plan.clusters.length} group${plan.clusters.length === 1 ? "" : "s"} of connected changes. Work through them in order: each starts at its entry points, then follows what they call.`}
      </p>

      {(wideReach.length > 0 || untested.length > 0 || plan.deleted_still_referenced.length > 0) && (
        <section className="attention" aria-label="Needs attention">
          <h3 className="plan-heading">Needs attention</h3>
          {plan.deleted_still_referenced.length > 0 && (
            <AttentionGroup title="Removed but still referenced" count={plan.deleted_still_referenced.length} tone="danger">
              {plan.deleted_still_referenced.map((d) => (
                <li key={d.id}>
                  <span className="attn-name struck">{d.name}</span>
                  {d.references.slice(0, 3).map((r) => (
                    <button key={`${r.path}:${r.line}`} className="btn btn-link small attn-ref" onClick={() => onOpenFile(r.path, r.line)}>
                      {basename(r.path)}:{r.line}
                    </button>
                  ))}
                </li>
              ))}
            </AttentionGroup>
          )}
          {wideReach.length > 0 && (
            <AttentionGroup title="Wide reach beyond this change" count={wideReach.length} tone="warn">
              {wideReach.map(([id, r]) => (
                <li key={id}>
                  <button className="attn-row" onClick={() => props.onFocusSymbol(id)} title="Map who depends on this">
                    <span className="attn-name">{label(id)}</span>
                    <span className="attn-meta">{r.external_callers} callers outside</span>
                  </button>
                </li>
              ))}
            </AttentionGroup>
          )}
          {untested.length > 0 && (
            <AttentionGroup title="No test reaches these" count={untested.length} tone="warn" collapsed>
              {untested.slice(0, 40).map(([id]) => {
                const c = changes.get(id)!;
                return (
                  <li key={id}>
                    <button className="attn-row" onClick={() => onOpenFile(c.path, c.start)} title={`${c.path}:${c.start}`}>
                      <span className={`dot dot-${c.status}`} />
                      <span className="attn-name">{label(id)}</span>
                    </button>
                  </li>
                );
              })}
            </AttentionGroup>
          )}
        </section>
      )}

      {plan.clusters.length > 0 && (
        <ol className="clusters" aria-label="Change groups in reading order">
          {plan.clusters.map((cluster, i) => (
            <ClusterRow
              key={cluster.id}
              index={i + 1}
              cluster={cluster}
              active={activeCluster === cluster.id}
              reviewedCount={reviewedIn(cluster.files)}
              label={label}
              risk={plan.risk}
              changes={changes}
              onToggle={() => onActivate(activeCluster === cluster.id ? null : cluster.id)}
              onOpenFile={onOpenFile}
              onFocusSymbol={props.onFocusSymbol}
              onExplain={props.onExplain}
              onMapCluster={props.onMapCluster}
            />
          ))}
        </ol>
      )}

      {plan.unmapped_files.length > 0 && (
        <section className="unmapped">
          <button className="insp-disclosure" onClick={() => setShowUnmapped((v) => !v)} aria-expanded={showUnmapped}>
            Outside the groups ({plan.unmapped_files.length} files, {reviewedIn(plan.unmapped_files)} reviewed)
          </button>
          <p className="muted small">Templates, config, docs and edits without function bodies.</p>
          {showUnmapped && (
            <ul className="unmapped-list">
              {plan.unmapped_files.map((path) => {
                const f = fileByPath.get(path);
                return (
                  <li key={path}>
                    <button className={`attn-row ${reviewed.has(path) ? "is-reviewed" : ""}`} onClick={() => onOpenFile(path)} title={path}>
                      {f && <span className="swatch" style={{ background: `var(--cat-${f.category})` }} />}
                      <span className="attn-name mono">{path}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      )}
    </div>
  );
}

function AttentionGroup({
  title,
  count,
  tone,
  collapsed = false,
  children,
}: {
  title: string;
  count: number;
  tone: "warn" | "danger";
  collapsed?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(!collapsed);
  return (
    <div className={`attn attn-${tone}`}>
      <button className="attn-head" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <span>{title}</span>
        <span className="attn-count">{count}</span>
      </button>
      {open && <ul className="attn-list">{children}</ul>}
    </div>
  );
}

function RoleBar({ categories }: { categories: Partial<Record<Category, number>> }) {
  const entries = Object.entries(categories).filter(([, n]) => n && n > 0) as [Category, number][];
  const total = entries.reduce((sum, [, n]) => sum + n, 0) || 1;
  return (
    <span className="role-bar" title={entries.map(([c, n]) => `${CATEGORY_LABEL[c]}: ${n}`).join("\n")}>
      {entries.map(([c, n]) => (
        <span key={c} style={{ flexGrow: n / total, background: `var(--cat-${c})` }} />
      ))}
    </span>
  );
}

function ClusterRow({
  index,
  cluster,
  active,
  reviewedCount,
  label,
  risk,
  changes,
  onToggle,
  onOpenFile,
  onFocusSymbol,
  onExplain,
  onMapCluster,
}: {
  index: number;
  cluster: PlanCluster;
  active: boolean;
  reviewedCount: number;
  label: (id: string) => string;
  risk: Record<string, SymbolRisk>;
  changes: Map<string, SymbolChange>;
  onToggle: () => void;
  onOpenFile: (path: string, line?: number) => void;
  onFocusSymbol: (id: string) => void;
  onExplain: ((target: ExplainTarget) => void) | null;
  onMapCluster: (id: string) => void;
}) {
  const done = reviewedCount === cluster.files.length;
  const entry = new Set(cluster.entry_points);
  return (
    <li className={`cluster ${active ? "active" : ""} ${done ? "done" : ""}`}>
      <button className="cluster-head" onClick={onToggle} aria-expanded={active}>
        <span className="cluster-no">{index}</span>
        <span className="cluster-main">
          <span className="cluster-title">{cluster.title}</span>
          <span className="cluster-meta">
            {cluster.symbols.length} symbol{cluster.symbols.length === 1 ? "" : "s"} in {cluster.files.length} file
            {cluster.files.length === 1 ? "" : "s"}
            <span className="add"> +{cluster.additions}</span> <span className="del">−{cluster.deletions}</span>
          </span>
          <span className="cluster-progress">
            <RoleBar categories={cluster.categories} />
            <span className="muted small">
              {reviewedCount}/{cluster.files.length} reviewed
            </span>
          </span>
        </span>
      </button>
      {active && (
        <div className="cluster-body">
          <div className="row gap-s">
            <button className="btn btn-small btn-primary" onClick={() => onMapCluster(cluster.id)}>
              Map this group
            </button>
            <span className="muted small">The file list is scoped to this group.</span>
          </div>
          <ol className="cluster-symbols">
            {cluster.symbols.map((id) => {
              const c = changes.get(id);
              const r = risk[id];
              return (
                <li key={id} className="plan-sym">
                  <button className="plan-sym-name" onClick={() => c && onOpenFile(c.path, c.start)} title={c ? `${c.path}:${c.start}` : id}>
                    <span className={`dot ${c ? `dot-${c.status}` : ""}`} />
                    <span className="sym-qual">
                      {entry.has(id) && <span className="entry-mark" title="Entry point: start reading here">▸ </span>}
                      {label(id)}
                    </span>
                  </button>
                  <div className="plan-sym-meta">
                    <span className="plan-sym-path" title={c?.path}>
                      {c ? basename(c.path) : ""}
                    </span>
                    {r && r.external_callers >= WIDE_REACH_MIN && (
                      <span className="risk-chip" title={`${r.external_callers} callers in files outside this change`}>
                        {r.external_callers} out
                      </span>
                    )}
                    {r && !r.tested && c?.category === "source" && c.kind !== "module" && (
                      <span className="risk-chip risk-untested" title="No test calls this within two hops">
                        untested
                      </span>
                    )}
                    {onExplain && (
                      <button
                        className="btn btn-small btn-ai plan-sym-action"
                        onClick={() => onExplain({ id, label: label(id) })}
                        title="Explain with AI"
                        aria-label={`Explain ${label(id)} with AI`}
                      >
                        AI
                      </button>
                    )}
                    <button className="btn btn-small btn-icon plan-sym-action" onClick={() => onFocusSymbol(id)} title="Map its connections" aria-label={`Map ${label(id)}`}>
                      ⌖
                    </button>
                  </div>
                </li>
              );
            })}
          </ol>
        </div>
      )}
    </li>
  );
}
