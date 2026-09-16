"""Socket connection to the Blender addon, plus addon-handshake state."""

import json
import logging
import os
import socket
import threading
import uuid

from dataclasses import dataclass, field
from typing import Any

from ..addon_manager import (
    AddonHandshake,
    format_handshake_log,
    handshake_addon,
    normalized_session_epoch,
    normalized_session_id,
)

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("BlenderMCPServer")

# Default configuration
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9876

_addon_handshake = None
_addon_handshake_checked = False
_addon_handshake_lock = threading.Lock()
# Set when a response reports a `(session_id, session_epoch)` pair that differs
# from the one the cached handshake was taken under. An `Event` rather than a
# bool: it is written from whichever thread parsed the response and read from
# whichever thread sends the next command, and its set/clear are atomic without
# either of them reaching for `global`. See `note_session_marker`.
_session_marker_stale = threading.Event()
# The pair that set the flag, so a refresh can tell "the addon now reports what
# I saw" from "the refresh failed and told me nothing". Without it a degraded
# handshake - `session_epoch: None` - looked like a successful refresh. A
# one-key holder rather than a bare name: it is written where the response is
# parsed and read where the next command is gated, and mutating a container
# needs no `global` in either place.
_OBSERVED_MARKER: dict[str, tuple[str | None, int | None] | None] = {"pending": None}
# Re-entrancy guard for the refresh itself, thread-local so a refresh on one
# connection cannot blind another connection's observation. See
# `note_session_marker` for the double round trip it prevents.
_refreshing = threading.local()


class BlenderOperationError(Exception):
    """
    Raised when Blender reports the requested operation failed.

    Kept distinct from the generic `except Exception` in
    `send_command_locked` so a clean failure message isn't relabeled as a
    communication error and doesn't drop a perfectly good socket.
    """


def ad_hoc_failure_message(result: object) -> str | None:
    """
    Detect an addon handler that returned a failure shape instead of raising.

    Many addon handlers return {"error": "..."}, {"succeed": False, "error":
    "..."}, or (from an unmatched dispatch branch) a bare "Error: ..."
    string, instead of raising. None of these set the top-level
    {"status": "error"} envelope that send_command otherwise checks for, so
    without this check a failed Blender-side operation would come back to
    the caller looking like a successful result.

    Args:
        result: The unwrapped `result` value from a Blender response.

    Returns:
        str | None: The failure message if `result` matches one of the
        known ad-hoc failure shapes, else None.

    """
    if isinstance(result, dict):
        if result.get("succeed") is False:
            return str(result.get("error") or result)
        error = result.get("error")
        if error:
            return str(error)
        return None
    if isinstance(result, str) and result.startswith("Error:"):
        return result
    return None


@dataclass
class BlenderConnection:
    """Manage a serialized socket connection to a Blender addon."""

    # Messages are newline-delimited JSON (see server_core.py's handle_client
    # for why framing is required). Keep this in sync with that file's
    # _MAX_MESSAGE_BYTES.
    _MAX_MESSAGE_BYTES = 64 * 1024 * 1024

    host: str
    port: int
    sock: socket.socket = None  # Changed from 'socket' to 'sock' to avoid naming conflict
    # Serializes send+receive so two commands can never interleave on one socket.
    # Without this, a second command's response can be read as the first's, and
    # the stream stays desynced until the 180s timeout fires.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Bytes received past the current message's `\n` terminator, carried over
    # to the next receive_full_response() call instead of being discarded.
    _recv_buffer: bytes = field(default=b"", repr=False)

    def connect(self) -> bool:
        """
        Connect to the Blender addon socket server.

        Returns:
            bool: Result produced by the operation.

        """
        if self.sock:
            return True

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            self._recv_buffer = b""
            logger.info(f"Connected to Blender at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Blender: {e!s}")
            self.sock = None
            return False

    def disconnect(self) -> None:
        """Disconnect from the Blender addon."""
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error(f"Error disconnecting from Blender: {e!s}")
            finally:
                self.sock = None

    def receive_full_response(self, sock, buffer_size=8192):
        r"""
        Receive exactly one newline-delimited JSON response.

        Messages are terminated by a single `\n` (see server_core.py's
        handle_client for why explicit framing is needed - trying to
        json.loads() a growing buffer can't tell "incomplete message" apart
        from "complete message plus the start of the next one", and treating
        the latter as incomplete means it can never parse again). Any bytes
        received past the terminator are kept in self._recv_buffer for the
        next call, in case the addon ever sends more than one frame per
        recv().

        Args:
            sock: Value for sock.
            buffer_size: Value for buffer size.

        Returns:
            Result produced by the operation.

        Raises:
            Exception: If the connection closes or the message never
                terminates within the size cap.
            BrokenPipeError: If the peer closes its write end during a receive.
            ConnectionError: If the socket connection fails while receiving.
            ConnectionResetError: If the peer resets the connection while receiving.
            TimeoutError: If no complete message arrives before the timeout.

        """
        sock.settimeout(180.0)  # Match the addon's timeout

        while b"\n" not in self._recv_buffer:
            if len(self._recv_buffer) > self._MAX_MESSAGE_BYTES:
                raise Exception(
                    f"Response exceeded max size ({len(self._recv_buffer)} bytes) without a terminator"
                )
            chunk = sock.recv(buffer_size)
            if not chunk:
                if not self._recv_buffer:
                    raise Exception("Connection closed before receiving any data")
                raise Exception("Connection closed mid-message")
            self._recv_buffer += chunk

        line, self._recv_buffer = self._recv_buffer.split(b"\n", 1)
        if len(line) > self._MAX_MESSAGE_BYTES:
            raise Exception(f"Response exceeded max size ({len(line)} bytes)")
        logger.info(f"Received complete response ({len(line)} bytes)")
        return line

    def send_command(self, command_type: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Send a command to Blender and return the response.

        Args:
            command_type: Value for command type.
            params: Value for params.

        Returns:
            dict[str, Any]: Result produced by the operation.

        Raises:
            Exception: If the operation cannot be completed.

        """
        # Not `get_last_handshake()`: that returns whatever was cached when this
        # process first connected, and the addon's `capabilities` set follows
        # the open `.blend`. If a swap has been observed since, the set is
        # re-read here - before the gate consults it, and before the connection
        # lock is taken, because the refresh sends a command of its own.
        handshake = refresh_handshake_if_session_changed(self)
        if handshake and handshake.capabilities and command_type not in handshake.capabilities:
            raise Exception(
                f"'{command_type}' is not supported by the installed Blender addon "
                f"(protocol {handshake.protocol_version}). Update the addon and reconnect."
            )
        # Hold the lock across send+receive: the response is matched to the
        # command purely by ordering on the stream (backstopped by the id
        # check below), so overlapping calls would hand each other's
        # responses back.
        with self._lock:
            return self.send_command_locked(command_type, params)

    def send_command_locked(self, command_type: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Blender")

        command = {"id": uuid.uuid4().hex, "type": command_type, "params": params or {}}

        try:
            # Log the command being sent
            logger.info(f"Sending command: {command_type} (request {command['id']}, {len(command['params'])} params)")

            # Send the command. Newline-terminated - see receive_full_response
            # for why this protocol needs explicit framing.
            self.sock.sendall(json.dumps(command).encode("utf-8") + b"\n")
            logger.info("Command sent, waiting for response...")

            # Set a timeout for receiving - use the same timeout as in receive_full_response
            self.sock.settimeout(180.0)  # Match the addon's timeout

            # Receive the response using the improved receive_full_response method
            response_data = self.receive_full_response(self.sock)
            logger.info(f"Received {len(response_data)} bytes of data")

            response = json.loads(response_data.decode("utf-8"))
            logger.info(f"Response parsed, status: {response.get('status', 'unknown')}")

            # Every response is a chance to notice the addon swapped its
            # database: the addon stamps the pair onto *every* frame, so an
            # artist doing File -> Open in Blender's own UI is observed on
            # whatever command happens next rather than only on a barrier
            # rejection or a handshake. Checked before the error branch below,
            # because the barrier's rejection *is* an error and is the most
            # direct notice there is.
            note_session_marker(response, command_type)

            if response.get("id") != command["id"]:
                # The lock already serializes one in-flight request per
                # connection, so this should be unreachable - but if the
                # stream ever desyncs, fail loudly instead of silently
                # returning another command's response.
                raise Exception(
                    f"Response id {response.get('id')!r} does not match request id {command['id']!r} - "
                    "the connection to Blender is desynced"
                )

            if response.get("status") == "error":
                logger.error(f"Blender error: {response.get('message')}")
                raise BlenderOperationError(response.get("message", "Unknown error from Blender"))

            result = response.get("result", {})
            failure_message = ad_hoc_failure_message(result)
            if failure_message is not None:
                logger.error(f"Blender handler reported failure without raising: {failure_message}")
                raise BlenderOperationError(failure_message)

            return result
        except TimeoutError as exc:
            logger.error("Socket timeout while waiting for response from Blender")
            # Don't try to reconnect here - let the get_blender_connection handle reconnection
            # Just invalidate the current socket so it will be recreated next time
            self.sock = None
            raise Exception(
                "Timeout waiting for Blender response - try simplifying your request. If Blender is running headless (blender -b), commands never execute; run Blender with a GUI or via 'xvfb-run -a blender' instead"
            ) from exc
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
            logger.error(f"Socket connection error: {e!s}")
            self.sock = None
            raise Exception(f"Connection to Blender lost: {e!s}") from e
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from Blender: {e!s}")
            # Try to log what was received
            if "response_data" in locals() and response_data:
                logger.error(f"Raw response (first 200 bytes): {response_data[:200]}")
            raise Exception(f"Invalid response from Blender: {e!s}") from e
        except BlenderOperationError:
            raise
        except Exception as e:
            logger.error(f"Error communicating with Blender: {e!s}")
            # Don't try to reconnect here - let the get_blender_connection handle reconnection
            self.sock = None
            raise Exception(f"Communication error with Blender: {e!s}") from e


# Global connection for resources (since resources can't access context)
_blender_connection = None


def _maybe_handshake_addon(blender: BlenderConnection) -> None:
    """
    Run addon version handshake once per process after a live connection.

    Args:
        blender: Value for blender.

    """
    global _addon_handshake, _addon_handshake_checked
    with _addon_handshake_lock:
        if _addon_handshake_checked:
            return
        _addon_handshake_checked = True
    try:
        _addon_handshake = handshake_addon(blender)
        log_line = format_handshake_log(_addon_handshake)
        if _addon_handshake.up_to_date:
            logger.info(log_line)
        else:
            logger.warning(log_line)
    except Exception as e:
        logger.debug(f"Addon handshake skipped: {e}")


def get_blender_connection():
    """
    Get or create a persistent Blender connection.

    Returns:
        Result produced by the operation.

    Raises:
        Exception: If the operation cannot be completed.

    """
    global _blender_connection

    # Reuse the existing connection. We deliberately do NOT probe it with a
    # command here: that put two commands on the wire for every tool call, and
    # any overlap desynced the response stream until the socket timeout fired.
    # A dead socket is detected by the next real command and reconnected then.
    if _blender_connection is not None and _blender_connection.sock is not None:
        return _blender_connection

    # Create a new connection if needed
    if _blender_connection is None:
        host = os.getenv("BLENDER_HOST", DEFAULT_HOST)
        port = int(os.getenv("BLENDER_PORT", DEFAULT_PORT))
        _blender_connection = BlenderConnection(host=host, port=port)
        if not _blender_connection.connect():
            logger.error("Failed to connect to Blender")
            _blender_connection = None
            raise Exception("Could not connect to Blender. Make sure the Blender addon is running.")
        logger.info("Created new persistent connection to Blender")
        _maybe_handshake_addon(_blender_connection)

    return _blender_connection


def disconnect_blender() -> None:
    """
    Disconnect and clear the module-level Blender connection, if any.

    Lives here (not in app.py's server_lifespan) for the same reason
    force_addon_handshake does: reaching into another module's `global` via a
    plain import copies the reference, not a live binding, so clearing the
    connection on shutdown needs a real function in the module that owns it.
    """
    global _blender_connection
    if _blender_connection:
        logger.info("Disconnecting from Blender on shutdown")
        _blender_connection.disconnect()
        _blender_connection = None


def force_addon_handshake(blender: BlenderConnection) -> AddonHandshake | None:
    """
    Force a fresh addon handshake, bypassing the once-per-process cache.

    Does what `get_addon_status` used to do inline against this module's
    globals directly. That can't be replicated across a module boundary via a
    plain `from ..connection import _addon_handshake_checked` (it copies the
    reference, not a live binding), so this needs to be a real function here.

    Args:
        blender: Value for blender.

    Returns:
        AddonHandshake | None: Result produced by the operation.

    """
    global _addon_handshake_checked
    with _addon_handshake_lock:
        _addon_handshake_checked = False
    _maybe_handshake_addon(blender)
    return _addon_handshake


# Commands whose `result` nests the session fields. Frame level is read on every
# response, because the addon stamps it there unconditionally; `result` level is
# read only for the two commands that are *specified* to report it. Without this
# gate, any handler returning a `result` that happens to contain a
# `session_epoch` key tripped a re-handshake - a command shape, not a session
# change, deciding when the process re-reads its capabilities.
_SESSION_REPORTING_COMMANDS = frozenset({"get_addon_info", "get_session_info"})


def _reported_session_marker(payload: object, command_type: str | None) -> tuple[str | None, int | None] | None:
    """
    Pull `(session_id, session_epoch)` out of a response, wherever the addon put it.

    Two shapes carry it, and both matter. Every frame carries the pair at
    **frame** level, including the file-swap barrier's rejection, whose command
    never ran and has no `result` at all. `get_addon_info` and `get_session_info`
    additionally nest it inside `result` like every other field they report.

    **The pair is normalized through the same helpers the cached handshake was
    parsed with**, and that is not cosmetic. `AddonHandshake.session_marker()`
    holds `normalized_session_id` / `normalized_session_epoch` output; returning
    the raw JSON values here meant a non-conforming peer's `"7"` or `7.0` could
    never equal the cached `7`, so the stale flag re-armed on every single
    response - one extra `get_addon_info` round trip per command, permanently.
    A JSON-string epoch is the cheapest way to trigger it, and the socket is
    unauthenticated;
    `test_a_non_conforming_epoch_does_not_re_arm_the_flag_forever` drives one.

    Args:
        payload: A decoded response frame; any JSON value, since it came off the
            socket.
        command_type: The command this frame answers, or None when unknown.

    Returns:
        tuple | None: The normalized pair, or None when this response carries no
        session fields at all.

    """
    if not isinstance(payload, dict):
        return None
    sources: list[object] = [payload]
    if command_type in _SESSION_REPORTING_COMMANDS:
        sources.append(payload.get("result"))
    for source in sources:
        if isinstance(source, dict) and ("session_epoch" in source or "session_id" in source):
            return (
                normalized_session_id(source.get("session_id")),
                normalized_session_epoch(source.get("session_epoch")),
            )
    return None


def note_session_marker(payload: object, command_type: str | None = None) -> None:
    """
    Flag the cached handshake stale when the addon reports a different session.

    `send_command` gates **every** command on `capabilities`, which is cached
    once per process behind `_addon_handshake_checked` and was refreshed only by
    an explicit `force_addon_handshake()`. That set is scene-gated - the Poly
    Haven / Sketchfab / ND handlers are advertised only when this `.blend`'s
    `blendermcp_use_*` flags say so - so it follows the swapped file, and
    `writable_output_roots` changes across a swap as well, which decides where
    files may be written. Before this, nothing noticed.

    The **pair** is compared, not the counter. The addon's module state is
    rebuilt at epoch 0 by a Blender restart or Reload Scripts, so epoch 1 ->
    restart -> 0 -> one swap -> 1 reads as "unchanged" to a counter-only
    comparison while being a different database in a different process.

    **A refresh's own response is not read as news.** `force_addon_handshake`
    calls `handshake_addon`, which sends `get_addon_info` through
    `BlenderConnection.send_command` - the **gating** wrapper, not
    `send_command_locked` (`addon_manager.py:739`; an earlier revision of this
    sentence named the wrong one). So the refresh re-enters
    `refresh_handshake_if_session_changed` on its way out, and its response lands
    back here while `_addon_handshake` still holds the *pre*-refresh value - so
    the comparison found a difference and re-set the flag that the refresh had
    just cleared, and the next command paid for a second round trip::

        after ping #1: wire = ['get_addon_info', 'ping'], stale = True
        after ping #2: wire = [..., 'get_addon_info', 'ping'], stale = False
        get_addon_info round trips for ONE swap: 2

    `_refreshing` is set for the duration of the refresh, on that thread only,
    which makes the inner call a no-op. It is thread-local rather than a module
    flag so a refresh on one connection cannot blind another connection's
    observation.

    **What that re-entry costs, stated because the recursion is real rather than
    hypothetical.** Going through the gating wrapper means
    `refresh_handshake_if_session_changed` runs again inside the refresh. It is
    bounded: the outer call clears `_session_marker_stale` before it sets
    `_refreshing.active`, so the inner call returns at its first line without
    sending anything. What it is **not** is reentrant - `_refreshing.active` is a
    plain bool, so a nested refresh that did reach its `finally` would clear the
    outer guard while the outer refresh was still in flight, and the outer
    refresh's own response would then be read as news again. Nothing today
    reaches that, because the inner call cannot get past the staleness check;
    it holds by arithmetic on one flag, not by the flag being a counter.

    **That suppression closed one re-arm loop and opened another**, and this
    docstring previously read as though the class were closed. Because the
    refresh's own response is skipped here, the pair it reports is recorded
    nowhere by this function - so when the addon has moved *past* the epoch
    observed above, `_OBSERVED_MARKER["pending"]` keeps naming an epoch that can
    never come back and the flag re-arms on every command, permanently: 20 extra
    round trips for 20 commands, measured. The recording is therefore done by
    `refresh_handshake_if_session_changed`, which is the one place that holds the
    refreshed handshake; see its docstring for the test that pins it.

    Nothing is re-read here: this runs while a response is being parsed, with
    the connection lock held, and a handshake sends a command of its own.
    `refresh_handshake_if_session_changed` does the work, before the next
    command is gated.

    Args:
        payload: The decoded response frame.
        command_type: The command this frame answers, used to decide whether a
            nested `result` may be read as a session report.

    """
    if getattr(_refreshing, "active", False):
        return
    observed = _reported_session_marker(payload, command_type)
    if observed is None or _addon_handshake is None or observed == _addon_handshake.session_marker():
        return
    logger.info(f"Blender session changed ({_addon_handshake.session_marker()} -> {observed}); handshake is stale")
    _OBSERVED_MARKER["pending"] = observed
    _session_marker_stale.set()


def refresh_handshake_if_session_changed(blender: BlenderConnection) -> AddonHandshake | None:
    """
    Re-read the handshake when a swap has been observed, then answer with it.

    **The staleness signal survives a failed refresh.** Clearing the flag before
    the refresh was right as far as it went - `force_addon_handshake` sends a
    command, which re-enters `send_command` - but nothing re-set it when the
    refresh did not produce a handshake at all, and `_maybe_handshake_addon`
    swallows every exception. Worse, `handshake_addon`'s own fallback replaces a
    good handshake with a degraded one whose `session_epoch` is None, so the
    process then gated every subsequent command on a capability set belonging to
    a file that was no longer open, permanently.
    `test_a_refresh_that_fails_leaves_the_staleness_signal_standing` drives that
    degraded handshake and asserts the signal survives it. Recursion is prevented
    by `_refreshing`, a re-entrancy flag, rather than by discarding the signal.

    **The test is "well-formed", not "equal to what was observed", and the
    difference is a permanent retry loop.** Requiring equality assumed the addon
    would still be at the epoch `note_session_marker` happened to see. It need
    not be: two File -> Opens, a second server process against the same Blender
    (documented in `README.md:85-89`), or one swap arriving inside the measured
    4.6 s `open_mainfile` window all move it again first. `_refreshing` then
    suppresses `note_session_marker` for the refresh's *own* response, so the
    newer pair it just learned was recorded nowhere either, and `pending` named
    an epoch the addon can never report again. Measured before the fix: **20
    extra `get_addon_info` round trips for 20 commands**, with the warning above
    logged 20 times, for the life of the process -
    `test_a_refresh_that_learns_a_newer_session_than_the_one_observed_stops_retrying`
    reproduces exactly that. A refreshed handshake carrying both halves of the
    pair *is* the addon's current answer, so it is adopted as the observation
    rather than compared against a stale one. Only a handshake with a missing
    half - the degraded fallback - leaves the signal standing.

    Args:
        blender: The connection to re-handshake over.

    Returns:
        AddonHandshake | None: The current handshake, refreshed if it was stale.

    """
    if not _session_marker_stale.is_set():
        return _addon_handshake
    observed = _OBSERVED_MARKER["pending"]
    _session_marker_stale.clear()
    logger.info("Re-handshaking: the addon reported a different session since the cached handshake")
    _refreshing.active = True
    try:
        refreshed = force_addon_handshake(blender)
    finally:
        _refreshing.active = False

    learned = refreshed.session_marker() if refreshed is not None else None
    if learned is None or None in learned:
        logger.warning(
            f"Re-handshake reported no usable session (observed {observed}, got {learned}); "
            "staying stale so the next command retries"
        )
        _session_marker_stale.set()
    else:
        _OBSERVED_MARKER["pending"] = learned
    return refreshed


def get_last_handshake() -> AddonHandshake | None:
    """
    Read accessor for the most recent addon handshake result, if any.

    Returns:
        AddonHandshake | None: Result produced by the operation.

    """
    return _addon_handshake
