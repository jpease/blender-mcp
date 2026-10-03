"""Tests for bounded provider HTTP helpers."""

import importlib.util
import sys
import types

from pathlib import Path

import pytest

NETWORK_PATH = Path(__file__).resolve().parents[1] / "src/blender_mcp/bundled/addon/network.py"
requests = types.ModuleType("requests")
requests.get = None
previous_requests = sys.modules.get("requests")
sys.modules["requests"] = requests
SPEC = importlib.util.spec_from_file_location("blender_mcp_provider_network_test", NETWORK_PATH)
assert SPEC is not None and SPEC.loader is not None
network = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(network)
if previous_requests is None:
    del sys.modules["requests"]
else:
    sys.modules["requests"] = previous_requests


class Response:
    """Small requests.Response stand-in for bounded-download tests."""

    def __init__(self, chunks, *, content_type="application/octet-stream", content_length=None) -> None:
        self._chunks = chunks
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size):
        assert chunk_size == network.CHUNK_BYTES
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


class _CancelledError(Exception):
    pass


class _Transfer:
    """Records what a fetch hears, and abandons it after `stop_after` chunks."""

    def __init__(self, stop_after: int | None = None) -> None:
        self.declared: list[int | None] = []
        self.chunks: list[int] = []
        self._stop_after = stop_after

    def begin(self, declared_bytes):
        self.declared.append(declared_bytes)

    def advance(self, received_bytes):
        self.chunks.append(received_bytes)
        if self._stop_after is not None and len(self.chunks) >= self._stop_after:
            raise _CancelledError


def test_streamed_download_enforces_actual_byte_limit(monkeypatch, tmp_path) -> None:
    response = Response([b"1234", b"5678"])
    monkeypatch.setattr(network.requests, "get", lambda *_args, **_kwargs: response)
    destination = tmp_path / "asset.bin"

    with pytest.raises(ValueError, match="exceeded"):
        network.download_file("https://example.invalid/asset", str(destination), max_bytes=7)
    assert response.closed


def test_json_fetch_enforces_declared_size_before_decode(monkeypatch) -> None:
    response = Response([b"{}"], content_length=network.MAX_JSON_BYTES + 1)
    monkeypatch.setattr(network.requests, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(ValueError, match="declares"):
        network.get_json("https://example.invalid/catalog")
    assert response.closed


def test_json_fetch_enforces_streamed_size_without_a_declaration(monkeypatch) -> None:
    monkeypatch.setattr(network, "MAX_JSON_BYTES", 3)
    monkeypatch.setattr(network.requests, "get", lambda *_args, **_kwargs: Response([b'{"a"', b": 1}"]))

    with pytest.raises(ValueError, match="exceeded"):
        network.get_json("https://example.invalid/catalog")


def test_a_transfer_hears_the_declared_size_and_every_chunk(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        network.requests, "get", lambda *_args, **_kwargs: Response([b"12", b"", b"345"], content_length=5)
    )
    transfer = _Transfer()

    written = network.download_file("https://example.invalid/a", str(tmp_path / "a"), max_bytes=5, transfer=transfer)

    assert (written, transfer.declared, transfer.chunks) == (5, [5], [2, 3])


def test_a_transfer_that_raises_abandons_the_download_and_closes_it(monkeypatch, tmp_path) -> None:
    response = Response([b"12", b"34", b"56"])
    monkeypatch.setattr(network.requests, "get", lambda *_args, **_kwargs: response)
    destination = tmp_path / "asset.bin"

    with pytest.raises(_CancelledError):
        network.download_file(
            "https://example.invalid/asset", str(destination), max_bytes=100, transfer=_Transfer(stop_after=1)
        )

    assert response.closed
    # The chunk that tripped the cancellation is never written.
    assert destination.read_bytes() == b""
