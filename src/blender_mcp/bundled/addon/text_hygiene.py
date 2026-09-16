r"""
The one place the addon decides what a client-facing string may contain.

**Why this module exists, rather than a fourth round of patches to
`session.py`.** Three repair cycles each hardened the string that had just been
broken and shipped the class: cycle 1 blocked the ASCII `/`, and `C:\\...`
walked through it; cycle 2 blocked both separator families, and U+FF0F walked
through that; cycle 3 blocked five named homoglyphs, and U+FE68 walked through
*that* - as did `///Users/...`, which needs no homoglyph at all. Every one of
those fixes was a **blocklist**, and a blocklist is a list of the attacks
somebody has already thought of.

So the predicate is inverted here, once, for both callers
(`session._failure_note` and `handlers/file_lifecycle._library_summary`):

- **Decide on the NFKC-normalised form.** U+FE68 SMALL REVERSE SOLIDUS is
  general category `Po` - outside `UNSAFE_CATEGORIES` entirely - and
  `unicodedata.normalize("NFKC", "\\ufe68")` is a real backslash, so any consumer
  that normalises gets a genuine separator out of a string this layer published
  as inert. Eleven code points outside the unsafe categories normalise to `/`,
  `\\` or `:`; `scripts/text_hygiene_enumeration.py` enumerates them
  and shows what each one does to both helpers below.
- **Sanitize, then gate, in that order.** Cycle 3's defect was the other
  order: `_is_reportable_whole` ran on the raw string and the publisher then
  stripped format characters, so `.<ZWSP>.` passed a gate that rejects `..` and
  was *published* as `..`. Every function here strips first and decides about
  the string it is actually going to return.
- **Publish only what an allowlist admits.** A link component may hold
  `LINK_COMPONENT_ALLOWED` and nothing else; a leaf name may hold letters,
  numbers, marks and `LEAF_PUNCTUATION` and nothing else. Anything else is
  reduced, not repaired - repairing forges a name the caller did not choose.

Nothing here imports `bpy`, which is what lets `tests/test_session_state.py`
load it straight from its file.

**The control-character block below is duplicated in
`src/blender_mcp/text_hygiene.py`, deliberately, and the duplication is
checked.** The addon is installed into Blender's own add-ons directory as a
self-contained package, so it can import nothing from `src/blender_mcp/`; the
handoff's §03 forbids the reverse direction outright. There is therefore no
module both sides of the socket can import, and the alternative to a checked
copy was the state this repair found: `client_safe_text` on the addon side and
nothing whatsoever on the server side, written in the same cycle.
`tests/test_addon_manager.py::test_both_sides_of_the_socket_hold_the_same_control_character_block`
compares the two regions character for character, so a fix applied to one copy
and not the other fails the suite instead of shipping.
"""

import os
import unicodedata

from contextlib import suppress

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


# Blender's marker for "relative to the open .blend".
RELATIVE_PREFIX = "//"
# Long enough for any real shot name, short enough that the field cannot be used
# as a megabyte-scale channel into a client's logs or an agent's context.
MAX_NOTE_NAME_CHARS = 64
UNNAMEABLE = "the requested file"
# What a *link* component may contain. Deliberately narrower than the leaf rule
# below: a link is published with its separators intact and therefore with
# structure a reader will act on, so it gets the strictest form of the
# predicate. The cost of that choice is that a linked library under a directory
# named in a non-Latin script is reported by its leaf rather than whole, which
# loses Task 7 a hint and loses the client nothing it is entitled to.
LINK_COMPONENT_ALLOWED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._ -")
# What a *leaf* name may contain, as categories rather than literals: letters
# (L*), numbers (N*) and combining marks (M*), plus the punctuation below. A
# leaf has no separators left to forge, so rejecting every accented or CJK file
# name would cost a real user the only useful word in the failure note while
# buying nothing. U+2044 FRACTION SLASH and U+2215 DIVISION SLASH are `Sm` and
# NFKC leaves both unchanged, which is exactly why this is an allowlist: they
# are excluded by not being admitted, not by being named.
LEAF_CATEGORY_PREFIXES = ("L", "N", "M")
LEAF_PUNCTUATION = frozenset(" ._-()[]#+,&'")
# Not a leaf, whatever else it looks like.
NOT_A_LEAF = frozenset({"", ".", ".."})
_LEAF_SEPARATORS = ("/", "\\")


def is_confusable(text: str) -> bool:
    r"""
    Report whether a string carries a disguise NFKC can name.

    **What this does and does not catch, stated as a measurement rather than as
    a promise.** An earlier revision of this docstring opened "report whether a
    string renders as something other than what it is", and that is false for
    the classic homoglyph class - the one a reader would think of first.
    Measured against this module (`scripts/text_hygiene_enumeration.py` section
    6 reproduces it): Cyrillic U+0430/U+043E/U+0435/U+0441, Greek U+03BF SMALL
    OMICRON and Cherokee U+13A0 are all general category `Ll`/`Lu`, all pass
    `_is_admissible_leaf`, all leave NFKC unchanged - so all return False here -
    and each one renders as `canon.blend` while naming a different file. This
    function does not see them, and it is the module's only normalisation
    decision - `grep -n "normalize(" text_hygiene.py` reports one call site
    outside the docstrings, the one below - so nothing else here sees them
    either. The honest scope is the **compatibility** class: a character NFKC
    folds into a different one. Closing the canonical-homoglyph class needs a
    confusables table (UTS #39), which this addon does not carry.

    **NFKC is used to reject, never to rewrite**, and the distinction is a
    correction this pass made to its own first draft. Normalising and then
    publishing the result forges a name: a library actually called
    `canon\u2024blend` would be published as `canon.blend`, which is a different
    file, asserted confidently. So the normalised form is compared, not adopted.

    **The comparison is NFKC against NFC, not against the raw string**, and the
    difference is a real shot name. `cafe\u0301.blend` - `e` plus U+0301
    COMBINING ACUTE - is what macOS produced for years and is *canonically*
    equivalent to `caf\u00e9.blend`: the two are the same text by Unicode's own
    definition, not a disguise for one another. Compared against the raw string
    it read as confusable and `client_safe_leaf` reduced it to
    `the requested file`, dropping the only useful word in a failure note.
    Composing first admits exactly the canonical-equivalence class: a string is
    refused here only when its compatibility form differs from its canonical
    form. **That class is wider than "characters NFKC leaves alone", and the
    difference is measured rather than reasoned about.**
    `scripts/text_hygiene_enumeration.py` section 8 enumerates the code points
    whose answer this change flipped - 1,116, of which 1,097 are leaf-admissible
    and 1,026 are singleton canonical decompositions, including U+212B ANGSTROM
    SIGN and U+2126 OHM SIGN. Those are canonical homoglyphs, so they join the
    class section 6 already measures rather than forming a new one; see
    `client_safe_leaf`'s second known limit. An earlier revision of this
    paragraph cited `NFKC(x) == NFKC(NFC(x))` as the supporting property. That
    identity is true (section 8 still checks it, 0 disagreements over 289,394
    assigned code points) but it is a statement about the *NFKC* form, and this
    predicate compares NFKC against NFC - so it never answered the question it
    was cited for. The caller still publishes the raw string, so no name is
    rewritten.

    Where it is load-bearing, most of the obvious cases being closed already:
    every one of the eleven code points that NFKC turns into `/`, `\` or `:` is
    `Po`, `Sm` or `So`, so `_is_admissible_leaf` already refuses them and
    `LINK_COMPONENT_ALLOWED` - being ASCII - refuses them twice over. What this
    catches is the compatibility confusable that *is* admissible: U+FF4E
    FULLWIDTH LATIN SMALL LETTER N is category `Ll`, so `ca\uff4eon.blend`
    passes the leaf allowlist and renders as `canon.blend` to every reader.
    `scripts/text_hygiene_enumeration.py` reports both halves of that.

    Args:
        text: The stripped candidate.

    Returns:
        bool: True when NFKC changes the string beyond what NFC already does.

    """
    return unicodedata.normalize("NFKC", text) != unicodedata.normalize("NFC", text)


def client_safe_text(value: object, max_chars: int = MAX_NOTE_NAME_CHARS) -> str:
    """
    Reduce a string to one bounded, control-free line.

    The hygiene that is wanted **without** any structural decision, for a caller
    that has already established the string may be published as it stands. It
    does not normalise: this function *publishes* what it returns, and rewriting
    a caller's name into its NFKC form would hand a client a different name than
    the one the datablock carries. `is_confusable` is where NFKC is consulted.

    Args:
        value: The text to reduce; anything, since it may have come off a socket.
        max_chars: The longest result; longer input is truncated with a marker,
            so a client cannot read a cut-off string as a whole one.

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
        leaf: A candidate leaf, already normalised and control-stripped.

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

    Strip, split on **both** separator families, bound the length, then require
    the survivor to be admissible *and* not a confusable - and fall back to a
    neutral phrase when it is not. The final check is the part that matters: a
    transformation that silently fails open is how a sanitizer passes its own
    tests while leaking, and the two clauses cover different things
    (`_is_admissible_leaf` refuses a character class, `is_confusable` refuses an
    admissible character that **NFKC folds** into a different one - which is a
    narrower thing than "renders as a different one", and `is_confusable`'s own
    docstring measures the gap).

    A path that resolves to a *directory* is refused before any of that. Blender
    reports the process CWD when it is handed an empty path, so the "leaf" there
    is the name of the directory the server happens to be running in - neither a
    file nor anything the caller named. **Known limit, deliberately accepted:**
    that `isdir` call is a directory-existence oracle. `/etc` comes back as
    `the requested file` and `/etcXYZNOPE` as `etcXYZNOPE`, which is one bit per
    probe, enumerable by an unauthenticated client once Task 6 lets it choose
    the path. It is recorded rather than removed because the alternative -
    publishing the CWD's own directory name - discloses more per probe than the
    bit does, and because Task 6's own root check is where path probing is
    meant to be stopped.

    What is promised: the result is either one line of at most
    `MAX_NOTE_NAME_CHARS` admissible characters carrying no compatibility
    disguise, taken verbatim from the caller's own last path component, or
    `UNNAMEABLE`. Nothing in between, and nothing rewritten - a name this
    function cannot vouch for is refused rather than repaired into a name nobody
    chose.

    **A second known limit, alongside the `isdir` oracle below.** "Carrying no
    compatibility disguise" is not "renders as what it is", and the admitted
    class is **not** "characters NFKC leaves alone" - an earlier revision of
    this paragraph said it was, and the two members named below are
    counter-examples to it. `is_confusable` compares the NFKC form against the
    **NFC** form, so what it admits is every character whose compatibility
    decomposition goes no further than its canonical one. That class has two
    populations and both are published verbatim while rendering as something
    else:

    - the characters NFKC really does leave alone - Cyrillic U+043E in place of
      `o`, Greek U+03BF, Cherokee U+13A0: `Ll`, admissible, an ASCII-looking
      name;
    - **the canonical singletons, which NFKC does change** and which the
      NFC comparison nevertheless admits. `scripts/text_hygiene_enumeration.py`
      section 8 counts them: of 1,116 code points the NFC change newly admits,
      1,097 are leaf-admissible and 1,026 are singleton canonical
      decompositions. U+212B ANGSTROM SIGN and U+2126 OHM SIGN are the worked
      cases - `client_safe_leaf('/shots/<U+212B>.blend')` was
      `the requested file` and now publishes that U+212B leaf verbatim, which on
      ext4 and NTFS names a different file from the U+00C5 spelling it renders
      as, and is a UTS #39 confusable pair.

    The NFC comparison is still right - it is what stops NFD `café.blend`,
    a real shot name macOS produced for years, from being reduced to
    `the requested file` - but it is a **widening**, not a no-op, and this is
    what it widened to. Closing either population needs a UTS #39 confusables
    table this addon does not carry. The consequence is bounded by what this
    function returns at all: one path component, taken after the split on both
    separator families and after `_is_admissible_leaf` has refused every
    category outside letters/numbers/marks and `LEAF_PUNCTUATION`. So a
    homoglyph buys a misleading *leaf*, not the directories above it.

    Args:
        file_path: The path Blender reported.

    Returns:
        str: A leaf file name, or `the requested file` when no admissible leaf
        exists.

    """
    raw = str(file_path or "")
    # No early return for an empty path: the admissibility check below already
    # answers it (`""` is in `NOT_A_LEAF`), and a second guard producing the
    # same answer is a branch no revert can distinguish - which is how a
    # behaviour becomes unfalsifiable by its own test suite.
    #
    # One stat, on the main thread, against a path Blender just failed on.
    with suppress(OSError, ValueError):
        if os.path.isdir(raw):
            return UNNAMEABLE

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

    `//` means "relative to the open .blend", but `//` followed by a root - the
    `///Users/victim/...` form - names an absolute location and renders as one.
    Reporting that as `is_relative: True` was how a whole absolute path reached
    the client through the branch that exists to keep absolute paths out.

    Args:
        text: The normalised, control-stripped link.

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

    The allowlist, in one place. A link survives only if, **after**
    `strip_unsafe` and never after any rewrite, it is a relative link whose body
    splits on `/` into components that are each non-empty, neither `.` nor `..`,
    and built solely from `LINK_COMPONENT_ALLOWED`. Everything else - an
    absolute path, a rooted `//` form, a traversal, a separator this layer does
    not admit, an over-long string - returns None and is reduced to a leaf by
    the caller.

    **Stripping happens before the gate, and the string gated is the string
    returned.** Cycle 3's defect was the opposite order: the gate ran on the raw
    text and the publisher then dropped format characters, turning a `.<ZWSP>.`
    the gate had admitted into the `..` it would have rejected. Here the ZWSP is
    gone before any component is inspected, so that input is refused.

    **No NFKC pass, and its absence is deliberate rather than an omission.**
    `LINK_COMPONENT_ALLOWED` is ASCII, `RELATIVE_PREFIX` and the separator are
    ASCII, and NFKC is the identity on every ASCII code point - so any string
    this function returns is already NFKC-stable, and a normalisation step here
    could only widen what is admitted. `is_confusable` carries that job for the
    leaf path, where the allowlist is category-based and therefore does admit
    characters NFKC changes. `scripts/text_hygiene_enumeration.py` checks the
    ASCII-stability claim rather than asserting it.

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
