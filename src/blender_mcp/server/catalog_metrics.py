"""
Measure the `tools/list` payload a server process advertises.

Every registered tool's schema is sent to every client on every request, so the payload is
permanent context occupancy rather than a one-time cost. These helpers are pure: they take
already-built tool objects and return byte counts, so they can be unit-tested without a
FastMCP app and without Blender.
"""

import json

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

# Rough divisor for turning bytes into a token estimate. JSON schema text tokenizes denser
# than prose, so this is an approximation kept explicit rather than buried in a call site.
BYTES_PER_TOKEN: float = 3.6


class _Dumpable(Protocol):
    """Structural type for objects that can serialize themselves like an MCP tool."""

    def model_dump(self, *, exclude_none: bool = False) -> dict[str, Any]:
        """
        Return a JSON-compatible dict of this object's fields.

        Args:
            exclude_none: When ``True``, omit fields whose value is ``None``.

        Returns:
            A dict suitable for JSON serialization.

        """
        ...


@dataclass(frozen=True)
class PayloadReport:
    """
    Byte accounting for one `tools/list` response.

    Attributes:
        total_bytes: Total wire bytes across every tool in the response.
        tool_count: Number of tools included in the response.
        per_tool: Wire bytes contributed by each tool, keyed by tool name.
        schema_bytes: Total bytes attributable to `inputSchema` across all tools.
        description_bytes: Total bytes attributable to `description` across all tools.

    """

    total_bytes: int
    tool_count: int
    per_tool: dict[str, int] = field(default_factory=dict)
    schema_bytes: int = 0
    description_bytes: int = 0

    @property
    def total_tokens(self) -> float:
        """
        Estimate the payload size in tokens.

        Returns:
            `total_bytes` divided by `BYTES_PER_TOKEN`.

        """
        return self.total_bytes / BYTES_PER_TOKEN


def _compact(value: Any) -> str:
    """
    Serialize a value to JSON using the most compact separators.

    Args:
        value: Any JSON-serializable value.

    Returns:
        The compact JSON string, matching the separators used on the wire.

    """
    return json.dumps(value, separators=(",", ":"))


def tool_bytes(tool: _Dumpable) -> int:
    """
    Compute the wire bytes one tool contributes to a `tools/list` payload.

    Args:
        tool: A tool-like object exposing `model_dump(exclude_none=...)`.

    Returns:
        The number of bytes the compact JSON encoding of the tool occupies, excluding
        fields that are never sent (i.e. fields whose value is `None`).

    """
    return len(_compact(tool.model_dump(exclude_none=True)))


def payload_bytes(tools: Sequence[_Dumpable]) -> int:
    """
    Compute the total wire bytes for a `tools/list` response.

    Args:
        tools: The tools that would be advertised in the response.

    Returns:
        The sum of `tool_bytes` across all tools.

    """
    return sum(tool_bytes(tool) for tool in tools)


def payload_report(tools: Sequence[_Dumpable]) -> PayloadReport:
    """
    Build a per-tool and aggregate byte accounting for a `tools/list` response.

    Args:
        tools: The tools that would be advertised in the response.

    Returns:
        A `PayloadReport` splitting total bytes by tool, and further by schema versus
        description contribution.

    """
    per_tool: dict[str, int] = {}
    schema_bytes = 0
    description_bytes = 0

    for tool in tools:
        dumped = tool.model_dump(exclude_none=True)
        per_tool[dumped["name"]] = len(_compact(dumped))
        schema_bytes += len(_compact(dumped.get("inputSchema") or {}))
        description_bytes += len(dumped.get("description") or "")

    return PayloadReport(
        total_bytes=sum(per_tool.values()),
        tool_count=len(per_tool),
        per_tool=per_tool,
        schema_bytes=schema_bytes,
        description_bytes=description_bytes,
    )
