"""
Resolve a client-supplied object name to exactly one object.

After a library override a shot holds two objects with one name: the local,
editable override and the linked original. The local object wins, because it is
the only one a tool can edit; a single linked object is returned otherwise;
several linked objects with the name and no local one are refused.

The local object is fetched with the `(name, None)` key, not `get(name)`, whose
choice follows list order rather than an API contract: among linked objects it
returns the one linked first. The tuple key is undocumented as well: `get`
types `key` as `str`.

`handlers/linking.resolve_unique_name` refuses every ambiguous name instead.
The tools reached from here take no `session_uid`, so that rule would make every
overridden name unusable; this module's refusal points at `create_override`.

Free of `bpy` so it is testable without Blender: callers pass `bpy.data.objects`.
"""

from .candidates import describe_library_candidates
from .text_hygiene import client_safe_text


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
            object does. The message lists a bounded set of candidates by
            library and `session_uid`, and the true total.

    """
    local = objects.get((name, None))  # type: ignore[attr-defined]
    if local is not None:
        return local
    matches = [obj for obj in objects.values() if obj.name == name]  # type: ignore[attr-defined]
    if len(matches) > 1:
        libraries = [obj.library for obj in matches]
        raise ValueError(
            f"object name {client_safe_text(name)!r} is linked from more than one library "
            f"({len(matches)} of them: {describe_library_candidates(libraries)}) and no local object has it; "
            "override the one to edit with create_override"
        )
    return matches[0] if matches else None
