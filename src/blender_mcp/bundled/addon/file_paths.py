"""
The filesystem trust boundary for `.blend` paths that arrive over the socket.

The socket is unauthenticated, so a command that opens, saves or links a
`.blend` hands whoever can reach it arbitrary-path read and overwrite. This
module is the one place that decides whether such a path is acceptable, and the
one place that removes paths from Blender's own error text before it reaches a
client.

**Policy.**

- *Roots.* `BLENDERMCP_FILE_ROOTS` (os.pathsep-separated) names the directories
  file commands may touch; when it is unset or blank, `BLENDERMCP_OUTPUT_ROOTS`
  is used instead (`output_roots.configured_file_roots`). **When neither yields
  a root the boundary is permissive**: the local-artist case is a GUI Blender
  on the user's own machine behind a loopback socket, and deny-by-default there
  bricks ordinary use for no gain. A pooled or container deployment sets the
  variable and gets enforcement. The handshake publishes which mode is active
  (`file_roots`, `file_roots_enforced`) so the asymmetry is observable.
  Enforced roots never come from the advisory `writable_output_roots` defaults
  (`~`, temp dirs), or the boundary would be the home directory.
- *Overwrite.* Writing over an existing file needs an explicit
  `confirm_overwrite=True`, default False, as `handlers/rendering.py` does.
  The save handler (plan Task 6) applies it; nothing here writes.
- *Scripts.* `wm.open_mainfile(use_scripts=...)` runs Python embedded in a
  `.blend` on load. It is never a tool parameter, every call passes
  `use_scripts=False` explicitly, and
  `preferences.filepaths.use_scripts_auto_execute` is part of the same
  code-execution surface and must be checked before a load. That runtime check
  needs `bpy`, so it lives in the handler (plan Task 6 Step 4b), not here.

Free of `bpy` so it is testable without Blender: Blender's `//` prefix is
expanded by the caller with `bpy.path.abspath` before `resolve_blend_path`.
"""

import contextlib
import os
import re

from collections.abc import Iterable

# Each constant is a *prefix*, and the lengths are load-bearing: pinning more
# bytes false-rejects valid files. `BLENDER` is what every header version
# shares (5.x writes `BLENDER17-01v050`, older ones `BLENDER-v293`); gzip's FLG
# byte (the 4th) varies, so only its two magic bytes are tested.
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
# One pass, so every occurrence is replaced and a quoted path is consumed
# before its interior can be matched again. A quoted path ends at its quote
# only when that quote ends a token, which keeps an apostrophe inside a
# `'...'` path from cutting it short. A bare path takes later words while they
# lead to a word containing a separator (a directory name with a space), but
# never past a word ending in `:`, `;` or `,`: after that comes the cause, and
# causes contain slashes too (`Input/output error`). Its trailing punctuation
# is left as text. Structural detection alone leaves a relative tail when a
# component contains a space in the last component, `, `, `: ` or `; ` in any
# component, or `')` / `'>` / `"?` inside a quoted name (measured on real 5.2.2
# messages); passing the known path to `sanitize_blender_error` closes all of
# them, so every caller that knows its path must pass it.
_PATH_IN_TEXT = re.compile(
    rf"""(?P<quote>["'])(?P<quoted>{_PATH_START}.*?)(?P=quote)(?=$|[\s:;,.?!)\]>])"""
    rf"""|(?<![^\s"'(\[=,])(?P<bare>{_PATH_START}(?=\S)\S*?{_TRAILING}"""
    rf"""(?:(?:\s+\S*[^\s:;,])*?\s+\S*[/\\]\S*?{_TRAILING})*)""",
    re.DOTALL,
)
# `Library.reload()` names the library by its raw ID name, type code included.
_LIBRARY_ID_NAME = re.compile(r"(library ')LI")


def canonical_path(path: str) -> str:
    """
    Reduce a path to the one form containment is decided on.

    `expanduser` makes `~` mean home rather than a directory named `~`.
    `abspath` mirrors `handlers/rendering.py`'s prior art for the bare relative
    path `bpy.path.abspath` hands through unchanged; CPython 3.13's `realpath`
    also absolutizes (measured: dropping `abspath` alone fails no test), so it
    is the pipeline's stated order, not a second defence. `realpath` resolves
    `..` (which `bpy.path.abspath('//../x')` preserves) and symlinks, which is
    what stops a link inside a root from pointing outside it.

    Args:
        path: A path string, already expanded from Blender's `//` form.

    Returns:
        str: The absolute, symlink-free path.

    """
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def _has_blend_suffix(path: str) -> bool:
    """
    Report whether a path's leaf is a named `.blend`, case-insensitively.

    A plain `endswith` also refuses `x.blend.` and `x.blend `, which Windows
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


def _require_save_target(path: str) -> None:
    """
    Refuse a save target whose directory is missing or unwritable.

    Args:
        path: A canonical path.

    Raises:
        ValueError: With a message that names no path.

    """
    if os.path.isdir(path):
        raise ValueError("path is a directory, not a .blend file")
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        raise ValueError("target directory does not exist")
    if not os.access(directory, os.W_OK):
        raise ValueError("target directory is not writable")


def resolve_blend_path(raw: object, *, must_exist: bool) -> str:
    """
    Validate a caller-supplied `.blend` path and return its canonical form.

    Refusals name the reason and never a path: the caller knows what it sent,
    and echoing a resolved form would disclose where a symlink or `~` led.

    Args:
        raw: The path, already passed through `bpy.path.abspath` by the caller.
        must_exist: True to open or link (the file must exist and carry a
            `.blend` header); False to save (its directory must be writable).

    Returns:
        str: The canonical path, to check with `enforce_roots` and hand to Blender.

    Callers must refuse a `//` path when `bpy.data.filepath` is empty: then
    `bpy.path.abspath('//x.blend')` returns the relative `'x.blend'` (measured,
    `scripts/blender_probes/file_path_error_shapes.py`), which would resolve
    against the process CWD. Plan Task 6 enforces it.

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
        _require_save_target(resolved)
    return resolved


def enforce_roots(path: str, roots: Iterable[str]) -> None:
    """
    Refuse a path outside every configured root; with no roots, allow all.

    Containment is `os.path.commonpath` on canonical forms of both sides. A
    string prefix test accepts `/output-evil` for root `/output`, and comparing
    un-resolved forms lets a symlink inside a root point anywhere. The refusal
    names the policy rather than the roots or the path: the roots are readable
    from the handshake, and the path is what the caller must not learn more
    about.

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

    `realpath` does not fold case, so on a case-insensitive volume (default
    APFS) `/X/output/x.blend` and root `/X/Output` differ by spelling while
    naming one directory. Comparing device and inode answers that without
    trusting spelling; both sides are already symlink-free, so it cannot admit
    a link out of the root. It accepts any spelling of the same directory, not
    only case variants (on APFS also the `/System/Volumes/Data` firmlink form),
    and trusts the filesystem's inode numbers.

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

    Blender puts absolute paths in its own error text in at least five shapes:
    quoted with `"` or `'`, bare, twice in one message, as a derived `<path>@`
    temp name, and as the process CWD for an empty path. None of those is
    guaranteed to equal what the caller sent, so paths are found structurally
    and replaced with `<path>`; every other token is kept wherever it sits. Log
    the raw text server-side if it is needed; return only this.

    Paths the caller already holds are replaced first, whole, with their
    derived `<path>@` form, because only they say where a name containing
    spaces ends.

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
    text = _PATH_IN_TEXT.sub(_placeholder_for, text)
    return _LIBRARY_ID_NAME.sub(r"\1", text)


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
