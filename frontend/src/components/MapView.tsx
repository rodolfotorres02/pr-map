import cytoscape, { type Core, type ElementDefinition, type StylesheetJson } from "cytoscape";
import dagre from "cytoscape-dagre";
import fcose from "cytoscape-fcose";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import type { FileFilter } from "../lib/filters";
import { CATEGORY_LABEL, basename } from "../lib/format";
import { cssVar } from "../lib/theme";
import type { Category, EdgeKind, Graph, GraphNode, IndexStatus, Review } from "../lib/types";
import { Spinner } from "./common";

cytoscape.use(dagre);
cytoscape.use(fcose);

export interface MapSettings {
  mode: "overview" | "focus";
  seeds: string[];
  depthIn: number;
  depthOut: number;
  fuzzy: boolean;
  kinds: EdgeKind[];
  followFilter: boolean;
  neighbors: boolean;
}

interface Props {
  review: Review;
  settings: MapSettings;
  onSettings: (update: (s: MapSettings) => MapSettings) => void;
  filter: FileFilter;
  visiblePaths: string[];
  clusterSymbols: string[] | null;
  status: IndexStatus | null;
  selected: GraphNode | null;
  onSelect: (node: GraphNode | null) => void;
  onFocus: (id: string) => void;
  onOverview: () => void;
  theme: "light" | "dark";
}

const ALL_CATEGORIES: Category[] = [
  "source", "test", "template", "style", "config", "migration", "docs", "generated", "asset", "other",
];

const EDGE_TOGGLES: { kind: EdgeKind; label: string; hint: string }[] = [
  { kind: "calls", label: "Calls", hint: "Function and method calls" },
  { kind: "instantiates", label: "Creates", hint: "Class instantiation (Foo(), new Foo())" },
  { kind: "renders", label: "Renders", hint: "JSX components (<Foo />)" },
  { kind: "inherits", label: "Inherits", hint: "Subclass and implements" },
  { kind: "references", label: "Uses", hint: "Passed or referenced without a call: callbacks, url routes, type annotations" },
];

function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(value), ms);
    return () => window.clearTimeout(t);
  }, [value, ms]);
  return debounced;
}

function graphStyle(mode: MapSettings["mode"]): StylesheetJson {
  const v = (name: string) => cssVar(name);
  const cat = (c: string) => v(`--cat-${c}`) || v("--cat-other");
  const tint: Record<string, string> = {
    added: v("--added-tint"),
    modified: v("--modified-tint"),
  };
  return [
    {
      selector: "node",
      style: {
        shape: "round-rectangle",
        width: "data(w)",
        height: "data(h)",
        "background-color": v("--canvas"),
        "border-width": 2.5,
        "border-color": (e: cytoscape.NodeSingular) => cat(e.data("category")),
        label: "data(label)",
        "text-wrap": "wrap",
        "text-valign": "center",
        "text-halign": "center",
        "font-family": "Overpass, system-ui, sans-serif",
        "font-size": 11,
        "line-height": 1.3,
        color: v("--ink"),
        "overlay-opacity": 0,
      },
    },
    {
      selector: "node[change]",
      style: {
        "background-color": (e: cytoscape.NodeSingular) => tint[e.data("change")] ?? v("--canvas"),
        "border-width": 3.5,
        "font-weight": 600,
      },
    },
    { selector: "node[!inPr]", style: { "border-style": "dashed", color: v("--muted") } },
    { selector: 'node[kind = "class"], node[kind = "interface"], node[kind = "object"]', style: { shape: "rectangle", "border-style": "double", "border-width": 5 } },
    { selector: 'node[kind = "module"]', style: { shape: "barrel" } },
    {
      selector: 'node[role = "focus"]',
      style: { "outline-width": 3, "outline-color": v("--ink"), "outline-offset": 3, "font-weight": 800 },
    },
    {
      selector: "node:parent",
      style: {
        shape: "round-rectangle",
        "background-color": v("--zone"),
        "background-opacity": 1,
        "border-width": 1,
        "border-color": v("--rule"),
        "border-style": "solid",
        label: "data(label)",
        "text-valign": "top",
        "text-halign": "center",
        "text-margin-y": -4,
        "font-family": "'Overpass Mono', ui-monospace, monospace",
        "font-size": 10,
        "font-weight": 400,
        color: v("--muted"),
        padding: "14px",
      },
    },
    {
      selector: "edge",
      style: {
        width: 2,
        "line-color": v("--edge"),
        "target-arrow-color": v("--edge"),
        "target-arrow-shape": "triangle",
        "arrow-scale": 0.85,
        "curve-style": mode === "focus" ? "taxi" : "bezier",
        "taxi-direction": "rightward",
        "taxi-turn": "50%",
        "taxi-radius": 10,
        "control-point-step-size": 30,
      } as cytoscape.Css.Edge,
    },
    { selector: 'edge[kind = "instantiates"]', style: { "target-arrow-shape": "triangle-backcurve" } },
    { selector: 'edge[kind = "renders"]', style: { "target-arrow-shape": "chevron" } },
    {
      selector: 'edge[kind = "inherits"]',
      style: { "target-arrow-fill": "hollow", "line-color": v("--ink-2"), "target-arrow-color": v("--ink-2"), "arrow-scale": 1.2 },
    },
    {
      selector: 'edge[kind = "references"]',
      style: { "line-style": "dashed", "line-dash-pattern": [6, 4], width: 1.5, "line-color": v("--edge-soft"), "target-arrow-color": v("--edge-soft") },
    },
    { selector: "edge[?fuzzy]", style: { "line-style": "dotted", "line-dash-pattern": [2, 4], opacity: 0.8 } },
    { selector: ".faded", style: { opacity: 0.14 } },
    { selector: "edge.lit", style: { width: 3, "line-color": v("--ink"), "target-arrow-color": v("--ink"), "z-index": 20 } },
    { selector: "node:selected", style: { "outline-width": 3, "outline-color": v("--focus"), "outline-offset": 3 } },
  ];
}

function nodeSize(label: string): { w: number; h: number } {
  const lines = label.split("\n");
  const w = Math.max(...lines.map((l, i) => l.length * (i === 0 ? 7.1 : 6.1))) + 26;
  return { w: Math.min(Math.max(w, 64), 320), h: lines.length > 1 ? 40 : 28 };
}

function toElements(graph: Graph, mode: MapSettings["mode"]): ElementDefinition[] {
  const elements: ElementDefinition[] = [];
  if (mode === "overview") {
    for (const path of new Set(graph.nodes.map((n) => n.path))) {
      elements.push({ data: { id: `file:${path}`, label: path, isFile: true } });
    }
  }
  for (const n of graph.nodes) {
    const name = n.kind === "module" ? `${basename(n.path)} module code` : n.label;
    const label = mode === "overview" ? name : `${name}\n${basename(n.path)}:${n.line}`;
    elements.push({
      data: {
        id: n.id,
        parent: mode === "overview" ? `file:${n.path}` : undefined,
        label,
        ...nodeSize(label),
        category: n.category,
        change: n.change ?? undefined,
        inPr: n.in_pr,
        kind: n.kind,
        role: n.role,
      },
    });
  }
  for (const e of graph.edges) {
    elements.push({ data: { id: e.id, source: e.source, target: e.target, kind: e.kind, fuzzy: e.fuzzy } });
  }
  return elements;
}

function runLayout(cy: Core, mode: MapSettings["mode"]) {
  const big = cy.nodes().length > 250;
  const options =
    mode === "focus"
      ? { name: "dagre", rankDir: "LR", nodeSep: 16, rankSep: 90, edgeSep: 8, ranker: "network-simplex", fit: true, padding: 36, animate: false }
      : {
          name: "fcose",
          quality: big ? "default" : "proof",
          randomize: true,
          animate: false,
          fit: true,
          padding: 36,
          nodeSeparation: 70,
          idealEdgeLength: 110,
          nodeRepulsion: 9000,
          nestingFactor: 0.6,
          gravity: 0.3,
          packComponents: true,
          tile: true,
        };
  cy.layout(options as cytoscape.LayoutOptions).run();
}

export function MapView({ review, settings, onSettings, filter, visiblePaths, clusterSymbols, status, selected, onSelect, onFocus, onOverview, theme }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const nodesRef = useRef<Map<string, GraphNode>>(new Map());
  const handlersRef = useRef({ onSelect, onFocus });
  handlersRef.current = { onSelect, onFocus };

  const [graph, setGraph] = useState<Graph | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ready = status?.state === "ready";

  // Compare by content: the parent recreates the array on every render.
  const pathsKey = useDebounced(visiblePaths.join("\n"), 250);
  const paths = useMemo(() => (pathsKey ? pathsKey.split("\n") : []), [pathsKey]);
  const clusterKey = clusterSymbols ? clusterSymbols.join("\n") : null;
  const excludeCategories = useMemo(
    () => (settings.followFilter && filter.categories ? ALL_CATEGORIES.filter((c) => !filter.categories!.includes(c)) : []),
    [settings.followFilter, filter.categories],
  );

  // Fetch the graph.
  useEffect(() => {
    if (!ready) return;
    if (settings.mode === "focus" && !settings.seeds.length) {
      setGraph(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    const options = { fuzzy: settings.fuzzy, edge_kinds: settings.kinds, exclude_categories: excludeCategories };
    const request =
      settings.mode === "focus"
        ? api.neighborhood(review.id, { ...options, seeds: settings.seeds, depth_in: settings.depthIn, depth_out: settings.depthOut })
        : api.overview(review.id, {
            ...options,
            paths: settings.followFilter ? paths : null,
            neighbors: settings.neighbors,
            symbols: clusterKey ? clusterKey.split("\n") : null,
          });
    request
      .then((g) => {
        if (cancelled) return;
        nodesRef.current = new Map(g.nodes.map((n) => [n.id, n]));
        setGraph(g);
        // Centering on a symbol opens its details straight away.
        if (settings.mode === "focus" && settings.seeds.length === 1) {
          handlersRef.current.onSelect(nodesRef.current.get(settings.seeds[0]) ?? null);
        }
      })
      .catch((err) => !cancelled && setError(String(err.message ?? err)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [ready, review.id, settings, excludeCategories, paths, clusterKey]);

  // Create the canvas once.
  useEffect(() => {
    if (!containerRef.current) return;
    const cy = cytoscape({
      container: containerRef.current,
      wheelSensitivity: 0.25,
      minZoom: 0.08,
      maxZoom: 3,
      boxSelectionEnabled: false,
    });
    cyRef.current = cy;

    const clearHighlight = () => {
      cy.elements().removeClass("faded lit");
    };
    const highlight = (id: string) => {
      const node = cy.$id(id);
      if (node.empty()) return;
      const hood = node.closedNeighborhood().union(node.ancestors());
      const keep = hood.union(hood.nodes().ancestors());
      cy.elements().not(keep).addClass("faded");
      node.connectedEdges().addClass("lit");
    };

    cy.on("tap", "node", (evt) => {
      const node = evt.target;
      if (node.data("isFile")) return;
      clearHighlight();
      highlight(node.id());
      handlersRef.current.onSelect(nodesRef.current.get(node.id()) ?? null);
    });
    cy.on("dbltap", "node", (evt) => {
      if (!evt.target.data("isFile")) handlersRef.current.onFocus(evt.target.id());
    });
    cy.on("tap", (evt) => {
      if (evt.target === cy) {
        clearHighlight();
        cy.$(":selected").unselect();
        handlersRef.current.onSelect(null);
      }
    });
    // Keep the canvas in sync with its box (pane resizes, inspector reflow).
    let fitTimer: number | undefined;
    const observer = new ResizeObserver(() => {
      cy.resize();
      window.clearTimeout(fitTimer);
      fitTimer = window.setTimeout(() => cy.fit(undefined, 36), 120);
    });
    observer.observe(containerRef.current);

    return () => {
      observer.disconnect();
      window.clearTimeout(fitTimer);
      cy.destroy();
      cyRef.current = null;
    };
  }, []);

  // Render graph data.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.batch(() => {
      cy.elements().remove();
      if (graph) cy.add(toElements(graph, settings.mode));
    });
    cy.style(graphStyle(settings.mode));
    if (graph?.nodes.length) runLayout(cy, settings.mode);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph]);

  // Re-theme without relayout.
  useEffect(() => {
    cyRef.current?.style(graphStyle(settings.mode));
  }, [theme, settings.mode]);

  // Reflect selection made elsewhere (inspector lists).
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.$(":selected").unselect();
    cy.elements().removeClass("faded lit");
    if (!selected) return;
    const node = cy.$id(selected.id);
    if (node.nonempty()) {
      node.select();
      const hood = node.closedNeighborhood();
      cy.elements().not(hood.union(hood.nodes().ancestors())).addClass("faded");
      node.connectedEdges().addClass("lit");
      if (!node.renderedBoundingBox() || !cy.extent()) return;
      const box = cy.extent();
      const p = node.position();
      if (p.x < box.x1 || p.x > box.x2 || p.y < box.y1 || p.y > box.y2) cy.animate({ center: { eles: node } }, { duration: 250 });
    }
  }, [selected]);

  const focusNode = settings.seeds.length ? nodesRef.current.get(settings.seeds[0]) : undefined;
  const categoriesInGraph = useMemo(() => [...new Set(graph?.nodes.map((n) => n.category) ?? [])], [graph]);
  const changeKinds = useMemo(() => new Set(graph?.nodes.map((n) => n.change).filter(Boolean)), [graph]);

  const set = (patch: Partial<MapSettings>) => onSettings((s) => ({ ...s, ...patch }));
  const toggleKind = (kind: EdgeKind) =>
    set({ kinds: settings.kinds.includes(kind) ? settings.kinds.filter((k) => k !== kind) : [...settings.kinds, kind] });

  return (
    <div className="map">
      <div className="map-toolbar">
        <div className="segmented segmented-small" role="tablist" aria-label="Map mode">
          <button role="tab" aria-selected={settings.mode === "overview"} className={settings.mode === "overview" ? "on" : ""} onClick={onOverview}>
            {clusterSymbols ? "This group" : "Whole change"}
          </button>
          <button
            role="tab"
            aria-selected={settings.mode === "focus"}
            className={settings.mode === "focus" ? "on" : ""}
            disabled={!settings.seeds.length}
            onClick={() => set({ mode: "focus" })}
            title={settings.seeds.length ? undefined : "Pick a function or class first"}
          >
            {focusNode ? `Around ${focusNode.label}` : "Around a symbol"}
          </button>
        </div>

        {settings.mode === "focus" ? (
          <>
            <Stepper label="Callers" hint="How many levels of code that calls into it" value={settings.depthIn} onChange={(depthIn) => set({ depthIn })} />
            <Stepper label="Callees" hint="How many levels of code it calls" value={settings.depthOut} onChange={(depthOut) => set({ depthOut })} />
          </>
        ) : (
          <label className="check check-small" title="Also show code outside the change that calls or is called by changed code">
            <input type="checkbox" checked={settings.neighbors} onChange={(e) => set({ neighbors: e.target.checked })} />
            Direct neighbors
          </label>
        )}

        <div className="toggle-group" role="group" aria-label="Link types">
          {EDGE_TOGGLES.map((t) => (
            <button
              key={t.kind}
              className={`toggle ${settings.kinds.includes(t.kind) ? "on" : ""}`}
              aria-pressed={settings.kinds.includes(t.kind)}
              title={t.hint}
              onClick={() => toggleKind(t.kind)}
            >
              {t.label}
            </button>
          ))}
        </div>
        <label className="check check-small" title="Include links matched only by method name (dotted lines)">
          <input type="checkbox" checked={settings.fuzzy} onChange={(e) => set({ fuzzy: e.target.checked })} />
          Guessed links
        </label>
        <label className="check check-small" title="Apply the sidebar file filter to the map">
          <input type="checkbox" checked={settings.followFilter} onChange={(e) => set({ followFilter: e.target.checked })} />
          Use file filter
        </label>
        <span className="grow" />
        {loading && <Spinner />}
        <button className="btn btn-small" onClick={() => cyRef.current?.fit(undefined, 36)} title="Fit the map to the window">
          Fit
        </button>
        <button className="btn btn-small" onClick={() => cyRef.current && runLayout(cyRef.current, settings.mode)} title="Lay the map out again">
          Re-layout
        </button>
      </div>

      <div className="map-canvas" ref={containerRef} />

      {!ready && (
        <div className="map-overlay">
          {status?.state === "error" ? (
            <p className="error-text">Couldn't map the code: {status.error}</p>
          ) : (
            <p>
              <Spinner /> Mapping the code base
              {status?.total ? ` (${status.done} of ${status.total} files)` : "…"}
            </p>
          )}
        </div>
      )}
      {ready && error && (
        <div className="map-overlay">
          <p className="error-text">{error}</p>
        </div>
      )}
      {ready && !error && settings.mode === "focus" && !settings.seeds.length && (
        <div className="map-overlay">
          <p>
            Pick a function from the diff, or press <kbd className="kbd">⌘K</kbd> to find any symbol.
          </p>
        </div>
      )}
      {ready && !error && graph && graph.nodes.length === 0 && (
        <div className="map-overlay">
          <p>
            {settings.mode === "overview"
              ? "No mappable functions changed in the files you're filtering on. prmap maps Python, JavaScript and TypeScript."
              : "Nothing connects to this symbol with the current link types."}
          </p>
        </div>
      )}
      {graph?.truncated && (
        <div className="map-note">
          {settings.mode === "focus"
            ? `Showing the closest ${graph.nodes.length} symbols. Lower the depth to see everything.`
            : `This change is too big to map at once (showing ${graph.nodes.length} symbols). Map one group at a time from the Plan tab.`}
        </div>
      )}

      {graph && graph.nodes.length > 0 && (
        <div className="legend" aria-label="Legend">
          <div className="legend-row">
            {categoriesInGraph.map((c) => (
              <span key={c} className="legend-item">
                <span className="legend-line" style={{ background: `var(--cat-${c})` }} />
                {CATEGORY_LABEL[c]}
              </span>
            ))}
          </div>
          <div className="legend-row">
            {changeKinds.has("added") && (
              <span className="legend-item">
                <span className="legend-box" style={{ background: "var(--added-tint)", borderColor: "var(--added)" }} />
                Added
              </span>
            )}
            {changeKinds.has("modified") && (
              <span className="legend-item">
                <span className="legend-box" style={{ background: "var(--modified-tint)", borderColor: "var(--modified)" }} />
                Modified
              </span>
            )}
            <span className="legend-item">
              <span className="legend-box legend-dashed" />
              Outside this change
            </span>
          </div>
          <div className="legend-row">
            <span className="legend-item">
              <svg width="26" height="8" aria-hidden="true"><line x1="0" y1="4" x2="26" y2="4" stroke="var(--edge)" strokeWidth="2" /></svg>
              Calls
            </span>
            <span className="legend-item">
              <svg width="26" height="8" aria-hidden="true"><line x1="0" y1="4" x2="26" y2="4" stroke="var(--edge-soft)" strokeWidth="1.5" strokeDasharray="6 4" /></svg>
              Uses
            </span>
            <span className="legend-item">
              <svg width="26" height="8" aria-hidden="true"><line x1="0" y1="4" x2="26" y2="4" stroke="var(--edge)" strokeWidth="2" strokeDasharray="2 4" /></svg>
              Guess
            </span>
          </div>
          <div className="legend-hint muted">Click a box to see its links. Double-click to center the map on it.</div>
        </div>
      )}
    </div>
  );
}

function Stepper({ label, hint, value, onChange }: { label: string; hint: string; value: number; onChange: (v: number) => void }) {
  return (
    <div className="stepper" title={hint}>
      <span>{label}</span>
      <button className="btn btn-small btn-icon" onClick={() => onChange(Math.max(0, value - 1))} disabled={value <= 0} aria-label={`Fewer ${label.toLowerCase()} levels`}>
        −
      </button>
      <span className="stepper-value" aria-live="polite">
        {value}
      </span>
      <button className="btn btn-small btn-icon" onClick={() => onChange(Math.min(6, value + 1))} disabled={value >= 6} aria-label={`More ${label.toLowerCase()} levels`}>
        +
      </button>
    </div>
  );
}
