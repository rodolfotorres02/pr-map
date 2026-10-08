import type { ReviewRequest } from "./types";

function read<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function write(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage unavailable (private mode) - state just won't persist */
  }
}

export interface RecentReview {
  id: string;
  title: string;
  request: ReviewRequest;
  repo: string;
  opened: string;
}

export const storage = {
  reviewed: (reviewId: string) => new Set(read<string[]>(`prmap:reviewed:${reviewId}`, [])),
  saveReviewed: (reviewId: string, paths: Set<string>) => write(`prmap:reviewed:${reviewId}`, [...paths]),
  recent: (repo: string) => read<RecentReview[]>("prmap:recent", []).filter((r) => r.repo === repo),
  addRecent: (entry: RecentReview) => {
    const all = read<RecentReview[]>("prmap:recent", []).filter((r) => r.id !== entry.id);
    write("prmap:recent", [entry, ...all].slice(0, 30));
  },
  requestFor: (reviewId: string) =>
    read<RecentReview[]>("prmap:recent", []).find((r) => r.id === reviewId)?.request ?? null,
  get: read,
  set: write,
};
