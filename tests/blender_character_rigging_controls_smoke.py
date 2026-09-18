"""Blender 5.1+ background smoke coverage for character control and deformation handlers.

Run with::

    blender --background --factory-startup --python tests/blender_character_rigging_controls_smoke.py
"""

# Blender runtime types are dynamic in this executable harness.
# ruff: file-ignore[missing-return-type-undocumented-public-function, undocumented-public-function]

import importlib
import sys
import types

from collections.abc import Iterable
from pathlib import Path

import bpy

addon_path = Path(__file__).resolve().parents[1] / "src" / "blender_mcp" / "bundled" / "addon"
package_name = "blender_mcp_character_rigging_smoke"
addon = types.ModuleType(package_name)
addon.__path__ = [str(addon_path)]
addon.ADDON_ID = package_name
sys.modules[package_name] = addon
character_handlers = importlib.import_module(f"{package_name}.handlers.character_rigging")
CharacterRiggingHandlersMixin = character_handlers.CharacterRiggingHandlersMixin


class CharacterRiggingSmokeHarness(CharacterRiggingHandlersMixin):
    """Expose character-rigging handlers without starting the socket server."""


def create_test_armature(name):
    """Create one bent two-bone limb and a settings control."""
    data = bpy.data.armatures.new(f"{name}Data")
    armature = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(armature)
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    upper = data.edit_bones.new("upper")
    upper.head = (0, 0, 0)
    upper.tail = (0, 0, 1)
    lower = data.edit_bones.new("lower")
    lower.head = upper.tail
    lower.tail = (0.2, 0, 2)
    lower.parent = upper
    lower.use_connect = True
    settings = data.edit_bones.new("settings")
    settings.head = (0, -0.5, 0)
    settings.tail = (0, -0.5, 0.25)
    settings.use_deform = False
    bpy.ops.object.mode_set(mode="OBJECT")
    armature.select_set(False)
    return armature


def create_test_mesh(name):
    """Create one triangle mesh suitable for transfer and shape-key smoke checks."""
    mesh = bpy.data.meshes.new(f"{name}Data")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


handler = CharacterRiggingSmokeHarness()
rig = create_test_armature("SmokeRig")

bendy = handler.configure_bendy_bones(
    rig.name,
    [{"bone_name": "upper", "segments": 4, "ease_in": 0.75, "ease_out": 0.75}],
)
assert bendy["changes"][0]["new"]["bbone_segments"] == 4

ik = handler.create_ik_chain(
    rig.name,
    ["upper", "lower"],
    {"name": "foot_ik", "head": (0.2, 0, 2), "tail": (0.2, 0, 2.25), "collection": "CTRL"},
    {
        "name": "pole",
        "head": (0, -1, 1),
        "tail": (0, -1, 1.25),
        "collection": "CTRL",
        "pole_angle": 0.0,
    },
)
assert ik["constraint"] == "IK"

pose = handler.set_character_pose(
    rig.name,
    [{"bone_name": "settings", "location": (0.1, 0, 0)}],
    space="LOCAL",
)
assert pose["bones"][0]["bone"] == "settings"


MATRIX_TOLERANCE = 1e-5
MINIMUM_POSE_CHANGE = 0.1


def rows(matrix: Iterable[Iterable[float]]) -> list[list[float]]:
    return [list(row) for row in matrix]


def max_difference(a: list[list[float]], b: list[list[float]]) -> float:
    return max(abs(x - y) for row_a, row_b in zip(a, b, strict=True) for x, y in zip(row_a, row_b, strict=True))


# A clean two-bone chain: `rig` carries an IK constraint that would move `lower` on its own.
chain = create_test_armature("PoseChain")
upper, lower = chain.pose.bones["upper"], chain.pose.bones["lower"]

# The report must describe the evaluated result, not the matrix read before re-evaluation.
reported = handler.set_character_pose(chain.name, [{"bone_name": "upper", "rotation_euler": (0.5, 0, 0)}])
assert (
    max_difference(reported["bones"][0]["after_pose_matrix"], reported["bones"][0]["before_pose_matrix"])
    > MINIMUM_POSE_CHANGE
)
assert max_difference(reported["bones"][0]["after_pose_matrix"], rows(upper.matrix)) < MATRIX_TOLERANCE

# Parent and child posed in POSE space in one call: the child must land where asked, not relative
# to its parent's stale pre-call matrix.
upper.rotation_mode = lower.rotation_mode = "XYZ"
upper.rotation_euler, lower.rotation_euler = (0.4, 0, 0), (0, 0.6, 0)
bpy.context.view_layer.update()
target_upper, target_lower = rows(upper.matrix), rows(lower.matrix)
for bone in (upper, lower):
    bone.matrix_basis.identity()
bpy.context.view_layer.update()
handler.set_character_pose(
    chain.name,
    [{"bone_name": "upper", "matrix": target_upper}, {"bone_name": "lower", "matrix": target_lower}],
    space="POSE",
)
assert max_difference(rows(upper.matrix), target_upper) < MATRIX_TOLERANCE
assert max_difference(rows(lower.matrix), target_lower) < MATRIX_TOLERANCE, (
    "child posed against its parent's stale matrix"
)

keyed = handler.keyframe_character_pose(
    rig.name,
    "SmokePose",
    3.5,
    [{"bone_name": "settings", "location": (0.2, 0, 0)}],
    space="LOCAL",
    action_policy="CREATE",
)
assert bpy.data.actions.get(keyed["action"]) is not None
assert keyed["action_slot"] is not None

widget = bpy.data.objects.new("Widget", None)
bpy.context.scene.collection.objects.link(widget)
shapes = handler.assign_bone_custom_shapes(
    rig.name,
    [{"bone_name": "settings", "shape_object_name": widget.name}],
    widget_collection_name="Widgets",
)
assert shapes["assignments"][0]["shape"] == widget.name

spline = handler.create_spline_ik_rig(
    rig.name,
    ["upper", "lower"],
    constraint_name="Spline",
    new_curve_name="SpineCurve",
    curve_points=[(0, 0, 0), (0, 0, 1), (0.2, 0, 2)],
    curve_collection_name="RigHelpers",
)
assert spline["curve_created"]

driven = handler.create_rig_property_driver(
    rig.name,
    "OBJECT",
    "spline_blend",
    [
        {
            "owner": "CONSTRAINT",
            "object_name": rig.name,
            "bone_name": "lower",
            "constraint_name": "Spline",
            "property_name": "influence",
            "existing_policy": "ERROR",
        }
    ],
)
assert driven["drivers"][0]["expression"] == "rig_property * 1 + 0"

source = create_test_mesh("SourceMesh")
source_group = source.vertex_groups.new(name="upper")
source_group.add([0, 1, 2], 1.0, "REPLACE")  # pyright: ignore[reportArgumentType]
target = create_test_mesh("TargetMesh")
transfer = handler.transfer_skin_weights(
    source.name,
    target.name,
    mapping="TOPOLOGY",
    source_groups="ALL",
)
assert transfer["modifier"] == "Rig Weight Transfer"

committed_target = create_test_mesh("CommittedTargetMesh")
committed_transfer = handler.transfer_skin_weights(
    source.name,
    committed_target.name,
    mapping="TOPOLOGY",
    source_groups="ALL",
    commit=True,
    confirm_commit=True,
    normalize=True,
)
assert committed_transfer["committed"]
assert committed_target.vertex_groups.get("upper") is not None
assert abs(committed_target.data.vertices[0].groups[0].weight - 1.0) < 1e-6

target.shape_key_add(name="Basis")
target.shape_key_add(name="Smile")
target.shape_key_add(name="SmileWide")
target.shape_key_add(name="Frown")
target.shape_key_add(name="JointCorrective")
facial = handler.create_shape_key_controls(
    target.name,
    rig.name,
    "POSE_BONE",
    [
        {
            "shape_key_name": "Smile",
            "property_name": "smile",
            "minimum": 0.0,
            "maximum": 1.0,
            "default": 0.0,
            "factor": 1.0,
            "offset": 0.0,
        }
    ],
    property_bone_name="settings",
)
assert facial["controls"][0]["shape_key"] == "Smile"

advanced_facial = handler.create_shape_key_controls(
    target.name,
    rig.name,
    "POSE_BONE",
    [
        {
            "mode": "SIGNED",
            "positive_shape_key_name": "SmileWide",
            "negative_shape_key_name": "Frown",
            "property_name": "expression",
            "default": 0.0,
            "factor": 1.0,
        },
        {
            "mode": "CORRECTIVE",
            "shape_key_name": "JointCorrective",
            "inputs": [
                {"property_name": "bend_x", "minimum": 0.0, "maximum": 1.0, "default": 0.0},
                {"property_name": "bend_y", "minimum": 0.0, "maximum": 1.0, "default": 0.0},
            ],
            "operation": "MULTIPLY",
            "factor": 1.0,
            "offset": 0.0,
        },
    ],
    property_bone_name="settings",
)
assert len(advanced_facial["controls"]) == 3

blend_rig = create_test_armature("BlendRig")
blend = handler.create_ik_fk_limb(
    blend_rig.name,
    ["upper", "lower"],
    "settings",
    ik_target={
        "name": "limb_ik",
        "head": (0.2, 0, 2),
        "tail": (0.2, 0, 2.25),
        "collection": "CTRL",
    },
    pole_control={
        "name": "limb_pole",
        "head": (0, -1, 1),
        "tail": (0, -1, 1.25),
        "collection": "CTRL",
    },
)
assert len(blend["blend_constraints"]) == 2

print("CHARACTER_RIGGING_CONTROLS_SMOKE_OK")
