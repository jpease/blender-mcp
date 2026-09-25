"""
Blender 5.1+ background smoke coverage for the axis letters `list_character_bones` reports.

`rest_axes` hands back nine numbers and used to leave every caller to work out which signed bone
axis stands up and which runs along the bone. A demo runbook did that derivation, cached the
wrong answer, and aimed a head 90 degrees over. The reply now names both in the vocabulary
`aim_at` takes - `up_axis` per bone, `length_axis` once, because Blender fixes it for every bone
it builds - and this script proves the naming is usable rather than merely plausible: it reads
the letters out of the reply and feeds them straight back into `aim_at`, with no arithmetic in
between, then measures how upright the bone ended up and what the naming cost.

The rigs are built to carry the exact rest axes that runbook recorded for `CHAR1_head_jnt` -
X = (0, 0, -1), Y = (1, 0, 0), Z = (0, -1, 0) in armature space - so the case that misled it is
the case under test. One copy sits at the origin and one is laid over in the scene, which is
where an answer derived from the armature-space numbers alone parts company with the one an aim
needs: those numbers cannot give it, because the rig's object matrix decides it.

Run with::

    blender --background --factory-startup --python tests/blender_rest_axis_letters_smoke.py
"""

# Blender runtime types are dynamic in this executable harness.

import importlib
import json
import math
import sys

from pathlib import Path

import bpy

from mathutils import Vector

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

package_name = "blender_mcp_rest_axis_letters_smoke"
load_addon(package_name)
character_handlers = importlib.import_module(f"{package_name}.handlers.character_rigging")

AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2}
WORLD_UP = Vector((0.0, 0.0, 1.0))
# The aim is built from exact vector arithmetic on a float32 matrix, so a hundredth of a degree
# is four orders of magnitude of slack; anything that fails this failed by choosing an axis, not
# by rounding.
ANGLE_TOLERANCE_DEGREES = 1e-2
# What a wrong letter costs: the six signed axes are 90 degrees apart, so a wrong up axis lands
# the bone's rest-up direction at least this far from world up. Nothing here may pass by being
# merely closer than a quarter turn.
WRONG_LETTER_DEGREES = 45.0
BONE = "CHAR1_head_jnt"


class RestAxisLettersSmokeHarness(character_handlers.CharacterRiggingHandlersMixin):
    """Expose character-rigging handlers without starting the socket server."""


def build_rig(name, rotation):
    """
    Build the runbook's bone: length along armature +X, rolled so its own -X stands up.

    The rig's own rotation is applied afterwards, so the bone's rest axes in armature space are
    the same on every copy and only the object matrix differs.
    """
    data = bpy.data.armatures.new(f"{name}Data")
    rig = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bone = data.edit_bones.new(BONE)
    bone.head, bone.tail = (0.0, 0.0, 1.0), (0.12, 0.0, 1.0)
    bone.roll = math.pi / 2.0
    upright = data.edit_bones.new("CHAR1_spine_jnt")
    upright.head, upright.tail = (0.0, 0.0, 0.0), (0.0, 0.0, 0.4)
    bpy.ops.object.mode_set(mode="OBJECT")
    rig.select_set(False)
    rig.rotation_euler = rotation
    bpy.context.view_layer.update()
    return rig


def signed_axis_world(rig, bone_name, signed):
    """Where a signed bone axis such as `-X` points in world space, as a unit vector."""
    sign = -1.0 if signed.startswith("-") else 1.0
    matrix = (rig.matrix_world @ rig.pose.bones[bone_name].matrix).to_3x3()
    return (matrix.col[AXIS_INDEX[signed.lstrip("-")]] * sign).normalized()


def degrees_between(first, second):
    return math.degrees(first.angle(second, 0.0))


def rest_pose(rig):
    for pose_bone in rig.pose.bones:
        pose_bone.matrix_basis.identity()
    bpy.context.view_layer.update()


def reported(rig, bone_name):
    """One bone's record, and the reply carrying it, from `list_character_bones(rest_axes=True)`."""
    page = handler.list_character_bones(rig.name, rest_axes=True, bone_names=[bone_name])
    return page, page["bones"]["items"][0]


def refuses(call, fragment):
    try:
        call()
    except ValueError as error:
        assert fragment in str(error), f"expected {fragment!r} in {error}"
        return
    raise AssertionError(f"expected a refusal mentioning {fragment!r}")


handler = RestAxisLettersSmokeHarness()
level = build_rig("LevelRig", (0.0, 0.0, 0.0))
# Laid over 69 degrees, which is what it takes for a different bone axis to become the one
# nearest world up. A rig imported from a Y-up package and stood upright in the scene is the
# ordinary version of this.
tilted = build_rig("TiltedRig", (1.2, 0.0, 0.35))

# --- 1. The rigs really do carry the rest axes the runbook recorded ----------------------------

RUNBOOK_REST_AXES = [0.0, 0.0, -1.0, 1.0, 0.0, 0.0, 0.0, -1.0, 0.0]
for rig in (level, tilted):
    axes = reported(rig, BONE)[1]["rest_axes"]
    drift = max(abs(a - b) for a, b in zip(axes, RUNBOOK_REST_AXES, strict=True))
    assert drift < 1e-3, f"{rig.name} reports {axes}, not the runbook's {RUNBOOK_REST_AXES}"

# --- 2. The letters the reply names, fed straight back into aim_at -----------------------------
#
# No arithmetic between reading and using: whatever the reply says is what the aim is given.

for rig in (level, tilted):
    page, record = reported(rig, BONE)
    track_axis, up_axis = page["length_axis"], record["up_axis"]
    rest_pose(rig)
    head = (rig.matrix_world @ rig.pose.bones[BONE].matrix).translation.copy()
    # Level with the head, so an upright bone is exactly upright rather than upright-as-possible.
    target = head + Vector((1.4, -0.9, 0.0))
    handler.set_character_pose(
        rig.name,
        [{"bone_name": BONE, "aim_at": {"target_point": tuple(target), "track_axis": track_axis, "up_axis": up_axis}}],
    )
    head = (rig.matrix_world @ rig.pose.bones[BONE].matrix).translation
    aim_error = degrees_between(signed_axis_world(rig, BONE, track_axis), (target - head).normalized())
    up_error = degrees_between(signed_axis_world(rig, BONE, up_axis), WORLD_UP)
    print(f"{rig.name}: track_axis={track_axis!r} up_axis={up_axis!r} aim {aim_error:.6f} deg, up {up_error:.6f} deg")
    assert aim_error < ANGLE_TOLERANCE_DEGREES, f"{rig.name}: the reply's track_axis missed by {aim_error} degrees"
    assert up_error < ANGLE_TOLERANCE_DEGREES, f"{rig.name}: the reply's up_axis left the bone {up_error} off upright"

    # And no other legal letter would have done: a wrong up axis is a quarter turn out, which is
    # the tilt the runbook shipped. This is what stops the assertions above passing by accident.
    for candidate in ("X", "-X", "Y", "-Y", "Z", "-Z"):
        if candidate.lstrip("-") == track_axis.lstrip("-") or candidate == up_axis:
            continue
        rest_pose(rig)
        handler.set_character_pose(
            rig.name,
            [
                {
                    "bone_name": BONE,
                    "aim_at": {
                        "target_point": tuple(target),
                        "track_axis": track_axis,
                        "up_axis": candidate,
                    },
                }
            ],
        )
        wrong = degrees_between(signed_axis_world(rig, BONE, up_axis), WORLD_UP)
        assert wrong > WRONG_LETTER_DEGREES, f"{rig.name}: up_axis={candidate!r} was only {wrong} degrees off"

# --- 3. The tilted rig is why up_axis is measured against world up, not armature up -----------
#
# Both rigs hold the same bone with the same nine numbers. A reader deriving "nearest up" from
# those numbers alone gets the same answer for both, and `aim_at.up_reference` is a world
# direction, so on a rig tilted in the scene that answer is not the one an aim wants. The rig's
# object matrix decides it, and the reply never carries that - which is why the derivation is
# not one a caller could have done correctly off this reply at all.

level_page, level_head = reported(level, BONE)
tilted_page, tilted_head = reported(tilted, BONE)
print(f"same rest_axes, up_axis level={level_head['up_axis']!r} tilted={tilted_head['up_axis']!r}")
assert level_head["up_axis"] == "-X", level_head
assert tilted_head["up_axis"] == "-Z", tilted_head
assert level_page["length_axis"] == tilted_page["length_axis"] == "Y"

# --- 4. A bone that stands up at rest names one letter twice, and the table resolves it --------

spine_page, spine = reported(level, "CHAR1_spine_jnt")
print(f"CHAR1_spine_jnt: up_axis={spine['up_axis']!r} length_axis={spine_page['length_axis']!r}")
assert spine["up_axis"] == spine_page["length_axis"] == "Y", spine
assert spine["aim_axis_for_world"]["+Z"] == spine["up_axis"], spine
rest_pose(level)
refuses(
    lambda: handler.set_character_pose(
        level.name,
        [
            {
                "bone_name": "CHAR1_spine_jnt",
                "aim_at": {"target_point": (2.0, 0.0, 0.0), "track_axis": "Y", "up_axis": spine["up_axis"]},
            }
        ],
    ),
    "different bone axis than track_axis",
)

# The refusal names the axes that are still free, and where each points at rest, because a
# caller holding only `length_axis` and `up_axis` has nothing else to try.
rest_pose(level)
refuses(
    lambda: handler.set_character_pose(
        level.name,
        [
            {
                "bone_name": "CHAR1_spine_jnt",
                "aim_at": {"target_point": (2.0, 0.0, 0.0), "track_axis": "Y", "up_axis": "Y"},
            }
        ],
    ),
    "The bone's other axes at rest:",
)

# And the table says which letter to track instead: the entry for the direction the bone should
# point along. Fed back unchanged, with the same up_axis, it is accepted and it lands.
rest_pose(level)
spine_head = (level.matrix_world @ level.pose.bones["CHAR1_spine_jnt"].matrix).translation.copy()
for direction, offset in (("+X", Vector((2.0, 0.0, 0.0))), ("-Y", Vector((0.0, -2.0, 0.0)))):
    track_axis = spine["aim_axis_for_world"][direction]
    assert track_axis.lstrip("-") != spine["up_axis"].lstrip("-"), f"{direction} named the upright axis: {track_axis}"
    rest_pose(level)
    target = spine_head + offset
    handler.set_character_pose(
        level.name,
        [
            {
                "bone_name": "CHAR1_spine_jnt",
                "aim_at": {"target_point": tuple(target), "track_axis": track_axis, "up_axis": spine["up_axis"]},
            }
        ],
    )
    aim_error = degrees_between(
        signed_axis_world(level, "CHAR1_spine_jnt", track_axis), (target - spine_head).normalized()
    )
    up_error = degrees_between(signed_axis_world(level, "CHAR1_spine_jnt", spine["up_axis"]), WORLD_UP)
    print(f"{direction}: track_axis={track_axis!r} aim {aim_error:.6f} deg, up {up_error:.6f} deg")
    assert aim_error < ANGLE_TOLERANCE_DEGREES, f"{direction}: the table's letter missed by {aim_error} degrees"
    assert up_error < ANGLE_TOLERANCE_DEGREES, f"{direction}: up_axis left the bone {up_error} off upright"

# --- 5. The length axis is the same letter for every bone, and what the naming costs ----------

wide = build_rig("WideRig", (0.0, 0.0, 0.0))
bpy.context.view_layer.objects.active = wide
wide.select_set(True)
bpy.ops.object.mode_set(mode="EDIT")
for index in range(185):
    extra = wide.data.edit_bones.new(f"CHAR1_filler_{index:03d}_jnt")
    extra.head, extra.tail = (0.01 * index, 0, 0.55), (0.01 * index, 0.02, 0.6)
    extra.parent = wide.data.edit_bones[BONE]
bpy.ops.object.mode_set(mode="OBJECT")
wide.select_set(False)
bone_count = len(wide.data.bones)
assert bone_count == 187, bone_count


def without_the_addition(reply):
    """Rebuild the reply as it was before the derived axis naming was added to it."""
    stripped = json.loads(json.dumps(reply))
    stripped.pop("length_axis", None)
    for item in stripped["bones"]["items"]:
        item.pop("up_axis", None)
        item.pop("aim_axis_for_world", None)
    return stripped


def wire_bytes(reply):
    """Count what the reply costs the way the budget does - `envelope._wire_bytes` uses indent=2."""
    return len(json.dumps(reply, indent=2))


# The reply states the length axis once because Blender fixes it for every bone it builds. That
# is a claim about all 187 of them, so check all 187 of them rather than the one the aim used.
full = handler.list_character_bones(wide.name, limit=200, rest_axes=True)
named = full["length_axis"]
for item in full["bones"]["items"]:
    bone = wide.data.bones[item["name"]]
    along = (wide.matrix_world.to_3x3() @ (bone.tail_local - bone.head_local)).normalized()
    reported_axis = signed_axis_world(wide, item["name"], named)
    off = degrees_between(reported_axis, along)
    assert off < ANGLE_TOLERANCE_DEGREES, f"{item['name']}: length_axis {named!r} is {off} degrees off the bone"
print(f"length_axis={named!r} runs head to tail on all {len(full['bones']['items'])} bones")

THREE = [BONE, "CHAR1_filler_010_jnt", "CHAR1_filler_120_jnt"]
for label, page in (
    ("3 bones", handler.list_character_bones(wide.name, rest_axes=True, bone_names=THREE)),
    ("187 bones", full),
):
    count = len(page["bones"]["items"])
    after, before = wire_bytes(page), wire_bytes(without_the_addition(page))
    per_bone = (after - before) / count
    print(f"{label}: rest_axes reply {before} -> {after} wire bytes (+{after - before}, {per_bone:.1f} a bone)")
    # Six one-line entries and their wrapper, plus one `up_axis` line, at the reply's
    # indentation: about 200 bytes a bone, against 170 for the nine rest numbers they resolve.
    # It is the reply's most expensive field and the only one that answers "which letter do I
    # pass", which is why `rest_axes` is opt-in and `bone_names` is the way to ask for it.
    assert 150 < per_bone < 260, f"{label}: the derived naming cost {per_bone} bytes a bone"

# --- 6. The sliders a pose call has to name, read off the rig instead of a document ----------
#
# A face rig keeps its controls as bounded custom properties on a pose bone, and
# `keyframe_character_pose` refuses a name the bone does not carry. Until now no tool in a
# posing-only process reported them: `get_character_rig_info` does and belongs to the
# rig-authoring bundle, so an agent assembling a shot had to be handed the names in prose.
# The UI bounds come from Blender's own `id_properties_ui`, which no fake `bpy` can stand in for.

face = wide.pose.bones[BONE]
face["expr_smile"] = 0.25
face.id_properties_ui("expr_smile").update(min=0.0, max=1.0, description="mouth corners")
face["sk_brow_up_in_L"] = 0.0

sliders = handler.list_character_bones(wide.name, bone_names=[BONE], custom_properties=True)["bones"]["items"][0]
quiet = handler.list_character_bones(wide.name, bone_names=[BONE])["bones"]["items"][0]

assert "custom_properties" not in quiet, quiet
by_name = {record["name"]: record for record in sliders["custom_properties"]}
assert set(by_name) == {"expr_smile", "sk_brow_up_in_L"}, sliders
assert math.isclose(by_name["expr_smile"]["value"], 0.25, abs_tol=1e-6), by_name
assert math.isclose(by_name["expr_smile"]["min"], 0.0, abs_tol=1e-6), by_name
assert math.isclose(by_name["expr_smile"]["max"], 1.0, abs_tol=1e-6), by_name
# A property with no authored UI data has no range to state and spends no bytes saying so.
assert "max" not in by_name["sk_brow_up_in_L"], by_name
assert (sliders["custom_property_count"], sliders["custom_property_next_offset"]) == (2, None), sliders
print(f"sliders on {BONE}: {sliders['custom_properties']}")

print("REST_AXIS_LETTERS_SMOKE_OK")
