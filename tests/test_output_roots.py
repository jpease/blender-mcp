"""
Coverage for the writable-output-root discovery reported by get_addon_info.

Where Blender can actually write is not knowable from the MCP server's side
once the two are on different filesystems, so the addon reports it. Kept free
of `bpy` and loaded straight from source, so these tests need no Blender.
"""

import os
import sys

from pathlib import Path
from types import ModuleType

import pytest

from conftest import load_addon_source_module
from test_mutation_transaction import _load_addon


def _load_output_roots() -> ModuleType:
    """
    Import the addon's output_roots module straight from source.

    Returns:
        ModuleType: The freshly loaded module.

    """
    return load_addon_source_module("output_roots.py", "addon_output_roots_under_test")


def test_configured_roots_splits_the_environment_variable() -> None:
    """Several roots reach the addon as one os.pathsep-joined string."""
    output_roots = _load_output_roots()
    environ = {output_roots.OUTPUT_ROOTS_ENV_VAR: os.pathsep.join(["/output", "/renders"])}

    assert output_roots.configured_roots(environ) == ["/output", "/renders"]


def test_configured_roots_is_empty_when_unset() -> None:
    """An unconfigured deployment falls back to the process's own defaults."""
    output_roots = _load_output_roots()

    assert output_roots.configured_roots({}) == []


def test_configured_roots_ignores_blank_entries() -> None:
    """A trailing separator or a stray space must not produce an empty root."""
    output_roots = _load_output_roots()
    environ = {output_roots.OUTPUT_ROOTS_ENV_VAR: os.pathsep.join(["/output", "", "  "])}

    assert output_roots.configured_roots(environ) == ["/output"]


def test_split_roots_trims_each_entry_and_drops_the_blanks() -> None:
    """Both readers share this: a stray space must not become a root that contains everything."""
    output_roots = _load_output_roots()

    assert output_roots.split_roots(os.pathsep.join([" /output ", "", "  ", "/renders"])) == ["/output", "/renders"]


def test_split_roots_reads_an_unset_variable_as_no_roots() -> None:
    """`Mapping.get` returns None for a variable nobody set, and that is not one blank root."""
    output_roots = _load_output_roots()

    assert output_roots.split_roots(None) == []


def test_normalized_candidates_expand_and_dedupe_without_probing_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pure half of `writable_roots`: none of these paths exists, and the rules still apply."""
    output_roots = _load_output_roots()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    candidates = output_roots.normalized_candidates([None, "", "nested", "~/renders", "nested", str(tmp_path)])

    assert candidates == [str(tmp_path / "nested"), str(tmp_path / "home" / "renders"), str(tmp_path)]


def test_writable_roots_keeps_existing_writable_directories(tmp_path: Path) -> None:
    """The happy path: a real, writable directory survives the filter."""
    output_roots = _load_output_roots()

    assert output_roots.writable_roots([str(tmp_path)]) == [str(tmp_path)]


def test_writable_roots_drops_paths_that_do_not_exist(tmp_path: Path) -> None:
    """Offering a root that is not there sends the agent's renders nowhere."""
    output_roots = _load_output_roots()
    missing = tmp_path / "not-created"

    assert output_roots.writable_roots([str(missing)]) == []


def test_writable_roots_drops_files(tmp_path: Path) -> None:
    """A root has to be a directory; a file would fail on the first write."""
    output_roots = _load_output_roots()
    a_file = tmp_path / "render.png"
    a_file.write_bytes(b"png")

    assert output_roots.writable_roots([str(a_file)]) == []


def test_writable_roots_drops_read_only_directories(tmp_path: Path) -> None:
    """Existing is not enough - the process must be able to write there."""
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


def test_writable_roots_dedupes_while_preserving_order(tmp_path: Path) -> None:
    """Order is the preference ranking, so a repeat must not demote the first hit."""
    output_roots = _load_output_roots()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    roots = output_roots.writable_roots([str(second), str(first), str(second)])

    assert roots == [str(second), str(first)]


def test_writable_roots_reports_absolute_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The MCP server resolves these on its own side, where the cwd differs."""
    output_roots = _load_output_roots()
    nested = tmp_path / "nested"
    nested.mkdir()
    monkeypatch.chdir(tmp_path)

    assert output_roots.writable_roots(["nested"]) == [str(nested)]


def test_writable_roots_ignores_empty_candidates(tmp_path: Path) -> None:
    """Unset Blender-derived candidates arrive as None and must not crash the scan."""
    output_roots = _load_output_roots()

    assert output_roots.writable_roots([None, "", str(tmp_path)]) == [str(tmp_path)]


# ---------------------------------------------------------------------------
# Reporting through the handshake
# ---------------------------------------------------------------------------


def test_get_addon_info_reports_writable_output_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A deployment-configured root has to reach the handshake to be of any use."""
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


def test_get_addon_info_reports_roots_without_any_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unconfigured Blender still has to tell the agent somewhere to write."""
    output_roots = _load_output_roots()
    monkeypatch.delenv(output_roots.OUTPUT_ROOTS_ENV_VAR, raising=False)

    addon, _bpy = _load_addon(monkeypatch, data={"filepath": ""})
    server_core = sys.modules[f"{addon.__name__}.server_core"]
    server = server_core.BlenderMCPServer()

    # The temp directory is always writable, so the list is never empty.
    assert server.get_addon_info()["writable_output_roots"]


# ---------------------------------------------------------------------------
# The enforcing read path
# ---------------------------------------------------------------------------


def test_configured_file_roots_read_their_own_variable_first() -> None:
    """A read-only canon mount can be a file root without becoming an output root."""
    output_roots = _load_output_roots()
    environ = {output_roots.FILE_ROOTS_ENV_VAR: "/canon", output_roots.OUTPUT_ROOTS_ENV_VAR: "/output"}

    assert output_roots.configured_file_roots(environ) == ["/canon"]


def test_configured_file_roots_fall_back_to_the_output_roots() -> None:
    """A deployment that set only the output roots is still enforced, against those."""
    output_roots = _load_output_roots()
    environ = {output_roots.OUTPUT_ROOTS_ENV_VAR: os.pathsep.join(["/output", "/renders"])}

    assert output_roots.configured_file_roots(environ) == ["/output", "/renders"]


def test_a_blank_file_roots_variable_counts_as_unset() -> None:
    """`BLENDERMCP_FILE_ROOTS=""` is how compose spells "not set"; it must not mean "no roots, permissive"."""
    output_roots = _load_output_roots()
    environ = {output_roots.FILE_ROOTS_ENV_VAR: f" {os.pathsep} ", output_roots.OUTPUT_ROOTS_ENV_VAR: "/output"}

    assert output_roots.configured_file_roots(environ) == ["/output"]


def test_configured_file_roots_never_include_the_advisory_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing configured the enforced set is empty, not `~` and the temp dir."""
    output_roots = _load_output_roots()
    monkeypatch.delenv(output_roots.FILE_ROOTS_ENV_VAR, raising=False)
    monkeypatch.delenv(output_roots.OUTPUT_ROOTS_ENV_VAR, raising=False)

    assert output_roots.configured_file_roots() == []


def test_get_addon_info_publishes_enforced_file_roots_in_canonical_form(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A symlinked mount is published under the name containment is decided on."""
    real = tmp_path / "real_canon"
    real.mkdir()
    (tmp_path / "canon").symlink_to(real, target_is_directory=True)
    output_roots = _load_output_roots()
    monkeypatch.setenv(output_roots.FILE_ROOTS_ENV_VAR, str(tmp_path / "canon"))

    addon, _bpy = _load_addon(monkeypatch, data={"filepath": ""})
    server_core = sys.modules[f"{addon.__name__}.server_core"]
    info = server_core.BlenderMCPServer().get_addon_info()

    assert info["file_roots"] == [os.path.realpath(real)]
    assert info["file_roots_enforced"] is True


def test_get_addon_info_publishes_a_permissive_policy_when_no_roots_are_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The advisory list is non-empty on every install; the enforced one must not borrow from it."""
    output_roots = _load_output_roots()
    monkeypatch.delenv(output_roots.FILE_ROOTS_ENV_VAR, raising=False)
    monkeypatch.delenv(output_roots.OUTPUT_ROOTS_ENV_VAR, raising=False)

    addon, _bpy = _load_addon(monkeypatch, data={"filepath": ""})
    server_core = sys.modules[f"{addon.__name__}.server_core"]
    info = server_core.BlenderMCPServer().get_addon_info()

    assert info["writable_output_roots"]
    assert info["file_roots"] == []
    assert info["file_roots_enforced"] is False
