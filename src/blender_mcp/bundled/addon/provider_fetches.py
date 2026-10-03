"""
Provider network I/O on worker threads, so Blender's main thread never waits on a download.

Every add-on command runs on Blender's main thread (`socket_transport` queues it and a
timer drains the queue). A provider command that fetched inside its handler froze the
UI and every queued command for as long as the download took, with no progress and no
way to stop it. Provider commands are now two-phase instead:

1. A `start_*` command validates its arguments on the main thread - reading the API key
   or the cache directory from `bpy` is all it does there - and hands a bpy-free job to
   `REGISTRY.start`, which runs it on a daemon thread and answers at once with the
   fetch's status, `fetch_id` among it.
2. `get_provider_fetch` reports that status (state, stage, bytes, failure, and a query's
   result) and `cancel_provider_fetch` stops a running fetch or discards a finished one.
3. A download's import command (`import_polyhaven_asset`, `import_sketchfab_model`)
   takes the finished fetch's private payload with `REGISTRY.take` and does only the
   `bpy` work, on the main thread, inside the usual mutation transaction.

A job receives a `FetchContext` and nothing else: its private directory, a progress and
cancellation hook it passes to `network.py` as the `Transfer`, and `stage` for the stage
text. It must never touch `bpy`. What it returns is the result: `{"error": ...}` fails
the fetch with that message, anything else succeeds it. A query's result is public and
rides on its status; a download's is private and only `take` reads it.

Bounds: at most `MAX_RUNNING_FETCHES` run at once and `MAX_RETAINED_FETCHES` are held
in all. A finished fetch is dropped `RESULT_TTL_SECONDS` after it finished, and a running
one nobody has asked about for `ABANDONED_AFTER_SECONDS` is cancelled, so a client that
went away leaves neither a thread nor a directory behind for long. A fetch's directory
is removed when it fails, is cancelled, is discarded, or expires, and by the import that
takes it.
"""

import shutil
import tempfile
import threading
import time
import uuid

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Literal

from .file_paths import sanitize_blender_error

FetchState = Literal["RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]

MAX_RUNNING_FETCHES = 3
MAX_RETAINED_FETCHES = 12
RESULT_TTL_SECONDS = 600.0
ABANDONED_AFTER_SECONDS = 120.0


class FetchCancelledError(Exception):
    """Raised on a worker thread once its fetch has been cancelled."""


@dataclass(slots=True)
class _Fetch:
    """One fetch's bookkeeping; every field is read and written under the registry's lock."""

    fetch_id: str
    provider: str
    kind: str
    directory: str | None
    keeps_files: bool
    failure_label: str
    started_at: float
    seen_at: float
    cancel: threading.Event = field(default_factory=threading.Event)
    state: FetchState = "RUNNING"
    stage: str = "starting"
    bytes_received: int = 0
    # The declared sizes of every response so far; None once one declared nothing.
    bytes_total: int | None = 0
    finished_at: float | None = None
    failure: str | None = None
    result: dict | None = None
    payload: dict | None = None
    # Cancelled or released by a client: dropped as soon as its worker exits.
    discarded: bool = False


class FetchContext:
    """
    What a worker job may use: its directory, its progress, and its cancellation.

    It is also the `network.Transfer` the job passes to every request, so each chunk
    is counted and a cancellation lands at the next chunk.
    """

    def __init__(self, registry: "FetchRegistry", record: _Fetch) -> None:
        """
        Bind one fetch to the registry that owns it.

        Args:
            registry: The registry, whose lock guards the record.
            record: The fetch this job runs for.

        """
        self._registry = registry
        self._record = record

    @property
    def directory(self) -> str:
        """
        The fetch's private directory, for a job that writes files.

        Raises:
            RuntimeError: For a query, which has no directory.

        """
        if self._record.directory is None:
            raise RuntimeError("this fetch keeps no files")
        return self._record.directory

    def check_cancelled(self) -> None:
        """
        Stop the job here if its fetch was cancelled.

        Raises:
            FetchCancelledError: Once the fetch has been cancelled.

        """
        if self._record.cancel.is_set():
            raise FetchCancelledError

    def stage(self, text: str) -> None:
        """
        Say what the job is doing now, for the status reply.

        Args:
            text: A short stage description, such as "downloading model".

        """
        self.check_cancelled()
        with self._registry.lock:
            self._record.stage = text

    def begin(self, declared_bytes: int | None) -> None:
        """
        Add one response's declared size to the fetch's total.

        Args:
            declared_bytes: Its Content-Length, or None when it declared none.

        """
        self.check_cancelled()
        with self._registry.lock:
            total = self._record.bytes_total
            self._record.bytes_total = None if total is None or declared_bytes is None else total + declared_bytes

    def advance(self, received_bytes: int) -> None:
        """
        Count one chunk, then stop the job if its fetch was cancelled meanwhile.

        Args:
            received_bytes: How many bytes the chunk held.

        """
        with self._registry.lock:
            self._record.bytes_received += received_bytes
        self.check_cancelled()


Job = Callable[[FetchContext], dict]


def _remove_directories(directories: list[str]) -> None:
    for directory in directories:
        with suppress(OSError):
            shutil.rmtree(directory)


def _outcome(job: Job, context: FetchContext, failure_label: str) -> tuple[FetchState, dict | None, str | None]:
    """
    Run one job and classify how it ended; called on its worker thread.

    Args:
        job: The work.
        context: Its fetch context.
        failure_label: Prefixed to an unexpected exception's sanitized text.

    Returns:
        tuple[FetchState, dict | None, str | None]: The final state, the job's dict when it
        succeeded, and the failure message when it did not.

    """
    try:
        reply = job(context)
        context.check_cancelled()
    except FetchCancelledError:
        return "CANCELLED", None, None
    except Exception as exc:
        return "FAILED", None, f"{failure_label}: {sanitize_blender_error(exc)}"
    if not isinstance(reply, dict):
        return "FAILED", None, f"{failure_label}: the provider job returned {type(reply).__name__}, not a dict"
    if reply.get("error"):
        return "FAILED", None, str(reply["error"])
    return "SUCCEEDED", reply, None


class FetchRegistry:
    """Every provider fetch the add-on holds, by id; safe to use from any thread."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """
        Start with no fetches.

        Args:
            clock: Monotonic seconds; tests pass a controllable one.

        """
        self.lock = threading.Lock()
        self._fetches: dict[str, _Fetch] = {}
        self._clock = clock
        self._threads: dict[str, threading.Thread] = {}

    def start(
        self,
        provider: str,
        kind: str,
        job: Job,
        *,
        keeps_files: bool,
        failure_label: str,
        directory_parent: str | None = None,
    ) -> dict:
        """
        Run one job on a new worker thread and report its starting status.

        Args:
            provider: `polyhaven` or `sketchfab`, for the status and for `take`.
            kind: What the job fetches, such as `catalog` or `asset`; `take` checks it.
            job: The bpy-free work; see the module docstring.
            keeps_files: True for a download, whose directory and payload wait for
                an import; False for a query, whose result rides on its status.
            failure_label: Prefixed to an unexpected exception's sanitized text.
            directory_parent: Where to make the download's directory; the system
                temp directory when None. A download that must later be renamed into
                a cache passes the cache, so the rename stays on one filesystem.

        Returns:
            dict: `status`'s reply for the new fetch.

        Raises:
            RuntimeError: When as many fetches as allowed are already running or held.

        """
        now = self._clock()
        with self.lock:
            expired = self._sweep_locked(now)
            running = sum(1 for record in self._fetches.values() if record.state == "RUNNING")
            refusal = None
            if running >= MAX_RUNNING_FETCHES:
                refusal = (
                    f"{running} provider downloads are already running in Blender, the most allowed at once; "
                    "wait for one to finish or cancel it with cancel_provider_fetch."
                )
            elif len(self._fetches) >= MAX_RETAINED_FETCHES:
                refusal = (
                    f"Blender already holds {len(self._fetches)} provider fetches, the most allowed; import or "
                    "cancel finished ones first."
                )
        _remove_directories(expired)
        if refusal is not None:
            raise RuntimeError(refusal)
        directory = tempfile.mkdtemp(prefix=f"blender_mcp_{provider}_", dir=directory_parent) if keeps_files else None
        record = _Fetch(
            fetch_id=uuid.uuid4().hex,
            provider=provider,
            kind=kind,
            directory=directory,
            keeps_files=keeps_files,
            failure_label=failure_label,
            started_at=now,
            seen_at=now,
        )
        thread = threading.Thread(
            target=self._run, args=(record, job), name=f"blender-mcp-fetch-{record.fetch_id[:8]}", daemon=True
        )
        with self.lock:
            self._fetches[record.fetch_id] = record
            self._threads[record.fetch_id] = thread
            reply = self._describe_locked(record, now)
        thread.start()
        return reply

    def _run(self, record: _Fetch, job: Job) -> None:
        """
        Run one job on its worker thread and record how it ended.

        Args:
            record: The fetch.
            job: Its work.

        """
        state, reply, failure = _outcome(job, FetchContext(self, record), record.failure_label)
        with self.lock:
            record.state = state
            record.finished_at = self._clock()
            record.failure = failure
            record.stage = state.lower()
            if state == "SUCCEEDED" and reply is not None:
                if record.keeps_files:
                    record.payload = reply
                else:
                    record.result = reply
            keep_directory = state == "SUCCEEDED" and not record.discarded
            if record.discarded:
                self._fetches.pop(record.fetch_id, None)
            self._threads.pop(record.fetch_id, None)
        if record.directory is not None and not keep_directory:
            _remove_directories([record.directory])

    def _sweep_locked(self, now: float) -> list[str]:
        """
        Cancel abandoned running fetches and drop expired finished ones; the lock is held.

        Args:
            now: The clock's reading.

        Returns:
            list[str]: Directories of dropped fetches, for the caller to remove
            once it has released the lock.

        """
        directories = []
        for fetch_id, record in list(self._fetches.items()):
            if record.state == "RUNNING":
                if not record.discarded and now - record.seen_at > ABANDONED_AFTER_SECONDS:
                    record.cancel.set()
                    record.discarded = True
            elif record.finished_at is not None and now - record.finished_at > RESULT_TTL_SECONDS:
                del self._fetches[fetch_id]
                if record.directory is not None:
                    directories.append(record.directory)
        return directories

    def _describe_locked(self, record: _Fetch, now: float) -> dict:
        """
        Build one fetch's status reply; the lock is held.

        Args:
            record: The fetch.
            now: The clock's reading.

        Returns:
            dict: `fetch_id`, `provider`, `kind`, `state`, `stage`, `bytes_received`,
            `bytes_total` (None when a response declared no size), `elapsed_seconds`,
            `ready_to_import` (a finished download), and `failure` or `result` when
            there is one. Never `error`: a failed fetch is a status, not a failed call.

        """
        reply: dict = {
            "fetch_id": record.fetch_id,
            "provider": record.provider,
            "kind": record.kind,
            "state": record.state,
            "stage": "cancelling" if record.discarded and record.state == "RUNNING" else record.stage,
            "bytes_received": record.bytes_received,
            "bytes_total": record.bytes_total,
            "elapsed_seconds": round((record.finished_at or now) - record.started_at, 3),
            "ready_to_import": record.state == "SUCCEEDED" and record.keeps_files,
        }
        if record.failure is not None:
            reply["failure"] = record.failure
        if record.result is not None:
            reply["result"] = record.result
        return reply

    def status(self, fetch_id: str) -> dict:
        """
        Report one fetch, and note that a client is still waiting on it.

        Args:
            fetch_id: The id `start` returned.

        Returns:
            dict: `_describe_locked`'s reply.

        Raises:
            ValueError: For an id the registry does not hold.

        """
        now = self._clock()
        with self.lock:
            expired = self._sweep_locked(now)
            record = self._fetches.get(fetch_id)
            if record is not None:
                record.seen_at = now
                reply = self._describe_locked(record, now)
        _remove_directories(expired)
        if record is None:
            raise ValueError(_unknown(fetch_id))
        return reply

    def cancel(self, fetch_id: str) -> dict:
        """
        Stop a running fetch, or discard a finished one with its files; idempotent.

        Args:
            fetch_id: The id `start` returned.

        Returns:
            dict: `fetch_id`, `discarded` (False for an id the registry no longer
            holds) and `state`: `CANCELLING` for a fetch whose worker is still
            stopping, the state a finished one ended in, or None for an unknown id.

        """
        now = self._clock()
        directories: list[str] = []
        with self.lock:
            directories = self._sweep_locked(now)
            record = self._fetches.get(fetch_id)
            if record is None:
                reply = {"fetch_id": fetch_id, "discarded": False, "state": None}
            elif record.state == "RUNNING":
                record.cancel.set()
                record.discarded = True
                reply = {"fetch_id": fetch_id, "discarded": True, "state": "CANCELLING"}
            else:
                del self._fetches[fetch_id]
                if record.directory is not None:
                    directories.append(record.directory)
                reply = {"fetch_id": fetch_id, "discarded": True, "state": record.state}
        _remove_directories(directories)
        return reply

    def take(self, fetch_id: str, *, provider: str, kind: str) -> tuple[dict, str | None]:
        """
        Hand a finished download to its import, which then owns its directory.

        Args:
            fetch_id: The id `start` returned.
            provider: The provider the import belongs to.
            kind: The kind of fetch the import consumes.

        Returns:
            tuple[dict, str | None]: The job's private payload and the directory its
            files are in; the caller removes the directory when it is done.

        Raises:
            ValueError: For an unknown id, another provider's or kind's fetch, one
                still running, or one that failed or was cancelled.

        """
        now = self._clock()
        with self.lock:
            expired = self._sweep_locked(now)
            record = self._fetches.get(fetch_id)
            refusal = None
            if record is None:
                refusal = _unknown(fetch_id)
            elif (record.provider, record.kind) != (provider, kind):
                refusal = f"Fetch {fetch_id} is a {record.provider} {record.kind} fetch, not a {provider} {kind} one."
            elif record.state == "RUNNING":
                refusal = f"Fetch {fetch_id} is still running; poll get_provider_fetch until it SUCCEEDED."
            elif record.state != "SUCCEEDED" or record.payload is None:
                refusal = record.failure or f"Fetch {fetch_id} ended {record.state}, with nothing to import."
            else:
                del self._fetches[fetch_id]
        _remove_directories(expired)
        if refusal is not None:
            raise ValueError(refusal)
        assert record is not None and record.payload is not None
        return record.payload, record.directory

    def shutdown(self) -> None:
        """Cancel every running fetch and remove every finished one's files, for unregister."""
        directories = []
        with self.lock:
            for fetch_id, record in list(self._fetches.items()):
                if record.state == "RUNNING":
                    record.cancel.set()
                    record.discarded = True
                else:
                    del self._fetches[fetch_id]
                    if record.directory is not None:
                        directories.append(record.directory)
        _remove_directories(directories)

    def join(self, fetch_id: str, timeout: float | None = None) -> None:
        """
        Wait for one fetch's worker thread to exit; for tests and smoke scripts.

        Args:
            fetch_id: The id `start` returned.
            timeout: Seconds to wait at most, or None to wait as long as it takes.

        """
        with self.lock:
            thread = self._threads.get(fetch_id)
        if thread is not None:
            thread.join(timeout)


def _unknown(fetch_id: str) -> str:
    return (
        f"Unknown provider fetch {fetch_id!r}: it was imported, cancelled, or finished more than "
        f"{int(RESULT_TTL_SECONDS)} seconds ago. Start it again."
    )


REGISTRY = FetchRegistry()


class ProviderFetchHandlersMixin:
    """The two commands every provider fetch shares: report one, and cancel or discard one."""

    def get_provider_fetch(self, fetch_id: str) -> dict:
        """
        Report a provider fetch's state, progress, failure, and a finished query's result.

        Args:
            fetch_id: The id a `start_*` command returned.

        Returns:
            dict: `FetchRegistry.status`'s reply.

        """
        return REGISTRY.status(str(fetch_id))

    def cancel_provider_fetch(self, fetch_id: str) -> dict:
        """
        Cancel a running provider fetch, or discard a finished one and its files.

        Args:
            fetch_id: The id a `start_*` command returned.

        Returns:
            dict: `FetchRegistry.cancel`'s reply.

        """
        return REGISTRY.cancel(str(fetch_id))
