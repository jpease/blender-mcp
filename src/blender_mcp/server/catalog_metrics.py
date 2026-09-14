"""
Measure the `tools/list` payload a server process advertises.

See `bundles.py` for why an advertised payload is permanent context cost rather than a
one-time startup cost. These helpers are pure: they take already-built tool objects and
return byte counts, so they can be unit-tested without a FastMCP app and without Blender.

Nothing at runtime imports this module. It ships inside the package deliberately: `scripts/`
is not an importable package, and keeping the pure half here is what makes it testable.
"""

import json

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

# Rough divisor for turning bytes into a token estimate. JSON schema text tokenizes denser
# than prose, so this is an approximation kept explicit rather than buried in a call site.
# 3.6 is an unvalidated rule of thumb -- no measurement in this repo derives it. Treat every
# printed token figure as order-of-magnitude, and re-derive this against a real tokenizer
# before using token counts to argue a threshold has been met.
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

    `payload_report` is the only supported constructor. `per_tool` is a read-only view, so a
    frozen report is immutable through and through; it is not hashable, since it carries a
    mapping. `total_bytes`, `tool_count` and `total_tokens` are properties derived from
    `per_tool`, so a report cannot disagree with itself.

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
        """Freeze `per_tool` so the documented immutability holds however the report was built."""
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

    This is the single definition of "how many bytes does this occupy on the wire" - every
    counter in this module routes through it, so no figure is computed in a different
    encoding. (`description_bytes` alone then subtracts its two delimiting quotes; see
    `_text_bytes`.) The encoding is ASCII-escaped, so the string's length is exactly its
    byte count.

    Args:
        value: A dumped tool, an `inputSchema` mapping, or a description string.

    Returns:
        The byte count of its compact JSON encoding.

    """
    return len(json.dumps(value, separators=(",", ":")))


def _text_bytes(text: str) -> int:
    r"""
    Compute the wire bytes a string contributes as a JSON value, excluding its quotes.

    Measuring the encoded form rather than `len(text)` keeps this counter in the same unit
    as the schema and total counters: a non-ASCII character costs what it actually costs on
    the wire (an em dash is six bytes as `\u2014`, not one).

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
            # Overwriting per_tool[name] while still accumulating both tools into
            # schema_bytes/description_bytes would yield an internally inconsistent,
            # artificially smaller report. A repeated name is a malformed payload.
            raise ValueError(f"duplicate tool name in payload: {name!r}")
        per_tool[name] = _json_bytes(dumped)
        schema_bytes += _json_bytes(dumped.get("inputSchema") or {})
        description_bytes += _text_bytes(dumped.get("description") or "")

    return PayloadReport(
        per_tool=per_tool,
        schema_bytes=schema_bytes,
        description_bytes=description_bytes,
    )
