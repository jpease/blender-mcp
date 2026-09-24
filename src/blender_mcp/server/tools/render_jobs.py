"""Typed tool for renders that run in their own Blender process: CREATE, READ, LIST and DELETE a job."""

from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

from ..app import mcp
from ._dispatch import call_blender

# The add-on issues `secrets.token_hex(6)`; anything else is refused before it reaches a path.
_JOB_ID_PATTERN = r"^[0-9a-f]{12}$"
# The parameters only CREATE takes, with the value that means "not given".
_CREATE_DEFAULTS: dict[str, object] = {
    "scene_name": None,
    "filepath": None,
    "mode": "STILL",
    "view_layer_name": None,
    "frame": None,
    "frame_start": None,
    "frame_end": None,
    "max_animation_frames": 250,
    "max_duration_seconds": None,
    "confirm_render": False,
    "confirm_overwrite": False,
    "confirm_frame_range": False,
    "create_directories": False,
}
_ACTION_PARAMETERS: dict[str, frozenset[str]] = {
    "CREATE": frozenset(_CREATE_DEFAULTS),
    "READ": frozenset({"job_id", "detail", "limit", "offset"}),
    "LIST": frozenset({"limit", "offset"}),
    "DELETE": frozenset({"job_id", "confirm_delete"}),
}
_DEFAULTS: dict[str, object] = {
    **_CREATE_DEFAULTS,
    "job_id": None,
    "confirm_delete": False,
    "detail": False,
    "limit": 20,
    "offset": 0,
}


def _refuse_foreign_parameters(action: str, given: dict[str, object]) -> None:
    """
    Refuse, by name, every parameter set that the action does not take.

    Args:
        action: The requested action.
        given: Every parameter's value as called.

    Raises:
        ToolError: When a parameter another action owns was set.

    """
    foreign = sorted(
        name for name, value in given.items() if value != _DEFAULTS[name] and name not in _ACTION_PARAMETERS[action]
    )
    if foreign:
        raise ToolError(f"{action} does not take {', '.join(foreign)}")


@mcp.tool()
async def manage_render_job(
    ctx: Context,
    action: Literal["CREATE", "READ", "LIST", "DELETE"],
    job_id: Annotated[str | None, Field(pattern=_JOB_ID_PATTERN)] = None,
    scene_name: str | None = None,
    filepath: Annotated[str | None, Field(min_length=1)] = None,
    mode: Literal["STILL", "ANIMATION"] = "STILL",
    view_layer_name: str | None = None,
    frame: int | None = None,
    frame_start: int | None = None,
    frame_end: int | None = None,
    max_animation_frames: Annotated[int, Field(ge=1, le=10_000)] = 250,
    max_duration_seconds: Annotated[float | None, Field(gt=0, le=604_800)] = None,
    confirm_render: bool = False,
    confirm_overwrite: bool = False,
    confirm_frame_range: bool = False,
    create_directories: bool = False,
    confirm_delete: bool = False,
    detail: bool = False,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """
    Run a render in its own background Blender, tracked as a job file: for long or time-bounded renders.

    render_scene blocks the connected Blender until done and cannot stop mid-frame. CREATE
    validates exactly as render_scene does, saves a copy of the open file (unsaved edits
    included, later edits not), starts a separate `blender -b` on it and returns at once with
    job_id, engine and effective_cycles_device. max_duration_seconds is a hard wall-clock limit:
    the process is ended mid-frame and the job reads TIMED_OUT, finished frames kept.

    Poll READ until state is DONE (states: QUEUED, RENDERING, DONE, CANCELLED, TIMED_OUT,
    FAILED); it reports frames_done/frames_total and last_file, plus log_tail when FAILED. See a
    frame with inspect_render_output(output_path=<last_file>). LIST pages jobs newest first.
    DELETE removes the job directory, never a rendered frame. Jobs outlive the session, the
    add-on and Blender itself, and still end at their deadline.

    Args:
        ctx: MCP request context.
        action: CREATE starts a job, READ reports one, LIST pages all, DELETE stops and removes one.
        job_id: The id CREATE returned; READ and DELETE only.
        scene_name: CREATE: scene to render.
        filepath: CREATE: output shaped as for render_scene (STILL "<dir>/sh010.png", ANIMATION
            "<dir>/sh010_"), inside the file roots when set; omit for the scene's own.
        mode: CREATE: STILL or ANIMATION.
        view_layer_name: CREATE: render only this view layer.
        frame: CREATE, STILL: frame to render; omit for the current frame.
        frame_start: CREATE, ANIMATION: first frame, with frame_end; omit both for the scene's range.
        frame_end: CREATE, ANIMATION: last frame, inclusive.
        max_animation_frames: CREATE: refuse an ANIMATION longer than this.
        max_duration_seconds: CREATE: hard wall-clock limit on the whole job, from CREATE.
        confirm_render: CREATE: required.
        confirm_overwrite: CREATE: replace existing output.
        confirm_frame_range: CREATE: allow the untouched 1-250 range.
        create_directories: CREATE: create a missing output directory.
        confirm_delete: DELETE: required to stop a job that is still running.
        detail: READ: add a limit/offset page of written files.
        limit: READ with detail, and LIST: page size.
        offset: Likewise: page start.

    Returns:
        the job record (CREATE, READ), a jobs page (LIST), or deleted/stopped/state/last_file (DELETE)

    """
    given = {
        "job_id": job_id,
        "scene_name": scene_name,
        "filepath": filepath,
        "mode": mode,
        "view_layer_name": view_layer_name,
        "frame": frame,
        "frame_start": frame_start,
        "frame_end": frame_end,
        "max_animation_frames": max_animation_frames,
        "max_duration_seconds": max_duration_seconds,
        "confirm_render": confirm_render,
        "confirm_overwrite": confirm_overwrite,
        "confirm_frame_range": confirm_frame_range,
        "create_directories": create_directories,
        "confirm_delete": confirm_delete,
        "detail": detail,
        "limit": limit,
        "offset": offset,
    }
    _refuse_foreign_parameters(action, given)
    if action == "CREATE":
        if scene_name is None:
            raise ToolError("CREATE requires scene_name")
        if not confirm_render:
            raise ToolError("confirm_render=True is required")
        if mode == "STILL" and (frame_start is not None or frame_end is not None):
            raise ToolError("frame_start/frame_end are only valid for ANIMATION renders; a STILL takes frame")
        if mode == "ANIMATION" and frame is not None:
            raise ToolError("frame is only valid for STILL renders")
        if (frame_start is None) != (frame_end is None):
            raise ToolError("Pass frame_start and frame_end together, or neither to use the scene's range")
        if frame_start is not None and frame_end is not None and frame_end < frame_start:
            raise ToolError("frame_end must be greater than or equal to frame_start")
    elif action in {"READ", "DELETE"} and job_id is None:
        raise ToolError(f"{action} requires job_id")
    params = {"action": action, **{name: value for name, value in given.items() if name in _ACTION_PARAMETERS[action]}}
    return await call_blender("manage_render_job", params)
