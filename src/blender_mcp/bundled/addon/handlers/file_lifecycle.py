"""
File-lifecycle commands: what shot is open, and what state it is in.

Task 3 creates this module carrying `get_session_info` only. **Task 6 extends
it** with `open_shot`, `save_shot` and `reset_session` - the commands that
actually swap the database - so the mixin is deliberately left with room for
them rather than being collapsed into a single function.

Every client-facing string this module publishes goes through `..text_hygiene`,
never through a rule written here: the whole reason that module exists is that
this field and `session._failure_note` kept being fixed one at a time.

`get_session_info` is read-only and touches no datablock, which is why it is in
`server_core._READ_ONLY_COMMANDS`: it is the cheap poll a client makes after a
swap it was told about, and it must never be wrapped in `mutation_transaction`.
"""

import bpy

from ..session import session_snapshot
from ..text_hygiene import (
    client_safe_leaf,
    relative_link_body,
    safe_relative_link,
    strip_unsafe,
)

# Long enough for a real project-relative link, short enough that this field
# cannot be used to push a multi-kilobyte string into an agent's context.
#
# **It is four times `client_safe_text`'s default, not "well under" it**, and an
# earlier revision of this comment said the opposite.
# `text_hygiene.MAX_NOTE_NAME_CHARS` is 64 and this is 256, so a link published
# whole is the longest single string this module emits. The bound that is
# actually being traded off is the *aggregate*: one entry per linked library
# multiplies it, and the library list is itself unbounded (recorded as a
# residual, owner Task 7). 256 is chosen against a real project-relative link -
# `//lib/chars/hero/hero_rig.blend` and several directories deeper - rather than
# against the leaf rule, which governs a name with no separators left in it and
# has no reason to be the same number.
_MAX_REPORTED_LINK_CHARS = 256


def _library_summary(library: object) -> dict[str, object]:
    r"""
    Describe one linked library by identity, not by where it sits on this machine.

    `Library.filepath` is whatever the link was made with, and the decision
    about how much of it may be published is **not taken here**. It is taken by
    `text_hygiene.safe_relative_link`, which is the single allowlist both this
    field and `session._failure_note` are gated on. Three repair cycles put the
    decision here as a blocklist and three critics walked through it - with
    `C:\\`, then U+FF0F, then U+FE68 and `///Users/...`. The predicate is
    inverted and shared so that the next character nobody has thought of is
    excluded by not being admitted.

    Which reduction applies:

    - A link `safe_relative_link` admits is published **exactly as that function
      returned it** - the normalised, control-stripped form it made its decision
      about. Gating one string and publishing another is what cycle 3 did: the
      gate ran on the raw text and the publisher then stripped format
      characters, manufacturing the `..` the gate had rejected.
    - Everything else - absolute, rooted (`///...`), traversing, over-long, or
      carrying a character the allowlist does not admit - is reduced to a leaf,
      because those are the studio's storage layout or a disguise for it, and
      this socket is unauthenticated.

    `is_relative` says which *kind* of link this is, and says it about the same
    stripped form the gate saw. It is not `filepath.startswith("//")`: that reported
    `///Users/victim/...` as relative, which is how a whole absolute path
    reached a client through the branch that exists to keep absolute paths out.

    **`name` is allowlisted too, and it is the fifth recurrence of the defect
    class above.** It sat one line up from `filepath` going through
    `client_safe_text` - which strips control characters and truncates and
    applies no allowlist at all - on the argument that a Blender ID name is a
    display label rather than a path. It is not: measured on Blender 5.2.2, a
    `Library.name` accepts `/Users/victim/shots/canon.blend`, `../../etc/passwd`
    and `C:\\studio\\vault` verbatim, and this socket is unauthenticated. In the
    benign case the name Blender mints *is* the basename, so `client_safe_leaf`
    returns it unchanged and the cost of the allowlist is zero;
    `tests/test_session_state.py::test_a_hostile_library_name_is_reduced_to_a_leaf_like_the_filepath_is`
    runs the whole hostile table through it.

    Args:
        library: A `bpy.types.Library`.

    Returns:
        dict[str, object]: `session_uid` - the handle Task 7's commands resolve
        by, *never* by name, because after a Route C override two libraries'
        contents can share a name. **A `session_uid` is valid only for the
        `(session_id, session_epoch)` it was read under.** Blender's own RNA
        description for it says "A session-wide unique identifier ... unchanged
        when reloading the file"; that is wrong, and a Task 7 implementer who
        reads RNA rather than this line will resolve the wrong library. Measured
        on 5.2.2 by `scripts/blender_probes/session_handlers.py`, which links one
        library into a shot and reopens the shot four times::

            uid sequence across four loads: [1452, 1485, 1518, 1551]
            STABLE across loads: False

        **The last line is the finding; the numbers are not.** They are
        allocation counters and move with everything the process did before the
        measurement. An earlier revision of this docstring quoted a three-value
        sequence attributed to "a critic", and no committed instrument produced
        it - the probe above did not measure session uids at all until this
        round. What the probe reproduces on any run is that the uid is different
        after every load, so a uid cached across a swap names nothing. `name`,
        reported for display only
        and reduced to an admissible leaf; `filepath` (whole only for a link the
        allowlist admits, a bare leaf otherwise); `is_relative`; and
        `is_missing`, which says the link is broken now and is what makes a swap
        or a reload worth attempting.

    """
    filepath = str(getattr(library, "filepath", "") or "")
    whole = safe_relative_link(filepath, _MAX_REPORTED_LINK_CHARS)
    return {
        "session_uid": getattr(library, "session_uid", None),
        "name": client_safe_leaf(getattr(library, "name", "")),
        "filepath": whole if whole is not None else client_safe_leaf(filepath),
        "is_relative": relative_link_body(strip_unsafe(filepath)) is not None,
        "is_missing": bool(getattr(library, "is_missing", False)),
    }


class FileLifecycleHandlersMixin:
    """
    Report and (from Task 6) change which .blend the session holds.

    `get_session_info` is a `staticmethod` because it reads process-wide session
    state and the open database, not anything belonging to one server instance.
    It is still reached as `self.get_session_info` from the dispatch table, like
    every other handler.
    """

    @staticmethod
    def get_session_info() -> dict[str, object]:
        """
        Report the current session: which file, which epoch, and what went wrong.

        `session_epoch` is what a client compares against the epoch it last saw.
        When it has moved, the addon's advertised `capabilities` may have moved
        with it - the Poly Haven / Sketchfab / ND handlers are gated on
        per-`.blend` scene flags - so the client re-handshakes before trusting
        its cached list. `is_dirty` and `libraries` are what make a swap
        destructive, so both are reported before one is attempted.

        Returns:
            dict[str, object]: `session_id` (str, minted once per addon
            process) and `session_epoch` (int) - the pair a client compares,
            because the counter alone restarts at 0 with the process;
            `current_filepath` (str | None; None when the session has never been
            saved **and after an aborted swap, because this process can no
            longer truthfully name the file it has open**), `last_load_error` /
            `last_save_error` (str | None, carrying no filesystem path),
            `session_indeterminate` (bool - true while a swap was aborted
            part-way and no completed load has happened since; the drain loop
            refuses every command except this one, `get_addon_info` and the
            swap commands while it is set), `is_dirty` (bool - unsaved work a
            swap would destroy), and `libraries`, one
            `{"session_uid", "name", "filepath", "is_relative", "is_missing"}`
            entry per linked library (see `_library_summary` for which links are
            reported whole and which are reduced to a leaf).

        """
        return {
            **session_snapshot(),
            "is_dirty": bool(bpy.data.is_dirty),
            "libraries": [_library_summary(library) for library in bpy.data.libraries],
        }
