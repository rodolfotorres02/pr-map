"""Review plan: split a large PR into cohesive clusters, suggest a reading order, flag risk.

Clustering is a union-find over the changed (non-deleted) symbols:

* *core* symbols (logic that is neither a test nor module-level code) are joined when
  an exact edge links them, when an exact path ``a -> x -> b`` runs through one
  unchanged symbol ``x``, or when they are members of the same class/object;
* *satellites* (tests and module-level pseudo-symbols) do not glue clusters together:
  each one attaches to the core cluster it is most connected to. Module-level code
  ignores ``references`` edges (``urls.py`` references every view). Tests that touch no
  changed logic form tests-only clusters; module-level edits with no links join their
  file's cluster, or are left out and their file is reported in ``unmapped_files``.
"""

from __future__ import annotations

import posixpath
import re
from collections import Counter, deque
from collections.abc import Callable, Iterable

from prmap.analysis.common import MODULE
from prmap.analysis.index import FUZZY_STOPLIST, CodeIndex, family, symbol_id
from prmap.classify import language_of

MAX_TEST_CALLERS = 5
MAX_REFERENCES = 25
MAX_IMPORT_LOOKUPS = 60
SPLIT_ABOVE = 40  # components bigger than this are split into modularity communities


class _DSU:
    def __init__(self, items: Iterable[str] = ()):
        self.parent: dict[str, str] = {x: x for x in items}

    def find(self, x: str) -> str:
        parent = self.parent
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            if rb < ra:
                ra, rb = rb, ra
            self.parent[rb] = ra


def _louvain(nodes: list[str], weights: dict[str, dict[str, float]], resolution: float = 1.0) -> list[list[str]]:
    """Modularity communities (Louvain) of the subgraph induced by ``nodes``; deterministic."""
    node_set = set(nodes)
    graph: dict[str, dict[str, float]] = {
        n: {m: w for m, w in weights[n].items() if m in node_set} for n in sorted(nodes)
    }
    members: dict[str, list[str]] = {n: [n] for n in graph}
    for _level in range(10):
        degree = {n: sum(nbrs.values()) for n, nbrs in graph.items()}
        m2 = sum(degree.values())
        if m2 <= 0:
            break
        comm = {n: n for n in graph}
        total = dict(degree)
        moved_any = False
        for _sweep in range(20):
            moved = False
            for n, nbrs in graph.items():
                current, k = comm[n], degree[n]
                links: dict[str, float] = {}
                for m, w in nbrs.items():
                    if m != n:
                        links[comm[m]] = links.get(comm[m], 0.0) + w
                total[current] -= k
                best, best_gain = current, links.get(current, 0.0) - resolution * total[current] * k / m2
                for c, w in links.items():
                    gain = w - resolution * total[c] * k / m2
                    # strict improvement over staying; equal candidates tie-break by label
                    if gain > best_gain + 1e-9 or (best != current and gain > best_gain - 1e-9 and c < best):
                        best, best_gain = c, gain
                total[best] += k
                if best != current:
                    comm[n] = best
                    moved = moved_any = True
            if not moved:
                break
        if not moved_any:
            break
        new_graph: dict[str, dict[str, float]] = {}
        new_members: dict[str, list[str]] = {}
        for n, nbrs in graph.items():
            c = comm[n]
            new_members.setdefault(c, []).extend(members[n])
            row = new_graph.setdefault(c, {})
            for m, w in nbrs.items():
                row[comm[m]] = row.get(comm[m], 0.0) + w
        if len(new_graph) == len(graph):
            break
        graph, members = new_graph, new_members
    return list(members.values())


def _split(nodes: list[str], weights: dict[str, dict[str, float]], depth: int = 0) -> list[list[str]]:
    """Break a connected component that is too big to read as one unit into communities."""
    if len(nodes) <= SPLIT_ABOVE or depth >= 3:
        return [nodes]
    parts = _louvain(nodes, weights)
    if len(parts) <= 1:
        return [nodes]
    result: list[list[str]] = []
    for part in parts:
        result.extend(_split(part, weights, depth + 1))
    return result


def build_plan(
    index: CodeIndex,
    changes: dict[str, dict],
    files: list[dict],
    category_of: Callable[[str], str],
    read_head: Callable[[str], str | None] | None = None,
) -> dict:
    return _Planner(index, changes, files, category_of, read_head).build()


class _Planner:
    def __init__(self, index, changes, files, category_of, read_head):
        self.index = index
        self.changes = changes
        self.files = files
        self.category_of = category_of
        self.read_head = read_head
        self.changed_paths = {f["path"] for f in files}
        symbols = index.symbols
        self.live = sorted(
            sid for sid, c in changes.items() if c["status"] != "deleted" and sid in symbols
        )
        self.live_set = set(self.live)
        self.is_test = {sid: changes[sid]["category"] == "test" for sid in self.live}
        self.is_module = {sid: symbols[sid].kind == "module" for sid in self.live}
        self.core = [s for s in self.live if not self.is_test[s] and not self.is_module[s]]
        self.core_set = set(self.core)
        # Reading-order adjacency among changed symbols: direct exact edges + bridged a->x->b.
        self.adj: dict[str, set[str]] = {s: set() for s in self.live}

    # -- helpers ------------------------------------------------------------

    def key(self, sid: str) -> tuple:
        sym = self.index.symbols[sid]
        return (sym.path, sym.start, sid)

    def churn(self, sid: str) -> int:
        c = self.changes[sid]
        return c["additions"] + c["deletions"]

    def links(self, a: str, skip_refs: bool) -> tuple[list[str], list[str]]:
        """Changed symbols reachable from ``a``: directly, and through one unchanged symbol."""
        edges, out, live = self.index.edges, self.index.out, self.live_set
        direct: list[str] = []
        bridged: list[str] = []
        for x in out.get(a, ()):
            e = edges[(a, x)]
            if e.fuzzy or (skip_refs and e.kind == "references"):
                continue
            if x in live:
                direct.append(x)
                continue
            if x in self.changes:
                continue
            for b in out.get(x, ()):
                if b != a and b in live and not edges[(x, b)].fuzzy:
                    bridged.append(b)
        return direct, bridged

    def in_links(self, s: str, skip_refs: bool) -> list[str]:
        edges = self.index.edges
        result = []
        for c in self.index.inn.get(s, ()):
            if c in self.live_set:
                e = edges[(c, s)]
                if not e.fuzzy and not (skip_refs and e.kind == "references"):
                    result.append(c)
        return result

    # -- clustering ---------------------------------------------------------

    def cluster(self) -> tuple[dict[str, list[str]], list[str]]:
        symbols, edges = self.index.symbols, self.index.edges
        core_set = self.core_set
        dsu = _DSU(self.core)
        weights: dict[str, dict[str, float]] = {a: {} for a in self.core}

        def link(a: str, b: str, w: float) -> None:
            dsu.union(a, b)
            weights[a][b] = weights[a].get(b, 0.0) + w
            weights[b][a] = weights[b].get(a, 0.0) + w

        siblings: dict[str, list[str]] = {}
        for a in self.core:
            direct, bridged = self.links(a, skip_refs=False)
            for b in direct:
                self.adj[a].add(b)
                if b in core_set:
                    link(a, b, 1.0 if edges[(a, b)].kind == "references" else 3.0)
            for b in bridged:
                self.adj[a].add(b)
                if b in core_set:
                    link(a, b, 1.0)
            parent = symbols[a].parent
            if parent:
                if parent in core_set:
                    link(a, parent, 1.0)
                siblings.setdefault(parent, []).append(a)
        for members in siblings.values():
            # Methods of one class changed together; neighbours in the file are linked
            # (a chain, not a clique, so that a god class does not swamp everything else).
            members.sort(key=self.key)
            for a, b in zip(members, members[1:]):
                link(a, b, 1.0)

        components: dict[str, list[str]] = {}
        for s in self.core:
            components.setdefault(dsu.find(s), []).append(s)
        owner: dict[str, str] = {}
        for root, members in components.items():
            for part in _split(members, weights):
                label = min(part)
                for s in part:
                    owner[s] = label
        sizes = Counter(owner.values())

        def best(votes: Counter) -> str | None:
            if not votes:
                return None
            return min(votes, key=lambda root: (-votes[root], -sizes.get(root, 0), root))

        satellites = [s for s in self.live if s not in self.core_set]
        sat_links: dict[str, tuple[list[str], list[str], list[str]]] = {}
        unattached: list[str] = []
        for s in satellites:
            skip_refs = self.is_module[s]
            direct, bridged = self.links(s, skip_refs)
            incoming = self.in_links(s, skip_refs)
            sat_links[s] = (direct, bridged, incoming)
            self.adj[s].update(direct)
            self.adj[s].update(bridged)
            votes: Counter = Counter()
            for b in direct:
                if b in self.core_set:
                    votes[owner[b]] += 2
            for b in incoming:
                if b in self.core_set:
                    votes[owner[b]] += 2
            for b in bridged:
                if b in self.core_set:
                    votes[owner[b]] += 1
            root = best(votes)
            if root is None:
                unattached.append(s)
            else:
                owner[s] = root

        # Tests that touch no changed logic: group them among themselves.
        loose_tests = [s for s in unattached if not self.is_module[s]]
        loose_set = set(loose_tests)
        tdsu = _DSU(loose_tests)
        by_parent = {}
        for s in loose_tests:
            direct, bridged, incoming = sat_links[s]
            for b in (*direct, *bridged, *incoming):
                if b in loose_set:
                    tdsu.union(s, b)
            parent = symbols[s].parent
            if parent:
                if parent in loose_set:
                    tdsu.union(s, parent)
                first = by_parent.setdefault(parent, s)
                if first != s:
                    tdsu.union(s, first)
        groups: dict[str, list[str]] = {}
        for s in loose_tests:
            groups.setdefault(tdsu.find(s), []).append(s)
        for root, members in groups.items():
            # A loose helper used by already-attached tests follows them.
            votes: Counter = Counter()
            for s in members:
                direct, bridged, incoming = sat_links[s]
                for b in (*direct, *incoming):
                    if b in owner and b not in self.core_set:
                        votes[owner[b]] += 1
            target = best(votes) or f"t:{root}"
            for s in members:
                owner[s] = target

        # Module-level edits with no links of their own.
        loose_modules = [s for s in unattached if self.is_module[s]]
        file_owner: dict[str, Counter] = {}
        for s, root in owner.items():
            file_owner.setdefault(symbols[s].path, Counter())[root] += 1
        skipped: list[str] = []
        for s in loose_modules:
            direct, bridged, incoming = sat_links[s]
            votes = Counter()
            for b in (*direct, *incoming):
                if b in owner:
                    votes[owner[b]] += 2
            for b in bridged:
                if b in owner:
                    votes[owner[b]] += 1
            target = best(votes) or best(file_owner.get(symbols[s].path, Counter()))
            if target is None:
                skipped.append(s)
            else:
                owner[s] = target

        clusters: dict[str, list[str]] = {}
        for s in self.live:
            if s in owner:
                clusters.setdefault(owner[s], []).append(s)
        return clusters, skipped

    # -- reading order ------------------------------------------------------

    def _bfs(self, allowed: set[str], roots: list[str], order: list[str], visited: set[str]) -> None:
        for r in roots:
            if r in visited or r not in allowed:
                continue
            visited.add(r)
            queue = deque([r])
            while queue:
                node = queue.popleft()
                order.append(node)
                nxt = [b for b in self.adj[node] if b in allowed and b not in visited]
                nxt.sort(key=self.key)
                for b in nxt:
                    visited.add(b)
                    queue.append(b)

    def _segment(self, allowed: set[str], preferred: list[str], order: list[str], visited: set[str]) -> None:
        if not allowed:
            return
        has_caller = set()
        for a in allowed:
            for b in self.adj[a]:
                if b in allowed and b != a:
                    has_caller.add(b)
        heads = sorted((m for m in allowed if m not in has_caller), key=self.key)
        rest = sorted(allowed, key=self.key)
        self._bfs(allowed, [*preferred, *heads, *rest], order, visited)

    def describe(self, members: list[str]) -> dict:
        mset = set(members)
        tests_only = all(self.is_test[m] for m in members)

        def counts(m: str) -> bool:
            return not self.is_module[m] and (tests_only or not self.is_test[m])

        candidates = [m for m in members if counts(m)] or list(members)
        called: set[str] = set()
        for a in members:
            if not counts(a):
                continue
            for b in self.adj[a]:
                if b in mset and b != a:
                    called.add(b)

        def internal_out(m: str) -> int:
            return sum(1 for b in self.adj[m] if b in mset)

        def priority(m: str) -> tuple:
            return (-internal_out(m), -self.churn(m), *self.key(m))

        entries = [m for m in candidates if m not in called]
        if not entries:
            entries = [min(candidates, key=priority)]
        entries.sort(key=priority)

        order: list[str] = []
        visited: set[str] = set()
        logic = {m for m in members if not self.is_test[m] and not self.is_module[m]}
        self._segment(logic, entries, order, visited)
        self._segment({m for m in members if not self.is_test[m] and self.is_module[m]}, [], order, visited)
        tests = {m for m in members if self.is_test[m]}
        self._segment(tests, entries if tests_only else [], order, visited)

        files: list[str] = []
        seen_files: set[str] = set()
        categories: Counter = Counter()
        additions = deletions = 0
        for sid in order:
            change = self.changes[sid]
            if change["path"] not in seen_files:
                seen_files.add(change["path"])
                files.append(change["path"])
            categories[change["category"]] += 1
            additions += change["additions"]
            deletions += change["deletions"]

        main = entries[0]
        sym = self.index.symbols[main]
        title = self.changes[main]["name"] if sym.kind == "module" else sym.qual
        return {
            "title": title,
            "symbols": order,
            "entry_points": entries,
            "files": files,
            "additions": additions,
            "deletions": deletions,
            "categories": dict(categories),
        }

    # -- risk ---------------------------------------------------------------

    def risk(self) -> dict[str, dict]:
        index = self.index
        symbols, edges, inn, out = index.symbols, index.edges, index.inn, index.out
        test_memo: dict[str, bool] = {}
        direct_memo: dict[str, list[str]] = {}

        def is_test_symbol(sid: str) -> bool:
            result = test_memo.get(sid)
            if result is None:
                result = test_memo[sid] = self.category_of(symbols[sid].path) == "test"
            return result

        def direct_tests(sid: str) -> list[str]:
            result = direct_memo.get(sid)
            if result is None:
                # exact callers first, then name-matched (fuzzy) ones
                result = sorted(
                    (c for c in inn.get(sid, ()) if is_test_symbol(c)),
                    key=lambda c: (edges[(c, sid)].fuzzy, c),
                )
                direct_memo[sid] = result
            return result

        risk: dict[str, dict] = {}
        changed_paths = self.changed_paths
        for sid in self.live:
            callers = inn.get(sid, ())
            external = sum(1 for c in callers if symbols[c].path not in changed_paths)
            found = [t for t in direct_tests(sid) if t != sid][:MAX_TEST_CALLERS]
            if len(found) < MAX_TEST_CALLERS:
                seen = set(found)
                for c in sorted(callers, key=lambda c: (edges[(c, sid)].fuzzy, c)):
                    for t in direct_tests(c):
                        if t != sid and t not in seen:
                            seen.add(t)
                            found.append(t)
                            if len(found) >= MAX_TEST_CALLERS:
                                break
                    if len(found) >= MAX_TEST_CALLERS:
                        break
            risk[sid] = {
                "callers": len(callers),
                "external_callers": external,
                "tested": bool(found),
                "test_callers": found,
                "fan_out": len(out.get(sid, ())),
            }
        return risk

    # -- deleted symbols that are still used ----------------------------------

    def deleted_references(self) -> list[dict]:
        """Deleted symbols whose name is still imported, called or referenced at head.

        Best-effort and tuned for precision: a name counts only when nothing at head
        defines or exports it any more and the use site can be tied to the deleted
        definition (a broken import from its module, a bare name that resolves to
        nothing, ``module.name`` on its module, or ``self.name`` / a typed receiver for
        methods of its class).
        """
        index = self.index
        deleted = sorted(
            (c for c in self.changes.values() if c["status"] == "deleted" and c["kind"] != "module"),
            key=lambda c: (c["path"], c["start"], c["id"]),
        )
        if not deleted:
            return []
        alive = set(index.top_by_name) | set(index.methods_by_name)
        by_name: dict[tuple[str, str], list[dict]] = {}
        for c in deleted:
            name = c["name"]
            if not name or name.startswith("__") or name in FUZZY_STOPLIST:
                continue
            fam = family(language_of(c["path"]) or "")
            if (fam, name) in alive:
                continue  # moved or redefined somewhere: uses probably resolve to that
            head = index.files.get(c["path"])
            is_method = "." in c["qual"]
            if head is not None:
                if not is_method and (
                    name in head.get("exports", {}) or name in head.get("locals", {}).get(MODULE, ())
                ):
                    continue  # still defined at head in a form that is not indexed as a symbol
                if is_method:
                    owner = symbol_id(c["path"], c["qual"].rsplit(".", 1)[0])
                    if owner in index.symbols and _safe(lambda: index.method_lookup(owner, name)):
                        continue  # inherited from a base class now
            by_name.setdefault((fam, name), []).append(c)
        if not by_name:
            return []

        found: dict[str, dict[tuple[str, int], dict]] = {c["id"]: {} for c in deleted}
        lookups = 0

        def add(c: dict, path: str, line: int | None) -> None:
            nonlocal lookups
            refs = found[c["id"]]
            if len(refs) >= MAX_REFERENCES:
                return
            if line is None:
                line = 1
                if self.read_head and lookups < MAX_IMPORT_LOOKUPS:
                    lookups += 1
                    line = _import_line(_safe(lambda: self.read_head(path)), c["name"])
            refs.setdefault((path, line), {"path": path, "line": line})

        for path, analysis in index.files.items():
            fam = family(analysis["language"])
            imports = index.imports.get(path, {})
            local_names = analysis.get("locals", {})
            module_locals = set(local_names.get(MODULE, ()))
            starred = bool(index.star_imports.get(path))
            table = index.file_symbols.get(path, {})

            def bare(scope: str, c: dict) -> bool:
                """Does a bare ``name`` used in ``scope`` still mean the deleted symbol?"""
                name = c["name"]
                if name in local_names.get(scope, ()):
                    return False
                entry = imports.get(name)
                if entry is not None:
                    return entry.get("name") == name and _safe(lambda: index.binding(path, name)) is None \
                        and self._module_matches(entry["module"], path, fam, c["path"])
                # a name that is neither local nor imported can only mean a definition in this file
                return path == c["path"] and not starred and name not in module_locals

            def via_module(head: str, c: dict) -> bool:
                if head not in imports:
                    return False
                target = _safe(lambda: index.binding(path, head))
                return bool(target) and target[0] == "module" and target[1] == c["path"]

            def via_class(scope: str, recv: str, c: dict) -> bool:
                owner_qual = c["qual"].rsplit(".", 1)[0]
                if recv.split(".")[0] in ("self", "this", "cls"):
                    return path == c["path"] and recv in ("self", "this", "cls") \
                        and scope.startswith(owner_qual + ".")
                target = _safe(lambda: index.resolve_receiver(path, scope, recv))
                return bool(target) and target[0] == "symbol" \
                    and target[1] == symbol_id(c["path"], owner_qual)

            for call in analysis["calls"]:
                targets = by_name.get((fam, call["name"]))
                if not targets:
                    continue
                recv, scope = call["recv"], call["scope"]
                for c in targets:
                    if "." in c["qual"]:
                        ok = call["kind"] == "attr" and recv is not None and via_class(scope, recv, c)
                    elif recv is None:
                        ok = call["kind"] != "attr" and bare(scope, c)
                    else:
                        ok = "." not in recv and via_module(recv, c)
                    if ok:
                        add(c, path, call["line"])

            for scope, names in analysis.get("refs", {}).items():
                for ref in names:
                    parts = ref.split(".")
                    targets = by_name.get((fam, parts[-1]))
                    if not targets:
                        continue
                    sid = table.get(scope) or table.get(MODULE)
                    line = index.symbols[sid].start if sid in index.symbols else 1
                    for c in targets:
                        if "." in c["qual"]:
                            ok = len(parts) == 2 and via_class(scope, parts[0], c)
                        elif len(parts) == 1:
                            ok = bare(scope, c)
                        else:
                            ok = len(parts) == 2 and via_module(parts[0], c)
                        if ok:
                            add(c, path, line)

            for local, entry in imports.items():
                targets = by_name.get((fam, entry.get("name")))
                if not targets:
                    continue
                if _safe(lambda: index.binding(path, local)) is not None:
                    continue
                for c in targets:
                    if "." not in c["qual"] and self._module_matches(entry["module"], path, fam, c["path"]):
                        add(c, path, None)

        result = []
        sources: dict[str, str | None] = {}
        for c in deleted:
            refs = found[c["id"]]
            if refs and self.read_head and c["path"] in index.files:
                # The parser does not index every definition form (``export const X = memo(Y)``).
                if c["path"] not in sources:
                    sources[c["path"]] = _safe(lambda: self.read_head(c["path"]))
                if _still_defined(sources[c["path"]], c["name"], "." in c["qual"]):
                    continue
            if refs:
                result.append(
                    {
                        "id": c["id"],
                        "name": c["name"],
                        "path": c["path"],
                        "references": sorted(refs.values(), key=lambda r: (r["path"], r["line"])),
                    }
                )
        return result

    def _module_matches(self, spec: str, from_path: str, fam: str, target: str) -> bool:
        index = self.index
        try:
            if fam == "py":
                resolved = index.resolve_py_module(spec, from_path)
            else:
                resolved = index.resolve_js_module(spec, from_path)
        except Exception:
            resolved = None
        if resolved:
            return resolved == target
        stem = target.rsplit(".", 1)[0]
        if fam == "py":
            stem = stem.removesuffix("/__init__")
            if spec.startswith("."):
                level = len(spec) - len(spec.lstrip("."))
                base = posixpath.dirname(from_path)
                for _ in range(level - 1):
                    base = posixpath.dirname(base)
                rest = spec[level:].replace(".", "/")
                return posixpath.normpath(posixpath.join(base, rest) if rest else base) == stem
            dotted = stem.replace("/", ".")
            return dotted == spec or ("." in spec and dotted.endswith("." + spec))
        if spec.startswith("."):
            joined = posixpath.normpath(posixpath.join(posixpath.dirname(from_path), spec))
            return joined == stem or f"{joined}/index" == stem
        return False

    # -- assembly -----------------------------------------------------------

    def build(self) -> dict:
        groups, _skipped = self.cluster()
        described = [self.describe(members) for members in groups.values()]
        described.sort(key=lambda c: (-len(c["symbols"]), -(c["additions"] + c["deletions"]), c["title"]))
        clusters = [{"id": f"c{i}", **c} for i, c in enumerate(described, 1)]
        covered = {path for c in clusters for path in c["files"]}
        return {
            "clusters": clusters,
            "unmapped_files": [f["path"] for f in self.files if f["path"] not in covered],
            "risk": self.risk(),
            "deleted_still_referenced": self.deleted_references(),
        }


def _safe(fn):
    """Index resolution helpers are heuristics; never let one break the plan."""
    try:
        return fn()
    except Exception:
        return None


def _still_defined(text: str | None, name: str, method: bool) -> bool:
    if not text:
        return False
    n = re.escape(name)
    if method:
        pattern = rf"^\s*(?:(?:public|private|protected|static|async|readonly|override|get|set|def)\s+)*{n}\s*[(=:<]"
    else:
        pattern = (
            rf"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:async\s+)?"
            rf"(?:const|let|var|function\*?|class|def|interface|type|enum|namespace)\s+{n}\b"
            rf"|^\s*{n}\s*(?::[^=]*)?=(?!=)"
            rf"|export\s*\{{[^}}]*\b{n}\b"
        )
    return re.search(pattern, text, re.MULTILINE) is not None


def _import_line(text: str | None, name: str) -> int:
    if not text:
        return 1
    fallback = None
    for number, line in enumerate(text.splitlines(), 1):
        if name in line:
            if "import" in line or "require" in line:
                return number
            if fallback is None:
                fallback = number
    return fallback or 1


def review_plan(manager, review) -> dict:
    """Plan for a review, cached on the Review object (changes() is cached there too)."""
    cached = getattr(review, "_plan", None)
    if cached is not None:
        return cached
    index = manager.require_index(review)
    changes = manager.changes(review)

    def read_head(path: str) -> str | None:
        content = manager.repo.show_file(review.head_sha, path)
        return content.decode(errors="replace") if content is not None else None

    plan = build_plan(index, changes, review.files, manager.category_of, read_head)
    review._plan = plan
    return plan
