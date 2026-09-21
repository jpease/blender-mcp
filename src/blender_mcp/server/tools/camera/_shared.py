"""Shared validation models and cross-file types for the camera tool package."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

TrackAxis = Literal[
    "TRACK_X",
    "TRACK_Y",
    "TRACK_Z",
    "TRACK_NEGATIVE_X",
    "TRACK_NEGATIVE_Y",
    "TRACK_NEGATIVE_Z",
]
UpAxis = Literal["UP_X", "UP_Y", "UP_Z"]
LockAxis = Literal["LOCK_X", "LOCK_Y", "LOCK_Z"]
ConstraintSpace = Literal["WORLD", "CUSTOM", "POSE", "LOCAL_WITH_PARENT", "LOCAL"]
FollowForwardAxis = Literal[
    "FORWARD_X",
    "FORWARD_Y",
    "FORWARD_Z",
    "TRACK_NEGATIVE_X",
    "TRACK_NEGATIVE_Y",
    "TRACK_NEGATIVE_Z",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def _dump(model: BaseModel | None) -> dict | None:
    return model.model_dump(exclude_none=True, exclude_unset=True) if model is not None else None


def _tool_params(values: dict) -> dict:
    """Remove FastMCP's context-only argument from a local tool payload."""
    return {key: value for key, value in values.items() if key != "ctx"}
