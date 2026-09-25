"""
Rows guarding which render properties the schemas reach, and the bone filter.

Label prefixes: `render coverage:`, `posing:`.
"""

from .common import ADDON_CR_PRIMITIVES, CRFT, LISTT, RCT, RENDER_COVERAGE_SCRIPT, Revert

ROWS: list[Revert] = [
    # --- artefact truth: which render properties the schemas reach ---
    Revert(
        "render coverage: reachability is judged by identifier, ignoring which owner carries it",
        RENDER_COVERAGE_SCRIPT,
        '    reached = sorted(f"{owner}:{name}" for owner, name in probed & set(reachable))',
        "    reached = sorted(\n"
        '        f"{owner}:{name}" for owner, name in probed if name in {name for _owner, name in reachable}\n'
        "    )",
        (f"{RCT}::test_coverage_classifies_the_same_identifier_per_owner",),
    ),
    Revert(
        "render coverage: an excluded property is reported as an unreachable gap",
        RENDER_COVERAGE_SCRIPT,
        '    unreachable = sorted(f"{owner}:{name}" for owner, name in probed - set(reachable) - set(excluded))',
        '    unreachable = sorted(f"{owner}:{name}" for owner, name in probed - set(reachable))',
        (f"{RCT}::test_coverage_never_reports_an_excluded_property_as_unreachable",),
    ),
    Revert(
        "render coverage: an absent owner's placeholder is read as a property name",
        RENDER_COVERAGE_SCRIPT,
        '        if identifier == "(absent)":\n            continue',
        "        if False:\n            continue",
        (f"{RCT}::test_coverage_ignores_an_absent_owner",),
    ),
    Revert(
        "render coverage: the routing table's flat render properties are never counted as reachable",
        RENDER_COVERAGE_SCRIPT,
        '    reachable = {("scene.render", name) for name in table.RENDER_PROPERTIES}',
        "    reachable = set()",
        (f"{RCT}::test_the_recorded_baseline_and_the_shipped_routes_agree_on_the_reachable_set",),
    ),
    Revert(
        "posing: the bone filter is ignored, so naming three bones still reads the whole rig",
        ADDON_CR_PRIMITIVES,
        "    if bone_names is None:\n        return bones",
        "    if True:\n        return bones",
        (
            f"{LISTT}::test_named_bones_are_returned_in_one_page_instead_of_paged_to",
            f"{LISTT}::test_an_unknown_bone_name_is_refused_rather_than_silently_dropped",
            f"{CRFT}::test_selected_bones_returns_named_bones_in_armature_order_not_request_order",
        ),
    ),
    Revert(
        "posing: a bone the rig does not have is dropped from the page instead of refused",
        ADDON_CR_PRIMITIVES,
        '    if missing:\n        raise ValueError(f"Bones not found in armature',
        '    if False:\n        raise ValueError(f"Bones not found in armature',
        (
            f"{LISTT}::test_an_unknown_bone_name_is_refused_rather_than_silently_dropped",
            f"{CRFT}::test_selected_bones_refuses_an_unknown_name",
        ),
    ),
    Revert(
        "posing: the bone filter accepts a shape that is not a list of names",
        ADDON_CR_PRIMITIVES,
        "    if not isinstance(bone_names, list) or not 1 <= len(bone_names) <= _MAX_BONE_PAGE:",
        "    if False:",
        (
            f"{LISTT}::test_a_malformed_bone_name_filter_is_refused[value0]",
            f"{LISTT}::test_a_malformed_bone_name_filter_is_refused[CHAR1_head_jnt]",
        ),
    ),
    Revert(
        "posing: a blank or non-string bone name passes the filter",
        ADDON_CR_PRIMITIVES,
        "        if not isinstance(name, str) or not name.strip():",
        "        if False:",
        (
            f"{LISTT}::test_a_malformed_bone_name_filter_is_refused[value1]",
            f"{LISTT}::test_a_malformed_bone_name_filter_is_refused[value2]",
        ),
    ),
    Revert(
        "posing: the list is narrowed even when no filter was asked for",
        ADDON_CR_PRIMITIVES,
        "    if bone_names is None:\n        return bones",
        "    if bone_names is None:\n        return bones[:1]",
        (
            f"{LISTT}::test_no_filter_still_lists_every_bone",
            f"{CRFT}::test_selected_bones_returns_every_bone_in_armature_order_when_unfiltered",
        ),
    ),
]
