import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";
import { EMPTY_FILTER, applyFilter, type FileFilter } from "../lib/filters";
import { storage } from "../lib/storage";
import type { Theme } from "../lib/theme";
import type { EdgeKind, FileDetail, GraphNode, IndexStatus, RepoInfo, Review, ReviewPlan, SymbolChange } from "../lib/types";
import { DiffView } from "./DiffView";
import { ExplainPanel, type ExplainTarget } from "./ExplainPanel";
import { FileTree, treeOrder } from "./FileTree";
import { FilterPanel } from "./FilterPanel";
import { Inspector } from "./Inspector";
import { MapView, type MapSettings } from "./MapView";
import { PlanPanel } from "./PlanPanel";
import { SymbolPalette } from "./SymbolPalette";
import { TopBar } from "./TopBar";

interface Props {
  repo: RepoInfo;
  review: Review;
  onClose: () => void;
  theme: Theme;
}

export type View = "diff" | "map";

const ALL_KINDS: EdgeKind[] = ["calls", "instantiates", "renders", "inherits", "references"];

function initialParam(name: string): string | null {
  return new URLSearchParams(window.location.search).get(name);
}

export function ReviewScreen({ repo, review, onClose, theme }: Props) {
  const [status, setStatus] = useState<IndexStatus | null>(null);
  const [filter, setFilterState] = useState<FileFilter>(() => storage.get(`prmap:filter:${repo.root}`, EMPTY_FILTER));
  const [reviewed, setReviewed] = useState<Set<string>>(() => storage.reviewed(review.id));
  const [view, setView] = useState<View>(() => (initialParam("view") === "map" ? "map" : "diff"));
  const [selectedPath, setSelectedPath] = useState<string | null>(() => initialParam("file"));
  const [detail, setDetail] = useState<FileDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [fullFile, setFullFile] = useState(false);
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [map, setMap] = useState<MapSettings>(() => ({
    mode: initialParam("focus") ? "focus" : "overview",
    seeds: initialParam("focus") ? [initialParam("focus")!] : [],
    depthIn: 2,
    depthOut: 2,
    fuzzy: true,
    kinds: ALL_KINDS,
    followFilter: true,
    neighbors: false,
  }));
  const filterInputRef = useRef<HTMLInputElement>(null);
  const [railTab, setRailTab] = useState<"plan" | "files">(() =>
    initialParam("group") || review.files.length > 15 ? "plan" : "files",
  );
  const [plan, setPlan] = useState<ReviewPlan | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const [changes, setChanges] = useState<Map<string, SymbolChange>>(new Map());
  const [explainTarget, setExplainTarget] = useState<ExplainTarget | null>(null);
  const explainFn = repo.claude_available ? setExplainTarget : null;
  const [activeCluster, setActiveCluster] = useState<string | null>(() => initialParam("group"));

  const setFilter = useCallback(
    (next: FileFilter) => {
      setFilterState(next);
      storage.set(`prmap:filter:${repo.root}`, { ...next, hideReviewed: false });
    },
    [repo.root],
  );

  const indexReady = status?.state === "ready";
  const cluster = plan?.clusters.find((c) => c.id === activeCluster) ?? null;
  const visibleFiles = useMemo(() => {
    if (!cluster) return treeOrder(applyFilter(review.files, filter, reviewed));
    // Inside a group, follow its reading order (entry points first).
    const rank = new Map(cluster.files.map((p, i) => [p, i]));
    return applyFilter(review.files.filter((f) => rank.has(f.path)), filter, reviewed).sort(
      (a, b) => rank.get(a.path)! - rank.get(b.path)!,
    );
  }, [review.files, filter, reviewed, cluster]);

  // The review plan (change groups, reading order, risk) needs the code index.
  useEffect(() => {
    if (!indexReady) return;
    let cancelled = false;
    api
      .changes(review.id)
      .then((r) => !cancelled && setChanges(new Map(r.changes.map((c) => [c.id, c]))))
      .catch(() => undefined);
    api
      .plan(review.id)
      .then((p) => !cancelled && setPlan(p))
      .catch((err) => !cancelled && setPlanError(`Couldn't build the review plan: ${err.message ?? err}`));
    return () => {
      cancelled = true;
    };
  }, [indexReady, review.id]);

  // Poll indexing progress until the call graph is ready.
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      try {
        const next = await api.status(review.id);
        if (cancelled) return;
        setStatus(next);
        if (next.state === "pending" || next.state === "building") timer = window.setTimeout(tick, 400);
      } catch {
        if (!cancelled) timer = window.setTimeout(tick, 1500);
      }
    };
    void tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [review.id]);

  // Default selection: first visible file.
  useEffect(() => {
    if (!selectedPath || !review.files.some((f) => f.path === selectedPath)) {
      setSelectedPath(visibleFiles[0]?.path ?? review.files[0]?.path ?? null);
    }
  }, [selectedPath, visibleFiles, review.files]);

  // Load the selected file's diff (reloads once the index is ready to pick up symbols).
  useEffect(() => {
    if (!selectedPath) return;
    let cancelled = false;
    setDetailError(null);
    api
      .file(review.id, selectedPath, fullFile ? 100000 : 4)
      .then((d) => !cancelled && setDetail(d))
      .catch((err) => !cancelled && setDetailError(String(err.message ?? err)));
    return () => {
      cancelled = true;
    };
  }, [review.id, selectedPath, fullFile, indexReady]);

  // Keep the URL shareable/reloadable.
  useEffect(() => {
    const params = new URLSearchParams({ review: review.id });
    if (activeCluster) params.set("group", activeCluster);
    if (selectedPath) params.set("file", selectedPath);
    if (view === "map") params.set("view", "map");
    if (view === "map" && map.mode === "focus" && map.seeds.length === 1) params.set("focus", map.seeds[0]);
    window.history.replaceState(null, "", `${window.location.pathname}?${params}`);
  }, [review.id, selectedPath, view, map.mode, map.seeds, activeCluster]);

  const toggleReviewed = useCallback(
    (path: string) => {
      setReviewed((prev) => {
        const next = new Set(prev);
        if (next.has(path)) next.delete(path);
        else next.add(path);
        storage.saveReviewed(review.id, next);
        return next;
      });
    },
    [review.id],
  );

  const openFile = useCallback((path: string, line?: number) => {
    setSelectedPath(path);
    setView("diff");
    if (line) {
      window.setTimeout(() => {
        document.getElementById(`line-${line}`)?.scrollIntoView({ block: "center", behavior: "smooth" });
      }, 250);
    }
  }, []);

  const focusSymbol = useCallback((id: string) => {
    setMap((m) => ({ ...m, mode: "focus", seeds: [id] }));
    setSelectedNode(null);
    setView("map");
  }, []);

  const showOverview = useCallback(() => {
    setMap((m) => ({ ...m, mode: "overview" }));
    setSelectedNode(null);
    setView("map");
  }, []);

  const activateCluster = useCallback(
    (id: string | null) => {
      setActiveCluster(id);
      const target = plan?.clusters.find((c) => c.id === id);
      if (target?.files[0]) setSelectedPath(target.files[0]);
    },
    [plan],
  );

  const step = useCallback(
    (delta: number) => {
      if (!visibleFiles.length) return;
      const index = visibleFiles.findIndex((f) => f.path === selectedPath);
      const next = visibleFiles[Math.min(visibleFiles.length - 1, Math.max(0, index + delta))];
      if (next) openFile(next.path);
    },
    [visibleFiles, selectedPath, openFile],
  );

  // Keyboard shortcuts.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen(true);
        return;
      }
      const target = e.target as HTMLElement;
      if (target.closest("input, textarea, select, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "j") step(1);
      else if (e.key === "k") step(-1);
      else if (e.key === "m") setView((v) => (v === "map" ? "diff" : "map"));
      else if (e.key === "o") showOverview();
      else if (e.key === "p") setRailTab((t) => (t === "plan" ? "files" : "plan"));
      else if (e.key === "r" && selectedPath) {
        const wasReviewed = reviewed.has(selectedPath);
        toggleReviewed(selectedPath);
        if (!wasReviewed) {
          // Advance to the next file still waiting for review.
          const index = visibleFiles.findIndex((f) => f.path === selectedPath);
          const next = [...visibleFiles.slice(index + 1), ...visibleFiles.slice(0, index)].find(
            (f) => !reviewed.has(f.path) && f.path !== selectedPath,
          );
          if (next) openFile(next.path);
        }
      }
      else if (e.key === "/") {
        e.preventDefault();
        setRailTab("files");
        window.setTimeout(() => filterInputRef.current?.focus(), 0);
      } else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [step, showOverview, toggleReviewed, selectedPath, reviewed, visibleFiles, openFile]);

  const reviewedCount = review.files.filter((f) => reviewed.has(f.path)).length;

  return (
    <div className="review">
      <TopBar
        repo={repo}
        review={review}
        status={status}
        view={view}
        onView={(v) => (v === "map" && map.mode === "overview" ? showOverview() : setView(v))}
        onClose={onClose}
        onSearch={() => setPaletteOpen(true)}
        reviewedCount={reviewedCount}
        theme={theme}
      />
      <aside className="rail" aria-label="Files">
        <div className="rail-tabs" role="tablist" aria-label="Sidebar">
          <button role="tab" aria-selected={railTab === "plan"} className={railTab === "plan" ? "on" : ""} onClick={() => setRailTab("plan")}>
            Plan{plan ? ` (${plan.clusters.length})` : ""}
          </button>
          <button role="tab" aria-selected={railTab === "files"} className={railTab === "files" ? "on" : ""} onClick={() => setRailTab("files")}>
            Files ({visibleFiles.length})
          </button>
        </div>
        {cluster && (
          <div className="scope-pill">
            <span className="scope-text" title={cluster.title}>
              Group {plan!.clusters.indexOf(cluster) + 1}: <strong>{cluster.title}</strong>
            </span>
            <button className="btn btn-link small" onClick={() => setActiveCluster(null)}>
              Show all
            </button>
          </div>
        )}
        {railTab === "plan" ? (
          <div className="rail-scroll">
            <PlanPanel
              plan={plan}
              error={planError}
              indexReady={indexReady}
              changes={changes}
              files={review.files}
              reviewed={reviewed}
              activeCluster={activeCluster}
              onActivate={activateCluster}
              onOpenFile={openFile}
              onFocusSymbol={focusSymbol}
              onExplain={explainFn}
              onMapCluster={(id) => {
                activateCluster(id);
                showOverview();
              }}
            />
          </div>
        ) : (
          <>
            <FilterPanel
              files={review.files}
              filter={filter}
              onChange={setFilter}
              reviewed={reviewed}
              visibleCount={visibleFiles.length}
              inputRef={filterInputRef}
            />
            <FileTree
              files={visibleFiles}
              selected={selectedPath}
              reviewed={reviewed}
              onSelect={(p) => openFile(p)}
              onToggleReviewed={toggleReviewed}
              totalFiles={review.files.length}
              onClearFilter={() => {
                setFilter(EMPTY_FILTER);
                setActiveCluster(null);
              }}
            />
          </>
        )}
      </aside>
      <main className="stage">
        {view === "diff" ? (
          <DiffView
            detail={detail && detail.file.path === selectedPath ? detail : null}
            error={detailError}
            reviewed={selectedPath ? reviewed.has(selectedPath) : false}
            onToggleReviewed={() => selectedPath && toggleReviewed(selectedPath)}
            fullFile={fullFile}
            onFullFile={setFullFile}
            onFocusSymbol={focusSymbol}
            onExplain={explainFn}
            indexReady={indexReady}
          />
        ) : (
          <MapView
            review={review}
            settings={map}
            onSettings={setMap}
            filter={filter}
            visiblePaths={visibleFiles.map((f) => f.path)}
            clusterSymbols={cluster?.symbols ?? null}
            status={status}
            selected={selectedNode}
            onSelect={setSelectedNode}
            onFocus={focusSymbol}
            onOverview={showOverview}
            theme={theme.resolved}
          />
        )}
      </main>
      <aside className="inspector" aria-label="Details">
        {explainTarget && (
          <ExplainPanel key={explainTarget.id} reviewId={review.id} target={explainTarget} onClose={() => setExplainTarget(null)} />
        )}
        <Inspector
          review={review}
          view={view}
          detail={detail && detail.file.path === selectedPath ? detail : null}
          status={status}
          selectedNode={selectedNode}
          risk={plan?.risk ?? null}
          changes={changes}
          scope={cluster}
          onExplain={explainFn}
          onFocus={focusSymbol}
          onOpenFile={openFile}
          onSelectNode={setSelectedNode}
        />
      </aside>
      {paletteOpen && (
        <SymbolPalette
          reviewId={review.id}
          ready={indexReady}
          onClose={() => setPaletteOpen(false)}
          onPick={(id) => {
            setPaletteOpen(false);
            focusSymbol(id);
          }}
        />
      )}
    </div>
  );
}
