"""
Resolve a client-supplied object name to exactly one object, deterministically.

After a library override (Route C) a shot holds two objects with one name: the
local, editable override and the linked original it was made from.
`bpy.data.objects.get(name)` returns the local one, and the `(name, None)` key
returns it explicitly - both measured on Blender 5.2.2 by
`scripts/blender_probes/shot_directories_and_override_names.py` section B,
which asserts each. The first is list ordering rather than a stated API
contract, which is the whole reason the key is used here: the same probe
measures that with two *linked* objects sharing a name and no local one,
`get(name)` follows `Main` insertion (link) order - the library linked first,
**not** the one whose name sorts first, which an earlier revision of this
docstring asserted and the probe disproved.

The tuple key's behaviour is measured, not documented: `bpy.data.objects.get`'s
own `__doc__` types `key` as `str`, and the pinned stubs agree, so the probe is
the instrument for it.

The rule here is explicit: the local object wins, looked up with the
`(name, None)` key, because it is the only one of the pair a tool can edit;
otherwise a single linked object is returned; several linked objects with the
name and no local one are refused rather than guessed between.

**Not the same rule as `handlers/linking.resolve_unique_name`, deliberately.**
That one refuses *any* ambiguous name and tells the caller to pass a
`session_uid`, which is right for the linking commands, because they take one.
The tools reached from here take no uid to pass, and only the local object is
editable, so a refusal on every overridden name would make them unusable; the
refusal this module does raise points at `create_override`, which is the
uid-based route back.

Free of `bpy` so it is testable without Blender: callers pass `bpy.data.objects`.
"""

from .text_hygiene import client_safe_name_leaf, client_safe_text


def find_object(objects: object, name: str) -> object | None:
    """
    Return the object a name means, preferring the local one over linked ones.

    Args:
        objects: `bpy.data.objects`, or any mapping with `get` and `values`.
        name: The object name the client sent.

    Returns:
        object | None: The local object with that name; else the one linked
        object with it; else None.

    Raises:
        ValueError: When no local object has the name and more than one linked
            object does.

    """
    local = objects.get((name, None))  # type: ignore[attr-defined]
    if local is not None:
        return local
    matches = [obj for obj in objects.values() if obj.name == name]  # type: ignore[attr-defined]
    if len(matches) > 1:
        libraries = sorted({client_safe_name_leaf(getattr(obj.library, "name", "")) for obj in matches})
        raise ValueError(
            f"object name {client_safe_text(name)!r} is linked from more than one library ({', '.join(libraries)}) "
            "and no local object has it; override the one to edit with create_override"
        )
    return matches[0] if matches else None
