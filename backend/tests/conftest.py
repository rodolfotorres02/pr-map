from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from prmap.gitrepo import GitRepo
from prmap.indexer import ParseCache
from prmap.review import ReviewManager

BASE_FILES = {
    "shop/__init__.py": "",
    "shop/models/__init__.py": "from .order import Order, OrderItem\n",
    "shop/models/order.py": """
        class BaseModel:
            def save(self):
                return persist(self)

        def persist(obj):
            return obj

        class Order(BaseModel):
            def total(self):
                return sum(item.price for item in self.items)

            def mark_paid(self):
                self.paid = True
                self.save()

        class OrderItem(BaseModel):
            pass
        """,
    "shop/services.py": """
        from shop.models import Order
        from shop.notify import Mailer

        class Checkout:
            def __init__(self, mailer: Mailer):
                self.mailer = mailer

            def complete(self, order_id):
                order = Order.objects.get(pk=order_id)
                order.mark_paid()
                self.mailer.send_receipt(order)
                return order

        def legacy_discount(order):
            return 0
        """,
    "shop/notify.py": """
        class Mailer:
            def send_receipt(self, order):
                return format_receipt(order)

        def format_receipt(order):
            return str(order)
        """,
    "shop/views.py": """
        from . import services
        from .notify import Mailer

        def checkout_view(request, order_id):
            flow = services.Checkout(Mailer())
            return flow.complete(order_id)
        """,
    "shop/urls.py": """
        from shop import views
        urlpatterns = [path("checkout/<int:order_id>/", views.checkout_view)]
        """,
    "shop/templates/shop/checkout.html": "<h1>{{ order.total }}</h1>\n",
    "tests/test_services.py": """
        from shop.services import Checkout

        def test_complete():
            Checkout(None).complete(1)
        """,
    "web/tsconfig.json": """
        {
          // comments are allowed in tsconfig
          "compilerOptions": { "baseUrl": ".", "paths": { "@/*": ["src/*"] }, },
        }
        """,
    "web/src/api/client.ts": """
        export class ApiClient {
          get(url: string) { return fetch(url); }
          listOrders() { return this.get("/orders"); }
        }
        """,
    "web/src/api/index.ts": """
        export * from "./client";
        export { formatMoney as money } from "./format";
        """,
    "web/src/api/format.ts": """
        export function formatMoney(value: number): string {
          return value.toFixed(2);
        }
        """,
    "web/src/store.ts": """
        import { ApiClient } from "@/api";

        export class OrderStore {
          constructor(private api: ApiClient) {}
          load = async () => {
            const orders = await this.api.listOrders();
            this.emit(orders);
          };
          emit(orders) { return orders; }
        }
        """,
    "web/src/components/OrderRow.tsx": """
        import { money } from "../api";

        export function OrderRow({ total }: { total: number }) {
          return <td>{money(total)}</td>;
        }
        """,
    "web/src/components/OrderTable.tsx": """
        import { OrderRow } from "./OrderRow";
        import { OrderStore } from "@/store";
        import { ApiClient } from "@/api";

        export default function OrderTable() {
          const store = new OrderStore(new ApiClient());
          store.load();
          return <table><OrderRow total={1} /></table>;
        }
        """,
    "web/src/components/OrderTable.test.tsx": """
        import OrderTable from "./OrderTable";
        test("renders", () => { render(<OrderTable />); });
        """,
    "package-lock.json": "{}\n",
    "README.md": "# shop\n",
}

HEAD_CHANGES = {
    # modified method + new function, deleted function
    "shop/services.py": """
        from shop.models import Order
        from shop.notify import Mailer

        class Checkout:
            def __init__(self, mailer: Mailer):
                self.mailer = mailer

            def complete(self, order_id):
                order = Order.objects.get(pk=order_id)
                order.mark_paid()
                self.mailer.send_receipt(order)
                audit(order)
                return order

        def audit(order):
            return order.total()
        """,
    "shop/notify.py": """
        class Mailer:
            def send_receipt(self, order):
                return format_receipt(order, currency="EUR")

        def format_receipt(order, currency="USD"):
            return f"{order} {currency}"
        """,
    "shop/templates/shop/checkout.html": "<h1>Total: {{ order.total }}</h1>\n",
    "tests/test_services.py": """
        from shop.services import Checkout, audit

        def test_complete():
            Checkout(None).complete(1)

        def test_audit():
            audit(None)
        """,
    "web/src/components/OrderRow.tsx": """
        import { money } from "../api";

        export function OrderRow({ total }: { total: number }) {
          return <td className="money">{money(total)}</td>;
        }
        """,
    "package-lock.json": '{"lockfileVersion": 3}\n',
}


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"))


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(root), "PATH": "/usr/bin:/bin:/opt/homebrew/bin"},
    ).stdout


@pytest.fixture(scope="session")
def fixture_repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("repo")
    _git(root, "init", "-q", "-b", "main")
    _write(root, BASE_FILES)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    _write(root, HEAD_CHANGES)
    (root / "README.md").unlink()
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "feature work")
    return root


@pytest.fixture(scope="session")
def manager(fixture_repo, tmp_path_factory) -> ReviewManager:
    cache = ParseCache(tmp_path_factory.mktemp("cache") / "cache.sqlite")
    return ReviewManager(GitRepo(fixture_repo), cache=cache)


@pytest.fixture(scope="session")
def review(manager):
    review = manager.open_branches("main", "feature")
    import time

    for _ in range(200):
        if review.job.state in ("ready", "error"):
            break
        time.sleep(0.05)
    assert review.job.state == "ready", review.job.error
    return review
