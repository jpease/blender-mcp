r"""
Regression coverage for message boundaries in the addon's socket buffer.

`handle_client` frames messages with a `b"\n"` terminator: each `recv()`
chunk is appended to `buffer`, then every complete `\n`-delimited line is
decoded and parsed as one JSON message, with any leftover bytes kept for the
next chunk. Two things this needs to get right:

- A multi-byte UTF-8 character (e.g. an accented letter, CJK text, or an
  emoji in an object name or in LLM-generated code) can still land split
  across a `recv()` chunk boundary - but never across the `\n` terminator
  itself, since `\n` (0x0A) can't occur inside a multi-byte UTF-8 sequence.
  So decoding only happens once a full line has been assembled.
- Two full messages arriving in a *single* `recv()` (e.g. the OS coalesces
  two `sendall()` calls, or a client doesn't wait for a response before
  sending the next command) must both be parsed and queued - not just the
  first one, and not left permanently stuck. Before framing was added, a
  single `json.loads()` over the whole buffer raised `json.JSONDecodeError:
  Extra data` for this case, which was indistinguishable from "incomplete
  data" and so the buffer was never cleared - the connection could never
  parse another message again.

A real loopback socket won't reliably reproduce an exact byte-offset split
(the OS may coalesce separate `sendall()` calls into one `recv()`), so this
drives `handle_client` directly with a fake socket that returns pre-scripted
chunks - deterministic, no network, no flakiness.
"""

from __future__ import annotations

import json

import pytest

from server.test_threading import BlenderMCPServer


class ScriptedSocket:
    """Fake client socket returning pre-scripted recv() chunks, one per call."""

    def __init__(self, chunks) -> None:
        self._chunks = list(chunks)
        self.sent = []

    def settimeout(self, timeout) -> None:
        pass

    def recv(self, bufsize):
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    def sendall(self, data) -> None:
        self.sent.append(data)

    def close(self) -> None:
        pass


def _make_server():
    server = BlenderMCPServer(port=0)
    server.execute_command = lambda command: {"status": "success", "result": {}}
    return server


def _split_after_lead_byte(payload: bytes) -> int:
    """
    Index right after a multi-byte UTF-8 lead byte's first byte.

    Splitting there guarantees the first chunk ends mid-character, so
    decoding it alone as UTF-8 raises UnicodeDecodeError.

    Returns:
        The index immediately after the lead byte.

    Raises:
        AssertionError: If the payload contains no multi-byte UTF-8 character.

    """
    for i, b in enumerate(payload):
        if b >= 0xC0:  # lead byte of a 2/3/4-byte sequence
            return i + 1
    raise AssertionError("payload has no multi-byte UTF-8 character to split")


def test_split_multibyte_utf8_boundary_is_not_dropped() -> None:
    body = json.dumps({"type": "ping", "params": {"note": "café ☕ 日本語"}}, ensure_ascii=False).encode("utf-8")
    split_idx = _split_after_lead_byte(body)
    chunk1, chunk2 = body[:split_idx], body[split_idx:] + b"\n"

    # Sanity check: confirm the split really does land mid-character, i.e.
    # this fixture actually exercises the bug and isn't accidentally valid.
    with pytest.raises(UnicodeDecodeError):
        chunk1.decode("utf-8")

    server = _make_server()
    server.running = True
    server.handle_client(ScriptedSocket([chunk1, chunk2]))

    assert not server.command_queue.empty(), (
        "command was dropped: a multi-byte UTF-8 character split across a "
        "recv() chunk boundary killed the connection instead of waiting for "
        "the rest of the buffer"
    )
    command, _client = server.command_queue.get_nowait()
    assert command["type"] == "ping"
    assert command["params"]["note"] == "café ☕ 日本語"


def test_split_multibyte_utf8_boundary_keeps_handler_loop_alive() -> None:
    """
    A second command sent right after the split payload must still arrive.

    If the split killed the loop, this second command would never be queued.
    """
    first_body = json.dumps({"type": "ping", "params": {"note": "emoji test 🎨"}}, ensure_ascii=False).encode("utf-8")
    split_idx = _split_after_lead_byte(first_body)
    second = json.dumps({"type": "ping", "params": {}}).encode("utf-8") + b"\n"

    server = _make_server()
    server.running = True
    server.handle_client(ScriptedSocket([first_body[:split_idx], first_body[split_idx:] + b"\n", second]))

    queued = []
    while not server.command_queue.empty():
        command, _client = server.command_queue.get_nowait()
        queued.append(command)

    assert len(queued) == 2, f"expected both commands queued, got {queued}"


def test_two_messages_concatenated_in_one_recv_are_both_queued() -> None:
    r"""
    Two full, newline-terminated messages landing in a single recv() chunk.

    Before framing was added, `handle_client` tried `json.loads()` on the
    whole accumulated buffer. A buffer containing "one complete JSON object
    followed by another complete JSON object" raises `json.JSONDecodeError:
    Extra data` - indistinguishable there from "incomplete, wait for more" -
    so the buffer was kept and could never parse again: this is the
    "permanently unparsable concatenated JSON" bug. With `\n`-framing, each
    line is parsed independently, so both messages in one chunk must be
    queued.
    """
    first = json.dumps({"type": "ping", "params": {"n": 1}}).encode("utf-8") + b"\n"
    second = json.dumps({"type": "ping", "params": {"n": 2}}).encode("utf-8") + b"\n"

    server = _make_server()
    server.running = True
    server.handle_client(ScriptedSocket([first + second]))

    queued = []
    while not server.command_queue.empty():
        command, _client = server.command_queue.get_nowait()
        queued.append(command["params"]["n"])

    assert queued == [1, 2], f"expected both concatenated commands queued in order, got {queued}"


def test_oversized_message_without_terminator_disconnects_instead_of_growing_forever() -> None:
    r"""
    Malformed/never-terminated input must not make the buffer grow forever.

    A client (malicious or buggy) that sends bytes without ever completing a
    `\n`-terminated message used to accumulate in `buffer` with no bound.
    Once the buffer exceeds `_MAX_MESSAGE_BYTES`, the connection is dropped
    instead.
    """
    server = _make_server()
    server.running = True
    server._MAX_MESSAGE_BYTES = 100  # keep the test fast
    garbage_chunk = b"x" * 200

    server.handle_client(ScriptedSocket([garbage_chunk]))

    assert server.command_queue.empty(), "garbage input must never be queued as a command"


def test_oversized_terminated_frame_is_rejected() -> None:
    r"""
    A single complete (`\n`-terminated) frame must be size-checked too.

    The unterminated-buffer check above only bounds "how long can we wait
    without ever seeing a terminator" - it does not stop a frame that
    *does* get a `\n` (e.g. because the terminating chunk lands in the same
    recv() call that pushes the buffer past the limit) from being decoded
    and queued at any size. Each line must be checked as soon as it is
    split off, before it is ever handed to json.loads().
    """
    server = _make_server()
    server.running = True
    server._MAX_MESSAGE_BYTES = 100  # keep the test fast
    oversized_frame = b"x" * 200 + b"\n"

    server.handle_client(ScriptedSocket([oversized_frame]))

    assert server.command_queue.empty(), "oversized terminated frame must never be queued as a command"


@pytest.mark.parametrize(
    "payload,message",
    [
        ([], "JSON object"),
        ({"type": "", "params": {}}, "non-empty string"),
        ({"id": 7, "type": "ping", "params": {}}, "id must be a string"),
        ({"type": "ping", "params": []}, "params must be a JSON object"),
    ],
)
def test_invalid_command_shapes_are_rejected_with_structured_errors(payload, message) -> None:
    client = ScriptedSocket([json.dumps(payload).encode("utf-8") + b"\n"])
    server = _make_server()
    server.running = True

    server.handle_client(client)

    assert server.command_queue.empty()
    response = json.loads(client.sent[0])
    assert response["status"] == "error"
    assert message in response["message"]


def test_full_command_queue_returns_retryable_error() -> None:
    server = _make_server()
    server._MAX_QUEUED_COMMANDS = 1
    server.command_queue = __import__("queue").Queue(maxsize=1)
    first_client = ScriptedSocket([])
    second_client = ScriptedSocket([])
    assert server._decode_and_queue_frame(b'{"type":"ping"}', first_client)

    assert server._decode_and_queue_frame(b'{"id":"two","type":"ping"}', second_client)

    assert server.command_queue.qsize() == 1
    response = json.loads(second_client.sent[0])
    assert response == {"id": "two", "status": "error", "message": "Blender command queue is full; retry later"}
