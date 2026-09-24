"""
A render job's file protocol, and the script that renders the job inside its own `blender -b`.

`manage_render_job(action="CREATE")` saves a copy of the open file into `<jobs_root>/<job_id>/`,
writes `job.json` beside it, and starts
`blender -b -q -Y <copy> --python-exit-code 1 --python <this file> -- <job.json>`. Run as
`__main__`, this file renders the frames `job.json` names exactly as `render_scene` renders in
process - `frame_set`, then one `bpy.ops.render.render(write_still=True)` per frame into the file
Blender's own `frame_path()` names - and keeps `job.json` current as it goes. Every written frame
is appended to `files.jsonl`, so `job.json`, rewritten every second, stays small however long the
animation.

Both processes read and write `job.json`, so the protocol is spelled here once: the add-on imports
the helpers above `main`; the child loads this file by path, outside the add-on package, so it
imports nothing from it.

A daemon watchdog thread refreshes `heartbeat_at` every second and ends the process - mid-frame,
which no in-process render can do - when `cancel_requested` is set, when `job.json` is gone, or when
`deadline_at` has passed. Blender releases the GIL while a render runs (measured on 5.2.2), so the
thread keeps running through a long frame.

Writes are atomic (a temporary file, then `os.replace`), so a reader never sees half a record. The
parent writes only `cancel_requested` while the child runs, and a terminal state only once it has
exited; a cancellation lost to a concurrent heartbeat is still delivered by the signal and the
directory removal that follow it.
"""

import json
import os
import shutil
import sys
import threading
import time

from contextlib import suppress
from typing import NoReturn

import bpy

JOB_FILENAME = "job.json"
BLEND_COPY_FILENAME = "scene.blend"
LOG_FILENAME = "render.log"
FILES_FILENAME = "files.jsonl"

QUEUED = "QUEUED"
RENDERING = "RENDERING"
DONE = "DONE"
CANCELLED = "CANCELLED"
TIMED_OUT = "TIMED_OUT"
FAILED = "FAILED"
TERMINAL_STATES = frozenset({DONE, CANCELLED, TIMED_OUT, FAILED})

HEARTBEAT_INTERVAL_SECONDS = 1.0
# How long `error` may grow; the reply carrying it is capped at 8 KiB.
MAX_ERROR_CHARS = 500

EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_CANCELLED = 3
EXIT_TIMED_OUT = 4
EXIT_GONE = 5


def read_job(path):
    """
    Read a job record.

    Args:
        path: The job's `job.json`.

    Returns:
        dict | None: The record, or None when the file is gone.

    """
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None


def write_job(path, record):
    """
    Replace a job record atomically, so no reader sees a partial file.

    Args:
        path: The job's `job.json`.
        record: The whole record to store.

    Raises:
        FileNotFoundError: When the job directory has been removed.

    """
    temporary = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(record, handle, sort_keys=True)
    os.replace(temporary, path)


def update_job(path, **fields):
    """
    Merge fields into the stored record, re-read immediately before writing.

    Args:
        path: The job's `job.json`.
        **fields: The fields to set.

    Returns:
        dict | None: The stored record, or None when the job is gone.

    """
    record = read_job(path)
    if record is None:
        return None
    record.update(fields)
    write_job(path, record)
    return record


def append_file_record(job_dir, entry):
    """
    Record one written frame in the job's append-only file list.

    Args:
        job_dir: The job directory.
        entry: {"frame": int, "path": str, "bytes": int}.

    """
    with open(os.path.join(job_dir, FILES_FILENAME), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def read_file_records(job_dir):
    """
    Read every written frame's record, in render order.

    Args:
        job_dir: The job directory.

    Returns:
        list[dict]: The records; empty before the first frame is written.

    """
    try:
        with open(os.path.join(job_dir, FILES_FILENAME), encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except FileNotFoundError:
        return []


def _exit_now(code, tempdir) -> NoReturn:
    """
    End this process at once, from any thread, removing Blender's own temporary directory.

    `os._exit` is the only exit a watchdog thread can take while the main thread is inside a
    render, and it skips the cleanup that would otherwise delete `bpy.app.tempdir`.

    Args:
        code: The exit code.
        tempdir: `bpy.app.tempdir`, read on the main thread at start.

    """
    with suppress(Exception):
        sys.stdout.flush()
        sys.stderr.flush()
    # Only Blender's own per-session `blender_XXXXXX` directory. When Blender cannot create one it
    # falls back to the base temporary directory itself, which is not this process's to remove.
    if tempdir and os.path.basename(os.path.normpath(tempdir)).startswith("blender_"):
        shutil.rmtree(tempdir, ignore_errors=True)
    os._exit(code)


class _ChildJob:
    """The child's handle on its `job.json`: one lock, because two threads write it."""

    def __init__(self, path, tempdir):
        self.path = path
        self.directory = os.path.dirname(path)
        self.tempdir = tempdir
        self.lock = threading.Lock()

    def update(self, **fields):
        """
        Merge fields into the record, or end the process when the job was deleted.

        Args:
            **fields: The fields to set.

        Returns:
            dict: The stored record.

        """
        with self.lock:
            try:
                record = update_job(self.path, **fields)
            except FileNotFoundError:
                record = None
            if record is None:
                _exit_now(EXIT_GONE, self.tempdir)
            return record

    def end(self, state, exit_code, **fields) -> NoReturn:
        """
        Record a terminal state and exit; the caller holds `lock`.

        Args:
            state: CANCELLED or TIMED_OUT.
            exit_code: The process exit code.
            **fields: Further fields to record.

        """
        with suppress(OSError):
            update_job(self.path, state=state, current_frame=None, finished_at=time.time(), **fields)
        _exit_now(exit_code, self.tempdir)


def _time_out(job: _ChildJob, record, when) -> NoReturn:
    """
    Record TIMED_OUT and exit; the caller holds the job's lock.

    Args:
        job: This process's job handle.
        record: The job record, for its bound.
        when: Where the deadline fell, for the reason.

    """
    reason = f"max_duration_seconds={record.get('max_duration_seconds')} elapsed {when}"
    job.end(TIMED_OUT, EXIT_TIMED_OUT, cancellation_reason=reason)


def _tick(job, deadline_at):
    """
    Run one watchdog pass under the job's lock; the caller handles file errors.

    Args:
        job: This process's job handle.
        deadline_at: Epoch seconds the job must end by, or None.

    Returns:
        bool: False once the job has reached a terminal state and needs no more watching.

    """
    record = read_job(job.path)
    if record is None:
        _exit_now(EXIT_GONE, job.tempdir)
    if record.get("state") in TERMINAL_STATES:
        return False
    if record.get("cancel_requested"):
        job.end(CANCELLED, EXIT_CANCELLED, cancellation_reason="cancel requested")
    if deadline_at is not None and time.time() >= deadline_at:
        _time_out(job, record, f"during frame {record.get('current_frame')}, which was abandoned")
    record["heartbeat_at"] = time.time()
    write_job(job.path, record)
    return True


def _watch(job, deadline_at):
    """
    Refresh the heartbeat every second; end the process on cancel, deletion or the deadline.

    Args:
        job: This process's job handle.
        deadline_at: Epoch seconds the job must end by, or None.

    """
    while True:
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)
        with job.lock:
            try:
                if not _tick(job, deadline_at):
                    return
            except FileNotFoundError:
                _exit_now(EXIT_GONE, job.tempdir)
            except (OSError, ValueError):
                # A transient write failure (a reader holding the file on Windows) must not stop
                # the heartbeat; the next tick tries again.
                continue


def _render(job, record):
    """
    Render every frame the record names, the way `render_scene` does in process.

    Args:
        job: This process's job handle.
        record: The job record as the parent wrote it.

    Raises:
        RuntimeError: When the scene is missing, a frame's file already exists unconfirmed, or
            Blender does not finish a frame or write its file.

    """
    scene = bpy.data.scenes.get(record["scene_name"])
    if scene is None:
        raise RuntimeError(f"Scene not found in the saved copy: {record['scene_name']}")
    output = record["filepath"]
    view_layer_name = record.get("view_layer_name")
    deadline_at = record.get("deadline_at")
    bytes_written = 0
    frames = range(record["frame_start"], record["frame_end"] + 1, record["frame_step"])
    for frames_done, frame in enumerate(frames, start=1):
        if deadline_at is not None and time.time() >= deadline_at:
            with job.lock:
                _time_out(job, record, f"before frame {frame}")
        # frame_set applies the timeline's camera markers, exactly as render_scene does.
        scene.frame_set(frame)
        if record["mode"] == "ANIMATION":
            scene.render.filepath = output
            frame_output = os.path.abspath(scene.render.frame_path(frame=frame))
        else:
            frame_output = output
        if os.path.exists(frame_output) and not record.get("confirm_overwrite"):
            raise RuntimeError(f"Output for frame {frame} appeared after the job was created: {frame_output}")
        scene.render.filepath = frame_output
        job.update(state=RENDERING, current_frame=frame)
        kwargs = {"animation": False, "write_still": True, "scene": scene.name}
        if view_layer_name:
            kwargs["layer"] = view_layer_name
        result = bpy.ops.render.render(**kwargs)
        if "FINISHED" not in result:
            raise RuntimeError(f"Blender render was cancelled at frame {frame}: {sorted(result)}")
        if not os.path.isfile(frame_output):
            raise RuntimeError(f"Render reported FINISHED but output is missing: {frame_output}")
        size = os.path.getsize(frame_output)
        append_file_record(job.directory, {"frame": frame, "path": frame_output, "bytes": size})
        bytes_written += size
        job.update(frames_done=frames_done, bytes_written=bytes_written, last_file=frame_output)
    job.update(state=DONE, current_frame=None, finished_at=time.time())


def main():
    """Render the job whose `job.json` follows `--` on the command line."""
    tempdir = bpy.app.tempdir
    arguments = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if len(arguments) != 1:
        print("usage: blender -b <copy.blend> --python render_job_supervisor.py -- <job.json>", file=sys.stderr)
        _exit_now(EXIT_USAGE, tempdir)
    job = _ChildJob(os.path.abspath(arguments[0]), tempdir)
    now = time.time()
    record = job.update(state=RENDERING, started_at=now, heartbeat_at=now, pid=os.getpid())
    watchdog = threading.Thread(target=_watch, args=(job, record.get("deadline_at")), name="render-job-watchdog")
    watchdog.daemon = True
    watchdog.start()
    try:
        _render(job, record)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
        job.update(state=FAILED, current_frame=None, finished_at=time.time(), error=message)
        # Re-raised so Blender prints the traceback into render.log and exits with
        # --python-exit-code.
        raise


if __name__ == "__main__":
    main()
