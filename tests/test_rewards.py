# SPDX-License-Identifier: Apache-2.0
"""v0.5 `rewards`, offline: read from the object, derived from a Token Profile, or not stated.

The NOBULL fixture is the object the Bounty app for Shopify composed from NOBULL's live
Okendo Loyalty program on 2026-10-15. NOBULL hasn't installed anything, so its real
storefront publishes nothing (the live test reports it as unknown); this test serves
that composed object from a mock store to check what an agent would get from it.
"""

import json
from pathlib import Path

import httpx
import pytest

from bounty_skill import discovery

FIX = Path(__file__).parent / "fixtures"
SCHEMA = json.loads((FIX / "bounty-v1.json").read_text())
NOBULL = json.loads((FIX / "nobull-as-composed.json").read_text())

TOKEN_STORE = {
    "bounty_schema_version": "0.5",
    "active": True,
    "program": {"name": "Example Token Program", "url": "https://tokens.example"},
    "earn": {"display": "Members earn QRY on every purchase.", "type": "token", "denomination": "QRY"},
    "tokens": ["https://tokens.example/v1/tokens/qry"],
    "listed_by": "store",
    "updated_at": "2026-10-15",
}
PROFILE = {
    "token_schema_version": "0.3",
    "symbol": "QRY",
    "canonical_url": "https://tokens.example/v1/tokens/qry",
    "issuer": {"name": "Example Token Program", "url": "https://tokens.example"},
    "status_ladder": {
        "basis": "held",
        "rungs": [{"rung": "Apprentice", "threshold": 250}, {"rung": "Journeyman", "threshold": 900}, {"rung": "Master", "threshold": 4200}],
    },
    "unlocks": [
        {"name": "Partner night", "state": "planned", "sponsor": "Example Venue"},
        {"name": "Members dinner", "state": "committed"},
        {"name": "Early access", "state": "funded"},
        {"name": "Mystery", "state": "maybe"},
    ],
    "unlocks_summary": {"planned": 1, "committed": 1, "funded": 1},
    "updated_at": "2026-10-15",
}
PLAIN = {k: v for k, v in TOKEN_STORE.items() if k != "tokens"}


def client(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == discovery.SCHEMA_URL:
            return httpx.Response(200, json=SCHEMA)
        status, body = routes.get(url, (404, "not found"))
        return httpx.Response(status, json=body) if isinstance(body, dict) else httpx.Response(status, text=str(body))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


@pytest.fixture(autouse=True)
def fresh_schema_cache():
    discovery._schema_cache.update(at=0.0, schema=None)


async def resolve(store, routes):
    async with client(routes) as c:
        return await discovery.resolve(store, client=c)


async def test_nobull_block_read_as_published():
    r = await resolve("nobull.example", {"https://nobull.example/.well-known/bounty.json": (200, NOBULL)})
    assert r["status"] == "declared"
    c = r["comparable"]
    assert c["source"] == "rewards"
    assert c["unit_value"] == "100 Points = $5 off at checkout, on eligible products."
    assert c["entry_tier_rule"] == {"earn_display": "Members earn 1 Point per dollar, redeemable at checkout.", "entry_tier": "Foundation"}
    assert c["funded_perks"] == 3
    assert c["ladder_depth"] == 3 and c["ladder_basis"] == "spend" and c["ladder_unit"] == "USD"


async def test_token_program_without_rewards_is_derived_from_its_profile():
    r = await resolve("tokens.example", {
        "https://tokens.example/.well-known/bounty.json": (200, TOKEN_STORE),
        "https://tokens.example/v1/tokens/qry": (200, PROFILE),
    })
    c = r["comparable"]
    assert c["source"] == "token_profile"
    assert c["unit_value"] is None  # no Token Profile field maps to `value`
    assert c["funded_perks"] == 1 and c["funded_perk_names"] == ["Early access"]
    assert c["not_yet_available"] == {"committed": 1, "planned": 1}
    assert c["ladder_depth"] == 3 and c["ladder_unit"] == "QRY"
    # The first rung starts at 250, so a new member holds no rung yet: no entry tier is named.
    assert c["entry_tier_rule"]["entry_tier"] is None
    planned = next(p for p in r["rewards"]["perks"] if p["state"] == "planned")
    assert "sponsor" not in planned
    assert all(p["state"] in discovery.STATES for p in r["rewards"]["perks"])


async def test_a_declared_object_without_rewards_states_none_of_it():
    r = await resolve("plain.example", {"https://plain.example/.well-known/bounty.json": (200, PLAIN)})
    c = r["comparable"]
    assert c["source"] == "not_stated"
    assert c["unit_value"] is None and c["funded_perks"] is None and c["ladder_depth"] is None
    assert c["entry_tier_rule"]["earn_display"] == PLAIN["earn"]["display"]


async def test_unknown_store_has_no_summary():
    r = await resolve("nothing.example", {})
    assert r["status"] == "unknown" and "comparable" not in r


async def test_an_invalid_rewards_block_makes_the_object_invalid():
    bad = {**NOBULL, "rewards": {**NOBULL["rewards"], "perks": [{"name": "Free shipping", "state": "live"}]}}
    r = await resolve("bad.example", {"https://bad.example/.well-known/bounty.json": (200, bad)})
    assert r["status"] == "unknown" and "invalid" in r["reason"]
