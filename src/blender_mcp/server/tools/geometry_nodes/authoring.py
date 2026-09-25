"""Validated interface and graph authoring tools."""

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context
from pydantic import Field, model_validator

from ...app import mcp
from .._dispatch import call_blender
from .._inputs import OpenPayloadModel, dump_inputs
from .._node_graph import NodeGraphEdit

SocketDirection = Literal["INPUT", "OUTPUT"]
CollisionPolicy = Literal["ERROR", "REUSE", "UNIQUE"]


class InterfaceSocketSpec(OpenPayloadModel):
    """Describe one public node-group socket and its agent-visible contract."""

    name: str = Field(min_length=1, max_length=128)
    direction: SocketDirection
    socket_type: str = Field(pattern=r"^NodeSocket[A-Za-z0-9_]+$")
    parent_panel: str | None = None
    description: str = ""
    default_value: Any = None
    min_value: float | int | None = None
    max_value: float | int | None = None
    subtype: str | None = None
    attribute_domain: str | None = None
    hide_value: bool | None = None
    default_attribute_name: str | None = None

    @model_validator(mode="after")
    def validate_range(self) -> "InterfaceSocketSpec":
        """Reject inverted numeric ranges before Blender is mutated."""
        if self.min_value is not None and self.max_value is not None and self.min_value > self.max_value:
            raise ValueError("min_value must not exceed max_value")
        return self


class InterfacePanelSpec(OpenPayloadModel):
    """Describe one interface panel that groups related exposed controls."""

    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    default_closed: bool = False
    parent_panel: str | None = None


class InterfaceEdit(OpenPayloadModel):
    """Describe one stable-identifier interface mutation."""

    operation: Literal["ADD_SOCKET", "ADD_PANEL", "UPDATE", "MOVE", "REMOVE"]
    identifier: str | None = None
    socket: InterfaceSocketSpec | None = None
    panel: InterfacePanelSpec | None = None
    changes: dict[str, Any] = Field(default_factory=dict)
    position: int | None = Field(default=None, ge=0)
    parent_identifier: str | None = None


class GraphEdit(NodeGraphEdit):
    """Describe one ordered Geometry Nodes graph mutation in an atomic patch."""


@mcp.tool()
async def create_geometry_node_group(
    ctx: Context,
    name: str,
    execution_role: Literal["MODIFIER", "TOOL"] = "MODIFIER",
    geometry_types: list[Literal["MESH", "CURVE", "POINTCLOUD", "GREASE_PENCIL"]] | None = None,
    tool_modes: list[Literal["OBJECT", "EDIT", "SCULPT", "PAINT"]] | None = None,
    sockets: list[InterfaceSocketSpec] | None = None,
    panels: list[InterfacePanelSpec] | None = None,
    description: str = "",
    color_tag: str = "NONE",
    collision_policy: CollisionPolicy = "ERROR",
    purpose: str = "custom procedural system",
) -> dict:
    """
    Create an editable Geometry Nodes group with an explicit execution role and interface.

    Modifier groups default to a Geometry input/output pass-through when ``sockets`` is omitted.
    The group receives a stable MCP UUID, schema version, and purpose tag for later discovery.
    """
    params = {
        "name": name,
        "execution_role": execution_role,
        "geometry_types": geometry_types,
        "tool_modes": tool_modes,
        "sockets": dump_inputs(sockets or []),
        "panels": dump_inputs(panels or []),
        "description": description,
        "color_tag": color_tag,
        "collision_policy": collision_policy,
        "purpose": purpose,
    }
    return await call_blender("create_geometry_node_group", params, changed_resources=[name])


@mcp.tool()
async def edit_node_group_interface(
    ctx: Context,
    node_group_name: str,
    edits: Annotated[list[InterfaceEdit], Field(min_length=1, max_length=200)],
    migration_policy: Literal["PRESERVE_COMPATIBLE", "ALLOW_BREAKING", "ERROR_ON_BREAKING"] = "ERROR_ON_BREAKING",
) -> dict:
    """
    Apply a preflighted batch of interface socket and panel edits by stable identifier.

    Removing or changing socket types requires ``ALLOW_BREAKING`` because modifier overrides and
    links may be invalidated. affected_users counts the objects that use the group;
    get_geometry_node_graph's IDENTITY section names each modifier.
    """
    return await call_blender(
        "edit_node_group_interface",
        {
            "node_group_name": node_group_name,
            "edits": dump_inputs(edits),
            "migration_policy": migration_policy,
        },
        changed_resources=[node_group_name],
    )


@mcp.tool()
async def patch_geometry_node_graph(
    ctx: Context,
    node_group_name: str,
    operations: Annotated[list[GraphEdit], Field(min_length=1, max_length=500)],
) -> dict:
    """
    Atomically add, configure, connect, frame, or remove nodes in one group.

    The complete patch is validated before mutation. Node types and writable properties are
    runtime-checked; socket endpoints use identifiers with an optional index fallback. If any
    operation fails, the original graph is restored. affected_users counts the objects that use it.
    """
    return await call_blender(
        "patch_geometry_node_graph",
        {"node_group_name": node_group_name, "operations": dump_inputs(operations)},
        changed_resources=[node_group_name],
    )
