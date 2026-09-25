"""
Resolve, validate, patch, restore and serialize the RNA properties a simulation handler writes.

Cloth, liquid and rigid-body handlers each carried their own copy of these helpers, and the
copies drifted. Each function here is the one version they now share; where the copies
disagreed, its docstring says which behaviour was kept and why.
"""

import contextlib
import math

import bpy


def get_object(name, types=None):
    """
    Return the object named exactly `name`.

    Raises:
        ValueError: No object has that name, or its type is not one of `types`.

    """
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise ValueError(f"Object not found: {name}")
    if types and obj.type not in types:
        raise ValueError(f"Object '{name}' must be one of {sorted(types)} (type={obj.type})")
    return obj


def finite(value, label):
    """
    Refuse a NaN or infinite number, alone or in a list or tuple, and return `value` unchanged.

    This guards a value whose RNA type is not known yet, so a bool, an enum identifier or a
    string passes through for `validate_rna_value` to judge. It is not the `_finite` in
    `retopology/_shared.py` or `character_rigging/primitives.py`: those coerce to float, which
    would turn True into 1.0 and refuse every enum identifier, so they stay a separate contract.

    Raises:
        ValueError: A number, or any item of a list or tuple, is not finite.

    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    if isinstance(value, (list, tuple)) and not all(
        isinstance(item, (int, float)) and math.isfinite(item) for item in value
    ):
        raise ValueError(f"{label} must contain only finite numbers")
    return value


def rna_property(owner, name):
    """
    Return the RNA definition of `owner.<name>`.

    Raises:
        ValueError: This Blender build has no such property, or it is read-only.

    """
    prop = owner.bl_rna.properties.get(name)
    if prop is None or prop.is_readonly:
        raise ValueError(f"Blender {bpy.app.version_string} does not expose writable {type(owner).__name__}.{name}")
    return prop


def validate_rna_value(owner, name, value):
    """
    Check `value` against the RNA definition of `owner.<name>` without writing it.

    A number must be finite, a scalar FLOAT or INT inside the hard range, an ENUM one of its
    identifiers, and an array exactly the property's length. The range test is one chained
    comparison, the form liquid's copy used, so a NaN fails it by itself rather than only
    because `finite` ran first; cloth's two-sided `<`/`>` form let a NaN through.

    Raises:
        ValueError: The property is unavailable or `value` does not fit it.

    """
    prop = rna_property(owner, name)
    finite(value, name)
    is_array = getattr(prop, "is_array", False)
    if prop.type in {"FLOAT", "INT"} and not is_array and not (prop.hard_min <= value <= prop.hard_max):
        raise ValueError(f"{name}={value} is outside Blender's RNA range [{prop.hard_min}, {prop.hard_max}]")
    if prop.type == "ENUM" and value not in {item.identifier for item in prop.enum_items}:
        raise ValueError(f"Invalid {name}: {value}")
    if is_array and len(value) != prop.array_length:
        raise ValueError(f"{name} must contain {prop.array_length} values")
    return value


def patch_rna(owner, patch, allowed):
    """
    Write an allowlisted patch to `owner` and report every property it names.

    Every value is validated before the first write, and a write that fails puts back what
    this call already wrote, so a refused patch leaves `owner` as it found it.

    Returns:
        dict: `{name: {"old": ..., "new": ...}}` in `serialize` form, one entry per patched
        property whether or not its value changed.

    Raises:
        ValueError: `patch` names a property outside `allowed`, or a value fails validation.

    """
    patch = patch or {}
    unknown = set(patch) - allowed
    if unknown:
        raise ValueError(f"Unsupported properties: {sorted(unknown)}")
    validated = {name: validate_rna_value(owner, name, value) for name, value in patch.items()}
    old = {name: serialize(getattr(owner, name)) for name in validated}
    try:
        for name, value in validated.items():
            setattr(owner, name, value)
    except Exception:
        # The assignment's own exception is re-raised below and is the one that explains the
        # failure; a property that also refuses its old value keeps whatever the patch left.
        for name, value in old.items():
            with contextlib.suppress(Exception):
                setattr(owner, name, value)
        raise
    return {name: {"old": old[name], "new": serialize(getattr(owner, name))} for name in validated}


def restore_rna(owner, changes):
    """
    Put back the `old` value of every entry in a `patch_rna`-shaped report, except pointers.

    `changes` holds reply-form values, and a POINTER's reply form is its datablock's name,
    which Blender refuses to assign to a pointer (TypeError), while a recorded None would clear
    the pointer. Callers that record a pointer in a report - cloth's collision and effector
    `collection` - keep the datablock itself and assign it back straight after this call.
    Cloth's copy skipped that one entry by name and liquid's attempted the write; skipping by
    RNA type is the rule both needed, without this module knowing a caller's key.

    Runs while the caller's exception propagates, so a property that refuses its old value is
    left as it is rather than replacing that exception with its own.
    """
    for name, values in changes.items():
        prop = owner.bl_rna.properties.get(name)
        if prop is not None and prop.type == "POINTER":
            continue
        with contextlib.suppress(Exception):
            setattr(owner, name, values["old"])


def serialize(value, *, typed_ids=False):
    """
    Convert an RNA value to its JSON reply form.

    Anything with a `name` (a datablock) becomes that name, or `{"id_type", "name"}` with
    `typed_ids`: liquid replies have always carried the type and cloth and rigid-body replies
    have not, and making them agree would change a reply. Anything iterable becomes a list,
    recursively, and everything else its `str`. Iteration is attempted rather than gated on
    `__iter__`: mathutils Vector, Euler, Quaternion and Matrix iterate through the sequence
    protocol and define no `__iter__`, so rigid body's gated copy returned a vector as its repr.

    Returns:
        The JSON-safe value.

    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "name"):
        return {"id_type": type(value).__name__, "name": value.name} if typed_ids else value.name
    try:
        return [serialize(item, typed_ids=typed_ids) for item in value]
    except TypeError:
        return str(value)


def read_fields(owner, fields, *, typed_ids=False):
    """
    Return each of `fields` that `owner` has, sorted by name, in `serialize` form.

    Returns:
        dict: Field name to serialized value; a name `owner` lacks is omitted.

    """
    return {
        name: serialize(getattr(owner, name), typed_ids=typed_ids) for name in sorted(fields) if hasattr(owner, name)
    }
