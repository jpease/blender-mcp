"""
Bundle, install, and version-check the Blender MCP addon.

Existing users often update only the MCP server (`pipx upgrade blender-mcp`). This module:
1. Ships a bundled copy of the addon package inside the package
2. Can copy it into Blender's user addons directory (`install-addon`)
3. Handshake with a running addon to detect outdated installs

The addon is a package directory (``__init__.py`` + submodules), not a single
``.py`` file. Everywhere below that deals with "the addon file" therefore
means ``<addon dir>/__init__.py`` when the path in question is a directory.
"""

from __future__ import annotations

import filecmp
import logging
import os
import re
import shutil
import sys

from dataclasses import dataclass, field
from pathlib import Path

from .text_hygiene import strip_unsafe

logger = logging.getLogger("BlenderMCPServer")

# Must match ADDON_PROTOCOL_VERSION in bundled/addon/__init__.py
EXPECTED_ADDON_PROTOCOL_VERSION = 31

_ADDON_MARKER = 'bl_info = {\n    "name": "Blender MCP"'
_INSTALLED_DIRNAME = "blender_mcp"
_PROTOCOL_RE = re.compile(r"ADDON_PROTOCOL_VERSION\s*=\s*(\d+)")
_BL_INFO_NAME_RE = re.compile(r"""["']name["']\s*:\s*["']Blender MCP["']""")


def _addon_init_file(path: Path) -> Path:
    """
    Return the file that actually carries the addon's metadata for `path`.

    `path` may be a single-file legacy install or a package directory; in the
    latter case the metadata (bl_info, ADDON_PROTOCOL_VERSION) lives in its
    __init__.py.

    Args:
        path: Filesystem path to inspect or update.

    Returns:
        Path: Result produced by the operation.

    """
    return path / "__init__.py" if path.is_dir() else path


def read_addon_protocol_version(path: Path) -> int | None:
    """
    Parse ADDON_PROTOCOL_VERSION from an installed addon file or package dir.

    Args:
        path: Filesystem path to inspect or update.

    Returns:
        int | None: Result produced by the operation.

    """
    try:
        text = _addon_init_file(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    match = _PROTOCOL_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def addon_file_needs_update(path: Path) -> bool:
    """
    True if path is missing protocol metadata or behind the bundled addon.

    Args:
        path: Filesystem path to inspect or update.

    Returns:
        bool: Result produced by the operation.

    """
    if not _addon_init_file(path).is_file():
        return True
    installed = read_addon_protocol_version(path)
    if installed is None:
        return True
    return installed < EXPECTED_ADDON_PROTOCOL_VERSION


@dataclass
class AddonStatusReport:
    """Read-only view of the addon files on disk versus the bundled copy."""

    checked: bool
    outdated_paths: list[str]
    missing: bool
    message: str
    reason: str | None = None

    @property
    def needs_action(self) -> bool:
        return bool(self.outdated_paths) or self.missing


_UPDATE_HINT = (
    "Run `blender-mcp install-addon` to update it, then in Blender: "
    "Preferences → Add-ons → disable and re-enable 'Interface: Blender MCP' "
    "(or restart Blender) and click Start MCP Server."
)


def check_addon_status_on_startup() -> AddonStatusReport:
    """
    Report whether the on-disk Blender addon is behind the bundled copy.

    Deliberately read-only. Starting an MCP server is not a request to modify
    files in the user's Blender configuration, and a silent overwrite at an
    unrelated moment can discard local edits with no prompt and no undo. We
    detect and tell; `install-addon` does the writing, when the user asks.
    Never raises; safe to call from server lifespan.

    Returns:
        AddonStatusReport: Result produced by the operation.

    """
    try:
        dirs = discover_blender_addon_dirs()
        if not dirs:
            return AddonStatusReport(
                checked=False,
                outdated_paths=[],
                missing=False,
                reason="no_addons_dir",
                message=(
                    "Could not find a Blender addons folder. If Blender is "
                    "installed, set BLENDERMCP_ADDONS_DIR, or install the addon "
                    "package manually from the repo."
                ),
            )

        existing = find_existing_addon_installs(dirs)
        if not existing:
            return AddonStatusReport(
                checked=True,
                outdated_paths=[],
                missing=True,
                reason="not_installed",
                message=(
                    "Blender MCP addon not found in any Blender addons folder. "
                    "Run `blender-mcp install-addon` to install it."
                ),
            )

        outdated = [str(p) for p in existing if addon_file_needs_update(p)]
        if not outdated:
            return AddonStatusReport(
                checked=True,
                outdated_paths=[],
                missing=False,
                reason="already_current",
                message=(f"Blender addon on disk is current (protocol {EXPECTED_ADDON_PROTOCOL_VERSION})."),
            )

        return AddonStatusReport(
            checked=True,
            outdated_paths=outdated,
            missing=False,
            reason="outdated",
            message=(
                f"Blender MCP addon on disk is outdated (expected protocol "
                f"{EXPECTED_ADDON_PROTOCOL_VERSION}): {', '.join(outdated)}. " + _UPDATE_HINT
            ),
        )
    except Exception as e:
        logger.debug(f"Addon status check failed: {e}")
        return AddonStatusReport(
            checked=False,
            outdated_paths=[],
            missing=False,
            reason="error",
            message=f"Could not check Blender addon status: {e}",
        )


@dataclass
class AddonInstallResult:
    """Describe the outcome and destination of an addon installation."""

    success: bool
    message: str
    target_path: str | None = None
    addons_dir: str | None = None


@dataclass
class AddonHandshake:
    """Store addon compatibility information reported during a handshake."""

    up_to_date: bool
    protocol_version: int | None
    addon_version: list[int] | None
    capabilities: list[str]
    blender_version: str | None
    source: str  # native | missing | error
    warning: str | None = None
    # Advisory. Older addons omit it, hence the empty default.
    writable_output_roots: list[str] = field(default_factory=list)
    # When the (session_id, session_epoch) pair changes, `capabilities` may be
    # stale: that set depends on the open .blend's scene flags. Older addons omit
    # all three.
    session_epoch: int | None = None
    current_filepath: str | None = None
    session_id: str | None = None
    # While set, the addon refuses every command except the handshake, session
    # info and swap commands, and a client that cannot read this field cannot tell
    # those refusals from a broken addon. Older addons omit it.
    session_indeterminate: bool = False
    # Where .blend file commands are confined, unlike the advisory
    # `writable_output_roots`. The defaults are true of older addons, which
    # enforce nothing.
    file_roots: list[str] = field(default_factory=list)
    file_roots_enforced: bool = False

    def session_marker(self) -> tuple[str | None, int | None]:
        """
        Report the pair a client compares to decide whether its capabilities went stale.

        The epoch restarts at 0 when the addon's modules reload, so the same epoch
        can recur for a different database; the id is minted anew each time.

        Returns:
            tuple[str | None, int | None]: `(session_id, session_epoch)`. Either
            half is None for an older addon, and then the pair never changes.

        """
        return (self.session_id, self.session_epoch)


def get_bundled_addon_path() -> Path:
    """
    Resolve the addon package directory shipped with this package.

    Returns:
        Path: Result produced by the operation.

    Raises:
        FileNotFoundError: If the operation cannot be completed.

    """
    here = Path(__file__).resolve().parent
    candidate = here / "bundled" / "addon"
    if candidate.is_dir() and (candidate / "__init__.py").is_file():
        return candidate
    raise FileNotFoundError(
        "Bundled Blender MCP addon package not found. Reinstall blender-mcp or "
        "copy the addon package from the GitHub repo into Blender manually."
    )


def discover_blender_addon_dirs() -> list[Path]:
    """
    Find Blender user scripts/addons directories across versions.

    Returns:
        list[Path]: Result produced by the operation.

    """
    dirs: list[Path] = []
    home = Path.home()

    if sys.platform == "darwin":
        base = home / "Library" / "Application Support" / "Blender"
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) / "Blender Foundation" / "Blender" if appdata else None
    else:
        base = home / ".config" / "blender"

    if base and base.is_dir():
        for child in sorted(base.iterdir(), reverse=True):
            if not child.is_dir():
                continue
            # Blender versions look like 3.6, 4.0, 4.2
            if not re.match(r"^\d+\.\d+", child.name):
                continue
            dirs.append(child / "scripts" / "addons")
            # Blender 4.2+ installs through the extensions system.
            extensions = child / "extensions" / "user_default"
            if extensions.is_dir():
                dirs.append(extensions)

    env = os.environ.get("BLENDER_USER_ADDONS") or os.environ.get("BLENDERMCP_ADDONS_DIR")
    if env:
        env_path = Path(env).expanduser()
        dirs.insert(0, env_path)

    seen: set[str] = set()
    unique: list[Path] = []
    for d in dirs:
        key = str(d)
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique


def _is_blendermcp_addon_file(path: Path) -> bool:
    """
    True only for a file whose bl_info declares it as the Blender MCP addon.

    Deliberately narrower than a substring search for "BlenderMCPServer": that
    also matches a user's own fork or a script that merely references the class,
    and install_addon overwrites everything this returns True for.

    Args:
        path: Filesystem path to inspect or update.

    Returns:
        bool: Result produced by the operation.

    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return _BL_INFO_NAME_RE.search(text) is not None


def find_existing_addon_installs(addons_dirs: list[Path] | None = None) -> list[Path]:
    """
    Locate already-installed Blender MCP addon files.

    Args:
        addons_dirs: Value for addons dirs.

    Returns:
        list[Path]: Result produced by the operation.

    """
    found: list[Path] = []
    for addons_dir in addons_dirs or discover_blender_addon_dirs():
        if not addons_dir.is_dir():
            continue
        for path in addons_dir.iterdir():
            if path.is_file() and path.suffix == ".py" and _is_blendermcp_addon_file(path):
                found.append(path)
            elif path.is_dir() and (path / "__init__.py").is_file():
                init = path / "__init__.py"
                if _is_blendermcp_addon_file(init):
                    found.append(init)
    return found


def _trees_equal(a: Path, b: Path) -> bool:
    """
    Recursively compare two directory trees for identical content.

    Args:
        a: Value for a.
        b: Value for b.

    Returns:
        bool: Result produced by the operation.

    """
    cmp = filecmp.dircmp(a, b)
    if cmp.left_only or cmp.right_only or cmp.diff_files or cmp.funny_files:
        return False
    return all(_trees_equal(a / sub, b / sub) for sub in cmp.common_dirs)


def _backup_addon_file(path: Path, source: Path | None = None) -> Path | None:
    """
    Keep one .bak copy before overwriting, so local edits are recoverable.

    `path` may be a single-file legacy install or a package directory.
    Skipped when it already matches `source`: a repeat install would
    otherwise overwrite a .bak holding the user's real previous version with
    an identical copy of the bundled addon, destroying the very edits the
    backup exists to preserve.

    Args:
        path: Filesystem path to inspect or update.
        source: Value for source.

    Returns:
        Path | None: Result produced by the operation.

    """
    if not path.exists():
        return None
    if source is not None:
        try:
            if path.is_dir() and source.is_dir() and _trees_equal(path, source):
                return None
            if path.is_file() and source.is_file() and path.read_bytes() == source.read_bytes():
                return None
        except OSError as e:
            logger.debug(f"Could not compare {path} with {source}: {e}")
    backup = path.with_name(path.name + ".bak")
    try:
        if backup.exists():
            shutil.rmtree(backup) if backup.is_dir() else backup.unlink()
        if path.is_dir():
            shutil.copytree(path, backup)
        else:
            shutil.copy2(path, backup)
        return backup
    except OSError as e:
        logger.debug(f"Could not back up {path}: {e}")
        return None


def install_addon(
    addons_dir: Path | None = None,
    *,
    create_dir: bool = True,
) -> AddonInstallResult:
    """
    Copy the bundled addon into Blender's user addons folder.

    Replaces known existing Blender MCP addon files in that directory.
    User must disable/enable the addon or restart Blender to load the new code.

    Args:
        addons_dir: Value for addons dir.
        create_dir: Value for create dir.

    Returns:
        AddonInstallResult: Result produced by the operation.

    """
    try:
        source = get_bundled_addon_path()
    except FileNotFoundError as e:
        return AddonInstallResult(False, str(e))

    if addons_dir is None:
        dirs = discover_blender_addon_dirs()
        if not dirs:
            return AddonInstallResult(
                False,
                "Could not find a Blender user addons directory. "
                "Set BLENDERMCP_ADDONS_DIR to your Blender scripts/addons path, "
                "or install the addon package manually from the repo.",
            )
        # Update where the addon already lives rather than the newest
        # scripts/addons dir.
        existing = find_existing_addon_installs(dirs)
        if existing:
            # A package install is reported by its `__init__.py`; the addons dir is one level higher.
            found = existing[0]
            addons_dir = found.parent.parent if found.name == "__init__.py" else found.parent
        else:
            addons_dir = dirs[0]

    addons_dir = Path(addons_dir).expanduser()
    if not addons_dir.exists():
        if not create_dir:
            return AddonInstallResult(
                False,
                f"Addons directory does not exist: {addons_dir}",
                addons_dir=str(addons_dir),
            )
        try:
            addons_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return AddonInstallResult(
                False,
                f"Failed to create addons directory {addons_dir}: {e}",
                addons_dir=str(addons_dir),
            )

    replaced: list[str] = []
    if addons_dir.is_dir():
        for path in list(addons_dir.iterdir()):
            is_legacy_file = path.is_file() and path.suffix == ".py" and _is_blendermcp_addon_file(path)
            is_package_dir = (
                path.is_dir() and (path / "__init__.py").is_file() and _is_blendermcp_addon_file(path / "__init__.py")
            )
            if not (is_legacy_file or is_package_dir):
                continue
            # A legacy single-file install can never match the package
            # source byte-for-byte, so always back it up rather than trying
            # to content-compare a file against a directory.
            _backup_addon_file(path, source if is_package_dir else None)
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            replaced.append(str(path))

    target = addons_dir / _INSTALLED_DIRNAME
    shutil.copytree(source, target)
    if str(target) not in replaced:
        replaced.append(str(target))

    msg = (
        f"Installed Blender MCP addon to {target}. "
        "In Blender: Preferences → Add-ons → disable then enable "
        "'Interface: Blender MCP', or restart Blender, then click Start MCP Server."
    )
    if len(replaced) > 1:
        msg += f" Also updated: {', '.join(replaced[:-1])}."

    return AddonInstallResult(
        True,
        msg,
        target_path=str(target),
        addons_dir=str(addons_dir),
    )


def normalized_session_epoch(value: object) -> int | None:
    """
    Read an integer an addon may not have sent, without confusing 0 for absent.

    A freshly started Blender reports `session_epoch` 0, which `value or None`
    would drop.

    Args:
        value: The raw payload value; untrusted, so any JSON type.

    Returns:
        int | None: The integer, or None when absent or unparsable. A JSON
        boolean is rejected rather than read as 0 or 1.

    """
    if isinstance(value, bool) or not isinstance(value, int | str):
        return None
    try:
        return int(value)
    except ValueError:
        return None


# Longer than any real path, and short enough that this field cannot push
# megabytes into an agent's context through `get_addon_status`.
_MAX_REPORTED_PATH_CHARS = 4096


def normalized_session_text(value: object, max_chars: int = _MAX_REPORTED_PATH_CHARS) -> str | None:
    r"""
    Read a string an addon may not have sent, refusing anything that is not one.

    This is the server boundary for an unauthenticated socket whose values reach
    an agent's context through `get_addon_status`, so it removes the control,
    format and bidi characters that can inject instructions or disguise a path.
    `strip_unsafe` is a checked copy of the addon's rule; `blender_mcp.text_hygiene`
    explains why.

    None means either "not reported" or "refused". For `current_filepath` that
    reads as a never-saved session, which an agent may treat as safe to
    overwrite. Separating the two would change the wire contract, so refusals are
    logged instead.

    Args:
        value: The raw payload value, possibly missing or of any JSON type.
        max_chars: The longest string accepted. Longer ones are refused, not
            truncated, because a truncated path reads as a real one.

    Returns:
        str | None: The string with every unsafe character removed, or None when
        absent, not a string, empty (before or after stripping), or implausibly
        long.

    """
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > max_chars:
        logger.warning(f"Addon reported an unusable text field ({type(value).__name__}); treating it as absent")
        return None
    cleaned = strip_unsafe(value)
    if cleaned != value:
        logger.warning(
            "Addon reported a text field carrying control, format or bidi characters; "
            f"{len(value) - len(cleaned)} character(s) were removed before it was published"
        )
    return cleaned or None


# A hex uuid is 32 characters; the slack lets a future addon lengthen it. The
# handshake and per-response parses must share this bound: an id one accepts and
# the other refuses never compares equal, marking the session stale every time.
_MAX_SESSION_ID_CHARS = 128


def normalized_session_id(value: object) -> str | None:
    """
    Read a `session_id` the same way on the handshake path and the response path.

    Args:
        value: The raw payload value, possibly missing or of any JSON type.

    Returns:
        str | None: The id, or None when absent or unusable.

    """
    return normalized_session_text(value, max_chars=_MAX_SESSION_ID_CHARS)


def _is_structurally_intact(element: object, cleaned: str) -> bool:
    r"""
    Report whether cleaning an element left the thing it names unchanged.

    Removing characters can create structure the raw string lacked:
    `/studio/out/.\u200b./secrets` becomes `/studio/secrets`, and `open_shot\u202e`
    becomes a real capability name. Trimmed whitespace does the same, and on POSIX
    a padded path is a different path, so only an exact no-op counts.

    Args:
        element: The raw payload element, of any JSON type.
        cleaned: What `normalized_session_text` made of it.

    Returns:
        bool: True when the element may be published.

    """
    return isinstance(element, str) and cleaned == element


def normalized_session_text_list(value: object, max_chars: int = _MAX_REPORTED_PATH_CHARS) -> list[str]:
    """
    Read a list of strings an addon may not have sent, element by element.

    A string payload would otherwise iterate as characters. Each element goes
    through `normalized_session_text`, and one it refuses or changes is dropped,
    not replaced: these fields are membership sets (`capabilities` gates
    `send_command`), where a placeholder or a cleaned element could match an entry
    the addon never sent. See `_is_structurally_intact`.

    Args:
        value: The raw payload value, possibly missing or of any JSON type.
        max_chars: The longest element accepted; longer ones are dropped.

    Returns:
        list[str]: The usable elements, in order. Empty when the payload was
        absent, was not a list, or carried nothing usable.

    """
    if value is None:
        return []
    if not isinstance(value, list | tuple):
        logger.warning(f"Addon reported an unusable list field ({type(value).__name__}); treating it as absent")
        return []
    admitted: list[str] = []
    for element in value:
        cleaned = normalized_session_text(element, max_chars=max_chars)
        if cleaned is None:
            continue
        if not _is_structurally_intact(element, cleaned):
            logger.warning(
                "Addon reported a list element whose meaning changes once unsafe characters are removed; "
                "it was dropped rather than published in its cleaned form"
            )
            continue
        admitted.append(cleaned)
    return admitted


# `bl_info["version"]` is a short tuple of ints; eight parts is ample.
_MAX_VERSION_PARTS = 8


def normalized_addon_version(value: object) -> list[int] | None:
    """
    Read `addon_version` as the list of integers it is declared to be.

    Unlike its sibling fields this is not text, so a string arriving here is
    refused rather than stripped and published as a version.

    Args:
        value: The raw payload value, possibly missing or of any JSON type.

    Returns:
        list[int] | None: The version parts, or None when the payload is absent,
        is not a list/tuple of plain integers, is empty, or is implausibly long.
        A JSON boolean does not count as an integer.

    """
    if value is None:
        return None
    if not isinstance(value, list | tuple) or not value or len(value) > _MAX_VERSION_PARTS:
        logger.warning(f"Addon reported an unusable addon_version ({type(value).__name__}); treating it as absent")
        return None
    if any(isinstance(part, bool) or not isinstance(part, int) for part in value):
        logger.warning("Addon reported an addon_version whose parts are not integers; treating it as absent")
        return None
    return list(value)


def handshake_addon(blender_connection) -> AddonHandshake:
    """
    Query a connected Blender addon for protocol version.

    Old addons without get_addon_info are treated as outdated (but still usable
    through validated dedicated tools elsewhere).

    Args:
        blender_connection: Value for blender connection.

    Returns:
        AddonHandshake: Result produced by the operation.

    """
    try:
        info = blender_connection.send_command("get_addon_info")
        if not isinstance(info, dict):
            return AddonHandshake(
                up_to_date=False,
                protocol_version=None,
                addon_version=None,
                capabilities=[],
                blender_version=None,
                source="error",
                warning="Addon returned invalid get_addon_info payload.",
            )
        protocol = info.get("protocol_version")
        try:
            protocol_i = int(protocol) if protocol is not None else None
        except (TypeError, ValueError):
            protocol_i = None

        up_to_date = protocol_i is not None and protocol_i >= EXPECTED_ADDON_PROTOCOL_VERSION
        warning = None
        if not up_to_date:
            warning = (
                f"Blender addon protocol {protocol_i!r} is behind "
                f"expected {EXPECTED_ADDON_PROTOCOL_VERSION}. "
                "Run `blender-mcp install-addon` to update it, then "
                "restart Blender or disable/enable 'Interface: Blender MCP', "
                "then Start MCP Server."
            )
        return AddonHandshake(
            up_to_date=up_to_date,
            protocol_version=protocol_i,
            # Every field is normalized: a raw one could carry newlines, ESC or
            # bidi characters into `get_addon_status` and the handshake log.
            addon_version=normalized_addon_version(info.get("addon_version")),
            capabilities=normalized_session_text_list(info.get("capabilities")),
            blender_version=normalized_session_text(info.get("blender_version")),
            source="native",
            warning=warning,
            writable_output_roots=normalized_session_text_list(info.get("writable_output_roots")),
            session_epoch=normalized_session_epoch(info.get("session_epoch")),
            current_filepath=normalized_session_text(info.get("current_filepath")),
            session_id=normalized_session_id(info.get("session_id")),
            # `is True`: a truthy string or int off the socket is not a yes.
            session_indeterminate=info.get("session_indeterminate") is True,
            file_roots=normalized_session_text_list(info.get("file_roots")),
            file_roots_enforced=info.get("file_roots_enforced") is True,
        )
    except Exception as e:
        msg = str(e).lower()
        if "unknown command" in msg or "get_addon_info" in msg:
            warning = (
                "Blender addon is outdated (no get_addon_info). "
                "Run `blender-mcp install-addon` to update it, then "
                "restart Blender or disable/enable 'Interface: Blender MCP', "
                "then Start MCP Server. Fallbacks keep working in the meantime."
            )
            return AddonHandshake(
                up_to_date=False,
                protocol_version=None,
                addon_version=None,
                capabilities=[],
                blender_version=None,
                source="missing",
                warning=warning,
            )
        return AddonHandshake(
            up_to_date=False,
            protocol_version=None,
            addon_version=None,
            capabilities=[],
            blender_version=None,
            source="error",
            # The one warning built from payload text: `e` can carry the addon's
            # own error message, so it is stripped before reaching an agent.
            warning=strip_unsafe(f"Addon handshake failed: {e}"),
        )


def format_handshake_log(result: AddonHandshake) -> str:
    if result.up_to_date:
        return (
            f"Blender addon up to date "
            f"(protocol {result.protocol_version}, "
            f"addon {result.addon_version}, "
            f"Blender {result.blender_version})"
        )
    return result.warning or "Blender addon may be outdated."


def run_cli(argv: list[str] | None = None) -> int:
    """
    CLI entry for install-addon / addon-status. Returns process exit code.

    Args:
        argv: Value for argv.

    Returns:
        int: Result produced by the operation.

    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="blender-mcp",
        description="Blender MCP server and addon installer",
    )
    sub = parser.add_subparsers(dest="command")

    install_p = sub.add_parser(
        "install-addon",
        help="Copy the bundled addon package into Blender's user addons folder",
    )
    install_p.add_argument(
        "--addons-dir",
        type=str,
        default=None,
        help="Override Blender scripts/addons directory (or set BLENDERMCP_ADDONS_DIR)",
    )

    sub.add_parser(
        "addon-paths",
        help="List discovered Blender user addons directories",
    )

    args = parser.parse_args(argv)

    if args.command == "install-addon":
        result = install_addon(
            Path(args.addons_dir) if args.addons_dir else None,
        )
        print(result.message)
        return 0 if result.success else 1

    if args.command == "addon-paths":
        dirs = discover_blender_addon_dirs()
        if not dirs:
            print("No Blender addons directories found.")
            return 1
        for d in dirs:
            marker = " (exists)" if d.is_dir() else " (missing)"
            print(f"{d}{marker}")
        existing = find_existing_addon_installs(dirs)
        if existing:
            print("\nExisting Blender MCP installs:")
            for p in existing:
                print(f"  {p}")
        return 0

    # No subcommand → caller should start MCP server
    return -1
