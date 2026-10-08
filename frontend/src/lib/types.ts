export type Category =
  | "source"
  | "test"
  | "template"
  | "style"
  | "config"
  | "migration"
  | "docs"
  | "generated"
  | "asset"
  | "other";

export type EdgeKind = "calls" | "instantiates" | "renders" | "inherits" | "references";
export type ChangeStatus = "added" | "modified" | "deleted";

export interface Branch {
  name: string;
  sha: string;
  remote: boolean;
  current: boolean;
  date: string;
}

export interface RepoInfo {
  root: string;
  name: string;
  current_branch: string | null;
  default_branch: string | null;
  branches: Branch[];
  gh_available: boolean;
  claude_available: boolean;
  categories: { key: Category; label: string }[];
  edge_kinds: EdgeKind[];
}

export interface PullRequest {
  number: number;
  title: string;
  url: string;
  author: { login: string } | null;
  isDraft: boolean;
  baseRefName: string;
  headRefName: string;
  updatedAt: string;
}

export interface ChangedFile {
  path: string;
  old_path: string | null;
  status: "added" | "modified" | "deleted" | "renamed" | "copied" | "typechange";
  additions: number;
  deletions: number;
  binary: boolean;
  category: Category;
  language: string | null;
  ext: string;
  analyzable: boolean;
}

export interface Review {
  id: string;
  kind: "pr" | "branches";
  title: string;
  base_label: string;
  head_label: string;
  base_sha: string;
  head_sha: string;
  diff_base: string;
  commits: number;
  pr: {
    number: number;
    url: string;
    author: string | null;
    state: string;
    draft: boolean;
    body: string;
    updated_at: string;
  } | null;
  files: ChangedFile[];
  stats: { files: number; additions: number; deletions: number };
}

export type ReviewRequest =
  | { source: "pr"; pr: string }
  | { source: "branches"; base: string; head: string; mode?: "merge-base" | "direct" };

export interface IndexStatus {
  state: "pending" | "building" | "ready" | "error";
  done: number;
  total: number;
  error: string | null;
  symbols: number;
  edges: number;
  seconds: number;
}

export interface DiffLine {
  type: "add" | "del" | "ctx";
  old: number | null;
  new: number | null;
  text: string;
}

export interface Hunk {
  header: string;
  section: string;
  old_start: number;
  old_lines: number;
  new_start: number;
  new_lines: number;
  lines: DiffLine[];
}

export interface FileSymbol {
  id: string;
  qual: string;
  name: string;
  kind: string;
  start: number;
  end: number;
  change: ChangeStatus | null;
  callers: number;
  callees: number;
}

export interface FileDetail {
  file: ChangedFile;
  diff: { binary: boolean; hunks: Hunk[] };
  truncated: boolean;
  symbols: FileSymbol[];
}

export interface SymbolChange {
  id: string;
  path: string;
  qual: string;
  name: string;
  kind: string;
  start: number;
  end: number;
  status: ChangeStatus;
  additions: number;
  deletions: number;
  category: Category;
}

export interface GraphNode {
  id: string;
  name: string;
  qual: string;
  label: string;
  kind: string;
  path: string;
  line: number;
  end: number;
  language: string;
  category: Category;
  change: ChangeStatus | null;
  in_pr: boolean;
  role: "focus" | "caller" | "callee" | "changed" | "bridge" | "neighbor" | null;
  depth: number | null;
  callers: number;
  callees: number;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  kind: EdgeKind;
  fuzzy: boolean;
  lines: number[];
}

export interface Graph {
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated: boolean;
}

export interface SourceSlice {
  path: string;
  start: number;
  end: number;
  lines: string[];
  total: number;
}

export interface PlanCluster {
  id: string;
  title: string;
  symbols: string[];
  entry_points: string[];
  files: string[];
  additions: number;
  deletions: number;
  categories: Partial<Record<Category, number>>;
}

export interface SymbolRisk {
  callers: number;
  external_callers: number;
  tested: boolean;
  test_callers: string[];
  fan_out: number;
}

export interface ReviewPlan {
  clusters: PlanCluster[];
  unmapped_files: string[];
  risk: Record<string, SymbolRisk>;
  deleted_still_referenced: {
    id: string;
    name: string;
    path: string;
    references: { path: string; line: number }[];
  }[];
}

export type ExplainEvent =
  | { type: "meta"; model: string | null; cached?: boolean }
  | { type: "text"; text: string }
  | { type: "done"; cost_usd: number | null; duration_ms: number | null; cached?: boolean }
  | { type: "error"; message: string };
