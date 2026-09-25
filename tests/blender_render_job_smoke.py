# ruff: file-ignore[module-import-not-at-top-of-file]
"""
Run with Blender 5.1+ to smoke-test `manage_render_job` against a real child Blender.

Three jobs: a two-frame animation that must finish DONE with both files written; a Cycles frame
far too slow for its `max_duration_seconds`, which must end TIMED_OUT with its process gone; and
a job DELETEd mid-render, whose process must be gone and whose finished frame must stay.

The parent runs `--factory-startup`; the child deliberately does not (it loads the user's
Preferences), so this also proves a job renders whatever this machine's startup state is.
"""

import os
import sys
import tempfile
import time

from pathlib import Path

import bpy

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

load_addon("blender_mcp_render_job_smoke")

from blender_mcp_render_job_smoke.handlers.render_jobs import RenderJobHandlersMixin

_TERMINAL = {"DONE", "CANCELLED", "TIMED_OUT", "FAILED"}


def _read(handler: RenderJobHandlersMixin, job_id: str, **kwargs: object) -> dict:
    return handler.manage_render_job("READ", job_id=job_id, **kwargs)


def _wait_for(handler: RenderJobHandlersMixin, job_id: str, done, timeout: float) -> dict:
    """Poll READ until `done(reply)` holds, failing with the last reply and the log tail."""
    deadline = time.monotonic() + timeout
    reply = _read(handler, job_id)
    while not done(reply):
        if time.monotonic() > deadline:
            log = (
                Path(reply["log_path"]).read_text(encoding="utf-8", errors="replace")[-2000:]
                if reply.get("log_path")
                else ""
            )
            raise AssertionError(f"job {job_id} timed out waiting: {reply}\n--- render.log ---\n{log}")
        time.sleep(0.2)
        reply = _read(handler, job_id)
    return reply


def _process_gone(pid: int) -> bool:
    """Whether a pid no longer names a running process (reaping it if it is this process's child)."""
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    else:
        if reaped == 0:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _slow_cycles(scene: bpy.types.Scene) -> None:
    """Make a frame take far longer than any bound this smoke sets."""
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.use_adaptive_sampling = False
    scene.cycles.use_denoising = False
    scene.cycles.samples = 1_000_000
    scene.render.resolution_x = 640
    scene.render.resolution_y = 480


def _check_a_job_renders_every_frame(handler: RenderJobHandlersMixin, scene: bpy.types.Scene, root: Path) -> None:
    dirty_before = bpy.data.is_dirty
    created = handler.manage_render_job(
        "CREATE",
        scene_name=scene.name,
        filepath=str(root / "renders" / "beat_"),
        mode="ANIMATION",
        frame_start=1,
        frame_end=2,
        confirm_render=True,
        create_directories=True,
    )
    job_id = created["job_id"]
    assert created["state"] == "QUEUED", created
    assert created["frames_total"] == 2, created
    assert created["created_directory"] is True, created
    assert created["engine"] == "BLENDER_WORKBENCH", created
    assert created["effective_cycles_device"] is None, created
    assert Path(created["blend_copy"]).is_file(), created
    # A copy leaves the open session exactly as it was: still unsaved, dirty flag untouched.
    assert not bpy.data.filepath, bpy.data.filepath
    assert bpy.data.is_dirty == dirty_before

    done = _wait_for(handler, job_id, lambda reply: reply["state"] in _TERMINAL, timeout=120)
    assert done["state"] == "DONE", done
    written = sorted(path.name for path in (root / "renders").iterdir())
    assert written == ["beat_0001.png", "beat_0002.png"], written
    assert done["frames_done"] == 2, done
    assert done["last_file"] == str(root / "renders" / "beat_0002.png"), done
    assert done["bytes_written"] > 0, done
    detailed = _wait_for(handler, job_id, lambda reply: reply["exit_code"] is not None, timeout=30)
    assert detailed["exit_code"] == 0, detailed
    page = _read(handler, job_id, detail=True, limit=1)
    assert page["files"][0]["frame"] == 1 and page["truncated"] is True and page["next_offset"] == 1, page

    try:
        handler.manage_render_job(
            "CREATE",
            scene_name=scene.name,
            filepath=str(root / "renders" / "beat_"),
            mode="ANIMATION",
            frame_start=1,
            frame_end=2,
            confirm_render=True,
        )
    except ValueError as exc:
        assert "confirm_overwrite" in str(exc), str(exc)
    else:
        raise AssertionError("A job over existing frames was accepted without confirm_overwrite")

    removed = handler.manage_render_job("DELETE", job_id=job_id)
    assert removed["deleted"] is True and removed["stopped"] is False, removed
    assert not Path(created["blend_copy"]).parent.exists(), "the job directory survived DELETE"
    assert (root / "renders" / "beat_0002.png").is_file(), "DELETE removed a rendered frame"


def _check_a_slow_frame_times_out(handler: RenderJobHandlersMixin, scene: bpy.types.Scene, root: Path) -> None:
    _slow_cycles(scene)
    output = root / "slow" / "hero.png"
    started = time.monotonic()
    created = handler.manage_render_job(
        "CREATE",
        scene_name=scene.name,
        filepath=str(output),
        frame=1,
        max_duration_seconds=3,
        confirm_render=True,
        create_directories=True,
    )
    assert created["effective_cycles_device"] == "CPU", created
    ended = _wait_for(handler, created["job_id"], lambda reply: reply["state"] in _TERMINAL, timeout=60)
    elapsed = time.monotonic() - started
    assert ended["state"] == "TIMED_OUT", ended
    assert "max_duration_seconds=3" in ended["cancellation_reason"], ended
    # The child ends itself at the deadline; the add-on's own backup would only act 10 s later.
    assert elapsed < 3 + 8, elapsed
    assert _wait_until_gone(ended["pid"]), f"pid {ended['pid']} outlived its TIMED_OUT job"
    assert not output.exists(), "an abandoned frame left a file behind"
    handler.manage_render_job("DELETE", job_id=created["job_id"])


def _wait_until_gone(pid: int) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _process_gone(pid):
            return True
        time.sleep(0.1)
    return False


def _check_delete_stops_a_running_job(handler: RenderJobHandlersMixin, scene: bpy.types.Scene, root: Path) -> None:
    # Frame 1 renders in a moment and frame 2 not in this smoke's lifetime, so the job is
    # reliably mid-render with one finished frame when DELETE arrives.
    _slow_cycles(scene)
    scene.cycles.samples = 1
    scene.cycles.keyframe_insert("samples", frame=1)
    scene.cycles.samples = 1_000_000
    scene.cycles.keyframe_insert("samples", frame=2)
    created = handler.manage_render_job(
        "CREATE",
        scene_name=scene.name,
        filepath=str(root / "deleted" / "shot_"),
        mode="ANIMATION",
        frame_start=1,
        frame_end=2,
        confirm_render=True,
        create_directories=True,
    )
    job_id = created["job_id"]
    running = _wait_for(
        handler, job_id, lambda reply: reply["current_frame"] == 2 or reply["state"] in _TERMINAL, timeout=60
    )
    assert running["state"] == "RENDERING" and running["frames_done"] == 1, running

    listed = handler.manage_render_job("LIST")
    assert listed["jobs"][0]["job_id"] == job_id, listed

    try:
        handler.manage_render_job("DELETE", job_id=job_id)
    except ValueError as exc:
        assert "confirm_delete=true" in str(exc), str(exc)
    else:
        raise AssertionError("A running job was deleted without confirm_delete")

    removed = handler.manage_render_job("DELETE", job_id=job_id, confirm_delete=True)
    assert removed["stopped"] is True and removed["state"] == "CANCELLED", removed
    assert removed["frames_done"] == 1, removed
    assert _wait_until_gone(running["pid"]), f"pid {running['pid']} survived DELETE"
    assert Path(removed["last_file"]).is_file(), "DELETE removed the frame the job had finished"
    assert not Path(created["blend_copy"]).parent.exists(), "the job directory survived DELETE"
    try:
        _read(handler, job_id)
    except ValueError as exc:
        assert "not found" in str(exc), str(exc)
    else:
        raise AssertionError("A deleted job could still be read")


def _check_a_render_blender_refuses_reads_failed(
    handler: RenderJobHandlersMixin, scene: bpy.types.Scene, root: Path
) -> None:
    """Check a child whose render raises records FAILED itself, and READ carries Blender's own words."""
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.camera = None
    created = handler.manage_render_job(
        "CREATE", scene_name=scene.name, filepath=str(root / "failed.png"), frame=1, confirm_render=True
    )
    failed = _wait_for(handler, created["job_id"], lambda reply: reply["exit_code"] is not None, timeout=60)
    assert failed["state"] == "FAILED" and failed["exit_code"] == 1, failed
    assert "camera" in failed["error"].lower(), failed
    assert failed["log_tail"], failed
    handler.manage_render_job("DELETE", job_id=created["job_id"])


def main() -> None:
    """Render, time out and delete real jobs; print RENDER_JOB_OK."""
    handler = RenderJobHandlersMixin()
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = 64
    scene.render.resolution_y = 48
    scene.render.image_settings.file_format = "PNG"
    previous_roots = os.environ.get("BLENDERMCP_OUTPUT_ROOTS")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        # Jobs, outputs and the file-roots check all land in this one throwaway directory.
        os.environ["BLENDERMCP_OUTPUT_ROOTS"] = str(root)
        try:
            _check_a_job_renders_every_frame(handler, scene, root)
            _check_a_slow_frame_times_out(handler, scene, root)
            _check_delete_stops_a_running_job(handler, scene, root)
            _check_a_render_blender_refuses_reads_failed(handler, scene, root)
        finally:
            if previous_roots is None:
                os.environ.pop("BLENDERMCP_OUTPUT_ROOTS", None)
            else:
                os.environ["BLENDERMCP_OUTPUT_ROOTS"] = previous_roots
    print("RENDER_JOB_OK")


if __name__ == "__main__":
    main()
