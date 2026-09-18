"""
Every reply a `shot`-mode tool sends must fit the per-reply byte budget.

`test_bundles.py` bounds what the catalog costs per turn; this bounds what one call costs. The
measurement is the harness in `scripts/measure_reply_sizes.py`, whose stub payloads mirror the
add-on handlers, so a handler that starts returning more state fails here rather than quietly
spending the agent's context. A tool that relies on the envelope shortening its page to get
under the budget still passes - the point is that no reply passes the budget on the wire.
"""

import json
import os
import subprocess
import sys

from conftest import REPO_ROOT

from blender_mcp.server.bundles import TOOLSETS_ENV_VAR
from blender_mcp.server.tools.envelope import REPLY_BYTE_BUDGET


def _measured_replies(selection: str) -> dict[str, int]:
    """
    Measure every reply the selection's tools send, in the bytes FastMCP puts on the wire.

    In a subprocess because the harness sets the toolset selection before importing the server.

    Args:
        selection: The BLENDER_MCP_TOOLSETS value to measure.

    Returns:
        Tool name mapped to its reply's wire bytes.

    """
    environment = {key: value for key, value in os.environ.items() if key != TOOLSETS_ENV_VAR}
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/measure_reply_sizes.py"), selection, "--json"],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
        env=environment,
    )
    measured = json.loads(result.stdout.splitlines()[-1])
    assert measured["budget"] == REPLY_BYTE_BUDGET, "the harness must measure against the shipped budget"
    return measured["replies"]


def test_no_shot_mode_reply_passes_the_budget() -> None:
    """A reply over the budget spends more of the session's context than any single call is worth."""
    over_budget = {
        tool: size for tool, size in _measured_replies("shot").items() if size > REPLY_BYTE_BUDGET
    }

    assert not over_budget, f"replies over the {REPLY_BYTE_BUDGET}-byte budget: {over_budget}"
