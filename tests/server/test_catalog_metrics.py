"""Unit coverage for the catalog payload metrics."""

from dataclasses import dataclass
from typing import Any

import pytest

from blender_mcp.server.catalog_metrics import (
    PayloadReport,
    payload_report,
)


@dataclass
class _FakeTool:
    """Minimal stand-in shaped like an MCP tool for pure-function tests."""

    name: str
    description: str
    inputSchema: dict[str, Any]  # ruff: ignore[mixed-case-variable-in-class-scope] - mirrors the MCP wire field name

    def model_dump(self, *, exclude_none: bool = False) -> dict[str, Any]:
        """
        Mimic an MCP tool's dump of itself.

        Args:
            exclude_none: When True, omit the always-None `outputSchema`.

        Returns:
            The dict a real tool would serialize to.

        """
        dumped: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.inputSchema,
        }
        if not exclude_none:
            dumped["outputSchema"] = None
        return dumped


def _tool(name: str = "t", description: str = "d") -> _FakeTool:
    """
    Build a fake tool for the pure-function tests.

    Args:
        name: Tool name.
        description: Tool description.

    Returns:
        A `_FakeTool` carrying a minimal one-property input schema.

    """
    return _FakeTool(name=name, description=description, inputSchema={"type": "object"})


def test_tool_bytes_excludes_none_fields() -> None:
    """Null fields never cross the wire, so they must not be counted."""
    report = payload_report([_tool()])
    assert report.per_tool["t"] == len('{"name":"t","description":"d","inputSchema":{"type":"object"}}')


def test_total_bytes_sums_every_tool() -> None:
    """The report total must equal the sum of each tool's own byte count, not just one of them."""
    tools = [_tool("a"), _tool("b")]
    report = payload_report(tools)
    assert report.total_bytes == payload_report([tools[0]]).total_bytes + payload_report([tools[1]]).total_bytes
    assert report.tool_count == len(tools)


def test_payload_report_splits_schema_and_description() -> None:
    """The report must split total bytes into per-tool, schema, and description parts."""
    report = payload_report([_tool("a", "hello")])
    assert isinstance(report, PayloadReport)
    assert report.tool_count == 1
    assert report.description_bytes == len("hello")
    assert report.schema_bytes == len('{"type":"object"}')


def test_payload_report_rejects_duplicate_tool_names() -> None:
    """A repeated tool name is a malformed payload, not a quantity to silently collapse."""
    with pytest.raises(ValueError, match="dup"):
        payload_report([_tool("dup"), _tool("dup")])


def test_total_tokens_divides_bytes_by_the_documented_divisor() -> None:
    """
    `total_tokens` must apply the module's byte-per-token divisor.

    The divisor is spelled literally rather than imported, so changing BYTES_PER_TOKEN fails
    here and forces the comment above it - which calls 3.6 an unvalidated rule of thumb - to
    be revisited alongside it.
    """
    report = payload_report([_tool("a", "hello")])
    assert report.total_tokens == pytest.approx(report.total_bytes / 3.6)


def test_description_bytes_counts_the_encoded_form() -> None:
    """A non-ASCII description must be counted as encoded, not as characters."""
    report = payload_report([_tool("a", "a\u2014b")])
    assert report.description_bytes == len("a\\u2014b")


def test_report_per_tool_is_not_mutable() -> None:
    """A frozen report must be frozen through its mapping too, not just its attribute bindings."""
    report = payload_report([_tool()])
    with pytest.raises(TypeError):
        report.per_tool["x"] = 1  # pyright: ignore[reportIndexIssue]  - proving the mapping is read-only
