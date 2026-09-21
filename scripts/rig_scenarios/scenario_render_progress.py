r"""
Prove the orchestrated ANIMATION render talks progress and cancellation to a real MCP client.

`render_scene(mode="ANIMATION")` reports one progress notification per frame, and a real
client cancellation lands between frames rather than being ignored.

This is the first path in this repository that speaks MCP as a *client*. Everything else
either drives the addon's own socket directly (`scripts/blender_rig.py` and the other
scenarios beside this one) or calls the tool coroutine in-process with a fake connection
(`tests/test_rendering_tools.py`). Neither can see what this one checks: that the
`progressToken` plumbing actually reaches `Context.report_progress`, and that a client's
`notifications/cancelled` stops the orchestration loop rather than being ignored while
Blender renders on.

The whole stack is real. The rig launches a GUI Blender running the staged addon on a
private port; this scenario spawns the shipped `blender-mcp` console script as a child
process over stdio with `BLENDER_PORT` pointing at that Blender, and drives it with the
SDK's own `ClientSession`. So the bytes go: client -> stdio -> FastMCP -> the render tool ->
addon socket -> Blender, and back.

Two facts about mcp 1.30.0 shape this scenario; both were verified by running the SDK, not
read off its docs:

- **Progress is opt-in from the client.** `Context.report_progress` returns silently unless
  the request carried a `progressToken` (`mcp/server/fastmcp/server.py`), and the token is
  injected only when the client passes `progress_callback` to `call_tool`
  (`mcp/shared/session.py`). A harness that forgot the callback would see zero
  notifications and mistake it for a broken feature.
- **Cancellation is not automatic.** Nothing in the SDK ever constructs a
  `CancelledNotification`; cancelling the client-side `await` alone leaves the server's
  handler running to completion. The client has to send the notification itself, addressed
  to the in-flight request's id.

Run it:

    just rig scripts/rig_scenarios/scenario_render_progress.py

or, as the permanent gate that skips itself without a live Blender:

    just gate
"""

import importlib.util
import json
import os

from pathlib import Path
from types import ModuleType

import anyio

from mcp import ClientSession, types
from mcp.client.stdio import stdio_client


def _sibling(file_name: str, module_name: str) -> ModuleType:
    """
    Load a sibling scenario-directory module by path.

    Scenarios are loaded by absolute path, so this directory is not on `sys.path` and a
    plain import does not resolve. Importing rather than copying keeps the readiness
    receipt's name, and the MCP host helpers, defined in one place each.

    Args:
        file_name: File name beside this scenario.
        module_name: Module name to register the loaded module under.

    Returns:
        module: The loaded module.

    Raises:
        SystemExit: If the file is missing or not loadable.

    """
    path = Path(__file__).resolve().parent / file_name
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"{path} is not loadable - this scenario was copied out of the repository")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


barrier = _sibling("scenario_file_swap_barrier.py", "scenario_file_swap_barrier_shared")
client = _sibling("_mcp_client.py", "rig_scenario_mcp_client")

Rig = client.Rig
_SERVER_BINARY = client.SERVER_BINARY
_STARTUP_TIMEOUT_SECONDS = client.STARTUP_TIMEOUT_SECONDS
_payload = client.payload
_unwrapped = client.unwrapped

REQUIRED_COMMANDS = ("plan_render_animation", "render_scene", "configure_render_settings")

# Three frames is enough for "one notification per frame", and starting at 7 rather than 1
# makes each message's scene frame differ from its ordinal, so a synthesized message would
# not match: the tool spells "Rendered frame 7 (1/3)".
_PROGRESS_FIRST_FRAME = 7
_PROGRESS_FRAMES = 3
# The cancellation run needs enough frames left after the first one that stopping is
# visible. Frames are 64x64 Workbench renders, so this is still seconds, not minutes.
_CANCEL_FRAMES = 24
_RENDER_RESOLUTION = 64
_RENDER_PERCENTAGE = 25
# Time to let a cancelled run's already-dispatched frame finish writing before counting
# files: the frame in flight when the cancel arrives completes inside Blender (the socket
# round trip runs on a worker thread and is not interruptible), and only later frames are
# skipped.
_SETTLE_SECONDS = 3.0
_STARTUP_TIMEOUT_SECONDS = 60.0
_CALL_TIMEOUT_SECONDS = 300.0


async def _configure(session: ClientSession, scene: str, first: int, last: int) -> None:
    """
    Set a tiny render configuration and an explicit frame range.

    An explicit `frame_start`/`frame_end` is not cosmetic: it sets the addon's
    `blender_mcp_frame_range_authored` marker, without which an animation over Blender's
    untouched 1-250 default is refused outright.

    Args:
        session: The live MCP client session.
        scene: Scene name to configure.
        first: First frame to render.
        last: Last frame to render.

    """
    result = await session.call_tool(
        "configure_render_settings",
        {
            "scene_name": scene,
            "patch": {
                "engine": "BLENDER_WORKBENCH",
                "resolution_x": _RENDER_RESOLUTION,
                "resolution_y": _RENDER_RESOLUTION,
                "resolution_percentage": _RENDER_PERCENTAGE,
                "image_format": "PNG",
                "frame_start": first,
                "frame_end": last,
                "frame_step": 1,
            },
        },
    )
    _payload(result)
    print(f"RIG: configured frames {first}-{last} at {_RENDER_RESOLUTION}px/{_RENDER_PERCENTAGE}%", flush=True)


def _animation_arguments(directory: Path) -> dict:
    """
    Build the orchestrated-ANIMATION arguments both runs share.

    Args:
        directory: Existing directory the frames are written into.

    Returns:
        dict: `render_scene` arguments.

    """
    return {
        "filepath": f"{directory}{os.sep}beat_",
        "mode": "ANIMATION",
        "confirm_render": True,
        "confirm_overwrite": True,
        "verify_outputs": True,
        # The frame's own passes are not what this scenario measures, and requiring them
        # would turn a Workbench render into a failure.
        "verify_passes": False,
        "orchestrate_animation": True,
        "detail": True,
    }


async def _check_progress_arrives_once_per_frame(session: ClientSession, scene: str, work_dir: Path) -> None:
    """
    Prove a client that asks for progress gets exactly one notification per rendered frame.

    Args:
        session: The live MCP client session.
        scene: Scene name to render.
        work_dir: The rig's work directory; frames go in a subdirectory of it.

    Raises:
        AssertionError: If the notifications do not match the frames the tool rendered.

    """
    last = _PROGRESS_FIRST_FRAME + _PROGRESS_FRAMES - 1
    await _configure(session, scene, _PROGRESS_FIRST_FRAME, last)

    directory = work_dir / "renders_progress"
    directory.mkdir(parents=True, exist_ok=True)
    seen: list[tuple[int, int | None, str | None]] = []

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        # `ProgressFnT` is an async protocol, so this has to be a coroutine even though
        # recording a notification needs no awaiting.
        await anyio.lowlevel.checkpoint()
        seen.append((int(progress), None if total is None else int(total), message))

    with anyio.fail_after(_CALL_TIMEOUT_SECONDS):
        result = await session.call_tool(
            "render_scene",
            {"scene_name": scene, **_animation_arguments(directory)},
            progress_callback=on_progress,
        )
    envelope = _payload(result)

    print(f"RIG: progress notifications {seen}", flush=True)
    assert len(seen) == _PROGRESS_FRAMES, f"expected one notification per frame, got {seen}"
    expected = [
        (
            index + 1,
            _PROGRESS_FRAMES,
            f"Rendered frame {_PROGRESS_FIRST_FRAME + index} ({index + 1}/{_PROGRESS_FRAMES})",
        )
        for index in range(_PROGRESS_FRAMES)
    ]
    assert seen == expected, f"{seen} does not match the frames the tool rendered: {expected}"

    written = sorted(path.name for path in directory.glob("*.png"))
    assert len(written) == _PROGRESS_FRAMES, f"{len(written)} frames on disk, expected {_PROGRESS_FRAMES}: {written}"
    data = envelope["data"]
    assert data["frame_count"] == _PROGRESS_FRAMES, data
    assert data["status"] == "COMPLETED", data
    assert data["cancelled"] is False, data
    # The notification count and the summary must agree; a progress stream that outran or
    # lagged the frames the tool actually aggregated would be worse than none.
    assert len(seen) == data["frame_count"], (seen, data["frame_count"])
    print(f"RIG: rendered {written} and reported frame_count={data['frame_count']}", flush=True)


async def _check_cancellation_lands_between_frames(session: ClientSession, scene: str, work_dir: Path) -> None:
    """
    Prove a client cancellation stops the orchestration loop instead of being ignored.

    The request id is read off the session's own counter immediately before the call: the
    SDK uses the id it is about to assign as the `progressToken` too, but hands the callback
    only `(progress, total, message)`, so there is no public way to learn it. Nothing else
    is in flight on this session, so the next id is deterministic.

    Args:
        session: The live MCP client session.
        scene: Scene name to render.
        work_dir: The rig's work directory; frames go in a subdirectory of it.

    Raises:
        AssertionError: If every frame rendered anyway, or nothing rendered at all.

    """
    await _configure(session, scene, 1, _CANCEL_FRAMES)

    directory = work_dir / "renders_cancel"
    directory.mkdir(parents=True, exist_ok=True)
    request_id = session._request_id  # pyright: ignore[reportPrivateUsage]
    first_frame_done = anyio.Event()
    seen: list[int] = []
    outcome: dict[str, str] = {}

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        await anyio.lowlevel.checkpoint()
        print(f"RIG: progress {int(progress)}/{total} {message!r}", flush=True)
        seen.append(int(progress))
        first_frame_done.set()

    async def call() -> None:
        try:
            result = await session.call_tool(
                "render_scene",
                {"scene_name": scene, **_animation_arguments(directory)},
                progress_callback=on_progress,
            )
            outcome["result"] = f"returned isError={result.isError}"
        except Exception as error:  # the SDK raises McpError("Request cancelled") here
            outcome["result"] = f"{type(error).__name__}: {error}"

    with anyio.fail_after(_CALL_TIMEOUT_SECONDS):
        async with anyio.create_task_group() as group:
            group.start_soon(call)
            await first_frame_done.wait()
            await session.send_notification(
                types.ClientNotification(
                    types.CancelledNotification(
                        method="notifications/cancelled",
                        params=types.CancelledNotificationParams(
                            requestId=request_id, reason="rig cancelled the animation after its first frame"
                        ),
                    )
                )
            )

    assert "cancelled" in outcome["result"].lower(), (
        f"the client's in-flight call should end as a cancellation, not as {outcome['result']!r}"
    )

    # The frame in flight when the cancel arrived finishes; later frames must never start.
    at_cancel = len(sorted(directory.glob("*.png")))
    await anyio.sleep(_SETTLE_SECONDS)
    written = sorted(path.name for path in directory.glob("*.png"))
    print(
        f"RIG: cancelled after {len(seen)} notification(s): {outcome['result']}; "
        f"{at_cancel} frames on disk at cancel, {len(written)} after {_SETTLE_SECONDS}s",
        flush=True,
    )
    assert written, "no frame rendered at all, so nothing proves the cancel arrived mid-run"
    assert len(written) < _CANCEL_FRAMES, (
        f"all {_CANCEL_FRAMES} frames rendered, so the cancellation never stopped the loop: {written}"
    )
    assert len(written) <= at_cancel + 1, (
        f"frames kept being rendered after the cancellation: {at_cancel} at cancel, {len(written)} later"
    )


async def _check_the_session_survives_a_cancellation(session: ClientSession, scene: str) -> None:
    """
    Prove cancelling one tool call leaves the session and the addon connection usable.

    Args:
        session: The live MCP client session.
        scene: Scene name to inspect.

    Raises:
        AssertionError: If the next call does not succeed.

    """
    result = await session.call_tool("list_scene_objects", {"limit": 1})
    envelope = _payload(result)
    assert envelope["data"]["name"] == scene, envelope
    print(f"RIG: the session still serves calls after the cancellation (scene {scene!r})", flush=True)


async def _drive(port: int, scene: str, work_dir: Path) -> None:
    """
    Run the whole client-side sequence against a freshly spawned server process.

    Args:
        port: Port the rig's Blender addon is listening on.
        scene: Scene name to render.
        work_dir: The rig's work directory.

    Raises:
        RuntimeError: If the server does not advertise `render_scene`.

    """
    async with (
        stdio_client(client.server_parameters(port, "rendering")) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        with anyio.fail_after(_STARTUP_TIMEOUT_SECONDS):
            await session.initialize()
        tools = {tool.name for tool in (await session.list_tools()).tools}
        if "render_scene" not in tools:
            raise RuntimeError(
                f"the server advertises {len(tools)} tools but not render_scene; "
                "check BLENDER_MCP_TOOLSETS in _server_parameters"
            )
        print(f"RIG: {_SERVER_BINARY.name} serving {len(tools)} tools over stdio, BLENDER_PORT={port}", flush=True)

        await _check_progress_arrives_once_per_frame(session, scene, work_dir)
        await _check_cancellation_lands_between_frames(session, scene, work_dir)
        await _check_the_session_survives_a_cancellation(session, scene)


def run(rig: Rig) -> None:
    """
    Check the addon side through the rig, then hand the rest to a real MCP client.

    Args:
        rig: The live rig, already serving on its socket.

    Raises:
        SystemExit: If the server binary is missing, or the addon does not advertise the
            commands the orchestrated path needs.

    """
    if not _SERVER_BINARY.is_file():
        raise SystemExit(f"no server console script at {_SERVER_BINARY}; install the project into .venv first")

    info = rig.send("get_addon_info")
    if info["status"] != "success":
        raise SystemExit(f"get_addon_info failed: {info}")
    capabilities = info["result"]["capabilities"]
    missing = [name for name in REQUIRED_COMMANDS if name not in capabilities]
    if missing:
        raise SystemExit(f"the addon does not advertise {missing}")

    listing = rig.send("list_scene_objects")
    if listing["status"] != "success":
        raise SystemExit(f"list_scene_objects failed: {listing}")
    scene = listing["result"]["name"]

    port = json.loads((rig.work_dir / barrier.RECEIPT_FILE_NAME).read_text(encoding="utf-8"))["port"]
    # The rig's own socket client and the server child both speak to this one addon; they are
    # never used concurrently, so their commands cannot interleave on the stream.
    try:
        anyio.run(_drive, port, scene, rig.work_dir)
    except BaseExceptionGroup as group:
        raise _unwrapped(group) from None

    pong = rig.send("ping")
    if pong["status"] != "success":
        raise SystemExit(f"the addon stopped serving after the cancelled render: {pong}")
    print("RIG: the addon still answers its own socket after the cancelled render", flush=True)
