from __future__ import annotations

import pytest

from prmap.classify import Classifier, builtin_category
from prmap.diffparse import parse_unified_diff
from prmap.indexer import parse_jsonc


def edge(index, src: str, dst: str):
    return index.edges.get((src, dst))


# -- classification -----------------------------------------------------------


@pytest.mark.parametrize(
    "path, category",
    [
        ("shop/views.py", "source"),
        ("tests/test_services.py", "test"),
        ("shop/test_utils.py", "test"),
        ("web/src/components/OrderTable.test.tsx", "test"),
        ("web/src/__tests__/a.js", "test"),
        ("conftest.py", "test"),
        ("shop/templates/shop/checkout.html", "template"),
        ("shop/templates/email.txt", "template"),
        ("web/src/app.scss", "style"),
        ("shop/migrations/0001_initial.py", "migration"),
        ("package-lock.json", "generated"),
        ("static/vendor/jquery.min.js", "generated"),
        ("web/tsconfig.json", "config"),
        (".github/workflows/ci.yml", "config"),
        ("vite.config.ts", "config"),
        ("docs/setup.md", "docs"),
        ("README.md", "docs"),
        ("web/public/logo.svg", "asset"),
        ("web/src/App.tsx", "source"),
    ],
)
def test_builtin_categories(path, category):
    assert builtin_category(path) == category


def test_category_overrides_win():
    classifier = Classifier({"test": ["shop/factories.py"], "generated": ["web/src/api/gen/**"]})
    assert classifier.category("shop/factories.py") == "test"
    assert classifier.category("web/src/api/gen/client.ts") == "generated"
    assert classifier.category("shop/views.py") == "source"


def test_parse_jsonc_handles_comments_and_trailing_commas():
    data = parse_jsonc('{ // c\n "a": "http://x", /* b */ "b": [1,2,], }')
    assert data == {"a": "http://x", "b": [1, 2]}


def test_parse_unified_diff_line_numbers():
    text = "diff --git a/x b/x\n@@ -1,3 +1,4 @@ def f():\n a\n-b\n+B\n+C\n c\n"
    parsed = parse_unified_diff(text)
    lines = parsed["hunks"][0]["lines"]
    assert [(l["type"], l["old"], l["new"]) for l in lines] == [
        ("ctx", 1, 1), ("del", 2, None), ("add", None, 2), ("add", None, 3), ("ctx", 3, 4),
    ]
    assert parsed["hunks"][0]["section"] == "def f():"


# -- review / changed files ---------------------------------------------------


def test_changed_files_and_categories(review):
    files = {f["path"]: f for f in review.files}
    assert files["README.md"]["status"] == "deleted"
    assert files["shop/services.py"]["status"] == "modified"
    assert files["shop/services.py"]["category"] == "source"
    assert files["tests/test_services.py"]["category"] == "test"
    assert files["shop/templates/shop/checkout.html"]["category"] == "template"
    assert files["package-lock.json"]["category"] == "generated"
    assert files["web/src/components/OrderRow.tsx"]["ext"] == ".tsx"
    assert review.commits == 1


def test_changed_symbols(manager, review):
    changes = manager.changes(review)
    status = {sid: c["status"] for sid, c in changes.items()}
    assert status["shop/services.py::Checkout.complete"] == "modified"
    assert status["shop/services.py::audit"] == "added"
    assert status["shop/services.py::legacy_discount"] == "deleted"
    assert status["shop/notify.py::format_receipt"] == "modified"
    assert status["shop/notify.py::Mailer.send_receipt"] == "modified"
    assert status["tests/test_services.py::test_audit"] == "added"
    assert status["web/src/components/OrderRow.tsx::OrderRow"] == "modified"
    # untouched symbols in changed files are not reported
    assert "shop/services.py::Checkout.__init__" not in status
    assert "shop/services.py::Checkout" not in status


# -- python call graph ----------------------------------------------------------


def test_python_cross_module_edges(review):
    index = review.job.index
    complete = "shop/services.py::Checkout.complete"
    # Order resolved through the package __init__ re-export; method via inferred type
    assert edge(index, complete, "shop/models/order.py::Order.mark_paid").fuzzy is False
    # self.mailer typed from the constructor annotation
    assert edge(index, complete, "shop/notify.py::Mailer.send_receipt").fuzzy is False
    assert edge(index, complete, "shop/services.py::audit").kind == "calls"
    # inherited method through self
    assert edge(index, "shop/models/order.py::Order.mark_paid", "shop/models/order.py::BaseModel.save")
    assert edge(index, "shop/models/order.py::Order", "shop/models/order.py::BaseModel").kind == "inherits"
    # `from . import services` + attribute access to a class -> instantiation
    view = "shop/views.py::checkout_view"
    assert edge(index, view, "shop/services.py::Checkout").kind == "instantiates"
    assert edge(index, view, "shop/notify.py::Mailer").kind == "instantiates"
    # urls.py references the view without calling it
    assert edge(index, "shop/urls.py::<module>", view).kind == "references"
    # tests touch the code under test
    assert edge(index, "tests/test_services.py::test_audit", "shop/services.py::audit")


def test_python_locals_do_not_shadow_resolution(review):
    index = review.job.index
    # `order` is a local in audit(); total() resolves fuzzily to the only `total` method
    e = edge(index, "shop/services.py::audit", "shop/models/order.py::Order.total")
    assert e is not None and e.fuzzy is True


# -- js / ts call graph ---------------------------------------------------------


def test_ts_alias_barrel_and_jsx(review):
    index = review.job.index
    table = "web/src/components/OrderTable.tsx::OrderTable"
    assert edge(index, table, "web/src/components/OrderRow.tsx::OrderRow").kind == "renders"
    # tsconfig paths alias "@/store"
    assert edge(index, table, "web/src/store.ts::OrderStore").kind == "instantiates"
    # "@/api" -> index.ts -> export * from "./client"
    assert edge(index, table, "web/src/api/client.ts::ApiClient").kind == "instantiates"
    # local var typed by `new OrderStore()`
    assert edge(index, table, "web/src/store.ts::OrderStore.load").fuzzy is False
    # aliased re-export: export { formatMoney as money }
    assert edge(index, "web/src/components/OrderRow.tsx::OrderRow", "web/src/api/format.ts::formatMoney")


def test_ts_this_typed_by_constructor_parameter_property(review):
    index = review.job.index
    load = "web/src/store.ts::OrderStore.load"
    assert edge(index, load, "web/src/api/client.ts::ApiClient.listOrders").fuzzy is False
    assert edge(index, load, "web/src/store.ts::OrderStore.emit").fuzzy is False
    assert edge(index, "web/src/api/client.ts::ApiClient.listOrders", "web/src/api/client.ts::ApiClient.get")


# -- graph queries --------------------------------------------------------------


def test_neighborhood_directions_and_category_filter(manager, review):
    query = manager.graph_query(review)
    graph = query.neighborhood(["shop/services.py::audit"], depth_in=2, depth_out=1)
    roles = {n["id"]: n["role"] for n in graph["nodes"]}
    assert roles["shop/services.py::audit"] == "focus"
    assert roles["shop/services.py::Checkout.complete"] == "caller"
    assert roles["tests/test_services.py::test_audit"] == "caller"
    assert roles["shop/models/order.py::Order.total"] == "callee"
    # depth 2 upstream reaches the view through Checkout.complete
    assert roles.get("shop/views.py::checkout_view") == "caller"

    no_tests = manager.graph_query(review, exclude_categories=["test"])
    graph = no_tests.neighborhood(["shop/services.py::audit"], depth_in=2, depth_out=1)
    assert all(n["category"] != "test" for n in graph["nodes"])

    exact = manager.graph_query(review, fuzzy=False)
    graph = exact.neighborhood(["shop/services.py::audit"], depth_in=0, depth_out=1)
    assert "shop/models/order.py::Order.total" not in {n["id"] for n in graph["nodes"]}


def test_class_seed_expands_to_members(manager, review):
    graph = manager.graph_query(review).neighborhood(["shop/notify.py::Mailer"], depth_in=1, depth_out=1)
    roles = {n["id"]: n["role"] for n in graph["nodes"]}
    assert roles["shop/notify.py::Mailer.send_receipt"] == "focus"
    assert roles["shop/notify.py::format_receipt"] == "callee"


def test_overview_links_changed_symbols(manager, review):
    graph = manager.graph_query(review).overview(paths=None, neighbors=False)
    ids = {n["id"] for n in graph["nodes"]}
    assert "shop/services.py::Checkout.complete" in ids
    assert "shop/services.py::audit" in ids
    edges = {(e["source"], e["target"]) for e in graph["edges"]}
    assert ("shop/services.py::Checkout.complete", "shop/services.py::audit") in edges
    assert ("shop/notify.py::Mailer.send_receipt", "shop/notify.py::format_receipt") in edges

    only_tests = manager.graph_query(review).overview(paths={"tests/test_services.py"}, neighbors=True)
    roles = {n["id"]: n["role"] for n in only_tests["nodes"]}
    assert roles["tests/test_services.py::test_audit"] == "changed"
    assert roles["shop/services.py::audit"] == "neighbor"


def test_file_detail_lists_symbols_with_change_status(manager, review):
    detail = manager.file_detail(review, "shop/services.py")
    by_qual = {s["qual"]: s["change"] for s in detail["symbols"]}
    assert by_qual["Checkout.complete"] == "modified"
    assert by_qual["audit"] == "added"
    assert by_qual["legacy_discount"] == "deleted"
    assert by_qual["Checkout.__init__"] is None
    assert detail["diff"]["hunks"]
