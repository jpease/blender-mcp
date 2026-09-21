"""
The keyframe-style vocabulary every keying handler validates and applies against.

Mirrors `blender_mcp.server.tools.key_style`, which spells the same values as typing
`Literal`s for the tool schemas. The add-on runs inside Blender and cannot import the server
package, so the *values* are necessarily duplicated; the validation and application logic is
not, which is what stops one keying domain from drifting to a different set of enum members
or a different rule for when a handle type is meaningful.
"""

INTERPOLATIONS = frozenset(
    {
        "CONSTANT",
        "LINEAR",
        "BEZIER",
        "SINE",
        "QUAD",
        "CUBIC",
        "QUART",
        "QUINT",
        "EXPO",
        "CIRC",
        "BACK",
        "BOUNCE",
        "ELASTIC",
    }
)
HANDLE_TYPES = frozenset({"FREE", "ALIGNED", "VECTOR", "AUTO", "AUTO_CLAMPED"})
EASINGS = frozenset({"AUTO", "EASE_IN", "EASE_OUT", "EASE_IN_OUT"})


def validate_key_style(interpolation, handle_left, handle_right, easing=None):
    """
    Reject an unsupported key style before anything is keyed.

    Called before an action is created or a bone is moved, so a misspelled mode is a refusal
    rather than a half-authored frame.

    Args:
        interpolation: A member of `INTERPOLATIONS`.
        handle_left: A member of `HANDLE_TYPES`.
        handle_right: A member of `HANDLE_TYPES`.
        easing: A member of `EASINGS`, or None to leave each key's easing as Blender set it.

    Raises:
        ValueError: Naming the offending argument and what it accepts.

    """
    if interpolation not in INTERPOLATIONS:
        raise ValueError(f"Unsupported interpolation: {interpolation}; expected one of {sorted(INTERPOLATIONS)}")
    for label, value in (("handle_left", handle_left), ("handle_right", handle_right)):
        if value not in HANDLE_TYPES:
            raise ValueError(f"Unsupported {label}: {value}; expected one of {sorted(HANDLE_TYPES)}")
    if easing is not None and easing not in EASINGS:
        raise ValueError(f"Unsupported easing: {easing}; expected one of {sorted(EASINGS)}")


def style_point(point, interpolation, *, handle_left, handle_right, easing=None):
    """
    Apply one key's style to one keyframe point.

    Handle types are only written for BEZIER, because that is the only interpolation whose
    segment a handle shapes; writing them under LINEAR would record a state the graph editor
    never shows. Easing is written whenever it is given: Blender accepts it on any key, and it
    only changes evaluation for the SINE..ELASTIC equations.

    Args:
        point: A `bpy.types.Keyframe`.
        interpolation: The interpolation to set, already validated.
        handle_left: The left handle type, applied only under BEZIER.
        handle_right: The right handle type, applied only under BEZIER.
        easing: The easing direction, or None to leave the point's easing alone.

    """
    point.interpolation = interpolation
    if interpolation == "BEZIER":
        point.handle_left_type = handle_left
        point.handle_right_type = handle_right
    if easing is not None:
        point.easing = easing
