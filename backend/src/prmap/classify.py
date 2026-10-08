"""Assign every file a review *category* (its role) and a *language*.

Category and extension are deliberately independent: "only HTML and JS" is an
extension filter, "logic without tests" is a category filter, and the UI lets
the reviewer combine both.
"""

from __future__ import annotations

import posixpath
import re

import pathspec

CATEGORIES: list[tuple[str, str]] = [
    ("source", "Logic"),
    ("test", "Tests"),
    ("template", "Templates"),
    ("style", "Styles"),
    ("config", "Config & build"),
    ("migration", "Migrations"),
    ("docs", "Docs"),
    ("generated", "Generated & locks"),
    ("asset", "Assets"),
    ("other", "Other"),
]
CATEGORY_KEYS = {key for key, _ in CATEGORIES}

LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "jsx",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".html": "html",
    ".htm": "html",
    ".vue": "vue",
    ".svelte": "svelte",
    ".css": "css",
    ".scss": "scss",
    ".sass": "scss",
    ".less": "less",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".rst": "rst",
    ".sql": "sql",
    ".sh": "shell",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".rs": "rust",
}

# Languages the call-graph analyser understands.
ANALYZABLE = {"python", "javascript", "jsx", "typescript", "tsx"}

_TEMPLATE_EXT = {
    ".html", ".htm", ".jinja", ".jinja2", ".j2", ".djhtml", ".hbs", ".handlebars",
    ".mustache", ".ejs", ".pug", ".jade", ".njk", ".liquid", ".twig", ".erb", ".haml",
    ".tpl", ".mjml",
}
_STYLE_EXT = {".css", ".scss", ".sass", ".less", ".styl", ".pcss", ".postcss"}
_DOCS_EXT = {".md", ".mdx", ".rst", ".adoc", ".txt"}
_ASSET_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico", ".svg", ".bmp",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".webm", ".wav",
    ".pdf", ".zip", ".gz",
}
_CONFIG_EXT = {
    ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".properties", ".xml", ".plist", ".lock",
}
_CONFIG_NAMES = {
    "dockerfile", "makefile", "procfile", "justfile", "gemfile", "pipfile",
    "requirements.txt", "setup.py", "manage.py", "package.json", "tsconfig.json",
    ".gitignore", ".gitattributes", ".dockerignore", ".editorconfig", ".nvmrc",
    ".python-version", ".prettierrc", ".eslintrc", ".babelrc", "codeowners",
}
_GENERATED_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "pipfile.lock",
    "uv.lock", "cargo.lock", "composer.lock", "gemfile.lock", "go.sum", "bun.lockb",
}

_TEST_DIR = re.compile(
    r"(^|/)(tests?|__tests__|__mocks__|specs?|e2e|cypress|playwright|testing|fixtures|test_utils)/"
)
_TEST_FILE = re.compile(
    r"(^test_.*\.py$|.*_tests?\.py$|^conftest\.py$|.*\.(test|spec|e2e|cy)\.[cm]?[jt]sx?$|.*\.snap$|.*_test\.go$|.*Tests?\.(java|kt)$)"
)
_MIGRATION = re.compile(r"(^|/)(migrations|alembic/versions|db/migrate)/")
_GENERATED_DIR = re.compile(r"(^|/)(dist|build|vendor|node_modules|__generated__|generated|\.next|coverage)/")
_CONFIG_DIR = re.compile(r"(^|/)(\.github|\.circleci|\.gitlab|\.husky|\.vscode|deploy|k8s|helm|terraform)/")
_CONFIG_FILE = re.compile(
    r"^(\.?[\w.-]*rc(\.\w+)?|[\w.-]+\.config\.[cm]?[jt]s|docker-compose[\w.-]*|requirements[\w.-]*\.txt|\.env[\w.-]*)$"
)


def extension(path: str) -> str:
    name = posixpath.basename(path).lower()
    if "." not in name.lstrip("."):
        return ""
    return name[name.rfind(".") :]


def language_of(path: str) -> str | None:
    return LANGUAGES.get(extension(path))


def builtin_category(path: str) -> str:
    lower = path.lower()
    name = posixpath.basename(lower)
    ext = extension(lower)

    if name in _GENERATED_NAMES or re.search(r"\.min\.(js|css)$|\.map$|\.pb\.go$|_pb2\.py$", name):
        return "generated"
    if _GENERATED_DIR.search(lower):
        return "generated"
    if _TEST_FILE.match(name) or _TEST_DIR.search(lower):
        return "test"
    if _MIGRATION.search(lower) and ext in {".py", ".sql", ".rb", ".js", ".ts"}:
        return "migration"
    if ext in _TEMPLATE_EXT or (ext == ".txt" and "/templates/" in f"/{lower}"):
        return "template"
    if ext in _STYLE_EXT:
        return "style"
    if ext in _ASSET_EXT:
        return "asset"
    if name in _CONFIG_NAMES or _CONFIG_FILE.match(name) or _CONFIG_DIR.search(lower):
        return "config"
    if ext in _DOCS_EXT or lower.startswith("docs/") or name in {"license", "changelog", "authors"}:
        return "docs"
    if ext in _CONFIG_EXT:
        return "config"
    if ext in LANGUAGES or ext in {".c", ".h", ".cpp", ".hpp", ".cs", ".swift", ".scala", ".ex", ".exs", ".lua", ".r", ".dart"}:
        return "source"
    return "other"


class Classifier:
    """Built-in rules plus per-repo overrides from ``.prmap.toml``.

    ``overrides`` maps a category to gitignore-style patterns; the first
    category whose patterns match wins over the built-in rules.
    """

    def __init__(self, overrides: dict[str, list[str]] | None = None):
        self._specs: list[tuple[str, pathspec.PathSpec]] = []
        for category, patterns in (overrides or {}).items():
            if category in CATEGORY_KEYS and patterns:
                self._specs.append((category, pathspec.PathSpec.from_lines("gitignore", patterns)))

    def category(self, path: str) -> str:
        for category, spec in self._specs:
            if spec.match_file(path):
                return category
        return builtin_category(path)
