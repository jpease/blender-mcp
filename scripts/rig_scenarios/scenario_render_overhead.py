r"""
Measure what the server-orchestrated ANIMATION path costs per frame, against a live Blender.

`render_scene(mode="ANIMATION")` has two implementations. The default
(`orchestrate_animation=True`) drives the animation from the MCP server, one
`render_scene` command per frame, which is what buys per-frame progress notifications and
a cancellation that lands between frames (proven by `scenario_render_progress.py`). The
escape hatch (`orchestrate_animation=False`) hands the whole range to Blender in a single
command, as the tool originally did.

That left an open question: nobody had timed the two against a
production-sized animation, and the orchestrated path is the default. A ten-second shot at
24 fps is 240 round trips; if each one costs a tenth of a second, the default silently adds
half a minute to every animation render.

This scenario answers it by rendering the same frame range both ways through a real MCP
client and differencing the wall clock. Frames are deliberately trivial (64px Workbench at
25%), because the quantity being measured is the per-frame *protocol* cost, not Blender's
rasterizer: the cheaper the frame, the larger the overhead's share of the total and the
more sensitive the measurement. Each path is measured twice, alternating, and the faster of
the two samples is used - a scheduler hiccup can only inflate a sample, never deflate it.

The assertion is a ceiling on per-frame overhead, not an equality: this is a timing
measurement on a shared desktop, and the useful failure is "the round trip now costs enough
to matter", not "it moved by a millisecond".
"""

import importlib.util
import json
import os
import time

from pathlib import Path
from types import ModuleType

import anyio

from mcp import ClientSession
from mcp.client.stdio import stdio_client


def _sibling(file_name: str, module_name: str) -> ModuleType:
    """
    Load a sibling scenario-directory module by path.

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

REQUIRED_COMMANDS = ("render_scene", "configure_render_settings")

# Ten seconds at 24 fps: the shot shape the open question named. Every frame is a real
# round trip on the orchestrated path, so this is 240 of them.
_FRAMES = 240
# A short run first, discarded: the first render of a session pays for Blender's render
# engine initialization and the server's first-call connection setup, which belong to
# neither path's steady state.
_WARMUP_FRAMES = 4
_RENDER_RESOLUTION = 64
_RENDER_PERCENTAGE = 25
_SAMPLES = 2
# Per-frame overhead this large would mean a 240-frame shot pays a full minute for the
# progress stream, which would make the default indefensible. The measured figure is two
# orders of magnitude below it; this ceiling exists to catch a regression, not to certify
# the current number.
_MAX_PER_FRAME_OVERHEAD_SECONDS = 0.25
_CALL_TIMEOUT_SECONDS = 1800.0


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
    client.payload(result)


async def _timed_run(
    session: ClientSession, scene: str, directory: Path, orchestrated: bool, expected_frames: int
) -> float:
    """
    Render the configured range one way and return how long the call took.

    Args:
        session: The live MCP client session.
        scene: Scene name to render.
        directory: Directory to write this run's frames into; created if absent.
        orchestrated: True for the server-driven per-frame path, False for the single call.
        expected_frames: How many files this run must leave behind.

    Returns:
        float: Wall-clock seconds the `tools/call` took, client side - which is what a host
        actually waits, protocol overhead included.

    Raises:
        RuntimeError: If the run did not write exactly `expected_frames` files.

    """
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("*.png"):
        stale.unlink()
    arguments = {
        "scene_name": scene,
        "filepath": f"{directory}{os.sep}f_",
        "mode": "ANIMATION",
        "confirm_render": True,
        "confirm_overwrite": True,
        "verify_outputs": True,
        # The frame's own passes are not what this scenario measures, and requiring them
        # would turn a Workbench render into a failure.
        "verify_passes": False,
        "orchestrate_animation": orchestrated,
    }
    started = time.perf_counter()
    with anyio.fail_after(_CALL_TIMEOUT_SECONDS):
        result = await session.call_tool("render_scene", arguments)
    elapsed = time.perf_counter() - started
    client.payload(result)
    written = len(list(directory.glob("*.png")))
    if written != expected_frames:
        path = "orchestrated" if orchestrated else "single-call"
        raise RuntimeError(f"the {path} run wrote {written} frames, expected {expected_frames}")
    return elapsed


async def _measure(session: ClientSession, scene: str, work_dir: Path) -> tuple[float, float]:
    """
    Time both paths over the same range, alternating, and return the best sample of each.

    Alternating matters: a monotonic background load would otherwise land entirely on
    whichever path ran second and be read as that path's cost.

    Args:
        session: The live MCP client session.
        scene: Scene name to render.
        work_dir: The rig's work directory; each run gets its own subdirectory.

    Returns:
        tuple[float, float]: (best orchestrated seconds, best single-call seconds).

    """
    orchestrated: list[float] = []
    single: list[float] = []
    for sample in range(_SAMPLES):
        orchestrated.append(
            await _timed_run(
                session, scene, work_dir / f"orchestrated_{sample}", orchestrated=True, expected_frames=_FRAMES
            )
        )
        single.append(
            await _timed_run(session, scene, work_dir / f"single_{sample}", orchestrated=False, expected_frames=_FRAMES)
        )
        print(
            f"RIG: sample {sample + 1}/{_SAMPLES} - orchestrated {orchestrated[-1]:.3f}s, "
            f"single-call {single[-1]:.3f}s",
            flush=True,
        )
    return min(orchestrated), min(single)


async def _drive(port: int, scene: str, work_dir: Path) -> None:
    """
    Warm the render path up, measure both animation paths, and report the overhead.

    Args:
        port: Port the rig's Blender addon is listening on.
        scene: Scene name to render.
        work_dir: The rig's work directory.

    Raises:
        RuntimeError: If the server does not advertise `render_scene`, or the measured
            per-frame overhead exceeds the ceiling.

    """
    async with (
        stdio_client(client.server_parameters(port, "rendering")) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        with anyio.fail_after(client.STARTUP_TIMEOUT_SECONDS):
            await session.initialize()
        tools = {tool.name for tool in (await session.list_tools()).tools}
        if "render_scene" not in tools:
            raise RuntimeError(
                f"the server advertises {len(tools)} tools but not render_scene; "
                "check BLENDER_MCP_TOOLSETS in _mcp_client.server_parameters"
            )

        await _configure(session, scene, 1, _WARMUP_FRAMES)
        warmup = {"expected_frames": _WARMUP_FRAMES}
        await _timed_run(session, scene, work_dir / "warmup_orchestrated", orchestrated=True, **warmup)
        await _timed_run(session, scene, work_dir / "warmup_single", orchestrated=False, **warmup)

        await _configure(session, scene, 1, _FRAMES)
        print(
            f"RIG: measuring {_FRAMES} frames at {_RENDER_RESOLUTION}px/{_RENDER_PERCENTAGE}% "
            f"({_SAMPLES} samples per path)",
            flush=True,
        )
        best_orchestrated, best_single = await _measure(session, scene, work_dir)

        overhead = best_orchestrated - best_single
        per_frame = overhead / _FRAMES
        single_per_frame = best_single / _FRAMES
        print(
            f"RIG: orchestrated {best_orchestrated:.3f}s vs single-call {best_single:.3f}s over {_FRAMES} frames "
            f"= {overhead:+.3f}s total, {per_frame * 1000:+.2f} ms/frame",
            flush=True,
        )
        print(
            f"RIG: the single-call path renders a frame in {single_per_frame * 1000:.2f} ms, so the round trip is "
            f"{100 * per_frame / single_per_frame:+.1f}% of a 64px Workbench frame and "
            f"{100 * per_frame / (1 / 24):+.2f}% of a 24 fps real-time frame budget",
            flush=True,
        )
        if per_frame > _MAX_PER_FRAME_OVERHEAD_SECONDS:
            raise RuntimeError(
                f"the orchestrated path costs {per_frame:.3f}s per frame over the single call, above the "
                f"{_MAX_PER_FRAME_OVERHEAD_SECONDS}s ceiling: a {_FRAMES}-frame shot pays {overhead:.1f}s for its "
                "progress stream"
            )


def run(rig: Rig) -> None:
    """
    Time both ANIMATION paths through a real MCP client against the rig's Blender.

    Args:
        rig: The live rig, already serving on its socket.

    Raises:
        SystemExit: If the server binary is missing, or the addon does not advertise the
            commands both paths need.

    """
    if not client.SERVER_BINARY.is_file():
        raise SystemExit(f"no server console script at {client.SERVER_BINARY}; install the project into .venv first")

    info = rig.send("get_addon_info")
    if info["status"] != "success":
        raise SystemExit(f"get_addon_info failed: {info}")
    missing = [name for name in REQUIRED_COMMANDS if name not in info["result"]["capabilities"]]
    if missing:
        raise SystemExit(f"the addon does not advertise {missing}")

    listing = rig.send("list_scene_objects")
    if listing["status"] != "success":
        raise SystemExit(f"list_scene_objects failed: {listing}")
    scene = listing["result"]["name"]

    port = json.loads((rig.work_dir / barrier.RECEIPT_FILE_NAME).read_text(encoding="utf-8"))["port"]
    try:
        anyio.run(_drive, port, scene, rig.work_dir)
    except BaseExceptionGroup as group:
        raise client.unwrapped(group) from None

    pong = rig.send("ping")
    if pong["status"] != "success":
        raise SystemExit(f"the addon stopped serving after the measured renders: {pong}")
    print("RIG: the addon still answers its own socket after both measured renders", flush=True)
