"""Static analysis: per-file symbol/call extraction and repo-wide call graph."""

from __future__ import annotations

from prmap.analysis.common import MODULE
from prmap.analysis.js_parser import analyze_js
from prmap.analysis.python_parser import analyze_python

__all__ = ["MODULE", "PARSER_VERSION", "analyze_file", "analyze_many"]

# Bump when parser output changes so cached analyses are invalidated.
PARSER_VERSION = "2"


def analyze_file(path: str, language: str, source: bytes) -> dict:
    if language == "python":
        return analyze_python(path, source)
    return analyze_js(path, language, source)


def analyze_many(items: list[tuple[str, str, bytes]]) -> list[dict]:
    """Worker entry point for process pools."""
    return [analyze_file(path, lang, src) for path, lang, src in items]
