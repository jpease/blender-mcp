"""
What this session authored: the datablocks its own commands created, oldest first.

`save_shot` writes this list into the `.blend` as part of the provenance block, so a recipient
can see which datablocks the add-on made rather than inferring it from names. Entries arrive
already sanitized from `Transaction.created_datablocks()`, which is also where linked
datablocks were excluded, so nothing foreign enters the ledger or the file.

Bounded: a long session must not grow an unbounded list, and an unbounded list would not fit
in a saved file anyway. On overflow the oldest record is dropped and `was_truncated()` starts
reporting True, so the file never claims a complete history it does not have.

Process-wide, mutated only on Blender's main thread (the command queue drains there), so no
lock. bpy-free: `transaction.py` records into it and `handlers/file_lifecycle.py` reads it.
"""

from collections.abc import Iterable, Mapping

MAX_TRACKED_AUTHORED = 500


class _Ledger:
    """
    The one mutable cell the authored list lives in.

    One insertion-ordered dict, not a list plus a membership set: the two had to be
    evicted from together, and "the keys mirror the entries" was an invariant the
    code had to keep by hand rather than one the data structure enforced. An
    instance rather than module globals, matching `session._StateStore` and
    `transaction._DispatchState`, so a writer needs no `global` statement.

    Attributes:
        entries: `(collection, name)` -> None, oldest first. The same pair twice is
            one datablock touched twice, not two datablocks.
        truncated: True once eviction dropped an older entry.

    """

    __slots__ = ("entries", "truncated")

    def __init__(self) -> None:
        """Start empty, claiming a complete history because it has one."""
        self.entries: dict[tuple[str, str], None] = {}
        self.truncated = False


_LEDGER = _Ledger()


def record(entries: Iterable[Mapping[str, str]]) -> None:
    """
    Remember datablocks this session created, oldest first.

    Args:
        entries: `{"collection": ..., "name": ...}` records, already sanitized.

    """
    for entry in entries:
        key = (str(entry.get("collection", "")), str(entry.get("name", "")))
        if key in _LEDGER.entries:
            continue
        if len(_LEDGER.entries) >= MAX_TRACKED_AUTHORED:
            # Evicting the key as well as the record: keeping it would suppress a
            # later datablock that legitimately reuses the freed name, forever.
            del _LEDGER.entries[next(iter(_LEDGER.entries))]
            _LEDGER.truncated = True
        _LEDGER.entries[key] = None


def snapshot() -> list[dict[str, str]]:
    """
    Return the ledger, oldest first.

    Returns:
        list[dict[str, str]]: A copy, so a caller cannot mutate the ledger by holding it.

    """
    return [{"collection": collection, "name": name} for collection, name in _LEDGER.entries]


def was_truncated() -> bool:
    """
    Report whether eviction dropped older entries.

    Returns:
        bool: True once anything was evicted.

    """
    return _LEDGER.truncated


def clear() -> None:
    """Forget everything: the database this ledger described is gone."""
    _LEDGER.entries.clear()
    _LEDGER.truncated = False
