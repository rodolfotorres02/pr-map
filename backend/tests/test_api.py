from __future__ import annotations

import time

from fastapi.testclient import TestClient

from prmap.server import create_app


def test_api_round_trip(fixture_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("PRMAP_CACHE_DIR", str(tmp_path))
    client = TestClient(create_app(fixture_repo))

    info = client.get("/api/repo").json()
    assert {b["name"] for b in info["branches"]} >= {"main", "feature"}
    assert info["current_branch"] == "feature"

    review = client.post("/api/reviews", json={"source": "branches", "base": "main", "head": "feature"}).json()
    assert review["stats"]["files"] == len(review["files"])
    rid = review["id"]

    for _ in range(200):
        status = client.get(f"/api/reviews/{rid}/status").json()
        if status["state"] in ("ready", "error"):
            break
        time.sleep(0.05)
    assert status["state"] == "ready"

    changes = client.get(f"/api/reviews/{rid}/changes").json()["changes"]
    assert any(c["id"] == "shop/services.py::audit" for c in changes)

    detail = client.get(f"/api/reviews/{rid}/file", params={"path": "shop/services.py"}).json()
    assert detail["diff"]["hunks"]

    graph = client.post(f"/api/reviews/{rid}/graph", json={"seeds": ["shop/services.py::audit"]}).json()
    assert any(n["role"] == "caller" for n in graph["nodes"])

    results = client.get(f"/api/reviews/{rid}/search", params={"q": "format"}).json()["results"]
    assert results[0]["name"] in ("format_receipt", "formatMoney")

    source = client.get(
        f"/api/reviews/{rid}/source", params={"path": "shop/services.py", "start": 1, "end": 3}
    ).json()
    assert source["lines"][0].startswith("from shop.models")

    bad = client.post("/api/reviews", json={"source": "branches", "base": "nope", "head": "feature"})
    assert bad.status_code == 400


def test_empty_comparisons_explain_themselves(fixture_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("PRMAP_CACHE_DIR", str(tmp_path))
    client = TestClient(create_app(fixture_repo))

    same = client.post("/api/reviews", json={"source": "branches", "base": "feature", "head": "feature"})
    assert same.status_code == 400
    assert "point to the same commit" in same.json()["detail"]
    assert "uncommitted" not in same.json()["detail"]

    behind = client.post("/api/reviews", json={"source": "branches", "base": "feature", "head": "main"})
    assert behind.status_code == 400
    assert "already in 'feature'" in behind.json()["detail"]

    assert client.get("/api/repo").json()["uncommitted"] == {"tracked": 0, "untracked": 0}
    clean = client.post(
        "/api/reviews", json={"source": "branches", "base": "feature", "head": "feature", "uncommitted": True}
    )
    assert clean.status_code == 400
    assert "no uncommitted changes either" in clean.json()["detail"]
    elsewhere = client.post(
        "/api/reviews", json={"source": "branches", "base": "feature", "head": "main", "uncommitted": True}
    )
    assert elsewhere.status_code == 400
    assert "checked out" in elsewhere.json()["detail"]
