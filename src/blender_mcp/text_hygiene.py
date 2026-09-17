r"""
The server's copy of the addon's control-character rule.

The addon is installed into Blender as a self-contained package, so it cannot
import from `blender_mcp`, and server code must not import addon code. The block
below is therefore duplicated in `bundled/addon/text_hygiene.py`, and a test
fails if the two copies differ.

Only the character rule is shared. The addon truncates an over-long string,
because it only needs a label; `addon_manager.normalized_session_text` refuses
one, because a truncated path reads as a real, wrong one.
"""

import unicodedata

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
