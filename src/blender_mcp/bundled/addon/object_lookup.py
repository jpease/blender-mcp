"""
Resolve a client-supplied object name to exactly one object, deterministically.

After a library override (Route C) a shot holds two objects with one name: the
local, editable override and the linked original it was made from. Measured on
Blender 5.2.2, `bpy.data.objects.get(name)` returns the local one because
Blender keeps local IDs ahead of linked ones in `Main`, but that is list
ordering, not an API contract, and when two *linked* objects share a name and
no local one exists it picks whichever library sorted first.

The rule here is explicit instead: the local object wins, looked up with the
documented `(name, None)` key, because it is the only one of the pair a tool
can edit; otherwise a single linked object is returned; several linked objects
with the name and no local one are refused rather than guessed between.

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
