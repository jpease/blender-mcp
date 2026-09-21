"""
The keyframe-style vocabulary is stated twice and must mean the same thing twice.

The add-on runs inside Blender and cannot import the server package, so
`bundled/addon/handlers/key_style.py` repeats the enum members that
`server/tools/key_style.py` advertises to clients. A member added to one side only is not a
type error and not a formatting error: it is a tool whose schema offers a mode the add-on
refuses at the far end of the socket. These tests are what makes that a failure here instead.
"""

import typing

import pytest

from conftest import load_addon_source_module

from blender_mcp.server.tools.key_style import Easing, HandleType, Interpolation

key_style = load_addon_source_module("handlers/key_style.py", "addon_key_style")


class _Point:
    """Stand-in for `bpy.types.Keyframe`, recording exactly what `style_point` writes."""

    def __init__(self) -> None:
        self.interpolation: str | None = None
        self.handle_left_type: str | None = None
        self.handle_right_type: str | None = None
        self.easing: str | None = None


@pytest.mark.parametrize(
    ("advertised", "enforced"),
    [
        (Interpolation, "INTERPOLATIONS"),
        (HandleType, "HANDLE_TYPES"),
        (Easing, "EASINGS"),
    ],
)
def test_the_advertised_vocabulary_is_the_one_the_addon_accepts(advertised: object, enforced: str) -> None:
    """A mode in the schema that the add-on rejects is a call that fails after the round trip."""
    assert set(typing.get_args(advertised)) == set(getattr(key_style, enforced))


def test_bezier_is_the_only_interpolation_that_records_handle_types() -> None:
    """A handle type shapes a Bézier segment; under any other mode it records state nothing shows."""
    bezier = _Point()
    key_style.style_point(bezier, key_style.KeyStyle("BEZIER", "VECTOR", "AUTO"))

    linear = _Point()
    key_style.style_point(linear, key_style.KeyStyle("LINEAR", "VECTOR", "AUTO"))

    assert (bezier.handle_left_type, bezier.handle_right_type) == ("VECTOR", "AUTO")
    assert (linear.handle_left_type, linear.handle_right_type) == (None, None)
    assert linear.interpolation == "LINEAR"


def test_easing_is_written_on_any_interpolation_and_omitted_when_unset() -> None:
    """Blender accepts easing on every key, so an easing request is never silently dropped."""
    eased = _Point()
    key_style.style_point(eased, key_style.KeyStyle("SINE", "AUTO", "AUTO", "EASE_IN_OUT"))

    untouched = _Point()
    key_style.style_point(untouched, key_style.KeyStyle("SINE", "AUTO", "AUTO"))

    assert eased.easing == "EASE_IN_OUT"
    assert untouched.easing is None


@pytest.mark.parametrize(
    ("style", "offender"),
    [
        (("SPLINE", "AUTO", "AUTO", None), "interpolation"),
        (("BEZIER", "SMOOTH", "AUTO", None), "handle_left"),
        (("BEZIER", "AUTO", "SMOOTH", None), "handle_right"),
        (("BEZIER", "AUTO", "AUTO", "EASE_MIDDLE"), "easing"),
    ],
)
def test_an_unsupported_style_is_refused_by_the_argument_that_is_wrong(
    style: tuple[str, str, str, str | None], offender: str
) -> None:
    """The refusal has to name the argument: four style arguments make "unsupported" unactionable."""
    with pytest.raises(ValueError, match=offender):
        key_style.KeyStyle(*style).validate()
