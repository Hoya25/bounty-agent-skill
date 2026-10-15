# Open Bounty skill for the Claude commerce-agents blueprint

Lets a shopping agent built on Anthropic's
[commerce-agents blueprint](https://github.com/anthropics/commerce-agents), such as
Shopify's [claude-for-commerce-examples](https://github.com/Shopify/claude-for-commerce-examples)
storefront, answer **"do I earn anything buying here?"** from the store's published
[Open Bounty Schema](https://github.com/Hoya25/open-bounty-schema) declaration, and answer it
truthfully.

It adds one skill (`open-bounty`) and one tool (`check_bounty`). The tool:

- runs the spec's **§4 discovery order** for any store domain: `/.well-known/bounty.json`, then
  `<link rel="bounty">` in the home page `<head>`;
- **validates** what it finds against the stable schema alias
  `https://api.nctr.live/schema/bounty/v1.json` (every `0.x` object is accepted);
- returns the store's **`earn.display` verbatim**, plus `program` and `listed_by` (who published the
  claim);
- **follows `api`** and **`tokens`** when present, and reports whether they answer;
- handles **`scope: "selected"`** by checking a product page's own declaration;
- reports **`unknown`, never "no"**, when nothing valid is published;
- reads the v0.5 **`rewards`** block, or derives it from the Token Profile when it's absent, and
  returns one **comparable** summary for any program: what the unit is worth (as stated), the
  entry-tier rule, the number of funded perks, and the ladder's depth.

The skill tells the agent how to say all that: quote the sentence, name the program, don't compute
values, never turn "unknown" into "no", and never present a planned or committed token unlock as
available.

---

## Add it to the claude-for-commerce-examples storefront

These steps were run against
[`Shopify/claude-for-commerce-examples`](https://github.com/Shopify/claude-for-commerce-examples)
at `d68c7fa`, with the blueprint pinned there
(`commerce-agents@fd4d59224ab96b43c6dc6888207c67b3bd5a24cf`).

**1. Install the package** into the same environment as the storefront:

```bash
pip install "git+https://github.com/Hoya25/bounty-agent-skill"
```

**2. Copy the skill** into the storefront's skills directory:

```bash
cp -R path/to/bounty-agent-skill/skills/open-bounty vendor/skills/shopping/
```

**3. Install the tool** right after the agent is built, in `storefront/api/main.py`. Two lines
(the full diff is in [`examples/claude-for-commerce-examples.patch`](examples/claude-for-commerce-examples.patch)):

```python
from bounty_skill.blueprint import install as install_bounty
# …
agent = ShoppingAgent(
    backend=backend,
    skills_dir=REPO_ROOT / "vendor" / "skills" / "shopping",
    config=build_shopping_config(store_name),
    memory_store=InMemoryMemoryStore(),
)
install_bounty(agent, default_store=store_name)
```

`default_store` is the store the agent sells for. An empty `store_domain` in a tool call means
this store.

**4. Run as usual** (`uvicorn storefront.api.main:app --port 8004`) and ask:
*"Do I earn any rewards if I buy here?"*

### Why steps 2 and 3 are both needed

- The **skill index is part of the static prompt**, built when `ShoppingAgent` is constructed, so
  the skill must be in the directory **before** construction (step 2).
- The **tool** is added after construction (step 3), through the blueprint's own seam:
  - `executor_class`, documented on `ShoppingAgent` as "the seam for a deployment's own
    ShoppingToolExecutor", gains a `check_bounty` handler;
  - the contract is appended once, so the tool list stays identical on every request, as the
    blueprint's prompt caching requires, with the cache breakpoint on the new last tool.

Nothing in the pinned blueprint packages is modified.

### Verified

| Check | Result |
|---|---|
| Patched storefront imports and builds | executor `BountyShoppingToolExecutor`; last tool `check_bounty` with the cache breakpoint; `open-bounty` in the skill index and the static prompt |
| The example's own test suite, patched | **231 passed**, the same as unpatched |
| This repo's tests | 22 offline tests (every §4 path, validation, scope, unreachable api/tokens, https-only, v0.5 rewards read and derived), 4 of them against the real blueprint executor |

**Store text is untrusted.** `check_bounty` returns its whole result through the executor's
`_fenced(...)`, like every other record read in the blueprint: the earn sentence, the program
name, everything. It reaches the model as quoted data. The tests include a store sentence that
forges the blueprint's own closing fence marker and then issues an instruction; it stays inside
the record.

---

## Use it without the blueprint

The discovery is plain Python with two dependencies (`httpx`, `jsonschema`):

```python
from bounty_skill import resolve
result = await resolve("merch.nctr.live")
```

or from the shell:

```bash
python -m bounty_skill merch.nctr.live oliveandjune.com api.nctr.live
python -m bounty_skill shop.example --product https://shop.example/products/thing
python -m bounty_skill merch.nctr.live --json
```

### What it returns

| Field | Meaning |
|---|---|
| `status` | `declared` (a valid, active object), `inactive` (the store says no bounty right now), or **`unknown`** (nothing valid found; **not "no"**) |
| `found_at` | `well-known` or `link`, the §4 step that found it |
| `earn_display` | the sentence, verbatim. Quote it; never reword it. |
| `program` | `{ name, url }` |
| `listed_by` | `store`, `program`, `index`, or absent (no publisher claimed) |
| `scope` | `store` or `selected`. With `selected` and no `product_url`, `product_check_needed: true` |
| `api` | `{ url, status, reachable }` |
| `tokens` | per Token Profile: `{ url, reachable, symbol, name, lifecycle, unlocks_summary }` |
| `validated_against` | the schema `$id` and the object's version |
| `rewards` | v0.5: the object's `rewards` block, or the same fields derived from the first reachable Token Profile (spec §7.4) |
| `comparable` | `{ source, unit_value, entry_tier_rule, funded_perks, funded_perk_names, not_yet_available, ladder_depth, ladder_basis, ladder_unit }`. `source` is `rewards`, `token_profile` or `not_stated`; `null` means not stated, never zero |
| `steps` | the discovery walk, request by request |
| `reason` | for `unknown`: nothing published, or which object was invalid and why |

---

## The live check

```bash
pip install -e ".[test]"
pytest                                   # offline: mocked HTTP, plus the blueprint seam if installed
BOUNTY_LIVE=1 pytest tests/test_live_cli.py -v
```

The live test runs the CLI against three domains:

| Store | Expected | Why |
|---|---|---|
| `merch.nctr.live` | **declared**, found through `<link rel="bounty">` (step 2) | the store's theme links to the program's object |
| `oliveandjune.com` | **unknown** | it runs a loyalty program, but publishes no bounty object. A loyalty adapter could compose one, but only when the store installs it. Unknown, never "no". |
| `api.nctr.live` | **declared** at `/.well-known/bounty.json` (step 1) | the NCTR Alliance's own program object |

Output from a run on 2026-10-14:

```
merch.nctr.live: DECLARED
    1 GET https://merch.nctr.live/.well-known/bounty.json → 404
    2 GET https://merch.nctr.live/ → 200; rel="bounty" link found
      GET https://api.nctr.live/.well-known/bounty.json → 200
    earn.display: "Members earn NCTR on every purchase — earning grows with status."
    program: NCTR Alliance (https://themall.nctr.live)
    listed_by: (absent, no publisher claimed) · found_at: link · scope: store
    validated against https://api.nctr.live/schema/bounty/v1.json · object version 0.4
    api: https://api.nctr.live/v1/bounties → reachable (200)

oliveandjune.com: UNKNOWN
    1 GET https://oliveandjune.com/.well-known/bounty.json → 404
    2 GET https://oliveandjune.com/ → 200; rel="bounty" link not found
    3 unknown
    (unknown is not "no": nothing valid was published)

api.nctr.live: DECLARED
    1 GET https://api.nctr.live/.well-known/bounty.json → 200
    …
```

---

## Notes

- **https only.** A link to an `http:` object is ignored. Responses are size-capped (512 KB JSON,
  3 MB HTML), and nothing fetched is ever executed.
- **A `200` HTML page at the well-known path** (some platforms answer every path) is treated as
  not found, and discovery continues to step 2.
- **An invalid object** is reported as `unknown`, with the validation errors in `reason`. An agent
  should not repeat a claim that doesn't validate.
- The schema alias is cached for 10 minutes in-process.

License: Apache-2.0, matching the blueprint it extends.
