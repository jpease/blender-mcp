"""
`manage_render_job`: a render in its own `blender -b` process, tracked by a file on disk.

A render in this process holds Blender's main thread for as long as it runs: no other command is
answered, and nothing can stop a frame part way. A job renders a saved copy of the open file in a
separate Blender instead, so CREATE returns at once and READ, LIST and DELETE stay answerable
throughout. `max_duration_seconds` is a hard wall-clock bound: the child's own watchdog ends the
process mid-frame at the deadline, and a child this add-on started that outlives it by
`_DEADLINE_GRACE_SECONDS` is killed from here.

A job is the directory `<jobs_root>/<job_id>/`: `job.json` (the record READ returns),
`scene.blend` (the copy that renders), `render.log` (the child's console) and `files.jsonl` (one
line per written frame). The record's protocol, and the script the child runs, live in
`..render_job_supervisor`. The rendered frames are written where the caller asked, never inside
the job directory, so removing a job never removes a frame.

The child is started without `--factory-startup`, so the user's Preferences - Cycles GPU devices,
enabled add-ons - apply to it as they do here, and with `--disable-autoexec`, so no script embedded
in the file runs, as for every file this add-on loads.

Jobs outlive this add-on on purpose: stopping the MCP server, disabling the add-on or quitting
Blender stops none of them, and each still ends at its own deadline. A job started before this
add-on was last registered is no longer a process it holds a handle to, so its liveness is judged
by its heartbeat and recorded pid instead.
"""

import logging
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import time

from contextlib import suppress
from dataclasses import dataclass

import bpy

from .. import render_job_supervisor as job_file
from ..file_paths import canonical_path, contains, create_save_directory, enforce_roots
from ..helpers import bounded_int, page_records
from ..output_roots import configured_file_roots, configured_roots, writable_roots
from ..render_devices import effective_cycles_device
from .blend_files import operator_failure_message, require_bool
from .rendering import plan_render_job

logger = logging.getLogger(__name__)

_CREATE_PARAMETERS = frozenset(
    {
        "scene_name",
        "filepath",
        "mode",
        "view_layer_name",
        "frame",
        "frame_start",
        "frame_end",
        "max_animation_frames",
        "max_duration_seconds",
        "confirm_render",
        "confirm_overwrite",
        "confirm_frame_range",
        "create_directories",
    }
)
# The parameters each action takes; anything else set to a non-default value is refused by name.
_ACTION_PARAMETERS = {
    "CREATE": _CREATE_PARAMETERS,
    "READ": frozenset({"job_id", "detail", "limit", "offset"}),
    "LIST": frozenset({"limit", "offset"}),
    "DELETE": frozenset({"job_id", "confirm_delete"}),
}
# `secrets.token_hex(6)`. Matched whole before any path is built from it, so an id can never
# name anything outside the jobs root.
_JOB_ID = re.compile(r"[0-9a-f]{12}")
_JOBS_DIRECTORY_NAME = "blender_mcp_render_jobs"
# The child writes a heartbeat every second; this much silence, with the process gone, is a crash.
_STALE_HEARTBEAT_SECONDS = 15.0
# How far past its deadline a child this add-on started may run before it is killed from here.
_DEADLINE_GRACE_SECONDS = 10.0
# DELETE first lets the child see `cancel_requested` and exit cleanly, then terminates, then kills.
_COOPERATIVE_EXIT_SECONDS = 3.0
_TERMINATE_WAIT_SECONDS = 2.0
_POLL_SECONDS = 0.05
_WATCH_INTERVAL_SECONDS = 1.0
_LOG_TAIL_BYTES = 4096
_LOG_TAIL_LINES = 12
_MAX_PAGE_SIZE = 100
# How many unsaved images CREATE's warning names before it counts the rest.
_IMAGES_NAMED = 3
# Everything a job directory holds; DELETE removes these and nothing else.
_JOB_FILES = (job_file.JOB_FILENAME, job_file.BLEND_COPY_FILENAME, job_file.LOG_FILENAME, job_file.FILES_FILENAME)
# What a READ reply carries of the record, in this order.
_RECORD_FIELDS = (
    "job_id",
    "state",
    "scene_name",
    "mode",
    "view_layer_name",
    "filepath",
    "frame_start",
    "frame_end",
    "frame_step",
    "frames_total",
    "frames_done",
    "current_frame",
    "last_file",
    "bytes_written",
    "max_duration_seconds",
    "created_at",
    "started_at",
    "finished_at",
    "deadline_at",
    "pid",
    "exit_code",
    "engine",
    "effective_cycles_device",
    "blend_copy",
    "log_path",
    "error",
    "cancellation_reason",
)
_SUMMARY_FIELDS = ("job_id", "state", "scene_name", "mode", "frames_done", "frames_total", "created_at", "last_file")


@dataclass(frozen=True, slots=True)
class _OwnedJob:
    """A child this add-on session started, and the record it renders from."""

    process: subprocess.Popen[bytes]
    path: str


# Children this add-on session started, by job id. `_watch_owned_jobs` reaps them and enforces
# their deadline; `unregister_handlers` forgets them, and they carry on regardless.
_OWNED_JOBS: dict[str, _OwnedJob] = {}


def _private_temp_root():
    """
    Name this user's own jobs directory under the system temporary directory, creating it private.

    A shared `/tmp` would let another account plant a `job.json` whose log path READ echoes or
    whose pid DELETE signals, so the directory is per-user, mode 0700, and refused when it is a
    symlink, is not owned by this user, or is open to anyone else. Windows' temporary directory is
    already per-user.

    Returns:
        str: The directory.

    Raises:
        ValueError: When the directory exists but is not private to this user.

    """
    if os.name == "nt":
        return os.path.join(tempfile.gettempdir(), _JOBS_DIRECTORY_NAME)
    root = os.path.join(tempfile.gettempdir(), f"{_JOBS_DIRECTORY_NAME}-{os.getuid()}")
    with suppress(FileExistsError):
        os.mkdir(root, 0o700)
    info = os.lstat(root)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(
            f"The render jobs directory {root} is not a private directory owned by this user; remove it, or set "
            "BLENDERMCP_OUTPUT_ROOTS so jobs are kept there instead"
        )
    return root


def jobs_root():
    """
    Name the directory render jobs are kept in.

    Returns:
        str: `blender_mcp_render_jobs` under the first writable configured output root, else under
        the first writable file root, so the saved copy of the open file stays inside the declared
        boundary; with no roots configured, a private per-user directory under the system
        temporary directory.

    Raises:
        ValueError: When roots are configured but none of them is writable, or the temporary
            directory's jobs directory is not private to this user.

    """
    configured = configured_roots() or configured_file_roots()
    if not configured:
        return _private_temp_root()
    writable = writable_roots(configured)
    if not writable:
        raise ValueError(
            "No configured output or file root (BLENDERMCP_OUTPUT_ROOTS, BLENDERMCP_FILE_ROOTS) is writable, so "
            "there is nowhere to keep render jobs"
        )
    return os.path.join(writable[0], _JOBS_DIRECTORY_NAME)


def _job_path(job_id):
    """
    Resolve a job id to its `job.json`, refusing any id CREATE could not have issued.

    Args:
        job_id: The caller's job id.

    Returns:
        str: The path of the job's record.

    Raises:
        ValueError: When the id is not 12 lowercase hex characters.

    """
    if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
        raise ValueError("job_id must be the 12-character lowercase hex id manage_render_job(action='CREATE') returned")
    return os.path.join(jobs_root(), job_id, job_file.JOB_FILENAME)


def _load(job_id):
    """
    Read one job's record.

    Args:
        job_id: The caller's job id.

    Returns:
        tuple[str, dict]: The record's path and the record.

    Raises:
        ValueError: When the id is malformed or names no job.

    """
    path = _job_path(job_id)
    record = job_file.read_job(path)
    if record is None:
        raise ValueError(f"Render job not found: {job_id}; LIST shows the jobs that exist")
    return path, record


def _pid_alive(pid):
    """
    Report whether a recorded pid still names a running process, without signalling it.

    A child this add-on started but no longer holds a handle to (the add-on was unregistered since) is
    reaped here, or it would linger as a zombie that looks alive.

    Args:
        pid: The recorded pid.

    Returns:
        bool | None: Whether it runs; None where that cannot be asked safely (Windows, whose
        `os.kill` terminates the process whatever the signal).

    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        return None
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    else:
        return reaped == 0
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until(finished, seconds):
    """
    Poll until a condition holds or time runs out.

    Args:
        finished: A no-argument callable.
        seconds: How long to wait at most.

    Returns:
        bool: Whether the condition held.

    """
    deadline = time.monotonic() + seconds
    while not finished():
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_SECONDS)
    return True


def _stop(process):
    """
    Terminate a child this session started, killing it if it does not exit.

    Args:
        process: The child's handle.

    Returns:
        int | None: Its exit code, or None if even a kill did not end it in time.

    """
    process.terminate()
    try:
        return process.wait(timeout=_TERMINATE_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
    try:
        return process.wait(timeout=_TERMINATE_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        return None


def _finish(path, state, **fields):
    """
    Record a terminal state on behalf of a child that can no longer record its own.

    Args:
        path: The job's `job.json`.
        state: The terminal state.
        **fields: Further fields to record.

    Returns:
        dict | None: The stored record, or None when the job is gone.

    """
    return job_file.update_job(path, state=state, current_frame=None, finished_at=time.time(), **fields)


def _reconcile_owned(job_id, owned, record, now):
    """
    Reap a child this session started, or stop one that outlived its deadline.

    Args:
        job_id: The job's id.
        owned: The child's handle and record path.
        record: The record as last read.
        now: Epoch seconds.

    Returns:
        dict: The record, updated when the child's fate changed it.

    """
    code = owned.process.poll()
    if code is None:
        deadline_at = record.get("deadline_at")
        if record.get("state") in job_file.TERMINAL_STATES or deadline_at is None:
            return record
        if now < deadline_at + _DEADLINE_GRACE_SECONDS:
            return record
        code = _stop(owned.process)
        _OWNED_JOBS.pop(job_id, None)
        reason = (
            f"max_duration_seconds={record.get('max_duration_seconds')} elapsed and the render process missed its "
            f"own deadline by {_DEADLINE_GRACE_SECONDS:.0f} s, so the add-on stopped it"
        )
        return _finish(owned.path, job_file.TIMED_OUT, exit_code=code, cancellation_reason=reason) or record
    _OWNED_JOBS.pop(job_id, None)
    # Re-read: the child records its own terminal state just before it exits.
    latest = job_file.read_job(owned.path) or record
    if latest.get("state") in job_file.TERMINAL_STATES:
        return job_file.update_job(owned.path, exit_code=code) or latest
    error = f"the render process exited with code {code} before finishing; see log_tail"
    return _finish(owned.path, job_file.FAILED, exit_code=code, error=error) or latest


def _reconcile(job_id, path, record, now):
    """
    Bring a record in line with its process: reap, time out, or declare a vanished child failed.

    Args:
        job_id: The job's id.
        path: The job's `job.json`.
        record: The record as last read.
        now: Epoch seconds.

    Returns:
        dict: The record, updated when reconciliation changed it.

    """
    owned = _OWNED_JOBS.get(job_id)
    if owned is not None:
        return _reconcile_owned(job_id, owned, record, now)
    if record.get("state") in job_file.TERMINAL_STATES:
        return record
    age = now - float(record.get("heartbeat_at") or record.get("created_at") or 0.0)
    if age <= _STALE_HEARTBEAT_SECONDS or _pid_alive(record.get("pid")):
        return record
    error = f"the render process is gone: no heartbeat for {age:.0f} s and pid {record.get('pid')} is not running"
    return _finish(path, job_file.FAILED, error=error) or record


def _watch_owned_job(job_id, owned):
    """
    Reap one of this session's children, or reconcile its record against its deadline.

    Args:
        job_id: The job's id.
        owned: Its entry in `_OWNED_JOBS`.

    """
    record = job_file.read_job(owned.path)
    if record is None:
        # Deleted out from under this session; the child exits when it sees that.
        if owned.process.poll() is not None:
            _OWNED_JOBS.pop(job_id, None)
        return
    _reconcile(job_id, owned.path, record, time.time())


def _watch_owned_jobs():
    """
    Reap this session's children and enforce their deadlines, from a `bpy.app.timers` tick.

    A backup: each child ends itself at its deadline. Timers never fire in `blender -b`, where
    READ and LIST reconcile the same way whenever they are called.

    Returns:
        float | None: The next interval while any child is held, else None to unregister.

    """
    for job_id, owned in list(_OWNED_JOBS.items()):
        try:
            _watch_owned_job(job_id, owned)
        except Exception:
            logger.exception("Render job watchdog failed for job %s", job_id)
    return _WATCH_INTERVAL_SECONDS if _OWNED_JOBS else None


def unregister_handlers():
    """
    Detach the watchdog timer and drop this session's child handles, as the add-on unregisters.

    The timer is `persistent`, so it would otherwise outlive the add-on and keep firing into a
    module a reload has replaced, beside the timer the new module registers for its own jobs.
    `_start` registers at most one, so one unregister removes it. The children are not stopped -
    jobs outlive the add-on by design - only forgotten: each still ends at its own deadline, and
    READ, LIST and DELETE judge it by its heartbeat and recorded pid, as they judge a job any
    earlier session started. The watchdog registers itself again when the next job starts.
    """
    if bpy.app.timers.is_registered(_watch_owned_jobs):
        bpy.app.timers.unregister(_watch_owned_jobs)
    _OWNED_JOBS.clear()


def _log_tail(path):
    """
    Read the last lines of a job's console, for a FAILED reply.

    The log is always `render.log` beside the job's `job.json`; the record's own `log_path` is
    never opened, so a record cannot point READ at a file outside the job.

    Args:
        path: The job's `job.json`.

    Returns:
        str | None: Up to `_LOG_TAIL_LINES` lines, or None when there is no log.

    """
    log_path = os.path.join(os.path.dirname(path), job_file.LOG_FILENAME)
    try:
        with open(log_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - _LOG_TAIL_BYTES))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-_LOG_TAIL_LINES:]) or None


def _unsaved_image_warning():
    """
    Name the images whose pixels exist only in memory, which the saved copy cannot carry.

    Returns:
        str | None: The warning, or None when every image is saved or packed.

    """
    dirty = [image.name for image in bpy.data.images if image.type == "IMAGE" and image.is_dirty]
    if not dirty:
        return None
    shown = ", ".join(dirty[:_IMAGES_NAMED]) + (
        f" and {len(dirty) - _IMAGES_NAMED} more" if len(dirty) > _IMAGES_NAMED else ""
    )
    return (
        f"{len(dirty)} image(s) have unsaved pixel edits ({shown}); the job renders a saved copy, which has "
        "their last saved pixels. Save or pack them and CREATE again to render the edits."
    )


def _save_copy(blend_copy):
    """
    Write the open session to the job directory without changing which file is open.

    `relative_remap` rewrites `//` paths for the copy's own directory, so textures and libraries
    the open file reaches relatively still resolve from there.

    Args:
        blend_copy: The absolute path of the copy.

    Raises:
        RuntimeError: When Blender cannot write it.

    """
    try:
        result = bpy.ops.wm.save_as_mainfile(filepath=blend_copy, copy=True, relative_remap=True, compress=False)
    except RuntimeError as exc:
        raise RuntimeError(operator_failure_message("manage_render_job", exc, (blend_copy,))) from exc
    if "FINISHED" not in result:
        raise RuntimeError(f"manage_render_job failed: saving the render copy returned {sorted(result)}")


def _spawn(path, blend_copy, log_path):
    """
    Start the child that renders the copy.

    Args:
        path: The job's `job.json`.
        blend_copy: The saved copy.
        log_path: Where the child's console goes.

    Returns:
        subprocess.Popen: The child.

    Raises:
        RuntimeError: When this Blender has no executable to start, or the start fails.

    """
    binary = bpy.app.binary_path
    if not binary:
        raise RuntimeError("manage_render_job needs a Blender executable, and bpy.app.binary_path is empty here")
    command = [
        binary,
        "--background",
        "--quiet",
        "--disable-autoexec",
        blend_copy,
        "--python-exit-code",
        str(job_file.EXIT_FAILED),
        "--python",
        os.path.abspath(job_file.__file__),
        "--",
        path,
    ]
    try:
        with open(log_path, "wb") as log:
            return subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
    except OSError as exc:
        raise RuntimeError(f"manage_render_job could not start Blender: {exc.strerror or exc}") from exc


def _new_record(root, plan, request, effective_device):
    """
    Build a job's first record: QUEUED, with the frames the plan names and the deadline it has.

    Args:
        root: The jobs root the job directory goes under.
        plan: `plan_render_job`'s reply.
        request: The CREATE parameters.
        effective_device: `effective_cycles_device`'s device for the scene.

    Returns:
        dict: The record, not yet written.

    """
    scene, frames = plan["scene"], plan["frames"]
    directory = os.path.join(root, secrets.token_hex(6))
    max_duration_seconds = request["max_duration_seconds"]
    created_at = time.time()
    return {
        "job_id": os.path.basename(directory),
        "state": job_file.QUEUED,
        "scene_name": scene.name,
        "mode": plan["mode"],
        "view_layer_name": request["view_layer_name"],
        "filepath": plan["output"],
        "frame_start": frames[0]["frame"],
        "frame_end": frames[-1]["frame"],
        "frame_step": scene.frame_step if plan["mode"] == "ANIMATION" else 1,
        "frames_total": len(frames),
        "frames_done": 0,
        "bytes_written": 0,
        "current_frame": None,
        "last_file": None,
        "confirm_overwrite": request["confirm_overwrite"],
        "max_duration_seconds": max_duration_seconds,
        "created_at": created_at,
        "deadline_at": None if max_duration_seconds is None else created_at + max_duration_seconds,
        "started_at": None,
        "finished_at": None,
        "heartbeat_at": created_at,
        "pid": None,
        "exit_code": None,
        "engine": scene.render.engine,
        "effective_cycles_device": effective_device,
        "blend_copy": os.path.join(directory, job_file.BLEND_COPY_FILENAME),
        "log_path": os.path.join(directory, job_file.LOG_FILENAME),
        "cancel_requested": False,
        "error": None,
        "cancellation_reason": None,
    }


def _start(record, create_directories):
    """
    Make the job directory, save the copy, write the record and start the child.

    Anything that fails removes the job directory again, so a job that never started leaves
    nothing to LIST.

    Args:
        record: The job's first record.
        create_directories: Whether to create the output's missing directory.

    Returns:
        tuple[dict, bool]: The stored record, now carrying the child's pid, and whether the
        output directory was created.

    """
    directory = os.path.dirname(record["blend_copy"])
    path = os.path.join(directory, job_file.JOB_FILENAME)
    os.makedirs(os.path.dirname(directory), exist_ok=True)
    os.makedirs(directory)
    try:
        _save_copy(record["blend_copy"])
        job_file.write_job(path, record)
        # Last before the start, so a refused or failed job leaves no output directory behind.
        created_directory = create_directories and create_save_directory(record["filepath"])
        process = _spawn(path, record["blend_copy"], record["log_path"])
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    _OWNED_JOBS[record["job_id"]] = _OwnedJob(process, path)
    if not bpy.app.timers.is_registered(_watch_owned_jobs):
        bpy.app.timers.register(_watch_owned_jobs, first_interval=_WATCH_INTERVAL_SECONDS, persistent=True)
    return job_file.update_job(path, pid=process.pid) or record, created_directory


def _create(request):
    """
    Validate, save a copy, write the record, and start the child; return without waiting for it.

    Args:
        request: The CREATE parameters.

    Returns:
        dict: The new job's record fields, and whether the output directory was created.

    Raises:
        ValueError: On a request `render_scene` would refuse, an output outside the file roots
            or inside the jobs directory, or unconfirmed rendering.

    """
    if not require_bool("confirm_render", request["confirm_render"]):
        raise ValueError("confirm_render=True is required")
    plan = plan_render_job(
        request["scene_name"],
        filepath=request["filepath"],
        mode=request["mode"],
        frame=request["frame"],
        frame_start=request["frame_start"],
        frame_end=request["frame_end"],
        max_animation_frames=request["max_animation_frames"],
        view_layer_name=request["view_layer_name"],
        max_duration_seconds=request["max_duration_seconds"],
        confirm_overwrite=request["confirm_overwrite"],
        confirm_frame_range=request["confirm_frame_range"],
        create_directories=request["create_directories"],
    )
    # The job writes after this call returns, from another process, so the output is held to the
    # file roots whether or not its directory exists.
    enforce_roots(plan["output"], configured_file_roots())
    root = jobs_root()
    if contains(canonical_path(root), canonical_path(plan["output"])):
        raise ValueError("filepath must not be inside the render jobs directory, which DELETE removes")
    device, device_warning = effective_cycles_device(plan["scene"])
    record = _new_record(root, plan, request, device)
    record, created_directory = _start(record, request["create_directories"])
    reply = {field: record.get(field) for field in _RECORD_FIELDS}
    reply["created_directory"] = created_directory
    warnings = [warning for warning in (device_warning, _unsaved_image_warning()) if warning]
    if warnings:
        reply["warnings"] = warnings
    return reply


def _read(job_id, detail, limit, offset):
    """
    Report one job, reconciled with its process.

    Args:
        job_id: The job's id.
        detail: Add a page of the written files.
        limit: The page size.
        offset: The page start.

    Returns:
        dict: The record's public fields, derived timings, the log tail when FAILED, and the
        files page when detail.

    """
    path, record = _load(job_id)
    now = time.time()
    record = _reconcile(job_id, path, record, now)
    reply = {field: record.get(field) for field in _RECORD_FIELDS}
    started = record.get("started_at") or record.get("created_at") or now
    reply["elapsed_seconds"] = round((record.get("finished_at") or now) - started, 3)
    warnings = []
    if record.get("state") not in job_file.TERMINAL_STATES:
        age = now - float(record.get("heartbeat_at") or record.get("created_at") or now)
        reply["heartbeat_age_seconds"] = round(age, 3)
        if age > _STALE_HEARTBEAT_SECONDS and record.get("state") == job_file.RENDERING:
            warnings.append(
                f"No heartbeat for {age:.0f} s, but pid {record.get('pid')} is still running (or its pid was reused)"
            )
    if record.get("state") == job_file.FAILED:
        reply["log_tail"] = _log_tail(path)
    if detail:
        records = job_file.read_file_records(os.path.dirname(path))
        reply |= page_records(records, offset, limit, _MAX_PAGE_SIZE, key="files")
    if warnings:
        reply["warnings"] = warnings
    return reply


def _list(limit, offset):
    """
    Page through every job, newest first.

    Args:
        limit: The page size.
        offset: The page start.

    Returns:
        dict: "jobs" (summary records) and the page keys.

    """
    root = jobs_root()
    try:
        names = [name for name in os.listdir(root) if _JOB_ID.fullmatch(name)]
    except FileNotFoundError:
        names = []
    records = []
    for name in names:
        path = os.path.join(root, name, job_file.JOB_FILENAME)
        try:
            record = job_file.read_job(path)
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            records.append((name, path, record))
    records.sort(key=lambda entry: float(entry[2].get("created_at") or 0.0), reverse=True)
    page = page_records(records, offset, limit, _MAX_PAGE_SIZE, key="jobs")
    now = time.time()
    jobs = []
    for job_id, path, record in page["jobs"]:
        reconciled = _reconcile(job_id, path, record, now)
        jobs.append({field: reconciled.get(field) for field in _SUMMARY_FIELDS})
    page["jobs"] = jobs
    return page


def _stop_unowned(pid, warnings):
    """
    Stop a child an earlier add-on session started: let it exit on its own, then signal it.

    Args:
        pid: Its recorded pid, whose heartbeat the caller found fresh.
        warnings: Where to note a pid this process may not signal.

    """
    if _wait_until(lambda: _pid_alive(pid) is False, _COOPERATIVE_EXIT_SECONDS):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        # Another account's process: never one this add-on started, so not the job's renderer.
        warnings.append(f"pid {pid} was not signalled: it belongs to a process this user may not signal")
        return
    # Absent on Windows, where SIGTERM already terminates the process outright.
    force = getattr(signal, "SIGKILL", None)
    if force is not None and not _wait_until(lambda: _pid_alive(pid) is False, _TERMINATE_WAIT_SECONDS):
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, force)


def _cancel(job_id, path, record, warnings):
    """
    Stop a running job: ask it to exit, then terminate it, and record CANCELLED.

    A process this session did not start is signalled only while its heartbeat is fresh, because
    a stale one means the recorded pid may now belong to something else.

    Args:
        job_id: The job's id.
        path: The job's `job.json`.
        record: The reconciled record.
        warnings: Where to note a process that was not signalled.

    Returns:
        dict: The record after the stop.

    """
    job_file.update_job(path, cancel_requested=True)
    owned = _OWNED_JOBS.pop(job_id, None)
    if owned is not None:
        if not _wait_until(lambda: owned.process.poll() is not None, _COOPERATIVE_EXIT_SECONDS):
            _stop(owned.process)
    else:
        pid = record.get("pid")
        age = time.time() - float(record.get("heartbeat_at") or record.get("created_at") or 0.0)
        if age <= _STALE_HEARTBEAT_SECONDS and _pid_alive(pid) is not False:
            _stop_unowned(pid, warnings)
        elif age > _STALE_HEARTBEAT_SECONDS:
            warnings.append(
                f"pid {pid} was not signalled: its heartbeat is {age:.0f} s old, so the pid may belong to another "
                "process now"
            )
    latest = job_file.read_job(path) or record
    if latest.get("state") in job_file.TERMINAL_STATES:
        return latest
    return _finish(path, job_file.CANCELLED, cancellation_reason="deleted while running") or latest


def _remove_job_directory(directory, warnings):
    """
    Remove a job's own files and then its directory, and nothing else.

    Args:
        directory: The job directory.
        warnings: Where to note anything left behind.

    """
    for name in os.listdir(directory):
        is_temporary_record = name.startswith(job_file.JOB_FILENAME + ".") and name.endswith(".tmp")
        if name in _JOB_FILES or is_temporary_record:
            os.remove(os.path.join(directory, name))
    try:
        os.rmdir(directory)
    except OSError:
        warnings.append("The job directory held files this job did not write, so it was left in place")


def _delete(job_id, confirm_delete):
    """
    Stop a job if it is running, then remove its directory; the rendered frames stay.

    Args:
        job_id: The job's id.
        confirm_delete: Whether a running job may be stopped.

    Returns:
        dict: What was removed, the job's final state, and the last frame it wrote.

    Raises:
        ValueError: When the job is running and confirm_delete is not true.

    """
    path, record = _load(job_id)
    record = _reconcile(job_id, path, record, time.time())
    running = record.get("state") not in job_file.TERMINAL_STATES
    warnings = []
    if running:
        if not require_bool("confirm_delete", confirm_delete):
            raise ValueError(
                f"Render job {job_id} is still {record.get('state')} ({record.get('frames_done')} of "
                f"{record.get('frames_total')} frames written); pass confirm_delete=true to stop it and remove its "
                "job directory. Frames it already wrote are kept."
            )
        record = _cancel(job_id, path, record, warnings)
    _remove_job_directory(os.path.dirname(path), warnings)
    reply = {
        "job_id": job_id,
        "deleted": True,
        "stopped": running,
        "state": record.get("state"),
        "frames_done": record.get("frames_done"),
        "frames_total": record.get("frames_total"),
        "last_file": record.get("last_file"),
        "filepath": record.get("filepath"),
    }
    if warnings:
        reply["warnings"] = warnings
    return reply


class RenderJobHandlersMixin:
    """Run renders in their own Blender process, tracked by a job file."""

    def manage_render_job(
        self,
        action,
        job_id=None,
        scene_name=None,
        filepath=None,
        mode="STILL",
        view_layer_name=None,
        frame=None,
        frame_start=None,
        frame_end=None,
        max_animation_frames=250,
        max_duration_seconds=None,
        confirm_render=False,
        confirm_overwrite=False,
        confirm_frame_range=False,
        create_directories=False,
        confirm_delete=False,
        detail=False,
        limit=20,
        offset=0,
    ):
        """
        CREATE, READ, LIST or DELETE a render job; see the module docstring for the lifecycle.

        Args:
            action: "CREATE", "READ", "LIST" or "DELETE".
            job_id: The job READ and DELETE address; refused by CREATE and LIST.
            scene_name: CREATE: the scene to render, or the active scene.
            filepath: CREATE: the output, shaped as for render_scene; defaults to the scene's.
            mode: CREATE: "STILL" or "ANIMATION".
            view_layer_name: CREATE: the one view layer to render, or every enabled one.
            frame: CREATE: the STILL frame, or the scene's current frame.
            frame_start: CREATE: an ANIMATION's first frame, given with frame_end.
            frame_end: CREATE: an ANIMATION's last frame, given with frame_start.
            max_animation_frames: CREATE: upper bound on an ANIMATION's frame count.
            max_duration_seconds: CREATE: hard wall-clock bound on the whole job.
            confirm_render: CREATE: must be true.
            confirm_overwrite: CREATE: allow replacing existing output files.
            confirm_frame_range: CREATE: accept the scene's untouched 1-250 default range.
            create_directories: CREATE: create a missing output directory.
            confirm_delete: DELETE: allow stopping a job that is still running.
            detail: READ: add a page of the written files.
            limit: READ and LIST: page size.
            offset: READ and LIST: page start.

        Returns:
            dict: The action's reply.

        Raises:
            ValueError: On an unknown action, a parameter the action does not take, or an
                invalid value.

        """
        action = str(action).upper()
        if action not in _ACTION_PARAMETERS:
            raise ValueError(f"action must be one of {sorted(_ACTION_PARAMETERS)}")
        given = {
            name
            for name, value, default in (
                ("job_id", job_id, None),
                ("scene_name", scene_name, None),
                ("filepath", filepath, None),
                ("mode", str(mode).upper(), "STILL"),
                ("view_layer_name", view_layer_name, None),
                ("frame", frame, None),
                ("frame_start", frame_start, None),
                ("frame_end", frame_end, None),
                ("max_animation_frames", max_animation_frames, 250),
                ("max_duration_seconds", max_duration_seconds, None),
                ("confirm_render", confirm_render, False),
                ("confirm_overwrite", confirm_overwrite, False),
                ("confirm_frame_range", confirm_frame_range, False),
                ("create_directories", create_directories, False),
                ("confirm_delete", confirm_delete, False),
                ("detail", detail, False),
                ("limit", limit, 20),
                ("offset", offset, 0),
            )
            if value != default
        }
        foreign = sorted(given - _ACTION_PARAMETERS[action])
        if foreign:
            raise ValueError(f"{action} does not take {', '.join(foreign)}")
        if action == "CREATE":
            return _create(
                {
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
                    "confirm_overwrite": require_bool("confirm_overwrite", confirm_overwrite),
                    "confirm_frame_range": require_bool("confirm_frame_range", confirm_frame_range),
                    "create_directories": require_bool("create_directories", create_directories),
                }
            )
        limit = bounded_int("limit", limit, 1, _MAX_PAGE_SIZE)
        offset = bounded_int("offset", offset, 0)
        if action == "LIST":
            return _list(limit, offset)
        if job_id is None:
            raise ValueError(f"{action} requires job_id")
        if action == "READ":
            return _read(job_id, require_bool("detail", detail), limit, offset)
        return _delete(job_id, confirm_delete)
