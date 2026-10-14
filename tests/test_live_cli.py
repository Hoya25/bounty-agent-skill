# SPDX-License-Identifier: Apache-2.0
"""The CLI against live stores. Runs only with BOUNTY_LIVE=1 (it uses the network).

    BOUNTY_LIVE=1 pytest tests/test_live_cli.py -v

  merch.nctr.live   declared, found through <link rel="bounty"> (step 2)
  oliveandjune.com  unknown: it runs a loyalty program, but publishes no bounty
                    object, and the adapter that could compose one only publishes
                    when the store installs it. Unknown, never "no".
  api.nctr.live     declared at /.well-known/bounty.json (step 1)
"""

import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("BOUNTY_LIVE") != "1", reason="set BOUNTY_LIVE=1 to hit the network")


def run(*stores):
    out = subprocess.run([sys.executable, "-m", "bounty_skill", "--json", *stores], capture_output=True, text=True, timeout=120, check=True)
    return {r["store"]: r for r in json.loads(out.stdout)}


def test_three_stores():
    r = run("merch.nctr.live", "oliveandjune.com", "api.nctr.live")

    merch = r["merch.nctr.live"]
    assert merch["status"] == "declared" and merch["found_at"] == "link"
    assert merch["earn_display"] and merch["program"]["name"]
    assert merch["api"]["reachable"] is True

    oj = r["oliveandjune.com"]
    assert oj["status"] == "unknown"
    assert "no" != oj["status"]

    api = r["api.nctr.live"]
    assert api["status"] == "declared" and api["found_at"] == "well-known"
    assert api["validated_against"]["schema"].endswith("/schema/bounty/v1.json")
    assert all(t["reachable"] for t in api.get("tokens", []))
