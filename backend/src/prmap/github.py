"""GitHub PR lookups through the user's authenticated ``gh`` CLI."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from prmap.gitrepo import GitError, GitRepo

PR_FIELDS = "number,title,url,author,state,isDraft,baseRefName,headRefName,baseRefOid,headRefOid,body,updatedAt"
LIST_FIELDS = "number,title,url,author,isDraft,baseRefName,headRefName,updatedAt"

_PR_URL = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")


class GitHubError(RuntimeError):
    pass


def gh_available() -> bool:
    return shutil.which("gh") is not None


_NO_GITHUB_REMOTE = (
    "This repository has no GitHub remote, so its pull requests can't be looked up. "
    "Compare branches instead, or paste a full PR URL from a repo you have cloned here."
)
# gh stderr fragments -> messages the UI can show as-is.
_GH_ERRORS = {
    "No git remotes found": _NO_GITHUB_REMOTE,
    "none of the git remotes configured": _NO_GITHUB_REMOTE,
    "gh auth login": "The GitHub CLI is not logged in. Run `gh auth login` in a terminal, then try again.",
    "set-default": "This clone has several GitHub remotes. Run `gh repo set-default` in it to pick the main one.",
}


def _gh(root: Path, *args: str) -> str:
    if not gh_available():
        raise GitHubError("The GitHub CLI (gh) is not installed. Install it and run `gh auth login`.")
    proc = subprocess.run(["gh", *args], cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:
        err = proc.stderr.strip()
        friendly = next((msg for frag, msg in _GH_ERRORS.items() if frag in err), None)
        raise GitHubError(friendly or err or f"gh {' '.join(args)} failed")
    return proc.stdout


def normalize_remote(url: str) -> str | None:
    """'git@github.com:Owner/Repo.git' -> 'owner/repo'."""
    match = re.search(r"github\.com[:/]+([^/]+)/([^/\s]+?)(?:\.git)?/?$", url.strip())
    return f"{match.group(1)}/{match.group(2)}".lower() if match else None


def parse_pr_input(value: str) -> str:
    value = value.strip()
    if _PR_URL.search(value):
        return value
    digits = value.lstrip("#")
    if digits.isdigit():
        return digits
    raise GitHubError("Enter a PR number (e.g. 123) or a GitHub pull request URL.")


def list_prs(repo: GitRepo, limit: int = 40) -> list[dict]:
    out = _gh(repo.root, "pr", "list", "--state", "open", "--limit", str(limit), "--json", LIST_FIELDS)
    return json.loads(out)


def view_pr(repo: GitRepo, pr: str) -> dict:
    value = parse_pr_input(pr)
    try:
        out = _gh(repo.root, "pr", "view", value, "--json", PR_FIELDS)
    except GitHubError as exc:
        if "Could not resolve to a PullRequest" in str(exc):
            label = value if value.startswith("http") else f"#{value}"
            raise GitHubError(f"Pull request {label} was not found on GitHub. Check the number or URL.") from exc
        raise
    return json.loads(out)


def fetch_pr(repo: GitRepo, pr: dict) -> tuple[str, str]:
    """Fetch the PR head and base into ``refs/prmap/`` and return (base_sha, head_sha).

    Open PRs are compared against the current tip of their base branch (like GitHub's
    "Files changed"). Merged/closed PRs use ``baseRefOid``, the base tip when they were
    merged or closed: the branch has since absorbed the PR (or been deleted/renamed).
    """
    match = _PR_URL.search(pr["url"])
    if not match:
        raise GitHubError(f"Unexpected PR url: {pr['url']}")
    owner_repo = f"{match.group(1)}/{match.group(2)}".lower()
    number = pr["number"]

    remote = next((name for name, url in repo.remotes().items() if normalize_remote(url) == owner_repo), None)
    if remote is None:
        raise GitHubError(
            f"PR #{number} belongs to {owner_repo}, but no git remote in {repo.root} points to it. "
            f"Run prmap inside a clone of {owner_repo}, or add it with "
            f"`git remote add upstream https://github.com/{owner_repo}.git`."
        )

    head_ref = f"refs/prmap/pr/{number}/head"
    base_ref = f"refs/prmap/pr/{number}/base"
    base_oid = pr.get("baseRefOid") or ""
    try:
        repo.fetch_ref(remote, f"refs/pull/{number}/head", head_ref)
    except GitError as exc:
        raise GitHubError(
            f"Could not fetch PR #{number} from remote '{remote}'. Check your network and that "
            f"`git fetch {remote}` works. ({exc})"
        ) from exc

    if pr.get("state") == "OPEN" or not base_oid:
        try:
            repo.fetch_ref(remote, f"refs/heads/{pr['baseRefName']}", base_ref)
            return repo.rev_parse(base_ref), repo.rev_parse(head_ref)
        except GitError as exc:
            if not base_oid:
                raise GitHubError(
                    f"Could not fetch base branch '{pr['baseRefName']}' of PR #{number} from '{remote}': {exc}"
                ) from exc
    try:
        if repo.has_commit(base_oid):
            repo.run("update-ref", base_ref, base_oid)
        else:
            repo.fetch_ref(remote, base_oid, base_ref)
    except GitError as exc:
        raise GitHubError(
            f"Could not fetch the base commit {base_oid[:12]} of PR #{number} from '{remote}': {exc}"
        ) from exc
    return repo.rev_parse(base_ref), repo.rev_parse(head_ref)
