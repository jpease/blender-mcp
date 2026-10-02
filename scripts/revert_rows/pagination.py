"""
Rows guarding the add-on's one pagination primitive and its one integer bound.

`helpers.page_records` and `helpers.bounded_int` replaced six hand-rolled pagers and five
integer checks that had drifted apart: some echoed an over-run offset back, some reported the
limit asked for rather than the page cut, some let a whole float through an integer bound.

Label prefix: `pagination:`.
"""

from .common import ADDON_HELPERS, ADDON_SCENE_INSPECTION, CAMT, LIGHTT, LKT, RJOBT, SOIT, Revert

ROWS: list[Revert] = [
    # --- one page shape for every paged reply ---------------------------------------------------
    Revert(
        # `get_object_info` paged every `type_data` list from `paginate(0, offset, ...)`'s start,
        # which a total of 0 clamps to 0: each page advertised a `next_offset` that led back to
        # the first page.
        "pagination: get_object_info ignores its offset, so every type_data page restarts at the first record",
        ADDON_SCENE_INSPECTION,
        "        type_data = self._object_type_data(obj, sections, limit, offset)\n",
        "        type_data = self._object_type_data(obj, sections, limit, 0)\n",
        (f"{SOIT}::test_get_object_info_resumes_its_type_data_pages_from_the_offset_it_was_given",),
    ),
    Revert(
        "pagination: a page reports the limit it was asked for, not the page size it was cut to",
        ADDON_HELPERS,
        '        "limit": _page_size(limit, max_limit),\n',
        '        "limit": limit,\n',
        (f"{SOIT}::test_get_object_info_reports_the_page_size_it_applied_not_the_one_asked_for",),
    ),
    Revert(
        # `paginate` clamps the start to the total, so an over-run reads as the end of the list;
        # the render-job and library listings used to echo the request back instead.
        "pagination: an offset past the last record is echoed back as where the page starts",
        ADDON_HELPERS,
        '        "offset": start,\n',
        '        "offset": max(0, int(offset)),\n',
        (
            f"{RJOBT}::test_a_list_offset_past_the_last_job_reports_the_empty_page_where_the_jobs_end",
            f"{LKT}::test_list_libraries_offset_past_the_last_library_reports_the_empty_page_where_they_end",
        ),
    ),
    # --- one integer bound ------------------------------------------------------------------------
    Revert(
        # The camera and lighting copies compared `int(value) != value`, which a whole float
        # passes; every other copy required an `int`.
        "pagination: a whole float passes an integer bound",
        ADDON_HELPERS,
        "        or not isinstance(value, int)\n",
        "        or int(value) != value\n",
        (
            f"{CAMT}::test_setting_the_scene_camera_refuses_a_whole_float_marker_frame_before_binding_anything",
            f"{LIGHTT}::test_a_light_listing_refuses_a_whole_float_page_bound[limit]",
            f"{LIGHTT}::test_a_light_listing_refuses_a_whole_float_page_bound[offset]",
        ),
    ),
    # --- the scene overview: counts, a hierarchy walk, and children paged one level at a time ---
    Revert(
        "pagination: a scene record's child_count is not counted",
        ADDON_SCENE_INSPECTION,
        '                    "child_count": child_counts.get(obj, 0),\n',
        '                    "child_count": 0,\n',
        (f"{SOIT}::test_list_scene_objects_records_count_each_objects_direct_children",),
    ),
    Revert(
        "pagination: parent_name pages the whole scene instead of that object's children",
        ADDON_SCENE_INSPECTION,
        "            candidates = [obj for obj in every_object if obj.parent is not None and obj.parent == parent]\n",
        "            candidates = every_object\n",
        (f"{SOIT}::test_list_scene_objects_pages_one_parents_direct_children",),
    ),
    Revert(
        "pagination: an empty parent_name is looked up as a name instead of refused",
        ADDON_SCENE_INSPECTION,
        "            if not isinstance(parent_name, str) or not parent_name:\n",
        "            if not isinstance(parent_name, str):\n",
        (f"{SOIT}::test_list_scene_objects_refuses_an_empty_or_unknown_parent_name[-parent_name]",),
    ),
    Revert(
        "pagination: an unknown parent_name pages nothing instead of being refused",
        ADDON_SCENE_INSPECTION,
        "            if parent is None:\n                raise",
        "            if False:\n                raise",
        (f"{SOIT}::test_list_scene_objects_refuses_an_empty_or_unknown_parent_name[Nobody-Nobody]",),
    ),
    Revert(
        "pagination: the summary's depth walk restarts at zero where it meets an already-measured parent",
        ADDON_SCENE_INSPECTION,
        "            depth = depths[node] if node is not None else 0\n",
        "            depth = 0\n",
        (f"{SOIT}::test_list_scene_objects_summary_counts_the_scene_without_records",),
    ),
    Revert(
        "pagination: the summary lists every top-level collection",
        ADDON_SCENE_INSPECTION,
        "        listed = collection_counts[: self._SUMMARY_SAMPLE]\n",
        "        listed = collection_counts\n",
        (f"{SOIT}::test_list_scene_objects_summary_stays_bounded_however_many_roots_and_collections",),
    ),
    Revert(
        "pagination: a summary silently ignores a search or parent_name beside it",
        ADDON_SCENE_INSPECTION,
        "        if search is not None or parent_name is not None:\n",
        "        if False:\n",
        (f"{SOIT}::test_list_scene_objects_summary_takes_no_filter",),
    ),
]
