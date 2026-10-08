import { useCallback, useEffect, useState } from "react";
import { ReviewScreen } from "./components/ReviewScreen";
import { StartScreen } from "./components/StartScreen";
import { ApiError, api } from "./lib/api";
import { storage } from "./lib/storage";
import type { RepoInfo, Review, ReviewRequest } from "./lib/types";
import { useTheme } from "./lib/theme";

function readUrl() {
  return new URLSearchParams(window.location.search);
}

export function App() {
  const [repo, setRepo] = useState<RepoInfo | null>(null);
  const [repoError, setRepoError] = useState<string | null>(null);
  const [review, setReview] = useState<Review | null>(null);
  const [opening, setOpening] = useState<string | null>(null);
  const [openError, setOpenError] = useState<string | null>(null);
  const theme = useTheme();

  const open = useCallback(
    async (request: ReviewRequest, label: string) => {
      setOpening(label);
      setOpenError(null);
      try {
        const result = await api.openReview(request);
        if (repo) {
          storage.addRecent({
            id: result.id,
            title: result.title,
            request,
            repo: repo.root,
            opened: new Date().toISOString(),
          });
        }
        const url = new URL(window.location.href);
        url.search = new URLSearchParams({ review: result.id }).toString();
        window.history.pushState(null, "", url);
        setReview(result);
      } catch (err) {
        setOpenError(err instanceof Error ? err.message : String(err));
      } finally {
        setOpening(null);
      }
    },
    [repo],
  );

  useEffect(() => {
    api
      .repo()
      .then(setRepo)
      .catch((err) => setRepoError(err instanceof ApiError ? err.message : "Can't reach the prmap server. Is `prmap` running?"));
  }, []);

  // Restore a review from the URL (?review=, ?pr=, ?base=&head=).
  useEffect(() => {
    if (!repo) return;
    const params = readUrl();
    const reviewId = params.get("review");
    if (reviewId) {
      api.review(reviewId).then(setReview).catch(() => {
        // Server restarted: re-open from the remembered request.
        const request = storage.requestFor(reviewId);
        if (request) void open(request, "Reopening review");
      });
    } else if (params.get("pr")) {
      void open({ source: "pr", pr: params.get("pr")! }, `Fetching PR ${params.get("pr")}`);
    } else if (params.get("base") && params.get("head")) {
      void open(
        { source: "branches", base: params.get("base")!, head: params.get("head")! },
        `Comparing ${params.get("head")}`,
      );
    }
  }, [repo, open]);

  useEffect(() => {
    const onPop = () => {
      if (!readUrl().get("review")) setReview(null);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const close = () => {
    const url = new URL(window.location.href);
    url.search = "";
    window.history.pushState(null, "", url);
    setReview(null);
  };

  if (repoError) {
    return (
      <div className="fatal">
        <h1>prmap</h1>
        <p>{repoError}</p>
      </div>
    );
  }
  if (!repo) return <div className="booting">Loading repository…</div>;

  if (review) {
    return <ReviewScreen key={review.id} repo={repo} review={review} onClose={close} theme={theme} />;
  }
  return <StartScreen repo={repo} onOpen={open} opening={opening} error={openError} theme={theme} />;
}
