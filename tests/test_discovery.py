# SPDX-License-Identifier: Apache-2.0
"""Spec §4 discovery, offline: every path through httpx.MockTransport."""

import json
from pathlib import Path

import httpx
import pytest

from bounty_skill import discovery

SCHEMA = json.loads((Path(__file__).parent / "fixtures" / "bounty-v1.json").read_text())

OBJ = {
    "bounty_schema_version": "0.4",
    "active": True,
    "program": {"name": "Example Rewards", "url": "https://rewards.example"},
    "earn": {"display": "Members earn points on every order.", "type": "points", "denomination": "points"},
    "api": "https://rewards.example/api/bounty",
    "tokens": ["https://rewards.example/tokens/pts"],
    "listed_by": "store",
    "updated_at": "2026-10-14",
}
# The same object without the fields that trigger follow-up fetches.
BARE = {k: v for k, v in OBJ.items() if k not in ("api", "tokens")}


def client(routes: dict[str, tuple[int, object]]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == discovery.SCHEMA_URL:
            return httpx.Response(200, json=SCHEMA)
        status, body = routes.get(url, (404, "not found"))
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=str(body))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


@pytest.fixture(autouse=True)
def fresh_schema_cache():
    discovery._schema_cache.update(at=0.0, schema=None)


async def test_step_1_well_known():
    c = client({
        "https://shop.example/.well-known/bounty.json": (200, OBJ),
        "https://rewards.example/api/bounty": (200, {"ok": True}),
        "https://rewards.example/tokens/pts": (200, {"symbol": "PTS", "unlocks_summary": {"planned": 1, "committed": 0, "funded": 2}}),
    })
    r = await discovery.resolve("https://Shop.Example/some/path", client=c)
    assert r["status"] == "declared" and r["found_at"] == "well-known"
    assert r["earn_display"] == OBJ["earn"]["display"], "verbatim"
    assert r["listed_by"] == "store" and r["program"]["name"] == "Example Rewards"
    assert r["api"] == {"url": OBJ["api"], "status": 200, "reachable": True}
    assert r["tokens"][0]["symbol"] == "PTS" and r["tokens"][0]["unlocks_summary"]["funded"] == 2


async def test_step_2_link_relative_href_and_rel_token_list():
    c = client({
        "https://shop.example/": (200, '<html><head><link rel="alternate bounty" type="application/json" href="/apps/bounty/bounty.json"></head><body></body></html>'),
        "https://shop.example/apps/bounty/bounty.json": (200, BARE),
    })
    r = await discovery.resolve("shop.example", client=c)
    assert r["status"] == "declared" and r["found_at"] == "link"
    assert r["object_url"] == "https://shop.example/apps/bounty/bounty.json"


async def test_a_link_in_body_is_not_discovery():
    c = client({
        "https://shop.example/": (200, '<html><head></head><body><link rel="bounty" href="/x.json"></body></html>'),
        "https://shop.example/x.json": (200, OBJ),
    })
    r = await discovery.resolve("shop.example", client=c)
    assert r["status"] == "unknown"


async def test_nothing_published_is_unknown_never_no():
    r = await discovery.resolve("shop.example", client=client({"https://shop.example/": (200, "<html><head></head></html>")}))
    assert r["status"] == "unknown"
    assert "not a statement that buying here pays nothing" in r["note"]
    assert r["steps"][-1] == "3 unknown"


async def test_html_200_at_the_well_known_path_continues_to_step_2():
    c = client({
        "https://shop.example/.well-known/bounty.json": (200, "<html>soft 404</html>"),
        "https://shop.example/": (200, '<head><link rel="bounty" href="https://cdn.example/b.json"></head>'),
        "https://cdn.example/b.json": (200, BARE),
    })
    r = await discovery.resolve("shop.example", client=c)
    assert r["status"] == "declared" and r["found_at"] == "link"


async def test_an_invalid_object_is_unknown_with_the_reason():
    bad = {**OBJ, "rate": 0.05}
    r = await discovery.resolve("shop.example", client=client({"https://shop.example/.well-known/bounty.json": (200, bad)}))
    assert r["status"] == "unknown" and "rate" in r["reason"]


async def test_a_one_x_object_is_not_accepted():
    r = await discovery.resolve("shop.example", client=client({"https://shop.example/.well-known/bounty.json": (200, {**OBJ, "bounty_schema_version": "1.0"})}))
    assert r["status"] == "unknown"


async def test_inactive_is_the_stores_own_statement():
    r = await discovery.resolve("shop.example", client=client({"https://shop.example/.well-known/bounty.json": (200, {**BARE, "active": False})}))
    assert r["status"] == "inactive"


async def test_unreachable_api_and_token_are_reported_not_hidden():
    r = await discovery.resolve("shop.example", client=client({"https://shop.example/.well-known/bounty.json": (200, OBJ)}))
    assert r["api"]["reachable"] is False
    assert r["tokens"][0]["reachable"] is False


async def test_scope_selected_needs_and_uses_the_product_check():
    selected = {**BARE, "scope": "selected"}
    page = ('<script type="application/ld+json">{"@type":"Product","offers":{"@type":"Offer",'
            '"additionalProperty":{"@type":"PropertyValue","propertyID":"bounty","value":"Earn double on this one."}}}</script>')
    routes = {
        "https://shop.example/.well-known/bounty.json": (200, selected),
        "https://shop.example/products/a": (200, page),
        "https://shop.example/products/b": (200, "<html>no declaration</html>"),
    }
    r = await discovery.resolve("shop.example", client=client(routes))
    assert r["product_check_needed"] is True
    a = await discovery.resolve("shop.example", product_url="https://shop.example/products/a", client=client(routes))
    assert a["product"]["declared"] and a["product"]["earn_display"] == "Earn double on this one."
    b = await discovery.resolve("shop.example", product_url="https://shop.example/products/b", client=client(routes))
    assert b["product"]["declared"] is False and "not covered" in b["product"]["meaning"]


async def test_a_product_url_on_another_store_is_not_checked():
    routes = {"https://shop.example/.well-known/bounty.json": (200, BARE),
              "https://elsewhere.example/p": (200, "<html></html>")}
    r = await discovery.resolve("shop.example", product_url="https://elsewhere.example/p", client=client(routes))
    assert r["product"]["checked"] is False


async def test_plain_http_is_never_fetched():
    routes = {"https://shop.example/": (200, '<head><link rel="bounty" href="http://insecure.example/b.json"></head>')}
    r = await discovery.resolve("shop.example", client=client(routes))
    assert r["status"] == "unknown"


def test_domain_normalisation():
    assert discovery.normalize_domain("https://Shop.Example/a?b") == "shop.example"
    assert discovery.normalize_domain("merch.nctr.live") == "merch.nctr.live"
    assert discovery.normalize_domain("not a domain") is None
    assert discovery.normalize_domain("localhost") is None
