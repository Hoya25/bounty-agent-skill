# SPDX-License-Identifier: Apache-2.0
"""v0.6, offline: signed objects (signed / unsigned / invalid as a hard stop), localized
display, the validity window, pass-through fields, and local goal matching."""

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from bounty_skill import discovery

FIX = Path(__file__).parent / "fixtures"
SCHEMA = json.loads((FIX / "bounty-v1.json").read_text())

KEY = Ed25519PrivateKey.generate()
X = base64.urlsafe_b64encode(KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).rstrip(b"=").decode()
JWKS = {"keys": [{"kty": "OKP", "crv": "Ed25519", "x": X, "kid": "k1", "use": "sig", "alg": "EdDSA"}]}

OBJ = {
    "bounty_schema_version": "0.6",
    "active": True,
    "program": {"name": "Example Maison Club", "url": "https://maison.example/club"},
    "earn": {"display": {"default": "Members earn 5 points per €1, redeemable at checkout.", "fr": "Les membres gagnent 5 points par 1 €."}},
    "currency": "EUR",
    "eligibility": {"who": "members", "join_url": "https://maison.example/club/join"},
    "conditions": [{"type": "pending_period", "value": 14, "note": "Points are pending for 14 days after purchase."}],
    "rewards": {
        "value": "100 points = €5 off at checkout.",
        "perks": [
            {"name": "Gear library", "state": "funded", "fulfills": ["outdoor_gear"]},
            {"name": "Youth league fees", "state": "planned", "fulfills": ["youth_sports"]},
            {"name": "Concert nights", "state": "committed", "fulfills": ["concert_tickets", "live_events"]},
        ],
    },
    "updated_at": "2026-10-15",
}


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def sign(body: bytes, key=KEY, kid="k1", alg="EdDSA") -> str:
    h = b64(json.dumps({"alg": alg, "kid": kid, "typ": "bounty+jws"}).encode())
    return f"{h}..{b64(key.sign(f'{h}.{b64(body)}'.encode()))}"


def client(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == discovery.SCHEMA_URL:
            return httpx.Response(200, json=SCHEMA)
        status, body, headers = routes.get(url, (404, b"not found", {}))
        return httpx.Response(status, content=body, headers=headers)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


@pytest.fixture(autouse=True)
def fresh_caches():
    discovery._schema_cache.update(at=0.0, schema=None)
    discovery._keys_cache.clear()


async def run(routes, **kw):
    async with client(routes) as c:
        return await discovery.resolve("maison.example", client=c, **kw)


WK = "https://maison.example/.well-known/bounty.json"
KEYS = "https://maison.example/.well-known/bounty-keys.json"


async def test_signed_object_is_reported_signed_with_its_origin():
    body = json.dumps(OBJ).encode()
    r = await run({WK: (200, body, {"Bounty-Signature": sign(body)}), KEYS: (200, json.dumps(JWKS).encode(), {})})
    assert r["status"] == "declared"
    assert r["signature"] == "signed" and r["signed_by"] == "https://maison.example"
    assert r["comparable"]["signature"] == "signed"


async def test_no_header_is_unsigned_and_used_as_before():
    r = await run({WK: (200, json.dumps(OBJ).encode(), {})})
    assert r["status"] == "declared" and r["signature"] == "unsigned" and "signed_by" not in r


@pytest.mark.parametrize("why, routes_for", [
    ("tampered body", lambda body: {WK: (200, body.replace(b"5 points", b"9 points"), {"Bounty-Signature": sign(body)}), KEYS: (200, json.dumps(JWKS).encode(), {})}),
    ("another key", lambda body: {WK: (200, body, {"Bounty-Signature": sign(body, key=Ed25519PrivateKey.generate())}), KEYS: (200, json.dumps(JWKS).encode(), {})}),
    ("unknown kid", lambda body: {WK: (200, body, {"Bounty-Signature": sign(body, kid="other")}), KEYS: (200, json.dumps(JWKS).encode(), {})}),
    ("another alg", lambda body: {WK: (200, body, {"Bounty-Signature": sign(body, alg="HS256")}), KEYS: (200, json.dumps(JWKS).encode(), {})}),
    ("keys unreachable", lambda body: {WK: (200, body, {"Bounty-Signature": sign(body)})}),
    ("garbage header", lambda body: {WK: (200, body, {"Bounty-Signature": "not-a-jws"}), KEYS: (200, json.dumps(JWKS).encode(), {})}),
])
async def test_invalid_signature_is_a_hard_stop(why, routes_for):
    r = await run(routes_for(json.dumps(OBJ).encode()))
    assert r["status"] == "unknown", why
    assert r["signature"] == "invalid", why
    # Nothing from the object leaks into the answer.
    for k in ("earn_display", "program", "rewards", "comparable", "eligibility"):
        assert k not in r, (why, k)


async def test_localized_display_picks_locale_then_language_then_default():
    body = json.dumps(OBJ).encode()
    routes = {WK: (200, body, {})}
    assert (await run(routes, locale="fr-CA"))["earn_display"] == "Les membres gagnent 5 points par 1 €."
    discovery._schema_cache.update(at=0.0, schema=None)
    r = await run(routes, locale="de")
    assert r["earn_display"] == OBJ["earn"]["display"]["default"] and r["earn_display_locale"] == "default"


async def test_outside_the_validity_window_is_inactive():
    ended = {**OBJ, "valid_until": "2026-01-31"}
    r = await run({WK: (200, json.dumps(ended).encode(), {})})
    assert r["status"] == "inactive" and "ended" in r["inactive_reason"]
    assert discovery.outside_window({"valid_from": "2999-01-01"}) and not discovery.outside_window({})
    # A date-only valid_until runs to the end of that day.
    assert discovery.outside_window({"valid_until": "2026-10-15"}, datetime(2026, 10, 15, 22, 0, tzinfo=timezone.utc)) is None


async def test_completeness_fields_pass_through_and_summarise():
    r = await run({WK: (200, json.dumps(OBJ).encode(), {})})
    assert r["currency"] == "EUR" and r["eligibility"]["who"] == "members"
    c = r["comparable"]
    assert c["eligibility_who"] == "members"
    assert c["condition_notes"] == ["Points are pending for 14 days after purchase."]


async def test_goals_match_locally_and_only_funded_is_available():
    r = await run({WK: (200, json.dumps(OBJ).encode(), {})}, goals=["outdoor_gear", "concert_tickets", "Youth_Sports", "travel"])
    g = r["goal_matches"]
    assert g["available"] == [{"name": "Gear library", "matches": ["outdoor_gear"]}]
    assert {(x["name"], x["state"]) for x in g["not_yet_available"]} == {("Concert nights", "committed"), ("Youth league fees", "planned")}
