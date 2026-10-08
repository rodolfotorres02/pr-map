"""HTTP API (and static UI) for one local repository."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from prmap import explain, github
from prmap.classify import CATEGORIES
from prmap.graph import ALL_EDGE_KINDS
from prmap.gitrepo import GitRepo
from prmap.plan import review_plan
from prmap.review import ReviewError, ReviewManager

STATIC_DIR = Path(__file__).parent / "static"


class OpenReview(BaseModel):
    source: Literal["pr", "branches"]
    pr: str | None = None
    base: str | None = None
    head: str | None = None
    mode: Literal["merge-base", "direct"] = "merge-base"


class GraphOptions(BaseModel):
    fuzzy: bool = True
    edge_kinds: list[str] = Field(default_factory=lambda: list(ALL_EDGE_KINDS))
    exclude_categories: list[str] = Field(default_factory=list)
    max_nodes: int = Field(default=150, ge=5, le=600)


class NeighborhoodRequest(GraphOptions):
    seeds: list[str]
    depth_in: int = Field(default=2, ge=0, le=8)
    depth_out: int = Field(default=2, ge=0, le=8)


class ExplainRequest(BaseModel):
    symbol_id: str
    refresh: bool = False


class OverviewRequest(GraphOptions):
    paths: list[str] | None = None
    symbols: list[str] | None = None
    neighbors: bool = False
    max_nodes: int = Field(default=250, ge=5, le=800)


def create_app(repo_path: str | Path) -> FastAPI:
    repo = GitRepo(repo_path)
    manager = ReviewManager(repo)
    app = FastAPI(title="prmap", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.manager = manager

    @app.exception_handler(ReviewError)
    async def review_error(_, exc: ReviewError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/api/repo")
    def repo_info():
        return {
            "root": str(repo.root),
            "name": repo.root.name,
            "current_branch": repo.current_branch(),
            "default_branch": repo.default_branch(),
            "branches": repo.branches(),
            "gh_available": github.gh_available(),
            "claude_available": explain.claude_path() is not None,
            "categories": [{"key": k, "label": label} for k, label in CATEGORIES],
            "edge_kinds": list(ALL_EDGE_KINDS),
        }

    @app.get("/api/prs")
    def prs():
        try:
            return {"prs": github.list_prs(repo), "error": None}
        except github.GitHubError as exc:
            return {"prs": [], "error": str(exc)}

    @app.post("/api/reviews")
    def open_review(body: OpenReview):
        if body.source == "pr":
            if not body.pr:
                raise HTTPException(400, "Enter a PR number or URL")
            review = manager.open_pr(body.pr)
        else:
            if not body.base or not body.head:
                raise HTTPException(400, "Choose both a base and a head branch")
            review = manager.open_branches(body.base, body.head, body.mode)
        return review.summary()

    @app.get("/api/reviews/{review_id}")
    def get_review(review_id: str):
        return manager.get(review_id).summary()

    @app.get("/api/reviews/{review_id}/status")
    def status(review_id: str):
        job = manager.get(review_id).job
        return {
            "state": job.state,
            "done": job.done,
            "total": job.total,
            "error": job.error,
            "symbols": len(job.index.symbols) if job.index else 0,
            "edges": len(job.index.edges) if job.index else 0,
            "seconds": round((job.finished or time.time()) - job.started, 1),
        }

    @app.get("/api/reviews/{review_id}/file")
    def file_detail(review_id: str, path: str, context: int = Query(4, ge=0, le=100000)):
        return manager.file_detail(manager.get(review_id), path, context)

    @app.get("/api/reviews/{review_id}/changes")
    def changes(review_id: str):
        review = manager.get(review_id)
        items = sorted(manager.changes(review).values(), key=lambda c: (c["path"], c["start"]))
        return {"changes": items}

    @app.get("/api/reviews/{review_id}/source")
    def source(review_id: str, path: str, start: int = 1, end: int = 200, side: Literal["head", "base"] = "head"):
        return manager.source(manager.get(review_id), path, start, end, side)

    @app.get("/api/reviews/{review_id}/search")
    def search(review_id: str, q: str):
        review = manager.get(review_id)
        query = manager.graph_query(review)
        return {"results": [query.node(sym.id) for sym in query.index.search(q)]}

    @app.post("/api/reviews/{review_id}/graph")
    def neighborhood(review_id: str, body: NeighborhoodRequest):
        query = manager.graph_query(
            manager.get(review_id),
            fuzzy=body.fuzzy,
            edge_kinds=body.edge_kinds,
            exclude_categories=body.exclude_categories,
        )
        return query.neighborhood(body.seeds, body.depth_in, body.depth_out, body.max_nodes)

    @app.post("/api/reviews/{review_id}/overview")
    def overview(review_id: str, body: OverviewRequest):
        query = manager.graph_query(
            manager.get(review_id),
            fuzzy=body.fuzzy,
            edge_kinds=body.edge_kinds,
            exclude_categories=body.exclude_categories,
        )
        paths = set(body.paths) if body.paths is not None else None
        symbols = set(body.symbols) if body.symbols is not None else None
        return query.overview(paths, body.neighbors, body.max_nodes, symbols=symbols)

    @app.get("/api/reviews/{review_id}/plan")
    def plan(review_id: str):
        return review_plan(manager, manager.get(review_id))

    @app.post("/api/reviews/{review_id}/explain")
    async def explain_symbol(review_id: str, body: ExplainRequest):
        review = manager.get(review_id)
        cache: dict[str, dict] = review.__dict__.setdefault("_explanations", {})
        cached = None if body.refresh else cache.get(body.symbol_id)
        prompt = None if cached else await asyncio.to_thread(explain.build_prompt, manager, review, body.symbol_id)

        async def events():
            def sse(event: dict) -> str:
                return f"data: {json.dumps(event)}\n\n"

            if cached:
                yield sse({"type": "meta", "model": cached["model"], "cached": True})
                yield sse({"type": "text", "text": cached["text"]})
                yield sse({"type": "done", **cached["done"], "cached": True})
                return
            text, model = [], None
            async for event in explain.run_claude(prompt, str(repo.root)):
                if event["type"] == "meta":
                    model = event["model"]
                elif event["type"] == "text":
                    text.append(event["text"])
                elif event["type"] == "done":
                    done = {k: v for k, v in event.items() if k != "type"}
                    cache[body.symbol_id] = {"text": "".join(text), "model": model, "done": done}
                yield sse(event)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if STATIC_DIR.exists():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            candidate = STATIC_DIR / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(STATIC_DIR / "index.html")

    return app
