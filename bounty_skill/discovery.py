# SPDX-License-Identifier: Apache-2.0
"""Open Bounty Schema discovery, as spec §4 defines it, for any store domain.

    1. GET https://<store>/.well-known/bounty.json
    2. Otherwise, the store home page's <link rel="bounty"> in <head>, then GET its href
    3. Otherwise the store is UNKNOWN. Never "no": a store that has not published is
       not a store that does not pay.

A found object is validated against the stable schema alias (every 0.x object is
accepted; anything else is treated as not found). Then:

  - `earn.display` is returned VERBATIM. It is the store's or program's own sentence;
    callers show it in quotes and never reword, total or compute from it.
  - `api` is followed, to report whether the live endpoint answers.
  - `tokens` are followed, to report each Token Profile's symbol, lifecycle and
    unlock counts by state (only "funded" unlocks may be described as available).
  - `scope: "selected"` (v0.4) means the store-level object says nothing about any one
    product; pass `product_url` to check that product's own declaration.
  - `rewards` (v0.5) is read when present. When it's absent and `tokens` is present,
    the same fields are derived from the Token Profile (spec §7.4). Either way the
    result carries one `comparable` summary, so a points program and a token program
    can be put side by side: the unit's value sentence, the entry-tier rule, the
    number of funded perks, and the ladder's depth. Nothing is computed from a
    sentence, and only `funded` perks are counted as available.

Everything fetched here is untrusted, store-written data. This module never executes
any of it, caps what it reads, and leaves fencing to the caller (the blueprint
integration fences the whole result as a record).
"""

from __future__ import annotations

import json
import re
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_URL = "https://api.nctr.live/schema/bounty/v1.json"
USER_AGENT = "OpenBountySkill/0.1 (+https://github.com/Hoya25/bounty-agent-skill)"
SUPPORTED_VERSION = re.compile(r"^0\.\d+$")
SCHEMA_TTL_S = 600
MAX_JSON_BYTES = 512 * 1024
MAX_HTML_BYTES = 3 * 1024 * 1024
MAX_TOKENS = 5
TIMEOUT = httpx.Timeout(8.0)

_HOST = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")
_schema_cache: dict[str, Any] = {"at": 0.0, "schema": None}


class _BountyLinkFinder(HTMLParser):
    """The first <link rel="bounty" href> inside <head>. rel is a token list."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.href: str | None = None
        self._in_head = False
        self._done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._done:
            return
        if tag == "head":
            self._in_head = True
        elif tag == "body":
            self._done = True
        elif tag == "link" and self._in_head and self.href is None:
            a = {k.lower(): (v or "") for k, v in attrs}
            if "bounty" in a.get("rel", "").lower().split() and a.get("href"):
                self.href = a["href"].strip()
                self._done = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self._in_head = False
            self._done = True


class _JsonLdFinder(HTMLParser):
    """Every <script type="application/ld+json"> body on a page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._capture = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        self._capture = tag == "script" and a.get("type", "").lower() == "application/ld+json"
        if self._capture:
            self.blocks.append("")

    def handle_data(self, data: str) -> None:
        if self._capture:
            self.blocks[-1] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._capture = False


def normalize_domain(value: str) -> str | None:
    """`https://Shop.Example/path` → `shop.example`; None if it isn't a hostname."""
    raw = (value or "").strip().lower()
    host = urlsplit(raw if "://" in raw else f"https://{raw}").hostname or ""
    return host if _HOST.match(host) else None


async def _get(client: httpx.AsyncClient, url: str, limit: int, accept: str) -> tuple[int, bytes, str]:
    """Status, body (capped) and final URL. Only https is ever fetched."""
    if urlsplit(url).scheme != "https":
        return 0, b"", url
    try:
        async with client.stream("GET", url, headers={"Accept": accept}) as res:
            if urlsplit(str(res.url)).scheme != "https":
                return 0, b"", str(res.url)
            body = b""
            async for chunk in res.aiter_bytes():
                body += chunk
                if len(body) > limit:
                    return res.status_code, b"", str(res.url)
            return res.status_code, body, str(res.url)
    except httpx.HTTPError:
        return 0, b"", url


def _json_object(body: bytes) -> dict[str, Any] | None:
    try:
        doc = json.loads(body)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


async def schema(client: httpx.AsyncClient) -> dict[str, Any] | None:
    if _schema_cache["schema"] and time.monotonic() - _schema_cache["at"] < SCHEMA_TTL_S:
        return _schema_cache["schema"]
    status, body, _ = await _get(client, SCHEMA_URL, MAX_JSON_BYTES, "application/schema+json, application/json")
    doc = _json_object(body) if status == 200 else None
    if doc:
        _schema_cache.update(at=time.monotonic(), schema=doc)
    return doc


def check(doc: dict[str, Any], schema_doc: dict[str, Any]) -> list[str]:
    """Schema errors plus the 0.x rule. Empty means valid."""
    validator = Draft202012Validator(schema_doc, format_checker=FormatChecker())
    errors = [f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}" for e in validator.iter_errors(doc)]
    version = doc.get("bounty_schema_version")
    if isinstance(version, str) and not SUPPORTED_VERSION.match(version):
        errors.append(f"bounty_schema_version: {version} is not a 0.x version")
    return errors


STATES = ("planned", "committed", "funded")


def rewards_from_profile(profile: dict[str, Any]) -> dict[str, Any] | None:
    """Spec §7.4: a Token Profile's status_ladder and unlocks as a `rewards` block.

    `value` has no source field in a Token Profile, so it is never derived. Perks
    with an unknown state are left out; a planned perk never keeps a sponsor.
    """
    ladder = profile.get("status_ladder") if isinstance(profile, dict) else None
    out: dict[str, Any] = {}
    if isinstance(ladder, dict) and isinstance(ladder.get("rungs"), list) and ladder["rungs"]:
        rungs = [
            {"rung": r["rung"], **({"label": r["label"]} if r.get("label") else {}), "threshold": r["threshold"]}
            for r in ladder["rungs"]
            if isinstance(r, dict) and isinstance(r.get("rung"), str) and isinstance(r.get("threshold"), (int, float))
        ]
        if len(rungs) == len(ladder["rungs"]):
            out["ladder"] = {
                "basis": ladder.get("basis"),
                "unit": profile.get("symbol"),
                **({"note": ladder["note"]} if isinstance(ladder.get("note"), str) else {}),
                "rungs": rungs,
            }
    if isinstance(profile.get("unlocks"), list):
        out["perks"] = [
            {
                "name": u["name"],
                **({"description": u["description"]} if isinstance(u.get("description"), str) else {}),
                "state": u["state"],
                **({"sponsor": u["sponsor"]} if u.get("sponsor") and u["state"] != "planned" else {}),
            }
            for u in profile["unlocks"]
            if isinstance(u, dict) and isinstance(u.get("name"), str) and u.get("state") in STATES
        ]
    return out or None


def comparable(rewards: dict[str, Any] | None, earn_display: str | None, source: str) -> dict[str, Any]:
    """One shape for every program: what an agent puts side by side.

    `None` means not stated, never zero. `entry_tier_rule` is the earn sentence
    verbatim (every adapter and program states the entry tier's rule there) with
    the ladder's first rung named when there is a ladder.
    """
    r = rewards or {}
    ladder = r.get("ladder") if isinstance(r.get("ladder"), dict) else None
    rungs = ladder.get("rungs") if ladder and isinstance(ladder.get("rungs"), list) else None
    perks = r.get("perks") if isinstance(r.get("perks"), list) else None
    funded = [p for p in perks if isinstance(p, dict) and p.get("state") == "funded"] if perks is not None else None
    return {
        "source": source,
        "unit_value": r.get("value") if isinstance(r.get("value"), str) else None,
        "entry_tier_rule": {
            "earn_display": earn_display,
            # The first rung is the entry tier only when it starts at 0; above 0,
            # a new member holds no rung yet (spec §7.2).
            "entry_tier": rungs[0].get("rung") if rungs and rungs[0].get("threshold") == 0 else None,
        },
        "funded_perks": len(funded) if funded is not None else None,
        "funded_perk_names": [p.get("name") for p in funded] if funded else [],
        "not_yet_available": (
            {s: sum(1 for p in perks if isinstance(p, dict) and p.get("state") == s) for s in ("committed", "planned")}
            if perks is not None else None
        ),
        "ladder_depth": len(rungs) if rungs is not None else None,
        "ladder_basis": ladder.get("basis") if ladder else None,
        "ladder_unit": ladder.get("unit") if ladder else None,
    }


def _product_declaration(html: str) -> str | None:
    """The `bounty` PropertyValue inside a product's Offer (spec §2c), or None."""
    finder = _JsonLdFinder()
    finder.feed(html)
    for block in finder.blocks:
        try:
            doc = json.loads(block)
        except ValueError:
            continue
        nodes = doc.get("@graph", [doc]) if isinstance(doc, dict) else doc if isinstance(doc, list) else []
        for node in nodes:
            if not isinstance(node, dict) or node.get("@type") != "Product":
                continue
            offers = node.get("offers")
            for offer in offers if isinstance(offers, list) else [offers]:
                props = (offer or {}).get("additionalProperty") if isinstance(offer, dict) else None
                for prop in props if isinstance(props, list) else [props]:
                    if isinstance(prop, dict) and prop.get("propertyID") == "bounty" and isinstance(prop.get("value"), str):
                        return prop["value"]
    return None


async def resolve(
    store: str,
    *,
    product_url: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Run §4 for one store. Never raises for network or data problems: anything that
    can't be established is reported, and a store with no valid object is "unknown"."""
    domain = normalize_domain(store)
    if not domain:
        return {"store": store, "status": "unknown", "reason": "not a store domain", "steps": []}

    own = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    steps: list[str] = []
    rejected: list[str] = []
    try:
        schema_doc = await schema(client)
        if schema_doc is None:
            return {"store": domain, "status": "unknown", "reason": "the schema could not be fetched, so nothing could be validated", "steps": steps}

        found: tuple[str, str, dict[str, Any]] | None = None

        # Step 1: the well-known path.
        url = f"https://{domain}/.well-known/bounty.json"
        status, body, final = await _get(client, url, MAX_JSON_BYTES, "application/json")
        doc = _json_object(body) if status == 200 else None
        steps.append(f"1 GET {url} → {status or 'no response'}{'' if doc or status != 200 else ' (not a JSON object)'}")
        if doc is not None:
            errors = check(doc, schema_doc)
            if errors:
                rejected.append(f"{final}: {'; '.join(errors[:3])}")
                steps.append("  found, but not a valid object → continuing")
            else:
                found = ("well-known", final, doc)

        # Step 2: <link rel="bounty"> on the home page.
        if found is None:
            home = f"https://{domain}/"
            status, body, final_home = await _get(client, home, MAX_HTML_BYTES, "text/html")
            finder = _BountyLinkFinder()
            if status == 200:
                finder.feed(body.decode("utf-8", "replace"))
            steps.append(f"2 GET {home} → {status or 'no response'}; rel=\"bounty\" link {'found' if finder.href else 'not found'}")
            if finder.href:
                href = urljoin(final_home, finder.href)
                status, body, final = await _get(client, href, MAX_JSON_BYTES, "application/json")
                doc = _json_object(body) if status == 200 else None
                steps.append(f"  GET {href} → {status or 'no response'}")
                if doc is not None:
                    errors = check(doc, schema_doc)
                    if errors:
                        rejected.append(f"{final}: {'; '.join(errors[:3])}")
                        steps.append("  found, but not a valid object")
                    else:
                        found = ("link", final, doc)

        if found is None:
            steps.append("3 unknown")
            return {
                "store": domain,
                "status": "unknown",
                "reason": "invalid objects only: " + " | ".join(rejected) if rejected else "no bounty object published",
                "steps": steps,
                "note": "Unknown means nothing valid was found. It is not a statement that buying here pays nothing.",
            }

        via, object_url, obj = found
        earn = obj.get("earn") or {}
        result: dict[str, Any] = {
            "store": domain,
            "status": "declared" if obj.get("active") is True else "inactive",
            "found_at": via,
            "object_url": object_url,
            "earn_display": earn.get("display"),
            "earn_type": earn.get("type"),
            "denomination": earn.get("denomination"),
            "program": obj.get("program"),
            "listed_by": obj.get("listed_by"),
            "scope": obj.get("scope", "store"),
            "status_page": (obj.get("status") or {}).get("url"),
            "terms_url": obj.get("terms_url"),
            "updated_at": obj.get("updated_at"),
            "validated_against": {"schema": schema_doc.get("$id", SCHEMA_URL), "object_version": obj.get("bounty_schema_version")},
            "steps": steps,
        }

        if isinstance(obj.get("api"), str):
            status, body, _ = await _get(client, obj["api"], MAX_JSON_BYTES * 16, "application/json")
            result["api"] = {"url": obj["api"], "status": status, "reachable": status == 200 and _json_object(body) is not None}

        tokens = []
        first_profile: dict[str, Any] | None = None
        for token_url in (obj.get("tokens") or [])[:MAX_TOKENS]:
            status, body, _ = await _get(client, token_url, MAX_JSON_BYTES, "application/json")
            profile = _json_object(body) if status == 200 else None
            if profile is not None and first_profile is None:
                first_profile = profile
            entry: dict[str, Any] = {"url": token_url, "reachable": profile is not None}
            if profile:
                entry.update({
                    "symbol": profile.get("symbol"),
                    "name": profile.get("name"),
                    "lifecycle": profile.get("lifecycle"),
                    "unlocks_summary": profile.get("unlocks_summary"),
                })
            tokens.append(entry)
        if tokens:
            result["tokens"] = tokens

        # v0.5 rewards: the object's own block first; otherwise derived from the
        # first reachable Token Profile (§7.4); otherwise not stated.
        if isinstance(obj.get("rewards"), dict):
            result["rewards"] = obj["rewards"]
            result["comparable"] = comparable(obj["rewards"], earn.get("display"), "rewards")
        elif first_profile is not None and (derived := rewards_from_profile(first_profile)):
            result["rewards"] = derived
            result["comparable"] = comparable(derived, earn.get("display"), "token_profile")
        else:
            result["comparable"] = comparable(None, earn.get("display"), "not_stated")

        if product_url:
            status, body, final = await _get(client, product_url, MAX_HTML_BYTES, "text/html")
            same_store = normalize_domain(final) == domain
            declared = _product_declaration(body.decode("utf-8", "replace")) if status == 200 and same_store else None
            result["product"] = {
                "url": product_url,
                "checked": status == 200 and same_store,
                "declared": declared is not None,
                "earn_display": declared,
            }
            if result["scope"] == "selected" and declared is None:
                result["product"]["meaning"] = "not covered: this store's bounty applies to selected products, and this product does not declare it"
        elif result["scope"] == "selected":
            result["product_check_needed"] = True

        return result
    finally:
        if own:
            await client.aclose()
