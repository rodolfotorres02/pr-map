"""Extract symbols, calls, references, imports and local type hints from Python.

Symbols are module-level functions/classes, methods and nested classes. Code in
functions nested inside functions is attributed to the enclosing function so the
graph stays at the granularity a reviewer cares about.
"""

from __future__ import annotations

from dataclasses import dataclass

import tree_sitter_python
from tree_sitter import Language, Node, Parser

from prmap.analysis.common import MODULE, Collector, text

PY_LANGUAGE = Language(tree_sitter_python.language())

_SKIP = {"string", "concatenated_string", "comment", "future_import_statement"}


@dataclass(frozen=True)
class Scope:
    qual: str  # symbol that calls are attributed to (MODULE at top level)
    kind: str  # module | class | function
    prefix: str  # qualified-name prefix for symbols defined here
    cls: str | None  # enclosing class (for self/cls/super resolution)


def dotted(node: Node | None) -> str | None:
    """'a.b.c' for an identifier/attribute chain, otherwise None."""
    if node is None:
        return None
    if node.type == "identifier":
        return text(node)
    if node.type == "attribute":
        obj = dotted(node.child_by_field_name("object"))
        attr = node.child_by_field_name("attribute")
        if obj and attr is not None:
            return f"{obj}.{text(attr)}"
    return None


def chain_root(node: Node | None) -> str | None:
    """Leftmost dotted prefix of a call chain: ``User.objects.filter(x).first()`` -> ``User.objects.filter``."""
    while node is not None:
        name = dotted(node)
        if name:
            return name
        if node.type == "call":
            node = node.child_by_field_name("function")
        elif node.type == "attribute":
            node = node.child_by_field_name("object")
        elif node.type == "await":
            node = node.named_children[0] if node.named_children else None
        else:
            return None
    return None


class PythonVisitor:
    def __init__(self, path: str):
        self.c = Collector(path, "python")

    # -- definitions --------------------------------------------------------

    def visit(self, node: Node, scope: Scope) -> None:
        handler = getattr(self, f"on_{node.type}", None) if node.is_named else None
        if handler is not None:
            handler(node, scope)
            return
        if node.type in _SKIP:
            return
        for child in node.children:
            self.visit(child, scope)

    def on_decorated_definition(self, node: Node, scope: Scope) -> None:
        definition = node.child_by_field_name("definition")
        target_scope = self.define(definition, scope, decorated=node) if definition else scope
        for child in node.children:
            if child.type == "decorator":
                for expr in child.named_children:
                    self.visit(expr, target_scope)

    def on_function_definition(self, node: Node, scope: Scope) -> None:
        self.define(node, scope)

    def on_class_definition(self, node: Node, scope: Scope) -> None:
        self.define(node, scope)

    def define(self, node: Node, scope: Scope, decorated: Node | None = None) -> Scope:
        """Register a def/class and visit its body. Returns the scope of the body."""
        name = text(node.child_by_field_name("name"))
        span = decorated or node
        is_symbol = scope.kind in ("module", "class") and bool(name)

        if node.type == "class_definition":
            supers = node.child_by_field_name("superclasses")
            bases = []
            if supers is not None:
                for arg in supers.named_children:
                    base = dotted(arg)
                    if base and base not in ("object", "ABC", "Generic", "Protocol"):
                        bases.append(base)
                    if arg.type != "keyword_argument":
                        self.visit(arg, scope)
            if is_symbol:
                qual = f"{scope.prefix}.{name}" if scope.prefix else name
                self.c.symbol(qual, name, "class", span, scope.prefix or None, bases=bases)
                inner = Scope(qual, "class", qual, qual)
            else:
                inner = scope
            body = node.child_by_field_name("body")
            if body is not None:
                self.visit(body, inner)
            return inner

        # function_definition
        if is_symbol:
            qual = f"{scope.prefix}.{name}" if scope.prefix else name
            kind = "method" if scope.kind == "class" else "function"
            returns = node.child_by_field_name("return_type")
            extra = {"returns": text(returns)} if returns is not None else {}
            self.c.symbol(qual, name, kind, span, scope.prefix or None, **extra)
            inner = Scope(qual, "function", qual, scope.cls if scope.kind == "class" else None)
        else:
            inner = scope
        params = node.child_by_field_name("parameters")
        if params is not None:
            self.visit_parameters(params, inner, outer=scope)
        body = node.child_by_field_name("body")
        if body is not None:
            self.visit(body, inner)
        return inner

    def visit_parameters(self, params: Node, inner: Scope, outer: Scope) -> None:
        for param in params.named_children:
            self.bind_targets(param, inner)
            if param.type in ("typed_parameter", "typed_default_parameter"):
                type_node = param.child_by_field_name("type")
                name_node = param.child_by_field_name("name")
                if name_node is None:
                    name_node = next((c for c in param.named_children if c.type == "identifier"), None)
                if type_node is not None and name_node is not None:
                    self.c.var_type(inner.qual, text(name_node), text(type_node))
                    self.visit(type_node, inner)
            if param.type in ("default_parameter", "typed_default_parameter"):
                value = param.child_by_field_name("value")
                if value is not None:
                    self.visit(value, outer)

    def bind_targets(self, node: Node | None, scope: Scope) -> None:
        """Record names bound by an assignment target / parameter as function locals."""
        if node is None or scope.kind != "function":
            return
        if node.type == "identifier":
            self.c.local(scope.qual, text(node))
        elif node.type in ("pattern_list", "tuple_pattern", "list_pattern", "list_splat_pattern",
                           "dictionary_splat_pattern", "typed_parameter", "parenthesized_expression"):
            for child in node.named_children:
                if child.type != "type":
                    self.bind_targets(child, scope)
        elif node.type in ("default_parameter", "typed_default_parameter"):
            self.bind_targets(node.child_by_field_name("name"), scope)

    def on_for_statement(self, node: Node, scope: Scope) -> None:
        self.bind_targets(node.child_by_field_name("left"), scope)
        for child in node.children:
            if child != node.child_by_field_name("left"):
                self.visit(child, scope)

    def on_for_in_clause(self, node: Node, scope: Scope) -> None:
        self.bind_targets(node.child_by_field_name("left"), scope)
        right = node.child_by_field_name("right")
        if right is not None:
            self.visit(right, scope)

    def on_as_pattern(self, node: Node, scope: Scope) -> None:
        for child in node.named_children:
            if child.type == "as_pattern_target":
                for target in child.named_children:
                    self.bind_targets(target, scope)
            else:
                self.visit(child, scope)

    def on_named_expression(self, node: Node, scope: Scope) -> None:
        self.bind_targets(node.child_by_field_name("name"), scope)
        value = node.child_by_field_name("value")
        if value is not None:
            self.visit(value, scope)

    # -- expressions --------------------------------------------------------

    def on_call(self, node: Node, scope: Scope) -> None:
        fn = node.child_by_field_name("function")
        if fn is not None:
            if fn.type == "identifier":
                self.c.call(scope.qual, "name", text(fn), None, fn)
            elif fn.type == "attribute":
                obj = fn.child_by_field_name("object")
                attr = text(fn.child_by_field_name("attribute"))
                if obj is not None and obj.type == "call" and text(obj.child_by_field_name("function")) == "super":
                    self.c.call(scope.qual, "super", attr, None, fn)
                else:
                    recv = dotted(obj)
                    self.c.call(scope.qual, "attr", attr, recv, fn)
                    if recv is None and obj is not None:
                        self.visit(obj, scope)
            else:
                self.visit(fn, scope)
        args = node.child_by_field_name("arguments")
        if args is not None:
            self.visit(args, scope)

    def on_attribute(self, node: Node, scope: Scope) -> None:
        name = dotted(node)
        if name:
            self.c.ref(scope.qual, name)
        else:
            obj = node.child_by_field_name("object")
            if obj is not None:
                self.visit(obj, scope)

    def on_identifier(self, node: Node, scope: Scope) -> None:
        self.c.ref(scope.qual, text(node))

    def on_keyword_argument(self, node: Node, scope: Scope) -> None:
        value = node.child_by_field_name("value")
        if value is not None:
            self.visit(value, scope)

    def on_lambda(self, node: Node, scope: Scope) -> None:
        body = node.child_by_field_name("body")
        if body is not None:
            self.visit(body, scope)

    def on_assignment(self, node: Node, scope: Scope) -> None:
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        annotation = node.child_by_field_name("type")
        target = dotted(left)
        if left is not None and left.type in ("identifier", "pattern_list", "tuple_pattern", "list_pattern"):
            self.bind_targets(left, scope)
        if target:
            type_scope = scope.qual
            if target.startswith("self.") and scope.cls:
                type_scope = scope.cls  # instance attributes are visible to every method
            if annotation is not None:
                self.c.var_type(type_scope, target, text(annotation))
            elif right is not None and right.type == "identifier":
                # self.repo = repo  -> inherit the parameter's annotated type
                known = self.c.types.get(scope.qual, {}).get(text(right))
                if known:
                    self.c.var_type(type_scope, target, known)
            elif right is not None and right.type in ("call", "await"):
                root = chain_root(right)
                if root:
                    self.c.var_type(type_scope, target, root)
        if left is not None and not target and left.type not in ("pattern_list", "tuple_pattern", "list_pattern"):
            self.visit(left, scope)  # subscripts may contain real references
        if annotation is not None:
            self.visit(annotation, scope)
        if right is not None:
            self.visit(right, scope)

    # -- imports ------------------------------------------------------------

    def on_import_statement(self, node: Node, scope: Scope) -> None:
        self.handle_import(node)

    def on_import_from_statement(self, node: Node, scope: Scope) -> None:
        self.handle_import(node)

    def handle_import(self, node: Node) -> None:
        if node.type == "import_statement":
            for child in node.named_children:
                if child.type == "dotted_name":
                    full = text(child)
                    head = full.split(".")[0]
                    self.c.imports.append({"local": head, "module": head, "name": None})
                    if "." in full:
                        # `import a.b.c` also makes a.b.c reachable through `a`.
                        self.c.imports.append({"local": full, "module": full, "name": None})
                elif child.type == "aliased_import":
                    module = text(child.child_by_field_name("name"))
                    alias = text(child.child_by_field_name("alias"))
                    self.c.imports.append({"local": alias, "module": module, "name": None})
            return

        if node.type == "import_from_statement":
            module_node = node.child_by_field_name("module_name")
            module = text(module_node).replace(" ", "")
            names = node.children_by_field_name("name")
            if any(c.type == "wildcard_import" for c in node.named_children):
                self.c.imports.append({"local": "*", "module": module, "name": "*"})
            for item in names:
                if item.type == "aliased_import":
                    name = text(item.child_by_field_name("name"))
                    local = text(item.child_by_field_name("alias"))
                else:
                    name = local = text(item)
                self.c.imports.append({"local": local, "module": module, "name": name})


def analyze_python(path: str, source: bytes) -> dict:
    parser = Parser(PY_LANGUAGE)
    tree = parser.parse(source)
    visitor = PythonVisitor(path)
    visitor.visit(tree.root_node, Scope(MODULE, "module", "", None))
    return visitor.c.result(tree.root_node.end_point[0] + 1)
