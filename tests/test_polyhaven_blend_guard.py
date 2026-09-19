"""
The Poly Haven `.blend` model import validates the download before Blender reads it.

The file must be a real `.blend` inside the handler's own download directory; the
deployment's file roots do not apply.
"""

import contextlib
import os
import sys
import types

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from test_mutation_transaction import _load_addon

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "blend"
_URL = "https://dl.polyhaven.org/file/ph-assets/Models/blend/1k/chair/chair_1k.blend"


class _Loads:
    """Record `bpy.data.libraries.load` calls and append what they would import."""

    def __init__(self, objects: list, *, error: Exception | None = None) -> None:
        """
        Remember where imported objects go.

        Args:
            objects: The stub `bpy.data.objects` list.
            error: Raised from the load instead of importing, when given.

        """
        self.calls: list[str] = []
        self._objects = objects
        self._error = error

    @contextlib.contextmanager
    def load(self, filepath: str, link: bool = False) -> Iterator[tuple[types.SimpleNamespace, types.SimpleNamespace]]:
        """
        Stand in for `bpy.data.libraries.load`.

        Args:
            filepath: The file Blender would read.
            link: Whether to link rather than append.

        Yields:
            tuple: `(data_from, data_to)` in the real API's shape.

        """
        del link
        self.calls.append(filepath)
        if self._error is not None:
            raise self._error
        data_to = types.SimpleNamespace(objects=[])
        try:
            yield types.SimpleNamespace(objects=["Chair"]), data_to
        finally:
            # Blender reads the file when the block exits, so the import lands here.
            imported = types.SimpleNamespace(name="Chair", session_uid=1)
            data_to.objects = [imported]
            self._objects.append(imported)


def _server(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes | None,
    *,
    link_to: Path | None = None,
    error: Exception | None = None,
) -> tuple[Any, _Loads]:
    """
    Build an addon server whose Poly Haven download writes a chosen file.

    Args:
        monkeypatch: Fixture used to install the stubs.
        payload: Bytes the "download" writes, when not linking.
        link_to: Make the downloaded name a symlink to this file instead.
        error: Exception the library load raises, if any.

    Returns:
        tuple: The server and the load recorder.

    """
    objects: list = []
    loads = _Loads(objects, error=error)
    addon, bpy = _load_addon(monkeypatch, data={"filepath": "", "objects": objects})
    bpy.data.libraries = types.SimpleNamespace(load=loads.load)
    bpy.context.collection = types.SimpleNamespace(objects=types.SimpleNamespace(link=lambda _obj: None))
    # Blender's factory value. The scripts check refuses when the preference is
    # unreadable, so the stub must provide it.
    bpy.context.preferences = types.SimpleNamespace(filepaths=types.SimpleNamespace(use_scripts_auto_execute=False))
    handler = sys.modules[f"{addon.__name__}.handlers.polyhaven"]
    files = {"blend": {"1k": {"blend": {"url": _URL}}}}
    monkeypatch.setattr(handler, "get_json", lambda *_a, **_k: files)

    def download(_url: str, destination: str, **_kwargs: object) -> None:
        if link_to is not None:
            os.symlink(link_to, destination)
        else:
            Path(destination).write_bytes(payload or b"")

    monkeypatch.setattr(handler, "download_file", download)
    server_core = sys.modules[f"{addon.__name__}.server_core"]
    return server_core.BlenderMCPServer(), loads


def test_a_valid_downloaded_blend_is_still_imported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A real `.blend` still imports, even when the file roots are enforced elsewhere."""
    elsewhere = tmp_path / "configured_root"
    elsewhere.mkdir()
    monkeypatch.setenv("BLENDERMCP_FILE_ROOTS", str(elsewhere))
    server, loads = _server(monkeypatch, (FIXTURES / "empty_zstd.blend").read_bytes())

    result = server.import_polyhaven_asset("chair", "models", file_format="blend")

    assert result.get("success") is True, result
    assert result["imported_objects"] == ["Chair"]
    assert len(loads.calls) == 1


def test_a_downloaded_blend_whose_header_is_not_a_blend_is_never_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A response that is an HTML error page or a hostile payload never reaches Blender's reader."""
    server, loads = _server(monkeypatch, b"<!doctype html><title>502</title>")

    result = server.import_polyhaven_asset("chair", "models", file_format="blend")

    assert loads.calls == []
    assert "not a .blend" in result["error"]


def test_a_downloaded_blend_resolving_outside_its_download_directory_is_never_loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A download that symlinks out of the handler's temp directory is refused."""
    outside = tmp_path / "outside.blend"
    outside.write_bytes((FIXTURES / "empty_zstd.blend").read_bytes())
    server, loads = _server(monkeypatch, None, link_to=outside)

    result = server.import_polyhaven_asset("chair", "models", file_format="blend")

    assert loads.calls == []
    assert "outside" in result["error"]


def test_a_failed_blend_load_reports_no_absolute_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blender's own error text names the temp file; the handler routes it through the sanitizer."""
    leak = RuntimeError('Error: Cannot read file "/private/var/folders/xy/T/tmpabc/chair_1k.blend": Missing DNA')
    server, loads = _server(monkeypatch, (FIXTURES / "empty_zstd.blend").read_bytes(), error=leak)

    result = server.import_polyhaven_asset("chair", "models", file_format="blend")

    assert len(loads.calls) == 1
    assert "/private" not in result["error"] and "tmpabc" not in result["error"], result
    assert "Missing DNA" in result["error"]


@pytest.mark.parametrize("preferences", ["on", "unreadable"])
def test_a_downloaded_blend_is_never_loaded_while_scripts_auto_execute_is_on(
    monkeypatch: pytest.MonkeyPatch, preferences: str
) -> None:
    """An on or unreadable preference refuses the import in Poly Haven's error shape, naming no path."""
    server, loads = _server(monkeypatch, (FIXTURES / "empty_zstd.blend").read_bytes())
    filepaths = types.SimpleNamespace(use_scripts_auto_execute=True) if preferences == "on" else types.SimpleNamespace()
    sys.modules["bpy"].context.preferences = types.SimpleNamespace(filepaths=filepaths)

    result = server.import_polyhaven_asset("chair", "models", file_format="blend")

    assert loads.calls == []
    assert "import_polyhaven_asset refuses" in result["error"] and "use_scripts_auto_execute" in result["error"]
    assert "/private" not in result["error"] and "/var/" not in result["error"] and "chair_1k" not in result["error"]


def test_a_downloaded_blend_is_loaded_while_scripts_auto_execute_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The factory default does not block the import."""
    server, loads = _server(monkeypatch, (FIXTURES / "empty_zstd.blend").read_bytes())
    sys.modules["bpy"].context.preferences.filepaths.use_scripts_auto_execute = False

    result = server.import_polyhaven_asset("chair", "models", file_format="blend")

    assert result.get("success") is True, result
    assert len(loads.calls) == 1


# ---------------------------------------------------------------------------
# Every Poly Haven error that interpolates an exception goes through the sanitizer
# ---------------------------------------------------------------------------


def _leak(tmp_path: Path) -> RuntimeError:
    """
    Build an error in `bpy.data.images.load`'s shape, naming a real absolute path.

    Args:
        tmp_path: Directory whose path must not reach the client.

    Returns:
        RuntimeError: `Error: Cannot read '<abs>': No such file or directory`.

    """
    return RuntimeError(f"Error: Cannot read '{tmp_path}/cache/asset_1k.hdr': No such file or directory")


def _assert_sanitized(result: dict, tmp_path: Path) -> None:
    """
    Assert an error result keeps its cause and names no path.

    Args:
        result: The handler's return value.
        tmp_path: The directory the leak named.

    """
    assert str(tmp_path) not in result["error"] and "asset_1k" not in result["error"], result
    assert "No such file or directory" in result["error"], result


def _raise(error: Exception) -> object:
    """
    Make a stub that raises `error` whatever it is called with.

    Args:
        error: The exception to raise.

    Returns:
        object: The raising callable.

    """

    def raiser(*_args: object, **_kwargs: object) -> object:
        raise error

    return raiser


def test_a_failed_hdri_setup_reports_no_absolute_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The HDRI branch sanitizes an `images.load`-shaped error, which names the cached file."""
    server, _loads = _server(monkeypatch, b"hdr")
    handler = sys.modules[type(server).__module__.rsplit(".", 1)[0] + ".handlers.polyhaven"]
    bpy = sys.modules["bpy"]
    files = {"hdri": {"1k": {"hdr": {"url": "https://dl.polyhaven.org/x/asset_1k.hdr"}}}}
    monkeypatch.setattr(handler, "get_json", lambda *_a, **_k: files)
    bpy.utils = types.SimpleNamespace(user_resource=lambda *_a, **_k: str(tmp_path))
    bpy.context.scene = types.SimpleNamespace(name="Scene", world=object())
    monkeypatch.setattr(type(server), "configure_hdri_environment", _raise(_leak(tmp_path)), raising=False)

    _assert_sanitized(server.import_polyhaven_asset("asset", "hdris"), tmp_path)


def test_a_failed_texture_load_reports_no_absolute_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The textures branch sanitizes an `images.load` error, which names the temp file."""
    server, _loads = _server(monkeypatch, b"jpg")
    handler = sys.modules[type(server).__module__.rsplit(".", 1)[0] + ".handlers.polyhaven"]
    files = {"diffuse": {"1k": {"jpg": {"url": "https://dl.polyhaven.org/x/asset_1k.jpg"}}}}
    monkeypatch.setattr(handler, "get_json", lambda *_a, **_k: files)
    sys.modules["bpy"].data.images = types.SimpleNamespace(load=_raise(_leak(tmp_path)))

    _assert_sanitized(server.import_polyhaven_asset("asset", "textures"), tmp_path)


@pytest.mark.parametrize(
    ("command", "arguments"),
    [
        ("import_polyhaven_asset", ("asset", "models")),
        ("get_polyhaven_categories", ("hdris",)),
        ("list_polyhaven_assets", ()),
    ],
)
def test_a_failure_before_any_download_reports_no_absolute_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str, arguments: tuple
) -> None:
    """The outer handlers sanitize any error, including an OS error naming a path."""
    server, _loads = _server(monkeypatch, None)
    handler = sys.modules[type(server).__module__.rsplit(".", 1)[0] + ".handlers.polyhaven"]
    monkeypatch.setattr(handler, "get_json", _raise(_leak(tmp_path)))

    _assert_sanitized(getattr(server, command)(*arguments), tmp_path)
