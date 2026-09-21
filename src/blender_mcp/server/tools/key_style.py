"""
Shared keyframe-style vocabulary for every tool that inserts or restyles keys.

Four tool modules key F-Curves - `animation.py`, `object_animation.py`, `camera/animation.py`
and `character_rigging/posing.py` - and each used to respell its own `Literal`s, which is how
the pose tools ended up carrying three of Blender's thirteen interpolation modes while the
camera tools carried the same three under a different spelling. The values here are Blender's
real `Keyframe.interpolation`, `Keyframe.handle_left_type` and `Keyframe.easing` enums.

The add-on mirrors these as frozensets in `bundled/addon/handlers/key_style.py`: add-on code
cannot import the server package, so the values are duplicated there and the validation and
application logic is not.
"""

from typing import Literal

# CONSTANT/LINEAR/BEZIER shape the segment directly; SINE..ELASTIC are Blender's easing
# equations, whose direction is chosen by `Easing`.
Interpolation = Literal[
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
]
HandleType = Literal["FREE", "ALIGNED", "VECTOR", "AUTO", "AUTO_CLAMPED"]
Easing = Literal["AUTO", "EASE_IN", "EASE_OUT", "EASE_IN_OUT"]
