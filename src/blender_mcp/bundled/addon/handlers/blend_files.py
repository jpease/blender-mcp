"""
What every command that touches a `.blend` on disk shares: paths, flags, libraries, failures.

`file_lifecycle`, `linking`, `delivery` and `polyhaven` all open, save, link or
describe `.blend` files, and they used to reach into `file_lifecycle` for these
helpers by their private names. They live here instead, public, so no module
imports another handler module's internals to get them.

`published_path_fields` is the one of them every reply goes through to report a
path: it resolves the path, publishes it whole when it lies inside the allowed
folders, reduces anything else to a leaf, and says which it did and why, so a
client can tell a redaction from a broken link.

Nothing here decides whether a path is authorized: `file_paths.resolve_blend_path`
does, once. `checked_blend_path` only expands Blender's `//` form first, which
needs `bpy` and therefore cannot live in `file_paths`. Publication reuses the
same canonical form and containment test, but only to decide what a reply shows.
"""

import os

from dataclasses import dataclass

import bpy

from ..file_paths import (
    BLENDER_RELATIVE_PREFIX,
    canonical_path,
    inside_roots,
    resolve_blend_path,
    sanitize_blender_error,
)
from ..output_roots import configured_file_roots
from ..text_hygiene import (
    admissible_link_component,
    client_safe_leaf,
    client_safe_name_leaf,
    relative_link_body,
    strip_unsafe,
)

# Room for a project-relative link several directories deep, while keeping a
# hostile link from pushing kilobytes into an agent's context. The bound applies
# per path, and the library list itself is unbounded.
MAX_REPORTED_LINK_CHARS = 256

# Why a path was reduced to a leaf rather than published whole. Machine stable: a
# client branches on these rather than on prose, and on `None` when the path was
# published whole.
#
#   DIRECTORY         A directory not published whole. Even its leaf is withheld,
#                     because a directory's last component is routinely a user name.
#   UNRESOLVABLE      Nothing to resolve it against: a `//` path in a session never
#                     saved, `//` followed by a root, or a relative path without the
#                     `//` marker, which Blender resolves against its working directory.
#   OUTSIDE_ROOTS     Resolved, and not inside the configured file roots - or, with
#                     none configured, the open .blend's directory.
#   TOO_LONG          Inside, but its published form exceeds MAX_REPORTED_LINK_CHARS.
#   UNSAFE_COMPONENT  Inside, but a component of its published form carries a
#                     character outside `text_hygiene.LINK_COMPONENT_ALLOWED`.
REDACTION_DIRECTORY = "DIRECTORY"
REDACTION_UNRESOLVABLE = "UNRESOLVABLE"
REDACTION_OUTSIDE_ROOTS = "OUTSIDE_ROOTS"
REDACTION_TOO_LONG = "TOO_LONG"
REDACTION_UNSAFE_COMPONENT = "UNSAFE_COMPONENT"
PATH_REDACTION_REASONS = (
    REDACTION_DIRECTORY,
    REDACTION_UNRESOLVABLE,
    REDACTION_OUTSIDE_ROOTS,
    REDACTION_TOO_LONG,
    REDACTION_UNSAFE_COMPONENT,
)


@dataclass(frozen=True, slots=True)
class PathFrame:
    """
    What publishing a path is decided against, gathered once for a batch of paths.

    Every field is in `file_paths.canonical_path` form, so a batch pays one
    `realpath` per root and per directory, and each path then costs its own one.

    Attributes:
        blend_directory: The open .blend's directory, which a `//` path expands
            against; "" in a session never saved.
        link_directory: The directory a published `//` link is relative to: the
            open .blend's, or the one a save is about to write into; "" publishes
            a contained path absolute.
        trees: Where a path must lie to be published whole: the configured file
            roots, or with none configured `link_directory`; empty when there is
            neither, so that nothing is.

    """

    blend_directory: str
    link_directory: str
    trees: tuple[str, ...]


def _canonical_directory(filepath: str) -> str:
    """
    Name the directory a `.blend` lives in, in the form containment is decided on.

    Args:
        filepath: An absolute `.blend` path, or "" for none.

    Returns:
        str: Its canonical directory, or "" when there is no file.

    """
    return canonical_path(os.path.dirname(filepath)) if filepath else ""


def path_frame(blend_filepath: str | None = None) -> PathFrame:
    """
    Gather the frame paths are published in, for the open .blend or one about to be written.

    Args:
        blend_filepath: The `.blend` published links are to be relative to,
            for a record written into a file a save has not yet moved to; None
            for the open file. A `//` path still expands against the open file,
            because that is what Blender resolves it against until the save.

    Returns:
        PathFrame: The canonical directories and trees.

    """
    blend_directory = _canonical_directory(str(bpy.data.filepath or ""))
    link_directory = blend_directory if blend_filepath is None else _canonical_directory(blend_filepath)
    roots = tuple(canonical_path(root) for root in configured_file_roots())
    return PathFrame(blend_directory, link_directory, roots or ((link_directory,) if link_directory else ()))


def _resolved(text: str, blend_directory: str) -> str | None:
    """
    Place a path Blender reported on this machine, or say it cannot be placed without guessing.

    A `//` path expands against the open .blend and an absolute one stands
    alone; anything else would resolve against Blender's working directory,
    which the file never named. `..` is collapsed by spelling first, as Blender
    collapses it before it opens a library, and `realpath` then follows
    symlinks, so a link that leads out of a root is judged by where it leads,
    as `open_shot`'s root check judges it.

    Args:
        text: The path as Blender reported it, unstripped: a stray character is
            part of the name Blender opens.
        blend_directory: `PathFrame.blend_directory`.

    Returns:
        str | None: The path in `canonical_path` form, or None when it cannot be
        resolved.

    """
    if text.startswith(BLENDER_RELATIVE_PREFIX):
        body = relative_link_body(text)
        if body is None or not blend_directory:
            return None
        text = os.path.join(blend_directory, body)
    elif not os.path.isabs(text):
        return None
    try:
        return canonical_path(os.path.normpath(text))
    except (OSError, ValueError):  # an embedded NUL, which no file system accepts
        return None


def _whole(candidate: str, text: str, link_directory: str) -> tuple[str, list[str]]:
    """
    Spell a contained path the way it is published, with the components the gate must check.

    Relative to `link_directory`, as a `//` link with `/` separators whatever
    spelling Blender stored; `..` steps are kept, because containment is
    already proven, so they climb only toward a configured root. With no
    `link_directory` - a session never saved, whose `current_filepath` is None -
    or no relative form at all (another Windows drive), the canonical absolute
    path is published instead: it lies inside a configured root, which the
    handshake already reports, and it is the form `current_filepath` uses.

    A trailing separator is kept, because Blender reads a render output ending
    in one as a directory.

    Args:
        candidate: The contained path, in `canonical_path` form.
        text: The path as Blender reported it.
        link_directory: `PathFrame.link_directory`.

    Returns:
        tuple[str, list[str]]: The published string and its components, which
        are every character of it besides the prefix and the separators.

    """
    trailing = text.endswith(("/", os.sep))
    relative = None
    if link_directory:
        try:
            relative = os.path.relpath(candidate, link_directory)
        except ValueError:  # another drive on Windows
            relative = None
    if relative is not None:
        parts = [] if relative == os.curdir else relative.split(os.sep)
        return BLENDER_RELATIVE_PREFIX + "/".join(parts) + ("/" if trailing and parts else ""), parts
    drive, rest = os.path.splitdrive(candidate)
    parts = [part for part in rest.split(os.sep) if part]
    return drive + os.sep + os.sep.join(parts) + (os.sep if trailing and parts else ""), parts


def _publication(text: str, frame: PathFrame) -> tuple[str | None, str | None]:
    """
    Decide, in one pass, what a path publishes as or why it is withheld.

    One pass, so the value and the reason cannot disagree: each refusal returns
    the code that names it, in the order the checks run.

    Args:
        text: The path as Blender reported it.
        frame: The frame to decide in.

    Returns:
        tuple[str | None, str | None]: The whole published form and None, or
        None and the reason it was withheld.

    """
    candidate = _resolved(text, frame.blend_directory)
    if candidate is None:
        return None, REDACTION_UNRESOLVABLE
    # `inside_roots` reads empty roots as "allow all"; here no tree means nothing is inside.
    if not frame.trees or not inside_roots(candidate, frame.trees):
        return None, REDACTION_OUTSIDE_ROOTS
    whole, parts = _whole(candidate, text, frame.link_directory)
    if len(whole) > MAX_REPORTED_LINK_CHARS:
        return None, REDACTION_TOO_LONG
    if not all(admissible_link_component(part) for part in parts):
        return None, REDACTION_UNSAFE_COMPONENT
    return whole, None


def published_path_fields(
    raw: object,
    *,
    key: str = "filepath",
    is_directory: bool = False,
    blank_is_unset: bool = False,
    frame: PathFrame | None = None,
) -> dict[str, object]:
    r"""
    Publish one path together with the two fields that say whether, and why, it was reduced.

    The single place any reply turns a path Blender reported into something a
    client may read. The path is resolved, not read by its spelling: a `//`
    path expands against the open .blend, and both forms are canonicalized, so
    `..` and symlinks are followed. A path that lands inside the allowed
    folders - the configured file roots, or with none configured the open
    .blend's directory - is published whole, re-derived as a `//` link relative
    to the .blend whatever spelling Blender stored: an absolute
    `<shot>/libs/canon.blend` publishes as `//libs/canon.blend`, and a canon
    folder beside the shot, inside a root, as `//../canon/set.blend`. In a
    session never saved there is no .blend to be relative to, so a path inside a
    configured root is published absolute, as `current_filepath` is. Anything
    else is reduced to its leaf, because the directories above it are this
    host's storage layout.

    A reduced path is the reason this returns three fields rather than one: on
    its own, `"canon.blend"` is indistinguishable from a broken or malformed
    link, so `<key>_redacted` states that the value is a display leaf and
    `<key>_redaction_reason` names the rule, one of `PATH_REDACTION_REASONS`.
    Neither reports breakage; the summary's own `is_missing` does.

    Containment is decided by spelling once both sides are canonical, as
    `file_paths.inside_roots` decides it, without the `stat` walk
    `enforce_roots` adds for a case-insensitive volume: a path spelled in other
    case than its root is withheld as `OUTSIDE_ROOTS`, the safe direction. Each
    path costs one `realpath`, and gathering a frame one per root and per
    directory, so a caller publishing a batch passes one `frame`.

    Args:
        raw: The path as Blender reported it.
        key: The name of the path field, which both flags are suffixed onto.
        is_directory: True when the caller established the path names a
            directory, whose leaf is routinely a user name and is never
            published; a contained directory is still published whole.
        blank_is_unset: True where "no path set" is a real state of the source,
            such as an unconfigured render output; a blank path is then
            published as `""` rather than reduced.
        frame: The frame to decide in; None gathers one for the open .blend.

    Returns:
        dict[str, object]: `<key>` (whole or a leaf), `<key>_redacted`, and
        `<key>_redaction_reason` (one of `PATH_REDACTION_REASONS`, or None when
        nothing was reduced).

    """
    text = str(raw or "")
    if blank_is_unset and not text.strip():
        return {key: "", f"{key}_redacted": False, f"{key}_redaction_reason": None}
    whole, reason = _publication(text, frame or path_frame())
    if whole is not None:
        return {key: whole, f"{key}_redacted": False, f"{key}_redaction_reason": None}
    return {
        key: client_safe_leaf(text, is_directory=is_directory),
        f"{key}_redacted": True,
        f"{key}_redaction_reason": REDACTION_DIRECTORY if is_directory else reason,
    }


def library_summary(library: object, *, frame: PathFrame | None = None) -> dict[str, object]:
    r"""
    Describe one linked library by identity, not by where it sits on this machine.

    `published_path_fields` decides whether `filepath` is published whole or
    reduced to a leaf, and says which happened: a library that resolves inside
    the configured file roots (with none configured, inside the open .blend's
    directory) is published as a `//` link, however Blender spelled it; one
    outside them, or one that cannot be resolved, is reported as
    `filepath_redacted: true` with a `filepath_redaction_reason`, because the
    path would reveal the studio's storage layout.

    Both verdicts are judged on the path Blender stored, not the published one:
    `is_relative` is false for an absolute library even when it is published as
    a `//` link or a leaf, and `is_missing` - not the redaction - is what
    reports a link that does not resolve.

    `name` is reduced to a leaf too: Blender lets a `.blend` author set
    `Library.name` to a path such as `/Users/victim/shots/canon.blend`. The name
    is never stat'ed: it is author-chosen text, and probing it would reveal
    whether a directory by that name exists on this machine. `filepath` costs
    one `realpath`, whose only visible outcome is the containment verdict.

    Args:
        library: A `bpy.types.Library`.
        frame: Passed through to `published_path_fields`.

    Returns:
        dict[str, object]: `session_uid`, the handle the linking commands
        resolve by, because two libraries' contents can share a name. It is
        valid only for the `(session_id, session_epoch)` it was read under:
        despite Blender's own description, it changes on every load. `name`,
        for display only; `filepath`, whole or reduced to a leaf, with
        `filepath_redacted` and `filepath_redaction_reason` saying which;
        `is_relative`; and `is_missing`, true when the link is broken now.

    """
    filepath = str(getattr(library, "filepath", "") or "")
    return {
        "session_uid": getattr(library, "session_uid", None),
        "name": client_safe_name_leaf(getattr(library, "name", "")),
        **published_path_fields(filepath, frame=frame),
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
