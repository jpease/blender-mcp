# pyright: reportGeneralTypeIssues=false, reportOptionalSubscript=false
"""
`pick_from_camera`: where frame points of a camera land in the scene, without changing it.

Each frame point becomes a ray (`pick_rays`, which has no `bpy`), cast with `scene.ray_cast` on
the evaluated depsgraph. Measured on Blender 5.2.2, that depsgraph is the viewport's: it holds no
object hidden in the viewport (`hide_viewport`, the eye toggle, a viewport-disabled or excluded
collection), so none of those can be hit, and it does hold `hide_render` objects, which can. So
under RENDER visibility a ray passes on through every surface the render would not show, and the
reply counts the render-visible objects the viewport hides, the ones no ray could reach.
"""

import math

import bpy

from .. import pick_rays
from ..helpers import MAX_FRAME, MIN_FRAME, render_exclusion_reason
from .camera._shared import _camera, _matrix_close, _required_name
from .character_rigging.posing import _place_playhead, restored_playhead

_VISIBILITIES = ("RENDER", "VIEWPORT")
_REGION_KEYS = ("u_min", "v_min", "u_max", "v_max")
# How many surfaces the render does not show one ray may pass through before it gives up.
# A closed proxy costs two (in and out), so eight is four proxies stacked in front of a subject.
_MAX_RECASTS = 8
# How far past a skipped surface the next cast starts: past the float32 noise in a hit position at
# a few hundred metres, and far thinner than anything a pick is meant to find behind it.
_RECAST_STEP_M = 1e-4
# Objects a ray can land on; the rest have no surface for the viewport-hidden count to miss.
_SURFACE_TYPES = {"MESH", "CURVE", "SURFACE", "META", "FONT"}
# Only the instance's own source object is known from a hit, never its instancer, so for a hit on
# an instance these collection codes describe the source collection, which instancing commonly
# excludes on purpose - not whether the render shows the instance.
_COLLECTION_CODES = {"COLLECTION_HIDE_RENDER", "VIEW_LAYER_EXCLUDED"}
_NAMED_LIMIT = 5
_DECIMALS = 6


def _unit_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{label} must be a number in [0, 1]")
    return float(value)


def _validated_points(points):
    if not isinstance(points, list) or not points:
        raise ValueError("points must be a non-empty list of [u, v] pairs")
    pairs = []
    for index, point in enumerate(points):
        try:
            u, v = point
        except (TypeError, ValueError):
            raise ValueError(f"points[{index}] must be a [u, v] pair") from None
        pairs.append((_unit_number(u, f"points[{index}][0]"), _unit_number(v, f"points[{index}][1]")))
    return pairs


def _validated_region(region):
    if not isinstance(region, dict) or set(region) != set(_REGION_KEYS):
        raise ValueError(f"region must have exactly the keys {', '.join(_REGION_KEYS)}")
    bounds = {key: _unit_number(region[key], f"region.{key}") for key in _REGION_KEYS}
    if not (bounds["u_min"] < bounds["u_max"] and bounds["v_min"] < bounds["v_max"]):
        raise ValueError("region must have u_min < u_max and v_min < v_max")
    return bounds


def _validated_frame(frame):
    if frame is None:
        return None
    if isinstance(frame, bool) or not isinstance(frame, (int, float)) or not math.isfinite(frame):
        raise ValueError("frame must be a finite number")
    if not MIN_FRAME <= frame <= MAX_FRAME:
        raise ValueError(f"frame must be in [{MIN_FRAME}, {MAX_FRAME}]")
    return float(frame)


def _rounded(vector):
    return [round(component, _DECIMALS) for component in vector]


def _record(u, v, hit, *, object_name=None, point=None, normal=None, distance=None, face_index=None):
    return {
        "u": round(u, _DECIMALS),
        "v": round(v, _DECIMALS),
        "hit": hit,
        "object_name": object_name,
        "target_point": None if point is None else _rounded(point),
        "normal": None if normal is None else _rounded(normal),
        "distance": None if distance is None else round(distance, _DECIMALS),
        "face_index": face_index,
    }


class _Caster:
    """
    Cast rays through one camera's frame on one evaluated depsgraph, and tally what they skipped.

    Attributes:
        skipped: Surfaces passed because the chosen visibility excludes them, by object name,
            each with the reason the first one was skipped.
        skipped_count: How many surfaces were passed, a closed proxy counting twice.
        unresolved_count: Rays that met more than _MAX_RECASTS skipped surfaces.

    """

    def __init__(self, scene, camera, visibility):
        self.scene = scene
        self.visibility = visibility
        self.depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = camera.evaluated_get(self.depsgraph)
        data = evaluated.data
        self.projection = data.type
        corners = [tuple(corner) for corner in data.view_frame(scene=scene)]
        self.bounds = pick_rays.frame_bounds(corners, perspective=data.type == "PERSP")
        self.matrix = [list(row) for row in evaluated.matrix_world]
        self.clip = (data.clip_start, data.clip_end)
        self.present = {obj.original.session_uid for obj in self.depsgraph.objects}
        self.skipped = {}
        self.skipped_count = 0
        self.unresolved_count = 0
        self._reasons = {}

    def _exclusion(self, obj, instance_matrix):
        """Say why the chosen visibility passes through a hit on `obj`, or None to stop there."""
        if self.visibility != "RENDER":
            return None
        if obj.name not in self._reasons:
            self._reasons[obj.name] = render_exclusion_reason(obj, self.scene)
        reason = self._reasons[obj.name]
        instanced = obj.session_uid not in self.present or not _matrix_close(
            instance_matrix, obj.evaluated_get(self.depsgraph).matrix_world
        )
        if instanced and reason in _COLLECTION_CODES:
            return None
        return reason

    def cast(self, u, v):
        """
        Find what one frame point shows.

        Returns:
            tuple[dict, str | None]: The point's record, and the object it hit (None otherwise).

        """
        ray = pick_rays.camera_ray(self.matrix, self.bounds, u, v, clip_start=self.clip[0], clip_end=self.clip[1])
        origin = ray.origin
        direction = ray.direction
        travelled = ray.near
        for _attempt in range(_MAX_RECASTS + 1):
            start = tuple(origin[axis] + direction[axis] * travelled for axis in range(3))
            hit, location, normal, face_index, obj, matrix = self.scene.ray_cast(
                self.depsgraph, start, direction, distance=ray.far - travelled
            )
            if not hit:
                return self._ground(u, v, origin, direction), None
            distance = travelled + math.dist(location, start)
            reason = self._exclusion(obj, matrix)
            if reason is None:
                record = _record(
                    u,
                    v,
                    "OBJECT",
                    object_name=obj.name,
                    point=location,
                    normal=normal,
                    distance=distance,
                    face_index=face_index,
                )
                return record, obj.name
            self.skipped.setdefault(obj.name, reason)
            self.skipped_count += 1
            travelled = distance + _RECAST_STEP_M
            if travelled >= ray.far:
                return self._ground(u, v, origin, direction), None
        self.unresolved_count += 1
        return _record(u, v, "UNRESOLVED"), None

    @staticmethod
    def _ground(u, v, origin, direction):
        landing = pick_rays.ground_plane_point(origin, direction)
        if landing is None:
            return _record(u, v, "NONE")
        point, distance = landing
        return _record(u, v, "GROUND_PLANE", point=point, normal=(0.0, 0.0, 1.0), distance=distance)

    def hidden_in_viewport(self):
        """Name the render-visible surface objects the viewport depsgraph holds no copy of."""
        if self.visibility != "RENDER":
            return []
        return [
            obj.name
            for obj in self.scene.objects
            if obj.type in _SURFACE_TYPES
            and obj.session_uid not in self.present
            and render_exclusion_reason(obj, self.scene) is None
        ]


def _listed(names):
    shown = ", ".join(f"'{name}'" for name in names[:_NAMED_LIMIT])
    more = len(names) - _NAMED_LIMIT
    return f"{shown} and {more} more" if more > 0 else shown


def _region_reply(caster, bounds):
    samples = pick_rays.region_samples(bounds["u_min"], bounds["v_min"], bounds["u_max"], bounds["v_max"])
    cast = [caster.cast(u, v) for u, v in samples]
    ranking = pick_rays.rank_region([owner for _record, owner in cast], samples)
    return {
        "region": bounds,
        "sample_count": len(samples),
        "objects": [
            {
                "object_name": entry.object_name,
                "coverage": round(entry.coverage, 4),
                "centroid": cast[entry.sample_index][0],
            }
            for entry in ranking.objects
        ],
        "dropped_object_count": ranking.dropped_object_count,
        "background_fraction": round(ranking.background_fraction, 4),
    }


def _tallies(caster):
    """State what the rays passed through, and what under RENDER no ray could reach."""
    tallies = {
        "skipped_hit_count": caster.skipped_count,
        "skipped_objects": [
            {"object_name": name, "reason": reason} for name, reason in list(caster.skipped.items())[:_NAMED_LIMIT]
        ],
    }
    warnings = []
    if caster.visibility == "RENDER":
        hidden = caster.hidden_in_viewport()
        tallies["hidden_in_viewport_count"] = len(hidden)
        if hidden:
            warnings.append(
                f"{len(hidden)} render-visible object(s) are hidden in the viewport, so no ray can hit them: "
                f"{_listed(hidden)}; show them in the viewport (hide_viewport, the eye toggle, or their "
                "collection's) to pick them"
            )
    if caster.unresolved_count:
        warnings.append(
            f"{caster.unresolved_count} ray(s) passed {_MAX_RECASTS} surfaces the chosen visibility excludes "
            "and stopped there; those points are UNRESOLVED"
        )
    if warnings:
        tallies["warnings"] = warnings
    return tallies


def _pick(scene, camera, visibility, points, region):
    caster = _Caster(scene, camera, visibility)
    result = {
        "camera_name": camera.name,
        "frame": round(scene.frame_current + scene.frame_subframe, _DECIMALS),
        "visibility": visibility,
        "projection": caster.projection,
    }
    if points is not None:
        result["points"] = [caster.cast(u, v)[0] for u, v in points]
    else:
        result.update(_region_reply(caster, region))
    result.update(_tallies(caster))
    return result


class PickHandlersMixin:
    """`pick_from_camera`, read-only: the playhead it borrows for `frame` is put back."""

    def pick_from_camera(self, camera_name=None, points=None, region=None, frame=None, visibility="RENDER"):
        """
        Find what frame points, or a frame region, of a camera show in the scene.

        Args:
            camera_name: The camera to look through; PERSP or ORTHO.
            points: [u, v] frame points, (0, 0) the frame's bottom-left corner.
            region: {u_min, v_min, u_max, v_max} to sample on a grid and rank by area, instead.
            frame: Evaluate at this frame, subframe included, and put the playhead back.
            visibility: RENDER passes through surfaces the render does not show; VIEWPORT stops
                at whatever the viewport shows.

        Returns:
            dict: The camera, frame, visibility and projection, a record per point or the region
            ranking, and what the rays skipped.

        """
        if visibility not in _VISIBILITIES:
            raise ValueError(f"visibility must be one of {', '.join(_VISIBILITIES)}")
        if (points is None) == (region is None):
            raise ValueError("Supply exactly one of points or region")
        pairs = None if points is None else _validated_points(points)
        bounds = None if region is None else _validated_region(region)
        frame = _validated_frame(frame)
        scene = bpy.context.scene
        camera = _camera(_required_name(camera_name, "camera_name"), scene=scene)
        if camera.data.type == "PANO":
            raise ValueError(
                f"Camera '{camera.name}' is panoramic; pick_from_camera supports PERSP and ORTHO cameras only"
            )
        if frame is None:
            return _pick(scene, camera, visibility, pairs, bounds)
        with restored_playhead(scene):
            _place_playhead(scene, frame)
            return _pick(scene, camera, visibility, pairs, bounds)
