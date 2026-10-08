"""Review sessions: what is being compared, which files changed, which symbols changed."""

from __future__ import annotations

import heapq
import threading
import time
from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache

from prmap import github
from prmap.analysis import MODULE
from prmap.analysis.index import CodeIndex, symbol_id
from prmap.classify import ANALYZABLE, Classifier, extension, language_of
from prmap.config import RepoConfig, load_config
from prmap.diffparse import changed_line_numbers, parse_unified_diff
from prmap.gitrepo import FileHunks, GitError, GitRepo
from prmap.graph import GraphQuery
from prmap.indexer import ParseCache, analyze_blobs, build_index, index_exclude

MAX_DIFF_LINES = 6000


class ReviewError(RuntimeError):
    pass


@dataclass
class IndexJob:
    head_sha: str
    state: str = "pending"  # pending | building | ready | error
    done: int = 0
    total: int = 0
    error: str | None = None
    index: CodeIndex | None = None
    started: float = field(default_factory=time.time)
    finished: float | None = None


@dataclass
class Review:
    id: str
    kind: str  # pr | branches
    title: str
    base_label: str
    head_label: str
    base_sha: str
    head_sha: str
    diff_base: str
    files: list[dict]
    job: IndexJob
    pr: dict | None = None
    commits: int = 0
    _changes: dict[str, dict] | None = None
    # path -> (base blob sha, base mode); filled from `git diff --raw` when the review opens
    _base_blobs: dict[str, tuple[str | None, str | None]] = field(default_factory=dict, repr=False)
    # changed line ranges + base-side symbols, computed in the background right after opening
    _prep: dict | None = field(default=None, repr=False)
    _changes_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def summary(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "base_label": self.base_label,
            "head_label": self.head_label,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "diff_base": self.diff_base,
            "commits": self.commits,
            "pr": self.pr,
            "files": self.files,
            "stats": {
                "files": len(self.files),
                "additions": sum(f["additions"] for f in self.files),
                "deletions": sum(f["deletions"] for f in self.files),
            },
        }

    def file(self, path: str) -> dict:
        for f in self.files:
            if f["path"] == path:
                return f
        raise ReviewError(f"{path} is not part of this review")


def _innermost(spans: list[tuple[int, int, str]], line: int) -> str:
    best, best_size = MODULE, None
    for start, end, qual in spans:
        if start <= line <= end and qual != MODULE:
            size = end - start
            if best_size is None or size < best_size:
                best, best_size = qual, size
    return best


def _count_innermost(spans: list[tuple[int, int, str]], ranges: list[tuple[int, int]]) -> dict[str, int]:
    """For (first_line, count) ranges, count lines per innermost enclosing span.

    Same answer as calling ``_innermost`` for every line (smallest span wins, ties go to
    the earlier span, MODULE when nothing encloses the line) but O((ranges + spans) log
    spans): a sweep with a heap of open spans, jumping between span boundaries, so a
    50k-line added file costs about as much as a one-line edit.
    """
    if not ranges:
        return {}
    live = sorted(
        ((start, end, i, qual) for i, (start, end, qual) in enumerate(spans) if qual != MODULE and end >= start),
        key=lambda item: item[0],
    )
    bounds = sorted({item[0] for item in live} | {item[1] + 1 for item in live})
    heap: list[tuple[int, int, int, str]] = []
    counts: dict[str, int] = {}
    k, done_until = 0, 0
    for first, length in sorted(ranges):
        line, last = max(first, done_until + 1), first + length - 1
        while line <= last:
            while k < len(live) and live[k][0] <= line:
                start, end, i, qual = live[k]
                heapq.heappush(heap, (end - start, i, end, qual))
                k += 1
            while heap and heap[0][2] < line:
                heapq.heappop(heap)
            qual = heap[0][3] if heap else MODULE
            j = bisect_right(bounds, line)
            seg_last = min(last, bounds[j] - 1) if j < len(bounds) else last
            counts[qual] = counts.get(qual, 0) + seg_last - line + 1
            line = seg_last + 1
        done_until = max(done_until, last)
    return counts


class ReviewManager:
    def __init__(self, repo: GitRepo, config: RepoConfig | None = None, cache: ParseCache | None = None):
        self.repo = repo
        self.config = config or load_config(repo.root)
        self.classifier = Classifier(self.config.category_overrides)
        self.exclude = index_exclude(self.config)
        self.cache = cache or ParseCache()
        self.reviews: dict[str, Review] = {}
        self.jobs: OrderedDict[str, IndexJob] = OrderedDict()
        self._lock = threading.Lock()
        self.category_of = lru_cache(maxsize=100_000)(self.classifier.category)

    # -- opening reviews ----------------------------------------------------

    def open_branches(self, base: str, head: str, mode: str = "merge-base") -> Review:
        try:
            base_sha = self.repo.rev_parse(base)
            head_sha = self.repo.rev_parse(head)
        except GitError as exc:
            raise ReviewError(f"Unknown branch or commit: {exc}") from exc
        if not base_sha or not head_sha:
            raise ReviewError(f"Unknown branch or commit: {base if not base_sha else head}")
        diff_base = self.repo.merge_base(base_sha, head_sha) if mode == "merge-base" else base_sha
        title = f"{head} → {base}"
        return self._create("branches", title, base, head, base_sha, head_sha, diff_base, None)

    def open_pr(self, pr_input: str) -> Review:
        try:
            pr = github.view_pr(self.repo, pr_input)
            base_sha, head_sha = github.fetch_pr(self.repo, pr)
        except (github.GitHubError, GitError) as exc:
            raise ReviewError(str(exc)) from exc
        try:
            diff_base = self.repo.merge_base(base_sha, head_sha)
        except GitError as exc:
            raise ReviewError(
                f"PR #{pr['number']} shares no history with '{pr['baseRefName']}' in this clone. "
                "If it is a shallow clone, run `git fetch --unshallow` and try again."
            ) from exc
        meta = {
            "number": pr["number"],
            "url": pr["url"],
            "author": (pr.get("author") or {}).get("login"),
            "state": pr.get("state"),
            "draft": pr.get("isDraft"),
            "body": pr.get("body") or "",
            "updated_at": pr.get("updatedAt"),
        }
        return self._create(
            "pr", pr["title"], pr["baseRefName"], pr["headRefName"], base_sha, head_sha, diff_base, meta
        )

    def _create(self, kind, title, base_label, head_label, base_sha, head_sha, diff_base, pr) -> Review:
        review_id = f"{diff_base[:12]}-{head_sha[:12]}"
        with self._lock:
            existing = self.reviews.get(review_id)
            if existing is not None:
                if pr:
                    existing.pr, existing.title, existing.kind = pr, title, kind
                return existing

        files = []
        base_blobs: dict[str, tuple[str | None, str | None]] = {}
        for changed in self.repo.changed_files(diff_base, head_sha):
            language = language_of(changed.path)
            base_blobs[changed.path] = (changed.old_sha, changed.old_mode)
            files.append(
                {
                    "path": changed.path,
                    "old_path": changed.old_path,
                    "status": changed.status,
                    "additions": changed.additions,
                    "deletions": changed.deletions,
                    "binary": changed.binary,
                    "category": self.category_of(changed.path),
                    "language": language,
                    "ext": extension(changed.path) or "(none)",
                    # index_exclude (vendor/, dist/, *.min.js, ...) is never parsed, at head or base
                    "analyzable": language in ANALYZABLE and not self.exclude.match_file(changed.path),
                }
            )
        files.sort(key=lambda f: f["path"])

        review = Review(
            id=review_id,
            kind=kind,
            title=title,
            base_label=base_label,
            head_label=head_label,
            base_sha=base_sha,
            head_sha=head_sha,
            diff_base=diff_base,
            files=files,
            job=self._index_job(head_sha),
            pr=pr,
            commits=self.repo.commit_count(diff_base, head_sha),
            _base_blobs=base_blobs,
        )
        with self._lock:
            self.reviews[review_id] = review
        # Diff line ranges and base-side symbols don't need the head index: compute them
        # while it builds so /changes is instant once the index is ready.
        threading.Thread(
            target=self._prepare_quietly, args=(review,), daemon=True, name=f"prep-{review_id[:8]}"
        ).start()
        return review

    def get(self, review_id: str) -> Review:
        review = self.reviews.get(review_id)
        if review is None:
            raise ReviewError("Review not found (the server may have restarted). Open it again.")
        return review

    # -- indexing -----------------------------------------------------------

    def _index_job(self, head_sha: str) -> IndexJob:
        with self._lock:
            job = self.jobs.get(head_sha)
            if job is not None and job.state != "error":
                self.jobs.move_to_end(head_sha)
                return job
            job = IndexJob(head_sha)
            self.jobs[head_sha] = job
            while len(self.jobs) > 4:
                self.jobs.popitem(last=False)
        threading.Thread(target=self._build, args=(job,), daemon=True, name=f"index-{head_sha[:8]}").start()
        return job

    def _build(self, job: IndexJob) -> None:
        job.state = "building"

        def progress(done: int, total: int) -> None:
            job.done, job.total = done, total

        try:
            job.index = build_index(self.repo, job.head_sha, self.config, self.cache, progress)
            job.state = "ready"
        except Exception as exc:  # surfaced to the UI
            job.state = "error"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.finished = time.time()

    def require_index(self, review: Review) -> CodeIndex:
        if review.job.state != "ready" or review.job.index is None:
            raise ReviewError("The code index is still being built. Try again in a moment.")
        return review.job.index

    # -- changed symbols ----------------------------------------------------

    def _prepare_quietly(self, review: Review) -> None:
        try:
            self._prepare(review)
        except Exception:  # retried (and surfaced) by changes()
            pass

    def _prepare(self, review: Review) -> dict:
        """Changed line ranges for every file (one ``git diff -U0``) and base-side symbols
        for every modified/renamed/deleted analyzable file (one batched parse)."""
        with review._changes_lock:
            if review._prep is not None:
                return review._prep
            hunks = self.repo.zero_context_hunks(review.diff_base, review.head_sha)
            wanted: dict[str, tuple[str, str]] = {}
            for f in review.files:
                if not f["analyzable"] or f["binary"] or f["status"] == "added":
                    continue
                old = f["old_path"] or f["path"]
                language = language_of(old)
                sha, mode = review._base_blobs.get(f["path"], (None, None))
                if language not in ANALYZABLE or not sha or mode in ("120000", "160000"):
                    continue  # symlinks and submodules have no source to parse
                if self.exclude.match_file(old):
                    continue
                wanted[old] = (sha, language)
            sizes = self.repo.blob_sizes(sorted({sha for sha, _ in wanted.values()}))
            targets = [
                (path, sha, language)
                for path, (sha, language) in wanted.items()
                if sha in sizes and sizes[sha] <= self.config.max_file_bytes
            ]
            analyses = analyze_blobs(self.repo, targets, self.cache) if targets else {}
            review._prep = {
                "hunks": hunks,
                "base": {path: analysis.get("symbols", []) for path, analysis in analyses.items()},
            }
            return review._prep

    def changes(self, review: Review) -> dict[str, dict]:
        if review._changes is not None:
            return review._changes
        index = self.require_index(review)
        with review._changes_lock:
            if review._changes is not None:
                return review._changes
            prep = self._prepare(review)
            result: dict[str, dict] = {}
            for f in review.files:
                self._file_changes(review, f, index, prep, result)
            review._changes = result
            return result

    def _file_changes(self, review: Review, f: dict, index: CodeIndex, prep: dict, result: dict) -> None:
        status = f["status"]
        if not f["analyzable"] or f["binary"]:
            return
        if status != "deleted" and f["path"] not in index.files:
            return  # excluded or too large at head: diffing symbols would report them all deleted
        old = f["old_path"] or f["path"]
        base_known = status == "added" or old in prep["base"]
        if status == "deleted" and not base_known:
            return  # excluded / too large / unreadable at base
        head_syms = [] if status == "deleted" else index.symbols_in_file(f["path"])
        base_syms = [] if status == "added" else prep["base"].get(old, [])

        if status == "added":
            added, deleted = ([(1, f["additions"])] if f["additions"] else []), []
        elif status == "deleted":
            added, deleted = [], ([(1, f["deletions"])] if f["deletions"] else [])
        else:
            hunks = prep["hunks"].get(f["path"])
            if hunks is None and (f["additions"] or f["deletions"]):
                hunks = self._single_file_hunks(review, f)  # path the bulk parser couldn't place
            added, deleted = (hunks.added, hunks.deleted) if hunks else ([], [])

        head_by_qual = {s.qual: s for s in head_syms}
        base_by_qual = {s["qual"]: s for s in base_syms}
        counts: dict[str, list[int]] = {}
        for qual, n in _count_innermost([(s.start, s.end, s.qual) for s in head_syms], added).items():
            counts.setdefault(qual, [0, 0])[0] += n
        for qual, n in _count_innermost([(s["start"], s["end"], s["qual"]) for s in base_syms], deleted).items():
            counts.setdefault(qual, [0, 0])[1] += n

        for qual, (adds, dels) in counts.items():
            sym = head_by_qual.get(qual)
            if sym is not None:
                # Unknown base (too large / excluded old path): call it modified, not added.
                is_new = status == "added" or (base_known and qual not in base_by_qual and qual != MODULE)
                result[sym.id] = self._change_entry(
                    sym.id, f, qual, sym.name, sym.kind, sym.start, sym.end,
                    "added" if is_new else "modified", adds, dels,
                )
        for qual, raw in base_by_qual.items():
            if qual not in head_by_qual and qual != MODULE:
                sid = symbol_id(f["path"], qual)
                result[sid] = self._change_entry(
                    sid, f, qual, raw["name"], raw["kind"], raw["start"], raw["end"],
                    "deleted", 0, counts.get(qual, [0, raw["end"] - raw["start"] + 1])[1],
                )

    def _single_file_hunks(self, review: Review, f: dict) -> FileHunks:
        parsed = parse_unified_diff(
            self.repo.file_diff(review.diff_base, review.head_sha, f["path"], f["old_path"], context=0)
        )
        added, deleted = changed_line_numbers(parsed)
        return FileHunks(f["path"], f["old_path"], [(n, 1) for n in sorted(deleted)], [(n, 1) for n in sorted(added)])

    def _change_entry(self, sid, f, qual, name, kind, start, end, status, adds, dels) -> dict:
        return {
            "id": sid,
            "path": f["path"],
            "qual": qual,
            "name": name if kind != "module" else f"{f['path'].rsplit('/', 1)[-1]} (module level)",
            "kind": kind,
            "start": start,
            "end": end,
            "status": status,
            "additions": adds,
            "deletions": dels,
            "category": f["category"],
        }

    # -- per-file detail ----------------------------------------------------

    def file_detail(self, review: Review, path: str, context: int = 4) -> dict:
        f = review.file(path)
        # Stream at most a bounded number of lines from git: a regenerated lockfile or
        # minified bundle can produce a diff of hundreds of thousands of lines.
        read_limit = MAX_DIFF_LINES + 500
        diff_text = self.repo.file_diff(
            review.diff_base, review.head_sha, f["path"], f["old_path"], context=context, max_lines=read_limit
        )
        parsed = parse_unified_diff(diff_text)
        total = sum(len(h["lines"]) for h in parsed["hunks"])
        truncated = diff_text.count("\n") >= read_limit
        if total > MAX_DIFF_LINES:
            kept, budget = [], MAX_DIFF_LINES
            for hunk in parsed["hunks"]:
                if budget <= 0:
                    break
                kept.append({**hunk, "lines": hunk["lines"][:budget]})
                budget -= len(hunk["lines"])
            parsed["hunks"], truncated = kept, True

        symbols: list[dict] = []
        if f["analyzable"] and review.job.state == "ready":
            changes = self.changes(review)
            index = review.job.index
            for sym in index.symbols_in_file(f["path"]) if f["status"] != "deleted" else []:
                change = changes.get(sym.id)
                if sym.kind == "module" and not change:
                    continue
                symbols.append(
                    {
                        "id": sym.id,
                        "qual": sym.qual,
                        "name": sym.name,
                        "kind": sym.kind,
                        "start": sym.start,
                        "end": sym.end,
                        "change": change["status"] if change else None,
                        "callers": len(index.inn.get(sym.id, ())),
                        "callees": len(index.out.get(sym.id, ())),
                    }
                )
            for change in changes.values():
                if change["path"] == f["path"] and change["status"] == "deleted":
                    symbols.append(
                        {
                            "id": change["id"], "qual": change["qual"], "name": change["name"],
                            "kind": change["kind"], "start": change["start"], "end": change["end"],
                            "change": "deleted", "callers": 0, "callees": 0,
                        }
                    )
        return {"file": f, "diff": parsed, "truncated": truncated, "symbols": symbols}

    def source(self, review: Review, path: str, start: int, end: int, side: str = "head") -> dict:
        ref = review.head_sha if side == "head" else review.diff_base
        content = self.repo.show_file(ref, path)
        if content is None:
            raise ReviewError(f"{path} does not exist at {side}")
        lines = content.decode(errors="replace").splitlines()
        start = max(1, start)
        end = min(len(lines), max(start, end))
        return {"path": path, "start": start, "end": end, "lines": lines[start - 1 : end], "total": len(lines)}

    # -- graphs -------------------------------------------------------------

    def graph_query(self, review: Review, **options) -> GraphQuery:
        index = self.require_index(review)
        return GraphQuery(
            index,
            self.category_of,
            self.changes(review),
            {f["path"] for f in review.files},
            **options,
        )
