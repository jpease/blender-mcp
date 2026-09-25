"""
The add-on's socket transport: the listening socket, one thread per client, and the framing.

Messages are newline-delimited JSON in both directions, capped at `_MAX_MESSAGE_BYTES`.
Client threads only frame, decode and queue; every command runs on Blender's main thread,
drained by `server_core.BlenderMCPServer`, which composes this mixin with the command
registry and the handlers.
"""

import json
import logging
import queue
import socket
import threading
import time

from collections.abc import Callable
from contextlib import suppress

import bpy

logger = logging.getLogger(__name__)


def extract_frames(buffer: bytes, max_message_bytes: int) -> tuple[list[bytes], bytes, bool]:
    r"""
    Split a receive buffer into the complete `\n`-delimited frames it holds.

    A single json.loads() over the whole buffer cannot tell "incomplete
    message" apart from "complete message plus the start of the next one":
    both raise json.JSONDecodeError, and treating the second as "wait for
    more" means the buffer can never parse again, since the trailing bytes are
    never valid on their own. Splitting on the newline terminator each side
    appends removes the ambiguity - each complete line is exactly one message.
    An empty line is a stray terminator, not a frame, and is skipped.

    Both size rules live here, because both are the same protocol violation
    seen at different moments: a frame larger than the cap, and a remainder
    larger than it with no terminator in sight. Without the second, a client
    that never terminates a message would grow the buffer forever.

    Scans each byte once (`find` from where the last frame ended, rather than
    re-splitting the tail per frame), so a pipelined burst costs one pass.

    Args:
        buffer: Everything received on this connection and not yet consumed.
        max_message_bytes: The largest single frame, and the largest
            terminator-less remainder, this server accepts.

    Returns:
        tuple[list[bytes], bytes, bool]: The complete frames, terminator
        excluded; the bytes left for the next `recv`; and whether the
        connection must be dropped. Frames that arrived intact before an
        oversized one are still returned - they are worth answering.

    """
    frames: list[bytes] = []
    start = 0
    while True:
        end = buffer.find(b"\n", start)
        if end < 0:
            break
        line = buffer[start:end]
        start = end + 1
        if not line:
            continue
        if len(line) > max_message_bytes:
            return frames, buffer[start:], True
        frames.append(line)
    return frames, buffer[start:], len(buffer) - start > max_message_bytes


def parse_command_frame(line: bytes) -> tuple[dict[str, object] | None, str | None, str | None]:
    """
    Decide whether one frame is a command this server can queue.

    Transport shape only - `id`, `type` and `params` - which is all this layer
    can judge: it runs on a client thread, where reading bpy data is not
    allowed. Any well-shaped frame is queued and judged further by the main
    thread.

    Pure: the caller owns the socket write, the session stamp and the queue.

    Args:
        line: One frame's bytes, terminator excluded.

    Returns:
        tuple[dict | None, str | None, str | None]: The command, or None when
        the frame is not one; the id to answer under, or None when the frame
        never produced a usable one; and a client-safe protocol error, or None
        when the frame is acceptable.

    """
    try:
        command = json.loads(line.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, None, "Malformed UTF-8 JSON command"

    if not isinstance(command, dict):
        return None, None, "Command must be a JSON object"
    request_id = command.get("id")
    if request_id is not None and not isinstance(request_id, str):
        # No id to answer under: a non-string one cannot be echoed back as the
        # protocol's `id`, and inventing one would match nothing on the client.
        return None, None, "Command id must be a string when provided"
    if not isinstance(command.get("type"), str) or not command["type"].strip():
        return None, request_id, "Command type must be a non-empty string"
    if not isinstance(command.get("params", {}), dict):
        return None, request_id, "Command params must be a JSON object"
    return command, request_id, None


class SocketTransportMixin:
    """Accept clients, frame what they send onto the command queue, and write frames back."""

    # Messages are newline-delimited JSON. Bound how large a single message
    # can grow before we give up on it - without this, malformed input (or
    # a client that never sends a terminator) would make `buffer` grow
    # forever. The largest legitimate payloads are paginated mesh/element
    # dumps (capped well under 1000 elements); screenshots are written to
    # disk and never cross the socket. 64 MiB is generous headroom above that.
    _MAX_MESSAGE_BYTES = 64 * 1024 * 1024

    # How often a client thread's recv() wakes to check self.running; also what
    # `_send_bounded` restores after a shorter write.
    _CLIENT_SOCKET_TIMEOUT_SECONDS = 1.0

    def start(self) -> None:
        """
        Listen on `host`:`port` and start draining the command queue on Blender's main thread.

        Refused in background mode, where the drain timer would never fire, and while
        already running. A failure part-way through bring-up is logged and undone by `stop()`.
        """
        if bpy.app.background:
            logger.error(
                "Cannot start the server in background mode (blender -b) - commands would never execute. "
                "Run Blender with a GUI, or use a virtual display: xvfb-run -a blender"
            )
            return

        if self.running:
            logger.warning("Server is already running")
            return

        self.running = True

        try:
            self._start_serving()
        except Exception:
            logger.exception("Failed to start the server on %s:%s", self.host, self.port)
            self.stop()

    def _start_serving(self) -> None:
        """
        Bind the listening socket, start its accept thread, and register the drain timer.

        `start`'s whole bring-up, kept in one call because `start` undoes all of it with
        `stop()` when any step raises.
        """
        # Create socket
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((self.host, self.port))
        # Backlog of 1 meant a reconnecting client could complete the TCP
        # handshake and then never be accept()ed - a connection that looks
        # established but is never serviced.
        self.socket.listen(5)

        # Start server thread
        self.server_thread = threading.Thread(target=self._server_loop)
        self.server_thread.daemon = True
        self.server_thread.start()

        # start() is called from the operator, i.e. the main thread, so
        # this is the only safe place to touch bpy.app.timers.
        self._register_drain_timer()

        logger.info("Server started on %s:%s", self.host, self.port)

    def stop(self) -> None:
        """
        Close the listening socket and every client, drop queued commands, and remove the drain timer.

        Safe on a server that never started or already stopped: a failed `start()`
        calls it to undo a partial bring-up.
        """
        self.running = False
        self._unregister_drain_timer()

        # Close socket
        if self.socket:
            with suppress(Exception):
                self.socket.close()
            self.socket = None

        # Shut down live client sockets. Without this, handler threads stay
        # parked in a blocking recv() forever; being daemon threads they then
        # outlive the restart and close connections the new server owns
        # (the WinError 10054 seen after toggling the addon).
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            with suppress(Exception):
                client.shutdown(socket.SHUT_RDWR)
            with suppress(Exception):
                client.close()

        # Drop any commands that will never be serviced now.
        while True:
            try:
                self.command_queue.get_nowait()
            except queue.Empty:
                break

        # Wait for thread to finish
        if self.server_thread:
            try:
                if self.server_thread.is_alive():
                    self.server_thread.join(timeout=1.0)
            except Exception:
                pass
            self.server_thread = None

        logger.info("Server stopped")

    def _server_loop(self) -> None:
        """Accept connections until the server stops, on the thread `start` gives it."""
        logger.info("Server thread started")
        self.socket.settimeout(1.0)  # Timeout to allow for stopping

        while self.running:
            try:
                self._accept_client()
            except Exception:
                logger.exception("Server loop error")
                if not self.running:
                    break
                time.sleep(0.5)

        logger.info("Server thread stopped")

    def _accept_client(self) -> None:
        """
        Wait up to the listening socket's timeout for one connection, and serve it on its own thread.

        A timeout is the loop's chance to re-check `running`, not a failure; any
        other accept failure is logged and paced so a persistent one cannot spin.
        """
        # Accept new connection
        try:
            client, address = self.socket.accept()
            logger.info("Connected to client: %s", address)

            # Handle client in a separate thread
            client_thread = threading.Thread(target=self.handle_client, args=(client,))
            client_thread.daemon = True
            client_thread.start()
        except TimeoutError:
            # Just check running condition
            return
        except Exception:
            logger.exception("Could not accept a connection; retrying")
            time.sleep(0.5)

    def _client_departed(self, client: object) -> bool:
        """
        Report whether a queued command's client has disconnected since it queued.

        `handle_client` registers its socket before it reads a frame and removes
        it only once the peer has gone, as does `_abandon_unreachable_client`, so
        a queued command whose socket is no longer registered has nobody to
        answer. A command queued with no client at all is never treated as
        orphaned.

        Args:
            client: The socket the command arrived on, or None.

        Returns:
            bool: True when the command's client has disconnected.

        """
        if client is None:
            return False
        with self._clients_lock:
            return client not in self._clients

    def _send_bounded(self, client: object, payload: bytes, timeout: float) -> bool:
        """
        Write one frame under a caller-chosen deadline, then put the socket's own back.

        The socket's `handle_client` thread is blocked in `recv()` on it at the
        same time, and a short timeout left behind would make that loop spin.
        Never pass 0, which makes the socket non-blocking. The restore is in a
        `finally` because this runs on Blender's main thread, where Esc raises
        KeyboardInterrupt: an `except Exception` would step over that and leave
        a 1 ms timeout behind, spinning that client's `recv()` at ~1 kHz for
        the rest of the connection. If the timeout cannot be put back, the send
        counts as failed, so the caller closes the socket.

        Args:
            client: The socket to write to.
            payload: The framed bytes.
            timeout: Seconds the write may block, applied to the write lock and
                to `sendall` separately.

        Returns:
            bool: True when the frame was written and the socket's own timeout
            restored; False otherwise.

        """
        settimeout = getattr(client, "settimeout", None)
        try:
            if settimeout is not None:
                settimeout(timeout)
            self._send_frame(client, payload, timeout)
        except Exception:
            delivered = False
        else:
            delivered = True
        finally:
            # Not a `return` in the `finally`: that would swallow the
            # KeyboardInterrupt this block exists to survive.
            restored = self._restore_socket_timeout(settimeout)
        return delivered and restored

    def _restore_socket_timeout(self, settimeout: Callable[[float], object] | None) -> bool:
        """
        Put a client socket's own timeout back after a bounded write.

        Args:
            settimeout: The socket's `settimeout`, or None when it has none
                (a stub in the tests), in which case there is nothing to undo.

        Returns:
            bool: True when the socket is back on its own timeout.

        """
        if settimeout is None:
            return True
        try:
            settimeout(self._CLIENT_SOCKET_TIMEOUT_SECONDS)
        except Exception:
            logger.warning(
                "Could not restore a client socket's own timeout - closing it rather than leaving it spinning"
            )
            return False
        return True

    def _abandon_unreachable_client(self, client: object) -> None:
        """
        Close a peer this thread could not answer, so it sees EOF rather than silence.

        A silent peer waits out its full 180 s timeout; a closed one fails at
        once, and its `handle_client` thread cleans up as on any disconnect.

        Args:
            client: The socket to drop.

        """
        with self._clients_lock:
            self._clients.pop(client, None)
        with suppress(Exception):
            client.shutdown(socket.SHUT_RDWR)
        with suppress(Exception):
            client.close()

    @staticmethod
    def _encode_frame(response: dict) -> bytes:
        """
        Serialize one response as a newline-terminated frame.

        Args:
            response: The JSON-serializable response body.

        Returns:
            bytes: The encoded frame, newline terminator included.

        """
        return json.dumps(response).encode("utf-8") + b"\n"

    def _encode_response(self, response: dict, request_id: str | None) -> bytes:
        """
        Turn a handler's response into a frame that is always sendable.

        A response that cannot be serialized, such as one holding a
        `mathutils.Vector`, would otherwise send nothing and leave the client
        waiting out its timeout. The client gets a generic error and the
        traceback goes to Blender's log, since scene data, paths and
        tracebacks must not reach a client.

        Args:
            response: The response body to encode.
            request_id: The request's id, echoed back so a fallback frame stays
                matchable to the command that produced it.

        Returns:
            bytes: The encoded frame, or an error frame if `response` could not
            be serialized or exceeds `_MAX_MESSAGE_BYTES`.

        """
        try:
            payload = self._encode_frame(response)
        except (TypeError, ValueError):
            logger.exception("A response could not be serialized to JSON - sending an error frame instead")
            return self._error_frame(request_id, "Response could not be serialized to JSON")

        if len(payload) > self._MAX_MESSAGE_BYTES:
            return self._error_frame(request_id, "Response exceeded the configured message-size limit")
        return payload

    def _error_frame(self, request_id: str | None, message: str) -> bytes:
        """
        Build the one error frame shape every failure path returns.

        Args:
            request_id: The request's id, or None when it could not be read.
            message: Client-safe explanation; never a path or a traceback.

        Returns:
            bytes: The encoded error frame.

        """
        return self._encode_frame({"id": request_id, "status": "error", "message": message})

    def _send_frame(self, client, payload: bytes, lock_timeout: float | None = None) -> None:
        """
        Write one frame to a client, excluding any other writer to that socket.

        The client's own thread sends protocol errors while the main thread sends
        responses, and `sendall` is not atomic, so without the lock one frame
        could land inside another. `_clients_lock` is released before the
        per-client lock is taken, and no queue or `bpy` work happens under either.

        The rejection path passes `lock_timeout` because the other writer can
        hold the lock for as long as its own send blocks, which would stall the
        main thread past its budget. Ordinary responses wait for the lock.

        Args:
            client: The socket to write to.
            payload: The framed bytes to write.
            lock_timeout: Seconds to wait for the per-client write lock, or None
                to wait indefinitely. 0 attempts the acquisition without waiting.

        Raises:
            TimeoutError: When `lock_timeout` elapsed with the write lock still
                held by the other writer.

        """
        with self._clients_lock:
            send_lock = self._clients.get(client)
        if send_lock is None:
            # Untracked or already removed: no other writer to exclude.
            client.sendall(payload)
            return
        if lock_timeout is None:
            with send_lock:
                client.sendall(payload)
            return
        # `acquire` waits forever on -1 and rejects other negatives, so <= 0 means "don't wait".
        acquired = send_lock.acquire(False) if lock_timeout <= 0 else send_lock.acquire(timeout=lock_timeout)
        if not acquired:
            raise TimeoutError("another thread holds this client's write lock")
        try:
            client.sendall(payload)
        finally:
            send_lock.release()

    def _send_protocol_error(self, client, request_id, message) -> None:
        """
        Return a bounded transport-level validation error without touching bpy data.

        Args:
            client: The socket the offending frame arrived on.
            request_id: The request's id, or None when it could not be read.
            message: Client-safe explanation; never a path or a traceback.

        """
        with suppress(Exception):
            self._send_frame(client, self._error_frame(request_id, message))

    def _decode_and_queue_frame(self, line: bytes, client) -> None:
        r"""
        Validate one framed line and queue it as a command for the main thread.

        `parse_command_frame` makes the decision; this adds the socket write
        for a rejected frame and the queue push for an accepted one.

        Args:
            line: The bytes of one `\n`-delimited frame (never containing
                the terminator itself).
            client: The socket the frame arrived on, passed through to the
                queued command so its response goes back to the right peer.

        """
        command, request_id, error = parse_command_frame(line)
        if command is None:
            # The log gets the length the client-safe message cannot carry; the
            # payload itself stays out of it.
            logger.warning("Discarding a %d-byte frame: %s", len(line), error)
            self._send_protocol_error(client, request_id, error)
            return

        # Hand off to the main thread. Never call
        # bpy.app.timers.register() from here - it is not thread-safe and
        # the callback can be silently lost.
        #
        # Stamp at enqueue, the last point where the command's database is known.
        self._stamp_session(command)
        logger.debug("Queued command: %s", command.get("type"))
        try:
            self.command_queue.put_nowait((command, client))
        except queue.Full:
            self._send_protocol_error(client, request_id, "Blender command queue is full; retry later")

    def handle_client(self, client) -> None:
        """
        Handle connected client.

        Args:
            client: Value for client.

        """
        logger.debug("Client handler started")
        # A finite timeout keeps this loop responsive to self.running instead
        # of parking in recv() forever.
        client.settimeout(self._CLIENT_SOCKET_TIMEOUT_SECONDS)
        with self._clients_lock:
            # Shared by this thread and the main thread; see _send_frame.
            self._clients[client] = threading.Lock()

        try:
            self._serve_client(client)
        except Exception:
            logger.exception("Client handler failed - dropping the connection")
        finally:
            with self._clients_lock:
                self._clients.pop(client, None)
            with suppress(Exception):
                client.close()
            logger.debug("Client handler stopped")

    def _serve_client(self, client) -> None:
        """
        Read from one client until it disconnects, breaks the size cap, or the server stops.

        A timeout is the loop's chance to re-check `running`, not a failure; any
        other read failure drops the connection.

        Args:
            client: The connected socket.

        """
        buffer = b""
        while self.running:
            # Receive data
            try:
                buffer, keep_reading = self._receive_frames(client, buffer)
            except TimeoutError:
                # Expected; loop round and re-check self.running.
                continue
            except BlockingIOError:
                # A non-blocking socket with nothing to read, not a dead peer.
                # This server never leaves the socket non-blocking (see
                # `_PAST_BUDGET_SEND_TIMEOUT_SECONDS`); this branch keeps
                # it from being fatal if something else does.
                continue
            except Exception:
                logger.exception("Could not receive from a client - dropping the connection")
                break
            if not keep_reading:
                break

    def _receive_frames(self, client, buffer: bytes) -> tuple[bytes, bool]:
        """
        Read once from a client and queue every frame that read completed.

        Args:
            client: The connected socket.
            buffer: Bytes received earlier and not yet consumed.

        Returns:
            tuple[bytes, bool]: The bytes left for the next read, and whether to
            keep reading: False once the peer has gone or broken the size cap.

        """
        data = client.recv(8192)
        if not data:
            logger.info("Client disconnected")
            return buffer, False

        frames, buffer, must_drop = extract_frames(buffer + data, self._MAX_MESSAGE_BYTES)
        for frame in frames:
            self._decode_and_queue_frame(frame, client)
        if must_drop:
            # Either a single frame or an unterminated remainder
            # went past the cap; both are protocol violations. The
            # frames above arrived intact and were queued, but the
            # drain discards them once this socket is unregistered.
            logger.warning(
                "Client sent a frame, or an unterminated message, over the %d-byte limit - disconnecting",
                self._MAX_MESSAGE_BYTES,
            )
            return buffer, False
        return buffer, True
