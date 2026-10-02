"""
Rows guarding `pick_from_camera`'s frame-point maths and its server-side contract.

The maths is `pick_rays`, bpy-free: a frame point (u, v) over `view_frame`'s corners becomes a
world ray, a miss meets the z = 0 ground plane, and a region is sampled and ranked by area. The
server half refuses an empty or out-of-frame region and advertises the tool read-only. What the
add-on handler does with real Blender (render visibility, the playhead, instances) is proven by
`tests/blender_pick_smoke.py`, which no pytest node runs.

Label prefixes: `pick:`.
"""

from .common import ADDON_PICK_RAYS, PICKT, PICKTOOLT, SERVER_DOCUMENTATION, SERVER_VIEWPORT_TOOL, Revert

_CENTRE = f"{PICKT}::test_the_frame_centre_ray_leaves_the_eye_along_the_view_axis"
_CORNERS = tuple(
    f"{PICKT}::test_the_frame_corners_map_to_the_view_frame_corners_whatever_their_order[{order}]"
    for order in ("order0", "order1")
)
_SHIFT = f"{PICKT}::test_a_shifted_frame_moves_the_centre_ray_off_the_view_axis"
_ORTHO = f"{PICKT}::test_an_ortho_camera_casts_parallel_rays_from_across_the_frame"
_SCALE = tuple(f"{PICKT}::test_the_camera_objects_scale_does_not_change_the_ray[{flag}]" for flag in ("True", "False"))
_CLIP = f"{PICKT}::test_clip_start_and_clip_end_bound_the_ray_by_depth_not_by_length"
_ORTHO_CLIP = f"{PICKT}::test_an_ortho_ray_is_clipped_along_its_own_length"
_FALLING = f"{PICKT}::test_a_ray_falling_toward_the_ground_meets_it_at_z_zero"
_LEVEL, _RISING = (
    f"{PICKT}::test_a_ray_level_with_or_rising_from_the_ground_never_meets_it[{case}]"
    for case in ("direction0", "direction1")
)
_SAMPLES = f"{PICKT}::test_region_samples_are_the_cell_centres_of_a_bounded_grid"
_RANKING = f"{PICKT}::test_region_ranking_orders_objects_by_coverage_and_drops_the_slivers"
_TOP_SIX = f"{PICKT}::test_region_ranking_keeps_at_most_six_objects"
_CENTROID = f"{PICKT}::test_the_centroid_hit_is_the_objects_own_sample_nearest_its_mean_position"
_AREA = tuple(
    f"{PICKTOOLT}::test_a_region_must_span_a_positive_area_of_the_frame[{case}]"
    for case in ("zero-width", "inverted-height")
)
_INSIDE = f"{PICKTOOLT}::test_a_region_stays_inside_the_frame"
_READ_ONLY = f"{PICKTOOLT}::test_pick_from_camera_is_advertised_read_only"

ROWS: list[Revert] = [
    Revert(
        "pick: a perspective frame is read without dividing its corners by their depth",
        ADDON_PICK_RAYS,
        "        xs = [corner[0] / -corner[2] for corner in corners]\n"
        "        ys = [corner[1] / -corner[2] for corner in corners]\n",
        "        xs = [corner[0] for corner in corners]\n        ys = [corner[1] for corner in corners]\n",
        (*_CORNERS, _SHIFT, _CLIP),
    ),
    Revert(
        "pick: an ortho camera is cast as a perspective one",
        ADDON_PICK_RAYS,
        "    if not bounds.perspective:\n",
        "    if False:\n",
        (_ORTHO, _ORTHO_CLIP),
    ),
    Revert(
        "pick: the camera object's scale reaches the ray",
        ADDON_PICK_RAYS,
        "        axes.append((axis[0] / length, axis[1] / length, axis[2] / length))\n",
        "        axes.append(axis)\n",
        _SCALE,
    ),
    Revert(
        "pick: the view axis points out of the back of the camera",
        ADDON_PICK_RAYS,
        "(-1.0 / stretch, z_axis)",
        "(1.0 / stretch, z_axis)",
        (_CENTRE, *_CORNERS, _SHIFT),
    ),
    Revert(
        "pick: lens shift is dropped, the frame re-centred on the view axis",
        ADDON_PICK_RAYS,
        "    x = bounds.x_min + u * (bounds.x_max - bounds.x_min)\n",
        "    x = (bounds.x_min - bounds.x_max) / 2 + u * (bounds.x_max - bounds.x_min)\n",
        (_SHIFT,),
    ),
    Revert(
        "pick: v runs down the frame instead of up it",
        ADDON_PICK_RAYS,
        "    y = bounds.y_min + v * (bounds.y_max - bounds.y_min)\n",
        "    y = bounds.y_min + (1.0 - v) * (bounds.y_max - bounds.y_min)\n",
        (*_CORNERS, _ORTHO),
    ),
    Revert(
        "pick: clip depths are taken as lengths along an off-axis ray",
        ADDON_PICK_RAYS,
        "    return CameraRay(location, direction, clip_start * stretch, clip_end * stretch)\n",
        "    return CameraRay(location, direction, clip_start, clip_end)\n",
        (_CLIP,),
    ),
    Revert(
        "pick: the ground plane is met behind the ray's origin",
        ADDON_PICK_RAYS,
        "    distance = -origin[2] / direction[2]\n",
        "    distance = origin[2] / direction[2]\n",
        (_FALLING, _RISING),
    ),
    Revert(
        "pick: a ray rising from the ground plane still lands on it",
        ADDON_PICK_RAYS,
        "    if distance <= 0.0:\n        return None\n",
        "",
        (_RISING,),
    ),
    Revert(
        "pick: a ray level with the ground plane divides by zero",
        ADDON_PICK_RAYS,
        "    if abs(direction[2]) <= LEVEL_EPSILON:\n        return None\n",
        "",
        (_LEVEL,),
    ),
    Revert(
        "pick: a region is sampled at cell corners instead of cell centres",
        ADDON_PICK_RAYS,
        "(u_min + (column + 0.5) * step_u, v_min + (row + 0.5) * step_v)",
        "(u_min + column * step_u, v_min + row * step_v)",
        (_SAMPLES,),
    ),
    Revert(
        "pick: a region's objects are ranked smallest first",
        ADDON_PICK_RAYS,
        "key=lambda item: (-item[1], item[0])",
        "key=lambda item: (item[1], item[0])",
        (_RANKING,),
    ),
    Revert(
        "pick: a sliver under 4% of the region is ranked",
        ADDON_PICK_RAYS,
        "    kept = [(name, count) for name, count in ordered if count / total >= REGION_MIN_COVERAGE]"
        "[:REGION_TOP_OBJECTS]\n",
        "    kept = [(name, count) for name, count in ordered][:REGION_TOP_OBJECTS]\n",
        (_RANKING,),
    ),
    Revert(
        "pick: a region ranks every object it hit",
        ADDON_PICK_RAYS,
        "if count / total >= REGION_MIN_COVERAGE][:REGION_TOP_OBJECTS]\n",
        "if count / total >= REGION_MIN_COVERAGE]\n",
        (_TOP_SIX,),
    ),
    Revert(
        "pick: an object's centroid hit may be a sample it does not cover",
        ADDON_PICK_RAYS,
        "        nearest = min(indices, key=",
        "        nearest = min(range(len(samples)), key=",
        (_CENTROID,),
    ),
    Revert(
        "pick: a region reports no background",
        ADDON_PICK_RAYS,
        "    background = sum(1 for owner in owners if owner is None) / total\n",
        "    background = 0.0\n",
        (_RANKING,),
    ),
    Revert(
        "pick: a region does not count the objects it dropped",
        ADDON_PICK_RAYS,
        "RegionRanking(ranked, len(counts) - len(ranked), background)",
        "RegionRanking(ranked, 0, background)",
        (_RANKING, _TOP_SIX),
    ),
    Revert(
        "pick: an empty or inverted region reaches the add-on",
        SERVER_VIEWPORT_TOOL,
        "        if not (self.u_min < self.u_max and self.v_min < self.v_max):\n",
        "        if False:\n",
        _AREA,
    ),
    Revert(
        "pick: a region may reach outside the frame",
        SERVER_VIEWPORT_TOOL,
        "UnitInterval = Annotated[float, Field(ge=0.0, le=1.0)]\n",
        "UnitInterval = float\n",
        (_INSIDE,),
    ),
    Revert(
        "pick: pick_from_camera is advertised as mutating",
        SERVER_DOCUMENTATION,
        '_READ_ONLY_TOOLS = {"pick_from_camera"}\n',
        "_READ_ONLY_TOOLS: set[str] = set()\n",
        (_READ_ONLY,),
    ),
]
