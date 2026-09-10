"""
Coverage for the two transports that carry an image back from Blender.

The temp-file transport only works when the MCP server and Blender share a
filesystem; the inline transport carries the bytes over the socket instead and
is the one used whenever the connected addon advertises support for it.
"""

import base64
import types

import pytest

from blender_mcp.server.tools import _image_transport


class _Connection:
    """Records commands, and optionally writes bytes to the requested filepath."""

    def __init__(self, result=None, writes=None):
        self.calls = []
        self._result = {} if result is None else result
        self._writes = writes

    def send_command(self, command, params):
        self.calls.append((command, params))
        if self._writes is not None and params.get("filepath"):
            with open(params["filepath"], "wb") as handle:
                handle.write(self._writes)
        return self._result


def _use(monkeypatch, connection, protocol_version):
    monkeypatch.setattr(_image_transport, "get_blender_connection", lambda: connection)
    monkeypatch.setattr(
        _image_transport,
        "get_last_handshake",
        lambda: None if protocol_version is None else types.SimpleNamespace(protocol_version=protocol_version),
    )


def test_inline_transport_returns_decoded_bytes_without_a_shared_path(monkeypatch) -> None:
    connection = _Connection({"image_base64": base64.b64encode(b"inline-png").decode("ascii"), "width": 640})
    _use(monkeypatch, connection, _image_transport.INLINE_IMAGE_PROTOCOL_VERSION)

    image_bytes, result = _image_transport.request_image(
        "get_viewport_screenshot", {"max_size": 800}, prefix="blender_mcp_test_"
    )

    assert image_bytes == b"inline-png"
    assert result["width"] == 640
    command, params = connection.calls[0]
    assert command == "get_viewport_screenshot"
    assert params["inline"] is True
    assert "filepath" not in params


def test_inline_transport_never_creates_a_local_temp_file(monkeypatch) -> None:
    connection = _Connection({"image_base64": base64.b64encode(b"inline-png").decode("ascii")})
    _use(monkeypatch, connection, _image_transport.INLINE_IMAGE_PROTOCOL_VERSION)

    def explode(**_kwargs):
        raise AssertionError("inline transport must not touch the local filesystem")

    monkeypatch.setattr(_image_transport.tempfile, "mkstemp", explode)

    image_bytes, _result = _image_transport.request_image("get_viewport_screenshot", {}, prefix="blender_mcp_test_")

    assert image_bytes == b"inline-png"


def test_inline_reply_without_image_data_is_an_error(monkeypatch) -> None:
    connection = _Connection({"width": 640})
    _use(monkeypatch, connection, _image_transport.INLINE_IMAGE_PROTOCOL_VERSION)

    with pytest.raises(Exception, match="no inline image data"):
        _image_transport.request_image("get_viewport_screenshot", {}, prefix="blender_mcp_test_")


def test_falls_back_to_the_temp_file_transport_for_an_older_addon(monkeypatch) -> None:
    connection = _Connection({"width": 640}, writes=b"file-png")
    _use(monkeypatch, connection, _image_transport.INLINE_IMAGE_PROTOCOL_VERSION - 1)

    image_bytes, result = _image_transport.request_image(
        "get_viewport_screenshot", {"max_size": 800}, prefix="blender_mcp_test_"
    )

    assert image_bytes == b"file-png"
    assert result["width"] == 640
    _command, params = connection.calls[0]
    assert params["filepath"].endswith(".png")
    assert "inline" not in params


def test_falls_back_to_the_temp_file_transport_when_no_handshake_happened(monkeypatch) -> None:
    connection = _Connection({}, writes=b"file-png")
    _use(monkeypatch, connection, None)

    image_bytes, _result = _image_transport.request_image("get_viewport_screenshot", {}, prefix="blender_mcp_test_")

    assert image_bytes == b"file-png"


def test_temp_file_transport_reports_an_error_reply(monkeypatch) -> None:
    connection = _Connection({"error": "No 3D viewport found"}, writes=b"file-png")
    _use(monkeypatch, connection, None)

    with pytest.raises(Exception, match="No 3D viewport found"):
        _image_transport.request_image("get_viewport_screenshot", {}, prefix="blender_mcp_test_")


def test_temp_file_is_removed_when_blender_fails(monkeypatch, tmp_path) -> None:
    created = tmp_path / "request.png"

    class _Failing:
        def send_command(self, *_args, **_kwargs):
            raise RuntimeError("capture failed")

    def fake_mkstemp(**_kwargs):
        import os

        return os.open(created, os.O_CREAT | os.O_RDWR), str(created)

    _use(monkeypatch, _Failing(), None)
    monkeypatch.setattr(_image_transport.tempfile, "mkstemp", fake_mkstemp)

    with pytest.raises(RuntimeError, match="capture failed"):
        _image_transport.request_image("get_viewport_screenshot", {}, prefix="blender_mcp_test_")

    assert not created.exists()


def test_temp_file_transport_reports_a_missing_written_file(monkeypatch) -> None:
    connection = _Connection({})  # writes nothing
    _use(monkeypatch, connection, None)

    with pytest.raises(Exception, match="did not write"):
        _image_transport.request_image("get_viewport_screenshot", {}, prefix="blender_mcp_test_")
