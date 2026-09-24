"""
`manage_render_job`: request validation, job-id containment, liveness reconciliation, DELETE safety.

The add-on side runs against a fake `bpy` and a scripted child process; the real child - a
`blender -b` rendering, timing out and being deleted - is `tests/blender_render_job_smoke.py`.
"""

import asyncio
import importlib
import os
import time
import types

from pathlib import Path

import pytest

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError
from test_mutation_transaction import _load_addon
from test_rendering_tools import _fake_scene

from blender_mcp.server.tools import render_jobs as render_job_tools

_PID = 4242


class _FakeChild:
    """A child process whose exit the test scripts."""

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = _PID
        self.returncode = None
        self.terminated = False
        self.on_poll = None

    def poll(self):
        if self.returncode is None and self.on_poll is not None:
            self.returncode = self.on_poll()
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture(name="jobs")
def _jobs(monkeypatch, tmp_path):
    """Build an add-on with one scene, output roots under tmp_path, and every child scripted."""
    addon, fake_bpy = _load_addon(monkeypatch, data={"scenes": {}, "images": []})
    rendering = importlib.import_module(f"{addon.__name__}.handlers.rendering")
    module = importlib.import_module(f"{addon.__name__}.handlers.render_jobs")
    fake_bpy.path = types.SimpleNamespace(abspath=lambda path: path)
    scene = _fake_scene(rendering)
    scene.frame_current = 7
    scene.render.frame_path = lambda frame: f"{scene.render.filepath}{frame:04d}.png"
    fake_bpy.data.scenes["Scene"] = scene
    saves = []

    def save_as_mainfile(**kwargs):
        saves.append(kwargs)
        Path(kwargs["filepath"]).write_bytes(b"BLENDER")
        return {"FINISHED"}

    fake_bpy.ops.wm = types.SimpleNamespace(save_as_mainfile=save_as_mainfile)
    fake_bpy.app.binary_path = "/opt/blender/blender"
    timers = []
    fake_bpy.app.timers = types.SimpleNamespace(
        is_registered=lambda function: any(function is registered for registered, _ in timers),
        register=lambda function, **kwargs: timers.append((function, kwargs)),
    )
    children = []

    def popen(args, **kwargs):
        child = _FakeChild(args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    root = tmp_path / "roots"
    root.mkdir()
    monkeypatch.setenv("BLENDERMCP_OUTPUT_ROOTS", str(root))
    monkeypatch.delenv("BLENDERMCP_FILE_ROOTS", raising=False)
    return types.SimpleNamespace(
        module=module,
        handler=module.RenderJobHandlersMixin(),
        root=root.resolve(),
        saves=saves,
        children=children,
        timers=timers,
    )


def _create(jobs, **overrides):
    request = {
        "scene_name": "Scene",
        "filepath": str(jobs.root / "renders" / "shot_"),
        "mode": "ANIMATION",
        "frame_start": 1,
        "frame_end": 3,
        "confirm_render": True,
        "create_directories": True,
        **overrides,
    }
    return jobs.handler.manage_render_job("CREATE", **request)


def _record(jobs, job_id):
    return jobs.module.job_file.read_job(str(jobs.root / "blender_mcp_render_jobs" / job_id / "job.json"))


def _update(jobs, job_id, **fields):
    path = str(jobs.root / "blender_mcp_render_jobs" / job_id / "job.json")
    return jobs.module.job_file.update_job(path, **fields)


def _job_directories(jobs):
    base = jobs.root / "blender_mcp_render_jobs"
    return sorted(path.name for path in base.iterdir()) if base.exists() else []


def _unowned_job(jobs, job_id, **fields):
    """Write a job some earlier add-on session started, which this one holds no handle to."""
    directory = jobs.root / "blender_mcp_render_jobs" / job_id
    directory.mkdir(parents=True)
    now = time.time()
    record = {
        "job_id": job_id,
        "state": "RENDERING",
        "scene_name": "Scene",
        "mode": "STILL",
        "filepath": str(jobs.root / "hero.png"),
        "frames_total": 1,
        "frames_done": 0,
        "created_at": now - 120,
        "heartbeat_at": now,
        "pid": 31337,
        "log_path": str(directory / "render.log"),
        **fields,
    }
    jobs.module.job_file.write_job(str(directory / "job.json"), record)
    return directory


def test_create_renders_a_saved_copy_in_a_blender_that_keeps_user_preferences(jobs) -> None:
    """The child must not reset Preferences (GPU devices) and must not run the file's own scripts."""
    created = _create(jobs, max_duration_seconds=60)

    assert created["state"] == "QUEUED"
    assert (created["frame_start"], created["frame_end"], created["frames_total"]) == (1, 3, 3)
    assert created["deadline_at"] == pytest.approx(created["created_at"] + 60)
    assert created["created_directory"] is True
    assert (jobs.root / "renders").is_dir()
    assert jobs.saves == [{"filepath": created["blend_copy"], "copy": True, "relative_remap": True, "compress": False}]
    child = jobs.children[0]
    assert "--factory-startup" not in child.args
    assert "--disable-autoexec" in child.args
    assert created["blend_copy"] in child.args
    assert child.args[-2:] == ["--", str(Path(created["blend_copy"]).with_name("job.json"))]
    assert child.kwargs["start_new_session"] is True
    assert _record(jobs, created["job_id"])["pid"] == _PID
    # A backup watchdog outlives file loads, as the job does.
    assert [kwargs["persistent"] for _, kwargs in jobs.timers] == [True]


@pytest.mark.parametrize(
    ("overrides", "refusal"),
    [
        ({"confirm_render": False}, "confirm_render=True is required"),
        ({"frame_end": None}, "frame_start and frame_end together"),
        ({"frame_start": 5, "frame_end": 2}, "frame_end must be greater than or equal to frame_start"),
        ({"frame": 4}, "frame is only valid for STILL renders"),
        ({"mode": "STILL", "filepath": None}, "frame_start/frame_end are only valid for ANIMATION"),
        ({"frame_start": 1, "frame_end": 20_000, "max_animation_frames": 10}, "exceeding max_animation_frames=10"),
        ({"max_duration_seconds": 0}, "max_duration_seconds must be a positive finite number"),
        ({"filepath": "/elsewhere/shot_"}, "outside the allowed file roots"),
        ({"filepath": "JOBS/shot_"}, "inside the render jobs directory"),
    ],
)
def test_create_refuses_a_bad_request_before_saving_or_starting_anything(jobs, overrides, refusal) -> None:
    if overrides.get("filepath") == "JOBS/shot_":
        (jobs.root / "blender_mcp_render_jobs").mkdir()
        overrides = {**overrides, "filepath": str(jobs.root / "blender_mcp_render_jobs" / "shot_")}

    with pytest.raises(ValueError, match=refusal):
        _create(jobs, **overrides)

    assert _job_directories(jobs) == []
    assert jobs.saves == []
    assert jobs.children == []


def test_create_refuses_to_render_over_existing_frames_unless_confirmed(jobs) -> None:
    """An in-process render finds this partway through; a job must find it before it starts."""
    (jobs.root / "renders").mkdir()
    (jobs.root / "renders" / "shot_0002.png").write_bytes(b"earlier take")

    with pytest.raises(ValueError, match="already exists for frame 2"):
        _create(jobs)
    assert jobs.children == []

    assert _create(jobs, confirm_overwrite=True)["state"] == "QUEUED"


def test_a_still_defaults_to_the_current_frame_and_refuses_an_existing_file(jobs) -> None:
    output = jobs.root / "hero.png"
    request = {"mode": "STILL", "frame_start": None, "frame_end": None, "filepath": str(output)}

    created = _create(jobs, **request)
    assert (created["frame_start"], created["frame_end"]) == (7, 7)

    output.write_bytes(b"rendered")
    with pytest.raises(ValueError, match="Output file already exists"):
        _create(jobs, **request)


@pytest.mark.parametrize("failure", ["save", "spawn"])
def test_a_job_that_cannot_start_leaves_no_job_directory(jobs, monkeypatch, failure) -> None:
    fake_bpy = jobs.module.bpy
    if failure == "save":
        monkeypatch.setattr(fake_bpy.ops.wm, "save_as_mainfile", lambda **_kwargs: {"CANCELLED"})
    else:

        def refuse(*_args, **_kwargs):
            raise FileNotFoundError(2, "No such file or directory")

        monkeypatch.setattr(jobs.module.subprocess, "Popen", refuse)

    with pytest.raises(RuntimeError, match="manage_render_job"):
        _create(jobs)

    assert _job_directories(jobs) == []


@pytest.mark.parametrize("job_id", ["../../etc", "..", "ABCDEF012345", "0123456789ab/..", "0123456789a", ""])
@pytest.mark.parametrize("action", ["READ", "DELETE"])
def test_a_job_id_create_could_not_have_issued_is_refused_before_any_path_is_built(jobs, action, job_id) -> None:
    with pytest.raises(ValueError, match="12-character lowercase hex id"):
        jobs.handler.manage_render_job(action, job_id=job_id, confirm_delete=action == "DELETE")


def test_each_action_refuses_parameters_another_action_owns(jobs) -> None:
    with pytest.raises(ValueError, match="READ does not take filepath, scene_name"):
        jobs.handler.manage_render_job("READ", job_id="0123456789ab", scene_name="Scene", filepath="/x.png")
    with pytest.raises(ValueError, match="CREATE does not take job_id"):
        _create(jobs, job_id="0123456789ab")
    with pytest.raises(ValueError, match="LIST does not take confirm_delete"):
        jobs.handler.manage_render_job("LIST", confirm_delete=True)


def test_a_child_that_exits_without_finishing_reads_as_failed_with_its_log(jobs) -> None:
    created = _create(jobs)
    Path(created["log_path"]).write_text("Read blend\nError: Out of GPU memory\n", encoding="utf-8")
    jobs.children[0].returncode = 1

    read = jobs.handler.manage_render_job("READ", job_id=created["job_id"])

    assert read["state"] == "FAILED"
    assert read["exit_code"] == 1
    assert "exited with code 1" in read["error"]
    assert read["log_tail"].endswith("Error: Out of GPU memory")


def test_a_child_that_recorded_its_own_end_keeps_that_state(jobs) -> None:
    created = _create(jobs)
    _update(jobs, created["job_id"], state="DONE", frames_done=3, last_file="/r/shot_0003.png")
    jobs.children[0].returncode = 0

    read = jobs.handler.manage_render_job("READ", job_id=created["job_id"])

    assert (read["state"], read["exit_code"], read["frames_done"]) == ("DONE", 0, 3)
    assert "log_tail" not in read


def test_a_child_that_outlives_its_deadline_is_stopped_by_the_add_on(jobs) -> None:
    created = _create(jobs, max_duration_seconds=5)
    _update(jobs, created["job_id"], state="RENDERING", deadline_at=time.time() - 60)

    read = jobs.handler.manage_render_job("READ", job_id=created["job_id"])

    assert jobs.children[0].terminated
    assert read["state"] == "TIMED_OUT"
    assert "max_duration_seconds=5" in read["cancellation_reason"]


def test_the_backup_watchdog_stops_polling_once_no_child_is_held(jobs) -> None:
    created = _create(jobs)
    watch = jobs.timers[0][0]
    assert watch() is not None

    _update(jobs, created["job_id"], state="DONE")
    jobs.children[0].returncode = 0
    assert watch() is None


@pytest.mark.parametrize(
    ("heartbeat_age", "pid_alive", "state", "warned"),
    [
        (60, False, "FAILED", False),
        (2, False, "RENDERING", False),
        (60, True, "RENDERING", True),
    ],
)
def test_a_job_from_an_earlier_session_fails_only_when_silent_and_gone(
    jobs, monkeypatch, heartbeat_age, pid_alive, state, warned
) -> None:
    """A stale heartbeat alone is not death: the pid must be gone too, or it may still be loading."""
    monkeypatch.setattr(jobs.module, "_pid_alive", lambda _pid: pid_alive)
    _unowned_job(jobs, "0123456789ab", heartbeat_at=time.time() - heartbeat_age)

    read = jobs.handler.manage_render_job("READ", job_id="0123456789ab")

    assert read["state"] == state
    assert ("warnings" in read) is warned


def test_delete_refuses_a_running_job_without_confirm_delete(jobs) -> None:
    created = _create(jobs)

    with pytest.raises(ValueError, match="confirm_delete=true"):
        jobs.handler.manage_render_job("DELETE", job_id=created["job_id"])

    assert not jobs.children[0].terminated
    assert Path(created["blend_copy"]).is_file()


@pytest.mark.parametrize("cooperative", [True, False])
def test_delete_stops_a_running_job_and_keeps_every_frame_it_wrote(jobs, monkeypatch, cooperative) -> None:
    monkeypatch.setattr(jobs.module, "_COOPERATIVE_EXIT_SECONDS", 0.05)
    created = _create(jobs)
    frame = jobs.root / "renders" / "shot_0001.png"
    frame.write_bytes(b"frame one")
    _update(jobs, created["job_id"], state="RENDERING", frames_done=1, last_file=str(frame), current_frame=2)
    child = jobs.children[0]
    if cooperative:
        # A child that sees cancel_requested exits on its own, and is never signalled.
        child.on_poll = lambda: 3 if _record(jobs, created["job_id"]).get("cancel_requested") else None

    removed = jobs.handler.manage_render_job("DELETE", job_id=created["job_id"], confirm_delete=True)

    assert child.terminated is not cooperative
    assert (removed["stopped"], removed["state"], removed["frames_done"]) == (True, "CANCELLED", 1)
    assert removed["last_file"] == str(frame)
    assert frame.read_bytes() == b"frame one"
    assert _job_directories(jobs) == []


def test_delete_never_signals_a_pid_whose_heartbeat_went_stale(jobs, monkeypatch) -> None:
    """After that long the recorded pid may belong to an unrelated process."""
    signalled = []
    monkeypatch.setattr(jobs.module, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(jobs.module.os, "kill", lambda pid, sig: signalled.append((pid, sig)))
    _unowned_job(jobs, "0123456789ab", heartbeat_at=time.time() - 60)

    removed = jobs.handler.manage_render_job("DELETE", job_id="0123456789ab", confirm_delete=True)

    assert signalled == []
    assert removed["state"] == "CANCELLED"
    assert "was not signalled" in removed["warnings"][0]
    assert _job_directories(jobs) == []


def test_delete_finishes_when_the_recorded_pid_is_another_accounts_process(jobs, monkeypatch) -> None:
    """A pid this user may not signal was never this add-on's child; DELETE must still complete."""
    monkeypatch.setattr(jobs.module, "_COOPERATIVE_EXIT_SECONDS", 0.01)
    monkeypatch.setattr(jobs.module, "_pid_alive", lambda _pid: True)

    def refuse(_pid, _sig):
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(jobs.module.os, "kill", refuse)
    _unowned_job(jobs, "0123456789ab")

    removed = jobs.handler.manage_render_job("DELETE", job_id="0123456789ab", confirm_delete=True)

    assert removed["state"] == "CANCELLED"
    assert any("may not signal" in warning for warning in removed["warnings"]), removed["warnings"]
    assert _job_directories(jobs) == []


def test_read_takes_the_log_from_the_job_directory_never_from_the_record(jobs, tmp_path) -> None:
    """A record naming another file must not turn READ into a read of that file."""
    secret = tmp_path / "secret.txt"
    secret.write_text("not a render log\n", encoding="utf-8")
    directory = _unowned_job(jobs, "0123456789ab", state="FAILED", log_path=str(secret))
    (directory / "render.log").write_text("Error: the job's own failure\n", encoding="utf-8")

    read = jobs.handler.manage_render_job("READ", job_id="0123456789ab")

    assert read["log_tail"] == "Error: the job's own failure"


def test_jobs_stay_inside_the_file_roots_when_no_output_root_is_set(jobs, monkeypatch, tmp_path) -> None:
    """The saved copy of the open file must not land outside the boundary the file roots declare."""
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.delenv("BLENDERMCP_OUTPUT_ROOTS")
    monkeypatch.setenv("BLENDERMCP_FILE_ROOTS", str(library))

    assert Path(jobs.module.jobs_root()).parent.resolve() == library.resolve()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership")
def test_with_no_roots_jobs_are_kept_in_a_private_per_user_directory(jobs, monkeypatch, tmp_path) -> None:
    """A shared temporary directory would let another account plant a record READ or DELETE trusts."""
    monkeypatch.delenv("BLENDERMCP_OUTPUT_ROOTS")
    monkeypatch.setattr(jobs.module.tempfile, "gettempdir", lambda: str(tmp_path))

    root = Path(jobs.module.jobs_root())

    assert root.name == f"blender_mcp_render_jobs-{os.getuid()}"
    assert root.stat().st_mode & 0o777 == 0o700
    root.chmod(0o777)
    with pytest.raises(ValueError, match="not a private directory"):
        jobs.handler.manage_render_job("LIST")


def test_delete_removes_only_the_files_a_job_writes(jobs) -> None:
    directory = _unowned_job(jobs, "0123456789ab", state="DONE")
    (directory / "render.log").write_text("done\n", encoding="utf-8")
    (directory / "notes.txt").write_text("keep me\n", encoding="utf-8")

    removed = jobs.handler.manage_render_job("DELETE", job_id="0123456789ab")

    assert removed["stopped"] is False
    assert sorted(path.name for path in directory.iterdir()) == ["notes.txt"]
    assert "left in place" in removed["warnings"][0]


def test_list_pages_jobs_newest_first(jobs) -> None:
    for job_id, created_at in (("00000000000a", 100.0), ("00000000000b", 300.0), ("00000000000c", 200.0)):
        _unowned_job(jobs, job_id, state="DONE", created_at=created_at)

    first = jobs.handler.manage_render_job("LIST", limit=2)
    rest = jobs.handler.manage_render_job("LIST", limit=2, offset=first["next_offset"])

    assert [job["job_id"] for job in first["jobs"]] == ["00000000000b", "00000000000c"]
    assert (first["total"], first["truncated"], first["next_offset"]) == (3, True, 2)
    assert [job["job_id"] for job in rest["jobs"]] == ["00000000000a"]
    assert rest["truncated"] is False


def test_read_with_detail_pages_the_written_files(jobs) -> None:
    directory = _unowned_job(jobs, "0123456789ab", state="DONE", frames_done=3)
    for frame in (1, 2, 3):
        jobs.module.job_file.append_file_record(str(directory), {"frame": frame, "path": f"/r/{frame}.png", "bytes": 9})

    page = jobs.handler.manage_render_job("READ", job_id="0123456789ab", detail=True, limit=2, offset=1)

    assert [entry["frame"] for entry in page["files"]] == [2, 3]
    assert (page["total"], page["truncated"], page["next_offset"]) == (3, False, None)


def _tool_arguments(**arguments):
    tool = render_job_tools.mcp._tool_manager._tools["manage_render_job"]
    return tool.fn_metadata.arg_model.model_validate(arguments)


def test_the_tool_refuses_a_job_id_the_add_on_could_not_have_issued() -> None:
    assert _tool_arguments(action="READ", job_id="0123456789ab").job_id == "0123456789ab"
    for job_id in ("../../etc/passwd", "0123456789AB", "0123456789abc"):
        with pytest.raises(ValidationError):
            _tool_arguments(action="READ", job_id=job_id)


@pytest.mark.parametrize(
    ("arguments", "refusal"),
    [
        ({"action": "READ", "job_id": "0123456789ab", "scene_name": "Scene"}, "READ does not take scene_name"),
        ({"action": "LIST", "job_id": "0123456789ab"}, "LIST does not take job_id"),
        ({"action": "DELETE", "job_id": "0123456789ab", "detail": True}, "DELETE does not take detail"),
        ({"action": "READ"}, "READ requires job_id"),
        ({"action": "CREATE", "scene_name": "Scene"}, "confirm_render=True is required"),
        ({"action": "CREATE", "confirm_render": True}, "CREATE requires scene_name"),
        (
            {"action": "CREATE", "scene_name": "Scene", "confirm_render": True, "mode": "ANIMATION", "frame_end": 9},
            "frame_start and frame_end together",
        ),
        (
            {"action": "CREATE", "scene_name": "Scene", "confirm_render": True, "frame_start": 1, "frame_end": 2},
            "only valid for ANIMATION",
        ),
    ],
)
def test_the_tool_refuses_a_malformed_request_before_dispatch(stub_blender_connection, arguments, refusal) -> None:
    connection = stub_blender_connection({})

    with pytest.raises(ToolError, match=refusal):
        asyncio.run(render_job_tools.manage_render_job(ctx=None, **arguments))

    assert connection.calls == []
