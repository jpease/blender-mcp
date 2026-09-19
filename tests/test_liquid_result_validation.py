"""Regression coverage for validate_liquid_result and its geometry/measurement helpers."""

import sys
import types

import pytest

from test_mutation_transaction import _load_addon


def _load_liquid_handler(monkeypatch):
    addon, _bpy = _load_addon(monkeypatch, data={})
    return addon, sys.modules[f"{addon.__name__}.handlers.liquid"]


def _edge(a, b):
    return types.SimpleNamespace(vertices=(a, b))


def _fake_scene_for(object_name, *, frame_start=1, frame_end=250):
    view_layer = types.SimpleNamespace(objects={object_name: object()}, update=lambda: None)
    scene = types.SimpleNamespace(
        name="Scene",
        frame_start=frame_start,
        frame_end=frame_end,
        frame_current=1,
        frame_subframe=0.0,
        objects={object_name: object()},
        view_layers=[view_layer],
    )

    def frame_set(frame, subframe=0.0):
        scene.frame_current = frame
        scene.frame_subframe = subframe

    scene.frame_set = frame_set
    return scene


# ---------------------------------------------------------------------------
# Pure geometry helpers
# ---------------------------------------------------------------------------


def test_volume_of_bounds_computes_product_of_extents(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    bounds = {"minimum": [0.0, 1.0, -1.0], "maximum": [2.0, 4.0, 1.0]}

    assert handler.result_validation._volume_of_bounds(bounds) == pytest.approx(12.0)


def test_volume_of_bounds_clamps_inverted_extents_to_zero(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    bounds = {"minimum": [5.0, 0.0, 0.0], "maximum": [1.0, 1.0, 1.0]}

    assert handler.result_validation._volume_of_bounds(bounds) == pytest.approx(0.0)


def test_union_bounds_takes_componentwise_min_and_max(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    bounds_list = [
        {"minimum": [0.0, 0.0, 0.0], "maximum": [1.0, 1.0, 1.0]},
        {"minimum": [-1.0, 2.0, 0.5], "maximum": [0.5, 3.0, 2.0]},
    ]

    assert handler.result_validation._union_bounds(bounds_list) == {
        "minimum": [-1.0, 0.0, 0.0],
        "maximum": [1.0, 3.0, 2.0],
    }


def test_point_in_bounds_is_inclusive_of_boundaries(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    bounds = {"minimum": [0.0, 0.0, 0.0], "maximum": [1.0, 1.0, 1.0]}

    assert handler.result_validation._point_in_bounds(bounds, (0.0, 1.0, 0.5)) is True
    assert handler.result_validation._point_in_bounds(bounds, (1.0001, 0.5, 0.5)) is False


def test_grid_points_returns_resolution_cubed_cell_centers(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    bounds = {"minimum": [0.0, 0.0, 0.0], "maximum": [2.0, 2.0, 2.0]}

    points = list(handler.result_validation._grid_points(bounds, resolution=2))

    assert len(points) == 8
    assert (0.5, 0.5, 0.5) in points
    assert (1.5, 1.5, 1.5) in points


class _Vec3:
    def __init__(self, x, y, z) -> None:
        self.x, self.y, self.z = x, y, z

    def dot(self, other):
        return self.x * other.x + self.y * other.y + self.z * other.z

    def cross(self, other):
        return _Vec3(
            self.y * other.z - self.z * other.y,
            self.z * other.x - self.x * other.z,
            self.x * other.y - self.y * other.x,
        )


class _IdentityMatrix:
    def __matmul__(self, vec):
        return vec


def test_mesh_volume_of_unit_right_tetrahedron_via_divergence_theorem(monkeypatch) -> None:
    # P0=(0,0,0), P1=(1,0,0), P2=(0,1,0), P3=(0,0,1): three of the four faces include the
    # origin as v0, contributing 0 to the divergence sum; only face (1,2,3) is non-trivial,
    # hand-verified to sum to 1.0, giving an expected enclosed volume of |1.0| / 6.0.
    _addon, handler = _load_liquid_handler(monkeypatch)
    vertices = [
        types.SimpleNamespace(co=_Vec3(0.0, 0.0, 0.0)),
        types.SimpleNamespace(co=_Vec3(1.0, 0.0, 0.0)),
        types.SimpleNamespace(co=_Vec3(0.0, 1.0, 0.0)),
        types.SimpleNamespace(co=_Vec3(0.0, 0.0, 1.0)),
    ]
    triangles = [
        types.SimpleNamespace(vertices=(0, 1, 2)),
        types.SimpleNamespace(vertices=(0, 1, 3)),
        types.SimpleNamespace(vertices=(0, 2, 3)),
        types.SimpleNamespace(vertices=(1, 2, 3)),
    ]
    mesh = types.SimpleNamespace(vertices=vertices, loop_triangles=triangles, calc_loop_triangles=lambda: None)

    volume = handler.result_validation._mesh_volume(_IdentityMatrix(), mesh)

    assert volume == pytest.approx(1.0 / 6.0)


def test_connected_components_counts_disjoint_pieces_and_isolated_vertices(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    # Vertices 0-2 form one connected piece via two edges; 3 and 4 are isolated vertices.
    mesh = types.SimpleNamespace(
        vertices=[object(), object(), object(), object(), object()],
        edges=[_edge(0, 1), _edge(1, 2)],
    )

    assert handler.result_validation._connected_components(mesh) == 3


# ---------------------------------------------------------------------------
# _point_inside_mesh: odd/even ray-crossing parity, zero hits, 64-cast cap
# ---------------------------------------------------------------------------


class _FakeRayVector:
    def __init__(self, value) -> None:
        self.value = value

    def copy(self):
        return _FakeRayVector(self.value)

    def __add__(self, other):
        return _FakeRayVector(self.value + other.value)

    def __mul__(self, scalar):
        return _FakeRayVector(self.value * scalar)


class _FixedHitBVH:
    """Reports a hit for the first ``hit_count`` ray casts, then reports a miss."""

    def __init__(self, hit_count) -> None:
        self.hit_count = hit_count
        self.calls = 0

    def ray_cast(self, origin, _direction):
        self.calls += 1
        if self.calls > self.hit_count:
            return (None, None, None, None)
        return (_FakeRayVector(origin.value + 1.0), None, None, 1.0)


def test_point_inside_mesh_returns_true_for_odd_crossing_count(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    bvh = _FixedHitBVH(hit_count=3)

    result = handler.result_validation._point_inside_mesh(bvh, _FakeRayVector(0.0), _FakeRayVector(1.0), 1e-6)

    assert result is True


def test_point_inside_mesh_returns_false_for_even_crossing_count(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    bvh = _FixedHitBVH(hit_count=2)

    result = handler.result_validation._point_inside_mesh(bvh, _FakeRayVector(0.0), _FakeRayVector(1.0), 1e-6)

    assert result is False


def test_point_inside_mesh_returns_false_for_zero_crossings(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    bvh = _FixedHitBVH(hit_count=0)

    result = handler.result_validation._point_inside_mesh(bvh, _FakeRayVector(0.0), _FakeRayVector(1.0), 1e-6)

    assert result is False


def test_point_inside_mesh_caps_ray_casts_at_64_even_with_unbounded_hits(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    bvh = _FixedHitBVH(hit_count=10_000)

    result = handler.result_validation._point_inside_mesh(bvh, _FakeRayVector(0.0), _FakeRayVector(1.0), 1e-6)

    assert bvh.calls == 64
    assert result is False


# ---------------------------------------------------------------------------
# _measure_container: FILL/SPILL/WALL_PENETRATION/ESCAPED classification
# ---------------------------------------------------------------------------


class _FakeVec:
    def __init__(self, seq) -> None:
        self.x, self.y, self.z = seq

    def normalized(self):
        return self


class _IdentityLinear:
    def __matmul__(self, other):
        return other


class _IdentityWorldInverse:
    def to_3x3(self):
        return _IdentityLinear()

    def __matmul__(self, other):
        return other


class _WorldMatrixStub:
    def inverted_safe(self):
        return _IdentityWorldInverse()


def test_measure_container_classifies_samples_by_bounds_precedence(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    monkeypatch.setattr(handler.result_validation.mathutils, "Vector", _FakeVec, raising=False)
    monkeypatch.setattr(handler.result_validation, "_point_inside_mesh", lambda *_a, **_k: True)

    # Union bounds = [-0.5, 2.5] x [-0.5, 1.5] x [-0.5, 1.5] (volume 12); resolution=2 puts every
    # sample's x at 0.25 (inside interior_bounds) or 1.75 (inside spill_bounds only), so all 8
    # samples split evenly 4 FILL / 4 SPILL with no WALL_PENETRATION or ESCAPED.
    spec = {
        "container_name": "Glass",
        "interior_bounds": {"minimum": [0.0, 0.0, 0.0], "maximum": [1.0, 1.0, 1.0]},
        "outer_bounds": {"minimum": [-0.5, -0.5, -0.5], "maximum": [1.5, 1.5, 1.5]},
        "spill_bounds": {"minimum": [1.5, -0.5, -0.5], "maximum": [2.5, 1.5, 1.5]},
    }

    result = handler.result_validation._measure_container(
        bvh=None, world_matrix=_WorldMatrixStub(), spec=spec, resolution=2, epsilon=1e-6
    )

    assert result["container"] == "Glass"
    assert result["sample_resolution"] == 2
    assert result["sample_counts"] == {"FILL": 4, "SPILL": 4, "WALL_PENETRATION": 0, "ESCAPED": 0}
    assert result["fill_volume"] == pytest.approx(6.0)
    assert result["spill_volume"] == pytest.approx(6.0)
    assert result["wall_penetration_volume"] == pytest.approx(0.0)
    assert result["escaped_volume_near_container"] == pytest.approx(0.0)
    assert result["interior_volume"] == pytest.approx(1.0)
    assert result["fill_fraction"] == pytest.approx(6.0)


def test_measure_container_reports_none_fill_fraction_for_degenerate_interior(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    monkeypatch.setattr(handler.result_validation.mathutils, "Vector", _FakeVec, raising=False)
    monkeypatch.setattr(handler.result_validation, "_point_inside_mesh", lambda *_a, **_k: False)
    spec = {
        "container_name": "Glass",
        "interior_bounds": {"minimum": [0.0, 0.0, 0.0], "maximum": [0.0, 1.0, 1.0]},
        "outer_bounds": {"minimum": [0.0, 0.0, 0.0], "maximum": [1.0, 1.0, 1.0]},
    }

    result = handler.result_validation._measure_container(
        bvh=None, world_matrix=_WorldMatrixStub(), spec=spec, resolution=2, epsilon=1e-6
    )

    assert result["fill_fraction"] is None
    assert "spill_volume" in result and result["spill_volume"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _resolve_container_specs: explicit names and shot-id auto-discovery
# ---------------------------------------------------------------------------


class _FakeVolumeObject:
    def __init__(self, name) -> None:
        self.name = name
        self._props: dict = {}

    def __setitem__(self, key, value) -> None:
        self._props[key] = value

    def get(self, key, default=None):
        return self._props.get(key, default)


def _tagged_volume(handler, name, role, container_name):
    obj = _FakeVolumeObject(name)
    obj[handler.inspection_and_setup._LIQUID_ROLE_PROPERTY] = role
    if container_name is not None:
        obj[handler.shot.VOLUME_CONTAINER_PROPERTY] = container_name
    return obj


def test_resolve_container_specs_explicit_names_group_by_container(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    interior = _tagged_volume(handler, "Glass Interior Volume", "CONTAINER_VOLUME", "Glass")
    spill = _tagged_volume(handler, "Glass Spill Volume", "SPILL_VOLUME", "Glass")
    glass = _FakeVolumeObject("Glass")
    registry = {"Glass Interior Volume": interior, "Glass Spill Volume": spill, "Glass": glass}
    monkeypatch.setattr(handler.result_validation, "_get_object", lambda name, _types=None: registry[name])
    monkeypatch.setattr(
        handler.result_validation,
        "_world_bounds",
        lambda obj, evaluated=False: {"tag": obj.name, "evaluated": evaluated},
    )
    domain_obj = _FakeVolumeObject("Domain")

    specs = handler.result_validation._resolve_container_specs(
        domain_obj, ["Glass Interior Volume", "Glass Spill Volume"]
    )

    assert len(specs) == 1
    spec = specs[0]
    assert spec["container_name"] == "Glass"
    assert spec["interior_bounds"] == {"tag": "Glass Interior Volume", "evaluated": False}
    assert spec["spill_bounds"] == {"tag": "Glass Spill Volume", "evaluated": False}
    assert spec["outer_bounds"] == {"tag": "Glass", "evaluated": True}


def test_resolve_container_specs_rejects_explicit_volume_without_container_property(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    untagged = _FakeVolumeObject("Untagged")
    monkeypatch.setattr(handler.result_validation, "_get_object", lambda name, _types=None: untagged)
    domain_obj = _FakeVolumeObject("Domain")

    with pytest.raises(ValueError, match=handler.shot.VOLUME_CONTAINER_PROPERTY):
        handler.result_validation._resolve_container_specs(domain_obj, ["Untagged"])


def test_resolve_container_specs_rejects_volume_with_unrecognized_role(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    weird = _tagged_volume(handler, "Weird", "EFFECTOR", "Glass")
    monkeypatch.setattr(handler.result_validation, "_get_object", lambda name, _types=None: weird)
    domain_obj = _FakeVolumeObject("Domain")

    with pytest.raises(ValueError, match="not tagged as a CONTAINER_VOLUME or SPILL_VOLUME"):
        handler.result_validation._resolve_container_specs(domain_obj, ["Weird"])


def test_resolve_container_specs_rejects_spill_volume_without_matching_container_volume(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    spill = _tagged_volume(handler, "Glass Spill Volume", "SPILL_VOLUME", "Glass")
    monkeypatch.setattr(handler.result_validation, "_get_object", lambda name, _types=None: spill)
    monkeypatch.setattr(handler.result_validation, "_world_bounds", lambda obj, evaluated=False: {})
    domain_obj = _FakeVolumeObject("Domain")

    with pytest.raises(ValueError, match="spill volume but no CONTAINER_VOLUME"):
        handler.result_validation._resolve_container_specs(domain_obj, ["Glass Spill Volume"])


def test_resolve_container_specs_auto_discovery_returns_empty_without_shot_id(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    domain_obj = _FakeVolumeObject("Domain")

    assert handler.result_validation._resolve_container_specs(domain_obj, None) == []


def test_resolve_container_specs_auto_discovers_by_shared_shot_id(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    domain_obj = _FakeVolumeObject("Domain")
    domain_obj[handler.shot.SHOT_ID_PROPERTY] = "shot-1"

    matching = _tagged_volume(handler, "Glass Interior Volume", "CONTAINER_VOLUME", "Glass")
    matching[handler.shot.SHOT_ID_PROPERTY] = "shot-1"
    other_shot = _tagged_volume(handler, "Bowl Interior Volume", "CONTAINER_VOLUME", "Bowl")
    other_shot[handler.shot.SHOT_ID_PROPERTY] = "shot-2"
    non_volume = _tagged_volume(handler, "Effector", "EFFECTOR", None)
    non_volume[handler.shot.SHOT_ID_PROPERTY] = "shot-1"

    bpy = sys.modules["bpy"]
    monkeypatch.setattr(bpy.data, "objects", [matching, other_shot, non_volume], raising=False)
    glass = _FakeVolumeObject("Glass")
    monkeypatch.setattr(handler.result_validation, "_get_object", lambda name, _types=None: glass)
    monkeypatch.setattr(
        handler.result_validation,
        "_world_bounds",
        lambda obj, evaluated=False: {"tag": obj.name, "evaluated": evaluated},
    )

    specs = handler.result_validation._resolve_container_specs(domain_obj, None)

    assert [spec["container_name"] for spec in specs] == ["Glass"]
    assert specs[0]["interior_bounds"] == {"tag": "Glass Interior Volume", "evaluated": False}


# ---------------------------------------------------------------------------
# _evaluate_targets: findings for topology, overflow policy, and fill deadlines
# ---------------------------------------------------------------------------


def _frame_report(frame, *, non_manifold=0, components=1, containers=()):
    return {
        "frame": frame,
        "liquid_mesh": {"non_manifold_edges": non_manifold, "connected_components": components},
        "containers": list(containers),
    }


def test_evaluate_targets_flags_non_manifold_mesh_as_info(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)

    findings = handler.result_validation._evaluate_targets([_frame_report(5, non_manifold=3)], None, None, "ALLOW")

    assert len(findings) == 1
    assert findings[0]["severity"] == "INFO"
    assert findings[0]["code"] == "NON_MANIFOLD_LIQUID_MESH"


def test_evaluate_targets_flags_multiple_bodies_as_info(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)

    findings = handler.result_validation._evaluate_targets([_frame_report(5, components=2)], None, None, "ALLOW")

    assert any(finding["code"] == "MULTIPLE_LIQUID_BODIES" for finding in findings)


def test_evaluate_targets_forbids_spill_only_under_forbid_policy(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    container = {"container": "Glass", "spill_volume": 1.0, "escaped_volume_near_container": 0.0, "fill_fraction": 1.0}
    report = _frame_report(5, containers=[container])

    allowed = handler.result_validation._evaluate_targets([report], None, None, "ALLOW")
    assert not any(finding["code"] == "OVERFLOW_FORBIDDEN" for finding in allowed)

    forbidden = handler.result_validation._evaluate_targets([report], None, None, "FORBID")
    assert any(finding["code"] == "OVERFLOW_FORBIDDEN" and finding["severity"] == "ERROR" for finding in forbidden)


def test_evaluate_targets_flags_fill_target_missed_at_and_after_deadline_not_before(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    container = {"container": "Glass", "spill_volume": 0.0, "escaped_volume_near_container": 0.0, "fill_fraction": 0.5}

    early_findings = handler.result_validation._evaluate_targets(
        [_frame_report(9, containers=[container])], 0.9, 10, "ALLOW"
    )
    assert not any(finding["code"] == "FILL_TARGET_MISSED" for finding in early_findings)

    late_findings = handler.result_validation._evaluate_targets(
        [_frame_report(10, containers=[container])], 0.9, 10, "ALLOW"
    )
    assert any(finding["code"] == "FILL_TARGET_MISSED" and finding["severity"] == "ERROR" for finding in late_findings)


# ---------------------------------------------------------------------------
# validate_liquid_result: input validation and orchestration
# ---------------------------------------------------------------------------


def _domain_settings(**overrides):
    base = dict(cache_type="REPLAY", has_cache_baked_any=False, cache_frame_start=1, resolution_max=50)
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_validate_liquid_result_rejects_empty_frames(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    obj = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    monkeypatch.setattr(handler.result_validation, "_get_domain", lambda *_args: (obj, modifier, _domain_settings()))

    with pytest.raises(ValueError, match="unique frame numbers"):
        handler.LiquidHandlersMixin().validate_liquid_result("Domain", "Liquid Domain", [])


def test_validate_liquid_result_rejects_duplicate_frames(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    obj = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    monkeypatch.setattr(handler.result_validation, "_get_domain", lambda *_args: (obj, modifier, _domain_settings()))

    with pytest.raises(ValueError, match="unique frame numbers"):
        handler.LiquidHandlersMixin().validate_liquid_result("Domain", "Liquid Domain", [5, 5])


def test_validate_liquid_result_rejects_too_many_frames(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    obj = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    monkeypatch.setattr(handler.result_validation, "_get_domain", lambda *_args: (obj, modifier, _domain_settings()))

    with pytest.raises(ValueError, match="unique frame numbers"):
        handler.LiquidHandlersMixin().validate_liquid_result("Domain", "Liquid Domain", list(range(1, 34)))


def test_validate_liquid_result_rejects_invalid_overflow_policy(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    obj = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    monkeypatch.setattr(handler.result_validation, "_get_domain", lambda *_args: (obj, modifier, _domain_settings()))

    with pytest.raises(ValueError, match="overflow_policy must be one of"):
        handler.LiquidHandlersMixin().validate_liquid_result("Domain", "Liquid Domain", [5], overflow_policy="IGNORE")


def test_validate_liquid_result_rejects_out_of_range_sample_resolution(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    obj = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    monkeypatch.setattr(handler.result_validation, "_get_domain", lambda *_args: (obj, modifier, _domain_settings()))

    with pytest.raises(ValueError, match="sample_resolution must be in"):
        handler.LiquidHandlersMixin().validate_liquid_result("Domain", "Liquid Domain", [5], sample_resolution=1)


def test_validate_liquid_result_rejects_out_of_range_target_fill_fraction(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    obj = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    monkeypatch.setattr(handler.result_validation, "_get_domain", lambda *_args: (obj, modifier, _domain_settings()))

    with pytest.raises(ValueError, match="target_fill_fraction must be in"):
        handler.LiquidHandlersMixin().validate_liquid_result("Domain", "Liquid Domain", [5], target_fill_fraction=1.5)


def test_validate_liquid_result_rejects_unbaked_non_replay_domain(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    obj = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = _domain_settings(cache_type="MODULAR", has_cache_baked_any=False)
    monkeypatch.setattr(handler.result_validation, "_get_domain", lambda *_args: (obj, modifier, settings))

    with pytest.raises(ValueError, match="REPLAY cache mode or an existing"):
        handler.LiquidHandlersMixin().validate_liquid_result("Domain", "Liquid Domain", [5])


def test_validate_liquid_result_rejects_when_no_container_specs_found(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    obj = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    monkeypatch.setattr(handler.result_validation, "_get_domain", lambda *_args: (obj, modifier, _domain_settings()))
    monkeypatch.setattr(handler.result_validation, "_resolve_container_specs", lambda *_args: [])

    with pytest.raises(ValueError, match="No validation volumes were found"):
        handler.LiquidHandlersMixin().validate_liquid_result("Domain", "Liquid Domain", [5])


def test_validate_liquid_result_rejects_when_sample_budget_exceeded(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    obj = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    monkeypatch.setattr(handler.result_validation, "_get_domain", lambda *_args: (obj, modifier, _domain_settings()))
    # 32 frames x 4 containers x 32**3 sample_resolution = 4,194,304 > the 4,000,000 cap.
    monkeypatch.setattr(
        handler.result_validation,
        "_resolve_container_specs",
        lambda *_args: [{"container_name": f"C{i}"} for i in range(4)],
    )

    with pytest.raises(ValueError, match="exceeding the"):
        handler.LiquidHandlersMixin().validate_liquid_result(
            "Domain", "Liquid Domain", list(range(1, 33)), sample_resolution=32
        )


def test_validate_liquid_result_rejects_frames_outside_scene_range(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    obj = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    monkeypatch.setattr(handler.result_validation, "_get_domain", lambda *_args: (obj, modifier, _domain_settings()))
    monkeypatch.setattr(
        handler.result_validation, "_resolve_container_specs", lambda *_args: [{"container_name": "Glass"}]
    )
    sys.modules["bpy"].data.scenes = [_fake_scene_for("Domain", frame_end=250)]

    with pytest.raises(ValueError, match="scene frame range"):
        handler.LiquidHandlersMixin().validate_liquid_result("Domain", "Liquid Domain", [300])


def test_validate_liquid_result_rejects_replay_frame_before_cache_start(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    obj = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = _domain_settings(cache_type="REPLAY", cache_frame_start=10)
    monkeypatch.setattr(handler.result_validation, "_get_domain", lambda *_args: (obj, modifier, settings))
    monkeypatch.setattr(
        handler.result_validation, "_resolve_container_specs", lambda *_args: [{"container_name": "Glass"}]
    )
    sys.modules["bpy"].data.scenes = [_fake_scene_for("Domain")]

    with pytest.raises(ValueError, match="cache_frame_start"):
        handler.LiquidHandlersMixin().validate_liquid_result("Domain", "Liquid Domain", [5])


def test_validate_liquid_result_rejects_replay_preroll_over_budget(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    obj = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = _domain_settings(cache_type="REPLAY", cache_frame_start=1)
    monkeypatch.setattr(handler.result_validation, "_get_domain", lambda *_args: (obj, modifier, settings))
    monkeypatch.setattr(
        handler.result_validation, "_resolve_container_specs", lambda *_args: [{"container_name": "Glass"}]
    )
    sys.modules["bpy"].data.scenes = [_fake_scene_for("Domain", frame_end=500)]

    with pytest.raises(ValueError, match="max_preroll_frames"):
        handler.LiquidHandlersMixin().validate_liquid_result("Domain", "Liquid Domain", [300], max_preroll_frames=250)


def test_validate_liquid_result_rejects_modular_frame_outside_baked_range(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    obj = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = _domain_settings(
        cache_type="MODULAR",
        has_cache_baked_any=True,
        cache_frame_start=1,
        cache_frame_end=100,
        use_mesh=True,
        cache_frame_pause_mesh=40,
        cache_frame_pause_data=0,
        cache_frame_pause_particles=0,
        cache_frame_pause_guide=0,
    )
    monkeypatch.setattr(handler.result_validation, "_get_domain", lambda *_args: (obj, modifier, settings))
    monkeypatch.setattr(
        handler.result_validation, "_resolve_container_specs", lambda *_args: [{"container_name": "Glass"}]
    )
    sys.modules["bpy"].data.scenes = [_fake_scene_for("Domain")]

    with pytest.raises(ValueError, match="baked cache range"):
        handler.LiquidHandlersMixin().validate_liquid_result("Domain", "Liquid Domain", [80])


def test_validate_liquid_result_modular_happy_path_returns_shape_and_passes(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    obj = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = _domain_settings(
        cache_type="MODULAR",
        has_cache_baked_any=True,
        cache_frame_start=1,
        cache_frame_end=100,
        use_mesh=True,
        cache_frame_pause_mesh=0,
        cache_frame_pause_data=0,
        cache_frame_pause_particles=0,
        cache_frame_pause_guide=0,
    )
    monkeypatch.setattr(handler.result_validation, "_get_domain", lambda *_args: (obj, modifier, settings))
    monkeypatch.setattr(
        handler.result_validation, "_resolve_container_specs", lambda *_args: [{"container_name": "Glass"}]
    )
    monkeypatch.setattr(
        handler.result_validation, "_world_bounds", lambda *_args, **_kwargs: {"dimensions": [2.0, 2.0, 2.0]}
    )
    sys.modules["bpy"].data.scenes = [_fake_scene_for("Domain")]

    measured_frames = []

    def fake_measure_frame(_domain_obj, frame, _container_specs, _resolution, _epsilon):
        measured_frames.append(frame)
        return {
            "frame": frame,
            "liquid_mesh": {"non_manifold_edges": 0, "connected_components": 1},
            "containers": [
                {
                    "container": "Glass",
                    "spill_volume": 0.0,
                    "escaped_volume_near_container": 0.0,
                    "fill_fraction": 0.9,
                }
            ],
        }

    monkeypatch.setattr(handler.result_validation, "_measure_frame", fake_measure_frame)

    result = handler.LiquidHandlersMixin().validate_liquid_result("Domain", "Liquid Domain", [10, 20])

    assert measured_frames == [10, 20]
    assert result["changed_objects"] == []
    assert result["domain"] == "Domain"
    assert result["cache_type"] == "MODULAR"
    assert result["requested_frames"] == [10, 20]
    assert result["evaluated_frames"] == [10, 20]
    assert result["timed_out"] is False
    assert result["findings"] == []
    assert result["passed"] is True
    assert result["timeline_restored"] == {"frame": 1, "subframe": 0.0}


def test_validate_liquid_result_replay_steps_sequentially_and_only_measures_requested_frames(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    obj = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = _domain_settings(cache_type="REPLAY", has_cache_baked_any=False, cache_frame_start=5)
    monkeypatch.setattr(handler.result_validation, "_get_domain", lambda *_args: (obj, modifier, settings))
    monkeypatch.setattr(
        handler.result_validation, "_resolve_container_specs", lambda *_args: [{"container_name": "Glass"}]
    )
    monkeypatch.setattr(
        handler.result_validation, "_world_bounds", lambda *_args, **_kwargs: {"dimensions": [2.0, 2.0, 2.0]}
    )
    scene = _fake_scene_for("Domain")
    sys.modules["bpy"].data.scenes = [scene]

    stepped_frames = []
    original_frame_set = scene.frame_set

    def tracking_frame_set(frame, subframe=0.0):
        stepped_frames.append(frame)
        original_frame_set(frame, subframe)

    scene.frame_set = tracking_frame_set

    measured_frames = []

    def fake_measure_frame(_domain_obj, frame, _container_specs, _resolution, _epsilon):
        measured_frames.append(frame)
        return {"frame": frame, "liquid_mesh": {"non_manifold_edges": 0, "connected_components": 1}, "containers": []}

    monkeypatch.setattr(handler.result_validation, "_measure_frame", fake_measure_frame)

    result = handler.LiquidHandlersMixin().validate_liquid_result("Domain", "Liquid Domain", [8])

    # Steps one frame at a time from cache_frame_start=5 through the requested frame 8,
    # then the `finally` block restores the scene to its original frame_current (1).
    assert stepped_frames == [5, 6, 7, 8, 1]
    assert measured_frames == [8]
    assert result["changed_objects"] == ["Domain"]
    assert result["preroll_frames"] == 4
