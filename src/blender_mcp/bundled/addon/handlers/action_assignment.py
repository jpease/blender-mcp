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

import bpy

# The three ways a caller can mean "this action". ENSURE is what an agent almost always wants
# (key here, whether or not the action is already in the file); CREATE and REUSE are assertions
# about the state of the file, and their value is that a wrong assumption becomes a refusal
# instead of a second action nobody asked for.
ACTION_POLICIES = ("ENSURE", "CREATE", "REUSE")


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


def _refuse_displacement(id_owner, action_name):
    """
    Refuse to unassign an action that holds keys.

    The keys in the assigned action are authored work - root motion keyed before a pose, most
    often - and unassigning it stops them driving anything and hands them to the next save to
    delete. AGENTS.md puts destructive acts behind a confirmation for exactly this reason: the
    caller may well mean it, but it has to be said rather than inferred from the call order.

    Args:
        id_owner: The ID about to be reassigned. Read, never written, so a refusal leaves the
            rig exactly as it was found.
        action_name: The action the caller asked to key into.

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
        f"Keying into '{action_name}' would unassign '{current.name}' from '{id_owner.name}', and "
        f"'{current.name}' holds {held} F-Curve(s) that would then drive nothing and be dropped at save. "
        f"Key into the same action by passing action_name='{current.name}', or pass "
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
        _refuse_displacement(id_owner, action_name)
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
