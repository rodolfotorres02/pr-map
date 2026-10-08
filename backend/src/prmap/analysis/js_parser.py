"""Extract symbols, calls, references, imports and exports from JS/TS/JSX/TSX.

Symbols are module-level functions/classes/arrow-function constants, class
methods (including arrow-function fields), members of module-level object
literals (``export const api = { load() {} }``) and TS interfaces/types/enums.
Functions nested inside functions (event handlers, callbacks) are attributed to
the enclosing symbol.
"""

from __future__ import annotations

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

from prmap.analysis.common import MODULE, Collector, text
from prmap.analysis.python_parser import Scope

_LANGS = {
    "javascript": Language(tree_sitter_javascript.language()),
    "jsx": Language(tree_sitter_javascript.language()),
    "typescript": Language(tree_sitter_typescript.language_typescript()),
    "tsx": Language(tree_sitter_typescript.language_tsx()),
}

_FUNCTION_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
_CLASS_DECLS = {"class_declaration", "abstract_class_declaration"}
_SKIP = {"comment", "string", "regex", "import_statement", "property_identifier",
         "statement_identifier", "hash_bang_line", "html_comment"}


def dotted(node: Node | None) -> str | None:
    if node is None:
        return None
    if node.type in ("identifier", "this", "type_identifier"):
        return text(node)
    if node.type == "member_expression":
        obj = dotted(node.child_by_field_name("object"))
        prop = node.child_by_field_name("property")
        if obj and prop is not None and prop.type in ("property_identifier", "private_property_identifier"):
            return f"{obj}.{text(prop)}"
    if node.type == "nested_type_identifier":
        return text(node).replace(" ", "")
    return None


def chain_root(node: Node | None) -> str | None:
    while node is not None:
        if node.type == "new_expression":
            return dotted(node.child_by_field_name("constructor"))
        name = dotted(node)
        if name:
            return name
        if node.type == "call_expression":
            node = node.child_by_field_name("function")
        elif node.type == "member_expression":
            node = node.child_by_field_name("object")
        elif node.type in ("await_expression", "parenthesized_expression", "as_expression",
                           "non_null_expression", "satisfies_expression"):
            node = node.named_children[0] if node.named_children else None
        else:
            return None
    return None


def _is_function_like(value: Node | None) -> bool:
    if value is None:
        return False
    if value.type in _FUNCTION_VALUES:
        return True
    # memo(() => ...), forwardRef(function X() {}), styled(...) wrappers
    if value.type == "call_expression":
        args = value.child_by_field_name("arguments")
        return args is not None and any(a.type in _FUNCTION_VALUES for a in args.named_children)
    return False


def _string_value(node: Node | None) -> str | None:
    if node is None or node.type not in ("string", "template_string"):
        return None
    return text(node)[1:-1]


class JsVisitor:
    def __init__(self, path: str, language: str):
        self.c = Collector(path, language)

    def visit(self, node: Node, scope: Scope) -> None:
        handler = getattr(self, f"on_{node.type}", None) if node.is_named else None
        if handler is not None:
            handler(node, scope)
            return
        if node.type in _SKIP:
            if node.type == "import_statement":
                self.handle_import(node)
            return
        for child in node.children:
            self.visit(child, scope)

    def visit_children(self, node: Node | None, scope: Scope) -> None:
        if node is not None:
            for child in node.children:
                self.visit(child, scope)

    def qual(self, scope: Scope, name: str) -> str:
        return f"{scope.prefix}.{name}" if scope.prefix else name

    # -- declarations -------------------------------------------------------

    def on_function_declaration(self, node: Node, scope: Scope) -> None:
        self.function(node, scope, text(node.child_by_field_name("name")) or "default")

    on_generator_function_declaration = on_function_declaration

    def function(self, node: Node, scope: Scope, name: str, span: Node | None = None) -> str | None:
        """Define (when at module/object level) and visit a function-like node."""
        if scope.kind in ("module", "object"):
            qual = self.qual(scope, name)
            ret = node.child_by_field_name("return_type")
            extra = {"returns": text(ret).lstrip(":").strip()} if ret is not None else {}
            kind = "method" if scope.kind == "object" else "function"
            self.c.symbol(qual, name, kind, span or node, scope.prefix or None, **extra)
            inner = Scope(qual, "function", qual, None)
        else:
            qual, inner = None, scope
        self.visit_function_body(node, inner)
        return qual

    def visit_function_body(self, node: Node, inner: Scope, cls: str | None = None) -> None:
        # Unwrap memo(() => ...)-style wrappers.
        if node.type == "call_expression":
            self.visit(node.child_by_field_name("function"), inner)
            args = node.child_by_field_name("arguments")
            for arg in args.named_children if args is not None else []:
                if arg.type in _FUNCTION_VALUES:
                    self.visit_function_body(arg, inner, cls)
                else:
                    self.visit(arg, inner)
            return
        params = node.child_by_field_name("parameters") or node.child_by_field_name("parameter")
        if params is not None:
            self.visit_parameters(params, inner, cls)
        ret = node.child_by_field_name("return_type")
        if ret is not None:
            self.visit(ret, inner)
        body = node.child_by_field_name("body")
        if body is not None:
            self.visit(body, inner)

    def bind_pattern(self, node: Node | None, scope: Scope) -> None:
        """Record identifiers bound by a declaration/parameter pattern as locals."""
        if node is None or scope.kind != "function":
            return
        if node.type in ("identifier", "shorthand_property_identifier_pattern"):
            self.c.local(scope.qual, text(node))
            return
        for child in node.named_children:
            if child.type == "pair_pattern":
                self.bind_pattern(child.child_by_field_name("value"), scope)
            elif child.type in ("assignment_pattern", "object_assignment_pattern"):
                self.bind_pattern(child.child_by_field_name("left"), scope)
            elif child.type in ("identifier", "shorthand_property_identifier_pattern", "object_pattern",
                                "array_pattern", "rest_pattern", "required_parameter", "optional_parameter"):
                if child.type in ("required_parameter", "optional_parameter"):
                    self.bind_pattern(child.child_by_field_name("pattern"), scope)
                else:
                    self.bind_pattern(child, scope)

    def visit_parameters(self, params: Node, inner: Scope, cls: str | None) -> None:
        if params.type == "identifier":  # x => ...
            if inner.kind == "function":
                self.c.local(inner.qual, text(params))
            return
        self.bind_pattern(params, inner)
        for param in params.named_children:
            if param.type in ("required_parameter", "optional_parameter"):
                pattern = param.child_by_field_name("pattern")
                type_node = param.child_by_field_name("type")
                if type_node is not None and pattern is not None and pattern.type == "identifier":
                    type_text = text(type_node).lstrip(":").strip()
                    self.c.var_type(inner.qual, text(pattern), type_text)
                    if cls and any(c.type == "accessibility_modifier" for c in param.children):
                        self.c.var_type(cls, f"this.{text(pattern)}", type_text)
                if type_node is not None:
                    self.visit(type_node, inner)
                value = param.child_by_field_name("value")
                if value is not None:
                    self.visit(value, inner)
            elif param.type == "assignment_pattern":
                right = param.child_by_field_name("right")
                if right is not None:
                    self.visit(right, inner)

    def on_class_declaration(self, node: Node, scope: Scope, name: str | None = None) -> None:
        name = name or text(node.child_by_field_name("name")) or "default"
        bases: list[str] = []
        heritage = next((c for c in node.children if c.type == "class_heritage"), None)
        if heritage is not None:
            for clause in heritage.named_children:
                if clause.type in ("extends_clause", "implements_clause"):
                    for part in clause.named_children:
                        base = dotted(part) or dotted(part.child_by_field_name("value"))
                        if base:
                            bases.append(base)
                elif dotted(clause):  # plain JS: `extends Base`
                    bases.append(dotted(clause))
            self.visit_children(heritage, scope)

        if scope.kind == "module":
            qual = self.qual(scope, name)
            self.c.symbol(qual, name, "class", node, None, bases=bases)
            cls_scope = Scope(qual, "class", qual, qual)
        else:
            cls_scope = scope
        body = node.child_by_field_name("body")
        if body is not None:
            for member in body.named_children:
                self.class_member(member, cls_scope)

    on_abstract_class_declaration = on_class_declaration

    def on_class(self, node: Node, scope: Scope) -> None:  # class expression
        self.on_class_declaration(node, scope, text(node.child_by_field_name("name")) or None)

    def class_member(self, member: Node, scope: Scope) -> None:
        if scope.kind != "class":
            self.visit(member, scope)
            return
        if member.type == "method_definition":
            name = text(member.child_by_field_name("name"))
            qual = self.qual(scope, name)
            ret = member.child_by_field_name("return_type")
            extra = {"returns": text(ret).lstrip(":").strip()} if ret is not None else {}
            self.c.symbol(qual, name, "method", member, scope.prefix, **extra)
            self.visit_function_body(member, Scope(qual, "function", qual, scope.cls), cls=scope.cls)
        elif member.type in ("field_definition", "public_field_definition"):
            name_node = member.child_by_field_name("name") or member.child_by_field_name("property")
            name = text(name_node)
            value = member.child_by_field_name("value")
            type_node = member.child_by_field_name("type")
            if type_node is not None:
                self.c.var_type(scope.cls, f"this.{name}", text(type_node).lstrip(":").strip())
            if _is_function_like(value):
                qual = self.qual(scope, name)
                self.c.symbol(qual, name, "method", member, scope.prefix)
                self.visit_function_body(value, Scope(qual, "function", qual, scope.cls), cls=scope.cls)
            elif value is not None:
                root = chain_root(value) if value.type in ("new_expression", "call_expression", "await_expression") else None
                if root:
                    self.c.var_type(scope.cls, f"this.{name}", root)
                self.visit(value, scope)
        else:
            self.visit(member, scope)

    def on_interface_declaration(self, node: Node, scope: Scope) -> None:
        self.type_decl(node, scope, "interface")

    def on_type_alias_declaration(self, node: Node, scope: Scope) -> None:
        self.type_decl(node, scope, "type")

    def on_enum_declaration(self, node: Node, scope: Scope) -> None:
        self.type_decl(node, scope, "enum")

    def type_decl(self, node: Node, scope: Scope, kind: str) -> None:
        name = text(node.child_by_field_name("name"))
        if scope.kind == "module" and name:
            bases = []
            if kind == "interface":
                ext = next((c for c in node.children if c.type == "extends_type_clause"), None)
                if ext is not None:
                    bases = [b for b in (dotted(t) for t in ext.named_children) if b]
            self.c.symbol(name, name, kind, node, None, bases=bases)
            inner = Scope(name, "function", name, None)
        else:
            inner = scope
        for child in node.children:
            if child.type not in ("type_identifier", "identifier") or child != node.child_by_field_name("name"):
                self.visit(child, inner)

    def on_variable_declarator(self, node: Node, scope: Scope) -> None:
        name_node = node.child_by_field_name("name")
        value = node.child_by_field_name("value")
        type_node = node.child_by_field_name("type")
        name = text(name_node) if name_node is not None and name_node.type == "identifier" else None

        # require(): const x = require('./x'); const {a, b: c} = require('./x')
        if value is not None and value.type == "call_expression" and text(value.child_by_field_name("function")) == "require":
            args = value.child_by_field_name("arguments")
            spec = _string_value(args.named_children[0]) if args is not None and args.named_children else None
            if spec:
                self.c.is_module = True
                if name:
                    self.c.imports.append({"local": name, "module": spec, "name": "*"})
                elif name_node is not None and name_node.type == "object_pattern":
                    for prop in name_node.named_children:
                        if prop.type == "shorthand_property_identifier_pattern":
                            self.c.imports.append({"local": text(prop), "module": spec, "name": text(prop)})
                        elif prop.type == "pair_pattern":
                            key = text(prop.child_by_field_name("key"))
                            local = text(prop.child_by_field_name("value"))
                            self.c.imports.append({"local": local, "module": spec, "name": key})
                return

        if name and _is_function_like(value):
            self.bind_pattern(name_node, scope)
            self.function(value, scope, name, span=node.parent if scope.kind == "module" else node)
            return
        if name and value is not None and value.type == "class" and scope.kind == "module":
            self.on_class_declaration(value, scope, name)
            return
        if name and value is not None and value.type == "object" and scope.kind == "module":
            if any(self._object_member_fn(m) for m in value.named_children):
                self.c.symbol(name, name, "object", node.parent or node, None)
                obj_scope = Scope(name, "object", name, None)
                for member in value.named_children:
                    self.object_member(member, obj_scope, scope)
                return

        if name_node is not None:
            self.bind_pattern(name_node, scope)
        if name and type_node is not None:
            self.c.var_type(scope.qual, name, text(type_node).lstrip(":").strip())
        elif name and value is not None:
            root = chain_root(value) if value.type in ("new_expression", "call_expression", "await_expression") else None
            if root:
                self.c.var_type(scope.qual, name, root)
        if type_node is not None:
            self.visit(type_node, scope)
        if name_node is not None and name_node.type != "identifier":
            self._visit_pattern_defaults(name_node, scope)
        if value is not None:
            self.visit(value, scope)

    def _visit_pattern_defaults(self, pattern: Node, scope: Scope) -> None:
        for child in pattern.named_children:
            if child.type in ("assignment_pattern", "object_assignment_pattern"):
                right = child.child_by_field_name("right")
                if right is not None:
                    self.visit(right, scope)
            elif child.type in ("object_pattern", "array_pattern", "pair_pattern"):
                self._visit_pattern_defaults(child, scope)

    @staticmethod
    def _object_member_fn(member: Node) -> bool:
        if member.type == "method_definition":
            return True
        return member.type == "pair" and _is_function_like(member.child_by_field_name("value"))

    def object_member(self, member: Node, obj_scope: Scope, outer: Scope) -> None:
        if member.type == "method_definition":
            name = text(member.child_by_field_name("name"))
            self.function(member, obj_scope, name)
        elif member.type == "pair" and _is_function_like(member.child_by_field_name("value")):
            key = text(member.child_by_field_name("key")).strip("'\"")
            self.function(member.child_by_field_name("value"), obj_scope, key, span=member)
        else:
            self.visit(member, Scope(obj_scope.qual, "function", obj_scope.prefix, None))

    # -- exports ------------------------------------------------------------

    def on_export_statement(self, node: Node, scope: Scope) -> None:
        self.c.is_module = True
        source = _string_value(node.child_by_field_name("source"))
        declaration = node.child_by_field_name("declaration")
        is_default = any(c.type == "default" for c in node.children)

        if declaration is not None:
            before = {s["qual"] for s in self.c.symbols}
            self.visit(declaration, scope)
            new = [s for s in self.c.symbols if s["qual"] not in before and s["parent"] is None]
            for sym in new:
                self.c.exports[sym["name"]] = {"local": sym["qual"]}
            if is_default and new:
                self.c.exports["default"] = {"local": new[0]["qual"]}
            return

        clause = next((c for c in node.named_children if c.type == "export_clause"), None)
        namespace = next((c for c in node.named_children if c.type == "namespace_export"), None)
        if clause is not None:
            for spec in clause.named_children:
                local = text(spec.child_by_field_name("name"))
                exported = text(spec.child_by_field_name("alias")) or local
                if source:
                    self.c.exports[exported] = {"from": source, "name": local}
                else:
                    self.c.exports[exported] = {"local": local}
            return
        if namespace is not None and source:
            alias = text(namespace.named_children[-1]) if namespace.named_children else ""
            self.c.exports[alias] = {"from": source, "name": "*"}
            return
        if source and any(c.type == "*" for c in node.children):
            self.c.star_exports.append(source)
            return

        value = node.child_by_field_name("value")
        if is_default and value is not None:
            if value.type == "identifier":
                self.c.exports["default"] = {"local": text(value)}
                self.visit(value, scope)
            elif value.type in _FUNCTION_VALUES:
                qual = self.function(value, scope, text(value.child_by_field_name("name")) or "default")
                self.c.exports["default"] = {"local": qual}
            elif value.type == "class":
                self.on_class_declaration(value, scope, text(value.child_by_field_name("name")) or "default")
                self.c.exports["default"] = {"local": text(value.child_by_field_name("name")) or "default"}
            else:
                # export default connect(mapState)(Component) -> guess Component
                idents = [text(n) for n in _identifiers(value)]
                self.c.exports["default"] = {"candidates": idents[::-1]}
                self.visit(value, scope)

    def on_assignment_expression(self, node: Node, scope: Scope) -> None:
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        target = dotted(left)
        if target and scope.kind == "module" and (target.startswith("module.exports") or target.startswith("exports.")):
            self.c.is_module = True
            name = target.split(".")[-1] if target not in ("module.exports",) else "default"
            if right is not None and _is_function_like(right):
                qual = self.function(right, scope, name if name != "exports" else "default")
                self.c.exports[name] = {"local": qual}
                return
            if right is not None and right.type == "identifier":
                self.c.exports[name] = {"local": text(right)}
            if target == "module.exports" and right is not None and right.type == "object":
                for member in right.named_children:
                    if member.type == "shorthand_property_identifier":
                        self.c.exports[text(member)] = {"local": text(member)}
                    elif member.type == "pair" and member.child_by_field_name("value").type == "identifier":
                        key = text(member.child_by_field_name("key")).strip("'\"")
                        self.c.exports[key] = {"local": text(member.child_by_field_name("value"))}
        elif target and scope.kind == "module" and target.count(".") == 1 and right is not None \
                and dotted(right) and not target.startswith("this."):
            owner, _, member = target.partition(".")
            self.c.static_members.append({"owner": owner, "name": member, "value": dotted(right)})
        elif target and target.startswith("this.") and scope.cls and right is not None:
            root = chain_root(right) if right.type in ("new_expression", "call_expression", "await_expression") else None
            if root:
                self.c.var_type(scope.cls, target, root)
        if left is not None and not target:
            self.visit(left, scope)
        if right is not None:
            self.visit(right, scope)

    # -- expressions --------------------------------------------------------

    def on_call_expression(self, node: Node, scope: Scope) -> None:
        fn = node.child_by_field_name("function")
        if fn is not None:
            if fn.type == "identifier":
                if text(fn) != "require":
                    self.c.call(scope.qual, "name", text(fn), None, fn)
            elif fn.type == "super":
                self.c.call(scope.qual, "super", "constructor", None, fn)
            elif fn.type == "member_expression":
                obj = fn.child_by_field_name("object")
                prop = text(fn.child_by_field_name("property"))
                if obj is not None and obj.type == "super":
                    self.c.call(scope.qual, "super", prop, None, fn)
                else:
                    recv = dotted(obj)
                    self.c.call(scope.qual, "attr", prop, recv, fn)
                    if recv is None and obj is not None:
                        self.visit(obj, scope)
            elif fn.type != "import":
                self.visit(fn, scope)
        for child in node.children:
            if child.type in ("arguments", "template_string"):
                self.visit(child, scope)

    def on_new_expression(self, node: Node, scope: Scope) -> None:
        ctor = node.child_by_field_name("constructor")
        name = dotted(ctor)
        if name:
            recv, _, attr = name.rpartition(".")
            self.c.call(scope.qual, "new", attr, recv or None, ctor)
        elif ctor is not None:
            self.visit(ctor, scope)
        args = node.child_by_field_name("arguments")
        if args is not None:
            self.visit(args, scope)

    def jsx_name(self, node: Node, scope: Scope) -> None:
        name_node = node.child_by_field_name("name")
        name = dotted(name_node)
        if name and name[:1].isupper():
            recv, _, attr = name.rpartition(".")
            self.c.call(scope.qual, "jsx", attr, recv or None, name_node)
        for child in node.children:
            if child.type in ("jsx_attribute", "jsx_expression", "type_arguments"):
                self.visit(child, scope)

    on_jsx_opening_element = jsx_name
    on_jsx_self_closing_element = jsx_name

    def on_jsx_closing_element(self, node: Node, scope: Scope) -> None:
        return

    def on_jsx_attribute(self, node: Node, scope: Scope) -> None:
        for child in node.named_children[1:]:
            self.visit(child, scope)

    def on_member_expression(self, node: Node, scope: Scope) -> None:
        name = dotted(node)
        if name:
            if not name.startswith("this."):
                self.c.ref(scope.qual, name)
        else:
            self.visit(node.child_by_field_name("object"), scope)

    def on_identifier(self, node: Node, scope: Scope) -> None:
        self.c.ref(scope.qual, text(node))

    def on_shorthand_property_identifier(self, node: Node, scope: Scope) -> None:
        self.c.ref(scope.qual, text(node))

    def on_type_identifier(self, node: Node, scope: Scope) -> None:
        self.c.ref(scope.qual, text(node))

    def on_nested_type_identifier(self, node: Node, scope: Scope) -> None:
        self.c.ref(scope.qual, text(node).replace(" ", ""))

    def on_pair(self, node: Node, scope: Scope) -> None:
        value = node.child_by_field_name("value")
        key = node.child_by_field_name("key")
        if key is not None and key.type == "computed_property_name":
            self.visit(key, scope)
        if value is not None:
            self.visit(value, scope)

    def on_arrow_function(self, node: Node, scope: Scope) -> None:
        self.visit_function_body(node, scope)

    on_function_expression = on_arrow_function
    on_function = on_arrow_function
    on_generator_function = on_arrow_function

    def on_method_definition(self, node: Node, scope: Scope) -> None:  # object-literal methods
        self.visit_function_body(node, scope)

    # -- imports ------------------------------------------------------------

    def handle_import(self, node: Node) -> None:
        self.c.is_module = True
        source = _string_value(node.child_by_field_name("source"))
        if not source:
            return
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        if clause is None:
            return
        for part in clause.named_children:
            if part.type == "identifier":
                self.c.imports.append({"local": text(part), "module": source, "name": "default"})
            elif part.type == "namespace_import":
                ident = next((c for c in part.named_children if c.type == "identifier"), None)
                if ident is not None:
                    self.c.imports.append({"local": text(ident), "module": source, "name": "*"})
            elif part.type == "named_imports":
                for spec in part.named_children:
                    if spec.type != "import_specifier":
                        continue
                    name = text(spec.child_by_field_name("name"))
                    alias = text(spec.child_by_field_name("alias")) or name
                    self.c.imports.append({"local": alias, "module": source, "name": name})


def _identifiers(node: Node):
    if node.type == "identifier":
        yield node
    for child in node.named_children:
        yield from _identifiers(child)


def analyze_js(path: str, language: str, source: bytes) -> dict:
    lang = _LANGS.get(language, _LANGS["tsx"])
    tree = Parser(lang).parse(source)
    visitor = JsVisitor(path, language)
    visitor.visit(tree.root_node, Scope(MODULE, "module", "", None))
    return visitor.c.result(tree.root_node.end_point[0] + 1)
