"""
The filesystem trust boundary for `.blend` paths that arrive over the socket.

The socket is unauthenticated, so any command that opens, saves or links a
`.blend` offers arbitrary-path read and overwrite. This module decides whether
such a path is acceptable, and removes paths from Blender's error text before it
reaches a client.

- Roots: `BLENDERMCP_FILE_ROOTS` (os.pathsep-separated), or
  `BLENDERMCP_OUTPUT_ROOTS` when that is unset or blank, names the directories file
  commands may touch. With neither set, every path is allowed: a local artist's
  Blender behind a loopback socket should just work, and a shared deployment sets
  the variable. The handshake reports the mode (`file_roots`,
  `file_roots_enforced`). Enforced roots never fall back to the advisory
  `writable_output_roots` defaults, or the home directory would be a root.
- Overwrite: writing over an existing file needs `confirm_overwrite=True`, which
  the `save_shot` handler checks.
- Directories: a save target's missing directory is refused unless the caller
  opts in, and `create_save_directory` makes it only after the roots check.
- Scripts: `wm.open_mainfile` can run Python embedded in a `.blend`. Every call
  passes `use_scripts=False`, and the handler's `_refuse_scripts_auto_execute`
  checks the auto-execute preference, which needs `bpy`.

Free of `bpy` so it can be tested without Blender; the caller expands Blender's
`//` prefix before `resolve_blend_path`.
"""

import contextlib
import os
import re

from collections.abc import Iterable

from .text_hygiene import client_safe_name_leaf

# Prefixes, and their lengths matter: a longer prefix rejects valid files.
# Every header version starts with `BLENDER`, and gzip's fourth byte varies, so
# only its two magic bytes are checked.
BLEND_MAGIC_UNCOMPRESSED = b"BLENDER"
BLEND_MAGIC_ZSTD = b"\x28\xb5\x2f\xfd"
BLEND_MAGIC_GZIP = b"\x1f\x8b"
BLEND_MAGIC_PREFIXES = (BLEND_MAGIC_UNCOMPRESSED, BLEND_MAGIC_ZSTD, BLEND_MAGIC_GZIP)

BLEND_SUFFIX = ".blend"
BLENDER_RELATIVE_PREFIX = "//"
PATH_PLACEHOLDER = "<path>"

# Where a path may start: POSIX root, UNC, `~` or `~user` home, drive letter.
_PATH_START = r"(?:/|\\\\|~[\w.-]*[/\\]|[A-Za-z]:[\\/])"
# Punctuation that may close a path without being part of it.
_TRAILING = r"(?=[:;,?!)\]>]*(?:\s|$))"
# One pass, so a quoted path is consumed before its interior can match again. A
# quoted path ends only at a quote that ends a token, so an apostrophe inside it
# does not cut it short. A bare path takes later words while they lead to one
# containing a separator (a directory name with a space), but never past a word
# ending in `:`, `;` or `,`, because the cause follows and may hold slashes too
# (`Input/output error`). Detection alone misses some paths with spaces or
# punctuation inside, so every caller that knows its path must pass it to
# `sanitize_blender_error`.
_PATH_IN_TEXT = re.compile(
    rf"""(?P<quote>["'])(?P<quoted>{_PATH_START}.*?)(?P=quote)(?=$|[\s:;,.?!)\]>])"""
    rf"""|(?<![^\s"'(\[=,])(?P<bare>{_PATH_START}(?=\S)\S*?{_TRAILING}"""
    rf"""(?:(?:\s+\S*[^\s:;,])*?\s+\S*[/\\]\S*?{_TRAILING})*)""",
    re.DOTALL,
)
# `Library.reload()` names the library by its raw ID name, type code included.
_LIBRARY_ID_NAME = re.compile(r"(library ')LI")
# Any quoted library name, which many Blender messages include. A `.blend` author
# can set `Library.name` to an absolute path, and the `LI` code in front hides it
# from path detection, so the name is reduced to its leaf first. DOTALL because a
# name may hold a newline.
_QUOTED_LIBRARY_NAME = re.compile(
    r"(?P<lead>[Ll]ibrary ')(?P<code>LI)?(?P<name>.*?)(?='(?:$|[\s:;,.?!)\]>]))", re.DOTALL
)


def canonical_path(path: str) -> str:
    """
    Reduce a path to the one form containment is decided on.

    `expanduser` makes `~` mean home rather than a directory named `~`.
    `realpath` resolves `..` and symlinks, so a link inside a root cannot point
    outside it.

    Args:
        path: A path string, already expanded from Blender's `//` form.

    Returns:
        str: The absolute, symlink-free path.

    """
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def _has_blend_suffix(path: str) -> bool:
    """
    Report whether a path's leaf is a named `.blend`, case-insensitively.

    The `endswith` test also refuses `x.blend.` and `x.blend `, which Windows
    and macOS resolve differently.

    Args:
        path: The path to check.

    Returns:
        bool: True for `name.blend` in any case.

    """
    leaf = os.path.basename(path)
    return leaf.lower().endswith(BLEND_SUFFIX) and len(leaf) > len(BLEND_SUFFIX)


def _require_blend_file(path: str) -> None:
    """
    Refuse a path that is not an existing, readable file with a `.blend` header.

    Args:
        path: A canonical path.

    Raises:
        ValueError: With a message that names no path.

    """
    if os.path.isdir(path):
        raise ValueError("path is a directory, not a .blend file")
    if not os.path.isfile(path):
        raise ValueError("file does not exist")
    try:
        with open(path, "rb") as handle:
            header = handle.read(max(len(prefix) for prefix in BLEND_MAGIC_PREFIXES))
    except OSError as exc:
        # OSError's own text carries the path, so it is chained, not quoted.
        raise ValueError("file could not be read") from exc
    if not header.startswith(BLEND_MAGIC_PREFIXES):
        raise ValueError("file is not a .blend file (unrecognised header)")


def _require_save_target(path: str, *, create_directories: bool) -> None:
    """
    Refuse a save target whose directory is missing (unless it may be created) or unwritable.

    Args:
        path: A canonical path.
        create_directories: True when the caller will create a missing directory
            with `create_save_directory`; its writability is then Blender's to report.

    Raises:
        ValueError: With a message that names no path.

    """
    if os.path.isdir(path):
        raise ValueError("path is a directory, not a .blend file")
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        if create_directories:
            return
        raise ValueError("target directory does not exist; pass create_directories=true to create it")
    if not os.access(directory, os.W_OK):
        raise ValueError("target directory is not writable")


def resolve_blend_path(raw: object, *, must_exist: bool, create_directories: bool = False) -> str:
    """
    Validate a caller-supplied `.blend` path and return its canonical form.

    Refusals give the reason, never a path: echoing the resolved form would reveal
    where a symlink or `~` led.

    Args:
        raw: The path, already passed through `bpy.path.abspath` by the caller.
        must_exist: True to open or link (the file must exist and carry a
            `.blend` header); False to save (its directory must be writable).
        create_directories: For a save, accept a directory that does not exist
            yet; the caller creates it with `create_save_directory`.

    Returns:
        str: The canonical path, to check with `enforce_roots` and hand to Blender.

    Callers must refuse a `//` path when no .blend is open: `bpy.path.abspath`
    then leaves it relative, and it would resolve against the working directory.
    `handlers/file_lifecycle._expand_blender_relative` does this.

    Raises:
        ValueError: If the path is refused.

    """
    if not isinstance(raw, str):
        raise ValueError("path must be a string")
    if not raw.strip():
        raise ValueError("path must not be empty")
    if "\x00" in raw:
        raise ValueError("path must not contain a NUL byte")
    if raw.startswith(BLENDER_RELATIVE_PREFIX):
        raise ValueError("a Blender-relative path must be expanded by the caller before it is resolved")
    resolved = canonical_path(raw)
    if not (_has_blend_suffix(raw) and _has_blend_suffix(resolved)):
        raise ValueError("path must name a file ending in .blend")
    if must_exist:
        _require_blend_file(resolved)
    else:
        _require_save_target(resolved, create_directories=create_directories)
    return resolved


def create_save_directory(path: str) -> bool:
    """
    Create a canonical save target's missing directory, with its missing parents.

    Call it only with `resolve_blend_path`'s result, after `enforce_roots`. That
    path is symlink-free, so every directory made is inside the root, unless
    another local process swaps an ancestor for a symlink in between; trusted
    deployments accept that window.

    Runs on Blender's main thread, so a dead network mount stalls every client
    until the mount times out.

    Args:
        path: The canonical `.blend` target.

    Returns:
        bool: True when a directory was created, False when it already existed.

    Raises:
        ValueError: When it cannot be created (a file is in the way, or a parent
            is unwritable), with a message that names no path.

    """
    directory = os.path.dirname(path)
    if os.path.isdir(directory):
        return False
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        # OSError's own text carries the path, so it is chained, not quoted.
        raise ValueError("target directory could not be created") from exc
    return True


def enforce_roots(path: str, roots: Iterable[str]) -> None:
    """
    Refuse a path outside every configured root; with no roots, allow all.

    Uses `os.path.commonpath` on canonical forms: a string prefix test accepts
    `/output-evil` for root `/output`, and unresolved forms let a symlink inside a
    root point anywhere. The refusal names neither roots nor path, since the
    handshake already reports the roots.

    Args:
        path: The path to check, normally `resolve_blend_path`'s result.
        roots: Configured roots; empty means the permissive default.

    Raises:
        ValueError: If roots are configured and none contains the path.

    """
    roots = list(roots)
    if not roots:
        return
    candidate = canonical_path(path)
    for root in roots:
        canonical_root = canonical_path(root)
        try:
            if os.path.commonpath((canonical_root, candidate)) == canonical_root:
                return
        except ValueError:
            pass  # different drives on Windows: not contained by spelling
        if _has_ancestor_directory(candidate, canonical_root):
            return
    raise ValueError(
        "path is outside the allowed file roots (BLENDERMCP_FILE_ROOTS); see file_roots in get_addon_status"
    )


def _has_ancestor_directory(candidate: str, root: str) -> bool:
    """
    Report whether some ancestor of `candidate` is the same directory as `root`.

    `realpath` does not fold case, so on a case-insensitive volume such as APFS
    `/X/output/x.blend` and root `/X/Output` name one directory in two spellings.
    Comparing device and inode settles it, and cannot admit a link out of the
    root because both sides are already symlink-free.

    Args:
        candidate: A canonical path, which need not exist.
        root: A canonical root.

    Returns:
        bool: True when an existing ancestor is the root directory itself.

    """
    try:
        root_stat = os.stat(root)
    except OSError:
        return False
    ancestor = os.path.dirname(candidate)
    while True:
        with contextlib.suppress(OSError):
            if os.path.samestat(os.stat(ancestor), root_stat):
                return True
        parent = os.path.dirname(ancestor)
        if parent == ancestor:
            return False
        ancestor = parent


def sanitize_blender_error(exc: BaseException, known_paths: Iterable[str] = ()) -> str:
    """
    Remove every filesystem path from an exception's text, keeping its cause.

    Blender's paths in error text may be quoted or bare, repeated, suffixed with
    `@` for a temp file, or the working directory, and need not match what the
    caller sent. So paths are found by shape and replaced with `<path>`. Paths the
    caller knows are replaced first, whole, because only they show where a name
    with spaces ends.

    Args:
        exc: The exception raised by an operator or data-API call.
        known_paths: Paths the call was given (raw and canonical forms).

    Returns:
        str: The message with paths replaced, or the exception's type name when
        it carries no text.

    """
    raw = str(exc).strip()
    if not raw:
        return type(exc).__name__
    text = raw
    for known in sorted(_with_temp_names(known_paths), key=len, reverse=True):
        text = text.replace(known, PATH_PLACEHOLDER)
    text = _QUOTED_LIBRARY_NAME.sub(_leaf_library_name, text)
    text = _PATH_IN_TEXT.sub(_placeholder_for, text)
    return _LIBRARY_ID_NAME.sub(r"\1", text)


def _leaf_library_name(match: re.Match[str]) -> str:
    """
    Reduce one quoted library name to an admissible leaf, without a filesystem call.

    Uses the same leaf rule as `_library_summary`, which does not stat the
    author-chosen name. The `LI` code is kept for the final strip.

    Args:
        match: A `_QUOTED_LIBRARY_NAME` match.

    Returns:
        str: The quote lead, the code if present, and the leaf.

    """
    return f"{match['lead']}{match['code'] or ''}{client_safe_name_leaf(match['name'])}"


def _with_temp_names(paths: Iterable[str]) -> set[str]:
    """
    Expand known paths with the `@` temp-write names Blender derives from them.

    Args:
        paths: Caller-known paths; non-strings and blanks are ignored.

    Returns:
        set[str]: Each path and the same path with `@` appended.

    """
    usable = {path for path in paths if isinstance(path, str) and path.strip()}
    return usable | {f"{path}@" for path in usable}


def _placeholder_for(match: re.Match[str]) -> str:
    """
    Replace one detected path, keeping its quotes so the sentence still reads.

    Args:
        match: A `_PATH_IN_TEXT` match.

    Returns:
        str: The placeholder, re-quoted when the path was quoted.

    """
    quote = match["quote"] or ""
    return f"{quote}{PATH_PLACEHOLDER}{quote}"
