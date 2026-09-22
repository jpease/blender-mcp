"""Shared strict request records for transactional node-graph patches."""

from typing import Any, Literal

from pydantic import Field

from ._inputs import OpenPayloadModel


class NodeGraphEdit(OpenPayloadModel):
    """One ordered, stable-name node-graph mutation."""

    operation: Literal[
        "ADD_NODE",
        "UPDATE_NODE",
        "SET_INPUT",
        "ADD_LINK",
        "REMOVE_LINK",
        "MOVE_TO_FRAME",
        "REMOVE_NODE",
        "SET_ACTIVE_OUTPUT",
    ]
    node_name: str | None = None
    bl_idname: str | None = None
    new_name: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    socket_identifier: str | None = None
    socket_index: int | None = Field(default=None, ge=0)
    value: Any = None
    from_node: str | None = None
    from_socket_identifier: str | None = None
    from_socket_index: int | None = Field(default=None, ge=0)
    to_node: str | None = None
    to_socket_identifier: str | None = None
    to_socket_index: int | None = Field(default=None, ge=0)
    frame_name: str | None = None
    managed_role: str | None = Field(default=None, min_length=1, max_length=128)
