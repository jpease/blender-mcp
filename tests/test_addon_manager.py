"""Tests for addon install + handshake (no Blender required)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, Mock

from blender_mcp.addon_manager import (
    EXPECTED_ADDON_PROTOCOL_VERSION,
    get_bundled_addon_path,
    handshake_addon,
    install_addon,
)


def test_bundled_addon_exists_and_has_protocol() -> None:
    path = get_bundled_addon_path()
    assert path.is_dir()
    text = (path / "__init__.py").read_text(encoding="utf-8")
    assert "ADDON_PROTOCOL_VERSION" in text
    assert f"ADDON_PROTOCOL_VERSION = {EXPECTED_ADDON_PROTOCOL_VERSION}" in text

    server_core = (path / "server_core.py").read_text(encoding="utf-8")
    assert "get_addon_info" in server_core


def test_install_addon_copies_into_target_dir(tmp_path: Path) -> None:
    addons = tmp_path / "scripts" / "addons"
    # Pre-existing oddly named install (what many users have)
    addons.mkdir(parents=True)
    legacy = addons / "addon.py"
    legacy.write_text('bl_info = {\n    "name": "Blender MCP"\n}\n# old\n', encoding="utf-8")

    result = install_addon(addons)
    assert result.success is True
    assert result.target_path is not None
    installed = Path(result.target_path)
    assert installed.is_dir()
    assert "ADDON_PROTOCOL_VERSION" in (installed / "__init__.py").read_text(encoding="utf-8")
    # The legacy single-file install is replaced by the package directory,
    # not left behind alongside it.
    assert not legacy.exists()


def test_handshake_up_to_date() -> None:
    blender = MagicMock()
    blender.send_command.return_value = {
        "protocol_version": EXPECTED_ADDON_PROTOCOL_VERSION,
        "addon_version": [1, 3],
        "capabilities": ["get_addon_info", "get_world_state_snapshot"],
        "blender_version": "4.2.0",
    }
    result = handshake_addon(blender)
    assert result.up_to_date is True
    assert result.source == "native"
    assert result.warning is None


def test_handshake_missing_command_on_old_addon() -> None:
    blender = MagicMock()
    blender.send_command.side_effect = Exception("Unknown command type: get_addon_info")
    result = handshake_addon(blender)
    assert result.up_to_date is False
    assert result.source == "missing"
    assert "install-addon" in (result.warning or "").lower() or "restart" in (result.warning or "").lower()


def test_handshake_outdated_protocol() -> None:
    blender = MagicMock()
    blender.send_command.return_value = {
        "protocol_version": 1,
        "addon_version": [1, 2],
        "capabilities": [],
        "blender_version": "4.0.0",
    }
    result = handshake_addon(blender)
    assert result.up_to_date is False
    assert result.protocol_version == 1


def _stale_addon_source() -> str:
    """
    A stand-in for an old-style single-.py-file legacy install.

    The addon's bl_info and ADDON_PROTOCOL_VERSION marker both live in
    __init__.py, so its content alone is enough to be recognized as a
    (stale) Blender MCP addon file by the marker-based checks below.
    """
    from blender_mcp import addon_manager as am

    # Derive the stale marker from the current expected version so this helper
    # keeps producing a genuinely outdated file across protocol bumps.
    return (
        (am.get_bundled_addon_path() / "__init__.py")
        .read_text(encoding="utf-8")
        .replace(
            f"ADDON_PROTOCOL_VERSION = {am.EXPECTED_ADDON_PROTOCOL_VERSION}",
            "ADDON_PROTOCOL_VERSION = 0",
            1,
        )
    )


def test_startup_check_never_writes(tmp_path: Path, monkeypatch) -> None:
    """Starting the server must not modify the user's Blender files."""
    from blender_mcp import addon_manager as am

    addons = tmp_path / "4.2" / "scripts" / "addons"
    addons.mkdir(parents=True)
    stale = addons / "blender_mcp.py"
    stale.write_text(_stale_addon_source(), encoding="utf-8")
    before = stale.read_bytes()
    listing_before = sorted(p.name for p in addons.iterdir())

    monkeypatch.setattr(am, "discover_blender_addon_dirs", lambda: [addons])
    report = am.check_addon_status_on_startup()

    assert report.needs_action is True
    assert str(stale) in report.outdated_paths
    assert "install-addon" in report.message
    # The whole point of detect-and-tell: nothing on disk changed.
    assert stale.read_bytes() == before
    assert sorted(p.name for p in addons.iterdir()) == listing_before


def test_startup_check_reports_current(tmp_path: Path, monkeypatch) -> None:
    from blender_mcp import addon_manager as am

    addons = tmp_path / "4.2" / "scripts" / "addons"
    addons.mkdir(parents=True)
    (addons / "blender_mcp.py").write_text(
        (am.get_bundled_addon_path() / "__init__.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    monkeypatch.setattr(am, "discover_blender_addon_dirs", lambda: [addons])
    report = am.check_addon_status_on_startup()
    assert report.needs_action is False
    assert report.reason == "already_current"


def test_startup_check_reports_missing_install(tmp_path: Path, monkeypatch) -> None:
    from blender_mcp import addon_manager as am

    addons = tmp_path / "4.2" / "scripts" / "addons"
    addons.mkdir(parents=True)

    monkeypatch.setattr(am, "discover_blender_addon_dirs", lambda: [addons])
    report = am.check_addon_status_on_startup()
    assert report.missing is True
    assert report.needs_action is True
    assert "install-addon" in report.message


def test_install_updates_extensions_dir_when_addon_lives_there(tmp_path: Path, monkeypatch) -> None:
    """Blender 4.2+: update the loaded copy, don't add a second one."""
    from blender_mcp import addon_manager as am

    scripts = tmp_path / "4.2" / "scripts" / "addons"
    extensions = tmp_path / "4.2" / "extensions" / "user_default"
    scripts.mkdir(parents=True)
    extensions.mkdir(parents=True)
    stale_install = extensions / "blender_mcp.py"
    stale_install.write_text(_stale_addon_source(), encoding="utf-8")

    # discover_blender_addon_dirs lists scripts/addons first.
    monkeypatch.setattr(am, "discover_blender_addon_dirs", lambda: [scripts, extensions])
    result = am.install_addon()

    assert result.success is True
    updated = extensions / "blender_mcp"
    assert am.read_addon_protocol_version(updated) == (am.EXPECTED_ADDON_PROTOCOL_VERSION), (
        "the actually-loaded extensions copy was left stale"
    )
    assert not stale_install.exists(), "old single-file install was left behind"
    assert not (scripts / "blender_mcp").exists(), (
        "installed a duplicate into scripts/addons instead of updating in place"
    )


def test_repeat_install_preserves_original_backup(tmp_path: Path) -> None:
    """A second install must not overwrite the .bak holding the user's edits."""
    from blender_mcp import addon_manager as am

    addons = tmp_path / "4.2" / "scripts" / "addons"
    addons.mkdir(parents=True)
    target = addons / "blender_mcp.py"
    original = _stale_addon_source() + "\n# USER LOCAL EDIT\n"
    target.write_text(original, encoding="utf-8")

    assert am.install_addon(addons).success
    backup = target.with_suffix(".py.bak")
    assert backup.is_file()
    assert "USER LOCAL EDIT" in backup.read_text(encoding="utf-8")

    # Second run: file already matches the bundled source, so nothing to back up.
    assert am.install_addon(addons).success
    assert "USER LOCAL EDIT" in backup.read_text(encoding="utf-8"), (
        "repeat install clobbered the backup of the user's previous addon"
    )


def test_handshake_surfaces_writable_output_roots() -> None:
    """The roots Blender reports have to reach the handshake the server caches."""
    blender = Mock()
    blender.send_command.return_value = {
        "protocol_version": EXPECTED_ADDON_PROTOCOL_VERSION,
        "addon_version": [1, 2, 0],
        "capabilities": ["get_addon_info"],
        "blender_version": "5.2.1",
        "writable_output_roots": ["/output", "/tmp"],
    }

    handshake = handshake_addon(blender)

    assert handshake.writable_output_roots == ["/output", "/tmp"]


def test_handshake_defaults_writable_output_roots_when_the_addon_omits_them() -> None:
    """
    An addon one protocol behind sends no roots, and must not break the handshake.

    The `== []` assertion alone cannot tell "the parse fell back correctly" from
    "no parse exists at all", because `AddonHandshake.writable_output_roots` is
    itself a `field(default_factory=list)`. The populated payload below is the
    control that makes the first assertion mean something: both answers come out
    of the same parse, so deleting that parse fails this test rather than
    leaving it quietly passing on the dataclass default.
    """
    blender = Mock()
    blender.send_command.return_value = {
        "protocol_version": EXPECTED_ADDON_PROTOCOL_VERSION - 1,
        "addon_version": [1, 2, 0],
        "capabilities": ["get_addon_info"],
        "blender_version": "5.2.1",
    }

    assert handshake_addon(blender).writable_output_roots == []

    blender.send_command.return_value = {
        **blender.send_command.return_value,
        "writable_output_roots": ["/output"],
    }

    assert handshake_addon(blender).writable_output_roots == ["/output"], (
        "the empty list above came from the dataclass default, not from parsing the payload"
    )
