"""
Measure the `tools/list` payload a server process advertises.

See `bundles.py` for why that payload matters. The helpers are pure, so they test without a
FastMCP app or Blender. Only `scripts/measure_catalog.py` uses this at runtime; it lives in the
package because `scripts/` is not importable.
"""

import json

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

# An unvalidated rule of thumb, so token figures are order-of-magnitude only. Check a real
# tokenizer before using one to argue a threshold is met.
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

    Build it with `payload_report`. It is immutable but not hashable, because it holds a mapping.

    Attributes:
        per_tool: Wire bytes contributed by each tool, keyed by tool name.
        schema_bytes: Total bytes attributable to `inputSchema` across all tools.
        description_bytes: Total bytes attributable to `description` across all tools,
            excluding each value's two surrounding quotes.

    """

    per_tool: Mapping[str, int]
    schema_bytes: int
    description_bytes: int

    def __post_init__(self) -> None:
        """Freeze `per_tool`, however the report was built."""
        object.__setattr__(self, "per_tool", MappingProxyType(dict(self.per_tool)))

    @property
    def total_bytes(self) -> int:
        """
        Total wire bytes across every tool in the response.

        Returns:
            The sum of every tool's byte contribution.

        """
        return sum(self.per_tool.values())

    @property
    def tool_count(self) -> int:
        """
        Number of tools included in the response.

        Returns:
            How many tools the payload advertises.

        """
        return len(self.per_tool)

    @property
    def total_tokens(self) -> float:
        """
        Estimate the payload size in tokens.

        Returns:
            `total_bytes` divided by `BYTES_PER_TOKEN`.

        """
        return self.total_bytes / BYTES_PER_TOKEN


def _json_bytes(value: dict[str, Any] | str) -> int:
    """
    Compute the wire bytes of any JSON-serializable value.

    Every counter here uses this, so all figures share one encoding. The output is ASCII-escaped,
    so its length is its byte count.

    Args:
        value: A dumped tool, an `inputSchema` mapping, or a description string.

    Returns:
        The byte count of its compact JSON encoding.

    """
    return len(json.dumps(value, separators=(",", ":")))


def _text_bytes(text: str) -> int:
    r"""
    Compute the wire bytes a string contributes as a JSON value, excluding its quotes.

    Measures the encoded form, not `len(text)`, so a non-ASCII character counts at its wire size
    (an em dash is six bytes as `\u2014`).

    Args:
        text: The raw string value, such as a tool description.

    Returns:
        The byte count of its compact JSON encoding, less the two surrounding quotes.

    """
    return _json_bytes(text) - 2


def payload_report(tools: Sequence[_Dumpable]) -> PayloadReport:
    """
    Build a per-tool and aggregate byte accounting for a `tools/list` response.

    Args:
        tools: The tools that would be advertised in the response.

    Returns:
        A `PayloadReport` splitting total bytes by tool, and further by schema versus
        description contribution.

    Raises:
        ValueError: If two tools share a name; a name-keyed report cannot represent that
            case without silently under-counting.

    """
    per_tool: dict[str, int] = {}
    schema_bytes = 0
    description_bytes = 0

    for tool in tools:
        dumped = tool.model_dump(exclude_none=True)
        name = dumped["name"]
        if name in per_tool:
            # Keying by name would drop one tool from per_tool but not from the other totals.
            raise ValueError(f"duplicate tool name in payload: {name!r}")
        per_tool[name] = _json_bytes(dumped)
        schema_bytes += _json_bytes(dumped.get("inputSchema") or {})
        description_bytes += _text_bytes(dumped.get("description") or "")

    return PayloadReport(
        per_tool=per_tool,
        schema_bytes=schema_bytes,
        description_bytes=description_bytes,
    )
