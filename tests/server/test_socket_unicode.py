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

The splitting itself is `extract_frames`, and the shape check is
`parse_command_frame`; both are pure, so the boundary cases a socket can only
reach by luck - a frame exactly at the size cap, one byte over, a remainder
that will never be terminated - are driven directly.
"""

from __future__ import annotations

import json

import pytest

from conftest import load_addon_for_module

_addon, _bpy = load_addon_for_module()
BlenderMCPServer = _addon.BlenderMCPServer
extract_frames = _addon.socket_transport.extract_frames
parse_command_frame = _addon.socket_transport.parse_command_frame


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
    server._decode_and_queue_frame(b'{"type":"ping"}', first_client)

    server._decode_and_queue_frame(b'{"id":"two","type":"ping"}', second_client)

    assert server.command_queue.qsize() == 1
    response = json.loads(second_client.sent[0])
    assert response == {"id": "two", "status": "error", "message": "Blender command queue is full; retry later"}


_CAP = 64


def test_a_character_split_across_chunks_waits_in_the_remainder() -> None:
    """
    A chunk ending mid-character yields no frame and loses no bytes.

    The socket-level test above proves the connection survives it; this pins
    the rule that makes it survive - nothing is decoded until the terminator
    arrives, and the partial character is handed back verbatim.
    """
    body = json.dumps({"type": "ping", "params": {"note": "café"}}, ensure_ascii=False).encode("utf-8")
    split = _split_after_lead_byte(body)

    frames, remainder, must_drop = extract_frames(body[:split], _CAP)

    assert (frames, remainder, must_drop) == ([], body[:split], False)
    assert extract_frames(remainder + body[split:] + b"\n", _CAP) == ([body], b"", False)


def test_every_frame_in_one_chunk_comes_back_in_order_without_the_empty_ones() -> None:
    """Two pipelined frames, a stray terminator between them, and a partial third."""
    frames, remainder, must_drop = extract_frames(b'{"n":1}\n\n{"n":2}\n{"n":3', _CAP)

    assert frames == [b'{"n":1}', b'{"n":2}']
    assert remainder == b'{"n":3'
    assert must_drop is False


def test_the_frame_size_cap_is_inclusive() -> None:
    """A frame of exactly the cap is delivered; one byte more ends the connection."""
    assert extract_frames(b"x" * _CAP + b"\n", _CAP) == ([b"x" * _CAP], b"", False)

    frames, _remainder, must_drop = extract_frames(b"x" * (_CAP + 1) + b"\n", _CAP)

    assert frames == []
    assert must_drop is True


def test_frames_that_arrived_before_an_oversized_one_are_still_returned() -> None:
    """
    The connection dies, but the frames ahead of the bad one were intact.

    They are already queued when the loop breaks, so dropping them here would
    silently lose commands the client is waiting on answers for.
    """
    frames, _remainder, must_drop = extract_frames(b'{"n":1}\n' + b"x" * (_CAP + 1) + b'\n{"n":2}\n', _CAP)

    assert frames == [b'{"n":1}']
    assert must_drop is True


def test_an_unterminated_remainder_is_capped_too() -> None:
    """
    A client that never terminates a message must not grow the buffer forever.

    The remainder is bounded by the same cap as a frame, and inclusively: at
    the cap it is still waiting for a terminator that may yet arrive.
    """
    assert extract_frames(b"x" * _CAP, _CAP) == ([], b"x" * _CAP, False)

    _frames, _remainder, must_drop = extract_frames(b"x" * (_CAP + 1), _CAP)

    assert must_drop is True


def test_a_well_shaped_frame_parses_to_its_command_and_id() -> None:
    command, request_id, error = parse_command_frame(b'{"id":"a1","type":"ping","params":{"n":1}}')

    assert command == {"id": "a1", "type": "ping", "params": {"n": 1}}
    assert (request_id, error) == ("a1", None)


@pytest.mark.parametrize(
    ("frame", "expected_id", "message"),
    [
        (b'{"id":"a1","type":""}', "a1", "non-empty string"),
        (b'{"id":"a1","type":"ping","params":[]}', "a1", "params must be a JSON object"),
        (b'{"id":7,"type":"ping"}', None, "id must be a string"),
        (b"[]", None, "JSON object"),
        (b"{not json", None, "Malformed UTF-8 JSON"),
        (b'{"type":"\xff"}', None, "Malformed UTF-8 JSON"),
    ],
)
def test_a_rejected_frame_is_answered_under_the_id_it_could_be_read_from(
    frame: bytes, expected_id: str | None, message: str
) -> None:
    """
    Every rejection carries the request id when the frame yielded a usable one.

    Without it the client cannot match the error to the command it sent, and
    waits out its own timeout instead. An id that is not a string cannot be
    echoed as one, so those rejections carry no id at all.

    Args:
        frame: The bytes of one rejected frame.
        expected_id: The id the error must be answered under, if any.
        message: A fragment of the client-safe explanation.

    """
    command, request_id, error = parse_command_frame(frame)

    assert command is None
    assert request_id == expected_id
    assert error is not None and message in error
