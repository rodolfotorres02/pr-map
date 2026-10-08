import type { Theme } from "../lib/theme";
import type { IndexStatus, RepoInfo, Review } from "../lib/types";
import { BrandMark, Kbd, Spinner, ThemeButton } from "./common";
import type { View } from "./ReviewScreen";

interface Props {
  repo: RepoInfo;
  review: Review;
  status: IndexStatus | null;
  view: View;
  onView: (view: View) => void;
  onClose: () => void;
  onSearch: () => void;
  reviewedCount: number;
  theme: Theme;
}

export function TopBar({ repo, review, status, view, onView, onClose, onSearch, reviewedCount, theme }: Props) {
  const progress = status && status.total ? Math.round((status.done / status.total) * 100) : 0;
  return (
    <header className="topbar">
      <button className="brand brand-button" onClick={onClose} title="Pick another review">
        <BrandMark />
        <span className="brand-name">prmap</span>
      </button>

      <div className="topbar-title">
        <div className="topbar-heading">
          {review.pr ? (
            <a href={review.pr.url} target="_blank" rel="noreferrer" className="pr-link">
              #{review.pr.number}
            </a>
          ) : null}
          <span className="title-text" title={review.title}>
            {review.title}
          </span>
        </div>
        <div className="topbar-sub">
          <code>{review.head_label}</code> into <code>{review.base_label}</code>
          <span className="sep" />
          {review.commits} commit{review.commits === 1 ? "" : "s"}
          <span className="sep" />
          <span className="add">+{review.stats.additions}</span> <span className="del">−{review.stats.deletions}</span>
          <span className="sep" />
          {reviewedCount}/{review.stats.files} files reviewed
          {review.pr?.author && (
            <>
              <span className="sep" />
              by {review.pr.author}
            </>
          )}
        </div>
      </div>

      <div className="topbar-index" title={status?.error ?? undefined}>
        {status?.state === "ready" && (
          <span className="muted small">
            {repo.name} mapped: {status.symbols.toLocaleString()} symbols, {status.edges.toLocaleString()} links
          </span>
        )}
        {(status?.state === "building" || status?.state === "pending" || !status) && (
          <span className="indexing">
            <Spinner />
            Mapping code{status?.total ? ` ${progress}%` : "…"}
          </span>
        )}
        {status?.state === "error" && <span className="error-text small">Mapping failed: {status.error}</span>}
      </div>

      <button className="btn btn-ghost search-trigger" onClick={onSearch} title="Find a function or class">
        Find symbol <Kbd>⌘K</Kbd>
      </button>

      <div className="segmented" role="tablist" aria-label="View">
        <button role="tab" aria-selected={view === "diff"} className={view === "diff" ? "on" : ""} onClick={() => onView("diff")}>
          Diff
        </button>
        <button role="tab" aria-selected={view === "map"} className={view === "map" ? "on" : ""} onClick={() => onView("map")}>
          Map
        </button>
      </div>
      <ThemeButton theme={theme} />
    </header>
  );
}
