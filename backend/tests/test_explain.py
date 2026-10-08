from __future__ import annotations

import json
import stat
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from prmap import explain
from prmap.server import create_app

# Mirrors the stream-json shape of `claude -p --output-format stream-json --verbose
# --include-partial-messages`, and records the prompt and flags it was given.
FAKE_CLAUDE = """#!/usr/bin/env python3
import json, sys
prompt = sys.stdin.read()
open(sys.argv[0] + ".prompt", "w").write(prompt)
open(sys.argv[0] + ".args", "w").write(json.dumps(sys.argv[1:]))
def emit(event):
    print(json.dumps(event), flush=True)
emit({"type": "system", "subtype": "init", "model": "claude-test"})
if "FAIL_LOGIN" in prompt:
    emit({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login"})
    sys.exit(1)
emit({"type": "system", "subtype": "commands_changed", "commands": [{"name": "x" * 70000}]})
for chunk in ["### What changed\\n", "`audit` now ", "totals orders."]:
    emit({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": chunk}}})
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "### What changed\\n`audit` now totals orders."}]}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": "done", "total_cost_usd": 0.0123, "duration_ms": 42})
"""


@pytest.fixture()
def fake_claude(tmp_path, monkeypatch) -> Path:
    script = tmp_path / "claude"
    script.write_text(FAKE_CLAUDE)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PRMAP_CLAUDE_BIN", str(script))
    return script


def _client(fixture_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("PRMAP_CACHE_DIR", str(tmp_path / "cache"))
    client = TestClient(create_app(fixture_repo))
    review = client.post("/api/reviews", json={"source": "branches", "base": "main", "head": "feature"}).json()
    for _ in range(200):
        if client.get(f"/api/reviews/{review['id']}/status").json()["state"] == "ready":
            break
        time.sleep(0.05)
    return client, review["id"]


def _events(response) -> list[dict]:
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def test_prompt_contains_diff_source_callers_and_signals(manager, review):
    prompt = explain.build_prompt(manager, review, "shop/services.py::audit")
    assert "function `audit` in `shop/services.py`, status: added" in prompt
    assert "```diff" in prompt and "+def audit(order):" in prompt
    assert "## Source at head" in prompt and "return order.total()" in prompt
    assert "`Checkout.complete`" in prompt  # caller
    assert "`test_audit`" in prompt  # test caller
    assert "Order.total" in prompt and "guess" in prompt  # fuzzy callee is labelled
    assert "Tested: yes" in prompt


def test_prompt_for_deleted_symbol_uses_base_source(manager, review):
    prompt = explain.build_prompt(manager, review, "shop/services.py::legacy_discount")
    assert "status: deleted" in prompt
    assert "## Source at base" in prompt and "return 0" in prompt


def test_explain_streams_and_caches(fixture_repo, tmp_path, monkeypatch, fake_claude):
    client, rid = _client(fixture_repo, tmp_path, monkeypatch)
    assert client.get("/api/repo").json()["claude_available"] is True

    response = client.post(f"/api/reviews/{rid}/explain", json={"symbol_id": "shop/services.py::audit"})
    events = _events(response)
    assert events[0] == {"type": "meta", "model": "claude-test"}
    text = "".join(e["text"] for e in events if e["type"] == "text")
    assert text == "### What changed\n`audit` now totals orders."  # deltas only, no duplicate full message
    assert events[-1] == {"type": "done", "cost_usd": 0.0123, "duration_ms": 42}

    args = json.loads(Path(str(fake_claude) + ".args").read_text())
    assert args[:2] == ["-p", "--output-format"]
    assert args[args.index("--tools") + 1] == ""  # no tools: Claude can't touch the repo
    assert "--strict-mcp-config" in args and "--system-prompt" in args
    assert "Checkout.complete" in Path(str(fake_claude) + ".prompt").read_text()

    Path(str(fake_claude) + ".prompt").unlink()
    again = _events(client.post(f"/api/reviews/{rid}/explain", json={"symbol_id": "shop/services.py::audit"}))
    assert again[-1]["cached"] is True
    assert not Path(str(fake_claude) + ".prompt").exists()  # served from cache, claude not run

    client.post(f"/api/reviews/{rid}/explain", json={"symbol_id": "shop/services.py::audit", "refresh": True})
    assert Path(str(fake_claude) + ".prompt").exists()


def test_explain_reports_login_errors(fixture_repo, tmp_path, monkeypatch, fake_claude):
    client, rid = _client(fixture_repo, tmp_path, monkeypatch)
    monkeypatch.setattr(explain, "build_prompt", lambda *a: "FAIL_LOGIN")
    events = _events(client.post(f"/api/reviews/{rid}/explain", json={"symbol_id": "shop/services.py::audit"}))
    assert events[-1]["type"] == "error"
    assert "isn't logged in" in events[-1]["message"]


def test_explain_unknown_symbol_is_a_400(fixture_repo, tmp_path, monkeypatch, fake_claude):
    client, rid = _client(fixture_repo, tmp_path, monkeypatch)
    response = client.post(f"/api/reviews/{rid}/explain", json={"symbol_id": "nope.py::missing"})
    assert response.status_code == 400
