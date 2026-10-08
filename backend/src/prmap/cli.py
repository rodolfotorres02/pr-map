"""``prmap`` command line entry point."""

from __future__ import annotations

import argparse
import socket
import threading
import webbrowser
from urllib.parse import urlencode

import uvicorn

from prmap.gitrepo import GitError, GitRepo
from prmap.server import STATIC_DIR, create_app


def _free_port(preferred: int) -> int:
    for port in (preferred, *range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="prmap",
        description="Mind-map a pull request: filter files by role and explore the call graph of changed code.",
    )
    parser.add_argument("--repo", default=".", help="path to a local git clone (default: current directory)")
    parser.add_argument("--pr", help="open this GitHub PR number or URL right away")
    parser.add_argument("--base", help="base branch/commit to compare against")
    parser.add_argument("--head", help="head branch/commit to review (default: current branch)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7420)
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = parser.parse_args(argv)

    try:
        repo = GitRepo(args.repo)
    except GitError as exc:
        parser.exit(2, f"prmap: {exc}\n")

    port = _free_port(args.port) if args.host in ("127.0.0.1", "localhost") else args.port
    query: dict[str, str] = {}
    if args.pr:
        query["pr"] = args.pr
    elif args.base or args.head:
        query["base"] = args.base or repo.default_branch() or "main"
        query["head"] = args.head or repo.current_branch() or "HEAD"
    url = f"http://{args.host}:{port}/" + (f"?{urlencode(query)}" if query else "")

    if not STATIC_DIR.exists():
        print("prmap: the web UI is not built yet - run `npm run build` in frontend/ (API still available).")
    print(f"prmap: reviewing {repo.root}")
    print(f"prmap: open {url}")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(create_app(repo.root), host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
