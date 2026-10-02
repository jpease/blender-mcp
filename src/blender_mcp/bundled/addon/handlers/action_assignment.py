"""
Assign a named Action to an ID's animation data, shared by every handler that authors keys.

An ID holds exactly one assigned action at a time, so assigning is always also an
unassignment, and an action nothing references carries zero users and is dropped when the file
is saved. That is how a shot lost its root motion: the location curves were keyed into one
action by `keyframe_object_transform`, the pose into a second by `keyframe_character_pose`,
and the second assignment left the first driving nothing at all. The characters stood still
and both replies said they had succeeded. The guard below is the answer - a call that would
displace an action holding keys has to ask for that on purpose - and it lives here rather than
in either handler because it is the same rig, the same animation data, and the same mistake
whichever of the two tools makes it.
"""

import contextlib
import math

import bpy

# The three ways a caller can mean "this action". ENSURE is what an agent almost always wants
# (key here, whether or not the action is already in the file); CREATE and REUSE are assertions
# about the state of the file, and their value is that a wrong assumption becomes a refusal
# instead of a second action nobody asked for.
ACTION_POLICIES = ("ENSURE", "CREATE", "REUSE")
# The hidden-strip notice is lifted whole into the envelope and never paged, so it names this
# many strips and channels and counts the rest.
_MAX_LISTED_OVERRIDES = 3
# What a keyframe point is, for a failed call to put back exactly. Vectors are read as tuples
# and written last: a handle written before its key's frame and type are back would be moved
# again by them. Blender's own `select_*` flags are restored too, since inserting a key
# selects it.
_POINT_VECTORS = ("co", "handle_left", "handle_right")
_POINT_FIELDS = (
    "interpolation",
    "easing",
    "back",
    "amplitude",
    "period",
    "type",
    "handle_left_type",
    "handle_right_type",
    "select_control_point",
    "select_left_handle",
    "select_right_handle",
)
# What a curve carries besides its keys. Blender removes an F-Curve whose last key is deleted,
# settings and modifiers with it, so a curve a failed call deleted or replaced is rebuilt from these.
_CURVE_FIELDS = ("extrapolation", "mute", "hide", "lock", "select", "color_mode", "auto_smoothing")


def action_fcurve_collections(action, slot=None):
    """
    Return the mutable F-Curve collections of a legacy or a Blender 5.x layered action.

    A layered action keeps its curves in a channelbag per (strip, slot) rather than in one flat
    `fcurves` collection, and `Action.fcurves` survives only as a compatibility view onto the
    same curves - so where the layered walk finds anything it wins outright, and reading both
    would count every curve twice.

    Args:
        action: The action to walk.
        slot: Narrow the walk to one action slot, which is what an ID sharing an action with
            other IDs needs: its own keys live in its own slot's channelbag and the other
            slots' curves belong to somebody else. None walks every channelbag in the action,
            which is what a question about the action as a whole ("does it hold any keys at
            all?") has to ask.

    Returns:
        list: The F-Curve collections, each still live - writing to one writes to the action.

    """
    layered = []
    for layer in getattr(action, "layers", ()):
        for strip in getattr(layer, "strips", ()):
            if slot is None:
                layered.extend(channelbag.fcurves for channelbag in getattr(strip, "channelbags", ()))
            elif getattr(strip, "type", None) == "KEYFRAME":
                # Only a keyframe strip has channelbags to ask for; `ensure=False` so reading
                # the action never creates the channelbag it was looking for.
                channelbag = strip.channelbag(slot, ensure=False)
                if channelbag is not None:
                    layered.append(channelbag.fcurves)
    if layered:
        return layered
    legacy = getattr(action, "fcurves", None)
    return [] if legacy is None else [legacy]


def cycled_curve_extent(curve):
    """
    Report the frames one curve already repeats, when a Cycles modifier makes it repeat at all.

    A Cycles F-Modifier repeats its own curve's first-to-last key extent and nothing else, so a
    key written outside that extent silently redefines the period - the trap that cost a walk
    cycle its interior repeats. Every handler that writes a key asks the same question of the
    same objects, so the question is asked in one place; each caller keeps its own frame
    tolerance, because a frame compared against this extent is compared in that caller's units.

    Args:
        curve: The F-Curve to read. Its keys and modifiers are read, never written.

    Returns:
        tuple[float, float] | None: The first and last key frames this curve repeats, or None
        when it carries no Cycles modifier, has fewer than two keys, or spans no frames at all -
        in every one of those cases there is no established period for a new key to redefine.

    """
    if not any(modifier.type == "CYCLES" for modifier in curve.modifiers):
        return None
    frames = [float(point.co[0]) for point in curve.keyframe_points]
    if len(frames) <= 1:
        return None
    first, last = min(frames), max(frames)
    return None if first == last else (first, last)


def refuse_displacement(id_owner, action_name):
    """
    Refuse to unassign an action that holds keys.

    The keys in the assigned action are authored work - root motion keyed before a pose, most
    often - and unassigning it stops them driving anything and hands them to the next save to
    delete. AGENTS.md puts destructive acts behind a confirmation for exactly this reason: the
    caller may well mean it, but it has to be said rather than inferred from the call order.
    Every tool that assigns an action asks this one question, so `confirm_displace_action` means
    the same thing on each of them.

    Args:
        id_owner: The ID about to be reassigned. Read, never written, so a refusal leaves the
            ID exactly as it was found.
        action_name: The action the caller asked to assign.

    Raises:
        ValueError: If a different action holding at least one F-Curve is assigned.

    """
    animation = getattr(id_owner, "animation_data", None)
    current = getattr(animation, "action", None) if animation is not None else None
    if current is None or current.name == action_name:
        return
    held = sum(len(collection) for collection in action_fcurve_collections(current))
    if not held:
        # An empty action drives nothing, so displacing it discards no work.
        return
    raise ValueError(
        f"Assigning '{action_name}' would unassign '{current.name}' from '{id_owner.name}', and "
        f"'{current.name}' holds {held} F-Curve(s) that would then drive nothing and be dropped at save. "
        f"Keep working in it by passing action_name='{current.name}', or pass "
        f"confirm_displace_action=True to move '{id_owner.name}' onto '{action_name}' and leave the "
        "earlier keys behind."
    )


def assign_named_action(id_owner, action_name, policy, slot_identifier=None, confirm_displace=False):
    """
    Resolve `action_name` under `policy` and leave it assigned to `id_owner`, slot included.

    Args:
        id_owner: Anything with `animation_data` and `animation_data_create()` - an armature
            object and a plain object are both keyed through here.
        action_name: The action to key into.
        policy: ENSURE creates the action when it is missing and reuses it when it is there;
            CREATE requires it to be missing; REUSE requires it to be there.
        slot_identifier: Which of a layered action's slots to key into, by its `identifier`.
            Only needed when the action carries several and none is unambiguously suitable.
        confirm_displace: Proceed even though a different action holding keys is assigned. The
            keys in that action stay in the file but stop driving `id_owner`.

    Returns:
        bpy.types.Action: The action now driving `id_owner`.

    Raises:
        ValueError: If the policy is unknown or its assertion about the file is wrong, if the
            slot is missing or ambiguous, or if the call would displace authored keys without
            confirmation.

    """
    if policy not in ACTION_POLICIES:
        raise ValueError(f"action_policy must be one of {list(ACTION_POLICIES)}")
    if not confirm_displace:
        # Before the action is resolved, so a refusal never leaves a freshly created and
        # entirely empty action behind in the file.
        refuse_displacement(id_owner, action_name)
    action = bpy.data.actions.get(action_name)
    if action is None:
        if policy == "REUSE":
            raise ValueError(f"Action not found: {action_name}")
        action = bpy.data.actions.new(action_name)
    elif policy == "CREATE":
        raise ValueError(f"Action already exists: {action_name}")
    animation = id_owner.animation_data_create()
    animation.action = action
    slots = list(getattr(action, "slots", ()))
    if slot_identifier is not None:
        slot = next((candidate for candidate in slots if candidate.identifier == slot_identifier), None)
        if slot is None:
            raise ValueError(f"Action slot not found on '{action.name}': {slot_identifier}")
        animation.action_slot = slot
    elif slots:
        suitable = list(getattr(animation, "action_suitable_slots", ()))
        if len(suitable) == 1:
            animation.action_slot = suitable[0]
        elif len(suitable) > 1:
            raise ValueError(f"Action '{action.name}' has multiple suitable slots; action_slot_identifier is required")
        elif len(slots) == 1:
            animation.action_slot = slots[0]
        else:
            raise ValueError(f"Action '{action.name}' has multiple slots; action_slot_identifier is required")
    return action


def assigned_action_name(id_owner):
    """
    Report which action drives `id_owner` now, or None when it has none.

    Args:
        id_owner: The ID to read; it may have no animation data at all.

    Returns:
        str | None: The assigned action's name.

    """
    animation = getattr(id_owner, "animation_data", None)
    return getattr(getattr(animation, "action", None), "name", None)


def assigned_slot_identifier(id_owner):
    """
    Report which action slot `id_owner`'s keys are landing in, or None when it has no slot.

    Args:
        id_owner: The ID that was just keyed.

    Returns:
        str | None: The assigned slot's `identifier`.

    """
    animation = getattr(id_owner, "animation_data", None)
    return getattr(getattr(animation, "action_slot", None), "identifier", None)


@contextlib.contextmanager
def restored_action_assignment(id_owner, warnings, *, only_on_error=True):
    """
    Hand an ID back the action and slot it arrived on: if the block raises, or always.

    A call that keyed into the action it assigned leaves that action assigned on purpose: an
    action nothing references carries zero users and Blender drops it at save. A call that
    raised authored nothing, so the action it displaced has to come back - `object_state` does
    not snapshot `animation_data.action`, so nothing else in the transaction would put it back,
    and the only symptom is a shot whose character has quietly stopped moving. A call keying a
    clip with `assign_action=False` borrows the assignment only because `keyframe_insert` writes
    into whatever is assigned, so it hands the assignment back on success as well.

    Animation data this borrow had to create is cleared again on restore while it holds no NLA
    track and no driver, so an ID that arrived without any leaves without an empty block.

    Assigning an action resets the slot, and Blender may refuse the old slot back (one it no
    longer considers suitable). That refusal is never swallowed: on success it becomes a notice
    in `warnings` for the reply to carry, and on the error path it is raised in place of the
    original error, chained to it, with both messages in its text.

    Args:
        id_owner: The ID whose `animation_data` is borrowed, created here when it has none.
        warnings: The reply's warning list; a slot that could not be restored is named here.
        only_on_error: Restore only if the block raises. False restores on success too.

    Yields:
        bpy.types.Action | None: The action the ID arrived on, for the reply to name.

    Raises:
        RuntimeError: When the block raised and the slot could not be restored either.

    """
    created = getattr(id_owner, "animation_data", None) is None
    animation = id_owner.animation_data_create()
    previous_action = animation.action
    previous_slot = getattr(animation, "action_slot", None)

    def restore():
        animation.action = previous_action
        refusal = None
        if previous_action is not None and previous_slot is not None:
            try:
                animation.action_slot = previous_slot
            except Exception as error:
                refusal = (
                    f"'{id_owner.name}' is back on action '{previous_action.name}', but Blender refused its "
                    f"previous slot '{previous_slot.identifier}' ({error}); it is now on slot "
                    f"'{assigned_slot_identifier(id_owner)}'. Check it with inspect_animation."
                )
        if created and not len(getattr(animation, "nla_tracks", ())) and not len(getattr(animation, "drivers", ())):
            id_owner.animation_data_clear()
        return refusal

    try:
        yield previous_action
    except BaseException as original:
        refusal = restore()
        if refusal is None:
            raise
        if isinstance(original, Exception):
            raise RuntimeError(f"{original}. {refusal}") from original
        # An abort keeps its own type; the refusal still travels with it.
        original.add_note(refusal)
        raise
    if not only_on_error:
        refusal = restore()
        if refusal is not None:
            warnings.append(refusal)


def _writable_fields(struct):
    """
    Read every plain RNA setting of one struct that a script may write back.

    Args:
        struct: An F-Modifier: its settings differ per type, so they are read off its RNA.

    Returns:
        dict: Property identifier to value, arrays as tuples; pointers, collections and
        read-only properties are left out, because none of them can be written back.

    """
    fields = {}
    for prop in struct.bl_rna.properties:
        if prop.is_readonly or prop.type in {"POINTER", "COLLECTION"}:
            continue
        value = getattr(struct, prop.identifier)
        fields[prop.identifier] = tuple(value) if getattr(prop, "array_length", 0) else value
    return fields


def _point_state(point):
    return (
        tuple(tuple(getattr(point, name)) for name in _POINT_VECTORS),
        tuple(getattr(point, name) for name in _POINT_FIELDS),
    )


def _curve_state(curve):
    return {
        "group": curve.group.name if curve.group is not None else "",
        "fields": {name: getattr(curve, name) for name in _CURVE_FIELDS},
        "color": tuple(curve.color),
        "modifiers": [(modifier.type, _writable_fields(modifier)) for modifier in curve.modifiers],
        "points": [_point_state(point) for point in curve.keyframe_points],
    }


def _key_snapshot(action, slot, data_paths):
    """
    Record the curves of `data_paths` one call is about to write, per F-Curve collection.

    Args:
        action: The action the keys will land in.
        slot: The slot they land in, or None to read every channelbag the action has.
        data_paths: The curve paths the call may write; nothing else is read.

    Returns:
        list[dict]: Per collection, in `action_fcurve_collections` order, each touched curve's
        state keyed by `(data_path, array_index)`.

    """
    return [
        {
            (curve.data_path, curve.array_index): _curve_state(curve)
            for curve in collection
            if curve.data_path in data_paths
        }
        for collection in action_fcurve_collections(action, slot)
    ]


def _restore_points(curve, points):
    """
    Put one curve's keyframe points back exactly as recorded, when they differ at all.

    Args:
        curve: The live F-Curve.
        points: `_point_state` records in the curve's own order.

    """
    if [_point_state(point) for point in curve.keyframe_points] == points:
        return
    keyframes = curve.keyframe_points
    keyframes.clear()
    keyframes.add(len(points))
    # Frames first, then the settings that move handles, then the handles themselves: each
    # stage is written only once the one it depends on is complete for every key.
    for point, (vectors, _fields) in zip(keyframes, points, strict=True):
        point.co = vectors[0]
    for point, (_vectors, fields) in zip(keyframes, points, strict=True):
        for name, value in zip(_POINT_FIELDS, fields, strict=True):
            setattr(point, name, value)
    for point, (vectors, _fields) in zip(keyframes, points, strict=True):
        point.handle_left, point.handle_right = vectors[1], vectors[2]


def _restore_curve(curve, state):
    """
    Put one curve back as recorded: settings, modifiers and keys, each only where it differs.

    A curve that is present again need not be the curve that was recorded: REPLACE deletes a
    lone key, Blender deletes the emptied curve with it, and the insert that follows creates a
    bare one in its place - extrapolation default, Cycles modifier gone.

    Args:
        curve: The live F-Curve.
        state: Its `_curve_state` record.

    """
    for name, value in state["fields"].items():
        if getattr(curve, name) != value:
            setattr(curve, name, value)
    if tuple(curve.color) != state["color"]:
        curve.color = state["color"]
    if [(modifier.type, _writable_fields(modifier)) for modifier in curve.modifiers] != state["modifiers"]:
        for modifier in list(curve.modifiers):
            curve.modifiers.remove(modifier)
        for modifier_type, fields in state["modifiers"]:
            modifier = curve.modifiers.new(modifier_type)
            for name, value in fields.items():
                setattr(modifier, name, value)
    _restore_points(curve, state["points"])


def _restore_keys(id_owner, action, slot, data_paths, snapshot):
    """
    Undo every write a failed call made to the curves of `data_paths`.

    Args:
        id_owner: The keyed ID, read for the action and slot the keys actually went into
            when the call arrived without one.
        action: The action recorded on entry, or None when the call had to create one.
        slot: The slot recorded on entry, or None.
        data_paths: The curve paths the call may have written.
        snapshot: `_key_snapshot` output, empty when there was no action to read.

    """
    animation = getattr(id_owner, "animation_data", None)
    if action is None:
        # The first key created the action; every curve of these paths in it is the call's.
        action = getattr(animation, "action", None)
        if action is None:
            return
    if slot is None and getattr(animation, "action", None) == action:
        # A slot-less action gains one with its first key, and that is where the keys went.
        slot = getattr(animation, "action_slot", None)
    collections = action_fcurve_collections(action, slot)
    for position, collection in enumerate(collections):
        recorded = snapshot[position] if position < len(snapshot) else {}
        present = set()
        for curve in list(collection):
            identity = (curve.data_path, curve.array_index)
            if curve.data_path not in data_paths:
                continue
            state = recorded.get(identity)
            if state is None:
                collection.remove(curve)
                continue
            present.add(identity)
            _restore_curve(curve, state)
        for identity, state in recorded.items():
            if identity not in present:
                # Blender removes a curve with its last key, so a deleted one is rebuilt.
                data_path, array_index = identity
                _restore_curve(collection.new(data_path, index=array_index, group_name=state["group"]), state)


@contextlib.contextmanager
def restored_keys_on_error(id_owner, data_paths, *, action=None, slot=None):
    """
    Put back every key a call wrote into the curves of `data_paths` if the block raises.

    Restoring the assignment is not enough once the action outlives the call: a REUSE or an
    existing ENSURE action is the shot's animation, possibly shared with other IDs and NLA
    strips, and a batch that failed at its tenth entry left the first nine entries' keys in
    it - overwritten, inserted or deleted - with an error reply saying nothing had changed.
    `object_state` does not snapshot curves, so this is the only thing that hands them back.
    Curves the call created are removed, curves it deleted are rebuilt, and every recorded
    curve gets its keys back point for point: frame, value, handles, interpolation, easing,
    key type and selection. Only the curves of `data_paths` are read, so the snapshot costs
    what the call writes rather than what the action holds. A rebuilt curve goes to the end of
    its collection, and a channel group a created curve brought with it stays, empty.

    Args:
        id_owner: The ID being keyed.
        data_paths: Every F-Curve data_path the block may write, insert into or delete from.
        action: The action written into. None reads the one `id_owner` is assigned now - or,
            when it has none, the one its first key creates.
        slot: The slot written into, with `action`. None reads `id_owner`'s.

    Raises:
        RuntimeError: When the block raised and its keys could not all be put back either,
            carrying both messages.

    """
    if action is None:
        animation = getattr(id_owner, "animation_data", None)
        action = getattr(animation, "action", None)
        slot = getattr(animation, "action_slot", None) if action is not None else None
    paths = frozenset(data_paths)
    snapshot = _key_snapshot(action, slot, paths) if action is not None else []
    try:
        yield
    except BaseException as original:
        try:
            _restore_keys(id_owner, action, slot, paths, snapshot)
        except Exception as failure:
            raise RuntimeError(
                f"{original}. The keys this call had already written could not all be put back ({failure}); "
                f"check '{id_owner.name}' with inspect_animation."
            ) from original
        raise


def _slot_channels(action, slot):
    """
    Walk the `(data_path, array_index)` channels one action slot animates, lazily.

    Lazy so a caller asking only whether a strip shares any channel stops at the first one.

    Args:
        action: The action to read, or None.
        slot: The slot whose channelbag is read, or None.

    Yields:
        tuple[str, int]: One channel per curve; nothing when either half is missing - a strip
        or an assignment without a slot animates nothing.

    """
    if action is None or slot is None:
        return
    for collection in action_fcurve_collections(action, slot):
        for curve in collection:
            yield (curve.data_path, curve.array_index)


def _overlaps(first, second):
    """
    Say whether two closed frame intervals share at least one frame.

    Args:
        first: `(start, end)`, either end possibly infinite.
        second: The same.

    Returns:
        bool: True when they intersect.

    """
    return first[0] <= second[1] and second[0] <= first[1]


def _active_action_frames(animation, action):
    """
    Measure the frames the active action is evaluated on over the NLA.

    Blender evaluates the active action as one more strip on top of the stack, spanning the
    action's `frame_range`; `action_extrapolation` decides what it does outside that range.

    Args:
        animation: The ID's `animation_data`.
        action: Its active action.

    Returns:
        tuple[float, float]: The interval, an end infinite where the action holds.

    """
    first, last = (float(frame) for frame in action.frame_range)
    extrapolation = animation.action_extrapolation
    if extrapolation == "HOLD":
        return (-math.inf, math.inf)
    if extrapolation == "HOLD_FORWARD":
        return (first, math.inf)
    return (first, last)


def _strip_frames(strips, index):
    """
    Measure the frames one strip is evaluated on within its track, extrapolation included.

    Blender holds a strip only through the gap up to the next strip in the same track, and holds
    before a strip only when it is the track's first and its extrapolation is HOLD (a later
    strip set to HOLD behaves as HOLD_FORWARD).

    Args:
        strips: The track's strips in frame order, muted ones included - they still occupy
            their frames.
        index: Which strip to measure.

    Returns:
        tuple[float, float]: The interval, an end infinite where the strip holds.

    """
    strip = strips[index]
    extrapolation = strip.extrapolation
    start = -math.inf if extrapolation == "HOLD" and index == 0 else float(strip.frame_start)
    if extrapolation == "NOTHING":
        end = float(strip.frame_end)
    elif index + 1 < len(strips):
        end = float(strips[index + 1].frame_start)
    else:
        end = math.inf
    return (start, end)


def _evaluated_layers(animation):
    """
    List what Blender evaluates for one ID, bottom to top: each strip, then the active action.

    Unmuted strips on unmuted tracks, solo honoured - a solo track disables every other track
    and the active action too.

    Args:
        animation: The ID's `animation_data`.

    Returns:
        list[dict]: One record per layer, with `track` (its index, the active action's above
        every track), `label`, `name`, `strip` (None for the active action), the `action` and
        `slot` it plays, `frames` it plays on, a strip's `own_frames` (frame_start..frame_end),
        and `overrides` - True when it replaces whatever is below at full influence. Channels
        are not read here: most layers never need theirs.

    """
    tracks = list(animation.nla_tracks)
    solo = any(track.is_solo for track in tracks)
    layers = []
    for track_index, track in enumerate(tracks):
        if track.mute or (solo and not track.is_solo):
            continue
        strips = sorted(track.strips, key=lambda strip: float(strip.frame_start))
        for index, strip in enumerate(strips):
            if strip.mute:
                continue
            layers.append(
                {
                    "track": track_index,
                    "label": f"strip '{strip.name}'",
                    "name": strip.name,
                    "strip": strip,
                    "action": strip.action,
                    "slot": getattr(strip, "action_slot", None),
                    "frames": _strip_frames(strips, index),
                    "own_frames": (float(strip.frame_start), float(strip.frame_end)),
                    # A strip's influence is Blender's own (blend-in/out) unless an F-Curve
                    # animates it, and then it is unknowable from one frame.
                    "overrides": strip.blend_type == "REPLACE" and not strip.use_animated_influence,
                }
            )
    action = animation.action
    if not solo and action is not None:
        layers.append(
            {
                "track": len(tracks),
                "label": f"its active action '{action.name}'",
                "name": action.name,
                "strip": None,
                "action": action,
                "slot": getattr(animation, "action_slot", None),
                "frames": _active_action_frames(animation, action),
                "overrides": animation.action_blend_type == "REPLACE" and animation.action_influence >= 1.0,
            }
        )
    return layers


def _bounded(items):
    """
    Join a sorted list for a notice, naming `_MAX_LISTED_OVERRIDES` and counting the rest.

    Args:
        items: The already-formatted entries.

    Returns:
        str: The joined text.

    """
    listed = ", ".join(items[:_MAX_LISTED_OVERRIDES])
    if len(items) > _MAX_LISTED_OVERRIDES:
        listed += f" and {len(items) - _MAX_LISTED_OVERRIDES} more"
    return listed


def _overrides(layers, strip):
    """
    Find which strips the layers above them override, and on which channels.

    Exact counts need every overridden channel, but not every strip read whole: once all the
    channels an overriding layer animates are counted, a strip below it only has to be found
    sharing one. And every check that reads no curve - blend, track order, the strip asked
    about, frames - comes first, so a layer with nothing under it never has its channels read.

    Args:
        layers: `_evaluated_layers` output.
        strip: As `hidden_strip_warning`'s.

    Returns:
        tuple: The hidden strips' names (set), every overridden `(data_path, array_index)` (set),
        and each overriding layer's label mapped to whether it is the active action.

    """
    hidden = set()
    overridden = set()
    overriders = {}
    for upper in layers:
        if not upper["overrides"]:
            continue
        if strip is None and upper["strip"] is not None:
            continue
        lowers = [
            lower
            for lower in layers
            if lower["strip"] is not None
            and lower["track"] < upper["track"]
            # `!=`, never `is`: Blender hands out a new Python wrapper per access, equal by pointer.
            and (strip is None or strip == lower["strip"] or strip == upper["strip"])
            and _overlaps(upper["frames"], lower["own_frames"])
        ]
        channels = set(_slot_channels(upper["action"], upper["slot"])) if lowers else set()
        for lower in lowers if channels else ():
            keyed = _slot_channels(lower["action"], lower["slot"])
            if channels <= overridden:
                hides = any(identity in channels for identity in keyed)
            else:
                shared = {identity for identity in keyed if identity in channels}
                overridden |= shared
                hides = bool(shared)
            if hides:
                hidden.add(lower["name"])
                overriders[upper["label"]] = upper["strip"] is None
    return hidden, overridden, overriders


def hidden_strip_warning(id_owner, strip=None):
    """
    Warn when a layer evaluated above an NLA strip overrides channels that strip keys.

    The active action is evaluated on top of the NLA stack, and a strip on a higher track over a
    lower one; at REPLACE with full influence a channel the upper layer animates is whatever it
    says, on every frame it plays, whatever the strip below says. A clip keyed into the active
    action and then added as a strip therefore plays nothing on those channels, and nothing in
    either reply said so. An override counts only where the upper layer plays - its
    extrapolation decides that - over the lower strip's own frame_start..frame_end.

    Args:
        id_owner: The ID whose NLA stack is read. Read, never written.
        strip: The strip a call just added: report what hides it and what it hides. None reports
            only what the active action hides, the one layer an assigning or keying call changes.

    Returns:
        list[str]: One bounded notice naming the overriding layers, the hidden strips and a few
        channels, else empty.

    """
    animation = getattr(id_owner, "animation_data", None)
    if animation is None or not getattr(animation, "use_nla", False):
        return []
    if getattr(animation, "use_tweak_mode", False):
        # In tweak mode the active action is the strip being edited, shown in its place.
        return []
    if not len(animation.nla_tracks):
        # No strip for anything to hide, and nothing below needs reading to say so.
        return []
    hidden, overridden, overriders = _overrides(_evaluated_layers(animation), strip)
    if not hidden:
        return []
    shared_channels = sorted(overridden)
    channels = _bounded([f"{path}[{index}]" for path, index in shared_channels])
    names = _bounded([f"'{name}'" for name in sorted(hidden)])
    # The active action first: it is the layer a keying call just changed.
    labels = _bounded(sorted(overriders, key=lambda label: (not overriders[label], label)))
    remedies = []
    if any(overriders.values()):
        remedies.append(
            "Unassign the active action (manage_animation_action UNASSIGN), move those keys into a strip of "
            "their own, or key the clip with assign_action=False."
        )
    if not all(overriders.values()):
        remedies.append(
            "Move the overriding strip off those frames, or change its blend_type or influence "
            "(manage_nla_tracks PATCH_STRIP)."
        )
    return [
        f"'{id_owner.name}' plays {labels} over the NLA at REPLACE, influence 1, overriding {len(shared_channels)} "
        f"channel(s) that strip(s) {names} also key ({channels}) on frames they share: those strip keys play "
        f"nothing there. {' '.join(remedies)}"
    ]
