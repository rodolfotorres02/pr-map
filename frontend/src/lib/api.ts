import type {
  EdgeKind,
  ExplainEvent,
  FileDetail,
  Graph,
  GraphNode,
  IndexStatus,
  PullRequest,
  RepoInfo,
  Review,
  ReviewPlan,
  ReviewRequest,
  SourceSlice,
  SymbolChange,
} from "./types";

export class ApiError extends Error {}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail)) detail = body.detail.map((d: { msg: string }) => d.msg).join("; ");
    } catch {
      /* keep status text */
    }
    throw new ApiError(detail);
  }
  return response.json() as Promise<T>;
}

const post = <T>(path: string, body: unknown) => request<T>(path, { method: "POST", body: JSON.stringify(body) });
const qs = (params: Record<string, string | number>) =>
  new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)])).toString();

export interface GraphOptions {
  fuzzy: boolean;
  edge_kinds: EdgeKind[];
  exclude_categories: string[];
}

export const api = {
  repo: () => request<RepoInfo>("/api/repo"),
  prs: () => request<{ prs: PullRequest[]; error: string | null }>("/api/prs"),
  openReview: (body: ReviewRequest) => post<Review>("/api/reviews", body),
  review: (id: string) => request<Review>(`/api/reviews/${id}`),
  status: (id: string) => request<IndexStatus>(`/api/reviews/${id}/status`),
  file: (id: string, path: string, context: number) =>
    request<FileDetail>(`/api/reviews/${id}/file?${qs({ path, context })}`),
  changes: (id: string) => request<{ changes: SymbolChange[] }>(`/api/reviews/${id}/changes`),
  source: (id: string, path: string, start: number, end: number, side: "head" | "base" = "head") =>
    request<SourceSlice>(`/api/reviews/${id}/source?${qs({ path, start, end, side })}`),
  search: (id: string, q: string) => request<{ results: GraphNode[] }>(`/api/reviews/${id}/search?${qs({ q })}`),
  neighborhood: (
    id: string,
    body: GraphOptions & { seeds: string[]; depth_in: number; depth_out: number; max_nodes?: number },
  ) => post<Graph>(`/api/reviews/${id}/graph`, body),
  overview: (
    id: string,
    body: GraphOptions & { paths: string[] | null; neighbors: boolean; symbols?: string[] | null },
  ) => post<Graph>(`/api/reviews/${id}/overview`, body),
  plan: (id: string) => request<ReviewPlan>(`/api/reviews/${id}/plan`),
  explain,
};

/** Stream an "Explain with AI" answer (server-sent events over a POST). */
async function* explain(id: string, symbolId: string, refresh: boolean, signal: AbortSignal): AsyncGenerator<ExplainEvent> {
  const response = await fetch(`/api/reviews/${id}/explain`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbol_id: symbolId, refresh }),
    signal,
  });
  if (!response.ok || !response.body) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* keep status text */
    }
    yield { type: "error", message: detail };
    return;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const chunk = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      for (const line of chunk.split("\n")) {
        if (line.startsWith("data: ")) yield JSON.parse(line.slice(6)) as ExplainEvent;
      }
    }
  }
}
