"""
Rows guarding edits that reach every object sharing one datablock.

An armature's rest data, a light's datablock and a Geometry Nodes group can each have any
number of object users. The reply counts them by type beside a sample of names, and
`changed_objects` names only what the caller named: the rig, the light, or nothing when the
group itself, in `changed_resources`, is the change.

Label prefix: `data users:`.
"""

from .common import (
    ADDON_CR_STRUCTURE,
    ADDON_GN_AUTHORING,
    ADDON_LIGHTING_CONSTRUCTION,
    CRTT,
    GNT,
    LIGHTT,
    Revert,
)

_RIG_NODE = f"{CRTT}::test_a_rest_edit_on_widely_shared_armature_data_counts_its_users_and_names_only_the_rig"
_LIGHT_NODE = f"{LIGHTT}::test_configuring_a_widely_shared_light_counts_its_users_and_names_only_the_light"
_GROUP_NODE = f"{GNT}::test_patching_a_widely_used_group_counts_its_user_objects_and_changes_only_the_group"

ROWS: list[Revert] = [
    Revert(
        "data users: a rest-data edit names every rig sharing the armature data as changed again",
        ADDON_CR_STRUCTURE,
        '        "changed_objects": [armature_obj.name],\n    }\n',
        '        "changed_objects": sorted(obj.name for obj in users),\n    }\n',
        (_RIG_NODE,),
    ),
    Revert(
        "data users: a rest-data edit lists every armature user by name, uncounted",
        ADDON_CR_STRUCTURE,
        '        "data_users_changed": counted_page(\n'
        "            sorted(users, key=lambda obj: obj.name),"
        " type_of=lambda obj: obj.type, name_of=lambda obj: obj.name\n"
        "        ),\n",
        '        "data_users_changed": sorted(obj.name for obj in users),\n',
        (_RIG_NODE,),
    ),
    Revert(
        "data users: configure_light names every object sharing the light datablock as changed again",
        ADDON_LIGHTING_CONSTRUCTION,
        '            "changed_objects": [obj.name],\n            "changed_resources": [obj.data.name],\n',
        '            "changed_objects": [candidate.name for candidate in affected],\n'
        '            "changed_resources": [obj.data.name],\n',
        (_LIGHT_NODE,),
    ),
    Revert(
        "data users: configure_light lists every light datablock user by name, uncounted",
        ADDON_LIGHTING_CONSTRUCTION,
        '            "data_users": counted_page(\n'
        "                affected, type_of=lambda candidate: candidate.type, name_of=lambda candidate: candidate.name\n"
        "            ),\n",
        '            "data_users": [candidate.name for candidate in affected],\n',
        (_LIGHT_NODE,),
    ),
    Revert(
        "data users: a graph patch names every object running the group as changed again",
        ADDON_GN_AUTHORING,
        '            "affected_users": _user_objects_page(users),\n'
        '            "changed_resources": [replacement.name],\n',
        '            "affected_users": _user_objects_page(users),\n'
        '            "changed_resources": [replacement.name],\n'
        '            "changed_objects": sorted({user["object"] for user in users}),\n',
        (_GROUP_NODE,),
    ),
    Revert(
        "data users: a group's user objects are listed by name, uncounted",
        ADDON_GN_AUTHORING,
        "    return counted_page(objects, type_of=lambda obj: obj.type, name_of=lambda obj: obj.name)\n",
        "    return [obj.name for obj in objects]  # type: ignore[return-value]\n",
        (_GROUP_NODE,),
    ),
    Revert(
        "data users: a group's users are counted per modifier, so an object running it twice counts twice",
        ADDON_GN_AUTHORING,
        '    objects = [bpy.data.objects[name] for name in sorted({user["object"] for user in users})]\n',
        '    objects = sorted((bpy.data.objects[user["object"]] for user in users), key=lambda obj: obj.name)\n',
        (_GROUP_NODE,),
    ),
]
