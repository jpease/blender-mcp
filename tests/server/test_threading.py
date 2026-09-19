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

    `open_mainfile` fires the real `session.py` handlers the way Blender does, with
    two positional arguments, the second None. A path ending in `_MISSING_SUFFIX`
    fails the way every open failure looks to the caller: `load_post_fail` fires
    instead of `load_post`, the database is untouched, and the operator raises
    `RuntimeError` rather than returning `{'CANCELLED'}`.

    Attributes:
        open_mainfile_calls: Every filepath handed to `open_mainfile`, in order.
        use_scripts_calls: The `use_scripts` value each call passed, so a test can
            catch a call that would run a shot's embedded Python.

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
            use_scripts: Named as production passes it; an `_use_scripts` parameter
                would reject that call with a TypeError.

        Returns:
            set[str]: `{'FINISHED'}`, the only result a successful open returns.

        Raises:
            RuntimeError: When `filepath` names an unloadable file.

        """
        self.open_mainfile_calls.append(filepath)
        self.use_scripts_calls.append(use_scripts)
        # Blender fires `load_pre` first on success and failure alike, while
        # `bpy.data` still holds the old file.
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

        `load_pre` fires, then neither `load_post` nor `load_post_fail`: the case
        the indeterminate latch exists for. Raising `KeyboardInterrupt` with no
        operator models an abort before any load began, which must not latch.

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

    `session.py` is the exception: the real module runs against the same `bpy`
    stub, so barrier tests see the epoch a stub swap moves.
    """
    source = SERVER_CORE.read_text(encoding="utf-8")
    tree = ast.parse(source)

    body: list[ast.stmt] = [
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "BlenderMCPServer"
    ]
    assert body, "BlenderMCPServer not found in server_core.py"

    main_thread = threading.current_thread()
    registered: list[object] = []

    class Timers:
        """
        Stub for bpy.app.timers: main-thread-only, and matching by identity.

        Blender treats two accesses of one bound method as different callbacks,
        though they compare and hash equal. Registrations are kept in a list and
        matched with `is`, so a `stop()` that unregisters a fresh access fails here as
        it does in Blender.
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

    # Installed only for this exec: a lingering sys.modules["bpy"] would change
    # what later test modules import.
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
        "LinkingHandlersMixin",
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
        # Same `session` module as the names above, so an abort and the next
        # command share one piece of state.
        "session_is_indeterminate": session.session_is_indeterminate,
        # Same module again: a flag from a second copy of `session.py` would never
        # move, letting guard tests pass for the wrong reason.
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

    Matches owner and function rather than identity with
    `server.drain_command_queue`: each access builds a new bound method, so an
    identity match would always count zero and this helper could never fail.

    Args:
        server: The BlenderMCPServer whose timers to count.

    Returns:
        int: How many registrations belong to `server`. More than one means
        restarts have multiplied the drain throttle.

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

    `start()` listens on the calling thread, so a connect alone proves nothing
    about the server thread. A `stop()` before `_server_loop` reaches its guarded
    loop closes the socket under its `settimeout` call and kills the thread.

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
    A started server holds exactly one drain timer, and stop() removes it.

    Counted rather than checked as a bool, because the failure is accumulation:
    unregistering a fresh bound method removes nothing.
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

    Each duplicate timer gets its own per-tick command and time allowance, so N
    stale timers let N times the work run on Blender's main thread per tick.
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

    Its repr holds a path and a secret, so a test can check neither reaches the
    client.
    """

    def __repr__(self) -> str:
        return "<Vector at /Users/someone/secret-token/scene.blend>"


def test_a_non_serializable_result_gets_an_error_frame_not_silence() -> None:
    """
    An unserializable handler result still produces a response frame.

    Serialized inside the send's `try`, the failure would be reported as a
    disconnect with no frame sent, and the client would wait out its 180 s timeout.
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
    The serialization error says the response was unserializable, never what it held.

    Scene reprs, absolute paths and tracebacks must not reach a client.
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

    The first `sendall` holds for `hold_seconds`, so a second writer that no lock
    excludes is caught inside it. Attributes:

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

    The client thread writes protocol errors while the main thread writes
    responses, and sendall() is not atomic, so an error frame could splice into a
    large response and desync the newline framing.
    """
    server = _make_server()
    server.running = True
    client = _SerializedWriteProbe(b"not json\n")

    handler = threading.Thread(target=server.handle_client, args=(client,), daemon=True)
    handler.start()
    try:
        assert client.send_started.wait(3.0), "the handler never sent its protocol error"

        # The handler is parked inside sendall(), so this write has to wait.
        # Stamped because the drain rejects unstamped commands.
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

    The barrier fires only for a swap command that has a handler, so each
    barrier test must say which commands this server can dispatch.

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

    A successful swap fires the real `session.py` `load_post` handler, moving
    the epoch the barrier's message reports.

    Returns:
        tuple: The server, and the list every executed command type is appended
        to, which tells a rejected command apart from one that ran and errored.

    """
    server = BlenderMCPServer(port=_free_port())
    executed: list[str | None] = []
    # The barrier checks the dispatch table, so it is stubbed along with
    # `execute_command`.
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

    Stamped through production's `_stamp_session`, because the drain rejects
    unstamped commands, and a hand-built stamp would keep passing if the stamp's
    key or shape changed.

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
    Commands queued behind a swap are answered, never run against the new file.

    Asserts on `executed`, because a response-only check passes even when the
    command ran.
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
    A failed swap also discards the commands queued behind it.

    The barrier fires at dequeue, before the outcome is known, and a failed open
    leaves the epoch unchanged, so epoch movement cannot drive it.
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
    The barrier message names the current epoch without claiming it changed.

    One wording serves both outcomes: after a successful swap the client sees a new
    epoch and re-handshakes; after a failed one it sees the same epoch and resends.
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
    """The barrier message carries no path, though the swap command in hand has one."""
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
    """The epoch moves once per successful swap through the drain loop, never on a failure."""
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
    Every command spanning a swap is answered, on both sockets.

    Several MCP server processes may share one Blender, so the barrier must answer
    commands from a process that never sent the swap. Both sockets write before
    either reads, so the whole batch is queued before the drain starts.
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
    Queue one command through the production enqueue path, `_decode_and_queue_frame`.

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
    A command that arrives during a load is rejected at dequeue by its stamp.

    The epoch moves in `load_post`, at the end of the load, and client threads,
    including other server processes', keep queuing throughout. The pre-swap queue
    snapshot misses those commands; only the stamp catches them.
    """
    server, executed = _make_swap_server()
    client = _RecordingClient()

    _decode(server, client, "cmd_mid_load", "mid")
    # The load completes while that command waits, so its stamp goes stale.
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


# `put()` too: a re-queue path could reasonably use the blocking form.
_ENQUEUE_METHODS = frozenset({"put", "put_nowait"})
# `async def` and class-scope lambdas enqueue too; see `_EVASIVE_PRODUCERS`.
_CALLABLE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _queue_producers(source: str) -> list[str]:
    """
    Name every callable in `source` that enqueues anything.

    Separate from the test below so it can also run on synthetic sources,
    which shows the static check can fail.

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


# Producer shapes a narrower scan would miss, each a second producer that does
# not stamp.
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
    The producer scan catches each evasion shape, so the static check can fail.

    Args:
        label: Which evasion shape this row is, for the failure message.
        source: A synthetic module containing exactly one second producer.

    """
    # The class-scope lambda has no name, so the assertion is only that it is seen.
    assert _queue_producers(source), f"{label} still evades the producer scan"


def test_the_enqueue_path_is_the_only_producer_and_it_stamps() -> None:
    """
    `_decode_and_queue_frame` is the only enqueue site, and it stamps.

    The drain already rejects unstamped commands; this keeps that branch
    unreachable. Any `.put` or `.put_nowait` call counts, whatever its receiver:
    an alias or a queue passed as an argument would evade a receiver check, and
    `command_queue` is the module's only queue.
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
    # The call, not the name: a comment or docstring naming `_stamp_session`
    # would pass a name check.
    assert "self._stamp_session(command)" in str(source), "the one producer does not stamp the command it queues"


def test_the_stamp_is_read_without_touching_bpy_on_the_client_thread() -> None:
    """
    `_decode_and_queue_frame` runs on a client thread, so it must not touch `bpy`.

    The stub `bpy` answers from any thread, so no other test here would catch it.
    """
    tree = ast.parse(SERVER_CORE.read_text(encoding="utf-8"))
    function = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_decode_and_queue_frame"
    )
    touched = [node.id for node in ast.walk(function) if isinstance(node, ast.Name) and node.id in {"bpy", "mathutils"}]
    assert not touched, f"the enqueue path touches Blender state off the main thread: {touched}"


# ---------------------------------------------------------------------------
# The barrier only fires for a swap the addon can dispatch
# ---------------------------------------------------------------------------


def test_a_swap_command_the_addon_cannot_dispatch_discards_nobodys_batch() -> None:
    """
    A swap command the addon cannot dispatch discards nobody's queued commands.

    Otherwise one small frame naming `open_shot` could throw away other server
    processes' commands.
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
    A peer that accepts no bytes: stopped, paused in a debugger, or half-open.

    CPython applies `handle_client`'s 1 s socket timeout to a whole `sendall`, so
    each rejection to such a peer costs a second unless the rejection path sets
    its own.

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
    A peer that reads, just not instantly.

    A client still draining a large response can be this slow, and closing it
    forfeits its other queued commands.

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

        Does not model a non-blocking socket (timeout 0), which production never
        sets.

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
    A healthy peer queued behind stalled ones still gets its rejection.

    Past the batch budget every remaining peer still gets a write attempt. Closing
    one untried drops a healthy socket, and this frame is its only prompt to
    re-handshake.

    This peer drains in well under 1 ms because past the budget a write gets only
    `_PAST_BUDGET_SEND_TIMEOUT_SECONDS`, and this stub, unlike a kernel send
    buffer, charges its delay on every write.
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
    A peer slower than a loopback reader is still answered and kept.

    The rejection send timeout detects dead peers. A 60 ms reader is healthy, and
    closing it forfeits its other queued commands.
    """
    server, _executed = _make_swap_server()
    slow = _SlowButHealthyClient(delay=0.06)
    swap_client = _RecordingClient()

    _queue(server, swap_client, "open_shot", "swap", filepath="/shots/sq051.blend")
    _queue(server, slow, "ping", "slow")

    server.drain_command_queue()

    assert len(slow.frames()) == 1, "a 60ms reader was treated as unreachable"
    assert not slow.shut_down, "a 60ms reader had its connection destroyed"


# `_send_bounded` sets a timeout and then restores the socket's own: two calls
# per attempt.
_SETTIMEOUT_CALLS_PER_ATTEMPT = 2
# Longer than any bound the rejection path promises, so waiting for the lock
# shows. Released eventually, so a regression fails an assertion instead of
# deadlocking the run.
_LOCK_HELD_SECONDS = 2.0


def test_a_malformed_frame_arriving_mid_rejection_cannot_park_the_main_thread() -> None:
    """
    A write lock held by another thread cannot park the main thread mid-rejection.

    The other holder is a `handle_client` thread sending a protocol error, which
    can hold the lock for as long as its own send blocks.
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
    A socket whose own timeout cannot be restored is dropped.

    Kept, its `handle_client` loop would go on running at the rejection path's
    short timeout.
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
    A command that reached the queue unstamped is rejected, not run.

    A static producer scan can miss shapes, and in a correct tree this branch is
    never taken, so failing closed costs nothing.
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
    Rejecting a full queue to one stalled peer stays within the time budget.

    Without a deadline each of `_MAX_QUEUED_COMMANDS` frames could cost the
    socket's own 1 s timeout, freezing Blender's UI while every other client times
    out.
    """
    server, _executed = _make_swap_server()
    stalled = _StalledClient()
    swap_client = _RecordingClient()

    # The swap goes first so everything behind it is drained and rejected; it
    # takes the last queue slot.
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
    # Without skipping abandoned peers, all 255 frames would be retried on a
    # socket this pass already closed.
    attempts = len(stalled.timeouts) // _SETTIMEOUT_CALLS_PER_ATTEMPT
    assert attempts <= 1, f"an abandoned peer was written to {attempts} times: {stalled.timeouts}"


def test_rejecting_a_full_queue_to_distinct_stalled_peers_is_bounded() -> None:
    """
    Rejecting a full queue to distinct stalled peers stays within the worst case.

    The one-peer test above skips every write after the first failure, so it never
    pays a per-peer cost. Distinct peers each get a past-budget attempt, which
    spends `_PAST_BUDGET_SEND_TIMEOUT_SECONDS` twice: on the write lock, then on
    `sendall`.

    The structural assertions carry the meaning; the timing only checks the order
    of magnitude, since a 1 ms sleep runs long on a busy machine.
    """
    server, _executed = _make_swap_server()
    swap_client = _RecordingClient()
    # One peer per command, so no entry can be skipped as already abandoned.
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
    # The budget bounds how long a send may block, never whether one is made.
    # Every unanswered peer is closed, so it sees EOF instead of silence.
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
    The pre-swap drain stops at an explicit count, even while producers refill it.

    `maxsize` caps what the queue holds at once, not how many entries one drain
    takes.
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
    The swap's own client is answered when the swap raises a `BaseException`.

    An abort such as `KeyboardInterrupt` is not an `Exception`. The message is
    checked, not just the frame: `_run_session_swap`'s fallback also sends one, but
    it cannot name the abort and says nothing was loaded, which is false mid-load.
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


# Call 1 dequeues the swap; calls 2 and 3 move v0 and v1 into `superseded`.
# The abort lands on call 4, when both are already off the queue.
_CALLS_BEFORE_THE_ABORT = 3


def test_a_base_exception_during_the_pre_swap_drain_still_answers_what_was_taken() -> None:
    """
    An abort during the pre-swap drain still answers the commands already taken.

    They are off the queue, so nothing else can answer them before their clients
    time out.
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
    The barrier rejection carries the epoch as a field, not only in prose.

    The server refreshes its cached capabilities from the field; the prose is for
    logs.
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
    An ordinary response carries the session marker too.

    A File > Open in Blender's own UI moves the epoch with no MCP command in
    flight, so without the marker on ordinary frames the server would keep gating
    on stale capabilities.
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
    An aborted swap invalidates the commands stamped during its load.

    Neither `load_post` nor `load_post_fail` fires on an abort, so unless
    `mark_session_indeterminate()` moves the marker, those stamps still match and
    the commands run against a half-replaced database.
    """
    server, executed = _make_swap_server()
    swap_client = _RecordingClient()
    other = _RecordingClient()

    def abort_midway(command: dict) -> dict:
        executed.append(command.get("type"))
        if command.get("type") != "open_shot":
            return {"status": "success", "result": {}}
        # Enqueued during the load: after the pre-swap snapshot, before the marker
        # moves.
        _queue(server, other, "ping", "midload")
        # `load_pre` fires and nothing else does. Raising with no operator would
        # model an abort before any load, a different case.
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
    An abort during the pre-swap drain answers the swap's own client.

    The swap is off the queue but not in `superseded`, and only
    `_execute_and_answer` answers it, so an abort before that call would leave its
    client waiting out its 180 s timeout. `_answer` writes the receipt first, so
    this cannot answer twice.
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
    An abort before the swap is dispatched does not latch the session.

    No load ran, so the database is untouched, and latching would refuse every
    later command and force a re-handshake in every connected process. Only
    `load_pre` sets `load_in_flight`, which decides the latch.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()

    def abort_before_dispatch(_command: dict, _sock: object, **_kwargs: object) -> None:
        # An abort inside `_execute_and_answer` before anything is dispatched or
        # answered, so `_run_session_swap`'s `except` answers the client. `**_kwargs`
        # absorbs `receipt=`.
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
    A failed load followed by an abort does not latch the session.

    A failed open leaves the database untouched and `load_post_fail` leaves the
    epoch alone, so a guard asking "did the marker move?" would latch wrongly.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()

    def fail_the_load_then_abort(_command: dict) -> dict:
        # The stub fires `load_post_fail` for this suffix, then raises like any
        # failed open.
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
    A second identical failure followed by an abort still does not latch.

    Two failed opens of one path write identical `last_load_error` notes, so a
    guard comparing the note before and after would latch on the retry.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()

    def fail_the_load_then_abort(_command: dict) -> dict:
        with suppress(RuntimeError):
            _window_manager_ops.open_mainfile(filepath="/shots/sq055.missing.blend", use_scripts=False)
        raise KeyboardInterrupt("aborted after the load had already failed")

    server.execute_command = fail_the_load_then_abort
    # A first failure, so the note already holds the string the second will write.
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
    An aborted swap leaves a note on `get_session_info` that any client can poll.

    Only the swap's own client is told directly. Another server process would
    otherwise re-handshake, find nothing wrong, and carry on.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()
    # Begins a load and aborts part-way; see `_StubWindowManagerOps.abort_open_mainfile`.
    server.execute_command = lambda _command: _window_manager_ops.abort_open_mainfile("/shots/sq071.blend")
    _queue(server, client, "open_shot", "swap", filepath="/shots/sq071.blend")

    with pytest.raises(KeyboardInterrupt):
        server.drain_command_queue()

    assert _session.session_snapshot()["last_load_error"] == _session.INDETERMINATE_SESSION_NOTE


# ---------------------------------------------------------------------------
# Snapshot timing, and the break that ends a swap's tick
# ---------------------------------------------------------------------------


def test_the_queue_is_snapshotted_before_the_swap_runs_not_after() -> None:
    """
    The pre-swap snapshot is taken before the load runs.

    A command enqueued during the load must stay queued for its stamp to reject on
    the next tick; draining after the load would sweep it into the snapshot.
    """
    server, _executed = _make_swap_server()
    swap_client = _RecordingClient()
    late = _RecordingClient()
    inner = server.execute_command

    def execute_command(command: dict) -> dict:
        if command.get("type") == "open_shot":
            # Another process's client thread, enqueuing mid-load.
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
    A swap ends its tick even when a fresh command is already queued.

    A command queued during the swap with the post-swap stamp passes the barrier,
    so only the `break` stops it running in the same tick as a long load.
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
    The stub accepts `use_scripts`, the argument that keeps a shot's own Python from running.

    Production passes `use_scripts=False`; a stub that rejected the keyword could
    not check it.
    """
    assert _window_manager_ops.open_mainfile(filepath="/shots/sq080.blend", use_scripts=False) == {"FINISHED"}
    assert _window_manager_ops.use_scripts_calls[-1] is False, "the stub cannot see the argument it must validate"


def test_an_escaping_exception_hands_the_drain_loop_to_a_fresh_timer() -> None:
    """
    An escaping exception hands the drain loop to a fresh timer.

    Blender unregisters a timer callback that raises, and the drain must re-raise
    aborts. Without a successor registered from the dying tick, the drain loop
    ends and every client waits out its timeout.
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
    Two dying ticks leave exactly one drain timer, and stop() leaves none.

    Blender drops a raising callback but this stub does not, so the handoff must
    unregister the dying timer itself. Each duplicate would multiply the per-tick
    budget that protects Blender's main thread.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()
    # Only a BaseException escapes the tick; `_execute_and_answer` answers an
    # `Exception` itself.
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
    Recovery does not re-register the drain timer of a stopped server.

    Otherwise every stop would leave a live timer behind.
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

# `_drain_batch` dequeues the swap, then `_run_session_swap` drains the queue:
# two `get_nowait` calls. Aborting on the first never reaches the guard under test.
_DEQUEUE_THEN_PRE_SWAP_DRAIN = 2


@pytest.fixture(autouse=True)
def _clear_the_indeterminate_latch() -> object:
    """
    Reset the indeterminate latch and the load-in-flight flag before every test.

    `_session` is shared and the latch lasts until a load completes, so one abort
    test would otherwise refuse later tests' commands. A stray `load_in_flight`
    would make a later abort latch without a load of its own. Reset before each
    test, not after, so a test still observes its own effect.

    Returns:
        object: Nothing; this fixture exists for its side effect.

    """
    _session._STATE.session_indeterminate = False
    _session._STATE.load_in_flight = False
    return None


def test_a_command_is_refused_while_the_session_is_indeterminate() -> None:
    """
    A command is refused while the session is indeterminate.

    Otherwise it would run and answer success against a database that may mix two
    files, and the client would act on that answer.
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
    The commands that report or repair an indeterminate session still run.

    `open_shot` and `reset_session` are the only commands that complete a load, which
    clears the latch, and the two info commands publish it. Refusing them would
    wedge the addon with no visible reason.
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
    An abort during the pre-swap drain does not latch the session.

    The `try` spans the drain, and no load has run yet, so latching would falsely
    report the database as possibly mixed and make every process re-handshake.
    """
    server, executed = _make_swap_server()
    client = _RecordingClient()
    before = _epoch()

    original_get_nowait = server.command_queue.get_nowait
    calls: list[int] = []

    def abort_on_the_pre_swap_drain() -> tuple:
        """
        Let the drain loop dequeue the swap, then abort its pre-swap snapshot.

        The first call must succeed, or `_run_session_swap` is never entered.

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
    An abort after a completed load does not bump the marker twice.

    `load_post` has already fired and cleared `load_in_flight`, so the database is
    wholly the new file and the guard must not latch.
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
    The past-budget send never sets a zero timeout on the socket.

    `settimeout(0)` makes the peer's own `recv()` raise `BlockingIOError`, which
    is not a `TimeoutError`.
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
    A peer whose recv reports would-block is not disconnected.

    `BlockingIOError` means nothing to read yet. This backs up the positive
    timeout floor, which one changed constant could undo.
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
# The single positive predicate: `load_in_flight`
# ---------------------------------------------------------------------------


def test_a_retry_then_an_abort_part_way_through_the_second_load_is_indeterminate() -> None:
    """
    A clean failure, a retry, and an abort part-way through the retry's load latches.

    `load_post_fail` cleared `load_in_flight` after the first open; `load_pre` set
    it again for the second, and no completion handler followed. A guard keyed on
    failures would see the first one and not latch.
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
    An abort after dispatch but before `wm.open_mainfile` does not latch.

    Dispatch and the handler's path validation run before any load. Blender fires
    `load_pre` while the old database is still whole, so no `load_pre` means
    nothing was replaced.
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
    An abort before `_execute_and_answer` is entered still answers the swap's client.

    `_answer` writes the receipt itself, so an unwritten receipt means nothing
    began answering, whichever code was meant to.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()

    def abort_before_the_callee_is_entered(_command: dict, _sock: object, **_kwargs: object) -> None:
        # Stands in for argument evaluation, lookup and the call itself, none of
        # which writes a receipt.
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
    A swap that begins its own answer is not answered again by the guard.

    A second frame for one command would desync a client reading responses in
    order.
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
    A second abort that began no load does not bump the marker again.

    `mark_session_indeterminate` clears `load_in_flight`; left set, any later abort
    would latch and bump the epoch again, making every process re-handshake.
    `open_shot` is used the second time because the latch refuses most other
    commands.
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
    An abort that beats the answer still latches a load that was in flight.

    Answering the swap's client and latching are independent, so they are two
    `if`s, not an `elif`: an abort inside `_execute_and_answer`'s `except` body,
    before `_answer` writes the receipt, needs both. The latch also clears
    `load_in_flight`, which would otherwise make the next unrelated abort latch.
    """
    server, _executed = _make_swap_server()
    client = _RecordingClient()
    real_answer = server._answer
    answers: list[object] = []

    def abort_inside_the_except_body_before_the_receipt(*args: object, **kwargs: object) -> None:
        # Anything in `_execute_and_answer`'s handler bodies that raises before
        # `_answer` writes the receipt; it must not write the receipt itself.
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
    With no load in flight, an abort is answered "unchanged" and does not latch.

    Otherwise every process would re-handshake over a database nothing touched.
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
