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
]
