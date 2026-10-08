"""Read-only access to a local git repository.

Everything here reads from the object database (commits, trees, blobs), so the
user's working tree and index are never touched. The only write operation is
`fetch_ref`, which stores fetched PR commits under ``refs/prmap/``.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class GitError(RuntimeError):
    pass


@dataclass
class ChangedFile:
    path: str
    old_path: str | None  # set for renames/copies
    status: str  # added | modified | deleted | renamed | copied | typechange
    additions: int
    deletions: int
    binary: bool
    old_sha: str | None = None  # blob at the base side (None when added)
    new_sha: str | None = None  # blob at the head side (None when deleted)
    old_mode: str | None = None
    new_mode: str | None = None


@dataclass
class FileHunks:
    """Changed line ranges of one file from a ``-U0`` diff: (first_line, count) per side."""

    path: str
    old_path: str | None
    deleted: list[tuple[int, int]] = field(default_factory=list)
    added: list[tuple[int, int]] = field(default_factory=list)


_NULL_SHA = "0" * 40

# Options shared by every diff we run, so rename pairing is identical across calls and
# immune to user config (external diff drivers, textconv, colour, custom prefixes).
_DIFF_OPTS = ("--no-color", "--no-ext-diff", "--no-textconv", "-M")

_HEADER_LINE = re.compile(rb"^(?:diff --git |@@ |rename from |rename to |--- |\+\+\+ ).*$", re.M)
_HUNK_HEADER = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_ESCAPES = {ord("a"): 7, ord("b"): 8, ord("t"): 9, ord("n"): 10, ord("v"): 11, ord("f"): 12, ord("r"): 13}


def unquote_path(raw: bytes) -> bytes:
    """Undo git's C-style path quoting (``"a/sp\\"ace"`` -> ``a/sp"ace``)."""
    if len(raw) < 2 or raw[:1] != b'"' or raw[-1:] != b'"':
        return raw
    body, out, i = raw[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch != 0x5C or i + 1 >= len(body):  # backslash
            out.append(ch)
            i += 1
            continue
        nxt, octal = body[i + 1], body[i + 1 : i + 4]
        if len(octal) == 3 and all(0x30 <= c <= 0x37 for c in octal):
            out.append(int(octal, 8) & 0xFF)
            i += 4
            continue
        out.append(_ESCAPES.get(nxt, nxt))
        i += 2
    return bytes(out)


def _header_path(rest: bytes, prefix: bytes) -> bytes | None:
    """Path from a ``--- a/x`` / ``+++ b/x`` line (``rest`` excludes the marker)."""
    if rest.endswith(b"\t"):  # git appends a tab when the name contains a space
        rest = rest[:-1]
    if rest == b"/dev/null":
        return None
    path = unquote_path(rest)
    return path[len(prefix):] if path.startswith(prefix) else path


def _git_header_paths(rest: bytes) -> tuple[bytes, bytes] | None:
    """Split ``a/x b/y`` from a ``diff --git`` line; None when ambiguous."""
    if rest.startswith(b'"'):
        end = 1
        while end < len(rest):
            if rest[end] == 0x5C:
                end += 2
                continue
            if rest[end] == 0x22:
                break
            end += 1
        a, b = rest[: end + 1], rest[end + 2 :]
        a, b = unquote_path(a), unquote_path(b)
    else:
        # Unquoted "a/P b/P": only unambiguous when both halves are equal.
        n = (len(rest) - 5) // 2
        if n <= 0 or len(rest) != 2 * n + 5 or rest[2 : 2 + n] != rest[5 + n :]:
            return None
        return rest[2 : 2 + n], rest[2 : 2 + n]
    if not (a.startswith(b"a/") and b.startswith(b"b/")):
        return None
    return a[2:], b[2:]


def parse_zero_context_diff(out: bytes) -> dict[str, FileHunks]:
    """Parse a multi-file ``git diff -U0`` into changed line ranges keyed by new path.

    Only header lines are inspected (body lines are skipped by the regex scan), so
    this stays fast on diffs with hundreds of thousands of changed lines.
    """
    result: dict[str, FileHunks] = {}
    current: FileHunks | None = None
    old = new = None
    in_header = False
    git_paths: tuple[bytes, bytes] | None = None

    def start_hunks() -> FileHunks | None:
        o = old if old is not None else (git_paths[0] if git_paths else None)
        n = new if new is not None else (git_paths[1] if git_paths else None)
        key = n if n is not None else o
        if key is None:
            return None
        path = key.decode(errors="replace")
        entry = result.get(path)
        if entry is None:
            old_text = o.decode(errors="replace") if o is not None and o != key else None
            entry = result[path] = FileHunks(path, old_text)
        return entry

    for match in _HEADER_LINE.finditer(out):
        line = match.group(0)
        if line.startswith(b"diff --git "):
            in_header, current, old, new = True, None, None, None
            git_paths = _git_header_paths(line[11:])
            continue
        if line.startswith(b"@@ "):
            if in_header:
                in_header = False
                current = start_hunks()
            hunk = _HUNK_HEADER.match(line)
            if current is None or hunk is None:
                continue
            o_start, o_len, n_start, n_len = hunk.groups()
            o_len = 1 if o_len is None else int(o_len)
            n_len = 1 if n_len is None else int(n_len)
            if o_len:
                current.deleted.append((int(o_start), o_len))
            if n_len:
                current.added.append((int(n_start), n_len))
            continue
        if not in_header:
            continue  # a body line that happens to look like a header ("--- x" deleted as "-- x")
        if line.startswith(b"rename from "):
            old = unquote_path(line[12:])
        elif line.startswith(b"rename to "):
            new = unquote_path(line[10:])
        elif line.startswith(b"--- "):
            path = _header_path(line[4:], b"a/")
            if path is not None and old is None:
                old = path
        elif line.startswith(b"+++ "):
            path = _header_path(line[4:], b"b/")
            if path is not None and new is None:
                new = path
    return result


_STATUS = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "typechange",
}


class GitRepo:
    def __init__(self, path: str | Path):
        start = Path(path).expanduser().resolve()
        try:
            top = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=start,
                capture_output=True,
                check=True,
                text=True,
            ).stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise GitError(f"{start} is not inside a git repository") from exc
        self.root = Path(top)

    # -- plumbing ---------------------------------------------------------

    def run(self, *args: str, input: bytes | None = None, check: bool = True) -> bytes:
        proc = subprocess.run(
            ["git", *args], cwd=self.root, input=input, capture_output=True
        )
        if check and proc.returncode != 0:
            msg = proc.stderr.decode(errors="replace").strip()
            raise GitError(f"git {' '.join(args)} failed: {msg}")
        return proc.stdout

    def text(self, *args: str) -> str:
        return self.run(*args).decode(errors="replace")

    # -- refs ---------------------------------------------------------------

    def rev_parse(self, ref: str) -> str:
        return self.text("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()

    def has_commit(self, sha: str) -> bool:
        return (
            subprocess.run(
                ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
                cwd=self.root,
                capture_output=True,
            ).returncode
            == 0
        )

    def merge_base(self, a: str, b: str) -> str:
        return self.text("merge-base", a, b).strip()

    def current_branch(self) -> str | None:
        out = self.run("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        return out.decode().strip() or None

    def branches(self) -> list[dict]:
        fmt = "%(refname)%00%(refname:short)%00%(objectname)%00%(committerdate:iso-strict)"
        out = self.text("for-each-ref", f"--format={fmt}", "refs/heads", "refs/remotes")
        current = self.current_branch()
        result = []
        for line in out.splitlines():
            full, short, sha, date = line.split("\0")
            if full.endswith("/HEAD"):
                continue
            result.append(
                {
                    "name": short,
                    "sha": sha,
                    "remote": full.startswith("refs/remotes/"),
                    "current": short == current,
                    "date": date,
                }
            )
        # Local branches first, most recently committed first within each group.
        locals_ = sorted((b for b in result if not b["remote"]), key=lambda b: b["date"], reverse=True)
        remotes = sorted((b for b in result if b["remote"]), key=lambda b: b["date"], reverse=True)
        return locals_ + remotes

    def default_branch(self) -> str | None:
        out = self.run("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
        name = out.decode().strip()
        if name:
            return name
        for candidate in ("main", "master", "develop"):
            if self.run("rev-parse", "--verify", "--quiet", candidate, check=False).strip():
                return candidate
        return None

    def remotes(self) -> dict[str, str]:
        out = self.text("remote", "-v")
        result: dict[str, str] = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] not in result:
                result[parts[0]] = parts[1]
        return result

    def fetch_ref(self, remote: str, src: str, dst: str) -> None:
        self.run("fetch", "--no-tags", "--quiet", remote, f"+{src}:{dst}")

    def commit_info(self, sha: str) -> dict:
        out = self.text("show", "-s", "--format=%H%x00%an%x00%aI%x00%s", sha)
        full, author, date, subject = out.strip().split("\0", 3)
        return {"sha": full, "author": author, "date": date, "subject": subject}

    def commit_count(self, base: str, head: str) -> int:
        return int(self.text("rev-list", "--count", f"{base}..{head}").strip() or 0)

    # -- diffs --------------------------------------------------------------

    def changed_files(self, base: str, head: str) -> list[ChangedFile]:
        raw = self.run("diff", *_DIFF_OPTS, "--raw", "-z", "--no-abbrev", base, head)
        numstat = self.run("diff", *_DIFF_OPTS, "--numstat", "-z", base, head)

        files: list[ChangedFile] = []
        tokens = raw.decode(errors="replace").split("\0")
        i = 0
        while i < len(tokens):
            meta = tokens[i]
            if not meta.startswith(":"):
                i += 1
                continue
            fields = meta[1:].split()
            letter = fields[-1][0]
            old_mode, new_mode, old_sha, new_sha = (fields + [None] * 4)[:4]
            if letter in ("R", "C"):
                old, new = tokens[i + 1], tokens[i + 2]
                i += 3
            else:
                old, new = None, tokens[i + 1]
                i += 2
            files.append(
                ChangedFile(
                    path=new,
                    old_path=old,
                    status=_STATUS.get(letter, "modified"),
                    additions=0,
                    deletions=0,
                    binary=False,
                    old_sha=None if old_sha in (None, _NULL_SHA) else old_sha,
                    new_sha=None if new_sha in (None, _NULL_SHA) else new_sha,
                    old_mode=old_mode,
                    new_mode=new_mode,
                )
            )

        # numstat -z: "<add>\t<del>\t<path>\0" or, for renames, "<add>\t<del>\t\0<old>\0<new>\0"
        stats: dict[str, tuple[int, int, bool]] = {}
        parts = numstat.decode(errors="replace").split("\0")
        j = 0
        while j < len(parts):
            entry = parts[j]
            if not entry:
                j += 1
                continue
            add, delete, path = entry.split("\t", 2)
            if path == "":
                path = parts[j + 2]
                j += 3
            else:
                j += 1
            binary = add == "-"
            stats[path] = (0 if binary else int(add), 0 if binary else int(delete), binary)

        for f in files:
            add, delete, binary = stats.get(f.path, (0, 0, False))
            f.additions, f.deletions, f.binary = add, delete, binary
        return files

    def file_diff(
        self,
        base: str,
        head: str,
        path: str,
        old_path: str | None = None,
        context: int = 3,
        max_lines: int | None = None,
    ) -> str:
        """Unified diff of one file. With ``max_lines``, stop reading git's output after that
        many lines so a pathological file (minified bundle, giant snapshot) stays cheap."""
        paths = [path] if not old_path or old_path == path else [old_path, path]
        args = [
            "--literal-pathspecs", "diff", *_DIFF_OPTS, "--src-prefix=a/", "--dst-prefix=b/",
            f"-U{context}", base, head, "--", *paths,
        ]
        if max_lines is None:
            return self.run(*args).decode(errors="replace")
        proc = subprocess.Popen(["git", *args], cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        lines: list[bytes] = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.append(line)
                if len(lines) >= max_lines:
                    break
        finally:
            proc.kill()
            proc.wait()
        return b"".join(lines).decode(errors="replace")

    def zero_context_hunks(self, base: str, head: str) -> dict[str, FileHunks]:
        """Changed line ranges for every modified/renamed file between two commits, from
        ONE ``git diff -U0``. Added and deleted files are filtered out (their ranges are
        simply the whole file: use the numstat counts), which keeps the output small."""
        out = self.run(
            "-c", "core.quotepath=off", "diff", *_DIFF_OPTS, "-U0", "--src-prefix=a/", "--dst-prefix=b/",
            "--diff-filter=ad", base, head,
        )
        return parse_zero_context_diff(out)

    def blob_sizes(self, shas: list[str]) -> dict[str, int]:
        if not shas:
            return {}
        out = self.run("cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                       input=("\n".join(shas) + "\n").encode())
        sizes: dict[str, int] = {}
        for line in out.decode(errors="replace").splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[1] == "blob":
                sizes[parts[0]] = int(parts[2])
        return sizes

    # -- trees and blobs ----------------------------------------------------

    def ls_tree(self, ref: str) -> list[tuple[str, str, int]]:
        """Return (path, blob_sha, size) for every regular file at ``ref``."""
        out = self.run("ls-tree", "-r", "-z", "--long", "--full-tree", ref)
        result = []
        for entry in out.split(b"\0"):
            if not entry:
                continue
            meta, path = entry.split(b"\t", 1)
            mode, kind, sha, size = meta.split()
            if kind != b"blob" or mode == b"120000":  # skip symlinks
                continue
            result.append((path.decode(errors="replace"), sha.decode(), int(size)))
        return result

    def read_blobs(self, shas: list[str], chunk: int = 400) -> dict[str, bytes]:
        result: dict[str, bytes] = {}
        for start in range(0, len(shas), chunk):
            batch = shas[start : start + chunk]
            out = self.run("cat-file", "--batch", input=("\n".join(batch) + "\n").encode())
            pos = 0
            for _ in batch:
                header_end = out.index(b"\n", pos)
                header = out[pos:header_end].split()
                pos = header_end + 1
                if len(header) < 3 or header[1] == b"missing":
                    continue
                size = int(header[2])
                result[header[0].decode()] = out[pos : pos + size]
                pos += size + 1
        return result

    def show_file(self, ref: str, path: str) -> bytes | None:
        out = self.run("show", f"{ref}:{path}", check=False)
        return out if out or self._exists(ref, path) else None

    def _exists(self, ref: str, path: str) -> bool:
        return bool(self.run("ls-tree", "--name-only", ref, "--", path, check=False).strip())
