"""
Describe several datablocks in one refusal, bounded, by name and `session_uid`.

Extracted from `handlers/linking.py`, which held the only copy while
`object_lookup.find_object` grew a second, weaker one: it applied the same name
allowlist and dropped the bound, so a shot linking N libraries turned one
refusal into an N-proportional string (measured at 19,933 B for 300 long names,
post-Phase-2 critic 3). Both now share this.

It lives here rather than in `handlers/linking.py` because `object_lookup` must
stay importable without Blender, and that module imports `bpy`. Nothing here
does: every field is read with `getattr`, so a plain stub exercises it.

`MAX_CANDIDATES` bounds the *list*; `text_hygiene` bounds each *name*. Both are
needed - the per-name bound alone still lets the count carry the payload.
"""

from collections.abc import Callable, Iterable

from .text_hygiene import client_safe_name_leaf, client_safe_text

# Enough to choose from, few enough that the refusal cannot be a channel: the
# exact total is reported beside the list, so nothing is hidden by the cap.
MAX_CANDIDATES = 10


def display_name(datablock: object) -> str:
    """
    Reduce a datablock name for a client, holding a library's name to the leaf rule.

    `Library.name` accepts a whole path (TASK_STATE T3-15), so it goes through
    `client_safe_name_leaf` as it does in `handlers/file_lifecycle._library_summary`
    (no filesystem call on author-chosen text); any other ID name is file-author
    text and goes through `client_safe_text`. The `id_type` discriminator is
    `'LIBRARY'` on a real `bpy.types.Library`, measured on 5.2.2 by
    `scripts/blender_probes/shot_directories_and_override_names.py` section B,
    because taking the wrong branch here would publish a path unreduced.

    Args:
        datablock: The datablock.

    Returns:
        str: The publishable name.

    """
    name = getattr(datablock, "name", "")
    if getattr(datablock, "id_type", None) == "LIBRARY":
        return client_safe_name_leaf(name)
    return client_safe_text(name)


def session_uid_of(datablock: object) -> int | None:
    """
    Read a possibly-absent datablock's uid.

    Args:
        datablock: A datablock or None.

    Returns:
        int | None: Its `session_uid`.

    """
    return getattr(datablock, "session_uid", None) if datablock is not None else None


def describe_candidates(datablocks: Iterable[object]) -> str:
    """
    Describe datablocks by name and uid, for a refusal that must let the client choose.

    The uid is what makes the list actionable: several candidates can reduce to
    one display name - identically named objects do so by construction, and
    hostile names all reduce to `text_hygiene`'s sentinel - and a refusal naming
    one thing twice tells the client nothing to act on.

    Names are reduced by `display_name`, which decides per datablock from
    `id_type`. A caller that already knows its candidates are libraries should
    use `describe_library_candidates` instead, so the leaf rule cannot be missed
    by a datablock that does not report the discriminator.

    Args:
        datablocks: The candidates.

    Returns:
        str: `'Name' (session_uid N)` entries, at most `MAX_CANDIDATES` of them,
        with a `, and N more` tail when there were more.

    """
    return _describe(datablocks, display_name)


def describe_library_candidates(libraries: Iterable[object]) -> str:
    """
    Describe libraries, holding every name to the leaf rule unconditionally.

    Same bound, uid and total as `describe_candidates`, but the name reduction is
    not conditional: `Library.name` can be an absolute path, a traversal or a
    UNC path (TASK_STATE T3-15), and a caller that knows it holds libraries must
    not publish one through the weaker branch because a stub or a future
    datablock failed to report `id_type == 'LIBRARY'`. Chosen after the shared
    helper's first use in `object_lookup` published `'/studio/a/canon.blend'`
    verbatim against a stub that had no `id_type`.

    Args:
        libraries: The candidate libraries.

    Returns:
        str: As `describe_candidates`, with every name reduced to a leaf.

    """
    return _describe(libraries, _library_leaf)


def _library_leaf(library: object) -> str:
    """
    Reduce a library's name to a leaf, without consulting `id_type`.

    Args:
        library: The library.

    Returns:
        str: The admissible leaf, or `text_hygiene`'s sentinel.

    """
    return client_safe_name_leaf(getattr(library, "name", ""))


def _describe(datablocks: Iterable[object], namer: Callable[[object], str]) -> str:
    """
    Format a bounded candidate list, naming each datablock with `namer`.

    Args:
        datablocks: The candidates.
        namer: Turns one datablock into its publishable name.

    Returns:
        str: The bounded, uid-carrying list with its `, and N more` tail.

    """
    items = list(datablocks)
    shown = ", ".join(f"{namer(d)!r} (session_uid {session_uid_of(d)})" for d in items[:MAX_CANDIDATES])
    return shown if len(items) <= MAX_CANDIDATES else f"{shown}, and {len(items) - MAX_CANDIDATES} more"
