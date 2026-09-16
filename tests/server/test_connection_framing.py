r"""
Regression coverage for BlenderConnection's newline-delimited framing.

Mirrors tests/test_socket_unicode.py on the addon side: `receive_full_response`
now reads up to a `\n` terminator instead of retrying json.loads() on a
growing buffer, and any bytes past that terminator are kept in
`self._recv_buffer` instead of being discarded. `send_command_locked` also
now tags each command with an "id" and checks the response echoes it back,
so a desynced stream fails loudly instead of returning the wrong response.
"""

from __future__ import annotations

import json
import threading
import types
import uuid

import pytest

from blender_mcp.addon_manager import EXPECTED_ADDON_PROTOCOL_VERSION, AddonHandshake
from blender_mcp.server import connection
from blender_mcp.server.connection import BlenderConnection, BlenderOperationError


class ScriptedSocket:
    """Fake socket returning pre-scripted recv() chunks, one per call."""

    def __init__(self, chunks) -> None:
        self._chunks = list(chunks)
        self.sent: list[bytes] = []

    def settimeout(self, timeout) -> None:
        pass

    def recv(self, bufsize):
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)


def test_two_frames_in_one_recv_are_not_glued_together() -> None:
    r"""
    Two newline-terminated responses landing in a single recv() must not be
    concatenated into one - the first call to receive_full_response() should
    return only the first frame, leaving the second buffered for the next
    call instead of failing to parse "response1\nresponse2" as one JSON value.
    """
    first = json.dumps({"status": "success", "result": {"n": 1}}).encode("utf-8") + b"\n"
    second = json.dumps({"status": "success", "result": {"n": 2}}).encode("utf-8") + b"\n"

    conn = BlenderConnection(host="localhost", port=0)
    sock = ScriptedSocket([first + second])

    line1 = conn.receive_full_response(sock)
    assert json.loads(line1) == {"status": "success", "result": {"n": 1}}

    line2 = conn.receive_full_response(sock)
    assert json.loads(line2) == {"status": "success", "result": {"n": 2}}


def test_oversized_response_without_terminator_raises_instead_of_growing_forever() -> None:
    conn = BlenderConnection(host="localhost", port=0)
    conn._MAX_MESSAGE_BYTES = 100  # keep the test fast
    sock = ScriptedSocket([b"x" * 200])

    with pytest.raises(Exception, match="exceeded max size"):
        conn.receive_full_response(sock)


def test_oversized_terminated_response_raises() -> None:
    r"""
    A single complete (`\n`-terminated) response must be size-checked too.

    The unterminated-buffer check above only bounds "how long can we wait
    without ever seeing a terminator" - it does not stop a response that
    *does* get a `\n` (e.g. because the terminating chunk lands in the same
    recv() call that pushes the buffer past the limit) from being returned
    at any size.
    """
    conn = BlenderConnection(host="localhost", port=0)
    conn._MAX_MESSAGE_BYTES = 100  # keep the test fast
    sock = ScriptedSocket([b"x" * 200 + b"\n"])

    with pytest.raises(Exception, match="exceeded max size"):
        conn.receive_full_response(sock)


def test_response_id_mismatch_raises_and_drops_the_socket(monkeypatch) -> None:
    """
    send_command_locked must not hand back a response meant for another
    request - the lock already prevents this in practice, but a mismatched
    id should fail loudly rather than silently succeed with the wrong data.
    """
    conn = BlenderConnection(host="localhost", port=0)
    conn.sock = ScriptedSocket([])
    monkeypatch.setattr(
        conn,
        "receive_full_response",
        lambda sock: json.dumps({"status": "success", "result": {}, "id": "not-the-request-id"}).encode("utf-8"),
    )

    with pytest.raises(Exception, match="does not match request id"):
        conn.send_command_locked("ping")

    assert conn.sock is None, "a desynced response must invalidate the connection"


# ---------------------------------------------------------------------------
# The session marker: the epoch is only a signal if something acts on it
# ---------------------------------------------------------------------------


def _reset_handshake_state(monkeypatch: pytest.MonkeyPatch, handshake: object) -> None:
    """
    Put connection.py's module globals into a known state for one test.

    Args:
        monkeypatch: Fixture that restores each global afterwards.
        handshake: The `AddonHandshake` the process is to believe it cached.

    """
    monkeypatch.setattr(connection, "_addon_handshake", handshake)
    monkeypatch.setattr(connection, "_addon_handshake_checked", True)
    monkeypatch.setattr(connection, "_session_marker_stale", threading.Event())
    monkeypatch.setitem(connection._OBSERVED_MARKER, "pending", None)
    monkeypatch.setattr(connection, "_refreshing", threading.local())


def _cached(**fields: object) -> AddonHandshake:
    """
    Build a handshake standing in for one this process already ran.

    Args:
        **fields: Overrides for the session fields under test.

    Returns:
        AddonHandshake: The stand-in.

    """
    defaults: dict[str, object] = {
        "capabilities": ["ping", "get_addon_info"],
        "session_id": "proc-a",
        "session_epoch": 1,
    }
    return AddonHandshake(
        up_to_date=True,
        protocol_version=EXPECTED_ADDON_PROTOCOL_VERSION,
        addon_version=[2, 0, 0],
        blender_version="5.2.2",
        source="native",
        **{**defaults, **fields},  # pyright: ignore[reportArgumentType]
    )


def test_an_unchanged_session_marker_does_not_invalidate_the_cached_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Re-handshaking on every response would put two commands on the wire per tool call.

    The premise the whole mechanism rests on is that the *pair* is compared, so
    the no-change path has to be asserted as explicitly as the change path.
    """
    _reset_handshake_state(monkeypatch, _cached())

    connection.note_session_marker({"status": "error", "session_id": "proc-a", "session_epoch": 1})

    assert connection._session_marker_stale.is_set() is False


def test_a_moved_epoch_marks_the_cached_handshake_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    `connection.py:186` gates **every** command on a `capabilities` set cached once per process.

    After a swap that gate is stale and nothing noticed - the exact failure the
    epoch exists to signal. That `writable_output_roots` really does change
    across a swap is measured by `scripts/blender_probes/session_handlers.py`,
    whose "writable_output_roots changes across a swap" section prints the
    before and after lists and which root was gained. **How many** roots it
    gains is a property of the machine it runs on, not of the mechanism, so no
    arity is asserted here - an earlier revision of this docstring named one and
    nothing in the tree produced it.
    """
    _reset_handshake_state(monkeypatch, _cached())

    connection.note_session_marker({"status": "error", "session_id": "proc-a", "session_epoch": 2})

    assert connection._session_marker_stale.is_set() is True


def test_a_restarted_addon_at_the_same_epoch_still_marks_the_handshake_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The ABA case: epoch 1 -> restart -> 0 -> one swap -> 1, and the numbers match.

    Comparing the counter alone reads that as "nothing happened" and keeps a
    capability set belonging to a different `.blend` in a different process.
    """
    _reset_handshake_state(monkeypatch, _cached())

    connection.note_session_marker({"status": "error", "session_id": "proc-b", "session_epoch": 1})

    assert connection._session_marker_stale.is_set() is True


def test_the_marker_is_read_from_a_command_result_as_well_as_the_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The barrier's rejection carries the fields at frame level; `get_session_info` nests them.

    A client whose commands never sat behind a swap sees only the second shape,
    and it must be enough - but only for the two commands that are *specified*
    to report a session, which is why the command name is passed.
    """
    _reset_handshake_state(monkeypatch, _cached())

    connection.note_session_marker(
        {"status": "success", "result": {"session_id": "proc-a", "session_epoch": 5}},
        "get_session_info",
    )

    assert connection._session_marker_stale.is_set() is True


def test_an_ordinary_commands_result_cannot_trip_a_re_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Any `result` carrying a `session_epoch` key used to force a fresh handshake.

    That let a *command shape* decide when the process re-reads its capability
    set: one handler returning a field of that name - a mesh dump, a scene
    report, anything - and every call to it paid for a second round trip. The
    frame-level pair is the addon's own stamp and is trusted unconditionally; a
    nested `result` is read only for the commands specified to report one.
    """
    _reset_handshake_state(monkeypatch, _cached())

    connection.note_session_marker(
        {"status": "success", "result": {"session_id": "proc-z", "session_epoch": 99}},
        "get_mesh_data",
    )

    assert connection._session_marker_stale.is_set() is False


def test_a_non_conforming_epoch_does_not_re_arm_the_flag_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A JSON-string epoch made the cached pair permanently un-equal to itself.

    `AddonHandshake.session_marker()` holds `normalized_session_epoch` output, so
    a peer reporting `"1"` (or `1.0`) against a cached `1` compared unequal on
    **every** response - the flag re-armed each time and the process ran one
    extra `get_addon_info` round trip per command, permanently. The observed pair
    is now normalized through the same helpers before it is compared. The socket
    is unauthenticated, so this is reachable input, not a hypothetical.
    """
    _reset_handshake_state(monkeypatch, _cached())

    connection.note_session_marker({"status": "success", "session_id": "proc-a", "session_epoch": "1"})

    assert connection._session_marker_stale.is_set() is False, "a normalized value must equal its cached twin"


def test_a_response_carrying_no_marker_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most responses carry no session fields, and absence is not a change."""
    _reset_handshake_state(monkeypatch, _cached())

    connection.note_session_marker({"status": "success", "result": {"pong": True}})

    assert connection._session_marker_stale.is_set() is False


class _SessionScriptedSocket:
    """
    A fake addon socket that answers every command with the session it is told to be in.

    Built rather than reused from `ScriptedSocket` because the defect this
    harness exists to catch is *re-entrant*: the refresh's own `get_addon_info`
    has to travel the same `send_command_locked` path a `ping` does, so the
    responses cannot be a fixed script - each one has to be generated from the
    request that arrives, echoing its id.

    Attributes:
        wire: Every command type this socket was asked for, in order. Counting
            `get_addon_info` here is the measurement; the previous version of
            this test replaced `force_addon_handshake` with a plain function
            that never went through `send_command`, so the re-entry the count is
            about could not happen and the test's own name was a false claim
            about production.
        session_id: The session the addon currently reports.
        session_epoch: The epoch the addon currently reports.

    """

    def __init__(self, session_id: str, session_epoch: int, capabilities: list[str]) -> None:
        """
        Start the fake addon in one session.

        Args:
            session_id: The session id to report.
            session_epoch: The epoch to report.
            capabilities: The capability set `get_addon_info` advertises.

        """
        self.wire: list[str] = []
        self.session_id = session_id
        self.session_epoch = session_epoch
        self._capabilities = capabilities
        self._pending: bytes = b""

    def settimeout(self, timeout: float) -> None:
        """
        Accept a timeout the way a socket does.

        Args:
            timeout: Ignored.

        """

    def sendall(self, data: bytes) -> None:
        """
        Answer one command, echoing its id and stamping the current session.

        Args:
            data: The framed command the connection wrote.

        """
        command = json.loads(data.decode("utf-8"))
        self.wire.append(command["type"])
        body: dict[str, object] = {
            "status": "success",
            "id": command["id"],
            # Every frame carries the pair, which is what production does.
            "session_id": self.session_id,
            "session_epoch": self.session_epoch,
        }
        if command["type"] == "get_addon_info":
            body["result"] = {
                "protocol_version": EXPECTED_ADDON_PROTOCOL_VERSION,
                "addon_version": [2, 0, 0],
                "blender_version": "5.2.2",
                "capabilities": self._capabilities,
                "session_id": self.session_id,
                "session_epoch": self.session_epoch,
            }
        else:
            body["result"] = {"pong": True}
        self._pending += json.dumps(body).encode("utf-8") + b"\n"

    def recv(self, _bufsize: int) -> bytes:
        """
        Hand back whatever the last `sendall` queued.

        Args:
            _bufsize: Ignored; a whole frame is always available.

        Returns:
            bytes: The queued response bytes.

        """
        chunk, self._pending = self._pending, b""
        return chunk


def test_one_swap_costs_exactly_one_re_handshake_over_a_real_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    One observed swap must put **one** extra `get_addon_info` on the wire, not two.

    The refresh's own `get_addon_info` response travels back through
    `send_command_locked` -> `note_session_marker` while `_addon_handshake` still
    holds the *pre*-refresh value, so the comparison found a difference and
    re-set the flag the refresh had just cleared. Reproduced before the fix::

        after ping #1: wire = ['get_addon_info', 'ping'], stale = True
        after ping #2: wire = [..., 'get_addon_info', 'ping'], stale = False
        get_addon_info round trips for ONE swap: 2

    This drives a **real** round trip for exactly that reason: the previous test
    stubbed `force_addon_handshake` with a function that never sent anything, so
    the re-entry could not occur and the count it asserted was measuring the
    stub.
    """
    _reset_handshake_state(monkeypatch, _cached())
    sock = _SessionScriptedSocket("proc-a", 2, ["ping", "get_addon_info"])
    blender = BlenderConnection(host="localhost", port=0)
    blender.sock = sock
    monkeypatch.setattr(connection, "get_blender_connection", lambda: blender)

    # Three, not two: with the re-entrancy guard removed the flag is re-set by
    # the refresh's own response, and the *third* command is where that becomes
    # a second round trip the count can see. Two pings would leave the wire
    # assertion passing and rest the whole test on the flag assertion below.
    for _call in range(3):
        blender.send_command("ping")

    handshakes = sock.wire.count("get_addon_info")
    assert handshakes == 1, f"one swap cost {handshakes} handshakes: {sock.wire}"
    assert connection._session_marker_stale.is_set() is False, "the refresh's own response re-armed the flag"


# Twenty commands, because the defect is *permanent*: it costs one extra round
# trip per command for the life of the process, so the only count that tells a
# transient retry apart from a latched one is a long one.
_COMMANDS_AFTER_A_DOUBLE_MOVE = 20


def test_a_refresh_that_learns_a_newer_session_than_the_one_observed_stops_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The addon moved twice; `pending` named an epoch it can never report again.

    `note_session_marker` pins `_OBSERVED_MARKER["pending"]` to the pair it saw,
    and the refresh re-armed the staleness flag unless the refreshed handshake
    reported **that exact pair**. But `_refreshing` suppresses
    `note_session_marker` for the refresh's own response, so the newer pair the
    refresh just learned was never recorded either. Two File -> Opens, a second
    server process against the same Blender (documented behaviour - `README.md`
    :85-89), or one arriving inside the 4.6 s `open_mainfile` window are each
    enough to put the addon past the observed epoch, and the flag then re-armed
    on every command forever::

        get_addon_info round trips for 20 commands, before: 20
        "staying stale so the next command retries" logged 20 times

    **This is the third time in this task that a fix reintroduced its own defect
    class**, and `note_session_marker`'s docstring claimed the class was closed.
    The condition is now "the refresh reported a well-formed marker", not "the
    refresh reported the stale one": a well-formed pair is by definition the
    addon's current answer, so it is what `pending` should have held all along.
    """
    _reset_handshake_state(monkeypatch, _cached(session_id="proc-a", session_epoch=1))
    # The addon is already at epoch 3 - past the epoch the observation below
    # names - which is the whole case.
    sock = _SessionScriptedSocket("proc-a", 3, ["ping", "get_addon_info"])
    blender = BlenderConnection(host="localhost", port=0)
    blender.sock = sock
    monkeypatch.setattr(connection, "get_blender_connection", lambda: blender)

    connection.note_session_marker({"status": "error", "session_id": "proc-a", "session_epoch": 2})
    assert connection._session_marker_stale.is_set() is True

    for _call in range(_COMMANDS_AFTER_A_DOUBLE_MOVE):
        blender.send_command("ping")

    handshakes = sock.wire.count("get_addon_info")
    assert handshakes == 1, (
        f"{handshakes} handshakes for {_COMMANDS_AFTER_A_DOUBLE_MOVE} commands - "
        f"the staleness flag re-armed permanently: {sock.wire}"
    )
    assert connection._session_marker_stale.is_set() is False, (
        "a refresh that reported a well-formed - and newer - session must clear the signal"
    )
    assert connection._OBSERVED_MARKER["pending"] == ("proc-a", 3), (
        f"the pair the refresh learned was discarded: {connection._OBSERVED_MARKER['pending']}"
    )


def test_a_refresh_that_fails_leaves_the_staleness_signal_standing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A failed re-handshake used to clear the signal permanently and never retry.

    `refresh_handshake_if_session_changed` clears the flag before refreshing
    (correct, against recursion) and nothing re-set it when the refresh produced
    nothing - and `_maybe_handshake_addon` swallows every exception, while
    `handshake_addon`'s fallback replaces a good handshake with a degraded one
    whose `session_epoch` is None - which is the handshake this test installs.
    The process then gated every later command on a capability set belonging to
    a file that was no longer open, for the life of the process.
    """
    _reset_handshake_state(monkeypatch, _cached())
    degraded = AddonHandshake(
        up_to_date=False,
        protocol_version=None,
        addon_version=None,
        capabilities=[],
        blender_version=None,
        source="error",
        warning="Addon handshake failed: connection reset",
    )
    monkeypatch.setattr(connection, "force_addon_handshake", lambda _blender: degraded)
    blender = BlenderConnection(host="localhost", port=0)

    connection.note_session_marker({"status": "error", "session_id": "proc-b", "session_epoch": 1})
    assert connection._session_marker_stale.is_set() is True

    connection.refresh_handshake_if_session_changed(blender)

    assert connection._session_marker_stale.is_set() is True, (
        "a refresh that never reported the observed session must leave the signal standing"
    )


def test_the_command_gate_reads_the_refreshed_capability_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The whole point: a command the *new* file supports must stop being refused.

    `capabilities` is scene-gated, so it follows the swapped `.blend`. Before
    this, the cached set was consulted for the life of the process.
    """
    _reset_handshake_state(monkeypatch, _cached(capabilities=["ping"]))
    connection._session_marker_stale.set()
    monkeypatch.setattr(
        connection,
        "force_addon_handshake",
        lambda _blender: (
            monkeypatch.setattr(
                connection, "_addon_handshake", _cached(capabilities=["ping", "import_polyhaven_asset"])
            )
            or connection._addon_handshake
        ),
    )
    conn = BlenderConnection(host="localhost", port=0)
    conn.sock = ScriptedSocket([json.dumps({"id": "x", "status": "success", "result": {}}).encode() + b"\n"])
    conn.send_command_locked = lambda command_type, _params=None: {"ok": command_type}  # pyright: ignore[reportAttributeAccessIssue]

    assert conn.send_command("import_polyhaven_asset") == {"ok": "import_polyhaven_asset"}


def test_a_barrier_rejection_read_off_the_socket_marks_the_handshake_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The observation has to be wired into the receive path, not merely available.

    `note_session_marker` is correct in isolation and useless unless
    `send_command_locked` calls it on every frame it parses. The barrier's
    rejection is an `error` frame, so the call has to happen **before** the
    error branch raises - which is exactly the ordering a later edit would
    quietly get wrong.
    """
    _reset_handshake_state(monkeypatch, _cached())
    frame = {
        "id": "cmd-1",
        "status": "error",
        "message": "Discarded without running: a session file swap was attempted ...",
        "session_id": "proc-a",
        "session_epoch": 2,
    }
    conn = BlenderConnection(host="localhost", port=0)
    conn.sock = ScriptedSocket([json.dumps(frame).encode() + b"\n"])  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setattr(uuid, "uuid4", lambda: types.SimpleNamespace(hex="cmd-1"))

    with pytest.raises(BlenderOperationError):
        conn.send_command_locked("ping")

    assert connection._session_marker_stale.is_set() is True, (
        "the barrier's own rejection did not reach note_session_marker"
    )
