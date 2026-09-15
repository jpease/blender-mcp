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
import threading
import time
import types

from contextlib import suppress

from conftest import ROOT_ADDON

SERVER_CORE = ROOT_ADDON.parent / "server_core.py"


def _load_server_class():
    """
    Compile BlenderMCPServer from server_core.py against stub modules.

    execute_command is overridden per-instance by every test in this file (see
    _make_server), so the real dispatch table and handler mixins are never
    invoked - only __init__/start/stop/the socket loop are exercised. The
    mixins only need to exist as base classes for the ClassDef to compile.
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

        Measured against Blender 5.2.2 (`blender --background --factory-startup`)::

            PROBE bound-method identity: False
            PROBE bound-method equality: True
            PROBE is_registered(fresh access): False
            PROBE is_registered(held ref):     True
            PROBE unregister(fresh access) raised: ValueError function is not registered

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

    bpy = types.ModuleType("bpy")
    bpy.app = types.SimpleNamespace(background=False, timers=Timers())
    bpy.context = types.SimpleNamespace(scene=types.SimpleNamespace())

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
        **{name: type(name, (), {}) for name in mixin_names},
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), "<addon>", "exec"), namespace)
    return namespace["BlenderMCPServer"], registered


BlenderMCPServer, _registered = _load_server_class()


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
        # wait rather than interleave.
        server.command_queue.put_nowait(({"type": "ping", "id": "req-3"}, client))
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
