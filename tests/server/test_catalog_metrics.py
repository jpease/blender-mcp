"""Unit coverage for the catalog payload metrics."""

from dataclasses import dataclass
from typing import Any

import pytest

from blender_mcp.server.catalog_metrics import (
    BYTES_PER_TOKEN,
    PayloadReport,
    payload_bytes,
    payload_report,
    tool_bytes,
)


@dataclass
class _FakeTool:
    """Minimal stand-in shaped like an MCP tool for pure-function tests."""

    name: str
    description: str
    inputSchema: dict[str, Any]  # ruff: ignore[mixed-case-variable-in-class-scope] - mirrors the MCP wire field name

    def model_dump(self, *, exclude_none: bool = False) -> dict[str, Any]:
        dumped: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.inputSchema,
        }
        if not exclude_none:
            dumped["outputSchema"] = None
        return dumped


def _tool(name: str = "t", description: str = "d") -> _FakeTool:
    return _FakeTool(name=name, description=description, inputSchema={"type": "object"})


def test_tool_bytes_excludes_none_fields() -> None:
    """Null fields never cross the wire, so they must not be counted."""
    assert tool_bytes(_tool()) == len('{"name":"t","description":"d","inputSchema":{"type":"object"}}')


def test_payload_bytes_sums_tools() -> None:
    """Payload bytes must equal the sum of each tool's own byte count."""
    tools = [_tool("a"), _tool("b")]
    assert payload_bytes(tools) == tool_bytes(tools[0]) + tool_bytes(tools[1])


def test_payload_report_splits_schema_and_description() -> None:
    """The report must split total bytes into per-tool, schema, and description parts."""
    report = payload_report([_tool("a", "hello")])
    assert isinstance(report, PayloadReport)
    assert report.tool_count == 1
    assert report.per_tool["a"] == tool_bytes(_tool("a", "hello"))
    assert report.description_bytes == len("hello")
    assert report.schema_bytes == len('{"type":"object"}')
    assert report.total_bytes == report.per_tool["a"]


def test_bytes_per_token_is_documented_not_guessed() -> None:
    """The divisor is an estimate; it must stay explicit so callers can see it."""
    # rel=0, abs=0 makes this an exact-equality check (any other value fails) without
    # tripping ruff's float-equality-comparison rule on a bare `==` between two floats.
    assert pytest.approx(3.6, rel=0, abs=0) == BYTES_PER_TOKEN
