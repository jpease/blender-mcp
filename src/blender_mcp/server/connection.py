"""Socket connection to the Blender addon, plus addon-handshake state."""

import json
import logging
import os
import socket
import threading
import time
import uuid

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..addon_manager import AddonHandshake, format_handshake_log, handshake_addon

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("BlenderMCPServer")

# Default configuration
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9876

# Blender is regularly not listening at the moment the server first reaches for
# it: the addon may still be enabling, or a container may have started without
# having bound its port yet. Retrying the *initial* connect turns that race into
# a short wait instead of a failed first tool call. Set BLENDER_CONNECT_ATTEMPTS
# to 1 to restore single-shot behaviour.
DEFAULT_CONNECT_ATTEMPTS = 3
DEFAULT_CONNECT_RETRY_DELAY = 0.5

# A separate, much larger budget for a socket that accepts but cannot answer
# yet. Docker publishes a container's port before Blender is listening inside
# it, so connect() succeeds immediately and only a real command reveals that
# Blender is still booting - measured at ~9s for this rig. Nothing listening at
# all stays on the small budget above, so a genuinely absent Blender still
# fails in about a second instead of hanging for half a minute.
DEFAULT_STARTUP_ATTEMPTS = 40

_addon_handshake = None
_addon_handshake_checked = False
_addon_handshake_lock = threading.Lock()


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
                raise Exception(f"Response exceeded max size ({len(self._recv_buffer)} bytes) without a terminator")
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
        handshake = get_last_handshake()
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


def connect_attempts() -> int:
    """
    Read how many times the initial connection should be attempted.

    Returns:
        int: Configured attempt count, never below 1.

    """
    try:
        return max(1, int(os.getenv("BLENDER_CONNECT_ATTEMPTS", DEFAULT_CONNECT_ATTEMPTS)))
    except ValueError:
        logger.warning("Ignoring unparseable BLENDER_CONNECT_ATTEMPTS; using %s", DEFAULT_CONNECT_ATTEMPTS)
        return DEFAULT_CONNECT_ATTEMPTS


def startup_attempts() -> int:
    """
    Read how long to keep waiting on a socket that accepts but stays silent.

    Returns:
        int: Configured attempt count, never below 1.

    """
    try:
        return max(1, int(os.getenv("BLENDER_STARTUP_ATTEMPTS", DEFAULT_STARTUP_ATTEMPTS)))
    except ValueError:
        logger.warning("Ignoring unparseable BLENDER_STARTUP_ATTEMPTS; using %s", DEFAULT_STARTUP_ATTEMPTS)
        return DEFAULT_STARTUP_ATTEMPTS


def connect_retry_delay() -> float:
    """
    Read how long to wait between initial-connection attempts, in seconds.

    Returns:
        float: Configured delay.

    """
    try:
        return float(os.getenv("BLENDER_CONNECT_RETRY_DELAY", DEFAULT_CONNECT_RETRY_DELAY))
    except ValueError:
        logger.warning("Ignoring unparseable BLENDER_CONNECT_RETRY_DELAY; using %s", DEFAULT_CONNECT_RETRY_DELAY)
        return DEFAULT_CONNECT_RETRY_DELAY


def connection_is_ready(connection: BlenderConnection) -> bool:
    """
    Report whether a connected Blender actually answers a command.

    An accepted socket is not proof of readiness when a port forwarder sits in
    front of Blender, so this asks for a real reply. `ping` is used because it
    is the cheapest command every addon build exposes.

    Args:
        connection: A connected BlenderConnection.

    Returns:
        bool: True when Blender answered, False when it did not.

    """
    try:
        connection.send_command("ping")
    except Exception as exc:
        logger.info("Socket accepted but Blender is not answering yet: %s", exc)
        return False
    return True


def connect_with_retry(
    connection: BlenderConnection,
    *,
    attempts: int,
    delay: float,
    sleep: Callable[[float], None] | None = None,
    probe: Callable[[BlenderConnection], bool] | None = None,
    startup_attempts: int = 0,
) -> bool:
    """
    Attempt `connection.connect()` until it succeeds or the attempts run out.

    Wraps `BlenderConnection.connect` rather than changing it: every tool call
    reaches `connect` through `get_blender_connection`, including the lazy
    reconnect after a dropped socket, so retrying inside `connect` itself would
    add latency to paths that deliberately fail fast.

    Args:
        connection: Object exposing a `connect()` returning truthy on success.
        attempts: Total attempts to make, including the first.
        delay: Seconds to wait between attempts.
        sleep: Injectable sleep, for tests. Resolved at call time so patching
            `time.sleep` on this module works too.
        probe: Optional readiness check run after connecting. Without it an
            accepted socket counts as success, which is wrong behind a port
            forwarder that accepts before Blender is listening.
        startup_attempts: Separate budget for the case where the socket is
            accepted but `probe` says Blender is not answering. Kept apart from
            `attempts` so a slow boot can be waited out without making an
            absent Blender slow to report.

    Returns:
        bool: True once connected and ready, False if every attempt failed.

    """
    sleeper = time.sleep if sleep is None else sleep
    unreachable_left = attempts
    starting_left = startup_attempts

    while True:
        connected = connection.connect()
        if connected and (probe is None or probe(connection)):
            return True

        if connected:
            starting_left -= 1
            remaining = starting_left
            reason = "Blender is accepting connections but has not finished starting"
        else:
            unreachable_left -= 1
            remaining = unreachable_left
            reason = "Blender is not reachable"

        if remaining <= 0:
            return False
        logger.info("%s; retrying in %ss (%s attempts left)", reason, delay, remaining)
        sleeper(delay)


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
        if not connect_with_retry(
            _blender_connection,
            attempts=connect_attempts(),
            delay=connect_retry_delay(),
            probe=connection_is_ready,
            startup_attempts=startup_attempts(),
        ):
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


def get_last_handshake() -> AddonHandshake | None:
    """
    Read accessor for the most recent addon handshake result, if any.

    Returns:
        AddonHandshake | None: Result produced by the operation.

    """
    return _addon_handshake
