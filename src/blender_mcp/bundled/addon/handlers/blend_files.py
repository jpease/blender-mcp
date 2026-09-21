"""
What every command that touches a `.blend` on disk shares: paths, flags, libraries, failures.

`file_lifecycle`, `linking`, `delivery` and `polyhaven` all open, save, link or
describe `.blend` files, and they used to reach into `file_lifecycle` for these
six helpers by their private names. They live here instead, public, so no module
imports another handler module's internals to get them.

Nothing here decides whether a path is authorized: `file_paths.resolve_blend_path`
does, once. `checked_blend_path` only expands Blender's `//` form first, which
needs `bpy` and therefore cannot live in `file_paths`.
"""

import bpy

from ..file_paths import BLENDER_RELATIVE_PREFIX, resolve_blend_path, sanitize_blender_error
from ..output_roots import configured_file_roots
from ..text_hygiene import client_safe_leaf, client_safe_name_leaf, relative_link_body, safe_relative_link, strip_unsafe

# Room for a project-relative link several directories deep, while keeping a
# hostile link from pushing kilobytes into an agent's context. The bound applies
# per library, and the library list itself is unbounded.
MAX_REPORTED_LINK_CHARS = 256


def library_summary(library: object) -> dict[str, object]:
    r"""
    Describe one linked library by identity, not by where it sits on this machine.

    `text_hygiene.safe_relative_link` decides whether `filepath` is published
    whole, exactly as it returned it. Anything it refuses (absolute, rooted
    `///...`, traversing, over-long, or with a character outside its allowlist)
    is reduced to a leaf, because it would reveal the studio's storage layout.
    `is_relative` is judged on the same stripped form, so `///Users/...` is not
    reported as relative.

    `name` is reduced to a leaf too: Blender lets a `.blend` author set
    `Library.name` to a path such as `/Users/victim/shots/canon.blend`. Neither
    field is stat'ed: both are author-chosen text, and probing one would reveal
    whether a directory by that name exists on this machine.

    Args:
        library: A `bpy.types.Library`.

    Returns:
        dict[str, object]: `session_uid`, the handle the linking commands
        resolve by, because two libraries' contents can share a name. It is
        valid only for the `(session_id, session_epoch)` it was read under:
        despite Blender's own description, it changes on every load. `name`,
        for display only; `filepath`, whole or reduced to a leaf; `is_relative`;
        and `is_missing`, true when the link is broken now.

    """
    filepath = str(getattr(library, "filepath", "") or "")
    whole = safe_relative_link(filepath, MAX_REPORTED_LINK_CHARS)
    return {
        "session_uid": getattr(library, "session_uid", None),
        "name": client_safe_name_leaf(getattr(library, "name", "")),
        "filepath": whole if whole is not None else client_safe_leaf(filepath),
        "is_relative": relative_link_body(strip_unsafe(filepath)) is not None,
        "is_missing": bool(getattr(library, "is_missing", False)),
    }


def is_indirect_library(library: object) -> bool:
    """
    Report whether a library is only reached through another library.

    `Library.parent` is set while the link is made, but after a reopen it is None
    whenever the main file's own entry for the library resolves, because Blender
    then re-reads it as direct; every user of an indirect library carries
    `is_library_indirect` either way. Blender resolves an indirect library
    through its parent rather than from the main file's entry, so its `//` path
    survives the main file moving.

    Args:
        library: A `bpy.types.Library`.

    Returns:
        bool: True when it has users and all of them are indirect.

    """
    users = list(getattr(library, "users_id", ()) or ())
    return bool(users) and all(getattr(user, "is_library_indirect", False) for user in users)


def require_bool(name: str, value: object) -> bool:
    """
    Refuse a flag that is not a real `bool`.

    `"false"` is truthy, so a JSON client that sends a string would otherwise
    confirm an overwrite it meant to decline.

    Args:
        name: The parameter name, for the message.
        value: What the client sent.

    Returns:
        bool: The value, unchanged.

    Raises:
        ValueError: If it is not a `bool`.

    """
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def _expand_blender_relative(raw: object) -> object:
    """
    Expand a `//` path against the open file, refusing one that has nothing to be relative to.

    With no file open, `bpy.path.abspath` leaves a `//` path relative, and it
    would resolve against the working directory, which the caller never named.

    Args:
        raw: The path the client sent; non-strings pass through to be refused
            by `resolve_blend_path`.

    Returns:
        object: The expanded path, or `raw` unchanged when it is not `//`-relative.

    Raises:
        ValueError: For a `//` path in a session that has never been saved.

    """
    if not (isinstance(raw, str) and raw.startswith(BLENDER_RELATIVE_PREFIX)):
        return raw
    if not bpy.data.filepath:
        raise ValueError(
            "a Blender-relative path (one starting with a double slash) is relative to the open .blend, "
            "and this session has never been saved; pass an absolute path"
        )
    return bpy.path.abspath(raw)


def checked_blend_path(raw: object, *, must_exist: bool, create_directories: bool = False) -> str:
    """
    Expand a client's `//` path and hand it to the deployment's configured file roots.

    The Blender half of the trust boundary: everything after the expansion,
    including the refusal order, belongs to `resolve_blend_path`.

    Args:
        raw: The path the client sent.
        must_exist: True to open, False to save.
        create_directories: For a save, accept a missing target directory.

    Returns:
        str: The canonical path to hand to Blender. A refusal propagates as the
        `ValueError` the refusing check raised, whose message names no path.

    """
    return resolve_blend_path(
        _expand_blender_relative(raw),
        roots=configured_file_roots(),
        must_exist=must_exist,
        create_directories=create_directories,
    )


def refuse_scripts_auto_execute(command: str = "open_shot") -> None:
    """
    Refuse a load while Blender is set to run scripts embedded in a `.blend`.

    The linking commands and the Poly Haven `.blend` import call it too. It does
    not fully control script execution: Blender runs drivers based on the
    session's trust flag (`-y`, or a load with `use_scripts=True`), which this
    preference does not reflect. Trusted deployments accept that; an untrusted
    one would need to detect the flag, which is not implemented.

    Refused rather than warned about, and stricter than unset file roots: the
    preference defaults to off, so finding it on means someone enabled it. An
    unreadable preference counts as on.

    Args:
        command: The refusing command, named in the message.

    Raises:
        ValueError: When the preference is on or unreadable.

    """
    filepaths = getattr(getattr(bpy.context, "preferences", None), "filepaths", None)
    if getattr(filepaths, "use_scripts_auto_execute", True) is not False:
        raise ValueError(
            f"{command} refuses to load while Blender's preferences.filepaths.use_scripts_auto_execute "
            "is on (or unreadable), because a .blend could run embedded scripts; turn it off in "
            "Preferences > Save & Load, then retry"
        )


def operator_failure_message(command: str, exc: BaseException, known_paths: tuple[object, ...]) -> str:
    """
    Build the client-facing message for a file operator that raised.

    Blender's file operators raise `RuntimeError` with the absolute path in the
    text, sometimes twice. The raw exception stays chained for Blender's console;
    the client gets only the sanitized text.

    Args:
        command: The command name, for the message.
        exc: What the operator raised.
        known_paths: Every form of the path the call held.

    Returns:
        str: `"<command> failed: <sanitized text>"`.

    """
    known = tuple(path for path in known_paths if isinstance(path, str))
    return f"{command} failed: {sanitize_blender_error(exc, known_paths=known)}"
