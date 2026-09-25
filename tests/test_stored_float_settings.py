"""
A reused cloth bind is compared to the request across one float32 round trip.

Blender stores RNA floats as C floats. `create_cloth_proxy_rig` refuses to reuse
a bound Surface Deform modifier whose settings differ from the request, and it
compared `float(modifier.falloff)` to the requested value exactly. A request
carrying 4.1 stores 4.099999904632568, so re-running the identical request was
rejected as a mismatch.
"""

from __future__ import annotations

import struct
import sys

from types import ModuleType

import pytest

from conftest import load_addon


def _as_stored(value: float) -> float:
    """
    Round a Python float through float32, the way Blender's RNA storage does.

    Args:
        value: The value a request carried.

    Returns:
        float: What reading the property back yields.

    """
    return struct.unpack("f", struct.pack("f", value))[0]


@pytest.fixture
def proxy_rigs(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """
    Import the cloth proxy-rig handlers over a fake bpy.

    Args:
        monkeypatch: Fixture used to install the stubs.

    Returns:
        ModuleType: The `handlers.cloth.proxy_rigs` module.

    """
    addon, _bpy = load_addon(monkeypatch, data={})
    return sys.modules[f"{addon.__name__}.handlers.cloth.proxy_rigs"]


@pytest.mark.parametrize("requested", [4.1, 0.3, 12.7, 1e-3])
def test_a_stored_setting_matches_the_request_that_produced_it(proxy_rigs: ModuleType, requested: float) -> None:
    """Writing a value and reading it back must not read as a different setting."""
    assert proxy_rigs._matches_stored_float(_as_stored(requested), requested)


def test_the_float32_round_trip_is_what_made_exact_comparison_wrong(proxy_rigs: ModuleType) -> None:
    """The tolerance is not decoration: 4.1 does not survive the round trip."""
    stored = _as_stored(4.1)

    # The inexactness is the point being asserted, not an accident.
    assert stored != 4.1  # ruff: ignore[float-equality-comparison]
    assert proxy_rigs._matches_stored_float(stored, 4.1)


def test_a_genuinely_different_setting_is_still_a_mismatch(proxy_rigs: ModuleType) -> None:
    """The tolerance covers storage precision only, not a different requested value."""
    assert not proxy_rigs._matches_stored_float(_as_stored(4.1), 4.2)
    assert not proxy_rigs._matches_stored_float(_as_stored(4.1), 4.10001)
