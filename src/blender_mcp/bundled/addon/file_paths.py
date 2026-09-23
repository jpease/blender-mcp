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
  passes `use_scripts=False`, and `handlers/blend_files.refuse_scripts_auto_execute`
  checks the auto-execute preference, which needs `bpy`.

Every decision is a pure function over facts (`inside_roots`, `blend_file_refusal`,
`save_target_refusal`, `contains`); the shells beside them gather those facts and
raise. `resolve_blend_path` is the single entry point that runs them in the one
order that never turns a refusal into an existence oracle.

Free of `bpy` so it can be tested without Blender; the caller expands Blender's
`//` prefix before `resolve_blend_path`.
"""

import contextlib
import os
import re

from collections.abc import Iterable, Sequence

from .text_hygiene import client_safe_name_leaf

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


# Prefixes, and their lengths matter: a longer prefix rejects valid files.
# Every header version starts with `BLENDER`, and gzip's fourth byte varies, so
# only its two magic bytes are checked.
BLEND_MAGIC_UNCOMPRESSED = b"BLENDER"
BLEND_MAGIC_ZSTD = b"\x28\xb5\x2f\xfd"
BLEND_MAGIC_GZIP = b"\x1f\x8b"
BLEND_MAGIC_PREFIXES = (BLEND_MAGIC_UNCOMPRESSED, BLEND_MAGIC_ZSTD, BLEND_MAGIC_GZIP)
# All the header test needs: a shorter read cannot match the longest prefix.
BLEND_HEADER_BYTES = max(len(prefix) for prefix in BLEND_MAGIC_PREFIXES)


def is_blend_header(header: bytes) -> bool:
    """
    Report whether a file's first bytes are one of Blender's `.blend` headers.

    Args:
        header: The first `BLEND_HEADER_BYTES` of the file. A short read, from a
            truncated or empty file, matches no prefix.

    Returns:
        bool: True for an uncompressed, zstd-compressed or gzip-compressed
        `.blend`.

    """
    return header.startswith(BLEND_MAGIC_PREFIXES)


class PathOutsideRootsError(ValueError):
    """A path resolved outside every configured root; a caller that has its own roots can tell it apart."""


# Named by the policy, not by the path or the roots, which the handshake already
# reports. One string, because one function refuses.
ROOTS_REFUSAL = "path is outside the allowed file roots (BLENDERMCP_FILE_ROOTS); see file_roots in get_addon_status"


def inside_roots(canonical_candidate: str, canonical_roots: Sequence[str]) -> bool:
    """
    Decide by spelling alone whether a path is authorized, from facts already gathered.

    The whole authorization rule, and no filesystem: with no roots configured
    every path is allowed (the local-artist default the handshake reports), and
    otherwise the path must lie in one of them. Both sides must already be
    canonical, or a symlink inside a root could point anywhere.

    Args:
        canonical_candidate: The path to place, in `canonical_path` form.
        canonical_roots: The configured roots, in `canonical_path` form; empty
            means the permissive default.

    Returns:
        bool: True when the path may be used. False only means no root contains
        it *by spelling*: a case-insensitive volume spells one directory two
        ways, which `enforce_roots` settles with `_has_ancestor_directory`.

    """
    return not canonical_roots or any(contains(root, canonical_candidate) for root in canonical_roots)


def blend_file_refusal(
    *, is_directory: bool, exists: bool, readable: bool, header: bytes, directory_exists: bool
) -> str | None:
    """
    Decide whether a path the caller wants to open is an existing, readable `.blend`.

    Args:
        is_directory: Whether the path names a directory.
        exists: Whether it names a regular file.
        readable: Whether its first bytes could be read.
        header: Those bytes; empty when nothing was read.
        directory_exists: Whether the path's parent directory exists. Which half of the path
            is wrong is the one thing "file does not exist" cannot say, and a caller told only
            that cannot tell a mistyped filename from a mistyped directory. Saying which,
            without saying where, keeps this message free of server layout - the invariant
            `tests/test_file_paths.py` enforces against every refusal here. Required rather
            than defaulted: a default would be the confident half of the answer, so a caller
            that forgot to stat the directory would report one that may not be there.

    Returns:
        str | None: None when the file may be opened, else the refusal, which
        names no path.

    """
    if is_directory:
        return "path is a directory, not a .blend file"
    if not exists:
        if not directory_exists:
            return "file does not exist, and neither does the directory named in its path"
        return "file does not exist, though the directory named in its path does"
    if not readable:
        return "file could not be read"
    if not is_blend_header(header):
        return "file is not a .blend file (unrecognised header)"
    return None


def save_target_refusal(
    *, is_directory: bool, directory_exists: bool, directory_writable: bool, create_directories: bool
) -> str | None:
    """
    Decide whether a path the caller wants to save to can be written.

    Args:
        is_directory: Whether the target itself names a directory.
        directory_exists: Whether the target's parent directory exists.
        directory_writable: Whether this process may write in it.
        create_directories: True when the caller will create a missing directory
            with `create_save_directory`; its writability is then Blender's to report.

    Returns:
        str | None: None when the target may be written, else the refusal, which
        names no path.

    """
    if is_directory:
        return "path is a directory, not a .blend file"
    if not directory_exists:
        if create_directories:
            return None
        return "target directory does not exist; pass create_directories=true to create it"
    if not directory_writable:
        return "target directory is not writable"
    return None


def contains(canonical_root: str, canonical_candidate: str) -> bool:
    """
    Decide by spelling alone whether a canonical root holds a canonical candidate.

    `os.path.commonpath` rather than a string prefix test, which accepts
    `/output-evil` for root `/output`.

    Args:
        canonical_root: A root in `canonical_path` form.
        canonical_candidate: The path to place, in `canonical_path` form.

    Returns:
        bool: True when the root is the candidate itself or one of its ancestors.

    """
    try:
        return os.path.commonpath((canonical_root, canonical_candidate)) == canonical_root
    except ValueError:
        return False  # different drives on Windows: not contained by spelling


def _require_blend_file(path: str) -> None:
    """
    Gather what the filesystem says about an open target, then refuse on `blend_file_refusal`'s verdict.

    Args:
        path: A canonical path.

    Raises:
        ValueError: With a message that names no path.

    """
    is_directory = os.path.isdir(path)
    exists = not is_directory and os.path.isfile(path)
    header = b""
    cause: OSError | None = None
    if exists:
        try:
            with open(path, "rb") as handle:
                header = handle.read(BLEND_HEADER_BYTES)
        except OSError as exc:
            cause = exc
    refusal = blend_file_refusal(
        is_directory=is_directory,
        exists=exists,
        readable=cause is None,
        header=header,
        directory_exists=os.path.isdir(os.path.dirname(path)),
    )
    if refusal is not None:
        # OSError's own text carries the path, so it is chained, not quoted.
        raise ValueError(refusal) from cause


def _require_save_target(path: str, *, create_directories: bool) -> None:
    """
    Gather what the filesystem says about a save target, then refuse on `save_target_refusal`'s verdict.

    Args:
        path: A canonical path.
        create_directories: Passed to the verdict.

    Raises:
        ValueError: With a message that names no path.

    """
    directory = os.path.dirname(path)
    directory_exists = os.path.isdir(directory)
    refusal = save_target_refusal(
        is_directory=os.path.isdir(path),
        directory_exists=directory_exists,
        directory_writable=directory_exists and os.access(directory, os.W_OK),
        create_directories=create_directories,
    )
    if refusal is not None:
        raise ValueError(refusal)


def enforce_roots(path: str, roots: Iterable[str]) -> None:
    """
    Refuse a path outside every configured root; with no roots, allow all.

    The syscalls behind `inside_roots`' verdict: every root is canonicalized
    once, and every root is tried by spelling before any root is tried by
    `_has_ancestor_directory`, whose `stat` walk a second root that plainly
    contains the path must not pay for.

    Args:
        path: The path to check. Canonicalized here, so a caller that has not
            resolved it cannot weaken the check; `canonical_path` is idempotent,
            so passing an already-canonical path costs one `realpath`.
        roots: Configured roots; empty means the permissive default.

    Raises:
        PathOutsideRootsError: If roots are configured and none contains the path.

    """
    canonical_roots = [canonical_path(root) for root in roots]
    if not canonical_roots:
        return  # the permissive default costs no syscall
    candidate = canonical_path(path)
    if inside_roots(candidate, canonical_roots):
        return
    # Only once no root contains the path by spelling: this walks the candidate's
    # ancestors with a `stat` each, and answers the case-insensitive volume.
    if any(_has_ancestor_directory(candidate, canonical_root) for canonical_root in canonical_roots):
        return
    raise PathOutsideRootsError(ROOTS_REFUSAL)


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


def resolve_blend_path(raw: object, *, roots: Iterable[str], must_exist: bool, create_directories: bool = False) -> str:
    """
    Validate, canonicalize and authorize a caller-supplied `.blend` path.

    The one place a `.blend` path becomes trusted, and the one place the refusal
    order is decided: the shape the client sent, then the canonical form, then
    the roots, then the `.blend` name, and only then what is on disk. The roots
    come before every question this host can answer about the path, so one
    outside them gets the same refusal whether it names a `.blend`, a directory
    or nothing; otherwise any path on the machine could be probed for existence.

    Refusals give the reason, never a path: echoing the resolved form would reveal
    where a symlink or `~` led.

    Args:
        raw: The path, already passed through `bpy.path.abspath` by the caller.
        roots: The directories this call may touch; empty allows every path.
        must_exist: True to open or link (the file must exist and carry a
            `.blend` header); False to save (its directory must be writable).
        create_directories: For a save, accept a directory that does not exist
            yet; the caller creates it with `create_save_directory`.

    Returns:
        str: The canonical path to hand to Blender. Never hand Blender the raw
        form: it resolves `..` before symlinks, which is not the path that was
        checked here.

    Callers must refuse a `//` path when no .blend is open: `bpy.path.abspath`
    then leaves it relative, and it would resolve against the working directory.
    `handlers/blend_files._expand_blender_relative` does this.

    Raises:
        PathOutsideRootsError: If the path lies outside `roots`.
        ValueError: If the path is refused for any other reason.

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
    enforce_roots(resolved, roots)
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

    Call it only with `resolve_blend_path`'s result. That path is symlink-free
    and inside the roots, so every directory made is inside the root, unless
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

    Uses the same leaf rule as `handlers/blend_files.library_summary`, which does not stat the
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
