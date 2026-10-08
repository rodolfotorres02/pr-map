"""'Explain with AI': ask the local Claude Code CLI (``claude -p``) about one changed symbol.

prmap assembles the review context itself (the symbol's diff and source, its callers
and callees from the code map, and its risk signals) and pipes it to ``claude -p``.
Claude runs with no tools, no MCP servers, no skills and a short system prompt, so a
call costs cents instead of carrying Claude Code's full agent context, and it can't
touch the repository. Authentication is whatever the user's ``claude`` login uses.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
from collections.abc import AsyncIterator

from prmap.analysis import MODULE
from prmap.plan import review_plan
from prmap.review import Review, ReviewError, ReviewManager

SYSTEM_PROMPT = """\
You are a staff engineer helping a senior engineer review a large pull request.
You are given one changed symbol: its diff, its current source, and its callers and
callees from a static call graph (links marked "guess" were matched by method name only,
so they may be wrong). Write for an expert: no basics, and don't restate the diff line by line.

Answer in GitHub-flavored Markdown with exactly these sections:
### What changed
### Impact on callers
### Risks to check

Keep it under 250 words. Put identifiers in backticks. Be specific and concrete. If the
context isn't enough to judge something, say what you would need to see."""

CLI_FLAGS = [
    "-p",
    "--output-format", "stream-json",
    "--verbose",
    "--include-partial-messages",
    "--tools", "",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--no-session-persistence",
]

MAX_DIFF_LINES = 400
MAX_SOURCE_LINES = 300
MAX_NEIGHBORS = 12
MAX_CALL_SITES = 6
_LIMIT = asyncio.Semaphore(3)  # concurrent claude processes


def claude_path() -> str | None:
    return os.environ.get("PRMAP_CLAUDE_BIN") or shutil.which("claude")


def claude_command() -> list[str]:
    binary = claude_path()
    if not binary:
        raise ReviewError("Claude Code isn't installed. Install it from https://claude.com/claude-code and log in.")
    command = [binary, *CLI_FLAGS, "--system-prompt", SYSTEM_PROMPT]
    if model := os.environ.get("PRMAP_CLAUDE_MODEL"):
        command += ["--model", model]
    return command + shlex.split(os.environ.get("PRMAP_CLAUDE_ARGS", ""))


# -- context -------------------------------------------------------------------


def _render_hunks(hunks: list[dict], start: int, end: int, side: str) -> str:
    key, size_key = ("new_start", "new_lines") if side == "new" else ("old_start", "old_lines")
    lines: list[str] = []
    for hunk in hunks:
        h_start = hunk[key]
        h_end = h_start + max(hunk[size_key], 1) - 1
        if h_end < start or h_start > end:
            continue
        lines.append(hunk["header"])
        for line in hunk["lines"]:
            sign = {"add": "+", "del": "-"}.get(line["type"], " ")
            lines.append(f"{sign}{line['text']}")
        if len(lines) > MAX_DIFF_LINES:
            lines = lines[:MAX_DIFF_LINES] + ["... (diff truncated)"]
            break
    return "\n".join(lines)


def _numbered(lines: list[str], first: int) -> str:
    return "\n".join(f"{first + i:>5}  {text}" for i, text in enumerate(lines))


def build_prompt(manager: ReviewManager, review: Review, symbol_id: str) -> str:
    index = manager.require_index(review)
    changes = manager.changes(review)
    change = changes.get(symbol_id)
    sym = index.symbols.get(symbol_id)
    if sym is None and change is None:
        raise ReviewError("That symbol isn't part of this review's code map.")

    path = sym.path if sym else change["path"]
    qual = sym.qual if sym else change["qual"]
    kind = sym.kind if sym else change["kind"]
    status = change["status"] if change else "unchanged"
    label = f"{path} (module-level code)" if qual == MODULE else qual
    deleted = status == "deleted"

    parts: list[str] = [f"Pull request: {review.title}"]
    parts.append(f"Comparing `{review.head_label}` into `{review.base_label}`.")
    if review.pr and review.pr.get("body"):
        parts.append("PR description:\n" + review.pr["body"].strip()[:3000])

    parts.append(f"\n## Symbol\n{kind} `{label}` in `{path}`, status: {status}")
    if change:
        parts.append(f"Lines changed in it: +{change['additions']} -{change['deletions']}")

    # Diff restricted to this symbol.
    start = change["start"] if change else sym.start
    end = change["end"] if change else sym.end
    if path in {f["path"] for f in review.files}:
        detail = manager.file_detail(review, path, context=3)
        diff = _render_hunks(detail["diff"]["hunks"], start, end, "old" if deleted else "new")
        if diff:
            parts.append(f"\n## Diff\n```diff\n{diff}\n```")

    # Source as of the PR head (or base, for deleted code).
    side = "base" if deleted else "head"
    try:
        source = manager.source(review, path, start, min(end, start + MAX_SOURCE_LINES - 1), side)
        truncated = "\n... (truncated)" if source["end"] < end else ""
        parts.append(f"\n## Source at {side}\n```\n{_numbered(source['lines'], source['start'])}{truncated}\n```")
    except ReviewError:
        pass

    if sym is not None:
        graph = manager.graph_query(review).neighborhood([symbol_id], depth_in=1, depth_out=1, max_nodes=120)
        focus = {n["id"] for n in graph["nodes"] if n["role"] == "focus"}
        by_id = {n["id"]: n for n in graph["nodes"]}
        callers = [e for e in graph["edges"] if e["target"] in focus and e["source"] not in focus]
        callees = [e for e in graph["edges"] if e["source"] in focus and e["target"] not in focus]

        def describe(edge: dict, other_id: str) -> str:
            other = by_id[other_id]
            name = f"{other['path']} module code" if other["kind"] == "module" else other["label"]
            tags = [edge["kind"]]
            if edge["fuzzy"]:
                tags.append("guess")
            if other["change"]:
                tags.append(f"{other['change']} in this PR")
            elif not other["in_pr"]:
                tags.append("outside this PR")
            return f"- `{name}` ({other['path']}:{other['line']}; {', '.join(tags)})"

        if callers:
            lines = [describe(e, e["source"]) for e in callers[:MAX_NEIGHBORS]]
            if len(callers) > MAX_NEIGHBORS:
                lines.append(f"- … and {len(callers) - MAX_NEIGHBORS} more")
            parts.append("\n## Callers (what touches it)\n" + "\n".join(lines))
            sites = _call_sites(manager, review, callers[:MAX_CALL_SITES], by_id)
            if sites:
                parts.append("Call sites:\n```\n" + "\n".join(sites) + "\n```")
        else:
            parts.append("\n## Callers\nNone found in the repository.")
        if callees:
            lines = [describe(e, e["target"]) for e in callees[:MAX_NEIGHBORS]]
            if len(callees) > MAX_NEIGHBORS:
                lines.append(f"- … and {len(callees) - MAX_NEIGHBORS} more")
            parts.append("\n## Callees (what it touches)\n" + "\n".join(lines))

        risk = (review_plan(manager, review).get("risk") or {}).get(symbol_id)
        if risk:
            tested = "yes" if risk["tested"] else "no test reaches it within two calls"
            parts.append(
                f"\n## Signals\nCallers: {risk['callers']} ({risk['external_callers']} in files outside this PR). "
                f"Tested: {tested}."
            )

    parts.append(f"\nExplain the change to `{label}` for the reviewer.")
    return "\n".join(parts)


def _call_sites(manager: ReviewManager, review: Review, edges: list[dict], by_id: dict) -> list[str]:
    sites: list[str] = []
    files: dict[str, list[str]] = {}
    for edge in edges:
        caller = by_id[edge["source"]]
        if caller["path"] not in files:
            content = manager.repo.show_file(review.head_sha, caller["path"])
            files[caller["path"]] = content.decode(errors="replace").splitlines() if content else []
        lines = files[caller["path"]]
        line_no = edge["lines"][0] if edge["lines"] else caller["line"]
        if 0 < line_no <= len(lines):
            sites.append(f"{caller['path']}:{line_no}: {lines[line_no - 1].strip()[:200]}")
    return sites


# -- streaming -------------------------------------------------------------------


async def run_claude(prompt: str, cwd: str) -> AsyncIterator[dict]:
    """Run ``claude -p`` and translate its stream-json output into simple events:

    ``{"type": "meta", "model"}``, ``{"type": "text", "text"}``,
    ``{"type": "done", "cost_usd", "duration_ms"}``, ``{"type": "error", "message"}``.
    """
    async with _LIMIT:
        try:
            proc = await asyncio.create_subprocess_exec(
                *claude_command(),
                cwd=cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=32 * 1024 * 1024,  # some stream-json lines (command lists) are large
            )
        except (OSError, ReviewError) as exc:
            yield {"type": "error", "message": f"Couldn't start Claude Code: {exc}"}
            return

        streamed = False
        finished = False
        try:
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()

            async for raw in proc.stdout:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                kind = event.get("type")
                if kind == "system" and event.get("subtype") == "init":
                    yield {"type": "meta", "model": event.get("model")}
                elif kind == "stream_event":
                    delta = (event.get("event") or {}).get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        streamed = True
                        yield {"type": "text", "text": delta["text"]}
                elif kind == "assistant" and not streamed:
                    content = (event.get("message") or {}).get("content") or []
                    text = "".join(block.get("text", "") for block in content if block.get("type") == "text")
                    if text:
                        yield {"type": "text", "text": text}
                elif kind == "result":
                    finished = True
                    if event.get("is_error"):
                        yield {"type": "error", "message": _friendly(str(event.get("result") or "Claude Code failed."))}
                    else:
                        yield {
                            "type": "done",
                            "cost_usd": event.get("total_cost_usd"),
                            "duration_ms": event.get("duration_ms"),
                        }
            await proc.wait()
            if not finished:
                stderr = (await proc.stderr.read()).decode(errors="replace").strip()
                yield {"type": "error", "message": _friendly(stderr or f"Claude Code exited with code {proc.returncode}.")}
        finally:
            if proc.returncode is None:  # client went away mid-stream
                proc.kill()
                await proc.wait()


def _friendly(message: str) -> str:
    lowered = message.lower()
    if "not logged in" in lowered or "/login" in lowered:
        return "Claude Code isn't logged in. Run `claude` in a terminal, log in, then try again."
    if "unknown option" in lowered:
        return f"Your Claude Code version doesn't support a flag prmap needs. Update it with `claude update`. ({message})"
    return message
