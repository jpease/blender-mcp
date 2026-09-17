"""
Describe several datablocks in one refusal, bounded, by name and `session_uid`.

Without the bound, a refusal grows with the number of linked libraries. It lives
apart from `handlers/linking.py`, which imports `bpy`, so `object_lookup` stays
importable without Blender; fields are read with `getattr` so a stub works.
`text_hygiene` bounds each name; `MAX_CANDIDATES` bounds how many.
"""

from collections.abc import Callable, Iterable

from .text_hygiene import client_safe_name_leaf, client_safe_text

# Enough to choose from; the tail still counts the rest, so the cap hides nothing.
MAX_CANDIDATES = 10


def display_name(datablock: object) -> str:
    """
    Reduce a datablock name for a client, holding a library's name to the leaf rule.

    `Library.name` can hold a whole path, which the other branch would publish
    unreduced.

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

    The uid tells apart candidates whose names reduce to the same text, such as
    hostile names that all become `text_hygiene`'s sentinel. For known libraries,
    use `describe_library_candidates`, which does not trust `id_type`.

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

    A library that fails to report `id_type == 'LIBRARY'` would otherwise have its
    name, possibly a full path, published whole.

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
