"""A recording mock of Kalshi's perps (margin) REST API for the margin client's tests. NO network: an
httpx.MockTransport that records every request that reaches it and answers like the spec (`perps_openapi.yaml`).

* `queue(method, path, status, body)` scripts one-shot answers, used in order before the default behaviour.
* Default behaviour: POST /margin/orders keeps an order book of our own orders keyed by client_order_id (a new id is
  created and filled in full at its price, 201; a repeated id answers 409, as the spec's ConflictError), and GET
  /margin/orders lists them filtered by ticker and subaccount. Anything else answers 404.
* `lose_first_post_reply`: the first POST is processed (the order IS placed) and its reply is lost (ReadTimeout).
* `hide_from_lists`: the order lists never show an order (the lists lagging a placement).
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from urllib.parse import parse_qsl

import httpx

P = "/trade-api/v2"
DEMO_HOST = "external-api.demo.kalshi.co"
PROD_HOST = "external-api.kalshi.com"


class MarginFake:
    def __init__(self, *, lose_first_post_reply: bool = False, hide_from_lists: bool = False) -> None:
        self.seen: list[dict] = []
        self.orders: dict[str, dict] = {}  # client_order_id -> MarginOrder
        self._script: dict[tuple[str, str], deque] = defaultdict(deque)
        self._lose = lose_first_post_reply
        self._hide = hide_from_lists

    def queue(self, method: str, path: str, status: int, body: dict | None = None,
              headers: dict[str, str] | None = None) -> None:
        """One scripted answer for (method, path under /trade-api/v2)."""
        self._script[(method, P + path)].append((status, body, headers or {}))

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append({"method": request.method, "host": request.url.host, "path": request.url.path,
                          "query": dict(parse_qsl(request.url.query.decode())),
                          "body": json.loads(request.content) if request.content else None,
                          "headers": dict(request.headers)})
        key = (request.method, request.url.path)
        if self._script[key]:
            status, body, headers = self._script[key].popleft()
            return httpx.Response(status, json=body if body is not None else {}, headers=headers)
        if key == ("POST", f"{P}/margin/orders"):
            resp = self._create(json.loads(request.content))
            if self._lose:
                self._lose = False
                raise httpx.ReadTimeout("response lost", request=request)  # the order WAS placed
            return resp
        if key == ("GET", f"{P}/margin/orders"):
            q = dict(parse_qsl(request.url.query.decode()))
            rows = [] if self._hide else [
                o for o in self.orders.values()
                if ("ticker" not in q or o["ticker"] == q["ticker"])
                and ("subaccount" not in q or str(o["_subaccount"]) == q["subaccount"])]
            return httpx.Response(200, json={"orders": [{k: v for k, v in o.items() if not k.startswith("_")}
                                                        for o in rows], "cursor": ""})
        return httpx.Response(404, json={"code": "not_found", "message": "not found"})

    def _create(self, body: dict) -> httpx.Response:
        cid = body["client_order_id"]
        if cid in self.orders:
            return httpx.Response(409, json={"code": "order_already_exists", "message": "duplicate client_order_id"})
        oid = f"ord-{len(self.orders) + 1}"
        self.orders[cid] = {"order_id": oid, "user_id": "u1", "client_order_id": cid, "ticker": body["ticker"],
                            "side": body["side"], "price": body["price"], "fill_count": body["count"],
                            "remaining_count": "0.00", "last_update_reason": "Trade",
                            "created_time": "2026-10-30T12:00:01.250Z", "last_update_time": "2026-10-30T12:00:01.250Z",
                            "self_trade_prevention_type": body["self_trade_prevention_type"],
                            "_subaccount": body.get("subaccount", 0)}
        return httpx.Response(201, json={"order_id": oid, "client_order_id": cid, "fill_count": body["count"],
                                         "remaining_count": "0.00", "average_fill_price": body["price"],
                                         "average_fee_paid": "0.0094"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def requests(self, method: str | None = None, path: str | None = None) -> list[dict]:
        return [r for r in self.seen if (method is None or r["method"] == method)
                and (path is None or r["path"] == P + path)]
