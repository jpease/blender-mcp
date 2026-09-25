"""
Rows guarding `save_shot.create_directories` and one object per name.

The lookup, the candidate list a refusal reuses, and the rule the server instructions
state.

Label prefixes: `create_directories:`, `object lookup:`, `candidates:`,
`server instructions:`.
"""

from .common import (
    ADDON_CANDIDATES,
    ADDON_FILE_LIFECYCLE,
    ADDON_FILE_PATHS,
    ADDON_OBJECT_LOOKUP,
    ADDON_SCENE,
    ADDON_SERVER_CORE,
    CANDT,
    FLT,
    FPT,
    OLT,
    SERVER_APP,
    SERVER_FILE_LIFECYCLE_TOOL,
    SFLT,
    SIT,
    SOIT,
    Revert,
)

ROWS: list[Revert] = [
    # --- save_shot.create_directories ---
    Revert(
        "create_directories: save_shot ignores create_directories and never makes the directory",
        ADDON_FILE_LIFECYCLE,
        "        created_directory = create_save_directory(request.canonical)\n",
        "        created_directory = False\n",
        (f"{FLT}::test_save_shot_refuses_a_missing_directory_unless_asked_to_create_it",),
    ),
    Revert(
        "create_directories: save_shot does not validate create_directories as a bool",
        ADDON_FILE_LIFECYCLE,
        '        create_directories = require_bool("create_directories", create_directories)\n',
        "",
        (
            f"{FLT}::test_save_shot_creates_no_directory_outside_the_roots_or_on_a_refusal",
            f"{FLT}::test_a_flag_that_is_not_a_real_bool_is_refused[save_shot-create_directories]",
        ),
    ),
    Revert(
        "create_directories: resolving a save target creates its directory before any refusal has run",
        ADDON_FILE_PATHS,
        ("    directory = os.path.dirname(path)\n    directory_exists = os.path.isdir(directory)\n"),
        (
            "    directory = os.path.dirname(path)\n"
            "    if create_directories:\n"
            "        os.makedirs(directory, exist_ok=True)\n"
            "    directory_exists = os.path.isdir(directory)\n"
        ),
        (f"{FPT}::test_a_missing_save_directory_is_accepted_and_created_only_on_opt_in",),
    ),
    Revert(
        "create_directories: create_save_directory reports a directory that already existed as created",
        ADDON_FILE_PATHS,
        "    if os.path.isdir(directory):\n        return False\n",
        "    if os.path.isdir(directory):\n        return True\n",
        (
            f"{FPT}::test_a_missing_save_directory_is_accepted_and_created_only_on_opt_in",
            f"{FLT}::test_save_shot_reports_no_created_directory_when_it_already_existed",
        ),
    ),
    Revert(
        "create_directories: a directory-creation OSError's text (and its path) reaches the refusal",
        ADDON_FILE_PATHS,
        '        raise ValueError("target directory could not be created") from exc',
        '        raise ValueError(f"target directory could not be created: {exc}") from exc',
        (f"{FPT}::test_a_save_directory_blocked_by_a_file_is_refused_without_naming_it",),
    ),
    Revert(
        "create_directories: the save_shot tool drops create_directories",
        SERVER_FILE_LIFECYCLE_TOOL,
        '            "create_directories": create_directories,\n',
        "",
        (
            f"{SFLT}::test_save_shot_forwards_every_parameter",
            f"{SFLT}::test_save_shot_default_filepath_is_none",
        ),
    ),
    # --- one object per name after a library override ---
    Revert(
        "object lookup: find_object does not prefer the local object by its (name, None) key",
        ADDON_OBJECT_LOOKUP,
        "    local = objects.get((name, None))",
        "    local = None",
        (
            f"{OLT}::test_the_local_override_wins_over_a_linked_original_listed_first",
            f"{SOIT}::test_object_name_lookups_resolve_to_the_override_even_when_the_linked_original_is_listed_first",
        ),
    ),
    Revert(
        "object lookup: several linked objects with one name are guessed between",
        ADDON_OBJECT_LOOKUP,
        "    if len(matches) > 1:",
        "    if False:",
        (f"{OLT}::test_two_linked_objects_with_one_name_and_no_local_one_are_refused",),
    ),
    Revert(
        "candidates: the ambiguity refusal publishes library names unreduced",
        ADDON_CANDIDATES,
        '    return client_safe_name_leaf(getattr(library, "name", ""))\n',
        '    return str(getattr(library, "name", ""))\n',
        (
            f"{OLT}::test_two_linked_objects_with_one_name_and_no_local_one_are_refused",
            f"{CANDT}::test_describe_library_candidates_reduces_to_a_leaf_without_consulting_id_type",
            f"{CANDT}::test_candidates_that_reduce_to_one_name_stay_distinguishable_by_uid",
        ),
    ),
    # --- the refusal reuses linking's bounded candidate list ---
    Revert(
        "candidates: the candidate list is unbounded again",
        ADDON_CANDIDATES,
        "MAX_CANDIDATES = 10\n",
        "MAX_CANDIDATES = 10_000\n",
        (
            f"{CANDT}::test_the_list_is_bounded_and_reports_how_many_were_left_out",
            f"{OLT}::test_the_ambiguity_refusal_is_bounded_however_many_libraries_link_the_name",
        ),
    ),
    Revert(
        "candidates: entries lose their session_uid",
        ADDON_CANDIDATES,
        'f"{namer(d)!r} (session_uid {session_uid_of(d)})"',
        'f"{namer(d)!r}"',
        (
            f"{CANDT}::test_a_non_library_name_is_published_not_blanked",
            f"{CANDT}::test_describe_candidates_reduces_a_library_name_to_a_leaf_by_its_id_type",
            f"{CANDT}::test_describe_library_candidates_reduces_to_a_leaf_without_consulting_id_type",
            f"{CANDT}::test_candidates_that_reduce_to_one_name_stay_distinguishable_by_uid",
            f"{OLT}::test_two_linked_objects_with_one_name_and_no_local_one_are_refused",
        ),
    ),
    Revert(
        "candidates: the refusal drops its true total",
        ADDON_OBJECT_LOOKUP,
        'f"({len(matches)} of them: {describe_library_candidates(libraries)}) and no local object has it; "',
        'f"({describe_library_candidates(libraries)}) and no local object has it; "',
        (
            f"{OLT}::test_two_linked_objects_with_one_name_and_no_local_one_are_refused",
            f"{OLT}::test_the_ambiguity_refusal_is_bounded_however_many_libraries_link_the_name",
        ),
    ),
    Revert(
        "candidates: the library describer trusts id_type instead of forcing the leaf rule",
        ADDON_CANDIDATES,
        "    return _describe(libraries, _library_leaf)\n",
        "    return _describe(libraries, display_name)\n",
        (
            f"{CANDT}::test_describe_library_candidates_reduces_to_a_leaf_without_consulting_id_type",
            f"{OLT}::test_two_linked_objects_with_one_name_and_no_local_one_are_refused",
        ),
    ),
    Revert(
        "candidates: a name string reaches a namer that expects the datablock, blanking names",
        ADDON_CANDIDATES,
        'f"{namer(d)!r} (session_uid',
        "f\"{namer(getattr(d, 'name', ''))!r} (session_uid",
        (f"{CANDT}::test_a_non_library_name_is_published_not_blanked",),
    ),
    Revert(
        "candidates: display_name loses its library branch",
        ADDON_CANDIDATES,
        '    if getattr(datablock, "id_type", None) == "LIBRARY":\n',
        "    if False:\n",
        (f"{CANDT}::test_describe_candidates_reduces_a_library_name_to_a_leaf_by_its_id_type",),
    ),
    # --- the name-resolution rule is stated once, in the server instructions ---
    Revert(
        "server instructions: the object-name resolution rule is deleted from the instructions",
        SERVER_APP,
        "\n\nObject names after a library override: a name shared with the linked original resolves to the\n"
        "editable override. A name linked from several libraries with no local object is refused with each\n"
        "library's session_uid - override one with create_override.",
        "",
        (
            f"{SIT}::test_the_instructions_state_how_an_overridden_name_resolves",
            f"{SIT}::test_the_instructions_name_the_way_out_of_the_ambiguity_refusal",
        ),
    ),
    Revert(
        "server instructions: the parameter-naming conventions are deleted from the instructions",
        SERVER_APP,
        "\n\nTool parameter naming follows three conventions, so a name is guessable rather than something to\n"
        "fail once and learn: a tool that creates a new object of some kind names that kind parameter\n"
        "`<noun>_type` (light_type, primitive_type, domain_type), never bare `type`. A tool that creates a\n"
        "new object always takes `collection_name` explicitly - it is looked up or created, never defaulted\n"
        "from scene state. A tool that patches fields on an object that already exists (set_object_transform,\n"
        "configure_light, configure_camera, ...) takes one nested argument named `patch` (or a named group\n"
        "such as `optics`/`display`) instead of flat top-level keywords - read that argument's own schema for\n"
        "its field list before calling.",
        "",
        (f"{SIT}::test_the_instructions_state_the_three_parameter_naming_conventions",),
    ),
    Revert(
        # The three rules an agent cannot infer from any single tool's schema: one shot is one
        # action, the playhead is movable, and a held contact is IK rather than repeated FK.
        "server instructions: the animation paragraph is deleted from the instructions",
        SERVER_APP,
        "\n\nAnimation is authored into one named action: pass the same `action_name` to\n"
        "`keyframe_object_transform` and the pose tools, because an ID holds one action and a second one\n"
        "silently stops the first driving the rig. The pose and transform keyers take many frames per call\n"
        '(`keyframe_character_pose(keys=[{"frame": ..., "poses": [...]}, ...])`), so a stride is one call.\n'
        "`set_scene_frame` is how any inspection tool or screenshot is pointed at another frame - they all\n"
        "report the current frame, so without it you are reviewing frame 1 forever. A contact that must hold\n"
        "still while the body moves over it - a planted foot, a hand on a prop - is held by\n"
        "`keyframe_bone_reach`, which re-solves the IK against the evaluated body pose at each frame;\n"
        "repeated FK rotation slides it instead.",
        "",
        (
            f"{SIT}::test_the_instructions_state_that_one_shot_is_one_action",
            f"{SIT}::test_the_instructions_name_the_tool_that_moves_the_playhead",
            f"{SIT}::test_the_instructions_point_a_held_contact_at_the_ik_tool",
        ),
    ),
    Revert(
        "server instructions: the instructions are written but never handed to FastMCP",
        SERVER_APP,
        "instructions=SERVER_INSTRUCTIONS)",
        "instructions=None)",
        (f"{SIT}::test_the_served_instructions_are_the_ones_stated_here",),
    ),
    # --- the lookup itself, and the commands that resolve a name through it ---
    Revert(
        "object lookup: a single linked object is not resolved",
        ADDON_OBJECT_LOOKUP,
        "    return matches[0] if matches else None",
        "    return None",
        (f"{OLT}::test_a_single_linked_object_is_returned_when_no_local_one_has_the_name",),
    ),
    Revert(
        "object lookup: a missing name resolves to some other object",
        ADDON_OBJECT_LOOKUP,
        "    return matches[0] if matches else None",
        "    return matches[0] if matches else next(iter(objects.values()), None)",
        (f"{OLT}::test_a_missing_name_is_none",),
    ),
    Revert(
        "object lookup: scene tools look objects up by Blender's list order",
        ADDON_SCENE,
        '    obj = find_object(bpy.data.objects, _required_name(name, "object_name"))',
        '    obj = bpy.data.objects.get(_required_name(name, "object_name"))',
        (f"{SOIT}::test_object_name_lookups_resolve_to_the_override_even_when_the_linked_original_is_listed_first",),
    ),
    Revert(
        "object lookup: get_object_info looks its object up by Blender's list order",
        ADDON_SERVER_CORE,
        # The 8-space call alone also matches, as a suffix, the deeper copy in
        # `_resolve_targets`, which comes first, and `apply()` replaces only the
        # first match. The trailing `if not obj:` makes the anchor unique.
        "        obj = find_object(bpy.data.objects, name)\n        if not obj:\n",
        "        obj = bpy.data.objects.get(name)\n        if not obj:\n",
        (f"{SOIT}::test_object_name_lookups_resolve_to_the_override_even_when_the_linked_original_is_listed_first",),
    ),
    Revert(
        "object lookup: the transaction snapshots its targets by Blender's list order",
        ADDON_SERVER_CORE,
        "                obj = find_object(bpy.data.objects, name)\n",
        "                obj = bpy.data.objects.get(name)\n",
        (f"{SOIT}::test_the_transaction_snapshots_the_same_object_the_handler_mutates_after_an_override",),
    ),
    Revert(
        "object lookup: an ambiguous target name raises out of the snapshot instead of being skipped",
        ADDON_SERVER_CORE,
        "            try:\n"
        "                obj = find_object(bpy.data.objects, name)\n"
        "            except ValueError:\n"
        "                continue\n",
        "            obj = find_object(bpy.data.objects, name)\n",
        (f"{SOIT}::test_an_ambiguous_target_name_is_skipped_rather_than_raising_out_of_the_snapshot",),
    ),
    Revert(
        "object lookup: get_object_info does not say whether it read an override",
        ADDON_SERVER_CORE,
        '            "is_override": getattr(obj, "override_library", None) is not None,',
        '            "is_override": False,',
        (f"{SOIT}::test_get_object_info_says_whether_it_resolved_an_override_or_a_linked_object",),
    ),
    Revert(
        "object lookup: get_object_info publishes a linked object's library name unreduced",
        ADDON_SERVER_CORE,
        '"library": client_safe_name_leaf(obj.library.name) if',
        '"library": obj.library.name if',
        (f"{SOIT}::test_get_object_info_says_whether_it_resolved_an_override_or_a_linked_object",),
    ),
]
