"""Parse ``git diff`` unified output for a single file into hunks."""

from __future__ import annotations

import re

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")


def parse_unified_diff(text: str) -> dict:
    hunks: list[dict] = []
    binary = False
    current: dict | None = None
    old_no = new_no = 0

    for line in text.splitlines():
        match = _HUNK.match(line)
        if match:
            old_no, new_no = int(match.group(1)), int(match.group(3))
            current = {
                "header": line,
                "section": match.group(5).strip(),
                "old_start": old_no,
                "old_lines": int(match.group(2) or 1),
                "new_start": new_no,
                "new_lines": int(match.group(4) or 1),
                "lines": [],
            }
            hunks.append(current)
            continue
        if current is None:
            if line.startswith("Binary files"):
                binary = True
            continue
        if line.startswith("\\"):  # "\ No newline at end of file"
            continue
        tag, body = line[:1], line[1:]
        if tag == "+":
            current["lines"].append({"type": "add", "old": None, "new": new_no, "text": body})
            new_no += 1
        elif tag == "-":
            current["lines"].append({"type": "del", "old": old_no, "new": None, "text": body})
            old_no += 1
        else:
            current["lines"].append({"type": "ctx", "old": old_no, "new": new_no, "text": body})
            old_no += 1
            new_no += 1

    return {"binary": binary, "hunks": hunks}


def changed_line_numbers(parsed: dict) -> tuple[set[int], set[int]]:
    """Return (added lines in the new file, deleted lines in the old file)."""
    added: set[int] = set()
    deleted: set[int] = set()
    for hunk in parsed["hunks"]:
        for line in hunk["lines"]:
            if line["type"] == "add":
                added.add(line["new"])
            elif line["type"] == "del":
                deleted.add(line["old"])
    return added, deleted
