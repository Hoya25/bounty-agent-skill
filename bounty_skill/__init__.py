# SPDX-License-Identifier: Apache-2.0
"""Open Bounty Schema discovery for shopping agents: spec §4, validated, never "no"."""

from .discovery import SCHEMA_URL, normalize_domain, resolve

__all__ = ["SCHEMA_URL", "normalize_domain", "resolve"]
__version__ = "0.1.0"
