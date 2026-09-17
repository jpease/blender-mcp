r"""
The one place the addon decides what a client-facing string may contain.

Blocklists kept missing forms (`C:\\...`, U+FF0F, U+FE68, `///Users/...`), so
the rules here admit rather than block:

- Refuse what NFKC would change. U+FE68 SMALL REVERSE SOLIDUS is not an unsafe
  category, yet any consumer that normalizes gets a backslash from it.
- Strip first, then gate the string that will be returned. Gating first lets
  `.<ZWSP>.` pass a check for `..` and then be published as `..`.
- Publish only what an allowlist admits, and reduce anything else rather than
  repair it: a repaired name is one the caller did not choose.

Nothing here imports `bpy`, so tests can load this file directly.

The control-character block below is duplicated in
`src/blender_mcp/text_hygiene.py`, because neither side of the socket can import
the other's code. A test fails if the two copies differ.
"""

import os
import unicodedata

from contextlib import suppress

# --- BEGIN SHARED CONTROL-CHARACTER BLOCK ---
# Unicode categories that must not reach a client-facing string:
#
#   Cc  C0, C1 and DEL: newline, ESC, NUL.
#   Cf  format controls: bidi overrides (U+202E shows a name backwards),
#       zero-width characters and the BOM.
#   Cs  lone surrogates, which UTF-8 cannot encode.
#   Co  private use, which renders however the reader's font decides.
#   Cn  unassigned, which a future Unicode version may make a control.
#   Zl, Zp  U+2028 and U+2029, line breaks an ASCII filter misses.
#
# Categories, not code-point ranges: most of these lie past C1.
UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def is_unsafe(character: str) -> bool:
    """
    Report whether a character must not survive into a client-facing string.

    Such strings reach logs and an agent's context, where a newline in a file
    name can pose as a new instruction, ESC as a terminal escape, and a bidi
    override as a different name.

    Args:
        character: A single character.

    Returns:
        bool: True when the character must be dropped.

    """
    return unicodedata.category(character) in UNSAFE_CATEGORIES


def strip_unsafe(value: object) -> str:
    """
    Drop every character `is_unsafe` rejects, and trim the surrounding whitespace.

    Args:
        value: Anything; it may come from an unauthenticated socket.

    Returns:
        str: One line of text with no unsafe character.

    """
    return "".join(character for character in str(value or "") if not is_unsafe(character)).strip()


# --- END SHARED CONTROL-CHARACTER BLOCK ---


# Blender's marker for "relative to the open .blend".
RELATIVE_PREFIX = "//"
# Bounds how much text a hostile name can push into client logs or an agent's context.
MAX_NOTE_NAME_CHARS = 64
UNNAMEABLE = "the requested file"
# Stricter than the leaf rule: a link is published with its separators, so a
# reader acts on its structure. A library under a non-ASCII directory is
# therefore reported by its leaf.
LINK_COMPONENT_ALLOWED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._ -")
# A leaf has no separators left to forge, so accented and CJK names are admitted.
# Being an allowlist, it also refuses look-alike slashes such as U+2044 and
# U+2215, which NFKC leaves unchanged.
LEAF_CATEGORY_PREFIXES = ("L", "N", "M")
LEAF_PUNCTUATION = frozenset(" ._-()[]#+,&'")
# Not a leaf, whatever else it looks like.
NOT_A_LEAF = frozenset({"", ".", ".."})
_LEAF_SEPARATORS = ("/", "\\")


def is_confusable(text: str) -> bool:
    r"""
    Report whether a string carries a disguise NFKC can name.

    NFKC is used only to reject. Publishing the normalized form would rename the
    file: `canon\u2024blend` would become `canon.blend`.

    The NFKC form is compared with the NFC form, not the raw string, so a
    decomposed `café.blend`, as macOS writes it, is not refused. This catches
    compatibility look-alikes the leaf allowlist admits, such as U+FF4E FULLWIDTH
    LATIN SMALL LETTER N. Look-alikes NFKC leaves alone (Cyrillic U+043E) or
    folds the same way as NFC (U+212B ANGSTROM SIGN) still pass; closing those
    needs a UTS #39 confusables table.

    Args:
        text: The stripped candidate.

    Returns:
        bool: True when NFKC changes the string beyond what NFC already does.

    """
    return unicodedata.normalize("NFKC", text) != unicodedata.normalize("NFC", text)


def client_safe_text(value: object, max_chars: int = MAX_NOTE_NAME_CHARS) -> str:
    """
    Reduce a string to one bounded, control-free line.

    Makes no structural decision and does not normalize, so the caller must
    already know the string may be published as it stands.

    Args:
        value: The text to reduce; it may come from a socket.
        max_chars: The longest result; longer input is truncated with a `...`
            marker, so a client cannot mistake it for the whole string.

    Returns:
        str: One line of safe text, at most `max_chars` characters.

    """
    text = strip_unsafe(value)
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


def _is_admissible_leaf(leaf: str) -> bool:
    """
    Decide whether a single path component may be published under its own name.

    Args:
        leaf: A candidate leaf, already control-stripped.

    Returns:
        bool: True when every character is a letter, number, mark or admitted
        punctuation, and the whole is not an empty or traversal component.

    """
    if leaf in NOT_A_LEAF:
        return False
    return all(
        character in LEAF_PUNCTUATION or unicodedata.category(character).startswith(LEAF_CATEGORY_PREFIXES)
        for character in leaf
    )


def client_safe_leaf(file_path: object) -> str:
    """
    Reduce a path Blender reported to one bounded, admissible leaf name.

    A path that is a directory is refused first: given an empty path, Blender
    reports the process working directory, which the caller never named. That
    check tells a client whether a directory exists, one bit per probe. Naming
    the working directory would disclose more, and the file-lifecycle tools' root
    check is what should stop path probing.

    The result is the caller's own last path component, verbatim, or
    `UNNAMEABLE`. Some look-alike names still pass (see `is_confusable`), but
    they can only mislead about the leaf, never the directories above it.

    Args:
        file_path: The path Blender reported.

    Returns:
        str: A leaf file name, or `the requested file` when no admissible leaf
        exists.

    """
    raw = str(file_path or "")
    # An empty path needs no guard of its own: `NOT_A_LEAF` refuses it.
    with suppress(OSError, ValueError):
        if os.path.isdir(raw):
            return UNNAMEABLE
    return client_safe_name_leaf(raw)


def client_safe_name_leaf(name: object) -> str:
    """
    Reduce a name to one admissible leaf without touching the filesystem.

    For text that names nothing on this machine, such as a `Library.name` a
    `.blend` author chose. A stat would probe the working directory for a
    relative name, or a network share for a Windows UNC one.

    Args:
        name: The text to reduce.

    Returns:
        str: A leaf, or `the requested file` when no admissible leaf exists.

    """
    raw = str(name or "")
    leaf = strip_unsafe(raw)
    for separator in _LEAF_SEPARATORS:
        leaf = leaf.rsplit(separator, 1)[-1]
    leaf = client_safe_text(leaf)

    if is_confusable(leaf) or not _is_admissible_leaf(leaf):
        return UNNAMEABLE
    return leaf


def relative_link_body(text: str) -> str | None:
    """
    Return the body of a Blender-relative link, or None when it is not one.

    `///Users/...` starts with `//` but names an absolute location; reporting it
    as relative would publish that absolute path.

    Args:
        text: The control-stripped link.

    Returns:
        str | None: Everything after the `//` prefix, or None when `text` does
        not begin with the prefix or begins a rooted path just after it.

    """
    if not text.startswith(RELATIVE_PREFIX):
        return None
    body = text[len(RELATIVE_PREFIX) :]
    if body[:1] in _LEAF_SEPARATORS:
        return None
    return body


def safe_relative_link(file_path: object, max_chars: int) -> str | None:
    """
    Return a link that may be published whole, or None when it must be reduced.

    The link must be relative, and each `/`-separated component non-empty, not
    `.` or `..`, and made only of `LINK_COMPONENT_ALLOWED`. The check runs on the
    stripped string, which is the string returned, so `.<ZWSP>.` is refused as
    `..`. No NFKC check is needed: every allowed character is ASCII, which NFKC
    leaves unchanged.

    Args:
        file_path: The raw `Library.filepath`.
        max_chars: The longest link that may be published whole.

    Returns:
        str | None: The exact string to publish, or None.

    """
    text = strip_unsafe(file_path)
    if len(text) > max_chars:
        return None
    body = relative_link_body(text)
    if not body:
        return None
    for component in body.split("/"):
        if component in NOT_A_LEAF or not set(component) <= LINK_COMPONENT_ALLOWED:
            return None
    return text
