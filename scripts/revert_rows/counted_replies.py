"""
Rows guarding replies that could name every object a bulk call touched, and now count them.

A managed rig's removal, a scene reset, a collection's membership, an ND cleanup, a Poly Haven
model import and the session's linked libraries each count what they cover by type beside a
sample of names, and `changed_objects` names only what the caller acts on next: nothing once
it is gone, the model's roots, or the objects that lost a modifier.

Label prefix: `counted replies:`.
"""

from .common import (
    ADDON_FILE_LIFECYCLE,
    ADDON_ND,
    ADDON_POLYHAVEN,
    ADDON_SCENE,
    FLT,
    NDOUTT,
    NDSTATUST,
    PHT,
    SCENETOOLT,
    SERVER_ND_TOOL,
    SERVER_POLYHAVEN_TOOL,
    SESSIONT,
    SRVPHT,
    Revert,
)

_RIG_NODE = f"{SCENETOOLT}::test_removing_a_managed_rig_counts_its_members_and_names_none_as_a_next_target"
_RESET_NODE = f"{SCENETOOLT}::test_reset_scene_counts_what_it_unlinked_and_names_the_scene_as_what_changed"
_COLLECTION_NODE = f"{SCENETOOLT}::test_a_collection_of_many_members_is_counted_and_is_itself_what_changed"
_CLEANUP_NODE = f"{NDSTATUST}::test_a_large_cleanup_counts_what_went_and_names_only_the_objects_that_lost_a_modifier"
_ND_LIFT_NODE = f"{NDOUTT}::test_nd_outcome_publishes_the_change_list_a_handler_names_itself_in_place_of_the_targets"
_ND_CANCEL_NODE = f"{NDOUTT}::test_nd_outcome_cancelled_drops_a_change_list_the_handler_named"
_MODEL_NODE = f"{PHT}::test_a_model_of_many_parts_is_counted_and_only_its_roots_are_changed_objects"
_MODEL_TOOL_NODE = f"{SRVPHT}::test_a_model_import_reports_the_roots_the_handler_named_not_every_imported_object"
_SESSION_NODE = f"{SESSIONT}::test_get_session_info_counts_every_library_and_carries_only_the_first_ten"
_SWAP_NODE = f"{FLT}::test_open_shot_reports_the_new_session_and_asks_for_a_rehandshake"

ROWS: list[Revert] = [
    # --- remove_scene_objects: a managed rig's members are counted, and none is a next target ---
    Revert(
        "counted replies: remove_scene_objects lists every removed object again",
        ADDON_SCENE,
        '            "removed": removed,\n',
        '            "removed": object_names,\n',
        (_RIG_NODE,),
    ),
    Revert(
        "counted replies: remove_scene_objects publishes the dependencies of every removed object",
        ADDON_SCENE,
        '            "dependencies": {name: dependencies[name] for name in sampled},\n',
        '            "dependencies": dependencies,\n',
        (_RIG_NODE,),
    ),
    Revert(
        "counted replies: a material the removed rig members share is retained once per member",
        ADDON_SCENE,
        '                    retained["MATERIAL", material["name"]] = {\n',
        '                    retained["MATERIAL", material["name"], len(retained)] = {\n',
        (_RIG_NODE,),
    ),
    Revert(
        "counted replies: remove_scene_objects names every removed object as changed",
        ADDON_SCENE,
        '            "purged_datablocks": [],\n            "changed_objects": [],\n',
        '            "purged_datablocks": [],\n            "changed_objects": object_names,\n',
        (_RIG_NODE,),
    ),
    # --- reset_scene: what left the scene is counted, and the scene is what changed ---
    Revert(
        "counted replies: reset_scene lists every object it unlinked",
        ADDON_SCENE,
        "        unlinked_objects = _counted_objects(sorted(scene.objects, key=lambda obj: obj.name))\n",
        "        unlinked_objects = sorted(obj.name for obj in scene.objects)\n",
        (_RESET_NODE,),
    ),
    Revert(
        "counted replies: reset_scene names every object it unlinked as changed",
        ADDON_SCENE,
        '            "changed_objects": [],\n            "changed_resources": [scene.name],\n',
        '            "changed_objects": [obj.name for obj in scene.objects],\n'
        '            "changed_resources": [scene.name],\n',
        (_RESET_NODE,),
    ),
    # --- manage_scene_collections: the collection's members are counted, the collection changed ---
    Revert(
        "counted replies: a collection reply lists every member of the collection",
        ADDON_SCENE,
        '            "objects": _counted_objects(list(collection.objects)),\n',
        '            "objects": [obj.name for obj in collection.objects],\n',
        (_COLLECTION_NODE,),
    ),
    Revert(
        "counted replies: a collection reply names every object it was handed as changed",
        ADDON_SCENE,
        '            "hide_render": collection.hide_render,\n            "changed_objects": [],\n',
        '            "hide_render": collection.hide_render,\n'
        '            "changed_objects": [obj.name for obj in objects],\n',
        (_COLLECTION_NODE,),
    ),
    Revert(
        "counted replies: a scene reply counts every object under one type",
        ADDON_SCENE,
        "    return counted_page(objects, type_of=lambda obj: obj.type, name_of=lambda obj: obj.name)\n",
        '    return counted_page(objects, type_of=lambda obj: "OBJECT", name_of=lambda obj: obj.name)\n',
        (_RIG_NODE, _RESET_NODE, _COLLECTION_NODE),
    ),
    # --- nd_clean_utils: a scene-wide cleanup is counted; the hosts it touched are named ---
    Revert(
        "counted replies: nd_clean_utils lists every utility object it removed",
        ADDON_ND,
        '"removed_objects": counted_page(removed_objects, type_of=before_types.__getitem__, name_of=str),',
        '"removed_objects": removed_objects,',
        (_CLEANUP_NODE,),
    ),
    Revert(
        "counted replies: nd_clean_utils publishes a record for every modifier it removed",
        ADDON_ND,
        '                **record_page("records", removed_modifiers, dict, MAX_LISTED_NAMES),\n',
        '                **record_page("records", removed_modifiers, dict, len(removed_modifiers) or 1),\n',
        (_CLEANUP_NODE,),
    ),
    Revert(
        "counted replies: nd_clean_utils names the deleted utilities as changed, not the hosts",
        ADDON_ND,
        '            "changed_objects": sorted({record["object"] for record in removed_modifiers}),\n',
        '            "changed_objects": removed_objects,\n',
        (_CLEANUP_NODE,),
    ),
    Revert(
        "counted replies: an ND reply's own change list stays in data and the targets are reported",
        SERVER_ND_TOOL,
        "return envelope_for(result, changed_objects=changed_objects or (), changed_resources=changed_resources or ())",
        "return ok(result, changed_objects=changed_objects, changed_resources=changed_resources)",
        (_ND_LIFT_NODE,),
    ),
    Revert(
        "counted replies: a cancelled ND reply keeps the handler's change list in data",
        SERVER_ND_TOOL,
        "unchanged = {key: value for key, value in result.items() if key not in",
        "unchanged = result if True else {key: value for key, value in result.items() if key not in",
        (_ND_CANCEL_NODE,),
    ),
    # --- a Poly Haven model is counted, and its roots are what the caller moves ---
    Revert(
        "counted replies: a Poly Haven model import lists every object it imported",
        ADDON_POLYHAVEN,
        '"imported_objects": counted_page(imported, type_of=lambda obj: obj.type, name_of=lambda obj: obj.name),',
        '"imported_objects": [obj.name for obj in imported],',
        (_MODEL_NODE,),
    ),
    Revert(
        "counted replies: a Poly Haven model import names every part as changed, not its roots",
        ADDON_POLYHAVEN,
        "obj.name for obj in imported if obj.parent is None or obj.parent.session_uid not in imported_ids",
        "obj.name for obj in imported",
        (_MODEL_NODE,),
    ),
    Revert(
        "counted replies: import_polyhaven_asset leaves the handler's roots in data",
        SERVER_POLYHAVEN_TOOL,
        "        return envelope_for(result, changed_objects=changed_objects, changed_resources=changed_resources)",
        "        return ok(result, changed_objects=changed_objects, changed_resources=changed_resources)",
        (_MODEL_TOOL_NODE,),
    ),
    # --- the session's linked libraries are counted beside a sample; list_libraries pages them ---
    Revert(
        "counted replies: get_session_info lists every linked library again",
        ADDON_FILE_LIFECYCLE,
        '            "libraries": _counted_libraries(),\n',
        '            "libraries": [library_summary(library, frame=path_frame()) for library in bpy.data.libraries],\n',
        (_SESSION_NODE,),
    ),
    Revert(
        "counted replies: the session's sample of library summaries is as long as the library list",
        ADDON_FILE_LIFECYCLE,
        "library_summary(library, frame=frame), MAX_LISTED_NAMES)",
        "library_summary(library, frame=frame), len(libraries) or 1)",
        (_SESSION_NODE,),
    ),
    Revert(
        "counted replies: a missing library is counted with the ones that resolve",
        ADDON_FILE_LIFECYCLE,
        '            "MISSING" if getattr(library, "is_missing", False) else "PRESENT" for library in libraries\n',
        '            "PRESENT" for library in libraries\n',
        (_SESSION_NODE,),
    ),
    Revert(
        "counted replies: a swap report lists every linked library again",
        ADDON_FILE_LIFECYCLE,
        '            report["libraries"] = _counted_libraries()\n',
        '            report["libraries"] = [\n'
        "                library_summary(library, frame=path_frame()) for library in bpy.data.libraries\n"
        "            ]\n",
        (_SWAP_NODE,),
    ),
]
