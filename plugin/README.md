# Bounty

Bounty answers one shopping question in Claude: **do I earn anything if I buy here?**

Stores, rewards programs and loyalty platforms publish what a purchase earns in the
[Open Bounty Schema](https://github.com/Hoya25/open-bounty-schema), the open-source standard for
rewards declarations. Each store publishes a small object at its own address with one plain
sentence that states the reward: points, store credit, tokens or perks. This plugin connects Claude
to the Bounty connector and teaches Claude to report what it finds truthfully.

## What's in the plugin

| Component | What it does |
|---|---|
| `bounty` MCP server (`.mcp.json`) | The hosted Bounty connector at `https://api.nctr.live/mcp/bounty`. No sign-in. |
| `open-bounty` skill | When and how to call the connector, and how to say what it returns: quote the store's sentence word for word, name the program, never turn "unknown" into "no", never compute a value. |

The connector has two read-only tools:

- **`get_bounty(domain, product?)`** reads a store's own declaration (its
  `/.well-known/bounty.json`, or the object its `<link rel="bounty">` points to), checks it against
  the standard and its signature, and returns the store's sentence, the program and the object.
- **`search_stores(query)`** searches the Bounty index of 6,000+ stores where a purchase earns, and
  returns up to 10 matches in alphabetical order. The index is an open registry any store can enter
  by publishing a declaration.

## Example prompts

- "Do I earn anything if I buy at shop.example?"
- "Does this store have a rewards program?"
- "Is this product covered by the store's rewards?"
- "Where can I buy outdoor gear and earn rewards?"

## What the plugin sends, and where

The plugin runs no local code. Claude sends the connector only what a tool needs: a store domain,
a product page address, or a search keyword. The connector fetches that store's public
declaration and home page, and returns the result. It keeps no request logs and sets no cookies;
each store's result is cached for 5 minutes by domain.

## Privacy policy

[https://api.nctr.live/privacy](https://api.nctr.live/privacy)

## Support

hello@butterflystudios.live · Butterfly Studios

## License

Apache-2.0. See [LICENSE](LICENSE).
