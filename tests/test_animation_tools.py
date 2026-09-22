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
    """
    The one F-Modifier field `set_action_cycle` discriminates on, plus the nine it writes.

    Assigning one range bound moves the other to keep `frame_start <= frame_end`, which is
    what Blender 5.2 measurably does (and not a clamp of the assigned value): a call that
    wrote only one bound would silently drag the other, so this suite catches that here
    rather than only in the smoke run.
    """

    def __init__(self) -> None:
        self.type = "CYCLES"
        self.mode_before = "NONE"
        self.mode_after = "NONE"
        self.cycles_before = 0
        self.cycles_after = 0
        self.use_restricted_range = False
        self.blend_in = 0.0
        self.blend_out = 0.0
        self._frame_start = 0.0
        self._frame_end = 0.0

    @property
    def frame_start(self):
        return self._frame_start

    @frame_start.setter
    def frame_start(self, value):
        self._frame_start = value
        self._frame_end = max(self._frame_end, value)

    @property
    def frame_end(self):
        return self._frame_end

    @frame_end.setter
    def frame_end(self, value):
        self._frame_end = value
        self._frame_start = min(self._frame_start, value)


class _FakeModifierStack(list):
    """`fcurve.modifiers`: only CYCLES is ever created here, so `new` ignores its type."""

    def new(self, type):
        modifier = _FakeCycleModifier()
        modifier.type = type
        self.append(modifier)
        return modifier


class _FakeKey:
    """One keyframe point; `set_action_cycle` reads the frame it sits on, nothing else."""

    def __init__(self, frame) -> None:
        self.co = (float(frame), 0.0)


class _FakeCurve:
    def __init__(self, data_path, array_index, *, cyclic=False, frames=(1.0, 25.0)) -> None:
        self.data_path = data_path
        self.array_index = array_index
        self.keyframe_points = [_FakeKey(frame) for frame in frames]
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


def test_a_cycle_reports_the_period_each_curve_will_actually_repeat(monkeypatch) -> None:
    """
    The period is the number the caller thinks they are setting and could not see.

    A Cycles modifier repeats its own curve's key extent. In the failing run the legs were
    keyed over frames 1-25 and the arms had picked up a later gesture's keys at 162, so one
    call made them cyclic at two different rates; both replies read as identical successes and
    the arms drifted through one slow interpolation for the rest of the shot.
    """
    curves = [
        _FakeCurve('pose.bones["thigh.L"].rotation_quaternion', 0, frames=(1.0, 25.0)),
        _FakeCurve('pose.bones["upper_arm.L"].rotation_quaternion', 0, frames=(1.0, 162.0)),
        _FakeCurve('pose.bones["head"].rotation_quaternion', 0, frames=(7.0,)),
    ]
    handler, target = _cycle_handler(monkeypatch, curves)

    result = handler.set_action_cycle(target, "Walk", action_slot_identifier="OBRig")

    assert [record["period_frames"] for record in result["modifiers"]] == [24.0, 161.0, None]
    assert [record["first_key_frame"] for record in result["modifiers"]] == [1.0, 1.0, 7.0]
    assert [record["last_key_frame"] for record in result["modifiers"]] == [25.0, 162.0, 7.0]
    # Unlimited in both directions by default, so there is no bound to report.
    assert not any("repeat_end_frame" in record or "repeat_start_frame" in record for record in result["modifiers"])
    disagreement = next(warning for warning in result["warnings"] if "do not share one cycle period" in warning)
    assert "24 frames" in disagreement and "161 frames" in disagreement
    assert 'pose.bones["thigh.L"].rotation_quaternion' in disagreement
    assert 'pose.bones["upper_arm.L"].rotation_quaternion' in disagreement
    assert any("fewer than two keys" in warning for warning in result["warnings"])


def test_a_finite_cycle_count_says_where_the_repeat_stops(monkeypatch) -> None:
    """
    cycles_after=6 stopped a walk dead at frame 169 and nothing said so.

    Past the last repeat the modifier contributes nothing and the raw curve's extrapolation
    takes over, which for a travelling root is a snap back to the last keyed value and then a
    freeze - the reported symptom, from an argument that looked like "six more strides".
    """
    handler, target = _cycle_handler(monkeypatch, [_FakeCurve("location", 1, frames=(1.0, 25.0))])

    result = handler.set_action_cycle(
        target,
        "Walk",
        # mode_before is NONE by default, which extrapolates nothing backwards and so bounds
        # nothing: this test is about the counts, so both directions are asked to repeat.
        mode_before="REPEAT_OFFSET",
        cycles_before=2,
        cycles_after=6,
        action_slot_identifier="OBRig",
    )

    record = result["modifiers"][0]
    assert record["repeat_end_frame"] == pytest.approx(25.0 + 6 * 24.0)
    assert record["repeat_start_frame"] == pytest.approx(1.0 - 2 * 24.0)
    forward = next(warning for warning in result["warnings"] if warning.startswith("cycles_after=6"))
    assert "frame 169" in forward
    assert "extrapolation" in forward
    backward = next(warning for warning in result["warnings"] if warning.startswith("cycles_before=2"))
    assert "frame -47" in backward


def test_a_cycle_extrapolates_only_forwards_unless_asked(monkeypatch) -> None:
    """
    A caller asking for a forward loop got an infinite backward one too, and never asked.

    REPEAT_OFFSET before the first key fills every frame ahead of the cycle with motion
    nobody requested - on a travelling root it walks the character backwards out of the set,
    which is only visible once something renders frame 0 or the timeline start moves.
    """
    curve = _FakeCurve("location", 1, frames=(1.0, 25.0))
    handler, target = _cycle_handler(monkeypatch, [curve])

    result = handler.set_action_cycle(target, "Walk", action_slot_identifier="OBRig")

    modifier = curve.modifiers[0]
    assert modifier.mode_before == "NONE"
    assert modifier.mode_after == "REPEAT_OFFSET"
    assert result["modifiers"][0]["mode_before"] == "NONE"
    assert result["modifiers"][0]["mode_after"] == "REPEAT_OFFSET"


def test_a_count_on_a_direction_that_extrapolates_nothing_is_reported_inert(monkeypatch) -> None:
    """`cycles_before` alone bounds repeats the default NONE mode never makes; say so."""
    handler, target = _cycle_handler(monkeypatch, [_FakeCurve("location", 1, frames=(1.0, 25.0))])

    result = handler.set_action_cycle(target, "Walk", cycles_before=3, action_slot_identifier="OBRig")

    assert "repeat_start_frame" not in result["modifiers"][0], "a NONE direction has no repeat to bound"
    inert = next(warning for warning in result["warnings"] if warning.startswith("cycles_before=3"))
    assert "mode_before=NONE" in inert


def test_a_later_key_that_redefined_the_period_is_refused_and_changes_nothing(monkeypatch) -> None:
    """
    The period is the curve's own key extent, so a later gesture key silently redefines it.

    The rehearsal cycled a curve at 20 frames, keyed three explicit strides onto it, and read
    back `period_frames: 60.0` from a reply that said success. `expected_period_frames` is how
    that is caught at the call instead of at playback - and it is checked across the whole
    selection before anything is created, so a mismatch leaves no modifier behind at all.
    """
    curves = [
        _FakeCurve('pose.bones["leg"].location', 0, frames=(1.0, 21.0)),
        _FakeCurve('pose.bones["leg"].location', 1, frames=(1.0, 60.0)),
    ]
    handler, target = _cycle_handler(monkeypatch, curves)

    with pytest.raises(ValueError) as refusal:
        handler.set_action_cycle(target, "Walk", expected_period_frames=20.0, action_slot_identifier="OBRig")

    message = str(refusal.value)
    assert "expected_period_frames=20" in message
    assert 'pose.bones["leg"].location[1] keys 1-60, a 59-frame extent' in message
    assert "manage_nla_tracks" not in message, "the handler names the route in prose, not a server tool symbol"
    assert "NLA strip" in message
    # The curve that did match must be untouched too: the guard runs before any mutation.
    assert all(not curve.modifiers for curve in curves), "a refused call left a Cycles modifier behind"


def test_an_expected_period_that_every_curve_matches_proceeds(monkeypatch) -> None:
    """Matching within the module's frame epsilon is a match: 20.0000001 frames is 20 frames."""
    curves = [_FakeCurve('pose.bones["leg"].location', index, frames=(1.0, 21.0000001)) for index in range(2)]
    handler, target = _cycle_handler(monkeypatch, curves)

    result = handler.set_action_cycle(target, "Walk", expected_period_frames=20.0, action_slot_identifier="OBRig")

    assert result["curve_count"] == 2
    assert all(any(modifier.type == "CYCLES" for modifier in curve.modifiers) for curve in curves)


def test_a_curve_with_nothing_to_repeat_fails_an_expected_period(monkeypatch) -> None:
    """One key has no extent, so it cannot carry the period the caller says the rig carries."""
    curves = [_FakeCurve("location", 0, frames=(7.0,))]
    handler, target = _cycle_handler(monkeypatch, curves)

    with pytest.raises(ValueError, match="fewer than two distinct key frames"):
        handler.set_action_cycle(target, "Walk", expected_period_frames=20.0, action_slot_identifier="OBRig")
    assert not curves[0].modifiers


def test_a_restricted_range_lands_on_the_modifier_and_in_the_reply(monkeypatch) -> None:
    """
    A cycle confined to part of a shot is the modifier's own restricted range.

    Assigning one bound drags the other, so this re-ranges an already-ranged modifier forward
    past its previous end: both bounds of the second call's window have to land, and the blend
    lengths with them.
    """
    curve = _FakeCurve("location", 1, frames=(1.0, 25.0))
    handler, target = _cycle_handler(monkeypatch, [curve])

    handler.set_action_cycle(target, "Walk", frame_start=1.0, frame_end=25.0, action_slot_identifier="OBRig")
    result = handler.set_action_cycle(
        target,
        "Walk",
        frame_start=100.0,
        frame_end=148.0,
        blend_in=4.0,
        blend_out=2.0,
        action_slot_identifier="OBRig",
    )

    modifier = curve.modifiers[0]
    assert modifier.use_restricted_range is True
    assert (modifier.frame_start, modifier.frame_end) == (100.0, 148.0)
    assert (modifier.blend_in, modifier.blend_out) == (4.0, 2.0)
    record = result["modifiers"][0]
    assert record["restricted_range"] == {
        "frame_start": 100.0,
        "frame_end": 148.0,
        "blend_in": 4.0,
        "blend_out": 2.0,
    }
    # The window bounds where the modifier applies; the period is still the curve's own extent.
    assert record["period_frames"] == pytest.approx(24.0)


def test_omitting_the_range_clears_a_previous_one(monkeypatch) -> None:
    """A re-run without a window must widen back to the whole timeline, not keep the old one."""
    curve = _FakeCurve("location", 1, frames=(1.0, 25.0))
    handler, target = _cycle_handler(monkeypatch, [curve])

    handler.set_action_cycle(target, "Walk", frame_start=10.0, frame_end=34.0, action_slot_identifier="OBRig")
    result = handler.set_action_cycle(target, "Walk", action_slot_identifier="OBRig")

    assert curve.modifiers[0].use_restricted_range is False
    assert "restricted_range" not in result["modifiers"][0]


def test_an_empty_or_half_given_range_is_refused(monkeypatch) -> None:
    """A window that ends where it starts applies the modifier nowhere; one bound is a typo."""
    curve = _FakeCurve("location", 1, frames=(1.0, 25.0))
    handler, target = _cycle_handler(monkeypatch, [curve])

    with pytest.raises(ValueError, match="frame_end must be greater than frame_start; got 40 to 40"):
        handler.set_action_cycle(target, "Walk", frame_start=40.0, frame_end=40.0, action_slot_identifier="OBRig")
    with pytest.raises(ValueError, match="frame_end must be greater than frame_start"):
        handler.set_action_cycle(target, "Walk", frame_start=40.0, frame_end=12.0, action_slot_identifier="OBRig")
    with pytest.raises(ValueError, match="must be given together"):
        handler.set_action_cycle(target, "Walk", frame_start=40.0, action_slot_identifier="OBRig")
    with pytest.raises(ValueError, match="blend_in/blend_out fade a restricted range"):
        handler.set_action_cycle(target, "Walk", blend_in=3.0, action_slot_identifier="OBRig")
    assert not curve.modifiers


def test_remove_refuses_the_arguments_that_only_describe_a_cycle(monkeypatch) -> None:
    """Accepting and ignoring them would report a success that did none of what was asked."""
    curve = _FakeCurve("location", 1, cyclic=True, frames=(1.0, 25.0))
    handler, target = _cycle_handler(monkeypatch, [curve])

    with pytest.raises(ValueError, match="REMOVE deletes the Cycles modifier"):
        handler.set_action_cycle(target, "Walk", "REMOVE", expected_period_frames=24.0, action_slot_identifier="OBRig")
    with pytest.raises(ValueError, match="REMOVE deletes the Cycles modifier"):
        handler.set_action_cycle(
            target, "Walk", "REMOVE", frame_start=1.0, frame_end=25.0, action_slot_identifier="OBRig"
        )
    assert len(curve.modifiers) == 1, "a refused REMOVE must not have removed anything"


def test_the_cycle_tool_refuses_a_malformed_range_before_the_socket(monkeypatch) -> None:
    """The pairing and ordering rules are pure arithmetic; a round trip to Blender buys nothing."""
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)
    target = animation.AnimationTarget(type="OBJECT", name="Rig")

    with pytest.raises(ToolError, match="must be given together"):
        asyncio.run(animation.set_action_cycle(None, target, "Walk", frame_start=10.0))
    with pytest.raises(ToolError, match="frame_end must be greater than frame_start"):
        asyncio.run(animation.set_action_cycle(None, target, "Walk", frame_start=10.0, frame_end=10.0))
    with pytest.raises(ToolError, match="cannot apply expected_period_frames"):
        asyncio.run(animation.set_action_cycle(None, target, "Walk", "REMOVE", expected_period_frames=20.0))
    assert connection.calls == []
    # A period of zero and a negative blend are refused by the advertised schema, which is
    # what an MCP client validates against before the call is ever made.
    advertised = animation.mcp._tool_manager._tools["set_action_cycle"].parameters["properties"]
    assert advertised["expected_period_frames"]["anyOf"][0]["exclusiveMinimum"] == pytest.approx(0.0)
    assert advertised["blend_in"]["minimum"] == pytest.approx(0.0)
    assert advertised["blend_out"]["minimum"] == pytest.approx(0.0)
    assert advertised["mode_before"]["default"] == "NONE"


def test_the_cycle_tool_forwards_the_new_arguments(monkeypatch) -> None:
    """Every parameter the schema advertises has to reach the handler, or it is decoration."""
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)
    target = animation.AnimationTarget(type="OBJECT", name="Rig")

    asyncio.run(
        animation.set_action_cycle(
            None,
            target,
            "Walk",
            expected_period_frames=20.0,
            frame_start=1.0,
            frame_end=61.0,
            blend_in=2.0,
            blend_out=3.0,
            data_path_prefix="location",
        )
    )

    _command, params = connection.calls[0]
    assert params["expected_period_frames"] == pytest.approx(20.0)
    assert (params["frame_start"], params["frame_end"]) == (1.0, 61.0)
    assert (params["blend_in"], params["blend_out"]) == (2.0, 3.0)
    assert params["mode_before"] == "NONE"
    assert params["mode_after"] == "REPEAT_OFFSET"


def test_an_unscoped_cycle_names_the_parameter_that_narrows_it(monkeypatch) -> None:
    """
    A cut `modifiers` page is gone, not paused: this tool takes no offset to resume from.

    The envelope's own shortening notice can only say "narrow the scope", which is not a move
    unless the reader already knows which parameter narrows. The hint rides in the reply the
    budget measures, so it is there in the same list as the notice it answers.
    """
    connection = _Connection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)
    target = animation.AnimationTarget(type="OBJECT", name="Rig")

    unscoped = asyncio.run(animation.set_action_cycle(None, target, "Walk"))
    scoped = asyncio.run(animation.set_action_cycle(None, target, "Walk", data_path_prefix="location"))

    assert any("data_path_prefix" in warning for warning in unscoped["warnings"]), unscoped["warnings"]
    assert any("no offset to resume from" in warning for warning in unscoped["warnings"])
    # A call that already narrowed has used the remedy; repeating it would be noise on the wire.
    assert scoped["warnings"] == []
