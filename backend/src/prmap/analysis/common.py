from __future__ import annotations

import re

from tree_sitter import Node

MODULE = "<module>"

_TYPE_WRAPPERS = re.compile(r"^(Optional|Annotated|Type|type|ClassVar|Final|Readonly|Partial|Required)\[(.*)\]$")
_LEADING_NAME = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")


def text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def normalize_type(raw: str) -> str | None:
    """Reduce a type annotation to the class name it most likely refers to.

    ``Optional["Order"]`` -> ``Order``, ``Repo<User> | null`` -> ``Repo``.
    """
    t = raw.strip().strip("'\"")
    for _ in range(3):
        match = _TYPE_WRAPPERS.match(t)
        if not match:
            break
        t = match.group(2).split(",")[0].strip().strip("'\"")
    parts = [p.strip() for p in re.split(r"\|", t)]
    parts = [p for p in parts if p not in {"None", "null", "undefined", ""}]
    if not parts:
        return None
    match = _LEADING_NAME.match(parts[0])
    if not match:
        return None
    name = match.group(0)
    if name in {"str", "int", "float", "bool", "bytes", "dict", "list", "set", "tuple", "Any",
                "string", "number", "boolean", "any", "unknown", "void", "object", "never",
                "Record", "Array", "Promise", "Map", "Set", "Callable", "Iterable", "Sequence"}:
        return None
    return name


class Collector:
    """Accumulates the JSON-serialisable analysis of one file."""

    def __init__(self, path: str, language: str):
        self.path = path
        self.language = language
        self.symbols: list[dict] = []
        self.calls: list[dict] = []
        self.refs: dict[str, set[str]] = {}
        self.imports: list[dict] = []
        self.types: dict[str, dict[str, str]] = {}
        self.locals: dict[str, set[str]] = {}
        self.exports: dict[str, dict] = {}
        self.star_exports: list[str] = []
        self.static_members: list[dict] = []
        self.is_module = False
        self._seen: set[str] = set()

    def symbol(self, qual: str, name: str, kind: str, node: Node, parent: str | None, **extra) -> str:
        # Duplicate definitions (e.g. property setter, overloads) keep the first one.
        if qual in self._seen:
            return qual
        self._seen.add(qual)
        self.symbols.append(
            {
                "qual": qual,
                "name": name,
                "kind": kind,
                "start": node.start_point[0] + 1,
                "end": node.end_point[0] + 1,
                "parent": parent,
                **extra,
            }
        )
        return qual

    def call(self, scope: str, kind: str, name: str, recv: str | None, node: Node) -> None:
        if name:
            self.calls.append(
                {"scope": scope, "kind": kind, "name": name, "recv": recv, "line": node.start_point[0] + 1}
            )

    def ref(self, scope: str, name: str) -> None:
        if name:
            self.refs.setdefault(scope, set()).add(name)

    def local(self, scope: str, name: str) -> None:
        if name and scope != MODULE:
            self.locals.setdefault(scope, set()).add(name)

    def var_type(self, scope: str, var: str, type_text: str) -> None:
        name = normalize_type(type_text)
        if name and var:
            self.types.setdefault(scope, {}).setdefault(var, name)

    def result(self, total_lines: int) -> dict:
        module = {
            "qual": MODULE,
            "name": self.path.rsplit("/", 1)[-1],
            "kind": "module",
            "start": 1,
            "end": max(total_lines, 1),
            "parent": None,
        }
        return {
            "path": self.path,
            "language": self.language,
            "symbols": [module, *self.symbols],
            "calls": self.calls,
            "refs": {scope: sorted(names) for scope, names in self.refs.items()},
            "imports": self.imports,
            "types": self.types,
            "locals": {scope: sorted(names) for scope, names in self.locals.items()},
            "exports": self.exports,
            "star_exports": self.star_exports,
            "static_members": self.static_members,
            "is_module": self.is_module,
        }
