from __future__ import annotations

import textwrap
import time

from fastapi.testclient import TestClient

from prmap.analysis import analyze_file
from prmap.analysis.index import CodeIndex
from prmap.classify import Classifier
from prmap.plan import build_plan, review_plan
from prmap.server import create_app

COMPLETE = "shop/services.py::Checkout.complete"
AUDIT = "shop/services.py::audit"
TEST_AUDIT = "tests/test_services.py::test_audit"
SEND = "shop/notify.py::Mailer.send_receipt"
FORMAT = "shop/notify.py::format_receipt"
ORDER_ROW = "web/src/components/OrderRow.tsx::OrderRow"


def cluster_of(plan: dict, sid: str) -> dict:
    matches = [c for c in plan["clusters"] if sid in c["symbols"]]
    assert len(matches) == 1, (sid, plan["clusters"])
    return matches[0]


def test_plan_clusters_related_changes(manager, review):
    plan = review_plan(manager, review)
    main = cluster_of(plan, COMPLETE)
    assert {AUDIT, TEST_AUDIT, SEND, FORMAT} <= set(main["symbols"])
    assert cluster_of(plan, ORDER_ROW)["id"] != main["id"]
    assert [c["id"] for c in plan["clusters"]] == [f"c{i}" for i in range(1, len(plan["clusters"]) + 1)]
    assert plan["clusters"][0]["id"] == main["id"]  # biggest first

    assert main["title"] == "Checkout.complete"
    assert main["entry_points"] == [COMPLETE]
    order = main["symbols"]
    assert order[0] == COMPLETE
    assert order.index(COMPLETE) < order.index(AUDIT) < order.index(TEST_AUDIT)
    assert order.index(SEND) < order.index(FORMAT)
    # non-test symbols are read before tests
    tests_start = min(i for i, sid in enumerate(order) if sid.startswith("tests/"))
    assert all(not sid.startswith("tests/") for sid in order[:tests_start])
    assert main["files"][0] == "shop/services.py"
    assert set(main["files"]) == {"shop/services.py", "shop/notify.py", "tests/test_services.py"}
    assert main["categories"]["test"] >= 1 and main["categories"]["source"] == 4
    changes = manager.changes(review)
    assert main["additions"] == sum(changes[s]["additions"] for s in order)
    assert main["deletions"] == sum(changes[s]["deletions"] for s in order)

    # deleted symbols never appear in clusters
    assert all("legacy_discount" not in sid for c in plan["clusters"] for sid in c["symbols"])
    assert review_plan(manager, review) is plan  # cached on the review


def test_plan_unmapped_files(manager, review):
    plan = review_plan(manager, review)
    unmapped = set(plan["unmapped_files"])
    assert {"shop/templates/shop/checkout.html", "package-lock.json", "README.md"} <= unmapped
    covered = {p for c in plan["clusters"] for p in c["files"]}
    assert not covered & unmapped
    assert covered | unmapped == {f["path"] for f in review.files}


def test_plan_risk(manager, review):
    risk = review_plan(manager, review)["risk"]
    assert risk[AUDIT]["tested"] is True
    assert TEST_AUDIT in risk[AUDIT]["test_callers"]
    assert risk[AUDIT]["callers"] == 2  # Checkout.complete + test_audit
    assert risk[AUDIT]["external_callers"] == 0
    # checkout_view (shop/views.py, untouched by the PR) calls Checkout.complete
    assert risk[COMPLETE]["external_callers"] >= 1
    assert risk[COMPLETE]["fan_out"] >= 3
    assert risk[FORMAT]["tested"] is False
    assert len(risk[AUDIT]["test_callers"]) <= 5


def test_plan_deleted_symbols_still_referenced():
    sources = {
        "pkg/__init__.py": "",
        "pkg/b.py": """
            def other():
                return 1
            """,
        "pkg/a.py": """
            from pkg.b import gone, other

            def use():
                return gone(other())
            """,
        "pkg/c.py": """
            def fine():
                return 2
            """,
    }
    analyses = {
        path: analyze_file(path, "python", textwrap.dedent(src).lstrip("\n").encode())
        for path, src in sources.items()
    }
    index = CodeIndex(analyses)
    changes = {
        "pkg/b.py::gone": {
            "id": "pkg/b.py::gone", "path": "pkg/b.py", "qual": "gone", "name": "gone", "kind": "function",
            "start": 4, "end": 5, "status": "deleted", "additions": 0, "deletions": 2, "category": "source",
        },
        "pkg/c.py::fine": {
            "id": "pkg/c.py::fine", "path": "pkg/c.py", "qual": "fine", "name": "fine", "kind": "function",
            "start": 1, "end": 2, "status": "modified", "additions": 1, "deletions": 1, "category": "source",
        },
    }
    files = [
        {"path": "pkg/b.py", "category": "source", "additions": 0, "deletions": 2, "analyzable": True},
        {"path": "pkg/c.py", "category": "source", "additions": 1, "deletions": 1, "analyzable": True},
    ]
    read = lambda path: textwrap.dedent(sources[path]).lstrip("\n")
    plan = build_plan(index, changes, files, Classifier({}).category, read)
    [entry] = plan["deleted_still_referenced"]
    assert entry["id"] == "pkg/b.py::gone" and entry["name"] == "gone" and entry["path"] == "pkg/b.py"
    refs = {(r["path"], r["line"]) for r in entry["references"]}
    assert ("pkg/a.py", 1) in refs  # the import
    assert ("pkg/a.py", 4) in refs  # the call
    assert plan["unmapped_files"] == ["pkg/b.py"]
    assert [c["title"] for c in plan["clusters"]] == ["fine"]


def test_plan_ignores_removed_names_nobody_uses(manager, review):
    assert review_plan(manager, review)["deleted_still_referenced"] == []


def test_overview_with_explicit_symbols(manager, review):
    graph = manager.graph_query(review).overview(paths=None, neighbors=False, symbols={AUDIT, COMPLETE, "nope"})
    roles = {n["id"]: n["role"] for n in graph["nodes"]}
    assert roles == {AUDIT: "changed", COMPLETE: "changed"}


def test_plan_and_overview_api(fixture_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("PRMAP_CACHE_DIR", str(tmp_path))
    client = TestClient(create_app(fixture_repo))
    rid = client.post("/api/reviews", json={"source": "branches", "base": "main", "head": "feature"}).json()["id"]
    for _ in range(200):
        if client.get(f"/api/reviews/{rid}/status").json()["state"] in ("ready", "error"):
            break
        time.sleep(0.05)

    plan = client.get(f"/api/reviews/{rid}/plan").json()
    assert set(plan) == {"clusters", "unmapped_files", "risk", "deleted_still_referenced"}
    first = plan["clusters"][0]
    assert set(first) == {
        "id", "title", "symbols", "entry_points", "files", "additions", "deletions", "categories",
    }
    assert first["title"] == "Checkout.complete"
    assert set(plan["risk"][AUDIT]) == {"callers", "external_callers", "tested", "test_callers", "fan_out"}

    graph = client.post(f"/api/reviews/{rid}/overview", json={"symbols": first["symbols"]}).json()
    changed = {n["id"] for n in graph["nodes"] if n["role"] == "changed"}
    assert {COMPLETE, AUDIT, TEST_AUDIT, SEND, FORMAT} <= changed <= set(first["symbols"])
    assert ORDER_ROW not in {n["id"] for n in graph["nodes"]}

    whole = client.post(f"/api/reviews/{rid}/overview", json={"symbols": None}).json()
    assert ORDER_ROW in {n["id"] for n in whole["nodes"]}

    assert client.get("/api/reviews/nope/plan").status_code == 400


def test_overview_caps_huge_changes(manager, review):
    graph = manager.graph_query(review).overview(paths=None, neighbors=False, max_nodes=5)
    changed = [n for n in graph["nodes"] if n["role"] == "changed"]
    assert graph["truncated"] is True
    assert len(changed) <= 5
