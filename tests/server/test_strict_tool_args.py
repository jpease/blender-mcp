"""
Regression coverage for the strict top-level tool arguments `_strict_args` installs.

Every tool schema this server advertises carries `additionalProperties: false`, but the SDK
generates its argument models from `ArgModelBase`, whose config never sets `extra` - so pydantic
defaulted to `extra="ignore"` and a key the handler does not declare was dropped mid-call, leaving
the tool to run as though it had never been sent. `_strict_args` flips that at package-import time
by reaching into `tool.fn_metadata.arg_model`, which is SDK internals, so these tests assert the
observable behaviour of the registered tool objects rather than the attribute the fix writes: an
SDK upgrade that renames those attributes or regenerates the models has to fail here.

The same pass refuses a non-finite float in a top-level argument. `StrictModel` already refused
NaN and infinity inside payload models, but a bare `float`, a coordinate tuple or a list of them
was validated by the SDK's model alone, and `create_primitive_object(location=[nan, 0, 0])`
reached the add-on, which forwarded it into Blender.

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

# Every way a top-level argument in this catalog holds a float, one argument each: bare, optional,
# in a tuple, in an optional tuple, in a list of tuples and in an optional one. The sweep must reach
# every shape, or a schema walk that stopped recognising one would pass by probing none of it.
FLOAT_ARGUMENT_SHAPES = [
    ["create_primitive_object", "size"],
    ["configure_camera_dof", "focus_distance"],
    ["create_primitive_object", "location"],
    ["create_primitive_object", "dimensions"],
    ["build_quad_patch", "corners"],
    ["create_camera_path_rig", "path_points"],
]

# Reports what one server process exposes: the tools it registered, which of them still swallow
# UNKNOWN_KEY, every top-level float argument and which of them still accept each non-finite
# value, and - when the camera and mesh bundles are in the selection - named calls: the malformed
# camera call from the reported incident and a well-formed one, and `create_primitive_object` run
# through its registered tool with a non-finite vector and with a finite one, against a recording
# connection. Run as source in a child process so nothing in this module imports a tool module.
_PROBE = """
import asyncio
import json
import math

from pydantic import ValidationError

from blender_mcp.server import mcp
from blender_mcp.server.tools import _dispatch

UNKNOWN_KEY = "definitely_not_a_tool_parameter"
NON_FINITE = {"nan": math.nan, "inf": math.inf, "-inf": -math.inf}
tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}


def refusals(model, payload):
    try:
        model.model_validate(payload)
    except ValidationError as error:
        return [[detail["type"], list(detail["loc"])] for detail in error.errors()]
    return []


def float_samples(schema, value):
    # One value per way this property holds a float directly - bare, optional, or inside a tuple or
    # list - with `value` in every float slot. A nested model is a `$ref`: its own config covers it.
    if "anyOf" in schema:
        return [sample for branch in schema["anyOf"] for sample in float_samples(branch, value)]
    if schema.get("type") == "number":
        return [value]
    if schema.get("type") != "array":
        return []
    slots = schema.get("prefixItems") or [schema.get("items", {})] * max(1, schema.get("minItems", 1))
    filled = [float_samples(slot, value) for slot in slots]
    return [[options[0] if options else None for options in filled]] if any(filled) else []


def accepts(tool_name, argument, value):
    model = tools[tool_name].fn_metadata.arg_model
    schema = model.model_json_schema()["properties"][argument]
    return any(
        not any(kind == "finite_number" and loc[0] == argument for kind, loc in refusals(model, {argument: sample}))
        for sample in float_samples(schema, value)
    )


float_arguments = [
    [tool_name, argument]
    for tool_name, tool in sorted(tools.items())
    for argument, schema in tool.fn_metadata.arg_model.model_json_schema().get("properties", {}).items()
    if float_samples(schema, 0.0)
]
report = {
    "names": sorted(tools),
    "accepts_unknown": sorted(
        name
        for name, tool in tools.items()
        if ["extra_forbidden", [UNKNOWN_KEY]] not in refusals(tool.fn_metadata.arg_model, {UNKNOWN_KEY: 1})
    ),
    "float_arguments": float_arguments,
    "accepts_non_finite": {
        label: [pair for pair in float_arguments if accepts(*pair, value)] for label, value in NON_FINITE.items()
    },
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
if "create_primitive_object" in tools:
    dispatched = []

    class RecordingConnection:
        def send_command(self, command, params):
            dispatched.append(command)
            return {"name": "Cube"}

    _dispatch.get_blender_connection = RecordingConnection

    def primitive_call(**vectors):
        # The registered tool's own run - argument validation, then the handler - which is where
        # every MCP call to it ends. Any failure is recorded, never raised, so a revert that lets
        # the call through cannot take the rest of this report down with it.
        dispatched.clear()
        try:
            asyncio.run(tools["create_primitive_object"].run({"primitive_type": "CUBE", **vectors}))
        except Exception as error:
            return {"refused_by": type(error.__cause__ or error).__name__, "dispatched": list(dispatched)}
        return {"refused_by": None, "dispatched": list(dispatched)}

    report["primitive_calls"] = {
        "finite": primitive_call(location=[1.0, 2.0, 3.0], rotation=[0.0, 0.5, 0.0], dimensions=[1.0, 2.0, 3.0]),
        **{
            argument: [primitive_call(**{argument: [1.0, value, 1.0]}) for value in NON_FINITE.values()]
            for argument in ("location", "rotation", "dimensions")
        },
    }
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


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_every_float_argument_in_the_catalog_refuses_a_non_finite_value(value: str) -> None:
    """
    No top-level float argument of any tool, in any shape, may carry NaN or infinity past validation.

    Offenders are listed by tool and argument. A bounded argument may already refuse one of the
    three by its bound; the refusal asked for is the non-finite one, so no argument passes on a
    bound that only happens to exclude the value tried.
    """
    report = _probe(ALL_SENTINEL)
    assert [shape for shape in FLOAT_ARGUMENT_SHAPES if shape not in report["float_arguments"]] == []
    assert report["accepts_non_finite"][value] == []


@pytest.mark.parametrize("argument", ["location", "rotation", "dimensions"])
def test_create_primitive_object_refuses_a_non_finite_vector_before_dispatch(argument: str) -> None:
    """
    The reported case, run through its registered tool: NaN or infinity in a vector never reaches Blender.

    The finite call through the same recording connection is what makes an empty dispatch list
    mean the refusal, rather than a connection that was never consulted.
    """
    calls = _probe(ALL_SENTINEL)["primitive_calls"]
    assert calls["finite"] == {"refused_by": None, "dispatched": ["create_primitive"]}
    assert calls[argument] == [{"refused_by": "ValidationError", "dispatched": []}] * 3
