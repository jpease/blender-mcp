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
import json
import logging
import os
import re
import shutil
import sys

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .text_hygiene import strip_unsafe

logger = logging.getLogger("BlenderMCPServer")

# Must match ADDON_PROTOCOL_VERSION in bundled/addon/__init__.py
EXPECTED_ADDON_PROTOCOL_VERSION = 41

_ADDON_MARKER = 'bl_info = {\n    "name": "Blender MCP"'
_INSTALLED_DIRNAME = "blender_mcp"
# Backups live here, a sibling of `scripts/addons`, because Blender loads every directory
# under `scripts/addons` that carries a `bl_info` - a backup kept there becomes a duplicate
# "Blender MCP" in the user's Add-ons list, and one of them can be enabled by mistake.
_BACKUP_DIRNAME = "blendermcp_backups"
_PROTOCOL_RE = re.compile(r"ADDON_PROTOCOL_VERSION\s*=\s*(\d+)")
_BL_INFO_NAME_RE = re.compile(r"""["']name["']\s*:\s*["']Blender MCP["']""")

# The committed snapshot of the bundled addon's dispatch surface, written by
# `scripts/update_addon_surface.py` and shipped in the wheel (see package-data in
# pyproject.toml). `tests/test_addon_surface.py` explains the incident it exists for.
ADDON_SURFACE_PATH = Path(__file__).resolve().parent / "addon_surface.json"


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
    Report whether path is missing protocol metadata or behind the bundled addon.

    Protocol-only, and deliberately so: this reads a file on disk, and comparing two
    integers is all a file read can do cheaply. It therefore cannot see the case that
    burned a user - an install whose protocol number matches but whose dispatch table
    predates commands this server knows about. `handshake_addon` covers that gap
    against the live addon, by diffing the reported commands against the committed
    `addon_surface.json`; no hash of the installed tree is needed here, because
    `tests/test_addon_surface.py` keeps the protocol number honest at the source.

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
    # Per-command accepted keyword names ("*" = accepts arbitrary kwargs via **kwargs).
    # Internal only - read by connection.send_command's preflight gate. Deliberately never
    # added to tools/core.py:_status_payload: `capabilities` alone already gets shortened by
    # envelope._fit_budget under get_addon_status(detail=True), and this field is far heavier
    # per entry - it would make that truncation dramatically worse.
    capability_params: dict[str, list[str] | str] = field(default_factory=dict)
    # What the committed `addon_surface.json` expects and this addon did not report,
    # computed once during the handshake so `get_addon_status` can show it without
    # rebuilding the diff. Non-empty means the installed addon predates this server
    # even though its protocol number does not say so. Both stay empty for an addon
    # that is behind on protocol too: the protocol warning already says reinstall.
    missing_commands: list[str] = field(default_factory=list)
    missing_parameters: dict[str, list[str]] = field(default_factory=dict)

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


def backup_directory(addons_dir: Path) -> Path:
    """
    Name the directory backups are kept in, beside the addons directory, never inside it.

    Blender loads every directory under `scripts/addons` that carries a `bl_info`, so a
    backup kept there is a second enabled-able "Blender MCP" in the user's Add-ons list,
    indistinguishable from the real one. `scripts/` itself is not an addon search path.

    Args:
        addons_dir: The Blender addons directory being installed into.

    Returns:
        Path: Where `_backup_addon_file` writes, created on demand.

    """
    return addons_dir.parent / _BACKUP_DIRNAME


def _backup_addon_file(path: Path, source: Path | None = None) -> Path | None:
    """
    Keep one backup copy before overwriting, so local edits are recoverable.

    `path` may be a single-file legacy install or a package directory.
    Skipped when it already matches `source`: a repeat install would
    otherwise overwrite a backup holding the user's real previous version with
    an identical copy of the bundled addon, destroying the very edits the
    backup exists to preserve.

    Args:
        path: Filesystem path to inspect or update.
        source: The bundled addon, to skip backing up a copy of itself.

    Returns:
        Path | None: The backup written, or None when there was nothing to keep.

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
    backup = backup_directory(path.parent) / (path.name + ".bak")
    try:
        backup.parent.mkdir(parents=True, exist_ok=True)
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


def _clear_existing_installs(addons_dir: Path, source: Path) -> tuple[list[str], list[str], list[str]]:
    """
    Back up and remove every Blender MCP install in `addons_dir`, leaving backups and links alone.

    A `<name>.bak` here was written by an older installer, which kept its backups where
    Blender scans. Treating one as an install would back it up again as `<name>.bak.bak`
    and leave both behind - the reason a single install grew into four entries in the
    user's Add-ons list. They are the user's data, so they are reported, not deleted.

    A symlink is a development setup pointing Blender straight at a checkout. Copying it
    would duplicate the working tree and `shutil.rmtree` refuses a symlink outright, so it
    is reported too and left exactly as it is.

    Args:
        addons_dir: The Blender addons directory to sweep.
        source: The bundled addon, so an install identical to it is not backed up again.

    Returns:
        tuple[list[str], list[str], list[str]]: The installs removed, the stale backups
        left in place, and the symlinks left in place.

    """
    replaced: list[str] = []
    stale_backups: list[str] = []
    linked: list[str] = []
    if not addons_dir.is_dir():
        return replaced, stale_backups, linked
    for path in sorted(addons_dir.iterdir()):
        is_legacy_file = not path.is_symlink() and path.is_file() and _is_blendermcp_addon_file(path)
        is_package_dir = (
            path.is_dir() and (path / "__init__.py").is_file() and _is_blendermcp_addon_file(path / "__init__.py")
        )
        if not ((is_legacy_file and path.suffix == ".py") or is_package_dir):
            continue
        if path.is_symlink():
            linked.append(f"{path} -> {os.readlink(path)}")
            continue
        if path.name.endswith(".bak"):
            stale_backups.append(str(path))
            continue
        # A legacy single-file install can never match the package source byte-for-byte,
        # so always back it up rather than trying to content-compare a file against a directory.
        _backup_addon_file(path, source if is_package_dir else None)
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        replaced.append(str(path))
    return replaced, stale_backups, linked


def _install_message(
    target: Path,
    addons_dir: Path,
    *,
    replaced: list[str],
    linked: list[str],
    stale_backups: list[str],
) -> str:
    """
    Say what was installed, and name anything else in that directory Blender will also load.

    Every copy under `scripts/addons` carrying the addon's `bl_info` appears in the user's
    Add-ons list under the same name, so one of them can be enabled by mistake and run a
    different protocol against this server. Naming them is the only way that is visible.

    Args:
        target: Where the addon was copied.
        addons_dir: The Blender addons directory installed into.
        replaced: Installs this run removed, `target` included.
        linked: Symlinked checkouts left in place.
        stale_backups: Backups an older installer left in `addons_dir`.

    Returns:
        str: The message `AddonInstallResult` carries.

    """
    msg = (
        f"Installed Blender MCP addon to {target}. "
        "In Blender: Preferences → Add-ons → disable then enable "
        "'Interface: Blender MCP', or restart Blender, then click Start MCP Server."
    )
    if len(replaced) > 1:
        msg += f" Also updated: {', '.join(replaced[:-1])}."
    if linked:
        msg += f" Left these symlinked addons alone, each a checkout Blender loads directly: {', '.join(linked)}."
    if stale_backups:
        msg += (
            f" Blender also loads these older backups as duplicate 'Blender MCP' addons, "
            f"and enabling one runs that version instead: {', '.join(stale_backups)}. "
            "Delete them once you no longer need their contents; backups now go to "
            f"{backup_directory(addons_dir)}, which Blender does not scan."
        )
    return msg


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

    replaced, stale_backups, linked = _clear_existing_installs(addons_dir, source)

    target = addons_dir / _INSTALLED_DIRNAME
    if target.is_symlink():
        # A development setup: Blender already loads the checkout this server runs from,
        # so there is nothing to copy and replacing the link would discard that setup
        # silently. Not a success: the caller asked for an install and did not get one.
        return AddonInstallResult(
            False,
            f"{target} is a symlink to {os.readlink(target)}, so Blender already loads that "
            "checkout directly and no copy was installed. Reload the addon in Blender to pick "
            f"up your edits, or delete the link first to install a copy of {source}.",
            addons_dir=str(addons_dir),
        )
    shutil.copytree(source, target)
    if str(target) not in replaced:
        replaced.append(str(target))

    msg = _install_message(target, addons_dir, replaced=replaced, linked=linked, stale_backups=stale_backups)

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


_MAX_CAPABILITY_PARAM_NAME_CHARS = 128
_MAX_CAPABILITY_PARAMS_PER_COMMAND = 64


def normalized_capability_params(value: object) -> dict[str, list[str] | str]:
    """
    Sanitize the handshake's per-command accepted-parameter-name map.

    Command names are a membership set the same way `capabilities` is - connection.py's gate
    compares them for exact equality against literal command names it already knows - so a
    name a cleaning pass would change is dropped rather than published cleaned, the same rule
    normalized_session_text_list applies to its own elements.

    Args:
        value: The raw `capability_params` field from get_addon_info, or anything else.

    Returns:
        dict[str, list[str] | str]: Command name to its accepted keyword names, or the literal
        "*" for a handler that accepts arbitrary keywords.

    """
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, list[str] | str] = {}
    for raw_command, params in value.items():
        command = normalized_session_text(raw_command, max_chars=_MAX_CAPABILITY_PARAM_NAME_CHARS)
        if command is None or not _is_structurally_intact(raw_command, command):
            continue
        if params == "*":
            cleaned[command] = "*"
            continue
        cleaned[command] = normalized_session_text_list(params, max_chars=_MAX_CAPABILITY_PARAM_NAME_CHARS)[
            :_MAX_CAPABILITY_PARAMS_PER_COMMAND
        ]
    return cleaned


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


@lru_cache(maxsize=1)
def load_addon_surface() -> dict[str, list[str] | str]:
    """
    Read the committed snapshot of the bundled addon's dispatch surface.

    Cached: the file never changes while the process runs, and `handshake_addon`
    is called on every reconnect. A missing or unreadable file degrades to an empty
    surface rather than raising - the snapshot only ever *adds* a staleness signal,
    so losing it must cost a warning, never a working connection. It should never be
    the normal path; the file ships in the wheel via pyproject's package-data.

    Returns:
        dict[str, list[str] | str]: Command name to its sorted accepted keyword
        names, or the ACCEPTS_ANY_KEYWORD sentinel `"*"` for a handler taking
        `**kwargs`. Empty when the snapshot is absent or malformed.

    """
    try:
        document = json.loads(ADDON_SURFACE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(f"Could not read the addon surface snapshot at {ADDON_SURFACE_PATH}: {e}")
        return {}
    commands = document.get("commands") if isinstance(document, dict) else None
    return commands if isinstance(commands, dict) else {}


def addon_surface_gap(
    capabilities: list[str], capability_params: dict[str, list[str] | str]
) -> tuple[list[str], dict[str, list[str]]]:
    """
    Diff what a live addon reports against what the committed snapshot expects.

    The comparison is one-directional on purpose. Commands the addon has and the
    snapshot does not are *not* staleness: that is a newer addon paired with an
    older server, plus the provider-gated handlers (polyhaven/sketchfab/nd) that the
    snapshot deliberately omits because they come and go with the open .blend's scene
    flags. Only the other direction - the snapshot expecting something the addon
    never advertised - means the install predates this server.

    Args:
        capabilities: Command names the addon advertised in its handshake.
        capability_params: Per-command accepted keyword names the addon advertised.

    Returns:
        tuple[list[str], dict[str, list[str]]]: Sorted missing command names, and
        per-command sorted missing keyword names for commands that do exist.

    """
    expected = load_addon_surface()
    advertised = set(capabilities)
    missing_commands = sorted(name for name in expected if name not in advertised)
    missing_parameters: dict[str, list[str]] = {}
    for name, parameters in expected.items():
        if name in missing_commands or not isinstance(parameters, list):
            continue
        reported = capability_params.get(name)
        # A non-list value is the ACCEPTS_ANY_KEYWORD sentinel, and an absent one is an
        # addon too old to publish capability_params at all. Neither enumerates keywords,
        # so there is nothing to subtract and guessing would invent a gap.
        if not isinstance(reported, list):
            continue
        gap = sorted(set(parameters) - set(reported))
        if gap:
            missing_parameters[name] = gap
    return missing_commands, missing_parameters


# A build that predates a whole toolset is short hundreds of commands, and this warning
# is read inside an agent's context window; the count carries the magnitude, the names
# only have to be recognizable enough to confirm the diagnosis.
_MAX_LISTED_MISSING = 5


def _capped(names: list[str]) -> str:
    """
    List a few names and count the rest.

    Args:
        names: The names to list, already in the order they should be read.

    Returns:
        str: Comma-separated names with a `, +N more` tail once they overflow.

    """
    overflow = len(names) - _MAX_LISTED_MISSING
    listed = ", ".join(names[:_MAX_LISTED_MISSING])
    return f"{listed}, +{overflow} more" if overflow > 0 else listed


def _surface_gap_warning(missing_commands: list[str], missing_parameters: dict[str, list[str]]) -> str:
    """
    Phrase the same-protocol staleness finding for an agent and the startup log.

    Args:
        missing_commands: Commands the snapshot expects and the addon never advertised.
        missing_parameters: Per-command keywords in the same position.

    Returns:
        str: A one-paragraph warning carrying `_UPDATE_HINT`.

    """
    shortfalls: list[str] = []
    if missing_commands:
        shortfalls.append(f"{len(missing_commands)} command(s) ({_capped(missing_commands)})")
    if missing_parameters:
        named = _capped(sorted(missing_parameters))
        shortfalls.append(f"parameters on {len(missing_parameters)} command(s) ({named})")
    return (
        f"Blender addon reports protocol {EXPECTED_ADDON_PROTOCOL_VERSION} but is missing "
        f"{' and '.join(shortfalls)} that this server ships. It was installed from an earlier "
        f"build carrying the same protocol number, so comparing version numbers cannot see it. "
        f"{_UPDATE_HINT}"
    )


def _is_transport_failure(error: BaseException) -> bool:
    """
    Report whether an exception means nothing was learned about the addon.

    `blender_mcp.server.connection` imports this module, so this module cannot import
    its exception classes back without an import cycle. `BlenderTransportError` therefore
    carries the class attribute `is_transport_failure`, and that flag - plus the builtin
    `ConnectionError` raised when there is no socket to send on at all - is the whole
    protocol between the two layers.

    Anything else reached the handshake because Blender answered and the answer was a
    refusal, which is a real finding about the installed addon.

    Args:
        error: The exception the handshake round trip raised.

    Returns:
        bool: True when the round trip never completed.

    """
    return isinstance(error, ConnectionError) or getattr(error, "is_transport_failure", False) is True


def handshake_addon(blender_connection) -> AddonHandshake:
    """
    Query a connected Blender addon for protocol version and dispatch surface.

    Old addons without get_addon_info are treated as outdated (but still usable
    through validated dedicated tools elsewhere).

    An addon whose protocol number already matches is checked a second way, against
    the committed `addon_surface.json`: a build from before a command was added
    carries the same protocol number as one from after it, and that equality is
    exactly what once let a missing `solve_bone_reach` read as a missing feature.

    Every handshake returned from here is something Blender said. A round trip that
    never completed is raised, not rendered: see `_is_transport_failure`.

    Args:
        blender_connection: Value for blender connection.

    Returns:
        AddonHandshake: What the addon reported about itself.

    Raises:
        Exception: If the round trip never completed - the transport's own error,
            re-raised unchanged, because nothing at all was learned about the addon.

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

        capabilities = normalized_session_text_list(info.get("capabilities"))
        capability_params = normalized_capability_params(info.get("capability_params"))
        missing_commands: list[str] = []
        missing_parameters: dict[str, list[str]] = {}
        # Only at exact equality. A protocol behind already has its warning above, and one
        # ahead means the addon is newer than this server, where the snapshot is the stale
        # party and anything it misses is not the user's problem to fix.
        if protocol_i == EXPECTED_ADDON_PROTOCOL_VERSION:
            missing_commands, missing_parameters = addon_surface_gap(capabilities, capability_params)
            if missing_commands or missing_parameters:
                up_to_date = False
                warning = _surface_gap_warning(missing_commands, missing_parameters)
        return AddonHandshake(
            up_to_date=up_to_date,
            protocol_version=protocol_i,
            # Every field is normalized: a raw one could carry newlines, ESC or
            # bidi characters into `get_addon_status` and the handshake log.
            addon_version=normalized_addon_version(info.get("addon_version")),
            capabilities=capabilities,
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
            capability_params=capability_params,
            missing_commands=missing_commands,
            missing_parameters=missing_parameters,
        )
    except Exception as e:
        if _is_transport_failure(e):
            # Not a version verdict: no protocol number, no capability list, not even
            # evidence that an addon is installed. Rendering this as a handshake put
            # `up_to_date=False`, `protocol_version=None` and an empty capability list
            # in front of an agent, which reads as "reinstall the addon" - the wrong
            # move when one socket simply went away. The caller reports the fault.
            raise
        # Everything below is Blender answering and refusing, which is a real finding.
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
