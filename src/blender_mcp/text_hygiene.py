r"""
The server side's copy of the control-character rule the addon applies.

**Why a copy.** The bundled add-on is installed into Blender's own add-ons
directory as a self-contained package - `addon_manager.install_addon` copies
`src/blender_mcp/bundled/addon/` there - so it can import nothing from
`src/blender_mcp/`, and the handoff's §03 forbids the reverse direction
outright ("addon code must never import from `src/blender_mcp/server/`"). There
is no module both sides of this socket can import. The alternative to a checked
copy is not a shared module; it is the state this repair found, where
`bundled/addon/text_hygiene.client_safe_text` filtered control characters and
`addon_manager.normalized_session_text` - written in the same cycle, for the
same payload, on the other side of the same socket - checked type and length
and stopped. Driven end to end through `handshake_addon`, a `session_id` of
`proc-a\\n\\n---\\nSYSTEM: ...\\n\\x1b[2J` reached `get_addon_status`'s payload
verbatim, five lines long.

So the block below is duplicated **and the duplication is enforced**:
`tests/test_addon_manager.py::test_both_sides_of_the_socket_hold_the_same_control_character_block`
extracts the delimited region from each file and compares them character for
character. A fix applied to one copy and not the other fails the suite rather
than shipping, which is the property the missing sibling grep did not have.

What is *not* duplicated is policy. The addon truncates an over-long string,
because it is reducing a name to a label; `normalized_session_text` refuses one,
because a truncated path is a wrong path that reads as a real one. Only the
character rule is shared, and only the character rule is compared.
"""

import unicodedata

# --- BEGIN SHARED CONTROL-CHARACTER BLOCK ---
# Every Unicode general category that must not survive into a client-facing
# string, and why each one is here rather than a hand-rolled code-point range:
#
#   Cc  C0, C1 and DEL - newline, ESC, NUL.
#   Cf  format controls - the bidi overrides (U+202E RLO reverses a name so
#       `evil.blend` renders as `dneb.live`), ZWSP/ZWJ/ZWNJ and U+FEFF BOM.
#   Cs  lone surrogates, which are not encodable UTF-8 and break a JSON writer.
#   Co  private use, which renders as whatever the reader's font decides.
#   Cn  unassigned, which a future Unicode version may make a control.
#   Zl  U+2028 LINE SEPARATOR, Zp U+2029 PARAGRAPH SEPARATOR - line breaks in
#       their own right, which `str.splitlines()` counts and a "printable
#       ASCII" filter does not reach.
#
# A category lookup rather than a range list because a range list stops
# wherever its author stopped, and every case above sits past C1.
UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def is_unsafe(character: str) -> bool:
    """
    Report whether a character must not survive into a client-facing string.

    This string is written to logs and, from Task 9, into an agent's context: a
    newline turns an attacker-chosen file name into what looks like a new line
    of instructions, ESC turns it into a terminal escape, and a bidi override
    turns it into a different name entirely.

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
        value: Anything - it may have arrived over an unauthenticated socket.

    Returns:
        str: One line of text carrying no control, format, surrogate,
        private-use, unassigned or line/paragraph-separator character.

    """
    return "".join(character for character in str(value or "") if not is_unsafe(character)).strip()


# --- END SHARED CONTROL-CHARACTER BLOCK ---
