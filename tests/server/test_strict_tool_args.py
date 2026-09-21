"""
Regression coverage for the strict top-level tool arguments `_strict_args` installs.

Every tool schema this server advertises carries `additionalProperties: false`, but the SDK
generates its argument models from `ArgModelBase`, whose config never sets `extra` - so pydantic
defaulted to `extra="ignore"` and a key the handler does not declare was dropped mid-call, leaving
the tool to run as though it had never been sent. `_strict_args` flips that at package-import time
by reaching into `tool.fn_metadata.arg_model`, which is SDK internals, so these tests assert the
observable behaviour of the registered tool objects rather than the attribute the fix writes: an
SDK upgrade that renames those attributes or regenerates the models has to fail here.

Measured out-of-process, like `test_bundles.py`: the catalog is registered as an import side
effect of the selected `BLENDER_MCP_TOOLSETS`, so importing a tool module in this process would
both pin the selection for every later test and register tools after the hardening pass had
already run. A child process per selection is the only way to see the surface a real server
process exposes.
"""

import functools
import json
import os
import subprocess
import sys

import pytest

from blender_mcp.server.bundles import ALL_SENTINEL, TOOLSETS_ENV_VAR

# A key no handler can ever declare, fed to every registered tool. Required-field errors come back
# alongside it and are ignored; the only question asked is whether the unknown key was refused.
UNKNOWN_KEY = "definitely_not_a_tool_parameter"

# Reports what one server process exposes: the tools it registered, which of them still swallow
# UNKNOWN_KEY, and - when the camera bundle is in the selection - two named calls, one malformed
# the way the reported incident was and one well-formed. Run as source in a child process so
# nothing in this module imports a tool module itself.
_PROBE = """
import json

from pydantic import ValidationError

from blender_mcp.server import mcp

UNKNOWN_KEY = "definitely_not_a_tool_parameter"
tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}


def refusals(model, payload):
    try:
        model.model_validate(payload)
    except ValidationError as error:
        return [[detail["type"], list(detail["loc"])] for detail in error.errors()]
    return []


report = {
    "names": sorted(tools),
    "accepts_unknown": sorted(
        name
        for name, tool in tools.items()
        if ["extra_forbidden", [UNKNOWN_KEY]] not in refusals(tool.fn_metadata.arg_model, {UNKNOWN_KEY: 1})
    ),
}
if "point_camera_at" in tools:
    report["misspelled_aim"] = refusals(
        tools["point_camera_at"].fn_metadata.arg_model,
        {"scene_name": "S", "camera_name": "C", "target_point": [0, 0, 0], "camera_locaiton": [1, 2, 3]},
    )
    report["wrongly_nested_optics"] = refusals(
        tools["configure_camera"].fn_metadata.arg_model,
        {"camera_name": "C", "patch": {"focal_length": 28.0}},
    )
    accepted = tools["configure_camera"].fn_metadata.arg_model.model_validate(
        {"camera_name": "C", "optics": {"lens": 28.0}}
    )
    report["optics"] = {"model": type(accepted.optics).__name__, "lens": accepted.optics.lens}
print(json.dumps(report))
"""


@functools.cache
def _probe(raw_value: str | None) -> dict:
    """
    Run the probe in a fresh server process for one BLENDER_MCP_TOOLSETS value.

    The variable is stripped from the inherited environment first, so a developer's own export
    cannot change which catalog is measured. Cached because each selection costs a full import.

    Args:
        raw_value: The BLENDER_MCP_TOOLSETS value to set, or None to leave it unset.

    Returns:
        dict: The child's decoded report.

    """
    env = {key: value for key, value in os.environ.items() if key != TOOLSETS_ENV_VAR}
    if raw_value is not None:
        env[TOOLSETS_ENV_VAR] = raw_value
    result = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True, text=True, check=True, env=env)
    return json.loads(result.stdout)


def test_a_misspelled_argument_is_refused_instead_of_dropped() -> None:
    """
    `camera_locaiton` is not `camera_location`, and the difference has to reach the caller.

    This is the reported failure shape: a plausible-looking key vanished during validation, the
    tool ran with that intent missing, and the envelope came back clean.
    """
    errors = _probe(ALL_SENTINEL)["misspelled_aim"]
    assert ["extra_forbidden", ["camera_locaiton"]] in errors, errors


def test_the_wrongly_nested_patch_from_the_incident_is_refused() -> None:
    """
    `configure_camera(patch={"focal_length": 28.0})` used to call Blender with `optics=None`.

    Nothing said so: the camera stayed at 50mm and only the renders showed it, several iterations
    later. The malformed key must now name itself in a validation error before Blender is touched.
    """
    errors = _probe(ALL_SENTINEL)["wrongly_nested_optics"]
    assert ["extra_forbidden", ["patch"]] in errors, errors


def test_a_valid_call_still_validates_through_its_nested_model() -> None:
    """Refusing extras must not cost the well-formed call its nested patch model."""
    assert _probe(ALL_SENTINEL)["optics"] == {"model": "CameraOpticsPatch", "lens": 28.0}


@pytest.mark.parametrize("selection", [ALL_SENTINEL, None])
def test_every_registered_tool_refuses_an_unknown_argument(selection: str | None) -> None:
    """
    No tool in the process may swallow a key it does not declare - not the whole catalog, not core.

    Offenders are listed rather than counted: an SDK upgrade that restores the permissive default,
    or a registration path that runs after the hardening pass, names itself here.
    """
    report = _probe(selection)
    assert report["names"], "a server process must register tools for this assertion to mean anything"
    assert report["accepts_unknown"] == []


def test_the_full_catalog_is_what_the_hardening_was_measured_against() -> None:
    """`all` must really widen the surface, or the catalog-wide assertion above proves only core."""
    assert set(_probe(None)["names"]) < set(_probe(ALL_SENTINEL)["names"])
