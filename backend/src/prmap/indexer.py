"""Build a CodeIndex for a commit, caching per-blob parse results on disk."""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import sys
import posixpath
import re
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pathspec

from prmap.analysis import PARSER_VERSION, analyze_file, analyze_many
from prmap.analysis.index import CodeIndex
from prmap.classify import ANALYZABLE, language_of
from prmap.config import RepoConfig
from prmap.gitrepo import GitRepo

Progress = Callable[[int, int], None]

log = logging.getLogger(__name__)

_POOL_THRESHOLD = 150
_CHUNK = 40


def cache_dir() -> Path:
    base = os.environ.get("PRMAP_CACHE_DIR") or os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    path = Path(base) / "prmap"
    path.mkdir(parents=True, exist_ok=True)
    return path


class ParseCache:
    """Parsed file analyses keyed by (parser version, language, blob sha)."""

    def __init__(self, path: Path | None = None):
        self.path = path or cache_dir() / "parse-cache.sqlite"
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("CREATE TABLE IF NOT EXISTS parse (key TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self._conn.commit()

    @staticmethod
    def key(language: str, blob: str) -> str:
        return f"{PARSER_VERSION}:{language}:{blob}"

    def get_many(self, keys: list[str]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        with self._lock:
            for start in range(0, len(keys), 500):
                chunk = keys[start : start + 500]
                marks = ",".join("?" * len(chunk))
                for key, data in self._conn.execute(f"SELECT key, data FROM parse WHERE key IN ({marks})", chunk):
                    result[key] = json.loads(data)
        return result

    def put_many(self, items: dict[str, dict]) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO parse (key, data) VALUES (?, ?)",
                [(k, json.dumps(v, separators=(",", ":"))) for k, v in items.items()],
            )
            self._conn.commit()


_JSONC_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.S)


def parse_jsonc(text: str) -> dict:
    """Parse tsconfig-style JSON with comments and trailing commas."""
    stripped = _JSONC_TOKEN.sub(lambda m: m.group(0) if m.group(0).startswith('"') else "", text)
    stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
    try:
        data = json.loads(stripped)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def analyze_blobs(
    repo: GitRepo,
    files: list[tuple[str, str, str]],
    cache: ParseCache,
    progress: Progress | None = None,
) -> dict[str, dict]:
    """Analyse (path, blob_sha, language) triples, using and filling the cache."""
    keys = {path: cache.key(lang, blob) for path, blob, lang in files}
    cached = cache.get_many(list(set(keys.values())))
    results: dict[str, dict] = {}
    missing: list[tuple[str, str, str]] = []
    for path, blob, lang in files:
        hit = cached.get(keys[path])
        if hit is not None:
            results[path] = {**hit, "path": path}
        else:
            missing.append((path, blob, lang))

    total = len(files)
    done = len(results)
    if progress:
        progress(done, total)
    if not missing:
        return results

    blobs = repo.read_blobs(sorted({blob for _, blob, _ in missing}))
    work = [(path, lang, blobs.get(blob, b"")) for path, blob, lang in missing]
    fresh: dict[str, dict] = {}

    def collect(batch_results: list[dict]) -> None:
        nonlocal done
        for analysis in batch_results:
            results[analysis["path"]] = analysis
            fresh[keys[analysis["path"]]] = analysis
        done += len(batch_results)
        if progress:
            progress(done, total)

    chunks = [work[i : i + _CHUNK] for i in range(0, len(work), _CHUNK)]
    finished = 0
    if len(work) >= _POOL_THRESHOLD and (os.cpu_count() or 1) > 1:
        try:
            with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 2), mp_context=_mp_context()) as pool:
                for batch in pool.map(analyze_many, chunks):
                    collect(batch)
                    finished += 1
        except Exception as exc:  # broken pool, spawn/import failure, pickling...: finish in-process
            log.warning("parallel parsing failed (%s: %s); continuing sequentially", type(exc).__name__, exc)
    for chunk in chunks[finished:]:
        collect([_analyze_safely(p, lang, src) for p, lang, src in chunk])

    cache.put_many({key: analysis for key, analysis in fresh.items() if not analysis.get("_failed")})
    return results


def _mp_context():
    # Never fork from the (multi-threaded) server process; spawn is the macOS/Windows default
    # and forkserver avoids re-importing __main__ on Linux.
    if sys.platform.startswith("linux") and "forkserver" in multiprocessing.get_all_start_methods():
        return multiprocessing.get_context("forkserver")
    return multiprocessing.get_context("spawn")


def _analyze_safely(path: str, language: str, source: bytes) -> dict:
    try:
        return analyze_file(path, language, source)
    except Exception as exc:  # one unparsable file must not sink the whole index
        log.warning("could not analyse %s: %s", path, exc)
        return {**analyze_file(path, language, b""), "_failed": True}


def index_exclude(config: RepoConfig) -> pathspec.PathSpec:
    return pathspec.PathSpec.from_lines("gitignore", config.index_exclude)


def build_index(
    repo: GitRepo,
    ref: str,
    config: RepoConfig,
    cache: ParseCache,
    progress: Progress | None = None,
) -> CodeIndex:
    entries = repo.ls_tree(ref)
    exclude = index_exclude(config)

    targets: list[tuple[str, str, str]] = []
    ts_configs: list[tuple[str, str]] = []
    package_files: list[tuple[str, str]] = []
    for path, blob, size in entries:
        if exclude.match_file(path):
            continue
        name = posixpath.basename(path)
        if re.fullmatch(r"(tsconfig[\w.-]*|jsconfig)\.json", name):
            ts_configs.append((path, blob))
            continue
        if name == "package.json":
            package_files.append((path, blob))
            continue
        language = language_of(path)
        if language in ANALYZABLE and size <= config.max_file_bytes:
            targets.append((path, blob, language))

    analyses = analyze_blobs(repo, targets, cache, progress)

    meta_files = ts_configs + package_files
    meta_blobs = repo.read_blobs([blob for _, blob in meta_files]) if meta_files else {}
    configs = {path: parse_jsonc(meta_blobs.get(blob, b"").decode(errors="replace")) for path, blob in ts_configs}
    packages: dict[str, str] = {}
    for path, blob in package_files:
        name = parse_jsonc(meta_blobs.get(blob, b"").decode(errors="replace")).get("name")
        if isinstance(name, str) and name:
            packages.setdefault(name, posixpath.dirname(path))
    return CodeIndex(analyses, configs, packages)
