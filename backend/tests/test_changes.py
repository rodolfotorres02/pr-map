"""Changed-symbol detection on renames, odd paths and index-excluded files (bulk diff path)."""

from __future__ import annotations

import random
import time

import pytest

from conftest import _git, _write
from prmap.analysis import MODULE
from prmap.gitrepo import GitRepo, parse_zero_context_diff
from prmap.indexer import ParseCache
from prmap.review import ReviewError, ReviewManager, _count_innermost, _innermost

ODD = 'web/odd [id] "q" é.ts'

ENGINE_BASE = """
    def alpha(x):
        total = 0
        for i in range(x):
            total += i
        return total


    def beta(x):
        return x * 2


    def gamma(x):
        return x - 1


    class Motor:
        def start(self):
            self.running = True
            return self

        def stop(self):
            self.running = False
            return self
    """

ENGINE_HEAD = """
    def alpha(x):
        total = 0
        for i in range(x):
            total += i
        return total


    def beta(x):
        return x * 3


    class Motor:
        def start(self):
            self.running = True
            return self

        def stop(self):
            self.running = False
            return self


    def delta(x):
        return beta(x) + alpha(x)
    """


@pytest.fixture(scope="module")
def rename_review(tmp_path_factory):
    root = tmp_path_factory.mktemp("renames")
    _git(root, "init", "-q", "-b", "main")
    _write(root, {
        "pkg/__init__.py": "",
        "pkg/engine.py": ENGINE_BASE,
        "pkg/gone.py": "def obsolete():\n    return 1\n",
        "assets/vendor.min.js": "function a(){return 1}function n(){return 2}function i(){return 3}\n",
        ODD: "export function f() {\n  return 1;\n}\n",
    })
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    _git(root, "mv", "pkg/engine.py", "pkg/engine_v2.py")
    _git(root, "rm", "-q", "pkg/gone.py")
    _write(root, {
        "pkg/engine_v2.py": ENGINE_HEAD,
        "assets/vendor.min.js": "function x(){return 1}function y(){return 2}\n",
        ODD: "export function f() {\n  return 2;\n}\n\nexport function g() {\n  return f();\n}\n",
    })
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "feature")

    manager = ReviewManager(GitRepo(root), cache=ParseCache(tmp_path_factory.mktemp("cache2") / "c.sqlite"))
    review = manager.open_branches("main", "feature")
    for _ in range(200):
        if review.job.state in ("ready", "error"):
            break
        time.sleep(0.05)
    assert review.job.state == "ready", review.job.error
    return manager, review


def test_rename_with_modifications(rename_review):
    manager, review = rename_review
    files = {f["path"]: f for f in review.files}
    assert files["pkg/engine_v2.py"]["status"] == "renamed"
    assert files["pkg/engine_v2.py"]["old_path"] == "pkg/engine.py"

    status = {sid: c["status"] for sid, c in manager.changes(review).items()}
    assert status["pkg/engine_v2.py::beta"] == "modified"
    assert status["pkg/engine_v2.py::delta"] == "added"
    assert status["pkg/engine_v2.py::gamma"] == "deleted"
    assert "pkg/engine_v2.py::alpha" not in status
    assert "pkg/engine_v2.py::Motor.start" not in status
    assert status["pkg/gone.py::obsolete"] == "deleted"

    detail = manager.file_detail(review, "pkg/engine_v2.py")
    by_qual = {s["qual"]: s["change"] for s in detail["symbols"]}
    assert by_qual["beta"] == "modified" and by_qual["gamma"] == "deleted" and by_qual["alpha"] is None


def test_odd_path_is_diffed(rename_review):
    manager, review = rename_review
    status = {sid: c["status"] for sid, c in manager.changes(review).items()}
    assert status[f"{ODD}::f"] == "modified"
    assert status[f"{ODD}::g"] == "added"
    detail = manager.file_detail(review, ODD)  # brackets in the path must not act as a glob
    assert [line["text"] for h in detail["diff"]["hunks"] for line in h["lines"] if line["type"] == "add"][0] == "  return 2;"


def test_index_excluded_files_report_no_symbols(rename_review):
    manager, review = rename_review
    files = {f["path"]: f for f in review.files}
    assert files["assets/vendor.min.js"]["status"] == "modified"
    assert files["assets/vendor.min.js"]["analyzable"] is False
    assert not [sid for sid in manager.changes(review) if sid.startswith("assets/vendor.min.js")]


def test_count_innermost_matches_per_line_scan():
    rng = random.Random(7)
    for _ in range(300):
        spans = []
        for k in range(rng.randint(0, 12)):
            start = rng.randint(1, 80)
            spans.append((start, start + rng.randint(-2, 30), f"s{k}" if rng.random() > 0.1 else MODULE))
        ranges, line = [], 1
        while line < 120:
            line += rng.randint(0, 15)
            length = rng.randint(1, 10)
            ranges.append((line, length))
            line += length
        expected: dict[str, int] = {}
        for first, length in ranges:
            for n in range(first, first + length):
                qual = _innermost(spans, n)
                expected[qual] = expected.get(qual, 0) + 1
        assert _count_innermost(spans, ranges) == expected


def test_zero_context_parser_ignores_header_lookalikes_in_bodies():
    out = (
        b'diff --git "a/x \\"y\\".py" "b/x \\"y\\".py"\n'
        b"index 1..2 100644\n"
        b'--- "a/x \\"y\\".py"\n'
        b'+++ "b/x \\"y\\".py"\n'
        b"@@ -3 +3,2 @@ def f():\n"
        b"--- not a header\n"
        b"+++ not a header either\n"
        b"+diff --git a/z b/z\n"
        b"diff --git a/sp ace.py b/sp ace.py\n"
        b"--- a/sp ace.py\t\n"
        b"+++ b/sp ace.py\t\n"
        b"@@ -10,0 +11 @@\n"
        b"+x\n"
    )
    hunks = parse_zero_context_diff(out)
    assert hunks['x "y".py'].deleted == [(3, 1)] and hunks['x "y".py'].added == [(3, 2)]
    assert hunks["sp ace.py"].deleted == [] and hunks["sp ace.py"].added == [(11, 1)]


def test_new_branch_with_only_uncommitted_work(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _write(root, {"app.py": "def f():\n    return 1\n"})
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    _write(root, {"app.py": "def f():\n    return 2\n"})

    manager = ReviewManager(GitRepo(root), cache=ParseCache(tmp_path / "c.sqlite"))
    with pytest.raises(ReviewError, match="same commit.*uncommitted changes aren't included"):
        manager.open_branches("main", "feature")

    review = manager.open_branches("main", "feature", uncommitted=True)
    assert review.uncommitted and review.commits == 0
    assert review.title == "feature + uncommitted → main" and review.head_label == "feature"
    assert [(f["path"], f["status"]) for f in review.files] == [("app.py", "modified")]
    assert manager.open_branches("main", "feature", uncommitted=True) is review
    assert _git(root, "status", "--porcelain").strip() == "M app.py"


def test_snapshot_worktree_leaves_index_and_refs_alone(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _write(root, {"a.py": "a = 1\n", "b.py": "b = 1\n", "gone.py": "x = 1\n", ".gitignore": "*.log\n"})
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    repo = GitRepo(root)
    head = repo.rev_parse("HEAD")
    assert repo.snapshot_worktree() == head  # nothing uncommitted

    _write(root, {"a.py": "a = 2\n", "b.py": "b = 2\n", "new.py": "n = 1\n", "debug.log": "noise\n"})
    _git(root, "add", "a.py")  # staged; b.py stays unstaged, new.py untracked, debug.log ignored
    (root / "gone.py").unlink()
    status = _git(root, "status", "--porcelain")
    refs = _git(root, "for-each-ref")
    index = (root / ".git" / "index").read_bytes()
    assert repo.uncommitted_counts() == (3, 1)

    snap = repo.snapshot_worktree()
    assert snap != head and repo.rev_parse(f"{snap}^") == head
    changed = {c.path: c.status for c in repo.changed_files(head, snap)}
    assert changed == {"a.py": "modified", "b.py": "modified", "gone.py": "deleted"}
    assert repo.snapshot_worktree() == snap  # same tree on the same HEAD: same commit
    with_new = repo.snapshot_worktree(untracked=True)
    assert {c.path for c in repo.changed_files(head, with_new)} == {"a.py", "b.py", "gone.py", "new.py"}
    assert (root / ".git" / "index").read_bytes() == index
    assert _git(root, "for-each-ref") == refs
    assert _git(root, "status", "--porcelain") == status
