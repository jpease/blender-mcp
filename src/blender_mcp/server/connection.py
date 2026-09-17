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
# Set when a response reports a different session than the cached handshake. An
# `Event` because different threads set and read it. See `note_session_marker`.
_session_marker_stale = threading.Event()
# The last session pair observed or learned, for the refresh's failure log. A dict so
# it can be updated without `global`.
_OBSERVED_MARKER: dict[str, tuple[str | None, int | None] | None] = {"pending": None}
# Set during a refresh. Thread-local so one connection's refresh cannot hide another
# connection's session change. See `note_session_marker`.
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
        # Capabilities follow the open .blend, so re-read them after a swap. Done before
        # taking the lock, because the refresh sends a command of its own.
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

            # Every frame carries the session, so a File > Open in Blender's UI is noticed
            # on the next command. Before the error branch, because the file-swap
            # barrier's rejection is itself an error.
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


# Only these commands' `result` is read for session fields. Any other handler that
# returned a `session_epoch` key would otherwise trigger a spurious re-handshake.
_SESSION_REPORTING_COMMANDS = frozenset({"get_addon_info", "get_session_info"})


def _reported_session_marker(payload: object, command_type: str | None) -> tuple[str | None, int | None] | None:
    """
    Pull `(session_id, session_epoch)` out of a response, wherever the addon put it.

    Every frame carries it at the top level; `get_addon_info` and `get_session_info` also
    nest it in `result`. The pair is normalized as the cached handshake was, or a peer's
    `"7"` would never equal the cached `7` and every command would re-handshake.

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

    The cached capabilities and writable output roots depend on the open .blend. The
    whole pair is compared because the epoch restarts at 0 when the addon reloads.

    The refresh's own response is ignored: it arrives while `_addon_handshake` still holds
    the old value and would re-set the flag, costing a second round trip. The guard is a
    plain bool, safe only because the refresh clears the flag first, so the nested gate
    returns early.

    Only flags; the connection lock is held here, and a handshake sends a command of its
    own. `refresh_handshake_if_session_changed` does the re-read.

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

    The flag is cleared first, because the refresh's own command passes through this gate.
    It is set again if the refresh yields no complete session pair: handshake failures
    are swallowed into a degraded handshake, and dropping the signal would gate every later
    command on the capabilities of a file no longer open.

    Any complete pair counts as success, not only the one observed. The addon may have
    moved on since, and the refresh's own response is never observed, so waiting for the
    old pair would re-handshake on every command.

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
