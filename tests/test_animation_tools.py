# ruff: file-ignore[yoda-conditions]
"""Schema, registration, and forwarding tests for generic animation tools."""

import asyncio
import types

import pytest

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError
from test_mutation_transaction import _load_addon

from blender_mcp.server.tools import _dispatch, animation

ANIMATION_COMMANDS = {
    "inspect_animation",
    "manage_animation_action",
    "edit_keyframes",
    "bake_evaluated_animation",
    "manage_nla_tracks",
}


class _Connection:
    def __init__(self) -> None:
        self.calls = []

    def send_command(self, command, params):
        self.calls.append((command, params))
        target = params["target"]
        return {"changed_resources": [target.get("name", target.get("object_name"))]}


def test_animation_tools_are_registered_and_dispatched(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()

    assert ANIMATION_COMMANDS <= set(animation.mcp._tool_manager._tools)
    assert ANIMATION_COMMANDS <= set(server._build_command_handlers())
    assert server.command_spec("inspect_animation").read_only
    assert not server.command_spec("edit_keyframes").read_only


def test_keyframe_edit_requires_operation_appropriate_value() -> None:
    with pytest.raises(ValidationError, match="UPSERT requires value"):
        animation.KeyframeEdit(data_path="location", frame=1)
    with pytest.raises(ValidationError, match="REMOVE does not accept value"):
        animation.KeyframeEdit(operation="REMOVE", data_path="location", frame=1, value=2)
    with pytest.raises(ValidationError):
        animation.KeyframeEdit(data_path="location", frame=1, value=float("nan"))


def test_action_tool_validates_conditional_arguments(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)
    target = animation.AnimationTarget(type="OBJECT", name="Cube")

    with pytest.raises(ToolError, match="requires action_name"):
        asyncio.run(animation.manage_animation_action(ctx=None, target=target, action="CREATE"))
    with pytest.raises(ToolError, match="source_action_name"):
        asyncio.run(
            animation.manage_animation_action(
                ctx=None,
                target=target,
                action="DUPLICATE",
                action_name="Copy",
            )
        )
    assert connection.calls == []


def test_edit_keyframes_serializes_batch(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)
    target = animation.AnimationTarget(type="OBJECT", name="Cube")

    result = asyncio.run(
        animation.edit_keyframes(
            ctx=None,
            target=target,
            action_name="Cube Motion",
            edits=[
                animation.KeyframeEdit(data_path="location", frame=1, value=[0, 0, 0]),
                animation.KeyframeEdit(data_path="location", array_index=0, frame=20, value=4),
            ],
        )
    )

    command, params = connection.calls[0]
    assert command == "edit_keyframes"
    assert params["target"] == {"type": "OBJECT", "name": "Cube"}
    assert len(params["edits"]) == 2
    assert result["changed_resources"] == ["Cube"]


def test_bake_evaluated_animation_requires_confirmation_and_serializes(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)
    target = animation.EvaluatedBakeTarget(object_name="Camera", transforms=["LOCATION", "ROTATION"])

    with pytest.raises(ToolError, match="confirm_bake"):
        asyncio.run(
            animation.bake_evaluated_animation(ctx=None, target=target, frame_start=1, frame_end=10, confirm_bake=False)
        )
    asyncio.run(
        animation.bake_evaluated_animation(
            ctx=None,
            target=target,
            frame_start=1,
            frame_end=10,
            action_name="Camera Bake",
            confirm_bake=True,
        )
    )
    assert connection.calls[-1][0] == "bake_evaluated_animation"
    assert connection.calls[-1][1]["target"]["object_name"] == "Camera"


def test_nla_tool_requires_operation_specific_inputs(monkeypatch) -> None:
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)
    target = animation.AnimationTarget(type="OBJECT", name="Cube")

    with pytest.raises(ToolError, match="requires strip_name"):
        asyncio.run(
            animation.manage_nla_tracks(
                ctx=None,
                target=target,
                action="PATCH_STRIP",
                track_name="Motion",
                strip_patch=animation.NlaStripPatch(influence=0.5),
            )
        )
    with pytest.raises(ToolError, match="confirm_remove"):
        asyncio.run(
            animation.manage_nla_tracks(
                ctx=None,
                target=target,
                action="REMOVE_TRACK",
                track_name="Motion",
            )
        )
    assert connection.calls == []


def test_a_driver_expression_may_name_frame_and_its_declared_variables(monkeypatch) -> None:
    """
    Every useful driver expression names something: `frame`, or a variable the call declares.

    `ast.walk` yields each `Name`'s `ctx` node as well as the `Name`, so an allowlist without
    `ast.Load` rejects every expression that is not a bare arithmetic constant.
    """
    addon, _bpy = _load_addon(monkeypatch, data={})
    handlers = addon.handlers.animation

    assert handlers._safe_expression("frame * 0.5", set()) == "frame * 0.5"
    assert handlers._safe_expression("-(offset + frame) / 24", {"offset"}) == "-(offset + frame) / 24"


def test_a_driver_expression_still_refuses_undeclared_names_and_calls(monkeypatch) -> None:
    """The allowlist must keep refusing what it existed to refuse: calls, attributes, other names."""
    addon, _bpy = _load_addon(monkeypatch, data={})
    handlers = addon.handlers.animation

    with pytest.raises(ValueError, match="undeclared variable: speed"):
        handlers._safe_expression("speed * 2", set())
    with pytest.raises(ValueError, match="only arithmetic"):
        handlers._safe_expression("__import__('os').system('ls')", set())
    with pytest.raises(ValueError, match="only arithmetic"):
        handlers._safe_expression("frame.real", set())
    with pytest.raises(ValueError, match="only arithmetic"):
        handlers._safe_expression("abs(frame)", set())


def test_a_scripted_driver_reaches_blender_with_its_frame_expression(monkeypatch) -> None:
    """The server validates the expression before dispatch, so its allowlist gates the whole tool."""
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)
    target = animation.AnimationTarget(type="OBJECT", name="Cube")

    asyncio.run(
        animation.manage_animation_driver(
            ctx=None,
            target=target,
            action="ADD",
            data_path="location",
            array_index=2,
            driver_type="SCRIPTED",
            expression="frame * 0.5",
        )
    )

    assert connection.calls[-1][0] == "manage_animation_driver"
    # `_validate_safe_expression` raises ValueError; FastMCP turns it into the client's tool error.
    with pytest.raises(ValueError, match="undeclared variable: speed"):
        asyncio.run(
            animation.manage_animation_driver(
                ctx=None,
                target=target,
                action="ADD",
                data_path="location",
                array_index=2,
                driver_type="SCRIPTED",
                expression="speed * 2",
            )
        )


class _FakeRnaProperty:
    """One entry of a `bl_rna.properties` mapping."""

    def __init__(self, *, array_length=0, is_readonly=False, is_animatable=True) -> None:
        self.array_length = array_length
        self.is_readonly = is_readonly
        self.is_animatable = is_animatable


class _FakeStruct:
    """
    `bpy_struct` stand-in: RNA properties, their values, and custom properties.

    `id_properties_ensure` is the discriminator production relies on, so only
    this class has it; a bone *collection* is modelled as a plain dict below,
    which is what a `bpy_prop_collection` looks like to the resolver.
    """

    def __init__(self, *, properties=None, values=None, custom=None, children=None) -> None:
        self.bl_rna = types.SimpleNamespace(properties=dict(properties or {}))
        self._custom = dict(custom or {})
        self._children = dict(children or {})
        for name, value in (values or {}).items():
            setattr(self, name, value)

    def id_properties_ensure(self):
        return self._custom

    def path_resolve(self, path):
        if path not in self._children:
            raise ValueError(f"{path} could not be resolved")
        return self._children[path]


def _rig(monkeypatch):
    """Load the handlers against a rig whose bone name contains a dot, as rig bones do."""
    addon, _bpy = _load_addon(monkeypatch, data={})
    bone = _FakeStruct(
        properties={"location": _FakeRnaProperty(array_length=3)},
        values={"location": (0.0, 0.0, 0.0)},
        custom={"IK_FK": 1.0, "limits": [0.0, 1.0], "label": "left arm"},
    )
    rig = _FakeStruct(
        properties={"location": _FakeRnaProperty(array_length=3)},
        values={"location": (0.0, 0.0, 0.0)},
        custom={"exposure": 0.5},
        children={'pose.bones["hand_ik.L"]': bone, "pose.bones": {"hand_ik.L": bone}},
    )
    return addon.handlers.animation, rig


def test_a_rig_control_custom_property_is_keyable(monkeypatch) -> None:
    """
    Rig controls are custom properties, and Blender keys them like any RNA property.

    Splitting the path on its last dot cut `pose.bones["hand_ik.L"]["IK_FK"]` inside
    the bone name, so the whole class of rig controls was unreachable.
    """
    handlers, rig = _rig(monkeypatch)
    path = 'pose.bones["hand_ik.L"]["IK_FK"]'

    assert handlers._resolve_property(rig, path) == (0, 1.0)
    assert handlers._resolve_property(rig, '["exposure"]') == (0, 0.5)

    expanded = handlers._expanded_edit(rig, {"data_path": path, "frame": 12, "value": 0.0})
    assert [(item[0], item[1], item[2], item[3], item[4]) for item in expanded] == [("UPSERT", path, 0, 12.0, 0.0)]


def test_an_array_custom_property_expands_to_one_channel_per_component(monkeypatch) -> None:
    handlers, rig = _rig(monkeypatch)
    path = 'pose.bones["hand_ik.L"]["limits"]'

    assert handlers._resolve_property(rig, path) == (2, [0.0, 1.0])

    expanded = handlers._expanded_edit(rig, {"data_path": path, "frame": 4, "value": [0.25, 0.75]})
    assert [(item[2], item[4]) for item in expanded] == [(0, 0.25), (1, 0.75)]


def test_an_rna_property_under_a_dotted_bone_name_still_resolves(monkeypatch) -> None:
    """The scan must not regress the ordinary case the old `rpartition` did handle."""
    handlers, rig = _rig(monkeypatch)

    assert handlers._resolve_property(rig, 'pose.bones["hand_ik.L"].location') == (3, (0.0, 0.0, 0.0))
    assert handlers._resolve_property(rig, "location") == (3, (0.0, 0.0, 0.0))


def test_a_collection_member_is_not_mistaken_for_a_custom_property(monkeypatch) -> None:
    """`pose.bones["Hand"]` subscripts a collection, not a property bag; keying it is meaningless."""
    handlers, rig = _rig(monkeypatch)

    with pytest.raises(ValueError, match="custom property holder"):
        handlers._resolve_property(rig, 'pose.bones["hand_ik.L"]')


def test_data_path_errors_name_the_path_and_the_remedy(monkeypatch) -> None:
    handlers, rig = _rig(monkeypatch)

    with pytest.raises(ValueError, match="use array_index instead"):
        handlers._resolve_property(rig, "location[0]")
    with pytest.raises(ValueError, match=r"Custom property not found: \[\"missing\"\]"):
        handlers._resolve_property(rig, '["missing"]')
    with pytest.raises(ValueError, match="not animatable"):
        handlers._resolve_property(rig, 'pose.bones["hand_ik.L"]["label"]')
    with pytest.raises(ValueError, match="scale"):
        handlers._resolve_property(rig, "scale")


class _FakeCycleModifier:
    """The one F-Modifier field `set_action_cycle` discriminates on, plus the four it writes."""

    def __init__(self) -> None:
        self.type = "CYCLES"
        self.mode_before = "NONE"
        self.mode_after = "NONE"
        self.cycles_before = 0
        self.cycles_after = 0


class _FakeModifierStack(list):
    """`fcurve.modifiers`: only CYCLES is ever created here, so `new` ignores its type."""

    def new(self, type):
        modifier = _FakeCycleModifier()
        modifier.type = type
        self.append(modifier)
        return modifier


class _FakeCurve:
    def __init__(self, data_path, array_index, *, cyclic=False) -> None:
        self.data_path = data_path
        self.array_index = array_index
        self.modifiers = _FakeModifierStack()
        if cyclic:
            self.modifiers.new(type="CYCLES")


def _cycle_handler(monkeypatch, curves):
    """
    Load the animation handlers over one object, one action, one slot and the given curves.

    Args:
        monkeypatch: pytest's monkeypatch fixture.
        curves: The slot's F-Curves, in the order `_iter_fcurves` must yield them.

    Returns:
        tuple: The bound handler mixin and the target spec to pass it.

    """
    slot = types.SimpleNamespace(identifier="OBRig", handle=7, target_id_type="OBJECT")
    bag = types.SimpleNamespace(slot_handle=7, fcurves=list(curves))
    strip = types.SimpleNamespace(channelbags=[bag])
    action = types.SimpleNamespace(name="Walk", slots=[slot], layers=[types.SimpleNamespace(strips=[strip])])
    rig = types.SimpleNamespace(name="Rig", id_type="OBJECT", animation_data=None)
    addon, _bpy = _load_addon(
        monkeypatch,
        data={
            "objects": types.SimpleNamespace(get=lambda name: rig if name == "Rig" else None),
            "actions": types.SimpleNamespace(get=lambda name: action if name == "Walk" else None),
        },
    )
    return addon.handlers.animation.AnimationHandlersMixin(), {"type": "OBJECT", "name": "Rig"}


def test_a_cycle_prefix_that_names_no_curve_is_refused(monkeypatch) -> None:
    """
    A prefix matching nothing reported success having made nothing cyclic.

    The caller's next move is playback, and an action that does not loop looks identical to
    one whose modifiers were never asked for - so the typo surfaced frames later, if at all.
    """
    handler, target = _cycle_handler(monkeypatch, [_FakeCurve("location", index) for index in range(3)])

    with pytest.raises(ValueError, match=r"no F-Curve in slot OBRig whose data_path starts with 'rotation_euler'"):
        handler.set_action_cycle(target, "Walk", data_path_prefix="rotation_euler", action_slot_identifier="OBRig")


def test_removing_a_cycle_no_curve_carries_is_still_a_success(monkeypatch) -> None:
    """Nothing to remove is not "your prefix is wrong": the prefix selected three real curves."""
    handler, target = _cycle_handler(monkeypatch, [_FakeCurve("location", index) for index in range(3)])

    result = handler.set_action_cycle(
        target, "Walk", "REMOVE", data_path_prefix="location", action_slot_identifier="OBRig"
    )

    assert result["curve_count"] == 0
    assert result["modifiers"] == []


def test_curve_count_reports_the_curves_the_call_touched(monkeypatch) -> None:
    """`curve_count` is the documented "how many curves were touched", not how many were selected."""
    curves = [_FakeCurve("location", 0, cyclic=True), _FakeCurve("location", 1), _FakeCurve("location", 2)]
    handler, target = _cycle_handler(monkeypatch, curves)

    result = handler.set_action_cycle(
        target, "Walk", "REMOVE", data_path_prefix="location", action_slot_identifier="OBRig"
    )

    assert result["curve_count"] == 1
    assert [record["array_index"] for record in result["modifiers"]] == [0]
    assert not any(modifier.type == "CYCLES" for curve in curves for modifier in curve.modifiers)
