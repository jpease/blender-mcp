"""Regression coverage for setup_liquid_shot (the liquid-shot orchestrator, item 11)."""

import asyncio
import sys
import types

import pytest

from pydantic import ValidationError
from test_mutation_transaction import _load_addon

from blender_mcp.server.tools import _dispatch, liquid


def _run(function, **kwargs):
    return asyncio.run(function(ctx=None, **kwargs))


def _load_liquid_handler(monkeypatch):
    addon, _bpy = _load_addon(monkeypatch, data={})
    return addon, sys.modules[f"{addon.__name__}.handlers.liquid"]


# ---------------------------------------------------------------------------
# Fakes for the orchestration-level tests
# ---------------------------------------------------------------------------


class _FakeIDObject:
    """Enough of a bpy object to be tagged with custom properties by name."""

    def __init__(self, name):
        self.name = name
        self._props = {}

    def __setitem__(self, key, value):
        self._props[key] = value

    def __getitem__(self, key):
        return self._props[key]

    def get(self, key, default=None):
        return self._props.get(key, default)


class _AutoObjectRegistry:
    """Fake ``_get_object`` that hands back the same fake object for a repeated name."""

    def __init__(self):
        self._objects = {}

    def __call__(self, name, types_=None):
        if name not in self._objects:
            self._objects[name] = _FakeIDObject(name)
        return self._objects[name]


def _fake_scene(linked_names, *, fps=24.0, fps_base=1.0, frame_start=1, name="Scene"):
    return types.SimpleNamespace(
        name=name,
        render=types.SimpleNamespace(fps=fps, fps_base=fps_base),
        frame_start=frame_start,
        objects=set(linked_names),
    )


def _standard_result_stubs(calls):
    """Sub-handler stubs shaped like the real return dicts, each recording that it ran."""

    def create_liquid_domain(*_a, **_k):
        calls.append("create_liquid_domain")
        return {
            "object": "Domain",
            "domain_uuid": "domain-uuid",
            "created_object": True,
            "cache_directory_resolved": "/tmp/cache",
            "warnings": [],
        }

    def create_liquid_proxy_rig(*_a, **_k):
        calls.append("create_liquid_proxy_rig")
        return {
            "proxy": "Glass Collision Proxy",
            "proxy_uuid": "proxy-uuid",
            "fluid_modifier": "Liquid Domain",
            "collection": "Liquid Proxies",
            "transform_validation": {"ok": True},
            "warnings": [],
        }

    def add_liquid_effector(*_a, **_k):
        calls.append("add_liquid_effector")
        return {"modifier": "Liquid Effector", "effector_collection": "Effectors", "warnings": []}

    def add_liquid_flow(*_a, **_k):
        calls.append("add_liquid_flow")
        return {"modifier": "Liquid Flow", "flow_collection": "Flows"}

    def animate_liquid_flow(*_a, **_k):
        calls.append("animate_liquid_flow")
        return {"keyframes": []}

    def fit_liquid_domain(*_a, **_k):
        calls.append("fit_liquid_domain")
        return {"fitted": True}

    def apply_liquid_quality_profile(*_a, **_k):
        calls.append("apply_liquid_quality_profile")
        return {"applied_sections": ["solver", "mesh"]}

    def validate_liquid_setup(*_a, **_k):
        calls.append("validate_liquid_setup")
        return {"findings": [], "limitations": []}

    return {
        "create_liquid_domain": create_liquid_domain,
        "create_liquid_proxy_rig": create_liquid_proxy_rig,
        "add_liquid_effector": add_liquid_effector,
        "add_liquid_flow": add_liquid_flow,
        "animate_liquid_flow": animate_liquid_flow,
        "fit_liquid_domain": fit_liquid_domain,
        "apply_liquid_quality_profile": apply_liquid_quality_profile,
        "validate_liquid_setup": validate_liquid_setup,
    }


def _wire_handlers(monkeypatch, handler, handlers, scene, registry, *, build_volumes=None):
    monkeypatch.setattr(handler.shot, "_get_scene", lambda name: scene)
    monkeypatch.setattr(handler.shot, "_get_object", registry)
    domain_settings = types.SimpleNamespace(cache_directory=None)
    monkeypatch.setattr(
        handler.shot,
        "_get_domain",
        lambda name, modifier_name: (
            registry(name),
            types.SimpleNamespace(name=modifier_name),
            domain_settings,
        ),
    )
    calls = []
    for method_name, stub in _standard_result_stubs(calls).items():
        monkeypatch.setattr(handlers, method_name, stub)
    monkeypatch.setattr(handlers, "_build_validation_volumes", build_volumes or (lambda *_a, **_k: []))
    return calls


# ---------------------------------------------------------------------------
# Pure geometry/logic helpers
# ---------------------------------------------------------------------------


def test_resolve_enabled_window_returns_none_when_not_requested(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    assert handler.shot._resolve_enabled_window(None, "INFLOW", 24.0, 1, 0) is None


def test_resolve_enabled_window_rejects_geometry_behavior(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    with pytest.raises(ValueError, match="GEOMETRY flow"):
        handler.shot._resolve_enabled_window((1.0, 2.0), "GEOMETRY", 24.0, 1, 0)


def test_resolve_enabled_window_rejects_non_increasing_pair(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    with pytest.raises(ValueError, match="increasing"):
        handler.shot._resolve_enabled_window((2.0, 2.0), "INFLOW", 24.0, 1, 0)
    with pytest.raises(ValueError, match="pair"):
        handler.shot._resolve_enabled_window((1.0, 2.0, 3.0), "INFLOW", 24.0, 1, 0)


def test_resolve_enabled_window_converts_seconds_to_frames(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    assert handler.shot._resolve_enabled_window((1.0, 3.0), "OUTFLOW", 24.0, 1, 0) == (25, 73)


def test_resolve_enabled_window_rejects_window_collapsing_to_one_frame(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    with pytest.raises(ValueError, match="single frame"):
        handler.shot._resolve_enabled_window((0.0, 0.01), "INFLOW", 1.0, 1, 0)


def test_interior_box_shrinks_by_wall_and_bottom_thickness(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    bounds = {"minimum": (0.0, 0.0, 0.0), "maximum": (2.0, 2.0, 2.0), "dimensions": (2.0, 2.0, 2.0)}
    center, dimensions = handler.shot._interior_box(bounds, "Z", 0.1, 0.2)
    assert dimensions == pytest.approx([1.8, 1.8, 1.8])
    assert center == pytest.approx([1.0, 1.0, 1.1])


def test_interior_box_rejects_thickness_that_collapses_interior(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    bounds = {"minimum": (0.0, 0.0, 0.0), "maximum": (1.0, 1.0, 1.0), "dimensions": (1.0, 1.0, 1.0)}
    with pytest.raises(ValueError, match="collapses"):
        handler.shot._interior_box(bounds, "Z", 0.6, 0.1)


def test_spill_box_extends_below_rim_with_margin(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    bounds = {"minimum": (0.0, 0.0, 0.0), "maximum": (2.0, 2.0, 2.0), "dimensions": (2.0, 2.0, 2.0)}
    center, dimensions = handler.shot._spill_box(bounds, "Z", 0.5, 0.25)
    assert dimensions == pytest.approx([2.5, 2.5, 2.5])
    assert center == pytest.approx([1.0, 1.0, 0.75])


def test_spill_margin_defaults_to_widest_lateral_extent(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    bounds = {"dimensions": (1.0, 3.0, 2.0)}
    assert handler.shot._spill_margin(bounds, "Z") == pytest.approx(3.0)
    assert handler.shot._spill_margin(bounds, "X") == pytest.approx(3.0)


def test_spill_margin_floors_at_a_tiny_value_for_degenerate_bounds(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    bounds = {"dimensions": (0.0, 0.0, 1.0)}
    assert handler.shot._spill_margin(bounds, "Z") == pytest.approx(1e-4)


def test_box_object_builds_centered_hidden_wireframe_box(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)

    class _FakeMesh:
        def __init__(self, name):
            self.name = name

        def from_pydata(self, vertices, edges, faces):
            self.vertices = vertices
            self.edges = edges
            self.faces = faces

        def validate(self):
            pass

        def update(self):
            pass

    class _FakeMeshes:
        def new(self, name):
            return _FakeMesh(name)

    class _FakeObject:
        def __init__(self, name, mesh):
            self.name = name
            self.data = mesh

    class _FakeObjects:
        def new(self, name, mesh):
            return _FakeObject(name, mesh)

    class _FakeMatrix:
        @staticmethod
        def Translation(vector):
            return ("translation", tuple(vector))

    monkeypatch.setattr(handler.shot.bpy, "data", types.SimpleNamespace(meshes=_FakeMeshes(), objects=_FakeObjects()))
    monkeypatch.setattr(handler.shot.mathutils, "Matrix", _FakeMatrix, raising=False)

    obj = handler.shot._box_object("Interior Volume", [1.0, 2.0, 3.0], [2.0, 4.0, 6.0])

    assert obj.name == "Interior Volume"
    assert obj.matrix_world == ("translation", (1.0, 2.0, 3.0))
    assert obj.hide_render is True
    assert obj.display_type == "WIRE"
    assert len(obj.data.vertices) == 8
    assert obj.data.vertices[0] == pytest.approx((-1.0, -2.0, -3.0))
    assert obj.data.vertices[6] == pytest.approx((1.0, 2.0, 3.0))


# ---------------------------------------------------------------------------
# Role-conflict rejection
# ---------------------------------------------------------------------------


def test_reject_role_conflicts_flags_duplicate_container(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    containers = [{"object": types.SimpleNamespace(name="Glass")}, {"object": types.SimpleNamespace(name="Glass")}]
    with pytest.raises(ValueError, match="containers.*more than once"):
        handler.LiquidHandlersMixin._reject_role_conflicts(containers, [], None)


def test_reject_role_conflicts_flags_container_source_overlap(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    containers = [{"object": types.SimpleNamespace(name="Glass")}]
    sources = [{"object": types.SimpleNamespace(name="Glass")}]
    with pytest.raises(ValueError, match="cannot be both container and source"):
        handler.LiquidHandlersMixin._reject_role_conflicts(containers, sources, None)


def test_reject_role_conflicts_flags_domain_object_reuse(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    containers = [{"object": types.SimpleNamespace(name="Glass")}]
    with pytest.raises(ValueError, match="domain object cannot also be"):
        handler.LiquidHandlersMixin._reject_role_conflicts(containers, [], "Glass")


# ---------------------------------------------------------------------------
# _resolve_containers / _resolve_sources validation
# ---------------------------------------------------------------------------


def test_resolve_containers_rejects_object_not_linked_to_scene(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    monkeypatch.setattr(handler.shot, "_get_object", _AutoObjectRegistry())
    scene = _fake_scene([])
    with pytest.raises(ValueError, match="not linked to scene"):
        handler.shot._resolve_containers(scene, [{"object_name": "Glass"}])


def test_resolve_containers_rejects_unknown_collision_proxy(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    monkeypatch.setattr(handler.shot, "_get_object", _AutoObjectRegistry())
    scene = _fake_scene(["Glass"])
    with pytest.raises(ValueError, match="collision_proxy"):
        handler.shot._resolve_containers(scene, [{"object_name": "Glass", "collision_proxy": "BOWL"}])


def test_resolve_containers_rejects_unknown_rim_axis(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    monkeypatch.setattr(handler.shot, "_get_object", _AutoObjectRegistry())
    scene = _fake_scene(["Glass"])
    with pytest.raises(ValueError, match="rim_axis"):
        handler.shot._resolve_containers(scene, [{"object_name": "Glass", "rim_axis": "UP"}])


def test_resolve_containers_rejects_non_positive_wall_thickness(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    monkeypatch.setattr(handler.shot, "_get_object", _AutoObjectRegistry())
    scene = _fake_scene(["Glass"])
    with pytest.raises(ValueError, match="wall_thickness"):
        handler.shot._resolve_containers(scene, [{"object_name": "Glass", "wall_thickness": 0.0}])


def test_resolve_containers_rejects_empty_or_oversized_lists(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    monkeypatch.setattr(handler.shot, "_get_object", _AutoObjectRegistry())
    scene = _fake_scene(["Glass"])
    with pytest.raises(ValueError, match="1-16"):
        handler.shot._resolve_containers(scene, [])
    with pytest.raises(ValueError, match="1-16"):
        handler.shot._resolve_containers(scene, [{"object_name": "Glass"}] * 17)


def test_resolve_containers_defaults_proxy_name_from_object(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    monkeypatch.setattr(handler.shot, "_get_object", _AutoObjectRegistry())
    scene = _fake_scene(["Glass"])
    resolved = handler.shot._resolve_containers(scene, [{"object_name": "Glass"}])
    assert resolved[0]["proxy_object_name"] == "Glass Collision Proxy"


def test_resolve_sources_rejects_object_not_linked_to_scene(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    monkeypatch.setattr(handler.shot, "_get_object", _AutoObjectRegistry())
    scene = _fake_scene([])
    with pytest.raises(ValueError, match="not linked to scene"):
        handler.shot._resolve_sources(scene, [{"object_name": "Pour"}], 24.0, 1)


def test_resolve_sources_rejects_unknown_behavior(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    monkeypatch.setattr(handler.shot, "_get_object", _AutoObjectRegistry())
    scene = _fake_scene(["Pour"])
    with pytest.raises(ValueError, match="behavior"):
        handler.shot._resolve_sources(scene, [{"object_name": "Pour", "behavior": "SPLASH"}], 24.0, 1)


def test_resolve_sources_rejects_empty_or_oversized_lists(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    monkeypatch.setattr(handler.shot, "_get_object", _AutoObjectRegistry())
    scene = _fake_scene(["Pour"])
    with pytest.raises(ValueError, match="1-16"):
        handler.shot._resolve_sources(scene, [], 24.0, 1)


# ---------------------------------------------------------------------------
# Full orchestration
# ---------------------------------------------------------------------------


def test_execute_shot_uses_proxy_rig_for_hollow_container_and_skips_direct_effector(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    scene = _fake_scene(["Glass", "Pour"])
    registry = _AutoObjectRegistry()
    handlers = handler.LiquidHandlersMixin()
    calls = _wire_handlers(monkeypatch, handler, handlers, scene, registry)

    result = handlers.setup_liquid_shot(
        "Scene",
        "/tmp/cache",
        containers=[{"object_name": "Glass", "collision_proxy": "HOLLOW_CONTAINER"}],
        sources=[{"object_name": "Pour", "behavior": "INFLOW"}],
        solver_patch={"resolution_max": 96},
        mesh_patch={"mesh_scale": 2},
    )

    assert "create_liquid_proxy_rig" in calls
    assert "add_liquid_effector" not in calls
    assert result["dry_run"] is False
    assert result["containers"][0]["proxy"] == "Glass Collision Proxy"
    assert result["changed_objects"] == sorted({"Domain", "Glass", "Glass Collision Proxy", "Pour"})


def test_execute_shot_uses_direct_effector_when_no_proxy_requested(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    scene = _fake_scene(["Glass", "Pour"])
    registry = _AutoObjectRegistry()
    handlers = handler.LiquidHandlersMixin()
    calls = _wire_handlers(monkeypatch, handler, handlers, scene, registry)

    result = handlers.setup_liquid_shot(
        "Scene",
        "/tmp/cache",
        containers=[{"object_name": "Glass", "collision_proxy": "NONE"}],
        sources=[{"object_name": "Pour", "behavior": "INFLOW"}],
        solver_patch={"resolution_max": 96},
        mesh_patch={"mesh_scale": 2},
    )

    assert "add_liquid_effector" in calls
    assert "create_liquid_proxy_rig" not in calls
    assert result["containers"][0]["proxy"] is None
    assert result["changed_objects"] == sorted({"Domain", "Glass", "Pour"})


def test_execute_shot_animates_flow_only_when_enabled_seconds_given(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    scene = _fake_scene(["Glass", "Pour"], fps=24.0, frame_start=1)
    registry = _AutoObjectRegistry()
    handlers = handler.LiquidHandlersMixin()
    calls = _wire_handlers(monkeypatch, handler, handlers, scene, registry)

    result = handlers.setup_liquid_shot(
        "Scene",
        "/tmp/cache",
        containers=[{"object_name": "Glass"}],
        sources=[{"object_name": "Pour", "behavior": "INFLOW", "enabled_seconds": (1.0, 3.0)}],
        solver_patch={"resolution_max": 96},
        mesh_patch={"mesh_scale": 2},
    )

    assert "animate_liquid_flow" in calls
    assert result["sources"][0]["enabled_frames"] == [25, 73]


def test_execute_shot_skips_animation_when_no_enabled_seconds(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    scene = _fake_scene(["Glass", "Pour"])
    registry = _AutoObjectRegistry()
    handlers = handler.LiquidHandlersMixin()
    calls = _wire_handlers(monkeypatch, handler, handlers, scene, registry)

    result = handlers.setup_liquid_shot(
        "Scene",
        "/tmp/cache",
        containers=[{"object_name": "Glass"}],
        sources=[{"object_name": "Pour", "behavior": "GEOMETRY"}],
        solver_patch={"resolution_max": 96},
        mesh_patch={"mesh_scale": 2},
    )

    assert "animate_liquid_flow" not in calls
    assert result["sources"][0]["enabled_frames"] is None


def test_setup_liquid_shot_rejects_geometry_behavior_with_enabled_seconds_before_any_mutation(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    scene = _fake_scene(["Glass", "Pour"])
    registry = _AutoObjectRegistry()
    handlers = handler.LiquidHandlersMixin()
    calls = _wire_handlers(monkeypatch, handler, handlers, scene, registry)

    with pytest.raises(ValueError, match="GEOMETRY flow"):
        handlers.setup_liquid_shot(
            "Scene",
            "/tmp/cache",
            containers=[{"object_name": "Glass"}],
            sources=[{"object_name": "Pour", "behavior": "GEOMETRY", "enabled_seconds": (1.0, 2.0)}],
            solver_patch={"resolution_max": 96},
            mesh_patch={"mesh_scale": 2},
        )
    assert calls == []


def test_setup_liquid_shot_rejects_duplicate_container_and_source_object(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    scene = _fake_scene(["Glass"])
    registry = _AutoObjectRegistry()
    handlers = handler.LiquidHandlersMixin()
    calls = _wire_handlers(monkeypatch, handler, handlers, scene, registry)

    with pytest.raises(ValueError, match="cannot be both container and source"):
        handlers.setup_liquid_shot(
            "Scene",
            "/tmp/cache",
            containers=[{"object_name": "Glass"}],
            sources=[{"object_name": "Glass"}],
            solver_patch={"resolution_max": 96},
            mesh_patch={"mesh_scale": 2},
        )
    assert calls == []


def test_setup_liquid_shot_rejects_domain_object_reused_as_container(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    scene = _fake_scene(["Glass", "Pour"])
    registry = _AutoObjectRegistry()
    handlers = handler.LiquidHandlersMixin()
    calls = _wire_handlers(monkeypatch, handler, handlers, scene, registry)

    with pytest.raises(ValueError, match="domain object cannot also be"):
        handlers.setup_liquid_shot(
            "Scene",
            "/tmp/cache",
            containers=[{"object_name": "Glass"}],
            sources=[{"object_name": "Pour"}],
            domain_object_name="Glass",
            solver_patch={"resolution_max": 96},
            mesh_patch={"mesh_scale": 2},
        )
    assert calls == []


def test_setup_liquid_shot_requires_a_resolved_quality_patch_unless_dry_run(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    scene = _fake_scene(["Glass", "Pour"])
    registry = _AutoObjectRegistry()
    handlers = handler.LiquidHandlersMixin()
    calls = _wire_handlers(monkeypatch, handler, handlers, scene, registry)

    with pytest.raises(ValueError, match="resolved solver/mesh patches"):
        handlers.setup_liquid_shot(
            "Scene",
            "/tmp/cache",
            containers=[{"object_name": "Glass"}],
            sources=[{"object_name": "Pour"}],
        )
    assert calls == []


def test_dry_run_reports_plan_without_mutating_or_needing_a_quality_patch(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    scene = _fake_scene(["Glass", "Pour"])
    registry = _AutoObjectRegistry()
    handlers = handler.LiquidHandlersMixin()
    calls = _wire_handlers(monkeypatch, handler, handlers, scene, registry)

    result = handlers.setup_liquid_shot(
        "Scene",
        "/tmp/cache",
        containers=[{"object_name": "Glass", "collision_proxy": "HOLLOW_CONTAINER"}],
        sources=[{"object_name": "Pour", "behavior": "INFLOW", "enabled_seconds": (1.0, 3.0)}],
        dry_run=True,
    )

    assert calls == []
    assert result["dry_run"] is True
    assert result["simulation_id"] is None
    assert result["changed_objects"] == []
    assert result["retained_live_modifier"] is True
    assert result["existing_setup_validation"] is None
    assert result["plan"]["containers"][0]["object"] == "Glass"
    assert result["plan"]["sources"][0]["enabled_frames"] == [25, 73]
    step_names = [step["step"] for step in result["plan"]["steps"]]
    assert "create_liquid_proxy_rig" in step_names
    assert "animate_liquid_flow" in step_names
    assert "validate_liquid_setup" in step_names


def test_dry_run_runs_preflight_validation_only_when_named_domain_already_exists(monkeypatch) -> None:
    addon, handler = _load_liquid_handler(monkeypatch)
    scene = _fake_scene(["Glass", "Pour"])
    registry = _AutoObjectRegistry()
    handlers = handler.LiquidHandlersMixin()
    calls = _wire_handlers(monkeypatch, handler, handlers, scene, registry)

    bpy = sys.modules["bpy"]
    monkeypatch.setattr(bpy, "data", types.SimpleNamespace(objects=types.SimpleNamespace(get=lambda _n: None)))
    result_missing = handlers.setup_liquid_shot(
        "Scene",
        "/tmp/cache",
        containers=[{"object_name": "Glass"}],
        sources=[{"object_name": "Pour"}],
        domain_object_name="Domain",
        dry_run=True,
    )
    assert result_missing["existing_setup_validation"] is None
    assert calls == []

    monkeypatch.setattr(bpy, "data", types.SimpleNamespace(objects=types.SimpleNamespace(get=lambda _n: object())))
    result_existing = handlers.setup_liquid_shot(
        "Scene",
        "/tmp/cache",
        containers=[{"object_name": "Glass"}],
        sources=[{"object_name": "Pour"}],
        domain_object_name="Domain",
        dry_run=True,
    )
    assert result_existing["existing_setup_validation"] == {"findings": [], "limitations": []}
    assert calls == ["validate_liquid_setup"]


# ---------------------------------------------------------------------------
# Server-side typed layer
# ---------------------------------------------------------------------------


def test_shot_container_rejects_proxy_object_name_without_hollow_container() -> None:
    with pytest.raises(ValidationError, match="proxy_object_name requires"):
        liquid.ShotContainer(object_name="Glass", collision_proxy="NONE", proxy_object_name="Glass Proxy")


def test_shot_container_allows_proxy_object_name_with_hollow_container() -> None:
    container = liquid.ShotContainer(
        object_name="Glass", collision_proxy="HOLLOW_CONTAINER", proxy_object_name="Glass Proxy"
    )
    assert container.proxy_object_name == "Glass Proxy"


def test_shot_source_rejects_enabled_seconds_for_geometry_behavior() -> None:
    with pytest.raises(ValidationError, match="GEOMETRY flow"):
        liquid.ShotSource(object_name="Pour", behavior="GEOMETRY", enabled_seconds=(0.0, 1.0))


def test_shot_source_rejects_non_increasing_or_negative_enabled_seconds() -> None:
    with pytest.raises(ValidationError, match="increasing, non-negative"):
        liquid.ShotSource(object_name="Pour", behavior="INFLOW", enabled_seconds=(2.0, 1.0))
    with pytest.raises(ValidationError, match="increasing, non-negative"):
        liquid.ShotSource(object_name="Pour", behavior="INFLOW", enabled_seconds=(-1.0, 1.0))


def test_shot_source_accepts_valid_enabled_seconds_window() -> None:
    source = liquid.ShotSource(object_name="Pour", behavior="OUTFLOW", enabled_seconds=(0.5, 2.0))
    assert source.enabled_seconds == (0.5, 2.0)


def test_changed_objects_unions_containers_sources_and_domain() -> None:
    containers = [liquid.ShotContainer(object_name="Glass"), liquid.ShotContainer(object_name="Bowl")]
    sources = [liquid.ShotSource(object_name="Pour")]
    assert liquid.shot._changed_objects(containers, sources, "Domain") == ["Bowl", "Domain", "Glass", "Pour"]
    assert liquid.shot._changed_objects(containers, sources, None) == ["Bowl", "Glass", "Pour"]


def test_changed_objects_dedupes_names() -> None:
    containers = [liquid.ShotContainer(object_name="Glass")]
    sources = [liquid.ShotSource(object_name="Glass")]
    assert liquid.shot._changed_objects(containers, sources, "Glass") == ["Glass"]


def test_shot_source_payload_flattens_enabled_seconds_to_a_list() -> None:
    with_window = liquid.ShotSource(object_name="Pour", behavior="INFLOW", enabled_seconds=(1.0, 2.0))
    payload = liquid.shot._shot_source_payload(with_window)
    assert payload["enabled_seconds"] == [1.0, 2.0]

    without_window = liquid.ShotSource(object_name="Pour")
    payload = liquid.shot._shot_source_payload(without_window)
    assert "enabled_seconds" not in payload


def test_setup_liquid_shot_tool_forwards_resolved_quality_profile_and_changed_objects(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )

    result = _run(
        liquid.setup_liquid_shot,
        scene_name="Scene",
        cache_directory="/tmp/cache",
        containers=[liquid.ShotContainer(object_name="Glass", collision_proxy="HOLLOW_CONTAINER")],
        sources=[liquid.ShotSource(object_name="Pour", enabled_seconds=(1.0, 2.0))],
        domain_object_name="Domain",
        quality="FINAL",
    )

    assert result["ok"] is True
    assert len(calls) == 1
    assert result["changed_objects"] == ["Domain", "Glass", "Pour"]
    command, params = calls[0]
    assert command == "setup_liquid_shot"
    assert params["containers"] == [{"object_name": "Glass", "collision_proxy": "HOLLOW_CONTAINER"}]
    assert params["sources"] == [{"object_name": "Pour", "enabled_seconds": [1.0, 2.0]}]
    solver_patch, mesh_patch = liquid.profile_patches("FINAL")
    assert params["solver_patch"] == solver_patch
    assert params["mesh_patch"] == mesh_patch
    assert params["dry_run"] is False


def test_setup_liquid_shot_tool_dry_run_sends_no_changed_objects(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )

    result = _run(
        liquid.setup_liquid_shot,
        scene_name="Scene",
        cache_directory="/tmp/cache",
        containers=[liquid.ShotContainer(object_name="Glass")],
        sources=[liquid.ShotSource(object_name="Pour")],
        dry_run=True,
    )

    assert len(calls) == 1
    assert result["changed_objects"] == []


def test_all_liquid_shot_names_are_registered() -> None:
    assert callable(liquid.setup_liquid_shot)
    assert issubclass(liquid.ShotContainer, object)
    assert issubclass(liquid.ShotSource, object)
    assert "setup_liquid_shot" in liquid.mcp._tool_manager._tools
