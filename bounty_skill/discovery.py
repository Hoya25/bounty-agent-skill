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
  - v0.6: a signed object (spec §9: a detached JWS in `Bounty-Signature`, keys at
    the object's origin `/.well-known/bounty-keys.json`) is reported `signed`; no
    header is `unsigned`; anything that fails is `invalid`, a HARD STOP: nothing
    from the object is used and the store is reported unknown.
  - v0.6: a localized `earn.display` is shown in the shopper's locale, else its
    `default`; an object outside `valid_from`/`valid_until` is inactive;
    eligibility, conditions, stacking, currency and verification are passed
    through; goals are matched to `fulfills` tags locally, and only funded items
    count as available.
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

import base64
from datetime import datetime, timezone

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
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


async def _get(
    client: httpx.AsyncClient, url: str, limit: int, accept: str, headers_out: dict[str, str] | None = None,
) -> tuple[int, bytes, str]:
    """Status, body (capped) and final URL. Only https is ever fetched.
    `headers_out`, when given, receives the response headers (lower-cased names)."""
    if urlsplit(url).scheme != "https":
        return 0, b"", url
    try:
        async with client.stream("GET", url, headers={"Accept": accept}) as res:
            if urlsplit(str(res.url)).scheme != "https":
                return 0, b"", str(res.url)
            if headers_out is not None:
                headers_out.update({k.lower(): v for k, v in res.headers.items()})
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
                **({"fulfills": u["fulfills"]} if isinstance(u.get("fulfills"), list) else {}),
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


# ── v0.6 §9: signed objects ─────────────────────────────────────────────────

KEYS_TTL_S = 600
_keys_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


async def _keys(client: httpx.AsyncClient, origin: str) -> dict[str, Any] | None:
    hit = _keys_cache.get(origin)
    if hit and time.monotonic() - hit[0] < KEYS_TTL_S:
        return hit[1]
    status, body, _ = await _get(client, f"{origin}/.well-known/bounty-keys.json", MAX_JSON_BYTES, "application/json")
    doc = _json_object(body) if status == 200 else None
    _keys_cache[origin] = (time.monotonic(), doc)
    return doc


def verify_signature(header: str, body: bytes, jwks: dict[str, Any] | None) -> tuple[str, str]:
    """Pure: ("signed", kid) or ("invalid", why) for a Bounty-Signature header over `body`."""
    parts = header.strip().split(".")
    if len(parts) != 3 or parts[1] != "":
        return "invalid", "not a detached JWS"
    try:
        protected = json.loads(_b64url_decode(parts[0]))
        sig = _b64url_decode(parts[2])
    except (ValueError, TypeError):
        return "invalid", "malformed"
    if protected.get("alg") != "EdDSA":
        return "invalid", f"unsupported alg {protected.get('alg')!r}"
    if not jwks or not isinstance(jwks.get("keys"), list):
        return "invalid", "the origin's key set couldn't be read"
    key = next((k for k in jwks["keys"] if isinstance(k, dict) and k.get("kid") == protected.get("kid")), None)
    if not key or key.get("kty") != "OKP" or key.get("crv") != "Ed25519" or not isinstance(key.get("x"), str):
        return "invalid", f"no Ed25519 key with kid {protected.get('kid')!r} at the origin"
    try:
        Ed25519PublicKey.from_public_bytes(_b64url_decode(key["x"])).verify(sig, f"{parts[0]}.{_b64url(body)}".encode("ascii"))
    except (InvalidSignature, ValueError):
        return "invalid", "the signature doesn't match the object"
    return "signed", str(protected.get("kid"))


# ── v0.6 completeness and goals ─────────────────────────────────────────────

def pick_display(display: Any, locale: str | None) -> tuple[str | None, str | None]:
    """A localized earn.display → (sentence, the key used). Exact locale, then its language, then default."""
    if isinstance(display, str) or display is None:
        return display, None
    if not isinstance(display, dict):
        return None, None
    want = (locale or "").lower()
    for key in (want, want.split("-")[0]):
        if key and isinstance(display.get(key), str):
            return display[key], key
    return display.get("default"), "default"


def _instant(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def outside_window(obj: dict[str, Any], now: datetime | None = None) -> str | None:
    """Why the object is outside its validity window, or None. A date-only valid_until runs to the end of that day."""
    now = now or datetime.now(timezone.utc)
    start, end = _instant(obj.get("valid_from")), _instant(obj.get("valid_until"))
    if isinstance(obj.get("valid_until"), str) and len(obj["valid_until"]) == 10 and end:
        end = end.replace(hour=23, minute=59, second=59)
    if start and now < start:
        return f"not started (valid_from {obj['valid_from']})"
    if end and now > end:
        return f"ended (valid_until {obj['valid_until']})"
    return None


def match_goals(goals: list[str], items: list[dict[str, Any]]) -> dict[str, Any]:
    """Spec §8, matched here and never sent anywhere: funded items whose tags meet a goal are
    available; committed or planned matches are listed as not yet available."""
    want = {g.strip().lower() for g in goals if isinstance(g, str) and g.strip()}
    available, later = [], []
    for it in items:
        tags = set(it.get("fulfills") or []) if isinstance(it, dict) else set()
        hit = sorted(tags & want)
        if not hit:
            continue
        entry = {"name": it.get("name"), "matches": hit}
        if it.get("state") == "funded":
            available.append({**entry, **({"description": it["description"]} if it.get("description") else {})})
        elif it.get("state") in ("committed", "planned"):
            later.append({**entry, "state": it["state"]})
    return {"goals": sorted(want), "available": available, "not_yet_available": later}


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
    locale: str | None = None,
    goals: list[str] | None = None,
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

        found: tuple[str, str, dict[str, Any], bytes, str | None] | None = None

        # Step 1: the well-known path.
        url = f"https://{domain}/.well-known/bounty.json"
        h1: dict[str, str] = {}
        status, body, final = await _get(client, url, MAX_JSON_BYTES, "application/json", h1)
        doc = _json_object(body) if status == 200 else None
        steps.append(f"1 GET {url} → {status or 'no response'}{'' if doc or status != 200 else ' (not a JSON object)'}")
        if doc is not None:
            errors = check(doc, schema_doc)
            if errors:
                rejected.append(f"{final}: {'; '.join(errors[:3])}")
                steps.append("  found, but not a valid object → continuing")
            else:
                found = ("well-known", final, doc, body, h1.get("bounty-signature"))

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
                h2: dict[str, str] = {}
                status, body, final = await _get(client, href, MAX_JSON_BYTES, "application/json", h2)
                doc = _json_object(body) if status == 200 else None
                steps.append(f"  GET {href} → {status or 'no response'}")
                if doc is not None:
                    errors = check(doc, schema_doc)
                    if errors:
                        rejected.append(f"{final}: {'; '.join(errors[:3])}")
                        steps.append("  found, but not a valid object")
                    else:
                        found = ("link", final, doc, body, h2.get("bounty-signature"))

        if found is None:
            steps.append("3 unknown")
            return {
                "store": domain,
                "status": "unknown",
                "reason": "invalid objects only: " + " | ".join(rejected) if rejected else "no bounty object published",
                "steps": steps,
                "note": "Unknown means nothing valid was found. It is not a statement that buying here pays nothing.",
            }

        via, object_url, obj, raw, sig_header = found

        # v0.6 §9: check the signature before anything in the object is used.
        signature, signed_by = "unsigned", None
        if sig_header:
            parts = urlsplit(object_url)
            origin = f"{parts.scheme}://{parts.netloc}"
            signature, detail = verify_signature(sig_header, raw, await _keys(client, origin))
            steps.append(f"  Bounty-Signature → {signature}" + (f" (kid {detail}, {origin})" if signature == "signed" else f": {detail}"))
            if signature == "invalid":
                return {
                    "store": domain,
                    "status": "unknown",
                    "signature": "invalid",
                    "reason": f"the object's signature is invalid ({detail}), so nothing in it is used",
                    "steps": steps,
                    "note": "An invalid signature is a hard stop (spec §9). Report the store as unknown and say the signature failed.",
                }
            signed_by = origin

        earn = obj.get("earn") or {}
        display, display_locale = pick_display(earn.get("display"), locale)
        window = outside_window(obj)
        result: dict[str, Any] = {
            "store": domain,
            "status": "declared" if obj.get("active") is True and not window else "inactive",
            **({"inactive_reason": window} if window else {}),
            "signature": signature,
            **({"signed_by": signed_by} if signed_by else {}),
            "found_at": via,
            "object_url": object_url,
            "earn_display": display,
            **({"earn_display_locale": display_locale} if display_locale else {}),
            "earn_type": earn.get("type"),
            "denomination": earn.get("denomination"),
            "program": obj.get("program"),
            "listed_by": obj.get("listed_by"),
            "scope": obj.get("scope", "store"),
            "status_page": (obj.get("status") or {}).get("url"),
            "terms_url": obj.get("terms_url"),
            "updated_at": obj.get("updated_at"),
            **{k: obj[k] for k in ("currency", "eligibility", "conditions", "valid_from", "valid_until", "stacking", "verification") if k in obj},
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
            result["comparable"] = comparable(obj["rewards"], display, "rewards")
        elif first_profile is not None and (derived := rewards_from_profile(first_profile)):
            result["rewards"] = derived
            result["comparable"] = comparable(derived, display, "token_profile")
        else:
            result["comparable"] = comparable(None, display, "not_stated")
        result["comparable"]["eligibility_who"] = (obj.get("eligibility") or {}).get("who")
        result["comparable"]["condition_notes"] = [c.get("note") for c in obj.get("conditions") or [] if isinstance(c, dict)]
        result["comparable"]["verification"] = (obj.get("verification") or {}).get("method")
        result["comparable"]["signature"] = signature

        if goals:
            perks = (result.get("rewards") or {}).get("perks") or []
            result["goal_matches"] = match_goals(goals, perks)

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
