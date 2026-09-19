"""
The module-level Blender connection must be built exactly once.

Since the tool layer moved onto `asyncio.to_thread`, `get_blender_connection` is
called from several threadpool workers at once. Unsynchronized, concurrent first
calls each construct, connect and publish their own `BlenderConnection`: every
socket but the last is orphaned - the addon keeps a connection nobody will ever
read from, the losing callers get a connection the module no longer tracks, and
shutdown closes only one of them.
"""

from __future__ import annotations

import threading
import time

import pytest

from blender_mcp.server import connection

_THREADS = 8
# Long enough that every worker is still inside the constructor when the first one
# would otherwise publish, so an unsynchronized get_blender_connection loses the
# race on every run rather than on an unlucky one.
_CONSTRUCT_DELAY_SECONDS = 0.05
# A barrier that never trips means a worker failed to start; fail instead of hanging.
_BARRIER_TIMEOUT_SECONDS = 30.0


def test_concurrent_first_calls_build_and_connect_exactly_one_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    constructed: list[object] = []
    connected: list[object] = []
    handed_out: list[object] = []
    record = threading.Lock()
    start = threading.Barrier(_THREADS)

    class _CountingConnection:
        """Stand-in for BlenderConnection that counts constructions and connects."""

        def __init__(self, host: str, port: int) -> None:
            self.host = host
            self.port = port
            self.sock: object | None = None
            with record:
                constructed.append(self)
            # Straddle the window between "no connection yet" and "connection
            # published", which is the entire race.
            time.sleep(_CONSTRUCT_DELAY_SECONDS)

        def connect(self) -> bool:
            self.sock = object()
            with record:
                connected.append(self)
            return True

    monkeypatch.setattr(connection, "BlenderConnection", _CountingConnection)
    monkeypatch.setattr(connection, "_blender_connection", None)
    # The handshake would send a command down a socket this stand-in does not have.
    monkeypatch.setattr(connection, "_maybe_handshake_addon", lambda _blender: None)

    def worker() -> None:
        start.wait(timeout=_BARRIER_TIMEOUT_SECONDS)
        blender = connection.get_blender_connection()
        with record:
            handed_out.append(blender)

    threads = [threading.Thread(target=worker) for _ in range(_THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(constructed) == 1, "concurrent first calls built more than one connection"
    assert len(connected) == 1, "a second socket was opened and immediately orphaned"
    assert handed_out == [constructed[0]] * _THREADS, "not every caller got the one published connection"


def test_a_connection_whose_socket_died_is_handed_back_rather_than_rebuilt(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A dead socket is reconnected by the next command, not replaced here.

    Building a fresh object instead would strand the per-connection send lock
    that serializes whatever command is still in flight on the old one.
    """

    class _Dead:
        sock = None

    def _unexpected_construction(**kwargs: object) -> None:
        raise AssertionError(f"get_blender_connection rebuilt a tracked connection: {kwargs}")

    dead = _Dead()
    monkeypatch.setattr(connection, "_blender_connection", dead)
    monkeypatch.setattr(connection, "BlenderConnection", _unexpected_construction)

    assert connection.get_blender_connection() is dead
