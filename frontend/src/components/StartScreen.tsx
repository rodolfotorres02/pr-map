import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { storage } from "../lib/storage";
import type { Theme } from "../lib/theme";
import type { PullRequest, RepoInfo, ReviewRequest } from "../lib/types";
import { relativeTime } from "../lib/format";
import { BrandMark, ThemeButton } from "./common";

interface Props {
  repo: RepoInfo;
  onOpen: (request: ReviewRequest, label: string) => void;
  opening: string | null;
  error: string | null;
  theme: Theme;
}

export function StartScreen({ repo, onOpen, opening, error, theme }: Props) {
  const [prInput, setPrInput] = useState("");
  const [prs, setPrs] = useState<PullRequest[] | null>(null);
  const [prError, setPrError] = useState<string | null>(null);
  const [prQuery, setPrQuery] = useState("");

  const localBranches = repo.branches.filter((b) => !b.remote);
  const defaultBase = repo.default_branch ?? localBranches.find((b) => b.name === "main")?.name ?? "";
  const baseName = defaultBase.replace(/^origin\//, "");
  const defaultHead =
    repo.current_branch && repo.current_branch !== baseName
      ? repo.current_branch
      : localBranches.find((b) => b.name !== baseName)?.name ?? "";
  const [base, setBase] = useState(defaultBase);
  const [head, setHead] = useState(defaultHead);
  const [onlyHead, setOnlyHead] = useState(true);
  const recent = useMemo(() => storage.recent(repo.root).slice(0, 6), [repo.root]);

  useEffect(() => {
    if (!repo.gh_available) {
      setPrError("Install the GitHub CLI (gh) and run `gh auth login` to list pull requests.");
      setPrs([]);
      return;
    }
    api
      .prs()
      .then((r) => {
        setPrs(r.prs);
        setPrError(r.error);
      })
      .catch((err) => setPrError(String(err)));
  }, [repo.gh_available]);

  const visiblePrs = (prs ?? []).filter((pr) => {
    const q = prQuery.trim().toLowerCase();
    return !q || `${pr.number} ${pr.title} ${pr.author?.login ?? ""} ${pr.headRefName}`.toLowerCase().includes(q);
  });

  const busy = opening !== null;

  return (
    <div className="start">
      <header className="start-head">
        <div className="brand">
          <BrandMark />
          <span className="brand-name">prmap</span>
        </div>
        <div className="start-repo" title={repo.root}>
          {repo.name}
          {repo.current_branch && <span className="muted"> on {repo.current_branch}</span>}
        </div>
        <ThemeButton theme={theme} />
      </header>

      <main className="start-main">
        <h1 className="start-title">What are you reviewing?</h1>
        <p className="start-lede">
          Pick a pull request or two branches. prmap lets you filter the changed files by role and trace how each
          changed function connects to the rest of {repo.name}.
        </p>

        {(error || opening) && (
          <div className={error ? "notice notice-error" : "notice"} role="status">
            {error ?? `${opening}…`}
          </div>
        )}

        <div className="start-grid">
          <section className="panel">
            <h2>Pull request</h2>
            <form
              className="row"
              onSubmit={(e) => {
                e.preventDefault();
                if (prInput.trim()) onOpen({ source: "pr", pr: prInput.trim() }, `Fetching PR ${prInput.trim()}`);
              }}
            >
              <input
                className="input grow"
                placeholder="PR number or GitHub URL"
                value={prInput}
                onChange={(e) => setPrInput(e.target.value)}
                aria-label="Pull request number or URL"
              />
              <button className="btn btn-primary" disabled={busy || !prInput.trim()}>
                Open PR
              </button>
            </form>

            <div className="pr-list-head">
              <span>Open pull requests</span>
              {prs && prs.length > 6 && (
                <input
                  className="input input-small"
                  placeholder="Filter"
                  value={prQuery}
                  onChange={(e) => setPrQuery(e.target.value)}
                  aria-label="Filter pull requests"
                />
              )}
            </div>
            {prError && <p className="muted small">{prError}</p>}
            {prs === null && <p className="muted small">Asking GitHub…</p>}
            {prs && prs.length === 0 && !prError && <p className="muted small">No open pull requests.</p>}
            <ul className="pr-list">
              {visiblePrs.map((pr) => (
                <li key={pr.number}>
                  <button
                    className="pr-item"
                    disabled={busy}
                    onClick={() => onOpen({ source: "pr", pr: String(pr.number) }, `Fetching PR #${pr.number}`)}
                  >
                    <span className="pr-number">#{pr.number}</span>
                    <span className="pr-title">
                      {pr.title}
                      {pr.isDraft && <span className="tag">draft</span>}
                    </span>
                    <span className="pr-meta">
                      {pr.author?.login ?? "unknown"} wants to merge <code>{pr.headRefName}</code> into{" "}
                      <code>{pr.baseRefName}</code>, updated {relativeTime(pr.updatedAt)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </section>

          <section className="panel">
            <h2>Branches</h2>
            <form
              className="stack"
              onSubmit={(e) => {
                e.preventDefault();
                onOpen(
                  { source: "branches", base, head, mode: onlyHead ? "merge-base" : "direct" },
                  `Comparing ${head} with ${base}`,
                );
              }}
            >
              <label className="field">
                <span>Review changes on</span>
                <RefInput value={head} onChange={setHead} repo={repo} />
              </label>
              <label className="field">
                <span>Compared with</span>
                <RefInput value={base} onChange={setBase} repo={repo} />
              </label>
              <label className="check">
                <input type="checkbox" checked={onlyHead} onChange={(e) => setOnlyHead(e.target.checked)} />
                <span>
                  Only show what the head branch changed
                  <span className="muted small block">
                    Diffs from the merge base, like GitHub does. Untick to diff the two tips directly.
                  </span>
                </span>
              </label>
              <button className="btn btn-primary" disabled={busy || !base || !head || base === head}>
                Compare branches
              </button>
            </form>

            {recent.length > 0 && (
              <>
                <h3 className="recent-head">Recent reviews</h3>
                <ul className="recent-list">
                  {recent.map((r) => (
                    <li key={r.id}>
                      <button className="link-row" disabled={busy} onClick={() => onOpen(r.request, `Reopening ${r.title}`)}>
                        <span>{r.title}</span>
                        <span className="muted small">{relativeTime(r.opened)}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </section>
        </div>
      </main>
    </div>
  );
}

function RefInput({ value, onChange, repo }: { value: string; onChange: (v: string) => void; repo: RepoInfo }) {
  const id = useMemo(() => `refs-${Math.random().toString(36).slice(2)}`, []);
  return (
    <>
      <input
        className="input mono"
        list={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="branch, tag or commit"
        spellCheck={false}
      />
      <datalist id={id}>
        {repo.branches.map((b) => (
          <option key={b.name} value={b.name}>
            {b.remote ? "remote" : b.current ? "current branch" : "local"}
          </option>
        ))}
      </datalist>
    </>
  );
}
