# SPDX-License-Identifier: Apache-2.0
"""python -m bounty_skill <store-domain> [<store-domain> ...] [--product URL] [--json]

Runs the spec §4 discovery for each store and prints what an agent would report.
Exit code 0 always for a completed check: "unknown" is an answer, not an error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .discovery import resolve


def _summary(r: dict) -> str:
    lines = [f"{r['store']}: {r['status'].upper()}"]
    for step in r.get("steps", []):
        lines.append(f"    {step}")
    if r["status"] in ("declared", "inactive"):
        program = r.get("program") or {}
        lines.append(f'    earn.display: "{r.get("earn_display")}"')
        lines.append(f"    program: {program.get('name')} ({program.get('url')})")
        lines.append(f"    listed_by: {r.get('listed_by') or '(absent, no publisher claimed)'} · found_at: {r.get('found_at')} · scope: {r.get('scope')}")
        lines.append(f"    validated against {r['validated_against']['schema']} · object version {r['validated_against']['object_version']}")
        if "api" in r:
            lines.append(f"    api: {r['api']['url']} → {'reachable' if r['api']['reachable'] else 'NOT reachable'} ({r['api']['status']})")
        for t in r.get("tokens", []):
            lines.append(f"    token: {t.get('symbol') or '?'} {t['url']} → {'reachable' if t['reachable'] else 'NOT reachable'}"
                         + (f" · unlocks {t['unlocks_summary']}" if t.get("unlocks_summary") else ""))
        c = r.get("comparable")
        if c:
            src = {"rewards": "from the object's rewards block", "token_profile": "derived from the Token Profile", "not_stated": "no rewards stated"}[c["source"]]
            lines.append(f"    comparable ({src}):")
            lines.append(f'      unit value: {chr(34) + c["unit_value"] + chr(34) if c["unit_value"] else "not stated"}')
            et = c["entry_tier_rule"]
            lines.append(f'      entry-tier rule: "{et["earn_display"]}"' + (f' (entry tier: {et["entry_tier"]})' if et["entry_tier"] else ""))
            fp = c["funded_perks"]
            lines.append(f"      funded perks: {'not stated' if fp is None else fp}" + (f" ({', '.join(c['funded_perk_names'])})" if c["funded_perk_names"] else "")
                         + (f" · not yet available: {c['not_yet_available']['committed']} committed, {c['not_yet_available']['planned']} planned" if c["not_yet_available"] and any(c["not_yet_available"].values()) else ""))
            lines.append(f"      ladder depth: {'not stated' if c['ladder_depth'] is None else c['ladder_depth']}"
                         + (f" ({c['ladder_basis']}, in {c['ladder_unit']})" if c["ladder_depth"] else ""))
        if "product" in r:
            p = r["product"]
            lines.append(f"    product {p['url']}: {'declares' if p['declared'] else 'no product-level declaration'}"
                         + (f' "{p["earn_display"]}"' if p.get("earn_display") else ""))
    else:
        lines.append(f"    reason: {r.get('reason')}")
        lines.append("    (unknown is not \"no\": nothing valid was published)")
    return "\n".join(lines)


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m bounty_skill", description=__doc__.splitlines()[0])
    parser.add_argument("stores", nargs="+")
    parser.add_argument("--product", help="a product page URL to check (for scope: selected)")
    parser.add_argument("--json", action="store_true", help="print the raw results as JSON")
    args = parser.parse_args(argv)
    results = [await resolve(s, product_url=args.product) for s in args.stores]
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print("\n\n".join(_summary(r) for r in results))
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
