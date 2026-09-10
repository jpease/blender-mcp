"""
Coverage for the writable-output-root discovery reported by get_addon_info.

Where Blender can actually write is not knowable from the MCP server's side
once the two are on different filesystems, so the addon reports it. Kept free
of `bpy` and loaded straight from source, like image_reply.
"""

import importlib.util
import os

import pytest

from conftest import ROOT_ADDON


def _load_output_roots():
    path = ROOT_ADDON.parent / "output_roots.py"
    spec = importlib.util.spec_from_file_location("addon_output_roots_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_configured_roots_splits_the_environment_variable() -> None:
    output_roots = _load_output_roots()
    environ = {output_roots.OUTPUT_ROOTS_ENV_VAR: os.pathsep.join(["/output", "/renders"])}

    assert output_roots.configured_roots(environ) == ["/output", "/renders"]


def test_configured_roots_is_empty_when_unset() -> None:
    output_roots = _load_output_roots()

    assert output_roots.configured_roots({}) == []


def test_configured_roots_ignores_blank_entries() -> None:
    output_roots = _load_output_roots()
    environ = {output_roots.OUTPUT_ROOTS_ENV_VAR: os.pathsep.join(["/output", "", "  "])}

    assert output_roots.configured_roots(environ) == ["/output"]


def test_writable_roots_keeps_existing_writable_directories(tmp_path) -> None:
    output_roots = _load_output_roots()

    assert output_roots.writable_roots([str(tmp_path)]) == [str(tmp_path)]


def test_writable_roots_drops_paths_that_do_not_exist(tmp_path) -> None:
    output_roots = _load_output_roots()
    missing = tmp_path / "not-created"

    assert output_roots.writable_roots([str(missing)]) == []


def test_writable_roots_drops_files(tmp_path) -> None:
    output_roots = _load_output_roots()
    a_file = tmp_path / "render.png"
    a_file.write_bytes(b"png")

    assert output_roots.writable_roots([str(a_file)]) == []


def test_writable_roots_drops_read_only_directories(tmp_path) -> None:
    output_roots = _load_output_roots()
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    if os.access(locked, os.W_OK):
        pytest.skip("filesystem or user ignores the write bit (running as root?)")

    try:
        assert output_roots.writable_roots([str(locked)]) == []
    finally:
        locked.chmod(0o700)


def test_writable_roots_dedupes_while_preserving_order(tmp_path) -> None:
    output_roots = _load_output_roots()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    roots = output_roots.writable_roots([str(second), str(first), str(second)])

    assert roots == [str(second), str(first)]


def test_writable_roots_reports_absolute_paths(tmp_path, monkeypatch) -> None:
    output_roots = _load_output_roots()
    nested = tmp_path / "nested"
    nested.mkdir()
    monkeypatch.chdir(tmp_path)

    assert output_roots.writable_roots(["nested"]) == [str(nested)]


def test_writable_roots_ignores_empty_candidates(tmp_path) -> None:
    output_roots = _load_output_roots()

    assert output_roots.writable_roots([None, "", str(tmp_path)]) == [str(tmp_path)]


# ---------------------------------------------------------------------------
# Reporting through the handshake
# ---------------------------------------------------------------------------


def test_get_addon_info_reports_writable_output_roots(monkeypatch, tmp_path) -> None:
    import sys

    from test_mutation_transaction import _load_addon

    mounted = tmp_path / "output"
    mounted.mkdir()

    output_roots = _load_output_roots()
    monkeypatch.setenv(output_roots.OUTPUT_ROOTS_ENV_VAR, str(mounted))

    addon, _bpy = _load_addon(monkeypatch, data={"filepath": ""})
    server_core = sys.modules[f"{addon.__name__}.server_core"]
    server = server_core.BlenderMCPServer()

    info = server.get_addon_info()

    assert str(mounted) in info["writable_output_roots"]
    # A deployment-configured root is offered before the process defaults.
    assert info["writable_output_roots"][0] == str(mounted)


def test_get_addon_info_reports_roots_without_any_configuration(monkeypatch) -> None:
    import sys

    from test_mutation_transaction import _load_addon

    output_roots = _load_output_roots()
    monkeypatch.delenv(output_roots.OUTPUT_ROOTS_ENV_VAR, raising=False)

    addon, _bpy = _load_addon(monkeypatch, data={"filepath": ""})
    server_core = sys.modules[f"{addon.__name__}.server_core"]
    server = server_core.BlenderMCPServer()

    # The temp directory is always writable, so the list is never empty.
    assert server.get_addon_info()["writable_output_roots"]


def test_addon_protocol_version_covers_the_inline_image_transport() -> None:
    from blender_mcp.addon_manager import EXPECTED_ADDON_PROTOCOL_VERSION
    from blender_mcp.server.tools._image_transport import INLINE_IMAGE_PROTOCOL_VERSION

    assert EXPECTED_ADDON_PROTOCOL_VERSION >= INLINE_IMAGE_PROTOCOL_VERSION
