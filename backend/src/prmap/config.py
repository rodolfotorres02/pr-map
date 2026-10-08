"""Optional per-repository settings read from ``.prmap.toml`` at the repo root.

Example::

    [categories]
    test = ["src/testing/**", "**/factories.py"]
    generated = ["src/api/client/**"]

    [index]
    exclude = ["legacy/**"]
    max_file_kb = 512
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_INDEX_EXCLUDE = [
    "node_modules/",
    "bower_components/",
    "vendor/",
    "dist/",
    "build/",
    ".next/",
    "coverage/",
    "*.min.js",
    "*.bundle.js",
    "*.d.ts",
    "site-packages/",
    ".venv/",
    "venv/",
]


@dataclass
class RepoConfig:
    category_overrides: dict[str, list[str]] = field(default_factory=dict)
    index_exclude: list[str] = field(default_factory=lambda: list(DEFAULT_INDEX_EXCLUDE))
    max_file_bytes: int = 512 * 1024


def load_config(root: Path) -> RepoConfig:
    config = RepoConfig()
    path = root / ".prmap.toml"
    if not path.exists():
        return config
    data = tomllib.loads(path.read_text())
    categories = data.get("categories", {})
    config.category_overrides = {k: list(v) for k, v in categories.items() if isinstance(v, list)}
    index = data.get("index", {})
    config.index_exclude += list(index.get("exclude", []))
    if "max_file_kb" in index:
        config.max_file_bytes = int(index["max_file_kb"]) * 1024
    return config
