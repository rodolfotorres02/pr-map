"""Graph queries over a CodeIndex: symbol neighbourhoods and the PR overview map."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from prmap.analysis.index import CodeIndex

ALL_EDGE_KINDS = ("calls", "instantiates", "renders", "inherits", "references")
CONTAINERS = ("class", "object", "interface")


class GraphQuery:
    def __init__(
        self,
        index: CodeIndex,
        category_of: Callable[[str], str],
        changes: dict[str, dict],
        changed_paths: set[str],
        *,
        fuzzy: bool = True,
        edge_kinds: Iterable[str] = ALL_EDGE_KINDS,
        exclude_categories: Iterable[str] = (),
    ):
        self.index = index
        self.category_of = category_of
        self.changes = changes
        self.changed_paths = changed_paths
        self.fuzzy = fuzzy
        self.edge_kinds = set(edge_kinds)
        self.exclude_categories = set(exclude_categories)

    def edge_ok(self, src: str, dst: str) -> bool:
        edge = self.index.edges[(src, dst)]
        return edge.kind in self.edge_kinds and (self.fuzzy or not edge.fuzzy)

    def node_ok(self, sid: str) -> bool:
        return self.category_of(self.index.symbols[sid].path) not in self.exclude_categories

    # -- neighbourhood ------------------------------------------------------

    def neighborhood(self, seeds: list[str], depth_in: int, depth_out: int, max_nodes: int = 150) -> dict:
        index = self.index
        focus: list[str] = []
        for sid in seeds:
            if sid not in index.symbols:
                continue
            focus.append(sid)
            if index.symbols[sid].kind in CONTAINERS:
                focus += sorted(index.members.get(sid, {}).values())
        depth: dict[str, int] = {sid: 0 for sid in focus}
        truncated = False
        frontier_out = list(dict.fromkeys(focus))
        frontier_in = list(frontier_out)

        for level in range(1, max(depth_in, depth_out) + 1):
            if truncated:
                break  # full: nothing else can be added, and the flag is already set
            for direction, frontier, limit in (("out", frontier_out, depth_out), ("in", frontier_in, depth_in)):
                if level > limit or truncated:
                    continue
                adjacency = index.out if direction == "out" else index.inn
                nxt: list[str] = []
                for node in frontier:
                    if truncated:
                        break
                    for other in sorted(adjacency.get(node, ())):
                        if other in depth or not self.node_ok(other):
                            continue
                        src, dst = (node, other) if direction == "out" else (other, node)
                        if not self.edge_ok(src, dst):
                            continue
                        if len(depth) >= max_nodes:
                            truncated = True
                            break
                        depth[other] = level if direction == "out" else -level
                        nxt.append(other)
                if direction == "out":
                    frontier_out = nxt
                else:
                    frontier_in = nxt

        roles = {sid: ("focus" if d == 0 else "callee" if d > 0 else "caller") for sid, d in depth.items()}
        return self._payload(depth.keys(), roles, depth, truncated)

    # -- overview -----------------------------------------------------------

    def overview(
        self, paths: set[str] | None, neighbors: bool, max_nodes: int = 250, symbols: set[str] | None = None
    ) -> dict:
        index = self.index
        if symbols is not None:  # an explicit core set (e.g. one review-plan cluster)
            core = {sid for sid in symbols if sid in index.symbols}
        else:
            core = {
                sid for sid, info in self.changes.items()
                if info["status"] != "deleted" and sid in index.symbols
                and (paths is None or index.symbols[sid].path in paths)
            }
        truncated = False
        if len(core) > max_nodes:
            # Too big to draw at once: keep the changed symbols most connected to the rest of
            # the change. The UI points reviewers at plan clusters for the full picture.
            def links(sid: str) -> int:
                return sum(1 for o in index.out.get(sid, ()) if o in core) + sum(
                    1 for i in index.inn.get(sid, ()) if i in core
                )

            core = set(sorted(core, key=lambda sid: (-links(sid), sid))[:max_nodes])
            truncated = True
        roles: dict[str, str] = {sid: "changed" for sid in core}

        # Unchanged symbols that sit between two changed ones (A -> X -> B). X's changed
        # successors are looked up once per X (at most two are needed to rule out B == A), so
        # hub symbols reached from thousands of changed callers are scanned only once.
        targets: dict[str, list[str]] = {}
        for a in core:
            for x in index.out.get(a, ()):
                if x in core or x in roles or not self.edge_ok(a, x) or not self.node_ok(x):
                    continue
                found = targets.get(x)
                if found is None:
                    found = []
                    for b in index.out.get(x, ()):
                        if b in core and self.edge_ok(x, b):
                            found.append(b)
                            if len(found) == 2:
                                break
                    targets[x] = found
                if (len(found) == 2 or (found and found[0] != a)) and len(roles) < 2 * max_nodes:
                    roles[x] = "bridge"

        if neighbors:
            for a in sorted(core):
                for other, ok in (
                    *((o, self.edge_ok(a, o)) for o in index.out.get(a, ())),
                    *((i, self.edge_ok(i, a)) for i in index.inn.get(a, ())),
                ):
                    if other in roles or not ok or not self.node_ok(other):
                        continue
                    if len(roles) >= max_nodes:
                        truncated = True
                        break
                    roles[other] = "neighbor"

        payload = self._payload(roles.keys(), roles, {}, truncated)
        # Hide module pseudo-nodes that ended up unconnected (usually import-only edits).
        connected = {e["source"] for e in payload["edges"]} | {e["target"] for e in payload["edges"]}
        payload["nodes"] = [n for n in payload["nodes"] if n["kind"] != "module" or n["id"] in connected]
        return payload

    # -- serialisation ------------------------------------------------------

    def _payload(self, ids: Iterable[str], roles: dict[str, str], depth: dict[str, int], truncated: bool) -> dict:
        node_ids = set(ids)
        nodes = [self.node(sid, roles.get(sid), depth.get(sid)) for sid in node_ids]
        nodes.sort(key=lambda n: (n["path"], n["line"]))
        edges = []
        for src in node_ids:
            for dst in self.index.out.get(src, ()):
                if dst in node_ids and self.edge_ok(src, dst):
                    edge = self.index.edges[(src, dst)]
                    edges.append(
                        {
                            "id": f"{src}->{dst}",
                            "source": src,
                            "target": dst,
                            "kind": edge.kind,
                            "fuzzy": edge.fuzzy,
                            "lines": sorted(edge.lines),
                        }
                    )
        return {"nodes": nodes, "edges": edges, "truncated": truncated}

    def node(self, sid: str, role: str | None = None, depth: int | None = None) -> dict:
        sym = self.index.symbols[sid]
        change = self.changes.get(sid)
        return {
            "id": sid,
            "name": sym.name,
            "qual": sym.qual,
            "label": sym.label,
            "kind": sym.kind,
            "path": sym.path,
            "line": sym.start,
            "end": sym.end,
            "language": sym.language,
            "category": self.category_of(sym.path),
            "change": change["status"] if change else None,
            "in_pr": sym.path in self.changed_paths,
            "role": role,
            "depth": depth,
            "callers": len(self.index.inn.get(sid, ())),
            "callees": len(self.index.out.get(sid, ())),
        }
