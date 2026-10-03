# ruff: file-ignore[too-many-branches, too-many-locals, undocumented-public-method]
"""Blender-side handlers for generic object transform keyframing (location/rotation/scale)."""

import contextlib
import math

import bpy
import mathutils

from ..helpers import MAX_FRAME, MIN_FRAME
from .action_assignment import (
    ACTION_POLICIES,
    action_fcurve_collections,
    assign_named_action,
    assigned_action_name,
    assigned_slot_identifier,
    cycled_curve_extent,
    hidden_strip_warning,
    restored_action_assignment,
    restored_keys_on_error,
)
from .animation import _continuous_rotation
from .character_rigging.posing import _place_playhead, restored_playhead
from .key_style import KeyStyle, style_point
from .scene import _object, _required_name
from .scene_physics import _scene, _scene_fps

# What a WORLD or LOCAL key writes on the way to keying it, and so what an unassigned clip hands
# back: the keyed values no longer drive the object once its own action is restored.
_TRANSFORM_CHANNELS = ("location", "rotation_euler", "rotation_quaternion", "rotation_axis_angle", "scale")
_KEYFRAME_MATCH_TOLERANCE = 1e-5
# One call may key 500 records, and the envelope lifts warnings whole rather than paging them,
# so the per-channel cycle notices are named up to this many and then counted.
_MAX_CYCLE_WARNINGS = 4
# The same envelope rule for the NLA-override notices: one per keyed object up to this many, then
# a line counting the rest.
_MAX_HIDDEN_STRIP_NOTICES = 4
_SPACES = {"LOCAL", "WORLD"}
_POLICIES = {"INSERT_ONLY", "REPLACE_EXISTING"}
_CHANNEL_LENGTHS = {"location": 3, "rotation_euler": 3, "rotation_quaternion": 4, "scale": 3}
_EULER_ORDERS = {"XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX"}


def _finite_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return value


def _finite_sequence(value, length, label):
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"{label} must contain exactly {length} numbers")
    return tuple(_finite_number(component, f"{label}[{index}]") for index, component in enumerate(value))


def _frame_value(value, label):
    value = _finite_number(value, label)
    if not MIN_FRAME <= value <= MAX_FRAME:
        raise ValueError(f"{label} must be between {MIN_FRAME} and {MAX_FRAME}")
    return value


def _resolve_frame(record, label, scene_cache):
    frame = record.get("frame")
    at_seconds = record.get("at_seconds")
    if (frame is None) == (at_seconds is None):
        raise ValueError(f"{label} must supply exactly one of frame or at_seconds")
    if frame is not None:
        return _frame_value(frame, f"{label}.frame")
    seconds = _finite_number(at_seconds, f"{label}.at_seconds")
    scene_name = record.get("scene_name")
    scene = scene_cache.get(scene_name)
    if scene is None:
        scene = _scene(scene_name)
        scene_cache[scene_name] = scene
    return _frame_value(scene.frame_start + seconds * _scene_fps(scene), f"{label}.at_seconds")


def _resolve_channels(record, label, obj):
    channels = {}
    for name, length in _CHANNEL_LENGTHS.items():
        value = record.get(name)
        if value is not None:
            channels[name] = _finite_sequence(value, length, f"{label}.{name}")
    if not channels:
        raise ValueError(f"{label} must supply at least one of {sorted(_CHANNEL_LENGTHS)}")
    if "rotation_euler" in channels and "rotation_quaternion" in channels:
        raise ValueError(f"{label}: supply rotation_euler or rotation_quaternion, not both")
    if "rotation_euler" in channels and obj.rotation_mode not in _EULER_ORDERS:
        raise ValueError(
            f"{label}: object '{obj.name}' has rotation_mode={obj.rotation_mode}; supply rotation_quaternion "
            "(QUATERNION mode) or use edit_keyframes directly (AXIS_ANGLE is not supported here)"
        )
    if "rotation_quaternion" in channels and obj.rotation_mode != "QUATERNION":
        raise ValueError(
            f"{label}: object '{obj.name}' has rotation_mode={obj.rotation_mode}; supply rotation_euler instead"
        )
    return channels


def _action_fcurves(id_owner):
    """
    Return the action driving `id_owner` and the F-Curves its own keys live in.

    Args:
        id_owner: The object being keyed.

    Returns:
        tuple: The assigned action (or None) and its F-Curves, narrowed to this owner's slot
        so a second object sharing the action never answers for this one's keys.

    """
    animation = getattr(id_owner, "animation_data", None)
    action = getattr(animation, "action", None) if animation is not None else None
    if action is None:
        return None, ()
    slot = getattr(animation, "action_slot", None)
    return action, [curve for collection in action_fcurve_collections(action, slot) for curve in collection]


def _has_key_at(obj, data_path, frame):
    _action, curves = _action_fcurves(obj)
    for curve in curves:
        if curve.data_path == data_path and any(
            abs(point.co[0] - frame) <= _KEYFRAME_MATCH_TOLERANCE for point in curve.keyframe_points
        ):
            return True
    return False


def _previous_key_value(obj, data_path, frame):
    """
    Read what one channel holds at its last key before `frame`, in the object's own slot.

    Args:
        obj: The object being keyed.
        data_path: A key of `_CHANNEL_LENGTHS`.
        frame: The frame about to be keyed.

    Returns:
        tuple | None: One value per array index, or None when the channel is not fully
        animated yet or has no key before `frame`.

    """
    width = _CHANNEL_LENGTHS[data_path]
    _action, curves = _action_fcurves(obj)
    by_index = {
        curve.array_index: curve for curve in curves if curve.data_path == data_path and 0 <= curve.array_index < width
    }
    if len(by_index) != width:
        return None
    earlier = [
        float(point.co[0])
        for curve in by_index.values()
        for point in curve.keyframe_points
        if point.co[0] < frame - _KEYFRAME_MATCH_TOLERANCE
    ]
    if not earlier:
        return None
    previous = max(earlier)
    return tuple(by_index[index].evaluate(previous) for index in range(width))


def _match_previous_world_rotation(obj, data_path, frame):
    """
    Re-spell the rotation `matrix_world` just decomposed onto the branch of the channel's previous key.

    The setter picks one branch per key - w >= 0 for a quaternion, +-180 degrees for an Euler -
    so two correct neighbouring poses on opposite branches made the curve between them spin the
    long way. Only WORLD keys come through here: a LOCAL value is the caller's own spelling, and a
    deliberate step past 180 degrees between two keys has no other way to be asked for.

    Args:
        obj: The object whose channel `matrix_world` was just assigned through.
        data_path: rotation_euler or rotation_quaternion, whichever is about to be keyed.
        frame: The frame about to be keyed.

    """
    previous = _previous_key_value(obj, data_path, frame)
    if previous is None:
        return
    if data_path == "rotation_quaternion":
        orientation = mathutils.Quaternion(obj.rotation_quaternion)
    else:
        orientation = mathutils.Euler(obj.rotation_euler, obj.rotation_mode).to_quaternion()
    setattr(obj, data_path, _continuous_rotation(obj.rotation_mode, orientation, previous))


def _style_inserted_keys(obj, data_path, frame, style):
    changed = []
    _action, curves = _action_fcurves(obj)
    for curve in curves:
        if curve.data_path != data_path:
            continue
        point = next(
            (item for item in curve.keyframe_points if abs(item.co[0] - frame) <= _KEYFRAME_MATCH_TOLERANCE), None
        )
        if point is None:
            continue
        style_point(point, style)
        changed.append({"data_path": data_path, "array_index": curve.array_index, "frame": frame})
    return changed


def _cycled_extent(obj, data_path):
    """
    Measure the key extent a cycling channel already repeats, if it cycles at all.

    One channel is several curves - `location` is three - and each carries its own modifier and
    its own keys, so the extent this channel repeats is the union of the cycling ones. A curve
    that cycles nothing contributes nothing: it has no period for a new key to redefine.

    Args:
        obj: The object about to be keyed.
        data_path: The channel this call writes.

    Returns:
        tuple | None: (first, last) key frame across that channel's cycling curves, or None
        when none of them carries a Cycles modifier or they hold one frame between them.

    """
    _action, curves = _action_fcurves(obj)
    extents = [
        extent
        for curve in curves
        if curve.data_path == data_path and (extent := cycled_curve_extent(curve)) is not None
    ]
    if not extents:
        return None
    return min(first for first, _last in extents), max(last for _first, last in extents)


def _cycle_extension_warnings(prepared):
    """
    Warn once per channel whose new key lands outside a cycle that channel already repeats.

    `keyframe_character_pose` has warned about this since the walk whose arms drifted; the
    object path carried the same trap and said nothing, which is worse, because the channel it
    redefines is usually the root's `location`. A rehearsal keyed a root at frame 199 over a
    16-frame travelling cycle and moved the whole character: the period became 199 frames, the
    stride stopped repeating, and every REPEAT_OFFSET repeat now carried the wrong distance.
    Extending a cycle on purpose is legitimate authoring, so this warns and keys rather than
    refusing.

    Args:
        prepared: The validated records, read after the batch's action is assigned so the
            curves measured are the ones this call is about to write into.

    Returns:
        list[str]: One warning per affected object channel, naming the frame, the extent it
        fell outside and the period that extent becomes, bounded by `_MAX_CYCLE_WARNINGS` with
        one summary line for the rest. Warnings are lifted whole into the envelope and never
        paged, so a 500-record batch must not be able to spend the reply budget on them.

    """
    stretched = {}
    for entry in prepared:
        for data_path in entry["channels"]:
            identity = (entry["object_name"], data_path)
            if identity in stretched:
                continue
            extent = _cycled_extent(entry["object"], data_path)
            if extent is None:
                continue
            first, last = extent
            if first - _KEYFRAME_MATCH_TOLERANCE <= entry["frame"] <= last + _KEYFRAME_MATCH_TOLERANCE:
                continue
            stretched[identity] = (first, last, entry["frame"])
    affected = list(stretched)
    warnings = []
    for identity in affected[:_MAX_CYCLE_WARNINGS]:
        name, data_path = identity
        first, last, frame = stretched[identity]
        warnings.append(
            f"'{name}'.{data_path} is keyed at frame {frame:g}, outside the frames {first:g}-{last:g} it already "
            f"cycles over. A Cycles modifier repeats its own curve's key extent, so this channel's period becomes "
            f"{max(last, frame) - min(first, frame):g} frames instead of {last - first:g}; under REPEAT_OFFSET "
            "each repeat then carries that much further, so a travelling root stops arriving where the cycle put "
            "it. Key it inside the cycle, or re-cycle the action deliberately."
        )
    remainder = affected[_MAX_CYCLE_WARNINGS:]
    if remainder:
        listed = ", ".join(f"'{name}'.{path}" for name, path in remainder[:_MAX_CYCLE_WARNINGS])
        trailing = f" and {len(remainder) - _MAX_CYCLE_WARNINGS} more" if len(remainder) > _MAX_CYCLE_WARNINGS else ""
        warnings.append(
            f"{len(remainder)} further channel(s) are keyed outside the cycle their own curves carry, stretching "
            f"it the same way: {listed}{trailing}."
        )
    return warnings


def _apply_and_key(obj, frame, space, channels):
    if space == "WORLD":
        # The parent chain sits where it does at the key's own frame, not wherever the playhead
        # arrived: solved against another frame's parent, the local values key a world pose
        # nobody asked for. The update also catches a parent moved earlier in this batch.
        _place_playhead(bpy.context.scene, frame)
        bpy.context.view_layer.update()
        current_location, current_rotation, current_scale = obj.matrix_world.decompose()
        location = mathutils.Vector(channels["location"]) if "location" in channels else current_location
        if "rotation_euler" in channels:
            rotation = mathutils.Euler(channels["rotation_euler"], obj.rotation_mode)
        elif "rotation_quaternion" in channels:
            rotation = mathutils.Quaternion(channels["rotation_quaternion"])
        else:
            rotation = current_rotation
        scale = mathutils.Vector(channels["scale"]) if "scale" in channels else current_scale
        obj.matrix_world = mathutils.Matrix.LocRotScale(location, rotation, scale)
        for data_path in ("rotation_euler", "rotation_quaternion"):
            if data_path in channels:
                _match_previous_world_rotation(obj, data_path, frame)
    else:
        if "location" in channels:
            obj.location = channels["location"]
        if "rotation_euler" in channels:
            obj.rotation_euler = channels["rotation_euler"]
        if "rotation_quaternion" in channels:
            obj.rotation_quaternion = channels["rotation_quaternion"]
        if "scale" in channels:
            obj.scale = channels["scale"]

    for data_path in channels:
        if not obj.keyframe_insert(data_path=data_path, frame=frame):
            raise RuntimeError(f"Blender refused keyframe insertion for {obj.name}:{data_path} at frame {frame}")
    return list(channels)


@contextlib.contextmanager
def _restored_object_transform(obj):
    """
    Hand the object back the transform channels it arrived with, whatever the block does.

    Keying writes the requested values onto the object first. With the keyed action assigned
    those values are what it plays; keyed into an unassigned clip they drive nothing, and left
    in place they would be an unkeyed edit to the object's own pose.

    Args:
        obj: The object being keyed.

    """
    snapshot = {name: tuple(getattr(obj, name)) for name in _TRANSFORM_CHANNELS if hasattr(obj, name)}
    try:
        yield
    finally:
        for name, value in snapshot.items():
            setattr(obj, name, value)


def _batch_owner(prepared, action_name):
    """
    Name the one object a batch naming an action keys, before anything is assigned or written.

    An ID holds one action, so a batch naming several objects while naming one action is asking
    for every object's keys to land in the same place: whichever object is assigned last wins
    the action and the rest are keyed into whatever else was driving them. That is almost never
    what an agent means by it, and it is indistinguishable afterwards from the keys having
    worked, so it is refused rather than resolved.

    Args:
        prepared: The validated records, read for the objects they key.
        action_name: The action every key in this batch belongs in.

    Returns:
        The batch's single object.

    Raises:
        ValueError: If the batch names more than one distinct object.

    """
    objects = {entry["object_name"]: entry["object"] for entry in prepared}
    if len(objects) > 1:
        raise ValueError(
            f"action_name='{action_name}' names one action, but this batch keys {len(objects)} objects "
            f"({', '.join(sorted(objects))}): an object holds one action, so call keyframe_object_transform "
            "once per object, naming the action that object's keys belong in"
        )
    return next(iter(objects.values()))


def _hidden_strip_warnings(objects):
    """
    Collect the NLA-override notice for every object a batch keyed into its active action.

    Args:
        objects: The keyed objects, repeats allowed.

    Returns:
        list[str]: One notice per affected object up to `_MAX_HIDDEN_STRIP_NOTICES`, then one line
        counting the rest - warnings are lifted whole and never paged.

    """
    unique = {obj.name: obj for obj in objects}
    affected = [(name, notice) for name, obj in unique.items() for notice in hidden_strip_warning(obj)]
    listed = [notice for _name, notice in affected[:_MAX_HIDDEN_STRIP_NOTICES]]
    remainder = affected[_MAX_HIDDEN_STRIP_NOTICES:]
    if remainder:
        listed.append(
            f"{len(remainder)} further keyed object(s) play an active action over NLA strips it overrides the same "
            f"way, starting with: {', '.join(name for name, _notice in remainder[:_MAX_HIDDEN_STRIP_NOTICES])}."
        )
    return listed


def _borrow_named_action(
    borrowed, owner, action_name, policy, slot_identifier, warnings, *, confirm_displace, assign_action
):
    """
    Put the batch's object on the named action for as long as `borrowed` stays open.

    Args:
        borrowed: The `ExitStack` the keying runs inside; the restores are entered on it.
        owner: The batch's single object.
        action_name: The action every key in this batch belongs in.
        policy: ENSURE, CREATE or REUSE - see `assign_named_action`.
        slot_identifier: Which of the action's slots to key into, or None to resolve it.
        warnings: The reply's warnings, where a slot the restore could not put back is named.
        confirm_displace: Whether the caller confirmed displacing an action that holds keys.
        assign_action: False keys a clip: the assignment and the object's transform come back
            on success too, and nothing is displaced for the guard to refuse.

    Returns:
        bpy.types.Action: The action the keys land in.

    """
    borrowed.enter_context(restored_action_assignment(owner, warnings, only_on_error=assign_action))
    if not assign_action:
        borrowed.enter_context(_restored_object_transform(owner))
    return assign_named_action(
        owner, action_name, policy, slot_identifier, confirm_displace=confirm_displace or not assign_action
    )


def _refuse_existing_keys(prepared):
    """
    Refuse an INSERT_ONLY batch any of whose channels already holds a key at its frame.

    Args:
        prepared: The validated records, read against the action each object is now keyed into.

    Raises:
        ValueError: Naming the first record that would overwrite a key.

    """
    for entry in prepared:
        existing = [path for path in entry["channels"] if _has_key_at(entry["object"], path, entry["frame"])]
        if existing:
            raise ValueError(f"A key already exists at {entry['label']} for {existing}; INSERT_ONLY made no changes")


def _prepare_records(keyframes):
    prepared = []
    seen = set()
    scene_cache = {}
    for index, source in enumerate(keyframes):
        if not isinstance(source, dict):
            raise ValueError(f"keyframes[{index}] must be an object")
        record = dict(source)
        label = f"keyframes[{index}]"
        object_name = _required_name(record.get("object_name"), f"{label}.object_name")
        obj = _object(object_name)
        frame = _resolve_frame(record, label, scene_cache)
        space = record.get("space", "WORLD")
        if space not in _SPACES:
            raise ValueError(f"{label}.space must be one of {sorted(_SPACES)}")
        channels = _resolve_channels(record, label, obj)
        identity = (object_name, frame)
        if identity in seen:
            raise ValueError(
                f"Duplicate keyframe destination at {label}: combine every channel for one object at "
                "one frame into a single record instead of separate records"
            )
        seen.add(identity)
        prepared.append(
            {
                "object": obj,
                "object_name": object_name,
                "label": label,
                "frame": frame,
                "space": space,
                "channels": channels,
            }
        )
    return prepared


def _in_frame_order(prepared):
    """
    Order each object's records by frame, leaving every other object's records where they were.

    A WORLD rotation is re-spelled against the channel's previous key, which only exists if the
    earlier frame was keyed first: listed descending, every key found nothing before it and kept
    the setter's +-180 spelling. Each object's records are sorted into the slots that object
    already held, so the interleaving across objects - a parent keyed before its child at a
    frame - stays as the caller listed it.

    Args:
        prepared: The validated records, in the caller's order.

    Returns:
        list: The same records, each object's ascending by frame.

    """
    by_object = {}
    for entry in prepared:
        by_object.setdefault(entry["object_name"], []).append(entry)
    ascending = {name: iter(sorted(entries, key=lambda item: item["frame"])) for name, entries in by_object.items()}
    return [next(ascending[entry["object_name"]]) for entry in prepared]


class ObjectAnimationHandlersMixin:
    """Keyframe an object's location/rotation/scale, in local or world space, across a scene."""

    def keyframe_object_transform(
        self,
        keyframes,
        policy="REPLACE_EXISTING",
        interpolation="BEZIER",
        handle_left="AUTO_CLAMPED",
        handle_right="AUTO_CLAMPED",
        action_name=None,
        action_policy="ENSURE",
        action_slot_identifier=None,
        confirm_displace_action=False,
        assign_action=True,
    ):
        if not isinstance(keyframes, list) or not keyframes:
            raise ValueError("keyframes must contain at least one record")
        if policy not in _POLICIES:
            raise ValueError(f"policy must be one of {sorted(_POLICIES)}")
        style = KeyStyle(interpolation, handle_left, handle_right)
        style.validate()
        if action_policy not in ACTION_POLICIES:
            raise ValueError(f"action_policy must be one of {list(ACTION_POLICIES)}")
        if not assign_action and action_name is None:
            raise ValueError("assign_action=False keys a named clip and requires action_name")

        prepared = _prepare_records(keyframes)

        keyed_action = None
        restore_warnings = []
        with contextlib.ExitStack() as borrowed:
            if action_name is not None:
                keyed_action = _borrow_named_action(
                    borrowed,
                    _batch_owner(prepared, action_name),
                    action_name,
                    action_policy,
                    action_slot_identifier,
                    restore_warnings,
                    confirm_displace=confirm_displace_action,
                    assign_action=assign_action,
                )
            if policy == "INSERT_ONLY":
                # Asked after the action is assigned, because "a key already exists here" is a
                # question about the action this call is about to write into, not about whatever
                # happened to be driving the object when the call arrived.
                _refuse_existing_keys(prepared)
            touched = {}
            for entry in prepared:
                touched.setdefault(entry["object_name"], (entry["object"], set()))[1].update(entry["channels"])
            for owner, data_paths in touched.values():
                # Entered last, so it unwinds first: the keys go back while each object is still
                # on the action they were written into.
                borrowed.enter_context(restored_keys_on_error(owner, data_paths))

            # Measured before a key is written: inserting one moves the extent it is measured
            # against, and the question is which cycle the call arrived to.
            warnings = _cycle_extension_warnings(prepared)
            if any(entry["space"] == "WORLD" for entry in prepared):
                borrowed.enter_context(restored_playhead(bpy.context.scene))
            inserted_keys = {}
            for entry in _in_frame_order(prepared):
                inserted = _apply_and_key(entry["object"], entry["frame"], entry["space"], entry["channels"])
                inserted_keys[entry["label"]] = [
                    record
                    for data_path in inserted
                    for record in _style_inserted_keys(entry["object"], data_path, entry["frame"], style)
                ]
            # Reported in the order the caller listed the records, whatever order they were keyed in.
            changed_keys = [record for entry in prepared for record in inserted_keys[entry["label"]]]

            changed_objects = list(dict.fromkeys(entry["object_name"] for entry in prepared))
            actions = sorted(
                {
                    action.name
                    for entry in prepared
                    for action in [_action_fcurves(entry["object"])[0]]
                    if action is not None
                }
            )
            # One slot is only well defined when one object was keyed: a batch spanning several
            # objects lands in as many slots as it has objects, and `actions` already names those.
            single_owner = prepared[0]["object"] if len(changed_objects) == 1 else None
            action_slot = assigned_slot_identifier(single_owner) if single_owner is not None else None
        warnings += restore_warnings
        if assign_action:
            warnings += _hidden_strip_warnings([entry["object"] for entry in prepared])
        return {
            "keyframes": changed_keys,
            "actions": actions,
            "action_slot": action_slot,
            # What drives the keyed object now: under assign_action=False, still its own action
            # rather than the clip `actions` names. Null for a batch spanning several objects.
            "assigned_action": assigned_action_name(single_owner),
            # Zero for a clip nothing holds yet: Blender drops it at save until a strip or an
            # assignment uses it. Null when no action was named.
            "keyed_action_users": keyed_action.users if keyed_action is not None else None,
            "policy": policy,
            # The envelope lifts these, so a period this call silently redefined reaches a
            # caller who read nothing but the warnings.
            "warnings": warnings,
            "changed_objects": changed_objects,
            "changed_resources": actions,
        }
