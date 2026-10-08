"""PR fetching against a local stand-in for GitHub (a bare repo with refs/pull/N/head)."""

from __future__ import annotations

import subprocess

import pytest

from prmap import github
from prmap.gitrepo import GitRepo
from prmap.indexer import ParseCache
from prmap.review import ReviewError, ReviewManager
from conftest import _git

URL = "https://github.com/acme/shop"


@pytest.fixture
def merged_pr(tmp_path):
    """A PR merged with a merge commit; returns (local clone, pr payload, pre-merge base sha)."""
    work, remote, local = tmp_path / "work", tmp_path / "remote.git", tmp_path / "local"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "app.py").write_text("def a():\n    return 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "checkout", "-q", "-b", "feature")
    (work / "feature.py").write_text("def b():\n    return 2\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "feature")
    _git(work, "checkout", "-q", "main")
    (work / "app.py").write_text("def a():\n    return 10\n")
    _git(work, "commit", "-q", "-am", "main moves on")
    pre_merge = _git(work, "rev-parse", "HEAD").strip()
    _git(work, "merge", "-q", "--no-ff", "-m", "Merge pull request #7", "feature")
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git(work, "push", "-q", str(remote), "main", "feature:refs/pull/7/head")
    _git(tmp_path, "clone", "-q", str(remote), str(local))
    pr = {
        "number": 7,
        "title": "Add b",
        "url": f"{URL}/pull/7",
        "state": "MERGED",
        "baseRefName": "main",
        "headRefName": "feature",
        "baseRefOid": pre_merge,
    }
    return local, pr, pre_merge


def _repo(local, monkeypatch) -> GitRepo:
    repo = GitRepo(local)
    monkeypatch.setattr(repo, "remotes", lambda: {"origin": f"{URL}.git"})
    return repo


def test_merged_pr_diffs_against_base_at_merge_time(merged_pr, monkeypatch, tmp_path):
    local, pr, pre_merge = merged_pr
    repo = _repo(local, monkeypatch)
    monkeypatch.setattr(github, "view_pr", lambda _repo, _value: pr)
    manager = ReviewManager(repo, cache=ParseCache(tmp_path / "c.sqlite"))

    review = manager.open_pr("7")

    # The base branch now contains the PR; diffing against its tip would show nothing.
    assert review.base_sha == pre_merge
    assert [f["path"] for f in review.files] == ["feature.py"]


def test_deleted_base_branch_falls_back_to_base_oid(merged_pr, monkeypatch):
    local, pr, pre_merge = merged_pr
    pr = {**pr, "state": "OPEN", "baseRefName": "renamed-away"}
    base, _head = github.fetch_pr(_repo(local, monkeypatch), pr)
    assert base == pre_merge


def test_pr_from_unknown_repo_explains_how_to_add_remote(merged_pr, monkeypatch, tmp_path):
    local, pr, _ = merged_pr
    repo = GitRepo(local)
    monkeypatch.setattr(github, "view_pr", lambda _repo, _value: {**pr, "url": "https://github.com/other/thing/pull/7"})
    manager = ReviewManager(repo, cache=ParseCache(tmp_path / "c.sqlite"))
    with pytest.raises(ReviewError, match="git remote add upstream https://github.com/other/thing.git"):
        manager.open_pr("https://github.com/other/thing/pull/7")
