"""Repo-wide symbol table and call graph built from per-file analyses.

Resolution is static and heuristic. Edges are *exact* when the target was found
through imports, local definitions, ``self``/``this``, inheritance or a known
variable type; they are *fuzzy* when only the method name matched (e.g.
``obj.process()`` with an unknown ``obj`` and a single ``process`` method in the
repo). The UI lets reviewers hide fuzzy edges.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field

from prmap.analysis.common import MODULE, normalize_type

JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
FUZZY_MAX_CANDIDATES = 3

# Method names too generic to guess a target from (builtin container/string/promise APIs).
FUZZY_STOPLIST = {
    # python
    "get", "set", "append", "extend", "pop", "items", "keys", "values", "update", "join",
    "split", "strip", "format", "replace", "startswith", "endswith", "lower", "upper", "add",
    "remove", "copy", "read", "write", "close", "open", "encode", "decode", "insert", "index",
    "count", "sort", "clear", "setdefault", "discard", "rstrip", "lstrip", "splitlines",
    "__init__", "__str__", "__repr__", "__eq__", "__hash__", "run", "send", "info", "debug",
    "warning", "error", "exception", "critical", "log", "assert_called_once_with",
    "assertEqual", "assertTrue", "assertFalse", "assertIn", "assertRaises",
    # js
    "map", "filter", "forEach", "reduce", "push", "slice", "splice", "then", "catch",
    "finally", "toString", "includes", "indexOf", "find", "findIndex", "some", "every",
    "entries", "has", "delete", "warn", "trim", "toLowerCase", "toUpperCase", "concat",
    "apply", "call", "bind", "json", "text", "emit", "on", "off", "once", "addEventListener",
    "removeEventListener", "querySelector", "querySelectorAll", "preventDefault",
    "stopPropagation", "setState", "constructor", "render", "subscribe", "unsubscribe",
    "next", "resolve", "reject", "test", "exec", "match", "parse", "stringify", "toFixed",
    "flat", "flatMap", "fill", "shift", "unshift", "reverse", "at", "assign", "freeze",
    "getItem", "setItem", "removeItem", "focus", "blur", "click", "dispatch", "use",
}

EDGE_STRENGTH = {"calls": 5, "instantiates": 5, "renders": 5, "inherits": 4, "references": 1}


@dataclass(slots=True)
class Symbol:
    id: str
    path: str
    qual: str
    name: str
    kind: str
    start: int
    end: int
    parent: str | None
    language: str
    bases: list[str] = field(default_factory=list)
    returns: str | None = None

    @property
    def label(self) -> str:
        return self.name if self.kind == "module" else self.qual


@dataclass(slots=True)
class Edge:
    src: str
    dst: str
    kind: str
    fuzzy: bool
    lines: list[int]


def family(language: str) -> str:
    return "py" if language == "python" else "js"


def symbol_id(path: str, qual: str) -> str:
    return f"{path}::{qual}"


class TsPaths:
    """Path aliases from tsconfig/jsconfig ``compilerOptions.paths`` / ``baseUrl``."""

    def __init__(self, configs: dict[str, dict]):
        # dir -> list of (base_dir, paths)
        self.by_dir: dict[str, list[tuple[str, dict[str, list[str]]]]] = {}
        for path, data in configs.items():
            options = self._merged_options(path, configs, 0)
            config_dir = posixpath.dirname(path)
            base_url = options.get("baseUrl")
            base_dir = posixpath.normpath(posixpath.join(config_dir, base_url)) if base_url else config_dir
            paths = options.get("paths") or {}
            if base_url or paths:
                self.by_dir.setdefault(config_dir, []).append(
                    (base_dir if base_dir != "." else "", paths if isinstance(paths, dict) else {})
                )

    def _merged_options(self, path: str, configs: dict[str, dict], depth: int) -> dict:
        data = configs.get(path) or {}
        options = dict(data.get("compilerOptions") or {})
        parent = data.get("extends")
        if isinstance(parent, str) and parent.startswith(".") and depth < 5:
            parent_path = posixpath.normpath(posixpath.join(posixpath.dirname(path), parent))
            if not parent_path.endswith(".json"):
                parent_path += ".json"
            inherited = self._merged_options(parent_path, configs, depth + 1)
            if "baseUrl" in inherited and "baseUrl" not in options:
                # baseUrl is relative to the config that declared it
                inherited["baseUrl"] = posixpath.relpath(
                    posixpath.join(posixpath.dirname(parent_path) or ".", inherited["baseUrl"]),
                    posixpath.dirname(path) or ".",
                )
            options = {**inherited, **options}
        return options

    def candidates(self, spec: str, from_path: str) -> list[str]:
        """Alias expansions from the nearest config outwards (monorepos often alias at the root)."""
        result: list[str] = []
        directory = posixpath.dirname(from_path)
        while True:
            for base_dir, paths in self.by_dir.get(directory, []):
                for pattern, targets in paths.items():
                    if pattern.endswith("*") and spec.startswith(pattern[:-1]):
                        star = spec[len(pattern) - 1 :]
                        result += [posixpath.normpath(posixpath.join(base_dir, t.replace("*", star))) for t in targets]
                    elif pattern == spec:
                        result += [posixpath.normpath(posixpath.join(base_dir, t)) for t in targets]
                result.append(posixpath.normpath(posixpath.join(base_dir, spec)))
            if not directory:
                return result
            directory = posixpath.dirname(directory)


class CodeIndex:
    def __init__(
        self,
        analyses: dict[str, dict],
        ts_configs: dict[str, dict] | None = None,
        packages: dict[str, str] | None = None,
    ):
        self.files = analyses
        self.ts_paths = TsPaths(ts_configs or {})
        # workspace package name -> directory, longest names first for prefix matching
        self.packages = sorted((packages or {}).items(), key=lambda item: -len(item[0]))
        self.symbols: dict[str, Symbol] = {}
        self.file_symbols: dict[str, dict[str, str]] = {}
        self.members: dict[str, dict[str, str]] = {}  # class/object id -> member name -> id
        self.methods_by_name: dict[tuple[str, str], list[str]] = {}
        self.top_by_name: dict[tuple[str, str], list[str]] = {}
        self.imports: dict[str, dict[str, dict]] = {}
        self.star_imports: dict[str, list[str]] = {}
        self.locals: dict[str, dict[str, set[str]]] = {}
        self.class_bases: dict[str, list[str]] = {}
        self.edges: dict[tuple[str, str], Edge] = {}
        self.out: dict[str, set[str]] = {}
        self.inn: dict[str, set[str]] = {}
        self._binding_cache: dict[tuple[str, str], tuple | None] = {}
        self._py_modules: dict[str, str] = {}
        self._py_suffix: dict[str, list[str]] = {}
        self._py_module_name: dict[str, str] = {}

        self._register()
        self._register_static_members()
        self._resolve_bases()
        self._resolve_calls()

    # -- construction -------------------------------------------------------

    def _register(self) -> None:
        for path, analysis in self.files.items():
            lang = analysis["language"]
            fam = family(lang)
            table: dict[str, str] = {}
            for raw in analysis["symbols"]:
                sid = symbol_id(path, raw["qual"])
                parent = symbol_id(path, raw["parent"]) if raw.get("parent") else None
                sym = Symbol(
                    id=sid, path=path, qual=raw["qual"], name=raw["name"], kind=raw["kind"],
                    start=raw["start"], end=raw["end"], parent=parent, language=lang,
                    bases=raw.get("bases") or [], returns=raw.get("returns"),
                )
                self.symbols[sid] = sym
                table[raw["qual"]] = sid
                if parent:
                    self.members.setdefault(parent, {})[sym.name] = sid
                    if sym.kind == "method":
                        self.methods_by_name.setdefault((fam, sym.name), []).append(sid)
                elif sym.kind != "module":
                    self.top_by_name.setdefault((fam, sym.name), []).append(sid)
            self.file_symbols[path] = table

            imports: dict[str, dict] = {}
            for entry in analysis["imports"]:
                if entry["local"] == "*":
                    self.star_imports.setdefault(path, []).append(entry["module"])
                else:
                    imports[entry["local"]] = entry
            self.imports[path] = imports

            if fam == "py":
                module = path.rsplit(".", 1)[0]
                if module.endswith("/__init__") or module == "__init__":
                    module = module[: -len("__init__")].rstrip("/")
                dotted = module.replace("/", ".")
                if dotted:
                    self._py_module_name[path] = dotted
                    self._py_modules.setdefault(dotted, path)
                    parts = dotted.split(".")
                    for i in range(len(parts)):
                        self._py_suffix.setdefault(".".join(parts[i:]), []).append(path)

    def _register_static_members(self) -> None:
        """``Menu.Item = MenuItem`` at module level makes ``<Menu.Item />`` resolve to MenuItem."""
        for path, analysis in self.files.items():
            for alias in analysis.get("static_members", []):
                owner = self.file_symbols[path].get(alias["owner"])
                target = self.resolve_dotted(path, alias["value"])
                if owner and target and target[0] == "symbol":
                    self.members.setdefault(owner, {}).setdefault(alias["name"], target[1])

    def _resolve_bases(self) -> None:
        for sym in list(self.symbols.values()):
            if sym.kind not in ("class", "interface") or not sym.bases:
                continue
            resolved = []
            for base in sym.bases:
                target = self.resolve_dotted(sym.path, base)
                if target and target[0] == "symbol" and self.symbols[target[1]].kind in ("class", "interface"):
                    resolved.append(target[1])
                    self._add_edge(sym.id, target[1], "inherits", False, sym.start)
            self.class_bases[sym.id] = resolved

    def _resolve_calls(self) -> None:
        for path, analysis in self.files.items():
            fam = family(analysis["language"])
            table = self.file_symbols[path]
            for call in analysis["calls"]:
                src = table.get(call["scope"]) or table[MODULE]
                targets = self._resolve_call(path, call, fam, analysis)
                for dst, fuzzy in targets:
                    kind = self._edge_kind(call["kind"], dst)
                    self._add_edge(src, dst, kind, fuzzy, call["line"])
                recv = call["recv"]
                if not targets and recv and recv.split(".")[0] not in ("self", "cls", "this") \
                        and not self._is_local(path, call["scope"], recv.split(".")[0]):
                    # Order.objects.filter(...) -> uses Order even though `objects` is framework magic
                    used = self._resolve_ref(path, recv)
                    if used and used != src and not self._is_related(src, used):
                        self._add_edge(src, used, "references", False, call["line"])
            for scope, names in analysis["refs"].items():
                src = table.get(scope) or table[MODULE]
                for name in names:
                    head = name.split(".", 1)[0]
                    if head in ("self", "cls", "this") or self._is_local(path, scope, head):
                        continue
                    target = self._resolve_ref(path, name)
                    if target and not self._is_related(src, target) and (src, target) not in self.edges:
                        self._add_edge(src, target, "references", False, self.symbols[src].start)

    def _edge_kind(self, call_kind: str, dst: str) -> str:
        if call_kind == "jsx":
            return "renders"
        if self.symbols[dst].kind == "class":
            return "instantiates"
        return "calls"

    def _add_edge(self, src: str, dst: str, kind: str, fuzzy: bool, line: int) -> None:
        if src == dst:
            return
        edge = self.edges.get((src, dst))
        if edge is None:
            self.edges[(src, dst)] = Edge(src, dst, kind, fuzzy, [line])
            self.out.setdefault(src, set()).add(dst)
            self.inn.setdefault(dst, set()).add(src)
            return
        if EDGE_STRENGTH[kind] > EDGE_STRENGTH[edge.kind]:
            edge.kind = kind
        edge.fuzzy = edge.fuzzy and fuzzy
        if line not in edge.lines and len(edge.lines) < 25:
            edge.lines.append(line)

    def _is_local(self, path: str, scope: str, name: str) -> bool:
        local_sets = self.files[path].get("locals", {})
        return name in local_sets.get(scope, ())

    def _is_related(self, a: str, b: str) -> bool:
        """True when b is an ancestor of a (a method referencing its own class)."""
        cur = self.symbols[a].parent
        while cur:
            if cur == b:
                return True
            cur = self.symbols[cur].parent
        return False

    # -- module resolution --------------------------------------------------

    def resolve_py_module(self, spec: str, from_path: str) -> str | None:
        if spec.startswith("."):
            level = len(spec) - len(spec.lstrip("."))
            rest = spec[level:]
            parts = from_path.split("/")[:-1]
            if level - 1 > len(parts):
                return None
            base = parts[: len(parts) - (level - 1)]
            dotted = ".".join(base + (rest.split(".") if rest else []))
            return self._py_modules.get(dotted)
        if spec in self._py_modules:
            return self._py_modules[spec]
        candidates = self._py_suffix.get(spec)
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        def shared(p: str) -> int:
            return len(posixpath.commonprefix([p, from_path]).rsplit("/", 1)[0])

        return min(candidates, key=lambda p: (-shared(p), len(p)))

    def resolve_js_module(self, spec: str, from_path: str) -> str | None:
        if spec.startswith("."):
            return self._js_file(posixpath.normpath(posixpath.join(posixpath.dirname(from_path), spec)))
        for candidate in self.ts_paths.candidates(spec, from_path):
            found = self._js_file(candidate)
            if found:
                return found
        for name, directory in self.packages:
            if spec == name or spec.startswith(name + "/"):
                sub = spec[len(name) + 1 :]
                roots = [f"{directory}/src", directory] if directory else ["src", ""]
                for root in roots:
                    found = self._js_file(posixpath.join(root, sub) if sub else posixpath.join(root, "index"))
                    if found:
                        return found
                return None
        if spec.startswith(("@/", "~/")):
            for prefix in ("src/", "app/", ""):
                found = self._js_file(prefix + spec[2:])
                if found:
                    return found
        return None

    def _js_file(self, base: str) -> str | None:
        if base in self.files:
            return base
        stem = base
        for ext in (".js", ".jsx", ".mjs", ".cjs"):
            if base.endswith(ext):
                stem = base[: -len(ext)]
                break
        for ext in JS_EXTS:
            if stem + ext in self.files:
                return stem + ext
        for ext in JS_EXTS:
            if f"{base}/index{ext}" in self.files:
                return f"{base}/index{ext}"
        return None

    # -- bindings -----------------------------------------------------------

    def binding(self, path: str, local: str, depth: int = 0) -> tuple | None:
        """What an imported local name refers to: ('symbol', id) or ('module', path)."""
        key = (path, local)
        if key in self._binding_cache:
            return self._binding_cache[key]
        entry = self.imports.get(path, {}).get(local)
        result = None
        if entry is not None and depth < 8:
            self._binding_cache[key] = None  # cycle guard
            if family(self.files[path]["language"]) == "py":
                result = self._py_import_target(entry, path, depth)
            else:
                result = self._js_import_target(entry, path, depth)
        self._binding_cache[key] = result
        return result

    def _py_import_target(self, entry: dict, from_path: str, depth: int) -> tuple | None:
        module, name = entry["module"], entry["name"]
        module_path = self.resolve_py_module(module, from_path)
        if name is None:
            return ("module", module_path) if module_path else None
        joined = module + name if module.endswith(".") else f"{module}.{name}"
        if module_path:
            sid = self.file_symbols[module_path].get(name)
            if sid:
                return ("symbol", sid)
        sub = self.resolve_py_module(joined, from_path)
        if sub:
            return ("module", sub)
        if module_path:
            # package __init__ re-exporting: `from .models import Order`
            reexport = self.binding(module_path, name, depth + 1)
            if reexport:
                return reexport
            for star in self.star_imports.get(module_path, []):
                star_path = self.resolve_py_module(star, module_path)
                if star_path and name in self.file_symbols[star_path]:
                    return ("symbol", self.file_symbols[star_path][name])
        return None

    def _js_import_target(self, entry: dict, from_path: str, depth: int) -> tuple | None:
        module_path = self.resolve_js_module(entry["module"], from_path)
        if not module_path:
            return None
        if entry["name"] == "*":
            return ("module", module_path)
        target = self.js_export(module_path, entry["name"], depth + 1)
        if target is None and entry["name"] == "default":
            return ("module", module_path)  # CommonJS module.exports = {...}
        return target

    def js_export(self, path: str, name: str, depth: int = 0) -> tuple | None:
        if depth > 8 or path not in self.files:
            return None
        analysis = self.files[path]
        exp = analysis["exports"].get(name)
        if exp:
            if "local" in exp and exp["local"]:
                return self._js_local(path, exp["local"], depth)
            if "from" in exp:
                module_path = self.resolve_js_module(exp["from"], path)
                if not module_path:
                    return None
                if exp["name"] == "*":
                    return ("module", module_path)
                return self.js_export(module_path, exp["name"], depth + 1)
            for candidate in exp.get("candidates", []):
                target = self._js_local(path, candidate, depth)
                if target and target[0] == "symbol":
                    return target
        if name != "default":
            sid = self.file_symbols[path].get(name)
            if sid:
                return ("symbol", sid)
        for star in analysis["star_exports"]:
            module_path = self.resolve_js_module(star, path)
            if module_path:
                target = self.js_export(module_path, name, depth + 1)
                if target:
                    return target
        return None

    def _js_local(self, path: str, local: str, depth: int) -> tuple | None:
        sid = self.file_symbols[path].get(local)
        if sid:
            return ("symbol", sid)
        return self.binding(path, local, depth + 1)

    # -- expression resolution ---------------------------------------------

    def resolve_dotted(self, path: str, expr: str) -> tuple | None:
        """Resolve ``name`` / ``mod.Class.method`` in the module scope of ``path``."""
        parts = expr.split(".")
        target, used = None, 0
        for k in range(len(parts), 0, -1):
            head = ".".join(parts[:k])
            if k == 1:
                sid = self.file_symbols[path].get(head)
                target = ("symbol", sid) if sid else self.binding(path, head)
                if target is None and family(self.files[path]["language"]) == "py":
                    target = self._py_star_lookup(path, head)
            else:
                target = self.binding(path, head)  # `import a.b.c`
            if target:
                used = k
                break
        if not target:
            return None
        for part in parts[used:]:
            target = self.member(target, part)
            if target is None:
                return None
        return target

    def _py_star_lookup(self, path: str, name: str) -> tuple | None:
        for star in self.star_imports.get(path, []):
            star_path = self.resolve_py_module(star, path)
            if star_path and name in self.file_symbols[star_path]:
                return ("symbol", self.file_symbols[star_path][name])
        return None

    def member(self, target: tuple, name: str) -> tuple | None:
        kind, value = target
        if kind == "module":
            sid = self.file_symbols[value].get(name)
            if sid:
                return ("symbol", sid)
            if family(self.files[value]["language"]) == "py":
                bound = self.binding(value, name)
                if bound:
                    return bound
                module_name = self._py_module_name.get(value)
                if module_name:
                    sub = self._py_modules.get(f"{module_name}.{name}")
                    if sub:
                        return ("module", sub)
                return None
            return self.js_export(value, name)
        sym = self.symbols[value]
        if sym.kind in ("class", "interface", "object"):
            found = self.method_lookup(value, name)
            return ("symbol", found) if found else None
        found = self.members.get(value, {}).get(name)  # function with static members
        return ("symbol", found) if found else None

    def method_lookup(self, class_id: str, name: str, depth: int = 0) -> str | None:
        found = self.members.get(class_id, {}).get(name)
        if found or depth > 6:
            return found
        for base in self.class_bases.get(class_id, []):
            found = self.method_lookup(base, name, depth + 1)
            if found:
                return found
        return None

    def enclosing_container(self, path: str, scope: str) -> str | None:
        sid = self.file_symbols[path].get(scope)
        cur = self.symbols[sid].parent if sid else None
        while cur:
            if self.symbols[cur].kind in ("class", "object"):
                return cur
            cur = self.symbols[cur].parent
        return None

    def type_to_class(self, path: str, type_text: str | None, depth: int = 0) -> str | None:
        if not type_text or depth > 3:
            return None
        parts = type_text.split(".")
        for k in range(len(parts), 0, -1):
            target = self.resolve_dotted(path, ".".join(parts[:k]))
            if not target:
                continue
            if target[0] != "symbol":
                return None
            sym = self.symbols[target[1]]
            if sym.kind in ("class", "interface"):
                return sym.id
            if sym.kind in ("function", "method") and sym.returns:
                return self.type_to_class(sym.path, normalize_type(sym.returns), depth + 1)
            return None
        return None

    def var_class(self, path: str, scope: str, var: str) -> str | None:
        types = self.files[path]["types"]
        cur = scope
        while cur:
            type_text = types.get(cur, {}).get(var)
            if type_text:
                return self.type_to_class(path, type_text)
            if cur == MODULE:
                break
            sid = self.file_symbols[path].get(cur)
            parent = self.symbols[sid].parent if sid else None
            cur = self.symbols[parent].qual if parent else MODULE
        return None

    def resolve_receiver(self, path: str, scope: str, recv: str) -> tuple | None:
        parts = recv.split(".")
        if parts[0] in ("self", "cls", "this"):
            container = self.enclosing_container(path, scope)
            if not container:
                return None
            if len(parts) == 1:
                return ("symbol", container)
            attr_key = f"{parts[0]}.{parts[1]}"
            type_text = self.files[path]["types"].get(self.symbols[container].qual, {}).get(attr_key)
            # Fall back to the base classes' declared attribute types.
            if not type_text:
                for base in self.class_bases.get(container, []):
                    base_sym = self.symbols[base]
                    type_text = self.files[base_sym.path]["types"].get(base_sym.qual, {}).get(attr_key)
                    if type_text:
                        cls = self.type_to_class(base_sym.path, type_text)
                        return self._walk(("symbol", cls), parts[2:]) if cls else None
            cls = self.type_to_class(path, type_text)
            return self._walk(("symbol", cls), parts[2:]) if cls else None
        if not self._is_local(path, scope, parts[0]) or self.files[path]["types"].get(scope, {}).get(parts[0]):
            cls = self.var_class(path, scope, parts[0])
            if cls:
                return self._walk(("symbol", cls), parts[1:])
        if self._is_local(path, scope, parts[0]):
            return None
        return self.resolve_dotted(path, recv)

    def _walk(self, target: tuple | None, parts: list[str]) -> tuple | None:
        for part in parts:
            if target is None:
                return None
            target = self.member(target, part)
        return target

    def _is_external(self, path: str, recv: str | None) -> bool:
        """Receiver rooted in an import we could not resolve (a third-party package)."""
        if not recv:
            return False
        head = recv.split(".")[0]
        return head in self.imports.get(path, {}) and self.binding(path, head) is None

    def _resolve_call(self, path: str, call: dict, fam: str, analysis: dict) -> list[tuple[str, bool]]:
        kind, name, recv, scope = call["kind"], call["name"], call["recv"], call["scope"]

        if kind == "super":
            container = self.enclosing_container(path, scope)
            for base in self.class_bases.get(container, []) if container else []:
                found = self.method_lookup(base, name if name != "constructor" else "constructor")
                if found:
                    return [(found, False)]
                if name == "constructor":
                    return [(base, False)]
            return []

        if recv is None:
            if kind == "attr":  # receiver is an arbitrary expression
                return self._fuzzy_method(fam, name)
            if self._is_local(path, scope, name):
                return []
            target = self.resolve_dotted(path, name)
            if target and target[0] == "symbol":
                return [(target[1], False)]
            if target:
                return []
            if fam == "js" and not analysis["is_module"]:
                return self._fuzzy_top(fam, name, path)
            return []

        target = self.resolve_receiver(path, scope, recv)
        if target:
            found = self.member(target, name)
            if found and found[0] == "symbol":
                return [(found[1], False)]
            if target[0] == "module":
                return []
            if self.symbols[target[1]].kind in ("class", "interface"):
                # Known class but method not found (inherited from a framework base).
                return []
        if self._is_external(path, recv):
            return []
        if kind == "attr":
            return self._fuzzy_method(fam, name)
        return []

    def _fuzzy_method(self, fam: str, name: str) -> list[tuple[str, bool]]:
        if name in FUZZY_STOPLIST or name.startswith("__"):
            return []
        candidates = self.methods_by_name.get((fam, name), [])
        if 0 < len(candidates) <= FUZZY_MAX_CANDIDATES:
            return [(c, True) for c in candidates]
        return []

    def _fuzzy_top(self, fam: str, name: str, path: str) -> list[tuple[str, bool]]:
        candidates = [
            c for c in self.top_by_name.get((fam, name), [])
            if not self.files[self.symbols[c].path]["is_module"] and self.symbols[c].path != path
        ]
        if 0 < len(candidates) <= FUZZY_MAX_CANDIDATES:
            return [(c, True) for c in candidates]
        return []

    def _resolve_ref(self, path: str, name: str) -> str | None:
        parts = name.split(".")
        for k in range(len(parts), 0, -1):
            target = self.resolve_dotted(path, ".".join(parts[:k]))
            if target and target[0] == "symbol":
                return target[1]
            if target:
                return None  # a module, or partially resolved chain
        return None

    # -- queries ------------------------------------------------------------

    def symbols_in_file(self, path: str) -> list[Symbol]:
        return [self.symbols[sid] for sid in self.file_symbols.get(path, {}).values()]

    def search(self, query: str, limit: int = 40) -> list[Symbol]:
        q = query.lower().strip()
        if not q:
            return []
        scored = []
        for sym in self.symbols.values():
            if sym.kind == "module":
                continue
            qual = sym.qual.lower()
            name = sym.name.lower()
            if name == q:
                score = 0
            elif name.startswith(q):
                score = 1
            elif q in qual:
                score = 2
            elif q in sym.path.lower():
                score = 3
            else:
                continue
            scored.append((score, len(sym.qual), sym.path, sym))
        scored.sort(key=lambda item: item[:3])
        return [item[3] for item in scored[:limit]]
