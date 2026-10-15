# SPDX-License-Identifier: Apache-2.0
"""Wiring for Anthropic's commerce-agents blueprint (the shopping agent), as deployed
by Shopify/claude-for-commerce-examples.

The blueprint's tool contracts are fixed in its core; a deployment extends it through
two seams, and this module uses exactly those:

  - ``executor_class`` — documented on ``ShoppingAgent`` as "the seam for a
    deployment's own ShoppingToolExecutor". The subclass adds one handler,
    ``check_bounty``, and fences its whole result as a record: the earn sentence,
    program name and every other field are written by the store, so they reach the
    model as quoted data, never as instructions.
  - one tool contract, appended to the agent's tool list once, when the agent is
    built. The list stays the same bytes on every request (the blueprint's prompt
    caching depends on it); the cache breakpoint moves to the new last tool.

The skill itself (skills/open-bounty/SKILL.md) is copied into the agent's skills
directory before the agent is constructed, because the skill index is part of the
static prompt built at construction.

Tested against commerce-agents @ fd4d59224ab96b43c6dc6888207c67b3bd5a24cf, the commit
claude-for-commerce-examples pins.
"""

from __future__ import annotations

from typing import Any

from . import discovery

TOOL_NAME = "check_bounty"

BOUNTY_TOOL: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Whether buying at a store pays the shopper a reward, read from the store's "
        "published Open Bounty declaration. Returns status (declared, inactive, or "
        "unknown), the store's earn sentence verbatim, the program, and who published it. "
        "Leave store_domain empty for this store. Pass product_url to check one product "
        "when the declaration covers selected products only. Pass goals (tags such as "
        "concert_tickets) to match the program's perks locally; only funded ones are available."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "store_domain": {
                "type": "string",
                "description": "A store's domain, e.g. shop.example. Empty means this store.",
            },
            "product_url": {
                "type": "string",
                "description": "Full https URL of one product page on that store, when needed.",
            },
            "goals": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The customer's goals as tags (concert_tickets, gym_membership, travel …). Matched on this server; never sent to the store or program.",
            },
            "locale": {
                "type": "string",
                "description": "The customer's locale (e.g. fr, pt-br), for a store that states its sentence in several languages.",
            },
        },
        "additionalProperties": False,
    },
}


def make_executor_class(base: type, default_store: str | None = None) -> type:
    """A subclass of the deployment's executor with ``check_bounty`` added."""

    from commerce_common.streaming import ToolOutcome

    class BountyToolExecutor(base):  # type: ignore[misc, valid-type]
        def handlers(self):  # noqa: D401 — blueprint hook
            return {**super().handlers(), TOOL_NAME: self._check_bounty}

        async def _check_bounty(self, tool_input: dict[str, Any]) -> Any:
            store = self._sanitize(tool_input.get("store_domain") or default_store or "", 253)
            if not store:
                return ToolOutcome.error("No store domain is configured; ask which store they mean.")
            product_url = self._sanitize(tool_input.get("product_url") or "", 2048) or None
            goals = [self._sanitize(str(g), 64) for g in (tool_input.get("goals") or [])[:20] if isinstance(g, str)]
            locale = self._sanitize(tool_input.get("locale") or "", 16) or None
            result = await discovery.resolve(store, product_url=product_url, locale=locale, goals=goals or None)
            return self._fenced(result)

    BountyToolExecutor.__name__ = f"Bounty{base.__name__}"
    return BountyToolExecutor


def install(agent: Any, *, default_store: str | None = None) -> Any:
    """Add the bounty tool to a constructed ``ShoppingAgent``. Idempotent."""

    from commerce_common.execution import with_status
    from commerce_common.prompt_assembly import with_tool_cache_control

    if not getattr(agent.executor_class, "_bounty_installed", False):
        cls = make_executor_class(agent.executor_class, default_store)
        cls._bounty_installed = True
        agent.executor_class = cls

    tools = [{k: v for k, v in tool.items() if k != "cache_control"} for tool in agent._tools]
    if not any(tool["name"] == TOOL_NAME for tool in tools):
        tools.append(with_status(BOUNTY_TOOL, "the customer"))
    agent._tools = with_tool_cache_control(tools)
    return agent
