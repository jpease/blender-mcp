"""
Tests for the addon's socket server threading model (no Blender required).

server_core.py cannot be imported without bpy, so BlenderMCPServer is lifted
out by AST and executed against stubs.

The bug these cover: commands used to be dispatched by calling
bpy.app.timers.register() from a client thread. bpy.app.timers is main-thread
only, so on Windows the callback could be silently dropped - the connection was
accepted but no response ever arrived, and the client hung until its 180s
socket timeout.
"""

from __future__ import annotations

import ast
import json
import socket
import sys
import threading
import time
import types

from contextlib import suppress

import pytest

from conftest import ROOT_ADDON, install_file_lifecycle_handler_lists, load_addon_source_module

SERVER_CORE = ROOT_ADDON.parent / "server_core.py"


class _StubWindowManagerOps:
    """
    Stand-in for `bpy.ops.wm` that makes a file swap observable without Blender.

    `open_mainfile` records the path it was asked for, mutates the stub
    database, and fires the real `session.py` handlers the way Blender fires
    them - two positional arguments, the second None (measured on 5.2.2). A path
    ending in `_MISSING_SUFFIX` reproduces the one failure mode every open
    failure collapses to from the caller's side: `load_post_fail` fires,
    `load_post` does not, the database is left untouched, and the operator
    raises `RuntimeError` rather than returning `{'CANCELLED'}`.

    Attributes:
        open_mainfile_calls: Every filepath handed to `open_mainfile`, in order.
        use_scripts_calls: The `use_scripts` value each of those calls passed.
            Recorded, not ignored: §07 makes an `open_mainfile` call that does
            not pass `use_scripts=False` explicitly automatically critical, so
            the harness has to be able to see it.

    """

    _MISSING_SUFFIX = ".missing.blend"

    def __init__(self, bpy_stub: types.ModuleType) -> None:
        """
        Bind the ops stub to the database and handler lists it will drive.

        Args:
            bpy_stub: The stub `bpy` module this instance mutates.

        """
        self._bpy = bpy_stub
        self.open_mainfile_calls: list[str] = []
        self.use_scripts_calls: list[bool] = []

    def open_mainfile(self, filepath: str = "", use_scripts: bool = False) -> set[str]:
        """
        Swap the stub database, or fail the way a missing file fails.

        Args:
            filepath: The .blend to open.
            use_scripts: Spelled exactly as production spells it. The name
                carries no underscore prefix on purpose: §07 makes a
                `wm.open_mainfile` call that does not pass `use_scripts=False`
                explicitly an automatically-critical failure, and a stub whose
                parameter is `_use_scripts` rejects production's own spelling
                with a TypeError - so the harness could never validate the one
                argument it most needs to.

        Returns:
            set[str]: `{'FINISHED'}`, the only result a successful open returns.

        Raises:
            RuntimeError: When `filepath` names an unloadable file.

        """
        self.open_mainfile_calls.append(filepath)
        self.use_scripts_calls.append(use_scripts)
        # `load_pre` first, on both paths. Measured on 5.2.2 by
        # `scripts/blender_probes/session_handlers.py`: Blender fires it for a
        # successful open and for every open failure alike, and it fires while
        # `bpy.data.filepath` and the object table are still the OLD file's -
        # which is why the stub fires it before touching `self._bpy.data`.
        for handler in list(self._bpy.app.handlers.load_pre):
            handler(filepath, None)
        if filepath.endswith(self._MISSING_SUFFIX):
            for handler in list(self._bpy.app.handlers.load_post_fail):
                handler(filepath, None)
            raise RuntimeError("stub open_mainfile: no such file")
        self._bpy.data.filepath = filepath
        for handler in list(self._bpy.app.handlers.load_post):
            handler(filepath, None)
        return {"FINISHED"}

    def abort_open_mainfile(self, filepath: str = "") -> None:
        """
        Begin a load and never finish it, the way Blender's own Esc does.

        The shape the indeterminate latch exists for, and the one the harness
        could not previously produce: `load_pre` fires, so Blender has begun
        reading over the database, and then **neither** `load_post` nor
        `load_post_fail` does. A test that raised `KeyboardInterrupt` without
        calling an operator at all was not modelling this - it was modelling an
        abort before any load began, which is a different case with a different
        correct answer.

        Args:
            filepath: The .blend the aborted load was reading.

        Raises:
            KeyboardInterrupt: Always, after `load_pre` has fired.

        """
        self.open_mainfile_calls.append(filepath)
        for handler in list(self._bpy.app.handlers.load_pre):
            handler(filepath, None)
        raise KeyboardInterrupt("stub open_mainfile: aborted part-way through the load")


def _load_server_class():
    """
    Compile BlenderMCPServer from server_core.py against stub modules.

    execute_command is overridden per-instance by every test in this file (see
    _make_server), so the real dispatch table and handler mixins are never
    invoked - only __init__/start/stop/the socket loop are exercised. The
    mixins only need to exist as base classes for the ClassDef to compile.

    The one exception is `session.py`: the drain loop's file-swap barrier reads
    the live session epoch, so the *real* module is executed here against this
    same `bpy` stub rather than being faked. That is what lets a barrier test
    assert on an epoch a stub swap actually moved.
    """
    source = SERVER_CORE.read_text(encoding="utf-8")
    tree = ast.parse(source)

    body = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "BlenderMCPServer"]
    assert body, "BlenderMCPServer not found in server_core.py"

    main_thread = threading.current_thread()
    registered: list[object] = []

    class Timers:
        """
        Stub for bpy.app.timers: main-thread-only, and matching by identity.

        Measured against Blender 5.2.2 and recorded in `PHASE2_TASK_STATE.md`
        under Task 1's timer-identity probe: two accesses of the same bound
        method are not identical but do compare equal, `is_registered` answers
        False for a fresh access and True only for the very object registered,
        and `unregister` on a fresh access raises `ValueError: function is not
        registered`. The transcript lives with the probe rather than here - a
        transcript in a docstring that no committed script emits cannot be
        re-run, and three of them were found in this task.

        So registrations are held in a *list* and matched with `is`, never in a
        dict or with `in`. Two bound methods of the same object compare equal
        and hash equal, so an equality-matched stub answers True where Blender
        answers False - which is exactly how a `stop()` that never unregistered
        its timer could pass its own regression test.
        """

        def register(self, fn, first_interval=0.0, persistent=False) -> None:
            if threading.current_thread() is not main_thread:
                raise AssertionError("bpy.app.timers.register() called from a non-main thread")
            registered.append(fn)

        def unregister(self, fn) -> None:
            for index, existing in enumerate(registered):
                if existing is fn:
                    del registered[index]
                    return
            raise ValueError("function is not registered")

        def is_registered(self, fn) -> bool:
            return any(existing is fn for existing in registered)

    handlers = types.ModuleType("bpy.app.handlers")
    handlers.persistent = lambda fn: fn
    install_file_lifecycle_handler_lists(handlers)

    app = types.ModuleType("bpy.app")
    app.background = False
    app.timers = Timers()
    app.handlers = handlers

    bpy = types.ModuleType("bpy")
    bpy.app = app
    bpy.context = types.SimpleNamespace(scene=types.SimpleNamespace())
    bpy.data = types.SimpleNamespace(filepath="", is_dirty=False, libraries=[])
    window_manager_ops = _StubWindowManagerOps(bpy)
    bpy.ops = types.SimpleNamespace(wm=window_manager_ops)

    # session.py imports bpy and nothing from its own package, so installing the
    # stub for the length of this exec is enough to load the real module. The
    # entries are removed again immediately: a lingering sys.modules["bpy"]
    # would change what every later test module imports.
    installed = {name: sys.modules.get(name) for name in ("bpy", "bpy.app", "bpy.app.handlers")}
    sys.modules["bpy"] = bpy
    sys.modules["bpy.app"] = bpy.app
    sys.modules["bpy.app.handlers"] = handlers
    try:
        session = load_addon_source_module("session.py", "blender_mcp_addon_session_threading")
    finally:
        for name, previous in installed.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    session.register_handlers()

    # BlenderMCPServer's real base classes; the tests here never call a
    # handler method (execute_command is stubbed per-instance in
    # _make_server), so empty stand-ins are enough to satisfy the ClassDef.
    mixin_names = (
        "ViewportHandlersMixin",
        "AnimationHandlersMixin",
        "CameraHandlersMixin",
        "LightingHandlers",
        "CharacterRiggingHandlersMixin",
        "RigidBodyHandlersMixin",
        "RetopologyHandlersMixin",
        "MeshHandlersMixin",
        "ModelHandlersMixin",
        "ClothHandlersMixin",
        "LiquidHandlersMixin",
        "SceneHandlersMixin",
        "ScenePhysicsHandlersMixin",
        "ObjectAnimationHandlersMixin",
        "RenderingHandlersMixin",
        "NDHandlersMixin",
        "PolyhavenHandlersMixin",
        "SketchfabHandlersMixin",
        "FileLifecycleHandlersMixin",
    )

    namespace = {
        "bpy": bpy,
        "socket": socket,
        "threading": threading,
        "json": json,
        "time": time,
        "queue": __import__("queue"),
        "traceback": __import__("traceback"),
        "os": __import__("os"),
        "suppress": suppress,
        "get_blendermcp_addon_preferences": lambda context=None: None,
        "session_snapshot": session.session_snapshot,
        "mark_session_indeterminate": session.mark_session_indeterminate,
        # The latch accessor the drain loop asks before dispatching. Bound to
        # the same `session` module object the two names above come from, so a
        # test that aborts a swap and then queues a command sees one consistent
        # piece of state rather than two.
        "session_is_indeterminate": session.session_is_indeterminate,
        # The single predicate `_run_session_swap`'s abort guard reads. Same
        # module object again: a flag read from a second copy of `session.py`
        # would never move, which would make the guard fire exactly where it
        # must not and the test pass anyway.
        "load_in_flight": session.load_in_flight,
        **{name: type(name, (), {}) for name in mixin_names},
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), "<addon>", "exec"), namespace)
    return namespace["BlenderMCPServer"], registered, session, window_manager_ops


BlenderMCPServer, _registered, _session, _window_manager_ops = _load_server_class()


def _free_port():
    with socket.socket() as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


def _make_server():
    server = BlenderMCPServer(port=_free_port())
    # Stub out command execution; these tests are about transport, not bpy.
    server.execute_command = lambda command: {
        "status": "success",
        "result": {"echo": command.get("type")},
    }
    return server


def _pump(server, deadline=3.0) -> None:
    """Act as Blender's main loop, draining the queue until timeout."""
    end = time.time() + deadline
    while time.time() < end:
        server.drain_command_queue()
        time.sleep(0.01)


def test_client_thread_never_registers_a_timer() -> None:
    """
    The regression itself: dispatch must not touch bpy.app.timers off-thread.

    The Timers stub raises if register() is called from a non-main thread, so
    the old per-command bpy.app.timers.register() would surface here.
    """
    server = _make_server()
    server.start()
    try:
        with socket.create_connection(("localhost", server.port), timeout=5) as client:
            client.sendall(json.dumps({"type": "ping"}).encode() + b"\n")

            pump = threading.Thread(target=_pump, args=(server,), daemon=True)
            pump.start()

            client.settimeout(5)
            response = json.loads(client.recv(8192).decode())

        assert response["status"] == "success"
        assert response["result"]["echo"] == "ping"
    finally:
        server.stop()


def test_command_is_queued_not_executed_on_client_thread() -> None:
    """Without a main-loop pump, the command waits in the queue - never lost."""
    server = _make_server()
    server.start()
    try:
        with socket.create_connection(("localhost", server.port), timeout=5) as client:
            client.sendall(json.dumps({"type": "ping"}).encode() + b"\n")

            # No pump running, so nothing should execute yet.
            deadline = time.time() + 2.0
            while time.time() < deadline and server.command_queue.empty():
                time.sleep(0.01)

            assert not server.command_queue.empty(), "command was dropped, not queued"

            # Now pump once: the queued command is serviced.
            server.drain_command_queue()
            client.settimeout(5)
            response = json.loads(client.recv(8192).decode())
            assert response["status"] == "success"
    finally:
        server.stop()


def test_stop_releases_client_threads() -> None:
    """
    stop() must unblock handlers so they cannot outlive a restart.

    Orphaned daemon threads parked in recv() were what produced the
    WinError 10054 after toggling the addon.
    """
    server = _make_server()
    server.start()

    client = socket.create_connection(("localhost", server.port), timeout=5)
    try:
        deadline = time.time() + 2.0
        while time.time() < deadline:
            with server._clients_lock:
                if server._clients:
                    break
            time.sleep(0.01)

        with server._clients_lock:
            assert server._clients, "server did not track the client socket"

        server.stop()

        # Handler threads should have exited and deregistered themselves.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            with server._clients_lock:
                if not server._clients:
                    break
            time.sleep(0.01)

        with server._clients_lock:
            assert not server._clients, "client sockets still tracked after stop()"

        assert not bpy_timer_registered(server), "drain timer left registered"
    finally:
        with suppress(OSError):
            client.close()


def bpy_timer_count(server) -> int:
    """
    Count the drain timers Blender still holds for `server`.

    Cannot ask for `server.drain_command_queue`: that expression builds a fresh
    bound method on every access, so comparing it by identity (the way Blender
    matches) would always answer zero and this helper could never fail. Bound
    methods are identified by their owner and underlying function instead,
    which pins the timer to *this* server without depending on which reference
    the server happens to hold internally.

    Args:
        server: The BlenderMCPServer whose timers to count.

    Returns:
        int: How many registrations belong to `server`. More than one means
        repeated start/stop cycles have been multiplying the drain throttle.

    """
    drain = type(server).drain_command_queue
    return sum(
        1 for fn in _registered if getattr(fn, "__self__", None) is server and getattr(fn, "__func__", None) is drain
    )


def bpy_timer_registered(server) -> bool:
    """
    Report whether `server`'s drain timer is still registered.

    Args:
        server: The BlenderMCPServer to inspect.

    Returns:
        bool: True while Blender would still call the drain callback.

    """
    return bpy_timer_count(server) > 0


def _await_accept_loop(server, timeout: float = 3.0) -> None:
    """
    Block until the server thread is inside its accept loop.

    `start()` binds and listens on the main thread, so a successful connect
    proves nothing about the server thread's own progress. Waiting until that
    thread has *accepted* and tracked a client does.

    Needed because `_server_loop` calls `self.socket.settimeout(1.0)` before
    entering its guarded loop, so a `stop()` landing in that window closes the
    socket out from under it and the thread dies with an unhandled EBADF. That
    race is pre-existing and untouched here; this wait keeps the start/stop
    tests below from hitting it at random.

    Args:
        server: The started BlenderMCPServer to wait on.
        timeout: Seconds to wait before giving up.

    Raises:
        AssertionError: If no connection was accepted within `timeout`.

    """
    with socket.create_connection(("localhost", server.port), timeout=timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with server._clients_lock:
                if server._clients:
                    return
            time.sleep(0.01)
    raise AssertionError("the server thread never reached its accept loop")


def test_stop_unregisters_the_drain_timer() -> None:
    """
    A started server holds exactly one drain timer, and stop() takes it away.

    Blender matches timers by identity, so a `stop()` that passes a freshly
    accessed `self.drain_command_queue` to `unregister()` removes nothing - it
    hands Blender an object it has never seen. Counting rather than asserting a
    bool is deliberate: the failure mode is accumulation, not absence.
    """
    server = _make_server()
    server.start()
    try:
        _await_accept_loop(server)
        assert bpy_timer_count(server) == 1, "start() did not register exactly one drain timer"
    finally:
        server.stop()

    assert bpy_timer_count(server) == 0, "stop() left the drain timer registered"


def test_repeated_start_stop_cycles_do_not_accumulate_drain_timers() -> None:
    """
    Toggling the addon must not multiply the drain throttle.

    Every surviving duplicate timer gets its own `_MAX_COMMANDS_PER_TICK` /
    `_DRAIN_TIME_BUDGET_SECONDS` allowance, so N stale registrations let N
    times as much work run on Blender's main thread per tick - the opposite of
    what that throttle exists to guarantee.
    """
    server = _make_server()
    for _cycle in range(3):
        server.start()
        _await_accept_loop(server)
        assert bpy_timer_count(server) == 1, "a restart registered a duplicate drain timer"
        server.stop()
        assert bpy_timer_count(server) == 0, "a stopped server still holds a drain timer"


class _Unserializable:
    """
    Stand-in for a `mathutils` value a handler returned by mistake.

    Its repr carries a path and a secret so the test can prove neither reaches
    the client. `handlers/` constructs `mathutils` values in ~95 places, so
    json.dumps raising on a handler result is reachable in practice.
    """

    def __repr__(self) -> str:
        return "<Vector at /Users/someone/secret-token/scene.blend>"


def test_a_non_serializable_result_gets_an_error_frame_not_silence() -> None:
    """
    An unserializable handler result must still produce a response frame.

    json.dumps() used to run inside the try that guards sendall, whose handler
    only printed "client disconnected" - so a serialization failure wrote no
    frame of any kind and was indistinguishable from a dropped socket. The
    client then sat in recv() until its own 180s timeout: a true hang.
    """
    server = _make_server()
    server.execute_command = lambda _command: {"status": "success", "result": _Unserializable()}
    server.start()
    try:
        with socket.create_connection(("localhost", server.port), timeout=5) as client:
            client.sendall(json.dumps({"type": "ping", "id": "req-1"}).encode() + b"\n")

            pump = threading.Thread(target=_pump, args=(server,), daemon=True)
            pump.start()

            client.settimeout(5)
            response = json.loads(client.recv(8192).decode())
    finally:
        server.stop()

    assert response["status"] == "error"
    assert response["id"] == "req-1", "the error frame must still be matchable to its request"


def test_the_serialization_error_frame_leaks_nothing_about_the_scene() -> None:
    """
    The client is told *that* the response was unserializable, never what it held.

    A repr of scene data, an absolute path, or a traceback in a client-facing
    message is a disclosure the transport layer has no business making.
    """
    server = _make_server()
    server.execute_command = lambda _command: {"status": "success", "result": _Unserializable()}
    server.start()
    try:
        with socket.create_connection(("localhost", server.port), timeout=5) as client:
            client.sendall(json.dumps({"type": "ping", "id": "req-2"}).encode() + b"\n")

            pump = threading.Thread(target=_pump, args=(server,), daemon=True)
            pump.start()

            client.settimeout(5)
            message = json.loads(client.recv(8192).decode())["message"]
    finally:
        server.stop()

    assert "/Users/" not in message, f"absolute path leaked to the client: {message!r}"
    assert "secret-token" not in message, f"scene-data repr leaked to the client: {message!r}"
    assert "Traceback" not in message, f"traceback leaked to the client: {message!r}"
    assert "_Unserializable" not in message, f"internal type name leaked to the client: {message!r}"


class _SerializedWriteProbe:
    """
    Duck-typed client socket that makes two overlapping writes observable.

    `sendall` parks for `hold_seconds` on its first call, so a second writer
    that is *not* excluded by a lock is caught inside the first call instead of
    racing harmlessly past it. Attributes:

        writes: Every payload handed to `sendall`, in order.
        send_started: Set as the first `sendall` is entered.
        overlapped: True if two threads were ever inside `sendall` together.
    """

    def __init__(self, first_frame: bytes, hold_seconds: float = 0.5) -> None:
        self._first_frame = first_frame
        self._hold_seconds = hold_seconds
        self._state_lock = threading.Lock()
        self._writers = 0
        self._frame_delivered = False
        self.writes: list[bytes] = []
        self.send_started = threading.Event()
        self.overlapped = False

    def settimeout(self, _timeout: float) -> None:
        """
        Accept handle_client's socket timeout.

        Args:
            _timeout: Ignored; this probe never blocks in recv.

        """

    def recv(self, _bufsize: int) -> bytes:
        """
        Deliver the seeded frame once, then behave like an idle socket.

        Args:
            _bufsize: Ignored; the whole frame is delivered in one read.

        Returns:
            bytes: The seeded frame on the first call.

        Raises:
            TimeoutError: On every later call, which handle_client treats as
                "nothing to read yet" and loops on.

        """
        if not self._frame_delivered:
            self._frame_delivered = True
            return self._first_frame
        raise TimeoutError

    def sendall(self, payload: bytes) -> None:
        """
        Record one write, flagging any concurrent writer.

        Args:
            payload: The framed bytes being written.

        """
        with self._state_lock:
            self._writers += 1
            if self._writers > 1:
                self.overlapped = True
            hold = not self.writes
            self.writes.append(payload)
        self.send_started.set()
        if hold:
            time.sleep(self._hold_seconds)
        with self._state_lock:
            self._writers -= 1

    def shutdown(self, _how: int) -> None:
        """
        Accept stop()'s socket shutdown.

        Args:
            _how: Ignored.

        """

    def close(self) -> None:
        """Accept handle_client's close in its finally block."""


def test_writes_to_one_client_are_serialized() -> None:
    """
    Only one thread may be inside sendall() for a given client at a time.

    The client handler thread writes transport-validation errors while the
    main-thread drain writes responses, and sendall() is not atomic: for a
    large response (the cap is 64 MiB) an error frame can splice into the
    middle of it and desync a stream both sides parse by newline framing.
    """
    server = _make_server()
    server.running = True
    client = _SerializedWriteProbe(b"not json\n")

    handler = threading.Thread(target=server.handle_client, args=(client,), daemon=True)
    handler.start()
    try:
        assert client.send_started.wait(3.0), "the handler never sent its protocol error"

        # The handler is parked inside sendall(); a second writer now has to
        # wait rather than interleave. Stamped, because the drain loop fails
        # closed on an unstamped command and this test is about the *write*
        # serialization, not about the barrier.
        _queue(server, client, "ping", "req-3")
        server.drain_command_queue()
    finally:
        server.running = False
        handler.join(3.0)

    assert not client.overlapped, "two threads wrote to one client socket concurrently"
    frames = [json.loads(line) for line in b"".join(client.writes).split(b"\n") if line]
    assert [frame["status"] for frame in frames] == ["error", "success"], (
        f"both frames must arrive whole, the handler's protocol error first: {frames}"
    )


def test_restart_rebinds_port_cleanly() -> None:
    """A stopped server must fully release the port for the next start()."""
    port = _free_port()

    first = BlenderMCPServer(port=port)
    first.execute_command = lambda command: {"status": "success", "result": {}}
    first.start()
    with socket.create_connection(("localhost", port), timeout=5):
        pass
    first.stop()

    second = BlenderMCPServer(port=port)
    second.execute_command = lambda command: {
        "status": "success",
        "result": {"echo": command.get("type")},
    }
    second.start()
    try:
        with socket.create_connection(("localhost", port), timeout=5) as client:
            client.sendall(json.dumps({"type": "ping"}).encode() + b"\n")
            pump = threading.Thread(target=_pump, args=(second,), daemon=True)
            pump.start()
            client.settimeout(5)
            response = json.loads(client.recv(8192).decode())
        assert response["status"] == "success"
    finally:
        second.stop()


# ---------------------------------------------------------------------------
# The file-swap barrier
# ---------------------------------------------------------------------------


class _RecordingClient:
    """
    Duck-typed client socket that keeps every frame written to it.

    Attributes:
        writes: Every payload handed to `sendall`, in order.

    """

    def __init__(self) -> None:
        """Start with nothing written."""
        self.writes: list[bytes] = []

    def sendall(self, payload: bytes) -> None:
        """
        Record one write.

        Args:
            payload: The framed bytes being written.

        """
        self.writes.append(payload)

    def frames(self) -> list[dict]:
        """
        Decode everything written so far.

        Returns:
            list[dict]: One decoded response per frame, in write order.

        """
        return [json.loads(line) for line in b"".join(self.writes).split(b"\n") if line]


def _advertise(server: object, *cmd_types: str) -> None:
    """
    Give a harness server a dispatch table, since the real one needs real mixins.

    `_build_command_handlers` reads ~200 bound methods off the mixins, which are
    empty stand-ins here (see `_load_server_class`). The barrier checks handler
    membership before it fires, so every barrier test has to say which commands
    this server can actually dispatch.

    Args:
        server: The server to install the table on.
        *cmd_types: The command names it should claim to handle.

    """
    table = dict.fromkeys(cmd_types, lambda **_params: {"status": "success"})
    table["ping"] = lambda **_params: {"status": "success"}
    server._build_command_handlers = lambda: table


def _make_swap_server() -> tuple[object, list[str | None]]:
    """
    Build a server whose `open_shot` really swaps the stub database.

    The swap runs through the same `bpy.ops.wm.open_mainfile` stub the harness
    installed, so a successful swap fires the real `session.py` `load_post`
    handler and moves the real epoch - which is what the barrier's error message
    reports.

    Returns:
        tuple: The server, and the list every executed command type is appended
        to. Asserting on that list is the only way to tell "answered with an
        error" apart from "ran and then answered with an error".

    """
    server = BlenderMCPServer(port=_free_port())
    executed: list[str | None] = []
    # The barrier is gated on handler membership, so a harness that stubs
    # `execute_command` has to stub the dispatch table with it - exactly as the
    # live rig's in-Blender half installs `open_shot` before asserting on it.
    # The real table cannot be built here: the mixins are empty stand-ins.
    _advertise(server, "open_shot")
    executed_commands = executed

    def execute_command(command: dict) -> dict:
        cmd_type = command.get("type")
        executed_commands.append(cmd_type)
        if cmd_type != "open_shot":
            return {"status": "success", "result": {"echo": cmd_type}}
        filepath = command.get("params", {}).get("filepath", "")
        try:
            result = _window_manager_ops.open_mainfile(filepath=filepath, use_scripts=False)
        except RuntimeError:
            return {"status": "error", "message": "Could not open the requested shot"}
        return {"status": "success", "result": {"operator_result": sorted(result)}}

    server.execute_command = execute_command
    server.running = True
    return server, executed


def _queue(server: object, client: object, cmd_type: str, request_id: str, **params: object) -> None:
    """
    Put one command on the queue the way a client thread would.

    **Stamped through production's own `_stamp_session`**, because that is what
    "the way a client thread would" means now that the drain loop fails closed on
    an unstamped command. Calling the production stamper rather than writing the
    key here is deliberate: a harness that builds its own stamp would keep
    passing if the key or the marker's shape changed, which is the whole class of
    defect the stamp exists to catch.

    Args:
        server: The server whose queue to fill.
        client: The socket the response must go back to.
        cmd_type: The command type.
        request_id: The id the response has to echo.
        **params: The command's parameters.

    """
    command = {"type": cmd_type, "id": request_id, "params": params}
    server._stamp_session(command)
    server.command_queue.put_nowait((command, client))


def _epoch() -> int:
    """
    Read the live session epoch out of the real session module.

    Returns:
        int: The current epoch.

    """
    return _session.session_snapshot()["session_epoch"]


def test_a_swap_is_the_last_command_its_tick_executes() -> None:
    """
    Commands queued behind a swap must be answered, never run against the new file.

    Measured on Blender 5.2.2 (Task 2 Step 7): the drain loop kept draining after
    `wm.open_mainfile` returned, inside the same tick, and answered a command
    queued against the *old* shot from the *new* file with `status: success` and
    nothing marking the change. Asserting on `executed` rather than on the
    responses is the whole point - a response-only assertion passes even when
    the command ran.
    """
    server, executed = _make_swap_server()
    client = _RecordingClient()

    _queue(server, client, "cmd_a", "a")
    _queue(server, client, "open_shot", "swap", filepath="/shots/sq010.blend")
    _queue(server, client, "cmd_b", "b")
    _queue(server, client, "cmd_c", "c")

    server.drain_command_queue()

    assert executed == ["cmd_a", "open_shot"], f"a command behind the swap was executed: {executed}"
    by_id = {frame["id"]: frame for frame in client.frames()}
    assert by_id["a"]["status"] == "success"
    assert by_id["swap"]["status"] == "success"
    assert by_id["b"]["status"] == "error"
    assert by_id["c"]["status"] == "error"


def test_a_failed_swap_also_discards_the_commands_queued_behind_it() -> None:
    """
    At dequeue time the outcome is not yet known, so the barrier cannot wait for it.

    The commands behind the swap were queued against an assumption - "the shot I
    named is still open" - that the *attempt* already put in doubt. The epoch is
    unchanged here (a failed open leaves the database untouched, measured across
    every open failure mode), so the barrier cannot be driven by epoch movement.
    """
    server, executed = _make_swap_server()
    client = _RecordingClient()
    before = _epoch()

    _queue(server, client, "open_shot", "swap", filepath="/shots/gone.missing.blend")
    _queue(server, client, "cmd_b", "b")

    server.drain_command_queue()

    assert executed == ["open_shot"], f"a command behind a failed swap was executed: {executed}"
    assert _epoch() == before, "a failed swap must not move the epoch"
    by_id = {frame["id"]: frame for frame in client.frames()}
    assert by_id["swap"]["status"] == "error"
    assert by_id["b"]["status"] == "error"


def test_the_barrier_message_names_the_epoch_without_claiming_it_moved() -> None:
    """
    One wording has to be true on both paths, so it states the epoch, not a change.

    On a successful swap the client compares the named epoch against the one it
    cached, sees a difference and re-handshakes. On a failed swap it sees the
    same number and correctly skips a pointless re-handshake across every
    connected process, simply resending. Wording that hard-codes "the epoch
    changed" would be a lie on the second path.
    """
    messages = {}
    for label, filepath in (("ok", "/shots/sq010.blend"), ("fail", "/shots/gone.missing.blend")):
        server, _executed = _make_swap_server()
        client = _RecordingClient()
        _queue(server, client, "open_shot", "swap", filepath=filepath)
        _queue(server, client, "cmd_b", "b")

        server.drain_command_queue()

        message = next(frame["message"] for frame in client.frames() if frame["id"] == "b")
        messages[label] = message
        assert str(_epoch()) in message, f"the {label} path must name the current epoch: {message!r}"

    for label, message in messages.items():
        lowered = message.lower()
        assert "changed" not in lowered, f"the {label} path asserts a change it cannot know about: {message!r}"
        assert "swap" in lowered, f"the {label} path must say why the command was discarded: {message!r}"


def test_the_barrier_message_leaks_no_filesystem_path() -> None:
    """
    Blender embeds absolute paths in its own error text; this message must carry none.

    The swap's filepath is right there in the command being processed, which is
    exactly how it would end up in a rejection written without thinking about it.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()

    _queue(server, client, "open_shot", "swap", filepath="/Users/someone/secret-project/sq010.blend")
    _queue(server, client, "cmd_b", "b")

    server.drain_command_queue()

    message = next(frame["message"] for frame in client.frames() if frame["id"] == "b")
    assert "/Users/" not in message, f"absolute path leaked to the client: {message!r}"
    assert "secret-project" not in message, f"the swap's path leaked to the client: {message!r}"
    assert not any(part.startswith("/") for part in message.split()), f"path-shaped token leaked: {message!r}"


def test_a_command_that_arrives_after_the_swap_is_serviced_normally() -> None:
    """The barrier discards a stale batch; it must not wedge the server."""
    server, executed = _make_swap_server()
    client = _RecordingClient()

    _queue(server, client, "open_shot", "swap", filepath="/shots/sq010.blend")
    server.drain_command_queue()

    _queue(server, client, "cmd_after", "after")
    server.drain_command_queue()

    assert executed == ["open_shot", "cmd_after"]
    assert next(frame for frame in client.frames() if frame["id"] == "after")["status"] == "success"


def test_the_epoch_moves_once_per_successful_swap_and_not_at_all_on_a_failed_one() -> None:
    """
    Driven through the drain loop rather than the handler, because that is the path.

    `tests/test_session_state.py` pins the handler's own arithmetic; this pins
    that a command going through the real dispatch loop reaches it exactly once.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()
    before = _epoch()

    _queue(server, client, "open_shot", "swap-1", filepath="/shots/sq010.blend")
    server.drain_command_queue()
    after_success = _epoch()

    _queue(server, client, "open_shot", "swap-2", filepath="/shots/gone.missing.blend")
    server.drain_command_queue()

    assert after_success == before + 1, "a successful swap must move the epoch exactly once"
    assert _epoch() == after_success, "a failed swap must not move the epoch at all"


def _read_frames(sock: socket.socket, expected: int, deadline: float) -> list[dict]:
    """
    Read until `expected` frames have arrived or `deadline` passes.

    Args:
        sock: The connected socket to read from.
        expected: How many newline-delimited frames to wait for.
        deadline: `time.time()` value after which to give up.

    Returns:
        list[dict]: Every frame decoded so far. Short of `expected` means the
        client was left waiting, which is the hang this asserts against.

    """
    buffer = b""
    frames: list[dict] = []
    while len(frames) < expected and time.time() < deadline:
        sock.settimeout(max(0.05, min(0.5, deadline - time.time())))
        try:
            chunk = sock.recv(8192)
        except TimeoutError:
            continue
        if not chunk:
            break
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if line:
                frames.append(json.loads(line))
    return frames


# Five commands per socket across two sockets, of which a3 and a4 sit behind the swap.
_BATCH_SIZE = 10
_DISCARDED_BY_THE_SWAP = 2


def test_every_command_spanning_a_swap_is_answered_on_both_sockets() -> None:
    """
    No client is left waiting - asserted by counting responses, not by argument.

    README documents multiple MCP server processes against one Blender, so "one
    client at a time" is not an invariant this design may assume: the barrier has
    to answer commands belonging to a process that never sent the swap. Both
    sockets' frames are written before either is read, so the whole batch is
    queued before the drain begins.
    """
    server = BlenderMCPServer(port=_free_port())
    _advertise(server, "open_shot")

    def execute_command(command: dict) -> dict:
        cmd_type = command.get("type")
        if cmd_type != "open_shot":
            return {"status": "success", "result": {"echo": cmd_type}}
        _window_manager_ops.open_mainfile(filepath=command.get("params", {}).get("filepath", ""), use_scripts=False)
        return {"status": "success", "result": {"swapped": True}}

    server.execute_command = execute_command
    server.start()
    try:
        with (
            socket.create_connection(("localhost", server.port), timeout=5) as first,
            socket.create_connection(("localhost", server.port), timeout=5) as second,
        ):
            sent = {
                first: ["a1", "a2", "swap", "a3", "a4"],
                second: ["b1", "b2", "b3", "b4", "b5"],
            }
            for sock, ids in sent.items():
                for request_id in ids:
                    cmd_type = "open_shot" if request_id == "swap" else "ping"
                    frame = {"type": cmd_type, "id": request_id, "params": {"filepath": "/shots/sq020.blend"}}
                    sock.sendall(json.dumps(frame).encode() + b"\n")

            queued_by = time.time() + 5.0
            while server.command_queue.qsize() < _BATCH_SIZE and time.time() < queued_by:
                time.sleep(0.01)
            assert server.command_queue.qsize() == _BATCH_SIZE, "the batch has to be queued before the drain starts"

            pump = threading.Thread(target=_pump, args=(server,), daemon=True)
            pump.start()

            deadline = time.time() + 5.0
            answered = {sock: _read_frames(sock, len(ids), deadline) for sock, ids in sent.items()}
    finally:
        server.stop()

    for sock, ids in sent.items():
        frames = answered[sock]
        assert len(frames) == len(ids), f"{len(ids) - len(frames)} command(s) were never answered: {frames}"
        assert sorted(frame["id"] for frame in frames) == sorted(ids), "every id must be answered exactly once"

    all_frames = [frame for frames in answered.values() for frame in frames]
    discarded = [frame for frame in all_frames if frame["status"] == "error"]
    assert len(discarded) >= _DISCARDED_BY_THE_SWAP, (
        "a3 and a4 were queued behind the swap and must have been discarded"
    )


# ---------------------------------------------------------------------------
# The enqueue-time stamp: the half of the barrier a snapshot cannot cover
# ---------------------------------------------------------------------------


def _decode(server: object, client: object, cmd_type: str, request_id: str, **params: object) -> None:
    """
    Queue one command through the **production** enqueue path, stamp and all.

    `_queue` above injects straight into the queue, which is what a test that
    only cares about the snapshot needs. This one goes through
    `_decode_and_queue_frame`, so the command carries the epoch it was queued
    under - the thing the dequeue comparison is made against.

    Args:
        server: The server whose queue to fill.
        client: The socket the response must go back to.
        cmd_type: The command type.
        request_id: The id the response has to echo.
        **params: The command's parameters.

    """
    frame = json.dumps({"type": cmd_type, "id": request_id, "params": params}).encode("utf-8")
    assert server._decode_and_queue_frame(frame, client) is True


def test_a_command_queued_before_a_swap_that_lands_elsewhere_is_rejected_at_dequeue() -> None:
    """
    The window the snapshot cannot see: a command that arrives *during* the load.

    `session_epoch` moves in `load_post`, at the **end** of the load, so a
    command enqueued mid-load carries the pre-swap epoch. `wm.open_mainfile` was
    measured at 4.6 s on a 1.05 GB fixture (TASK_STATE decision 11) and every
    `handle_client` thread keeps running throughout it, including threads
    belonging to a second server *process* that cannot possibly know a swap is
    under way. A pre-swap queue snapshot is taken before any of that and misses
    all of it; only a stamp compared at dequeue can reject it.

    This is the design recorded at `docs/superpowers/plans/PHASE2_TASK_STATE.md:2265-2275`.
    """
    server, executed = _make_swap_server()
    client = _RecordingClient()

    _decode(server, client, "cmd_mid_load", "mid")
    # The load completes while that command sits in the queue: load_post fires,
    # the epoch moves, and the command's stamp is now stale.
    _window_manager_ops.open_mainfile(filepath="/shots/sq030.blend", use_scripts=False)

    server.drain_command_queue()

    assert executed == [], f"a command queued against the previous file was executed: {executed}"
    frame = next(frame for frame in client.frames() if frame["id"] == "mid")
    assert frame["status"] == "error"
    assert "swap" in frame["message"].lower(), f"the client was not told why: {frame['message']!r}"


def test_a_command_queued_after_the_swap_is_serviced_normally_under_the_stamp() -> None:
    """The stamp must reject a stale batch, not wedge every command that follows it."""
    server, executed = _make_swap_server()
    client = _RecordingClient()

    _window_manager_ops.open_mainfile(filepath="/shots/sq031.blend", use_scripts=False)
    _decode(server, client, "cmd_after", "after")

    server.drain_command_queue()

    assert executed == ["cmd_after"]
    assert next(frame for frame in client.frames() if frame["id"] == "after")["status"] == "success"


# Every enqueue spelling, not just the one the code happens to use today.
# `put_nowait` alone misses a blocking `put()`, which is a perfectly ordinary
# thing for a Task 6 re-queue path to reach for.
_ENQUEUE_METHODS = frozenset({"put", "put_nowait"})
# Every callable form a producer can hide in. `ast.FunctionDef` alone misses an
# `async def` and misses a `lambda` bound at class scope - two of the five
# shapes demonstrated to evade the previous form of this check.
_CALLABLE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _queue_producers(source: str) -> list[str]:
    """
    Name every callable in `source` that enqueues anything.

    Factored out of the test below so the same logic can be run against
    synthetic sources whose answer is known - which is the only way a *static*
    check can be shown to be falsifiable at all. The previous form of this scan
    passed its own real-source assertion while failing every synthetic one.

    Args:
        source: Python source text.

    Returns:
        list[str]: One entry per enqueue call, named by its enclosing callable.

    """
    tree = ast.parse(source)
    producers: list[str] = []
    for function in ast.walk(tree):
        if not isinstance(function, _CALLABLE_NODES):
            continue
        name = getattr(function, "name", "<lambda>")
        producers.extend(
            name
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _ENQUEUE_METHODS
        )
    return producers


# The five producer shapes a critic demonstrated evading the previous scan. Each
# one is a second producer that would have to stamp and does not; each one
# returned an empty producer list, so the assertion passed and the property it
# claimed to hold was false.
_EVASIVE_PRODUCERS = (
    ("blocking put()", "def sneak(self, item):\n    self.command_queue.put(item)\n"),
    ("async def", "async def sneak(self, item):\n    self.command_queue.put_nowait(item)\n"),
    ("local alias", "def sneak(self, item):\n    q = self.command_queue\n    q.put_nowait(item)\n"),
    ("lambda at class scope", "class C:\n    sneak = lambda self, item: self.command_queue.put_nowait(item)\n"),
    ("helper takes the queue", "def sneak(target, item):\n    target.put_nowait(item)\n"),
)


@pytest.mark.parametrize(("label", "source"), _EVASIVE_PRODUCERS)
def test_the_producer_scan_catches_every_shape_that_evaded_it(label: str, source: str) -> None:
    """
    A static check nothing can fail is not a check, and this one could not fail.

    A critic ran the previous scan's exact logic against these five synthetic
    sources::

        blocking put()   async def   local alias   lambda at class scope   helper takes the queue
        -> assertion PASSES (evaded) in every case

    This is that experiment, committed, so the scan's own falsifiability is a
    test result rather than a claim about one.

    Args:
        label: Which evasion shape this row is, for the failure message.
        source: A synthetic module containing exactly one second producer.

    """
    # Named "sneak" in four of the five; the class-scope lambda has no name of
    # its own, so what is asserted is that it is *seen*, which is the property.
    assert _queue_producers(source), f"{label} still evades the producer scan"


def test_the_enqueue_path_is_the_only_producer_and_it_stamps() -> None:
    """
    The runtime fails closed on an unstamped command; this keeps the closed branch unreachable.

    The drain loop now **rejects** a command carrying no stamp, so a second
    producer can no longer run one against a database it was not sent for - it
    gets an error frame instead. This check is therefore a second line of
    defence rather than the only one, and it is written to actually hold:
    a critic ran the previous version's exact logic against synthetic sources
    and **all five** producer shapes evaded it::

        blocking put()   async def   local alias   lambda at class scope   helper takes the queue
        -> assertion PASSES (evaded) in every case

    It walked `ast.FunctionDef` only (missing `AsyncFunctionDef` and lambdas),
    matched only `.put_nowait` (missing a blocking `.put`), and required the
    receiver spelled `<x>.command_queue` (missing an alias and a queue passed as
    an argument). The last two are exactly what a Task 6 re-queue path looks
    like.

    So the receiver is **not** inspected at all. Every `.put` / `.put_nowait`
    call anywhere in `server_core.py`, inside any callable, is a producer as far
    as this check is concerned, and `self.command_queue` is the only queue in the
    module - so there is nothing for that strictness to falsely accuse. An alias,
    a helper taking the queue as an argument, a lambda and an `async def` are all
    caught by the same rule, because none of them can enqueue without spelling
    one of those two method names somewhere.
    """
    text = SERVER_CORE.read_text(encoding="utf-8")
    assert _queue_producers(text) == ["_decode_and_queue_frame"], "the queue has producers that may not stamp"
    tree = ast.parse(text)
    source = ast.get_source_segment(
        text,
        next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_decode_and_queue_frame"
        ),
    )
    # The *call*, not the name: the function's own comment block cites
    # `_stamp_session` by name, so a bare name check passes on a body that
    # no longer calls it.
    assert "self._stamp_session(command)" in str(source), "the one producer does not stamp the command it queues"


def test_the_stamp_is_read_without_touching_bpy_on_the_client_thread() -> None:
    """
    `_decode_and_queue_frame` runs on a client thread, where `bpy` is forbidden.

    The stamp is a plain `int` and `str` off module state, which is why reading
    it there is legal. A `bpy` attribute in this function would be the bug the
    whole addon/main-thread split exists to prevent, and it would not fail any
    other test in this file - the stub `bpy` answers happily.
    """
    tree = ast.parse(SERVER_CORE.read_text(encoding="utf-8"))
    function = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_decode_and_queue_frame"
    )
    touched = [node.id for node in ast.walk(function) if isinstance(node, ast.Name) and node.id in {"bpy", "mathutils"}]
    assert not touched, f"the enqueue path touches Blender state off the main thread: {touched}"


# ---------------------------------------------------------------------------
# The barrier only fires for a swap the addon can actually dispatch
# ---------------------------------------------------------------------------


def test_a_swap_command_the_addon_cannot_dispatch_discards_nobodys_batch() -> None:
    """
    `open_shot` does not exist until Task 6, and an unknown command must not be a weapon.

    Until then, one frame naming `open_shot` would make the addon throw away up
    to `_MAX_QUEUED_COMMANDS` commands belonging to **other** server processes
    and answer the sender with "Unknown command type" - a denial of service
    costing the attacker a single 40-byte write. The barrier is therefore gated
    on handler membership, checked before the queue is touched.
    """
    server = BlenderMCPServer(port=_free_port())
    executed: list[str | None] = []

    def execute_command(command: dict) -> dict:
        cmd_type = command.get("type")
        executed.append(cmd_type)
        if cmd_type == "open_shot":
            return {"status": "error", "message": "Unknown command type: open_shot"}
        return {"status": "success", "result": {"echo": cmd_type}}

    server.execute_command = execute_command
    server.running = True
    _advertise(server, "victim")
    assert "open_shot" not in server._build_command_handlers(), (
        "the premise of this test is that open_shot has no handler yet"
    )
    client = _RecordingClient()

    _queue(server, client, "open_shot", "swap")
    _queue(server, client, "victim", "victim")

    server.drain_command_queue()

    assert executed == ["open_shot", "victim"], f"another process's command was discarded by a typo: {executed}"
    assert next(frame for frame in client.frames() if frame["id"] == "victim")["status"] == "success"


# ---------------------------------------------------------------------------
# Liveness: the rejection path cannot pin Blender's main thread
# ---------------------------------------------------------------------------


class _StalledClient:
    """
    A peer that accepts no bytes - SIGSTOPped, paused in a debugger, or half-open.

    `handle_client` sets `settimeout(1.0)` and CPython applies that as a whole
    `sendall` deadline, so each rejection frame to such a peer costs a full
    second unless the rejection path sets its own.

    Attributes:
        timeouts: Every value `settimeout` was called with, in order.
        shut_down: Whether the server gave up on this peer and closed it.

    """

    def __init__(self) -> None:
        """Start unstalled, with nothing recorded."""
        self.timeouts: list[float | None] = []
        self.shut_down = False

    def settimeout(self, value: float | None) -> None:
        """
        Record a timeout change.

        Args:
            value: The timeout the server asked for.

        """
        self.timeouts.append(value)

    def sendall(self, _payload: bytes) -> None:
        """
        Block for as long as the current timeout allows, then fail like a real socket.

        Args:
            _payload: Ignored.

        Raises:
            TimeoutError: Always - this peer never accepts anything.

        """
        time.sleep(self.timeouts[-1] if self.timeouts and self.timeouts[-1] else 1.0)
        raise TimeoutError("stalled peer")

    def shutdown(self, _how: int) -> None:
        """
        Record that the server gave up on this peer.

        Args:
            _how: Ignored.

        """
        self.shut_down = True

    def close(self) -> None:
        """Record that the server closed this peer."""
        self.shut_down = True


class _SlowButHealthyClient:
    """
    A peer that reads, just not instantly - the case a 50 ms threshold destroyed.

    `_discard_superseded`'s own docstring names "backpressured by a previous
    large paginated response" as a reason a peer accepts bytes slowly, and the
    first threshold closed exactly that peer's connection, forfeiting every other
    command it had queued.

    Attributes:
        writes: Every payload accepted, in order.
        shut_down: Whether the server gave up on this peer.
        delay: How long each accepted write takes.

    """

    def __init__(self, delay: float = 0.06) -> None:
        """
        Start healthy, reading at `delay` seconds per frame.

        Args:
            delay: Seconds each write takes before completing.

        """
        self.writes: list[bytes] = []
        self.timeouts: list[float | None] = []
        self.shut_down = False
        self.delay = delay

    def settimeout(self, value: float | None) -> None:
        """
        Record a timeout change.

        Args:
            value: The timeout the server asked for.

        """
        self.timeouts.append(value)

    def sendall(self, payload: bytes) -> None:
        """
        Accept one frame slowly, failing only if the current timeout is shorter.

        **The `timeout == 0` special case is gone, and its removal is the point.**
        It made a non-blocking write succeed unconditionally, which is not what a
        non-blocking socket does: a write that cannot complete immediately raises
        `BlockingIOError`, and `BlockingIOError` is not a `TimeoutError`, so the
        peer's own `handle_client` thread took it as a dead connection and closed
        the socket. Production no longer asks for `settimeout(0)` at all -
        `_PAST_BUDGET_SEND_TIMEOUT_SECONDS` is a 1 ms floor - so the branch was
        modelling a call that can no longer be made, and modelling it wrongly.

        Args:
            payload: The framed bytes.

        Raises:
            TimeoutError: When the server allowed less time than this peer needs.

        """
        allowed = self.timeouts[-1] if self.timeouts else None
        if allowed is not None and 0 < allowed < self.delay:
            time.sleep(allowed)
            raise TimeoutError("slow peer, shorter deadline")
        time.sleep(self.delay)
        self.writes.append(payload)

    def frames(self) -> list[dict]:
        """
        Decode everything accepted so far.

        Returns:
            list[dict]: One decoded response per frame, in write order.

        """
        return [json.loads(line) for line in b"".join(self.writes).split(b"\n") if line]

    def shutdown(self, _how: int) -> None:
        """
        Record that the server gave up on this peer.

        Args:
            _how: Ignored.

        """
        self.shut_down = True

    def close(self) -> None:
        """Record that the server closed this peer."""
        self.shut_down = True


def test_a_healthy_peer_queued_behind_stalled_ones_is_still_answered() -> None:
    """
    `if deadline_passed or not self._send_bounded(...)` short-circuits, so the send never happened.

    Once the global batch budget was spent, every remaining client was closed
    with **zero** write attempts and its health never tested; five slow peers in
    front of one healthy one are enough, which is the queue this test builds.
    Restore the short-circuit (revert-matrix row "`or` short-circuits again") and
    the healthy peer below receives nothing and is closed.

    That is §07's dropped-healthy-socket item, and it also defeated the
    rejection's own purpose: this frame is the only carrier of the
    `session_id`/`session_epoch` pair, so the peers the barrier dropped were
    exactly the ones that never learned to re-handshake.

    Nothing in the suite asserted this, because
    `test_rejecting_a_full_queue_to_a_stalled_peer_is_bounded` sends all 255
    rejections to **one** stalled peer - so its elapsed-time assertion was
    satisfied *by* the bug, which abandoned 250 of them unsent.

    **The peer here drains in well under a millisecond, and the number is not
    arbitrary.** Past the batch budget the write is made under
    `_PAST_BUDGET_SEND_TIMEOUT_SECONDS`, a 1 ms floor that exists because
    `settimeout(0)` takes the socket out of timeout mode and its own
    `handle_client` thread then takes `BlockingIOError` as a disconnect. A real
    peer with room in its send buffer takes ~0 s for a 250-byte frame whatever
    its read rate, because the kernel buffers it; this stub charges its `delay`
    for every write instead, so the delay here is set to the case the stub can
    represent. The sibling test
    `test_a_peer_slower_than_a_loopback_reader_is_not_destroyed_for_it` is where
    a genuinely slow, *within-budget* peer is pinned at 60 ms, and that is what
    keeps `_REJECTION_SEND_TIMEOUT_SECONDS` falsifiable.
    """
    server, _executed = _make_swap_server()
    stalled = [_StalledClient() for _ in range(5)]
    healthy = _SlowButHealthyClient(delay=0.0002)
    swap_client = _RecordingClient()

    _queue(server, swap_client, "open_shot", "swap", filepath="/shots/sq050.blend")
    for index, peer in enumerate(stalled):
        _queue(server, peer, "ping", f"stalled{index}")
    _queue(server, healthy, "ping", "victim")

    server.drain_command_queue()

    frames = healthy.frames()
    assert len(frames) == 1, f"the healthy peer behind five stalled ones got {len(frames)} frames"
    assert frames[0]["id"] == "victim"
    assert frames[0]["status"] == "error"
    assert "session_epoch" in frames[0], "the frame the barrier drops is the only carrier of the marker"
    assert not healthy.shut_down, "a healthy peer was closed without a single write being attempted"


def test_a_peer_slower_than_a_loopback_reader_is_not_destroyed_for_it() -> None:
    """
    50 ms was a performance target being used as a health threshold.

    A peer taking 60 ms to drain its buffer - the delay this test's stub uses -
    received zero frames and had its connection destroyed, forfeiting every
    other command it had queued, while
    `_discard_superseded`'s own docstring listed "backpressured by a previous
    large paginated response" as a cause, i.e. it knew a healthy peer can be slow
    and closed it anyway.
    """
    server, _executed = _make_swap_server()
    slow = _SlowButHealthyClient(delay=0.06)
    swap_client = _RecordingClient()

    _queue(server, swap_client, "open_shot", "swap", filepath="/shots/sq051.blend")
    _queue(server, slow, "ping", "slow")

    server.drain_command_queue()

    assert len(slow.frames()) == 1, "a 60ms reader was treated as unreachable"
    assert not slow.shut_down, "a 60ms reader had its connection destroyed"


# `_send_bounded` borrows the socket's timeout and hands it back, so one write
# attempt shows as two `settimeout` calls.
_SETTIMEOUT_CALLS_PER_ATTEMPT = 2
# Longer than every bound the rejection path promises, so a build that waits for
# the lock is visibly distinguishable from one that gives up on it. Released by
# the holder rather than left held: an unbounded `acquire` inside the drain would
# otherwise deadlock the whole run instead of failing an assertion.
_LOCK_HELD_SECONDS = 2.0


def test_a_malformed_frame_arriving_mid_rejection_cannot_park_the_main_thread() -> None:
    """
    `with send_lock:` has no timeout, so the rejection path's own bounds did not hold.

    The other writer is `_send_protocol_error`, on a `handle_client` thread whose
    socket timeout is `_CLIENT_SOCKET_TIMEOUT_SECONDS`, so one malformed frame
    arriving during a rejection batch parked Blender's main thread for as long as
    that thread held the lock - *inside* a call whose docstring asserted a 50 ms
    cap.

    The lock is held from another thread for longer than any bound this path
    promises, and the assertion is that the rejection gives up on it rather than
    waiting it out.
    """
    server, _executed = _make_swap_server()
    victim = _RecordingClient()
    swap_client = _RecordingClient()
    held = threading.Lock()
    with server._clients_lock:
        server._clients[victim] = held

    _queue(server, swap_client, "open_shot", "swap", filepath="/shots/sq052.blend")
    _queue(server, victim, "ping", "blocked")

    holder_ready = threading.Event()

    def hold_the_write_lock() -> None:
        """Stand in for a `handle_client` thread parked inside `sendall`."""
        with held:
            holder_ready.set()
            time.sleep(_LOCK_HELD_SECONDS)

    holder = threading.Thread(target=hold_the_write_lock, daemon=True)
    holder.start()
    assert holder_ready.wait(3.0), "the holder thread never took the lock"

    started = time.monotonic()
    server.drain_command_queue()
    elapsed = time.monotonic() - started
    holder.join(_LOCK_HELD_SECONDS + 2.0)

    bound = server._REJECTION_TIME_BUDGET_SECONDS + 2 * server._REJECTION_SEND_TIMEOUT_SECONDS
    assert elapsed < bound + 0.5, f"a held write lock pinned the main thread for {elapsed:.4f}s"
    assert victim.writes == [], "the frame went out while another thread held the write lock"
    assert victim not in server._clients, "a peer that could not be answered must be dropped, not left silent"


class _UnrestorableClient(_RecordingClient):
    """
    A peer that accepts a frame but will not take its own timeout back afterwards.

    Attributes:
        shut_down: Whether the server dropped this peer.

    """

    def __init__(self) -> None:
        """Start with nothing written and no timeout set."""
        super().__init__()
        self.shut_down = False
        self._borrowed = False

    def settimeout(self, _value: float | None) -> None:
        """
        Accept the borrow, then refuse to hand the socket back.

        Args:
            _value: The timeout the server asked for; this stub refuses either way.

        Raises:
            OSError: On the restoring call only.

        """
        if self._borrowed:
            raise OSError("cannot restore the socket's own timeout")
        self._borrowed = True

    def shutdown(self, _how: int) -> None:
        """
        Record that the server gave up on this peer.

        Args:
            _how: Ignored.

        """
        self.shut_down = True

    def close(self) -> None:
        """Record that the server closed this peer."""
        self.shut_down = True


def test_a_socket_whose_timeout_cannot_be_restored_is_dropped_not_left_spinning() -> None:
    """
    `_send_bounded` returned True on a failed restore, leaving the peer at the borrowed value.

    That socket stays in the registry with its `handle_client` thread's `recv`
    loop running at the rejection path's timeout instead of its own - a spin at
    20 Hz for the life of the process at the old 50 ms, and a fully non-blocking
    socket at the post-deadline value of 0. Reporting the send as not delivered
    routes it to `_abandon_unreachable_client`, which closes it: a peer whose
    timeout cannot be set is one this server can no longer talk to on the terms
    the rest of the code assumes.
    """
    server, _executed = _make_swap_server()
    victim = _UnrestorableClient()
    swap_client = _RecordingClient()

    _queue(server, swap_client, "open_shot", "swap", filepath="/shots/sq053.blend")
    _queue(server, victim, "ping", "unrestorable")

    server.drain_command_queue()

    assert victim.shut_down, "a socket left at the rejection path's timeout was kept in the registry"
    assert victim not in server._clients


def test_a_command_that_reached_the_queue_unstamped_is_rejected_not_run() -> None:
    """
    An unstamped command used to execute, on the strength of a static check that did not hold.

    `_stamp_session` is the sole producer and the AST scan asserts it, but five
    producer shapes were demonstrated to evade that scan's previous form - two of
    them exactly what a Task 6 re-queue path looks like. Failing open bought
    nothing, because in a correct tree this branch is unreachable; failing closed
    turns "ran against a database it was not sent for" into one error frame.
    """
    server, executed = _make_swap_server()
    client = _RecordingClient()
    # Straight onto the queue with no stamp, which is what a second producer
    # that forgot to stamp would produce.
    server.command_queue.put_nowait(({"type": "ping", "id": "unstamped", "params": {}}, client))

    server.drain_command_queue()

    assert executed == [], f"an unstamped command executed: {executed}"
    frame = client.frames()[0]
    assert frame["id"] == "unstamped"
    assert frame["status"] == "error"
    assert "session stamp" in frame["message"], f"the rejection must say why, not claim a swap: {frame['message']}"
    assert frame["session_epoch"] == _epoch()


def test_rejecting_a_full_queue_to_a_stalled_peer_is_bounded() -> None:
    """
    Before any bound existed this was `_MAX_QUEUED_COMMANDS` x the socket's own timeout.

    `_run_session_swap` drained the whole queue and `_discard_superseded` sent
    one frame per entry with no deadline, each costing
    `_CLIENT_SOCKET_TIMEOUT_SECONDS` - 256 x 1.0 s, arithmetic rather than a
    stopwatch reading. Blender's UI is frozen throughout and every other
    connected process blows its own 180 s client timeout having received neither
    a response nor an error - §07's "any tested path where a client receives no
    response and no error".
    """
    server, _executed = _make_swap_server()
    stalled = _StalledClient()
    swap_client = _RecordingClient()

    # The swap goes in first: the queue is FIFO, so everything behind it is
    # what `_run_session_swap` drains and `_discard_superseded` must answer.
    # One slot short of the cap, because the swap itself takes the last one.
    _queue(server, swap_client, "open_shot", "swap", filepath="/shots/sq040.blend")
    for index in range(server._MAX_QUEUED_COMMANDS - 1):
        _queue(server, stalled, "ping", f"q{index}")

    started = time.monotonic()
    server.drain_command_queue()
    elapsed = time.monotonic() - started

    budget = server._REJECTION_TIME_BUDGET_SECONDS + server._REJECTION_SEND_TIMEOUT_SECONDS
    assert elapsed < budget + 1.0, f"the rejection path pinned the main thread for {elapsed:.4f}s"
    assert stalled.shut_down, "a peer the server could not answer was left waiting in recv() with no EOF"
    assert server._REJECTION_SEND_TIMEOUT_SECONDS in stalled.timeouts, (
        f"the rejection send used the socket's own 1.0s timeout: {stalled.timeouts}"
    )
    # Without the abandoned set, all 255 frames are re-offered to a socket this
    # pass has already closed - a syscall each and, before the deadline, a
    # timeout each.
    attempts = len(stalled.timeouts) // _SETTIMEOUT_CALLS_PER_ATTEMPT
    assert attempts <= 1, f"an abandoned peer was written to {attempts} times: {stalled.timeouts}"


def test_rejecting_a_full_queue_to_distinct_stalled_peers_is_bounded() -> None:
    """
    The sibling above sends all 255 frames to **one** peer, which is abandoned on the first write.

    That makes it structurally blind to the cost this test measures. Once a peer
    fails, `_discard_superseded` skips every later entry belonging to it, so the
    one-peer case exercises exactly **one** past-budget send however full the
    queue is. The multi-process case the plan requires this barrier to hold for
    (`README.md:85-89`) is the opposite shape: up to `_MAX_QUEUED_COMMANDS`
    entries belonging to as many different peers, each of which costs a separate
    attempt that no `abandoned` lookup can skip.

    **What that attempt costs, as arithmetic.** Past the time budget the send is
    still made, under `_PAST_BUDGET_SEND_TIMEOUT_SECONDS`. That is a 1 ms floor,
    not zero, and it is spent **twice** - `_send_frame` takes the per-client
    write lock under the same value before `sendall` blocks under it - so the
    tail is bounded by `2 * _PAST_BUDGET_SEND_TIMEOUT_SECONDS * _MAX_QUEUED_COMMANDS`
    and not by nothing. Three committed docstrings called it "non-blocking";
    they now name this number instead.

    The structural assertions carry the meaning and the stopwatch only guards
    the order of magnitude: on a contended box a 1 ms sleep is not a 1 ms sleep,
    which is what `scripts/quiet_box.py` exists to say out loud.
    """
    server, _executed = _make_swap_server()
    swap_client = _RecordingClient()
    # One peer per queued command, so nothing can be skipped as already
    # abandoned - the property the one-peer sibling cannot express.
    peers = [_StalledClient() for _ in range(server._MAX_QUEUED_COMMANDS - 1)]

    _queue(server, swap_client, "open_shot", "swap", filepath="/shots/sq040.blend")
    for index, peer in enumerate(peers):
        _queue(server, peer, "ping", f"q{index}")

    started = time.monotonic()
    server.drain_command_queue()
    elapsed = time.monotonic() - started

    worst_case = (
        server._REJECTION_TIME_BUDGET_SECONDS
        + 2 * server._REJECTION_SEND_TIMEOUT_SECONDS
        + 2 * server._PAST_BUDGET_SEND_TIMEOUT_SECONDS * server._MAX_QUEUED_COMMANDS
    )
    assert elapsed < worst_case + 1.0, (
        f"{len(peers)} distinct stalled peers pinned the main thread for {elapsed:.4f}s, "
        f"against a documented worst case of {worst_case:.3f}s"
    )
    # Every peer is *attempted* - the budget bounds how long a send may block,
    # never whether one is made - and every peer that could not be answered is
    # closed, so none is left in recv() with neither a response nor an error.
    unattempted = [index for index, peer in enumerate(peers) if not peer.timeouts]
    assert not unattempted, f"{len(unattempted)} peers were never written to at all: {unattempted[:8]}"
    silent = [index for index, peer in enumerate(peers) if not peer.shut_down]
    assert not silent, f"{len(silent)} peers were left waiting with no response and no EOF: {silent[:8]}"
    over_attempted = [
        index for index, peer in enumerate(peers) if len(peer.timeouts) // _SETTIMEOUT_CALLS_PER_ATTEMPT > 1
    ]
    assert not over_attempted, f"peers written to more than once: {over_attempted[:8]}"


# More refills than `_MAX_QUEUED_COMMANDS`, so an unbounded drain visibly exceeds it.
_REFILLS = 400


def test_the_pre_swap_drain_is_bounded_by_an_explicit_count() -> None:
    """
    `while True: get_nowait()` races producers that free slots as it drains.

    The docstring claimed the result was "bounded by `_MAX_QUEUED_COMMANDS`" and
    nothing enforced it, so a producer keeping the queue fed could make one tick
    take an unbounded number of entries. The bound is now the loop's own.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()
    refills = {"left": _REFILLS}

    class _RefillingQueue:
        """A queue that a concurrent producer keeps topping up as it is drained."""

        def __init__(self, inner: object) -> None:
            self._inner = inner

        def get_nowait(self) -> tuple[dict, object]:
            if refills["left"] > 0:
                refills["left"] -= 1
                self._inner.put_nowait(({"type": "ping", "id": f"r{refills['left']}", "params": {}}, client))
            return self._inner.get_nowait()

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

    _queue(server, client, "open_shot", "swap", filepath="/shots/sq041.blend")
    for index in range(10):
        _queue(server, client, "ping", f"q{index}")
    server.command_queue = _RefillingQueue(server.command_queue)

    server.drain_command_queue()

    drained = len([frame for frame in client.frames() if frame["status"] == "error"])
    assert refills["left"] < _REFILLS, "the refilling producer never ran, so this proves nothing"
    assert drained <= server._MAX_QUEUED_COMMANDS, f"the pre-swap drain took {drained} entries in one tick"


# ---------------------------------------------------------------------------
# BaseException: the swap's own client must not be stranded
# ---------------------------------------------------------------------------


def test_the_swaps_own_client_is_answered_when_the_swap_raises_a_base_exception() -> None:
    """
    `MemoryError` is an `Exception` and was caught; `KeyboardInterrupt` was not.

    A 1 GB `open_mainfile` on Blender's main thread is exactly where a
    blender-side abort lands. Before the fix the abort propagated out of
    `drain_command_queue` and the swap's own client received no frame at all;
    this test reproduces that on demand under the revert-matrix row "only
    Exception is caught, so a blender-side abort strands the swap's own client",
    which is a re-runnable instrument where a pasted transcript is not.

    The answer is written first, then the exception is re-raised - swallowing it
    would turn an abort into a silent no-op.

    **The message is asserted, not just the frame**, and that is what keeps this
    node falsifiable. Since `_run_session_swap` decides by observational receipt
    rather than by a predictive flag, an abort that escapes `_execute_and_answer`
    uncaught is answered by the guard's own fallback instead - so "a frame
    arrived" is now true either way. The two answers are not interchangeable:
    only this handler can name the exception type, and the fallback says
    "nothing was loaded and the open database is unchanged", which is exactly
    the claim that must not be made about an abort during `wm.open_mainfile`.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()
    victim = _RecordingClient()

    def execute_command(_command: dict) -> dict:
        raise KeyboardInterrupt("blender-side abort during open_mainfile")

    server.execute_command = execute_command
    _queue(server, client, "open_shot", "swap", filepath="/shots/sq050.blend")
    _queue(server, victim, "ping", "victim")

    with pytest.raises(KeyboardInterrupt):
        server.drain_command_queue()

    swap_frames = [frame for frame in client.frames() if frame["id"] == "swap"]
    assert swap_frames, "the swap's own client was left waiting on a BaseException"
    assert swap_frames[0]["status"] == "error"
    assert "KeyboardInterrupt" in swap_frames[0]["message"], (
        "the swap's client was answered by `_run_session_swap`'s fallback, which cannot name the abort and "
        f"tells the client nothing was loaded: {swap_frames[0]['message']!r}"
    )
    assert [frame["id"] for frame in victim.frames()] == ["victim"], "the superseded batch was stranded too"


# Call 1 is the swap's own dequeue in the drain loop; calls 2 and 3 take v0 and
# v1 into `superseded`. The abort lands on call 4, with both already off the
# queue and reachable by no other code path.
_CALLS_BEFORE_THE_ABORT = 3


def test_a_base_exception_during_the_pre_swap_drain_still_answers_what_was_taken() -> None:
    """
    A partially built `superseded` list holds commands that are already off the queue.

    Built outside the `try`, those entries are lost to both the rejection path
    and `stop()`'s own drain, so their clients wait out a 180 s timeout for a
    response that no code path can still produce.
    """
    server, _executed = _make_swap_server()
    swap_client = _RecordingClient()
    victim = _RecordingClient()
    taken = {"count": 0}
    real_get = server.command_queue.get_nowait

    def exploding_get() -> tuple[dict, object]:
        taken["count"] += 1
        if taken["count"] > _CALLS_BEFORE_THE_ABORT:
            raise KeyboardInterrupt("abort mid-drain")
        return real_get()

    _queue(server, swap_client, "open_shot", "swap", filepath="/shots/sq051.blend")
    for index in range(4):
        _queue(server, victim, "ping", f"v{index}")
    server.command_queue.get_nowait = exploding_get

    with pytest.raises(KeyboardInterrupt):
        server.drain_command_queue()

    answered = [frame["id"] for frame in victim.frames()]
    assert answered == ["v0", "v1"], f"commands taken off the queue were never answered: {answered}"


# ---------------------------------------------------------------------------
# The rejection is machine-readable, not prose a client has to regex
# ---------------------------------------------------------------------------


def test_the_barrier_rejection_carries_the_epoch_as_a_first_class_field() -> None:
    """
    A client that must regex an English sentence to act on the epoch will not act on it.

    `connection.py` caches `capabilities` once per process and refreshes only on
    an explicit `force_addon_handshake()`; after a swap that cache is stale and
    nothing notices. The field is what lets the refresh be automatic. The prose
    stays, because it is what a human reads in a log.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()

    _queue(server, client, "open_shot", "swap", filepath="/shots/sq060.blend")
    _queue(server, client, "cmd_b", "b")

    server.drain_command_queue()

    rejection = next(frame for frame in client.frames() if frame["id"] == "b")
    assert rejection["session_epoch"] == _epoch()
    assert rejection["session_id"] == _session.SESSION_ID
    assert rejection["status"] == "error"
    assert str(_epoch()) in rejection["message"], "the prose must still name it for a human reader"


def test_an_ordinary_response_carries_the_session_marker_too() -> None:
    """
    Blender's own File -> Open moved the epoch and the server never noticed.

    Before this, the marker reached the server on exactly three routes: a barrier
    rejection, `get_addon_info`, and `get_session_info`. An artist opening a
    different shot in Blender's own UI with no MCP command in flight takes none
    of them, so `send_command` kept gating on a stale `capabilities` set and
    `writable_output_roots` - which decides where files may be written - stayed
    wrong. That is a durability hazard, not a cosmetic one, and two integers on a
    frame the server is already parsing closes it.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()

    _queue(server, client, "ping", "p1")
    server.drain_command_queue()

    frame = client.frames()[0]
    assert frame["session_id"] == _session.SESSION_ID
    assert frame["session_epoch"] == _epoch()
    assert frame["status"] == "success", "the marker must ride an ordinary success, not only an error"


def test_an_aborted_swap_invalidates_the_stamps_taken_during_its_load() -> None:
    """
    `load_post` does not fire on an aborted load, and neither does `load_post_fail`.

    So nothing moved the marker, every command a client thread enqueued *during*
    the load carried a stamp that still matched, and it executed on the next tick
    against a half-replaced database - which is exactly what this test observes
    when `mark_session_indeterminate()` is reverted (revert-matrix row "an
    aborted swap leaves the marker where it was"). The swap's own client was told
    the session might be indeterminate; nobody else was told anything. This is the one place the plan's "never bump on a
    failure" ruling is deliberately not read literally - an abort is not a
    `load_post_fail`, and the "the database is completely untouched" measurement
    that ruling rests on does not cover it. Recorded as a decision.
    """
    server, executed = _make_swap_server()
    swap_client = _RecordingClient()
    other = _RecordingClient()

    def abort_midway(command: dict) -> dict:
        executed.append(command.get("type"))
        if command.get("type") != "open_shot":
            return {"status": "success", "result": {}}
        # Enqueued *during* the load, exactly as a live client thread would:
        # after `_run_session_swap` took its snapshot, before the marker moved.
        _queue(server, other, "ping", "midload")
        # `load_pre` fires and nothing else does - the abort shape the latch
        # exists for. Raising `KeyboardInterrupt` here without touching the ops
        # stub would model an abort that began no load at all, which is
        # `test_an_abort_before_the_swap_is_dispatched_...`'s case, not this one.
        _window_manager_ops.abort_open_mainfile("/shots/sq070.blend")
        raise AssertionError("unreachable: abort_open_mainfile always raises")

    server.execute_command = abort_midway
    before = _epoch()
    _queue(server, swap_client, "open_shot", "swap", filepath="/shots/sq070.blend")

    with pytest.raises(KeyboardInterrupt):
        server.drain_command_queue()

    assert _epoch() != before, "an aborted load left every stamp taken during it still matching"
    executed.clear()
    server.drain_command_queue()

    assert executed == [], f"a command queued during an aborted load still executed: {executed}"
    midload = next(frame for frame in other.frames() if frame["id"] == "midload")
    assert midload["status"] == "error", "the other client was told nothing about the aborted swap"
    assert midload["session_epoch"] == _epoch()


def test_an_abort_during_the_pre_swap_drain_answers_the_swaps_own_client() -> None:
    """
    The swap command is the one entry in `superseded`'s batch nothing answers.

    `_drain_queue_into` runs inside the `try` and **before** the swap is handed
    to `_execute_and_answer`, which is the only thing that answers the swap
    command itself. Its superseded siblings are answered in the `finally`;
    it is not. So an abort during the pre-swap drain leaves the swap's own client
    in `recv()` for its full 180 s timeout with neither a response nor an error -
    §07's automatically-critical outcome, and here it is on demand rather than in
    prose. `test_a_base_exception_during_the_pre_swap_drain_still_answers_what_was_taken`
    is the sibling that asserts the *other* clients are answered, and it passes
    throughout, which is why this went unseen.

    There is no double-answer risk: the receipt is written by `_answer` itself,
    as its first statement, so once anything has begun answering this socket the
    guard's branch is not taken.
    `test_a_swap_that_answers_normally_is_not_answered_a_second_time_by_the_guard`
    is that direction. It used to read `handed_off`, a flag the *caller* set
    immediately before `_execute_and_answer` - which is the prediction
    `test_an_abort_in_the_swaps_prologue_still_answers_the_swaps_own_client`
    falsified.
    """
    server, _executed = _make_swap_server()
    swap_client = _RecordingClient()
    victim = _RecordingClient()
    taken = {"count": 0}
    real_get = server.command_queue.get_nowait

    def exploding_get() -> tuple[dict, object]:
        taken["count"] += 1
        if taken["count"] > _CALLS_BEFORE_THE_ABORT:
            raise KeyboardInterrupt("abort mid-drain")
        return real_get()

    _queue(server, swap_client, "open_shot", "swap", filepath="/shots/sq052.blend")
    for index in range(4):
        _queue(server, victim, "ping", f"v{index}")
    server.command_queue.get_nowait = exploding_get

    with pytest.raises(KeyboardInterrupt):
        server.drain_command_queue()

    swap_frames = [frame for frame in swap_client.frames() if frame["id"] == "swap"]
    assert swap_frames, "the swap's own client was left with no response and no error"
    assert swap_frames[0]["status"] == "error", f"the swap was reported as something else: {swap_frames[0]}"


def test_an_abort_before_the_swap_is_dispatched_does_not_claim_the_database_is_half_replaced() -> None:
    """
    No load ran, so "the open database may be partly replaced" is a false statement about user data.

    `swap_started` was set *before* `_execute_and_answer`, so every abort from
    that point on latched `session_indeterminate` - including an abort in the
    window before the swap is dispatched at all, where the database is exactly
    what it was. The cost of that lie is not cosmetic: the latch refuses every
    subsequent command, publishes the indeterminate note on the poll surface the
    design points clients at, and forces a re-handshake in every connected
    process - the storm the plan's epoch ruling forbids.

    The flag is now fired from inside `_execute_and_answer`, immediately before
    `execute_command` is called, so it says "the swap was dispatched" rather than
    "the swap was about to be handed over". This test models an abort in exactly
    that window; the positive direction - an abort *after* dispatch really does
    latch - is
    `test_an_aborted_swap_invalidates_the_stamps_taken_during_its_load`.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()

    def abort_before_dispatch(_command: dict, _sock: object, **_kwargs: object) -> None:
        # Stands in for an abort inside `_execute_and_answer`'s own prologue:
        # entered, nothing dispatched, and - because this stand-in never reaches
        # `_answer` - no receipt written either. So the swap's own client is
        # answered by `_run_session_swap`'s `except`, which is the window the
        # observational receipt closed and the predictive `handed_off` flag did
        # not. `**_kwargs` absorbs `receipt=`, which production passes by
        # keyword.
        raise KeyboardInterrupt("aborted before the swap was dispatched")

    server._execute_and_answer = abort_before_dispatch
    before = _epoch()
    _queue(server, client, "open_shot", "swap", filepath="/shots/sq053.blend")

    with pytest.raises(KeyboardInterrupt):
        server.drain_command_queue()

    assert _session.session_snapshot()["session_indeterminate"] is False, (
        "an abort that dispatched no load claimed the database may be half replaced"
    )
    assert _epoch() == before, f"the epoch moved for a swap that never ran: {before} -> {_epoch()}"


def test_a_failed_load_then_an_abort_does_not_claim_a_known_clean_database_is_half_replaced() -> None:
    """
    `load_post_fail` leaves the epoch alone by design, so the marker guard misfires.

    The guard asked "did the marker move?" and never "did a failure handler
    already run?". A failed `wm.open_mainfile` leaves the old database completely
    untouched - the measurement the plan's whole epoch ruling rests on - and
    `_on_load_post_fail` records it **without** moving the epoch, exactly as that
    ruling requires. So the marker is unchanged, the guard fires, and a database
    Blender has just told us is intact is published as "may be partly replaced"
    on `get_session_info` - plus the re-handshake storm the ruling forbids. It is
    the mirror image of the bug the structural pass repaired.

    The signal is a counter rather than the `last_load_error` string: two
    consecutive failed opens of the same path produce the *same* note, so a
    before/after string comparison reads "unchanged" on the second one and the
    guard misfires again.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()

    def fail_the_load_then_abort(_command: dict) -> dict:
        # The stub fires `load_post_fail` for this suffix and then raises, which
        # is how every open failure looks from the caller's side.
        with suppress(RuntimeError):
            _window_manager_ops.open_mainfile(filepath="/shots/sq054.missing.blend", use_scripts=False)
        raise KeyboardInterrupt("aborted after the load had already failed")

    server.execute_command = fail_the_load_then_abort
    before = _epoch()
    _queue(server, client, "open_shot", "swap", filepath="/shots/sq054.missing.blend")

    with pytest.raises(KeyboardInterrupt):
        server.drain_command_queue()

    assert _session.session_snapshot()["last_load_error"] is not None, "the harness did not fire load_post_fail"
    assert _session.session_snapshot()["session_indeterminate"] is False, (
        "a database load_post_fail says is untouched was published as possibly half replaced"
    )
    assert _epoch() == before, f"a failed load moved the epoch: {before} -> {_epoch()}"


def test_a_second_identical_failure_then_an_abort_is_still_not_indeterminate() -> None:
    """
    Why the signal is a counter and not the `last_load_error` string.

    `_failure_note` is `f"Loading {leaf} failed; ..."`, so two consecutive failed
    opens of the same path produce a byte-identical note. A guard that captured
    the note before the swap and compared it after would read "unchanged" on the
    second failure and latch anyway - the same false claim, one retry later, on
    the retry path a client that got a bad path from a user actually takes.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()

    def fail_the_load_then_abort(_command: dict) -> dict:
        with suppress(RuntimeError):
            _window_manager_ops.open_mainfile(filepath="/shots/sq055.missing.blend", use_scripts=False)
        raise KeyboardInterrupt("aborted after the load had already failed")

    server.execute_command = fail_the_load_then_abort
    # The first failure, so the note below is already set to exactly the string
    # the second one will produce.
    with suppress(RuntimeError):
        _window_manager_ops.open_mainfile(filepath="/shots/sq055.missing.blend", use_scripts=False)
    first_note = _session.session_snapshot()["last_load_error"]

    _queue(server, client, "open_shot", "swap", filepath="/shots/sq055.missing.blend")
    with pytest.raises(KeyboardInterrupt):
        server.drain_command_queue()

    assert _session.session_snapshot()["last_load_error"] == first_note, "the harness did not repeat the note"
    assert _session.session_snapshot()["session_indeterminate"] is False, (
        "a repeated identical failure was read as no failure at all"
    )


def test_an_aborted_swap_publishes_an_indeterminate_note_every_client_can_poll() -> None:
    """
    The swap's own client is told directly; everyone else has to be able to ask.

    `get_session_info` is the poll surface, so the abort has to leave something
    there - otherwise a second server process sees a moved epoch, re-handshakes,
    finds nothing wrong, and carries on against a database that may be half
    replaced.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()
    # Begins a load and is aborted part-way through it; see
    # `_StubWindowManagerOps.abort_open_mainfile` for why that is not the same
    # as raising `KeyboardInterrupt` with no operator involved.
    server.execute_command = lambda _command: _window_manager_ops.abort_open_mainfile("/shots/sq071.blend")
    _queue(server, client, "open_shot", "swap", filepath="/shots/sq071.blend")

    with pytest.raises(KeyboardInterrupt):
        server.drain_command_queue()

    assert _session.session_snapshot()["last_load_error"] == _session.INDETERMINATE_SESSION_NOTE


# ---------------------------------------------------------------------------
# The two behaviours a critic found unfalsifiable by the whole suite
# ---------------------------------------------------------------------------


def test_the_queue_is_snapshotted_before_the_swap_runs_not_after() -> None:
    """
    The ordering the `_run_session_swap` docstring calls "the whole barrier".

    A command enqueued *while the load is running* must not be in the pre-swap
    snapshot - that snapshot is defined as "everything queued against the old
    file". Draining after the load instead would sweep it up, which is the
    variant Task 2's cycle-2 correction rejected. It is caught one tick later by
    its stamp, which is the division of labour the two mechanisms exist for.
    """
    server, _executed = _make_swap_server()
    swap_client = _RecordingClient()
    late = _RecordingClient()
    inner = server.execute_command

    def execute_command(command: dict) -> dict:
        if command.get("type") == "open_shot":
            # A second process's client thread, enqueuing ~1 s into a 4.6 s load.
            _decode(server, late, "cmd_mid_load", "late")
        return inner(command)

    server.execute_command = execute_command
    _queue(server, swap_client, "open_shot", "swap", filepath="/shots/sq070.blend")

    server.drain_command_queue()

    assert late.frames() == [], "the mid-load arrival was swept into the pre-swap snapshot"
    assert server.command_queue.qsize() == 1, "the mid-load arrival did not survive the tick"

    server.drain_command_queue()

    rejected = next(frame for frame in late.frames() if frame["id"] == "late")
    assert rejected["status"] == "error", "the stamp did not catch what the snapshot could not see"


def test_a_swap_ends_its_tick_even_when_a_fresh_command_is_already_queued() -> None:
    """
    Plan Step 2 / criterion 1 mandate the `break` verbatim; this is what makes it falsifiable.

    Emptying the queue makes the `break` look like dead code, so the case that
    distinguishes it is a command enqueued **during** the swap and stamped with
    the *post*-swap epoch - genuinely "sent after", so the stamp will not reject
    it. Without the `break` the same tick runs it, straight after a load
    measured at 4.6 s, instead of returning to Blender.
    """
    server, executed = _make_swap_server()
    swap_client = _RecordingClient()
    follower = _RecordingClient()
    inner = server.execute_command

    def execute_command(command: dict) -> dict:
        response = inner(command)
        if command.get("type") == "open_shot":
            # Enqueued after load_post fired, so its stamp is the new epoch.
            _decode(server, follower, "cmd_fresh", "fresh")
        return response

    server.execute_command = execute_command
    _queue(server, swap_client, "open_shot", "swap", filepath="/shots/sq071.blend")

    server.drain_command_queue()

    assert executed == ["open_shot"], f"the tick continued past the swap: {executed}"
    assert follower.frames() == []

    server.drain_command_queue()

    assert executed == ["open_shot", "cmd_fresh"], "the fresh command was never serviced"
    assert next(frame for frame in follower.frames() if frame["id"] == "fresh")["status"] == "success"


def test_the_open_mainfile_stub_takes_use_scripts_the_way_production_passes_it() -> None:
    """
    The harness could never validate the argument §07 makes automatically-critical.

    The stub's parameter was named `_use_scripts` while the real call site and
    `scripts/rig_scenarios/in_blender_open_shot_spike.py:55` pass
    `use_scripts=False`, so `TypeError: got an unexpected keyword argument
    'use_scripts'. Did you mean '_use_scripts'?` was the only thing production's
    own spelling could produce here.
    """
    assert _window_manager_ops.open_mainfile(filepath="/shots/sq080.blend", use_scripts=False) == {"FINISHED"}
    assert _window_manager_ops.use_scripts_calls[-1] is False, "the stub cannot see the argument it must validate"


def test_an_escaping_exception_hands_the_drain_loop_to_a_fresh_timer() -> None:
    """
    Blender **drops** a timer callback that raises - measured live on 5.2.2.

    ::

        RIG: timer after it raised once ->
          {"calls_after_raising_once": 1, "still_registered": false,
           "successor_calls": 9, "successor_still_registered": true}

    So re-raising out of `drain_command_queue` - which `_execute_and_answer`
    must do, to avoid swallowing an abort - would kill the drain loop
    permanently, and **every** connected client would then hang forever with no
    response and no error. That is a far worse failure than the one command that
    caused it.

    The same measurement gives the fix: a **successor** registered from inside
    the failing callback, as a different function object, survives and keeps
    ticking (9 calls and still registered). So the dying tick hands off before
    it re-raises.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()
    server.execute_command = lambda _command: (_ for _ in ()).throw(KeyboardInterrupt("abort"))
    _registered.clear()
    server._drain_timer = None
    _queue(server, client, "open_shot", "swap", filepath="/shots/sq090.blend")

    with pytest.raises(KeyboardInterrupt):
        server.drain_command_queue()

    assert server._drain_timer is not None, "the dying tick registered no successor"
    assert any(existing is server._drain_timer for existing in _registered), (
        "the successor was never handed to bpy.app.timers, so the drain loop dies with this tick"
    )


def test_a_dying_tick_leaves_exactly_one_live_drain_timer() -> None:
    """
    "One live drain timer" rested entirely on a behaviour the stub does not model.

    `_replace_this_dying_timer` cleared `self._drain_timer` and registered
    unconditionally, bypassing `_register_drain_timer`'s idempotence guard and
    never attempting to remove the dying object. Blender really does drop a
    callback that raises - the live rig reports
    `calls_after_raising_once: 1, still_registered: false` - but this stub does
    **not** model the drop, so before this the suite structurally could not see a
    duplicate: two dying ticks left three registrations and `stop()` left two.

    Each duplicate carries its own `_MAX_COMMANDS_PER_TICK` and
    `_DRAIN_TIME_BUDGET_SECONDS` allowance, so accumulation multiplies exactly
    the budget that protects Blender's main thread.

    The invariant is now held by construction: the successor is registered only
    after the dying object has been handed to `unregister`. That makes it
    assertable against a stub that drops **and** one that does not, which is
    what this asserts - two dying ticks, and no leftovers at `stop()`.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()
    # KeyboardInterrupt, not RuntimeError: `_execute_and_answer` catches
    # `Exception` and answers the client, so only an abort-class BaseException
    # actually escapes the tick and reaches the handoff path under test.
    server.execute_command = lambda _command: (_ for _ in ()).throw(KeyboardInterrupt("abort"))
    _registered.clear()
    server._drain_timer = None
    server._register_drain_timer()
    assert len(_registered) == 1, "the harness did not start from one live timer"

    for tick in range(2):
        _queue(server, client, "open_shot", f"swap{tick}", filepath=f"/shots/sq09{tick}.blend")
        with pytest.raises(KeyboardInterrupt):
            server.drain_command_queue()
        assert len(_registered) == 1, f"tick {tick} left {len(_registered)} live drain timers"

    server.stop()
    assert _registered == [], f"stop() left {len(_registered)} drain timers behind"


def test_a_stopped_server_does_not_resurrect_its_drain_timer() -> None:
    """
    Recovery must not undo `stop()`, which is the one place the timer is meant to go.

    `stop()` sets `running` False and unregisters; a tick that then raised and
    re-registered would leave a live timer behind every stop, which is the
    accumulation `_unregister_drain_timer` exists to prevent.
    """
    server, _executed = _make_swap_server()
    server.running = False
    _registered.clear()
    server._drain_timer = None

    server._replace_this_dying_timer()

    assert server._drain_timer is None
    assert _registered == []


# ---------------------------------------------------------------------------
# The indeterminate latch, and the two aborts the guard must not claim
# ---------------------------------------------------------------------------

# `_drain_batch` dequeues the swap, then `_run_session_swap` snapshots the queue
# behind it: two `get_nowait` calls before any load runs. Aborting on the first
# would never enter `_run_session_swap` at all, so the guard under test would
# never be reached and the test would pass for the wrong reason - which is what
# the first draft of `test_an_abort_before_the_swap_runs...` did.
_DEQUEUE_THEN_PRE_SWAP_DRAIN = 2


@pytest.fixture(autouse=True)
def _clear_the_indeterminate_latch() -> object:
    """
    Reset the latch, and the load-in-flight edge, before every test here.

    `_session` is one module object shared by every test here, and the latch
    deliberately survives until a load completes - so an abort test would
    otherwise leave every later test's commands refused, and the failure would
    land on whichever test happened to run next. Cleared *before* each test, not
    after, so a test that sets it still observes its own effect.

    `load_in_flight` is reset for the same reason and one more: it is now the
    abort guard's only input, so a stray True left by an earlier test would make
    a later abort latch for a load that test never ran - a green suite proving
    nothing about the guard.

    Returns:
        object: Nothing; this fixture exists for its side effect.

    """
    _session._STATE.session_indeterminate = False
    _session._STATE.load_in_flight = False
    return None


def test_a_command_is_refused_while_the_session_is_indeterminate() -> None:
    """
    The latch was advisory, and advice does not stop a command from running.

    `session.INDETERMINATE_SESSION_NOTE` had no read site anywhere in `src/`:
    every hit was a write, a field or a comment. So the command after an abort
    was dequeued, executed and answered `status: success` against a database that
    may be part of two files - and a client acting on that answer is the
    durability failure, not the abort.
    """
    server, executed = _make_swap_server()
    client = _RecordingClient()
    _session.mark_session_indeterminate()

    _queue(server, client, "ping", "after-abort")
    server.drain_command_queue()

    assert executed == [], f"a command ran against an indeterminate session: {executed}"
    frame = client.frames()[0]
    assert frame["status"] == "error"
    assert "aborted" in frame["message"], f"the refusal does not say why: {frame['message']!r}"
    assert frame["id"] == "after-abort", "the refused command was not answered on its own id"


def test_the_commands_that_report_or_repair_an_indeterminate_session_still_run() -> None:
    """
    A latch nothing can clear is a wedged addon, and a condition nobody can see is worse.

    `open_shot` and `reset_session` are the only things that reach a completed
    `load_post`, which is the only event that clears the flag; `get_addon_info`
    and `get_session_info` are the two surfaces that publish it. Refusing any of
    the four would leave a client unable to tell a refusal from a broken addon,
    and unable to do anything about it either way.
    """
    server, executed = _make_swap_server()
    client = _RecordingClient()
    _session.mark_session_indeterminate()

    _queue(server, client, "open_shot", "repair", filepath="/shots/sq080.blend")
    server.drain_command_queue()

    assert executed == ["open_shot"], f"the repair path was refused too: {executed}"
    assert _session.session_snapshot()["session_indeterminate"] is False, "a completed load did not clear the latch"
    assert set(server._INDETERMINATE_SAFE_COMMANDS) == {
        "get_addon_info",
        "get_session_info",
        "open_shot",
        "reset_session",
    }


def test_an_abort_before_the_swap_runs_does_not_claim_the_database_was_touched() -> None:
    """
    The `try` spans the pre-swap drain, so a bare `except` fired when no load had run.

    Aborting during the queue snapshot means `wm.open_mainfile` was never
    reached: the database is untouched, and "the open database may be partly
    replaced" is a false claim about the user's data that costs a re-handshake
    in every connected process - the storm the plan's epoch ruling exists to
    prevent.
    """
    server, executed = _make_swap_server()
    client = _RecordingClient()
    before = _epoch()

    original_get_nowait = server.command_queue.get_nowait
    calls: list[int] = []

    def abort_on_the_pre_swap_drain() -> tuple:
        """
        Let the drain loop dequeue the swap, then abort its pre-swap snapshot.

        The **first** call is `_drain_batch`'s own dequeue, which has to succeed
        or `_run_session_swap` is never entered and the guard under test is never
        reached. The second call is the pre-swap snapshot inside it, which is
        where Blender's own Esc lands when the queue is long.

        Returns:
            tuple: The dequeued `(command, client)` pair, on the first call only.

        Raises:
            KeyboardInterrupt: On every call after the first.

        """
        calls.append(1)
        if len(calls) == 1:
            return original_get_nowait()
        raise KeyboardInterrupt("aborted before the swap ran")

    _queue(server, client, "open_shot", "swap", filepath="/shots/sq081.blend")
    server.command_queue.get_nowait = abort_on_the_pre_swap_drain

    with pytest.raises(KeyboardInterrupt):
        server.drain_command_queue()

    assert len(calls) >= _DEQUEUE_THEN_PRE_SWAP_DRAIN, f"the abort never reached the pre-swap drain: {calls}"
    assert executed == [], f"the swap ran, so this is not the no-load case: {executed}"
    assert _epoch() == before, "an abort that ran no load still moved the marker"
    assert _session.session_snapshot()["session_indeterminate"] is False, (
        "an abort that ran no load claimed the database may be partly replaced"
    )


def test_an_abort_after_a_completed_load_does_not_bump_the_marker_twice() -> None:
    """
    A clean swap that aborts while answering is not an indeterminate session.

    `load_post` has already fired, so the database is wholly the new file. The
    bare guard bumped again and published "the database may be partly replaced"
    on `get_session_info` - the very surface the design points clients at - after
    a swap that completed. The second condition is the marker comparison.
    """
    server, executed = _make_swap_server()
    client = _RecordingClient()
    real_execute = server.execute_command

    def swap_then_abort_while_answering(command: dict) -> dict:
        """
        Complete the swap, then abort the way a failure during the response would.

        Args:
            command: The command being run.

        Returns:
            dict: Never for `open_shot`; the real result otherwise.

        Raises:
            KeyboardInterrupt: After the load completed.

        """
        result = real_execute(command)
        if command.get("type") == "open_shot":
            raise KeyboardInterrupt("aborted after load_post fired")
        return result

    server.execute_command = swap_then_abort_while_answering
    before = _epoch()
    _queue(server, client, "open_shot", "swap", filepath="/shots/sq082.blend")

    with pytest.raises(KeyboardInterrupt):
        server.drain_command_queue()

    assert executed == ["open_shot"], f"the swap did not run: {executed}"
    assert _epoch() == before + 1, f"a clean swap moved the marker {_epoch() - before} times, not once"
    assert _session.session_snapshot()["session_indeterminate"] is False, (
        "a completed swap was published as an indeterminate session"
    )


# ---------------------------------------------------------------------------
# The past-budget send: a floor, not a non-blocking socket
# ---------------------------------------------------------------------------


def test_the_past_budget_send_never_takes_the_socket_out_of_timeout_mode() -> None:
    """
    `settimeout(0.0)` made the peer's own `recv()` raise `BlockingIOError`.

    That is `OSError` -> `Exception` and **not** a `TimeoutError`, so it fell
    past `handle_client`'s `except TimeoutError: continue` into `except
    Exception: break` and closed a healthy connection - reintroducing, inside the
    fix for it, the dropped-healthy-socket defect §07 makes automatically
    critical. Every timeout this path asks for has to be strictly positive.
    """
    server, _executed = _make_swap_server()
    stalled = [_StalledClient() for _ in range(6)]
    swap_client = _RecordingClient()

    _queue(server, swap_client, "open_shot", "swap", filepath="/shots/sq083.blend")
    for index, peer in enumerate(stalled):
        _queue(server, peer, "ping", f"stalled{index}")

    server.drain_command_queue()

    asked = [value for peer in stalled for value in peer.timeouts if value is not None]
    assert asked, "no peer was written to at all, so this test proves nothing"
    assert all(value > 0 for value in asked), f"a zero timeout put a live socket in non-blocking mode: {asked}"
    assert server._PAST_BUDGET_SEND_TIMEOUT_SECONDS > 0
    assert min(asked) < server._REJECTION_SEND_TIMEOUT_SECONDS, (
        "no peer was written to past the budget, so the floor was never exercised"
    )


def test_a_peer_whose_recv_reports_would_block_is_not_disconnected() -> None:
    """
    Defence in depth behind the floor, because the floor is one constant away.

    `BlockingIOError` means "nothing to read right now", not "this peer is gone".
    Treating it as a disconnect is what cost ~100 of 150 rejection frames, and a
    one-line branch is cheaper than trusting that nobody ever hands this socket
    back non-blocking again.
    """
    server = BlenderMCPServer(port=_free_port())
    server.running = True
    frames: list[bytes] = []
    reads = [BlockingIOError(11, "Resource temporarily unavailable"), b'{"type": "ping", "id": "p1"}\n', b""]

    class _WouldBlockOnce:
        """A socket that reports EAGAIN once, then delivers a real frame, then EOF."""

        def __init__(self) -> None:
            """Script the three reads, and start with nothing written."""
            self.reads: list[object] = list(reads)
            self.frames: list[bytes] = frames

        def settimeout(self, _value: float | None) -> None:
            """
            Accept a timeout change.

            Args:
                _value: Ignored.

            """

        def recv(self, _size: int) -> bytes:
            """
            Return the next scripted read.

            Args:
                _size: Ignored.

            Returns:
                bytes: The next scripted payload, or - on the first call - the
                `BlockingIOError` scripted there, raised rather than returned.

            """
            head = self.reads.pop(0)
            if isinstance(head, BaseException):
                raise head
            assert isinstance(head, bytes)
            return head

        def sendall(self, payload: bytes) -> None:
            """
            Record a frame.

            Args:
                payload: The framed bytes.

            """
            self.frames.append(payload)

        def close(self) -> None:
            """Accept a close."""
            self.frames = self.frames

    client = _WouldBlockOnce()
    server.handle_client(client)

    assert client.reads == [], "the handler gave up before reading the frame that followed the EAGAIN"
    assert server.command_queue.qsize() == 1, "the frame after a BlockingIOError never reached the queue"


# ---------------------------------------------------------------------------
# The single positive predicate: what three negative ones got wrong
# ---------------------------------------------------------------------------


def test_a_retry_then_an_abort_part_way_through_the_second_load_is_indeterminate() -> None:
    """
    The false negative the three-predicate guard had, on the path a client actually takes.

    A user hands the client a bad path, the open fails cleanly, the client
    retries with a good one, and Blender's own Esc lands part-way through that
    second, real load. The old guard asked "did the failure counter move across
    this window?" - and it had, on the *first* open - so it read the abort as
    already accounted for and left `session_indeterminate` False. The second
    load really had been cut in half, and the next command ran against it with
    `status: success`.

    `load_in_flight` is not an inference: `load_pre` fired for the second load
    and neither completion handler followed it, so the flag is still set and the
    guard fires. The first failure cannot satisfy it, because `load_post_fail`
    cleared it.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()

    def fail_one_open_then_abort_part_way_through_the_next(_command: dict) -> dict:
        with suppress(RuntimeError):
            _window_manager_ops.open_mainfile(filepath="/shots/sq056.missing.blend", use_scripts=False)
        _window_manager_ops.abort_open_mainfile("/shots/sq056.blend")
        raise AssertionError("unreachable: abort_open_mainfile always raises")

    server.execute_command = fail_one_open_then_abort_part_way_through_the_next
    _queue(server, client, "open_shot", "swap", filepath="/shots/sq056.blend")

    with pytest.raises(KeyboardInterrupt):
        server.drain_command_queue()

    assert _session.session_snapshot()["session_indeterminate"] is True, (
        "a load that was cut in half after an earlier clean failure was published as a known-good session"
    )


def test_an_abort_between_the_dispatch_and_the_load_is_not_claimed_to_have_touched_the_database() -> None:
    """
    The false positive the three-predicate guard had, recorded as a Task 6 residual.

    Between `execute_command` being entered and `wm.open_mainfile` actually
    starting there is the dispatch table lookup and (from Task 6) the swap
    handler's own path validation. An abort there dispatched a command and ran
    no load, so the old guard's three conditions all passed - it latched, refused
    every subsequent command and forced a re-handshake in every connected
    process, about a database nothing had touched.

    `load_pre` had not fired, and the probe measures that when it does fire the
    old database is still whole - so "no `load_pre`, nothing replaced" is
    Blender's ordering rather than an assumption.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()

    def abort_inside_the_handler_before_any_operator(_command: dict) -> dict:
        raise KeyboardInterrupt("aborted after dispatch, before wm.open_mainfile")

    server.execute_command = abort_inside_the_handler_before_any_operator
    before = _epoch()
    _queue(server, client, "open_shot", "swap", filepath="/shots/sq057.blend")

    with pytest.raises(KeyboardInterrupt):
        server.drain_command_queue()

    assert _window_manager_ops.open_mainfile_calls[-1:] != ["/shots/sq057.blend"], (
        "the harness ran a load, so this is not the no-load window"
    )
    assert _session.session_snapshot()["session_indeterminate"] is False, (
        "an abort that began no load claimed the open database may be half replaced"
    )
    assert _epoch() == before, f"the marker moved for a swap that loaded nothing: {before} -> {_epoch()}"


def test_an_abort_in_the_swaps_prologue_still_answers_the_swaps_own_client() -> None:
    """
    The window `handed_off = True` claimed to close and did not.

    `handed_off` was set in `_run_session_swap`, immediately before
    `self._execute_and_answer(...)`; that function's responsibility begins at its
    own `try:`. A `BaseException` raised between those two points - the argument
    evaluation, the attribute lookup, the call itself - was answered by nobody:
    the caller's `except` believed the callee had it, and the callee had never
    been entered. The swap's own client then waited out its full 180 s timeout
    with neither a response nor an error, which §07 makes automatically critical.
    The committed test for that window asserted only on the latch, so it drove
    the shape without ever looking at the socket.

    A prediction cannot be made accurate by moving it closer to the thing it
    predicts. The receipt is written by `_answer` itself, as its first statement,
    so `not receipt["answered"]` is an observation: nothing has begun answering
    this socket, whoever was supposed to.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()

    def abort_before_the_callee_is_entered(_command: dict, _sock: object, **_kwargs: object) -> None:
        # Stands in for the whole prologue - argument evaluation, the bound
        # method lookup, the call - none of which is inside `_execute_and_answer`
        # and none of which writes a receipt.
        raise KeyboardInterrupt("aborted before _execute_and_answer's own try")

    server._execute_and_answer = abort_before_the_callee_is_entered
    _queue(server, client, "open_shot", "swap", filepath="/shots/sq058.blend")

    with pytest.raises(KeyboardInterrupt):
        server.drain_command_queue()

    swap_frames = [frame for frame in client.frames() if frame["id"] == "swap"]
    assert swap_frames, "the swap's own client was left with no response and no error"
    assert swap_frames[0]["status"] == "error", f"the swap was reported as something else: {swap_frames[0]}"
    assert len(swap_frames) == 1, f"the swap's client was answered {len(swap_frames)} times: {swap_frames}"


def test_a_swap_that_answers_normally_is_not_answered_a_second_time_by_the_guard() -> None:
    """
    The other direction of the receipt: an observation must not double-answer.

    `_answer` writes the receipt before it can fail, so once an answer has begun
    on this socket `_run_session_swap`'s `except` never writes a second frame -
    whatever it then learns about the abort. A client here matches responses by
    stream order, so two frames for one command is a correctness problem, not a
    cosmetic one.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()

    def swap_then_abort_after_the_answer(_command: dict) -> dict:
        _window_manager_ops.abort_open_mainfile("/shots/sq059.blend")
        raise AssertionError("unreachable: abort_open_mainfile always raises")

    server.execute_command = swap_then_abort_after_the_answer
    _queue(server, client, "open_shot", "swap", filepath="/shots/sq059.blend")

    with pytest.raises(KeyboardInterrupt):
        server.drain_command_queue()

    swap_frames = [frame for frame in client.frames() if frame["id"] == "swap"]
    assert len(swap_frames) == 1, f"the swap's client was answered {len(swap_frames)} times: {swap_frames}"
    assert _session.session_snapshot()["session_indeterminate"] is True, (
        "the mid-load abort did not latch, so this test proves nothing about double-answering"
    )


def test_a_second_abort_that_began_no_load_does_not_bump_the_marker_again() -> None:
    """
    Why `mark_session_indeterminate` clears the edge instead of leaving it set.

    The flag means "a load was begun and its outcome is unaccounted for", and an
    abort that latches *is* the accounting. Leaving it set would make the next
    abort - however unrelated, and even one that dispatched no load at all -
    latch again on the strength of this one, and each latch bumps the epoch. That
    is a re-handshake in every connected process for an event that changed
    nothing, which is the exact cost the plan's epoch ruling rejects on the
    failed-swap path.

    `open_shot` is the command driven the second time because the latch refuses
    everything else, so this is the sequence a client repairing an aborted
    session actually walks.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()
    before = _epoch()

    def abort_part_way_through_the_load(_command: dict) -> dict:
        _window_manager_ops.abort_open_mainfile("/shots/sq060.blend")
        raise AssertionError("unreachable: abort_open_mainfile always raises")

    server.execute_command = abort_part_way_through_the_load
    _queue(server, client, "open_shot", "swap-one", filepath="/shots/sq060.blend")
    with pytest.raises(KeyboardInterrupt):
        server.drain_command_queue()

    assert _session.session_snapshot()["session_indeterminate"] is True, "the first abort did not latch"
    after_first = _epoch()
    assert after_first == before + 1, f"the first abort moved the marker {after_first - before} times"

    def abort_before_any_operator(_command: dict) -> dict:
        raise KeyboardInterrupt("aborted after dispatch, before wm.open_mainfile")

    server.execute_command = abort_before_any_operator
    _queue(server, client, "open_shot", "swap-two", filepath="/shots/sq061.blend")
    with pytest.raises(KeyboardInterrupt):
        server.drain_command_queue()

    assert _epoch() == after_first, (
        f"a second abort that began no load bumped the marker again: {after_first} -> {_epoch()}"
    )


def test_an_abort_that_beats_the_answer_still_latches_a_load_that_was_in_flight() -> None:
    """
    The guard's two concerns are independent, and `elif` made them exclusive.

    Answering the swap's own client and latching the session are different
    obligations with different triggers: one asks "has anything begun answering
    this socket", the other asks "was Blender part-way through replacing the
    database". An `elif` between them means the second is skipped whenever the
    first fires - which is exactly the mid-load case, because a second
    `BaseException` landing in `_execute_and_answer`'s `except` body before
    `_answer` writes the receipt leaves the receipt unwritten while `load_pre`
    has already fired.

    The comment on `_ABORTED_BEFORE_HANDOFF` asserted that could not happen:
    "every route out of `_execute_and_answer` writes the receipt through
    `_answer` first". Two routes do not - a `BaseException` inside the
    `except Exception` body (`print`, `traceback.print_exc()`) and one inside the
    `except BaseException` body before `_answer` is reached. This drives the
    second, and the frame it produced told the client the open database was
    unchanged while `load_in_flight` was True.

    The stuck flag is the other half. `mark_session_indeterminate` clears
    `load_in_flight` because an abort that latches *is* the accounting (T3-19);
    skipping the latch leaves the flag set with nothing to clear it, so the next
    unrelated abort latches on the strength of this one.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()
    real_answer = server._answer
    answers: list[object] = []

    def abort_inside_the_except_body_before_the_receipt(*args: object, **kwargs: object) -> None:
        # Stands in for anything in `_execute_and_answer`'s handler bodies that
        # can raise before `_answer` writes the receipt. It must not write the
        # receipt itself, which is the whole shape of the window.
        answers.append(args)
        if len(answers) == 1:
            raise KeyboardInterrupt("aborted inside the except body, before the receipt was written")
        real_answer(*args, **kwargs)

    def abort_part_way_through_the_load(_command: dict) -> dict:
        _window_manager_ops.abort_open_mainfile("/shots/sq062.blend")
        raise AssertionError("unreachable: abort_open_mainfile always raises")

    server.execute_command = abort_part_way_through_the_load
    server._answer = abort_inside_the_except_body_before_the_receipt
    _queue(server, client, "open_shot", "swap", filepath="/shots/sq062.blend")

    with pytest.raises(KeyboardInterrupt):
        server.drain_command_queue()

    assert _window_manager_ops.open_mainfile_calls[-1:] == ["/shots/sq062.blend"], (
        "the harness never began a load, so this is not the mid-load window"
    )
    assert _session.session_snapshot()["session_indeterminate"] is True, (
        "an abort with a load in flight left the session unlatched because the client was answered instead"
    )
    assert _session.load_in_flight() is False, (
        "the latch did not account for the load, so the next unrelated abort latches on the strength of this one"
    )
    swap_frames = [frame for frame in client.frames() if frame["id"] == "swap"]
    assert len(swap_frames) == 1, f"the swap's client was answered {len(swap_frames)} times: {swap_frames}"
    assert "unchanged" not in swap_frames[0]["message"], (
        f"the client was told the open database is unchanged while a load was in flight: {swap_frames[0]['message']!r}"
    )


def test_an_abort_with_no_load_in_flight_still_answers_and_still_claims_nothing_moved() -> None:
    """
    The other direction of the same split: two `if`s must not latch more than the `elif` did.

    With the two concerns separated, the no-load case has to keep the answer it
    had and keep *not* latching - otherwise the repair buys a client an answer at
    the price of the false positive T3-18 removed, and every connected process
    pays a re-handshake for a database nothing touched.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()
    before = _epoch()

    def abort_before_the_callee_is_entered(_command: dict, _sock: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt("aborted before _execute_and_answer's own try")

    server._execute_and_answer = abort_before_the_callee_is_entered
    _queue(server, client, "open_shot", "swap", filepath="/shots/sq063.blend")

    with pytest.raises(KeyboardInterrupt):
        server.drain_command_queue()

    swap_frames = [frame for frame in client.frames() if frame["id"] == "swap"]
    assert len(swap_frames) == 1, f"the swap's client was answered {len(swap_frames)} times: {swap_frames}"
    assert "unchanged" in swap_frames[0]["message"], (
        f"an abort that began no load stopped saying the database is unchanged: {swap_frames[0]['message']!r}"
    )
    assert _session.session_snapshot()["session_indeterminate"] is False, (
        "an abort that began no load claimed the open database may be half replaced"
    )
    assert _epoch() == before, f"the marker moved for a swap that loaded nothing: {before} -> {_epoch()}"
