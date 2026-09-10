"""
Coverage for the two transports that carry an image back from Blender.

The temp-file transport only works when the MCP server and Blender share a
filesystem; the inline transport carries the bytes over the socket instead and
is the one used whenever the connected addon advertises support for it.
"""

import base64
import os
import types

import pytest

from blender_mcp.server.tools import _image_transport


class _Recorder:
    """A `send`, recording its commands and optionally writing to the requested filepath."""

    def __init__(self, result=None, writes=None):
        self.calls = []
        self._result = {} if result is None else result
        self._writes = writes

    def __call__(self, command, params):
        self.calls.append((command, params))
        if self._writes is not None and params.get("filepath"):
            with open(params["filepath"], "wb") as handle:
                handle.write(self._writes)
        return self._result


def _handshake(monkeypatch, protocol_version):
    monkeypatch.setattr(
        _image_transport,
        "get_last_handshake",
        lambda: None if protocol_version is None else types.SimpleNamespace(protocol_version=protocol_version),
    )


def _request(send, command="get_viewport_screenshot", params=None):
    return _image_transport.request_image(
        send,
        command,
        {} if params is None else params,
        prefix="blender_mcp_test_",
        missing_file="Screenshot file was not created",
    )


def test_inline_transport_returns_decoded_bytes_without_a_shared_path(monkeypatch) -> None:
    send = _Recorder({"image_base64": base64.b64encode(b"inline-png").decode("ascii"), "width": 640})
    _handshake(monkeypatch, _image_transport.INLINE_IMAGE_PROTOCOL_VERSION)

    image_bytes, result = _request(send, params={"max_size": 800})

    assert image_bytes == b"inline-png"
    assert result["width"] == 640
    command, params = send.calls[0]
    assert command == "get_viewport_screenshot"
    assert params["inline"] is True
    assert "filepath" not in params


def test_inline_transport_never_creates_a_local_temp_file(monkeypatch) -> None:
    send = _Recorder({"image_base64": base64.b64encode(b"inline-png").decode("ascii")})
    _handshake(monkeypatch, _image_transport.INLINE_IMAGE_PROTOCOL_VERSION)

    def explode(**_kwargs):
        raise AssertionError("inline transport must not touch the local filesystem")

    monkeypatch.setattr(_image_transport.tempfile, "mkstemp", explode)

    image_bytes, _result = _request(send)

    assert image_bytes == b"inline-png"


def test_inline_reply_without_image_data_is_an_error(monkeypatch) -> None:
    send = _Recorder({"width": 640})
    _handshake(monkeypatch, _image_transport.INLINE_IMAGE_PROTOCOL_VERSION)

    with pytest.raises(Exception, match="no inline image data"):
        _request(send)


def test_falls_back_to_the_temp_file_transport_for_an_older_addon(monkeypatch) -> None:
    send = _Recorder({"width": 640}, writes=b"file-png")
    _handshake(monkeypatch, _image_transport.INLINE_IMAGE_PROTOCOL_VERSION - 1)

    image_bytes, result = _request(send, params={"max_size": 800})

    assert image_bytes == b"file-png"
    assert result["width"] == 640
    _command, params = send.calls[0]
    assert params["filepath"].endswith(".png")
    assert "inline" not in params


def test_falls_back_to_the_temp_file_transport_when_no_handshake_happened(monkeypatch) -> None:
    send = _Recorder({}, writes=b"file-png")
    _handshake(monkeypatch, None)

    image_bytes, _result = _request(send)

    assert image_bytes == b"file-png"


def test_temp_file_transport_reports_an_error_reply(monkeypatch) -> None:
    send = _Recorder({"error": "No 3D viewport found"}, writes=b"file-png")
    _handshake(monkeypatch, None)

    with pytest.raises(Exception, match="No 3D viewport found"):
        _request(send)


def test_temp_file_is_removed_when_blender_fails(monkeypatch, tmp_path) -> None:
    created = tmp_path / "request.png"

    def failing(*_args, **_kwargs):
        raise RuntimeError("capture failed")

    def fake_mkstemp(**_kwargs):
        return os.open(created, os.O_CREAT | os.O_RDWR), str(created)

    _handshake(monkeypatch, None)
    monkeypatch.setattr(_image_transport.tempfile, "mkstemp", fake_mkstemp)

    with pytest.raises(RuntimeError, match="capture failed"):
        _request(failing)

    assert not created.exists()


def test_temp_file_transport_reports_a_reply_that_wrote_nothing(monkeypatch) -> None:
    # mkstemp already created the file, so an empty one - not a missing one - is what a
    # Blender that cannot see the server's filesystem leaves behind.
    send = _Recorder({})  # writes nothing
    _handshake(monkeypatch, None)

    with pytest.raises(Exception, match=r"Screenshot file was not created.*did not write"):
        _request(send)
