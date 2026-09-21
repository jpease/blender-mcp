"""
The keyframe-style vocabulary every keying handler validates and applies against.

Mirrors `blender_mcp.server.tools.key_style`, which spells the same values as typing
`Literal`s for the tool schemas. The add-on runs inside Blender and cannot import the server
package, so the *values* are necessarily duplicated; the validation and application logic is
not, which is what stops one keying domain from drifting to a different set of enum members
or a different rule for when a handle type is meaningful.
"""

from typing import NamedTuple

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


class KeyStyle(NamedTuple):
    """
    How one call shapes every key it writes: the four values that always travel together.

    They are validated together before an action exists and applied together to each point, so
    a bare 4-tuple splatted into three functions made argument order the whole contract - and
    an easing silently arriving as a handle type is not a type error anywhere. Naming them
    once is what makes a caller's mistake a refusal at the boundary instead.
    """

    interpolation: str
    handle_left: str = "AUTO_CLAMPED"
    handle_right: str = "AUTO_CLAMPED"
    easing: str | None = None

    def validate(self) -> None:
        """
        Reject an unsupported style before anything is keyed.

        Called before an action is created or a bone is moved, so a misspelled mode is a
        refusal rather than a half-authored frame.

        Raises:
            ValueError: Naming the offending field and what it accepts.

        """
        if self.interpolation not in INTERPOLATIONS:
            raise ValueError(
                f"Unsupported interpolation: {self.interpolation}; expected one of {sorted(INTERPOLATIONS)}"
            )
        for label, value in (("handle_left", self.handle_left), ("handle_right", self.handle_right)):
            if value not in HANDLE_TYPES:
                raise ValueError(f"Unsupported {label}: {value}; expected one of {sorted(HANDLE_TYPES)}")
        if self.easing is not None and self.easing not in EASINGS:
            raise ValueError(f"Unsupported easing: {self.easing}; expected one of {sorted(EASINGS)}")


def style_point(point, style):
    """
    Apply one key style to one keyframe point.

    Handle types are only written for BEZIER, because that is the only interpolation whose
    segment a handle shapes; writing them under LINEAR would record a state the graph editor
    never shows. Easing is written whenever it is given: Blender accepts it on any key, and it
    only changes evaluation for the SINE..ELASTIC equations.

    Args:
        point: A `bpy.types.Keyframe`.
        style: The already-validated `KeyStyle` to write.

    """
    point.interpolation = style.interpolation
    if style.interpolation == "BEZIER":
        point.handle_left_type = style.handle_left
        point.handle_right_type = style.handle_right
    if style.easing is not None:
        point.easing = style.easing
