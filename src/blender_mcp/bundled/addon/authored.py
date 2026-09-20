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

_ENTRIES: list[dict[str, str]] = []
# The same (collection, name) pair twice is one datablock touched twice, not two datablocks.
_KEYS: set[tuple[str, str]] = set()
_TRUNCATED = False


def record(entries: Iterable[Mapping[str, str]]) -> None:
    """
    Remember datablocks this session created, oldest first.

    Args:
        entries: `{"collection": ..., "name": ...}` records, already sanitized.

    """
    global _TRUNCATED  # ruff: ignore[global-statement] - module-level ledger; a singleton class buys nothing
    for entry in entries:
        collection = str(entry.get("collection", ""))
        name = str(entry.get("name", ""))
        key = (collection, name)
        if key in _KEYS:
            continue
        if len(_ENTRIES) >= MAX_TRACKED_AUTHORED:
            oldest = _ENTRIES.pop(0)
            # Discard the evicted key too: keeping it would suppress a later datablock that
            # legitimately reuses the freed name, forever.
            _KEYS.discard((oldest["collection"], oldest["name"]))
            _TRUNCATED = True
        _ENTRIES.append({"collection": collection, "name": name})
        _KEYS.add(key)


def snapshot() -> list[dict[str, str]]:
    """
    Return the ledger, oldest first.

    Returns:
        list[dict[str, str]]: A copy, so a caller cannot mutate the ledger by holding it.

    """
    return [dict(entry) for entry in _ENTRIES]


def was_truncated() -> bool:
    """
    Report whether eviction dropped older entries.

    Returns:
        bool: True once anything was evicted.

    """
    return _TRUNCATED


def clear() -> None:
    """Forget everything: the database this ledger described is gone."""
    global _TRUNCATED  # ruff: ignore[global-statement] - see `record`
    _ENTRIES.clear()
    _KEYS.clear()
    _TRUNCATED = False
