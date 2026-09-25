"""Regression coverage for the typed phase-zero liquid MCP surface."""

import asyncio
import math
import sys
import types

import pytest

from conftest import load_liquid_handler
from pydantic import ValidationError

from blender_mcp.server.tools import _dispatch, liquid


def _run(function, **kwargs):
    return asyncio.run(function(ctx=None, **kwargs))


def test_liquid_patch_models_forbid_unrestricted_rna_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        liquid.LiquidSolverPatch(arbitrary_rna=1)  # pyright: ignore[reportCallIssue] - rejection is the assertion


def test_solver_patch_rejects_inverted_ranges() -> None:
    with pytest.raises(ValidationError, match="timesteps_min"):
        liquid.LiquidSolverPatch(timesteps_min=5, timesteps_max=2)
    with pytest.raises(ValidationError, match="particle_min"):
        liquid.LiquidSolverPatch(particle_min=9, particle_max=4)


def test_solver_tool_serializes_only_explicit_patch_fields(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )

    result = _run(
        liquid.configure_liquid_solver,
        domain_object_name="Domain",
        modifier_name="Liquid Domain",
        patch=liquid.LiquidSolverPatch(resolution_max=96, flip_ratio=0.9),
    )

    assert result["ok"] is True
    assert result["changed_objects"] == ["Domain"]
    assert calls == [
        (
            "configure_liquid_solver",
            {
                "domain_object_name": "Domain",
                "modifier_name": "Liquid Domain",
                "patch": {"resolution_max": 96, "flip_ratio": 0.9},
            },
        )
    ]


def test_flow_tool_forwards_typed_liquid_only_settings(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"changed_objects": ["Pour", "Domain"]},
    )

    result = _run(
        liquid.add_liquid_flow,
        object_name="Pour",
        domain_object_name="Domain",
        behavior="INFLOW",
        settings=liquid.LiquidFlowPatch(use_inflow=True, subframes=2, velocity_coord=(0.0, 0.0, -1.0)),
    )

    assert result["changed_objects"] == ["Pour", "Domain"]
    assert calls[0][1]["settings"] == {
        "use_inflow": True,
        "subframes": 2,
        "velocity_coord": (0.0, 0.0, -1.0),
    }


def test_read_only_liquid_tool_reports_no_changes(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: (
            calls.append((command, params)) or {"domains": [], "dependencies": [], "changed_objects": []}
        ),
    )

    result = _run(liquid.get_liquid_simulation_info, scene_name="Scene")

    assert result["changed_objects"] == []
    assert calls[0][0] == "get_liquid_simulation_info"


def test_all_twelve_phase_zero_commands_are_registered() -> None:
    names = {
        "get_liquid_simulation_info",
        "get_fluid_object_info",
        "create_liquid_domain",
        "fit_liquid_domain",
        "configure_liquid_solver",
        "add_liquid_flow",
        "configure_liquid_flow",
        "add_liquid_effector",
        "configure_liquid_effector",
        "configure_liquid_scope_and_boundaries",
        "estimate_liquid_resources",
        "validate_liquid_setup",
    }

    assert all(callable(getattr(liquid, name)) for name in names)
    assert set(liquid.mcp._tool_manager._tools) >= names


class _FakeRnaProperty:
    def __init__(self, *, prop_type="FLOAT", minimum=0.0, maximum=10.0, readonly=False) -> None:
        self.type = prop_type
        self.hard_min = minimum
        self.hard_max = maximum
        self.is_readonly = readonly
        self.is_array = False
        self.array_length = 0
        self.enum_items = []


class _FakeRnaProperties(dict):
    def __iter__(self):
        return iter(self.values())


def test_liquid_commands_dispatch_and_read_only_classification(monkeypatch) -> None:
    addon, _handler = load_liquid_handler(monkeypatch)
    server = addon.BlenderMCPServer()
    commands = server._build_command_handlers()

    assert "create_liquid_domain" in commands
    assert "validate_liquid_setup" in commands
    assert server.command_spec("get_liquid_simulation_info").read_only
    assert server.command_spec("estimate_liquid_resources").read_only
    assert not server.command_spec("create_liquid_domain").read_only
    assert server.command_spec("fit_liquid_domain").geometry


def test_resource_estimate_formula_is_explicit_and_conservative(monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    settings = types.SimpleNamespace(
        resolution_max=100,
        cache_frame_start=1,
        cache_frame_end=10,
        use_mesh=True,
        mesh_scale=2,
        particle_number=2,
        particle_min=8,
        particle_max=16,
        use_spray_particles=False,
        use_foam_particles=False,
        use_bubble_particles=False,
        use_tracer_particles=False,
        cache_type="REPLAY",
        cache_data_format="UNI",
    )
    obj = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    monkeypatch.setattr(handler.inspection_and_setup, "_get_domain", lambda *_args: (obj, modifier, settings))
    monkeypatch.setattr(
        handler.inspection_and_setup,
        "_world_bounds",
        lambda *_args, **_kwargs: {"dimensions": [4.0, 2.0, 1.0]},
    )

    result = handler.LiquidHandlersMixin().estimate_liquid_resources("Domain", "Liquid Domain")

    assert result["estimated_grid"]["cell_size"] == pytest.approx(0.04)
    assert result["estimated_grid"]["cells_xyz"] == [100, 50, 25]
    assert result["estimated_grid"]["base_cell_count"] == 125_000
    assert result["frame_count"] == 10
    assert result["relative_cost_index"] > result["estimated_grid"]["base_cell_count"] * 10


def _fake_flow(*, flow_behavior) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        flow_behavior=flow_behavior,
        use_inflow=False,
        bl_rna=types.SimpleNamespace(properties=_FakeRnaProperties(use_inflow=_FakeRnaProperty(prop_type="BOOLEAN"))),
    )


def _fake_flow_object(name: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(name=name, vertex_groups=types.SimpleNamespace(get=lambda _name: None))


def test_configure_flow_settings_accepts_use_inflow_for_outflow_behavior(monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    flow = _fake_flow(flow_behavior="OUTFLOW")

    changes = handler.inspection_and_setup.LiquidInspectionAndSetupHandlers._configure_flow_settings(
        _fake_flow_object("Drain"), flow, {"use_inflow": True}
    )

    assert changes["use_inflow"]["new"] is True
    assert flow.use_inflow is True


def test_configure_flow_settings_accepts_use_inflow_for_inflow_behavior(monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    flow = _fake_flow(flow_behavior="INFLOW")

    changes = handler.inspection_and_setup.LiquidInspectionAndSetupHandlers._configure_flow_settings(
        _fake_flow_object("Pour"), flow, {"use_inflow": False}
    )

    assert changes["use_inflow"]["new"] is False


def test_configure_flow_settings_rejects_use_inflow_for_geometry_behavior(monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    flow = _fake_flow(flow_behavior="GEOMETRY")

    with pytest.raises(ValueError, match="GEOMETRY"):
        handler.inspection_and_setup.LiquidInspectionAndSetupHandlers._configure_flow_settings(
            _fake_flow_object("Shape"), flow, {"use_inflow": True}
        )


def test_domain_bounds_reports_evaluated_bounds_only_once_baked(monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    seen_evaluated = []

    def fake_world_bounds(_obj, evaluated=True):
        seen_evaluated.append(evaluated)
        return {"dimensions": [1.0, 1.0, 1.0], "evaluated": evaluated}

    monkeypatch.setattr(handler.inspection_and_setup, "_world_bounds", fake_world_bounds)
    obj = types.SimpleNamespace(name="Domain")

    unbaked = types.SimpleNamespace(has_cache_baked_data=False, has_cache_baked_mesh=False)
    result = handler.inspection_and_setup._domain_bounds(obj, unbaked)

    assert result["base_domain_bounds"] == {"dimensions": [1.0, 1.0, 1.0], "evaluated": False}
    assert result["evaluated_liquid_bounds"] is None
    assert seen_evaluated == [False]

    baked = types.SimpleNamespace(has_cache_baked_data=True, has_cache_baked_mesh=False)
    result = handler.inspection_and_setup._domain_bounds(obj, baked)

    assert result["evaluated_liquid_bounds"] == {"dimensions": [1.0, 1.0, 1.0], "evaluated": True}


def test_baked_frame_ceiling_falls_back_to_cache_frame_end_when_never_paused(monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    settings = types.SimpleNamespace(
        use_mesh=True,
        cache_frame_end=250,
        cache_frame_pause_mesh=0,
        cache_frame_pause_data=0,
        cache_frame_pause_particles=0,
        cache_frame_pause_guide=0,
    )

    assert handler.simulation._baked_frame_ceiling(settings) == 250


def test_baked_frame_ceiling_uses_earliest_pause_frame(monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    settings = types.SimpleNamespace(
        use_mesh=True,
        cache_frame_end=250,
        cache_frame_pause_mesh=40,
        cache_frame_pause_data=60,
        cache_frame_pause_particles=0,
        cache_frame_pause_guide=0,
    )

    assert handler.simulation._baked_frame_ceiling(settings) == 40


def _fake_scene_for(object_name, *, frame_start=1, frame_end=250):
    return types.SimpleNamespace(
        name="Scene",
        frame_start=frame_start,
        frame_end=frame_end,
        frame_current=1,
        frame_subframe=0.0,
        objects={object_name: object()},
        view_layers=[types.SimpleNamespace(objects={object_name: object()})],
    )


def test_sample_liquid_simulation_rejects_frame_before_replay_cache_start(monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    obj = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = types.SimpleNamespace(cache_type="REPLAY", cache_frame_start=10, has_cache_baked_any=False)
    monkeypatch.setattr(handler.simulation, "_get_domain", lambda *_args: (obj, modifier, settings))
    sys.modules["bpy"].data.scenes = [_fake_scene_for("Domain")]

    with pytest.raises(ValueError, match="cache_frame_start"):
        handler.simulation.LiquidSimulationHandlers().sample_liquid_simulation("Domain", "Liquid Domain", [5])


def test_sample_liquid_simulation_rejects_preroll_over_budget(monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    obj = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = types.SimpleNamespace(cache_type="REPLAY", cache_frame_start=1, has_cache_baked_any=False)
    monkeypatch.setattr(handler.simulation, "_get_domain", lambda *_args: (obj, modifier, settings))
    sys.modules["bpy"].data.scenes = [_fake_scene_for("Domain", frame_end=500)]

    with pytest.raises(ValueError, match="max_preroll_frames"):
        handler.simulation.LiquidSimulationHandlers().sample_liquid_simulation(
            "Domain", "Liquid Domain", [300], max_preroll_frames=250
        )


def test_sample_liquid_simulation_rejects_frame_outside_modular_baked_range(monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    obj = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = types.SimpleNamespace(
        cache_type="MODULAR",
        cache_frame_start=1,
        cache_frame_end=100,
        has_cache_baked_any=True,
        use_mesh=True,
        cache_frame_pause_mesh=40,
        cache_frame_pause_data=0,
        cache_frame_pause_particles=0,
        cache_frame_pause_guide=0,
    )
    monkeypatch.setattr(handler.simulation, "_get_domain", lambda *_args: (obj, modifier, settings))
    sys.modules["bpy"].data.scenes = [_fake_scene_for("Domain")]

    with pytest.raises(ValueError, match="baked cache range"):
        handler.simulation.LiquidSimulationHandlers().sample_liquid_simulation("Domain", "Liquid Domain", [80])


def _polygon(*vertex_indices):
    return types.SimpleNamespace(vertices=list(vertex_indices))


def _edge(a, b):
    return types.SimpleNamespace(vertices=(a, b))


def test_topology_from_mesh_reports_closed_manifold_as_clean(monkeypatch) -> None:
    # A tetrahedron: every edge is shared by exactly two of its four triangular faces.
    _addon, handler = load_liquid_handler(monkeypatch)
    mesh = types.SimpleNamespace(
        polygons=[_polygon(0, 1, 2), _polygon(0, 1, 3), _polygon(0, 2, 3), _polygon(1, 2, 3)],
        edges=[_edge(0, 1), _edge(0, 2), _edge(0, 3), _edge(1, 2), _edge(1, 3), _edge(2, 3)],
    )

    assert handler.inspection_and_setup._topology_from_mesh(mesh) == {
        "boundary_edges": 0,
        "non_manifold_edges": 0,
    }


def test_topology_from_mesh_reports_open_surface_edges_as_boundary_and_nonmanifold(monkeypatch) -> None:
    # A single triangle: all three edges belong to only one face.
    _addon, handler = load_liquid_handler(monkeypatch)
    mesh = types.SimpleNamespace(
        polygons=[_polygon(0, 1, 2)],
        edges=[_edge(0, 1), _edge(0, 2), _edge(1, 2)],
    )

    assert handler.inspection_and_setup._topology_from_mesh(mesh) == {
        "boundary_edges": 3,
        "non_manifold_edges": 3,
    }


def test_evaluated_mesh_topology_reflects_modifier_stack_not_base_mesh(monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    sys.modules["bpy"].context.evaluated_depsgraph_get = lambda: "depsgraph"
    manifold_base_mesh = types.SimpleNamespace(
        polygons=[_polygon(0, 1, 2), _polygon(0, 1, 3), _polygon(0, 2, 3), _polygon(1, 2, 3)],
        edges=[_edge(0, 1), _edge(0, 2), _edge(0, 3), _edge(1, 2), _edge(1, 3), _edge(2, 3)],
    )
    open_evaluated_mesh = types.SimpleNamespace(
        polygons=[_polygon(0, 1, 2)],
        edges=[_edge(0, 1), _edge(0, 2), _edge(1, 2)],
    )
    to_mesh_clear_calls = []

    class FakeEvaluated:
        def to_mesh(self):
            return open_evaluated_mesh

        def to_mesh_clear(self):
            to_mesh_clear_calls.append("cleared")

    obj = types.SimpleNamespace(data=manifold_base_mesh, evaluated_get=lambda _depsgraph: FakeEvaluated())

    assert handler.inspection_and_setup._mesh_topology(obj) == {"boundary_edges": 0, "non_manifold_edges": 0}
    assert handler.inspection_and_setup._evaluated_mesh_topology(obj) == {
        "boundary_edges": 3,
        "non_manifold_edges": 3,
    }
    assert to_mesh_clear_calls == ["cleared"]


class _FakeVector3:
    """Minimal real 3D vector math - just enough of mathutils.Vector's contract for these tests."""

    def __init__(self, seq) -> None:
        self.x, self.y, self.z = seq

    def __add__(self, other):
        return _FakeVector3((self.x + other.x, self.y + other.y, self.z + other.z))

    def __iadd__(self, other):
        self.x += other.x
        self.y += other.y
        self.z += other.z
        return self

    def __sub__(self, other):
        return _FakeVector3((self.x - other.x, self.y - other.y, self.z - other.z))

    def __neg__(self):
        return _FakeVector3((-self.x, -self.y, -self.z))

    def __mul__(self, scalar):
        return _FakeVector3((self.x * scalar, self.y * scalar, self.z * scalar))

    __rmul__ = __mul__

    def __truediv__(self, scalar):
        return _FakeVector3((self.x / scalar, self.y / scalar, self.z / scalar))

    def __itruediv__(self, scalar):
        self.x /= scalar
        self.y /= scalar
        self.z /= scalar
        return self

    def dot(self, other):
        return self.x * other.x + self.y * other.y + self.z * other.z

    @property
    def length_squared(self):
        return self.dot(self)

    @property
    def length(self):
        return math.sqrt(self.length_squared)

    def normalize(self) -> None:
        length = self.length
        if length:
            self.x /= length
            self.y /= length
            self.z /= length

    def normalized(self):
        clone = _FakeVector3((self.x, self.y, self.z))
        clone.normalize()
        return clone

    def __iter__(self):
        return iter((self.x, self.y, self.z))


class _FakeUniformMatrix3:
    """A uniform-scale-only 3x3 linear map - enough to exercise the normal/direction math."""

    def __init__(self, factor) -> None:
        self.factor = factor

    def inverted_safe(self):
        return _FakeUniformMatrix3(1.0 / self.factor if self.factor else 0.0)

    def transposed(self):
        return self

    def __matmul__(self, vector):
        return _FakeVector3((vector.x * self.factor, vector.y * self.factor, vector.z * self.factor))


class _FakeUniformMatrix4:
    """A uniform-scale-plus-translation affine map standing in for obj.matrix_world."""

    def __init__(self, factor, translation=(0.0, 0.0, 0.0)) -> None:
        self.factor = factor
        self.translation = _FakeVector3(translation)

    def to_3x3(self):
        return _FakeUniformMatrix3(self.factor)

    def inverted_safe(self):
        inverse_factor = 1.0 / self.factor if self.factor else 0.0
        return _FakeUniformMatrix4(
            inverse_factor,
            (
                -self.translation.x * inverse_factor,
                -self.translation.y * inverse_factor,
                -self.translation.z * inverse_factor,
            ),
        )

    def __matmul__(self, vector):
        return _FakeVector3(
            (
                vector.x * self.factor + self.translation.x,
                vector.y * self.factor + self.translation.y,
                vector.z * self.factor + self.translation.z,
            )
        )


class _FakeBVH:
    """Stand-in for BVHTree.FromObject's local-space tree: returns a hit at a fixed local distance."""

    def __init__(self, local_thickness) -> None:
        self.local_thickness = local_thickness

    def ray_cast(self, origin, direction):
        return origin + direction * self.local_thickness, None, None, self.local_thickness


def _fake_evaluated(mesh, matrix_world):
    return types.SimpleNamespace(matrix_world=matrix_world, to_mesh=lambda: mesh, to_mesh_clear=lambda: None)


def test_flow_normal_orientation_flags_majority_inward_normals(monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    monkeypatch.setattr(handler.inspection_and_setup.mathutils, "Vector", _FakeVector3, raising=False)
    sys.modules["bpy"].context.evaluated_depsgraph_get = lambda: "depsgraph"
    identity = _FakeUniformMatrix4(1.0)

    def _make_object(normal):
        mesh = types.SimpleNamespace(
            vertices=[
                types.SimpleNamespace(co=_FakeVector3((-1.0, 0.0, 0.0))),
                types.SimpleNamespace(co=_FakeVector3((1.0, 0.0, 0.0))),
            ],
            polygons=[types.SimpleNamespace(center=_FakeVector3((1.0, 0.0, 0.0)), normal=_FakeVector3(normal))],
        )
        evaluated = _fake_evaluated(mesh, identity)
        return types.SimpleNamespace(evaluated_get=lambda _depsgraph: evaluated)

    outward = handler.inspection_and_setup._flow_normal_orientation(_make_object((1.0, 0.0, 0.0)))
    assert outward == {"faces_sampled": 1, "inward_faces": 0, "inward_fraction": 0.0}

    inward = handler.inspection_and_setup._flow_normal_orientation(_make_object((-1.0, 0.0, 0.0)))
    assert inward == {"faces_sampled": 1, "inward_faces": 1, "inward_fraction": 1.0}


def test_wall_thickness_samples_maps_local_space_bvh_hit_into_world_units(monkeypatch) -> None:
    # Regression for the verified-from-source fact that BVHTree.FromObject builds its tree in the
    # object's local space (no matrix_world multiplication) - rays must be cast in local space and
    # hits mapped back through matrix_world to report a correct world-space thickness.
    _addon, handler = load_liquid_handler(monkeypatch)
    monkeypatch.setattr(handler.inspection_and_setup.mathutils, "Vector", _FakeVector3, raising=False)
    sys.modules["bpy"].context.evaluated_depsgraph_get = lambda: "depsgraph"
    world_matrix = _FakeUniformMatrix4(2.0, (10.0, 0.0, 0.0))
    mesh = types.SimpleNamespace(
        polygons=[types.SimpleNamespace(center=_FakeVector3((0.0, 0.0, 0.0)), normal=_FakeVector3((1.0, 0.0, 0.0)))]
    )
    evaluated = _fake_evaluated(mesh, world_matrix)
    obj = types.SimpleNamespace(evaluated_get=lambda _depsgraph: evaluated)
    monkeypatch.setattr(
        handler.inspection_and_setup.mathutils.bvhtree.BVHTree,
        "FromObject",
        staticmethod(lambda *_args, **_kwargs: _FakeBVH(local_thickness=3.0)),
        raising=False,
    )

    thicknesses = handler.inspection_and_setup._wall_thickness_samples(obj, cell_size=1.0)

    assert thicknesses == [pytest.approx(6.0)]


class _FakeIdObject:
    """Minimal stand-in for a bpy ID object's custom-property mapping (obj[...]/.get())."""

    def __init__(self, name) -> None:
        self.name = name
        self._custom_properties = {}

    def get(self, key, default=None):
        return self._custom_properties.get(key, default)

    def __setitem__(self, key, value):
        self._custom_properties[key] = value


def test_ensure_liquid_uuid_is_idempotent(monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    obj = _FakeIdObject("Domain")

    first = handler.inspection_and_setup._ensure_liquid_uuid(obj)
    second = handler.inspection_and_setup._ensure_liquid_uuid(obj)

    assert first == second
    assert obj.get("blendermcp_liquid_uuid") == first


def test_manifest_round_trip_records_stage_and_domain_uuid(tmp_path, monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    directory = str(tmp_path)

    assert handler.simulation._read_manifest(directory) is None

    written = handler.simulation._write_manifest_entry(directory, "domain-uuid-1", "BAKE_DATA", "MODULAR", [1, 10])

    assert written["domain_uuid"] == "domain-uuid-1"
    assert written["stages"]["BAKE_DATA"]["cache_type"] == "MODULAR"
    assert written["stages"]["BAKE_DATA"]["frame_range"] == [1, 10]

    reread = handler.simulation._read_manifest(directory)
    assert reread == written

    updated = handler.simulation._write_manifest_entry(directory, "domain-uuid-1", "BAKE_MESH", "MODULAR", [1, 10])
    assert set(updated["stages"]) == {"BAKE_DATA", "BAKE_MESH"}


def _fake_domain_settings(**overrides):
    base = {
        "cache_directory": "",
        "cache_type": "MODULAR",
        "cache_frame_start": 1,
        "cache_frame_end": 10,
        "has_cache_baked_data": False,
        "has_cache_baked_guide": False,
        "has_cache_baked_mesh": False,
        "has_cache_baked_particles": False,
        "has_cache_baked_any": False,
        "is_cache_baking_any": False,
        "use_mesh": True,
        "use_spray_particles": False,
        "use_foam_particles": False,
        "use_bubble_particles": False,
        "use_tracer_particles": False,
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _finished(**_kwargs):
    return {"FINISHED"}


def _stub_fluid_operators():
    stub = _finished
    return types.SimpleNamespace(
        bake_data=stub,
        bake_guides=stub,
        bake_mesh=stub,
        bake_particles=stub,
        bake_all=stub,
        pause_bake=stub,
        free_data=stub,
        free_guides=stub,
        free_mesh=stub,
        free_particles=stub,
        free_all=stub,
    )


def test_manage_liquid_cache_status_reports_manifest_ownership(tmp_path, monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    sys.modules["bpy"].path = types.SimpleNamespace(abspath=lambda p: p)
    obj = _FakeIdObject("Domain")
    obj["blendermcp_liquid_uuid"] = "known-uuid"
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = _fake_domain_settings(cache_directory=str(tmp_path))
    monkeypatch.setattr(handler.simulation, "_get_domain", lambda *_args: (obj, modifier, settings))
    handler.simulation._write_manifest_entry(str(tmp_path), "known-uuid", "BAKE_DATA", "MODULAR", [1, 10])

    result = handler.simulation.LiquidSimulationHandlers().manage_liquid_cache("Domain", "Liquid Domain")

    assert result["domain_uuid"] == "known-uuid"
    assert result["manifest"]["domain_uuid"] == "known-uuid"
    assert result["directory_is_manifest_owned"] is True


def test_manage_liquid_cache_status_reports_foreign_directory_as_unowned(tmp_path, monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    sys.modules["bpy"].path = types.SimpleNamespace(abspath=lambda p: p)
    obj = _FakeIdObject("Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = _fake_domain_settings(cache_directory=str(tmp_path))
    monkeypatch.setattr(handler.simulation, "_get_domain", lambda *_args: (obj, modifier, settings))
    (tmp_path / "unrelated_render.png").write_bytes(b"not ours")

    result = handler.simulation.LiquidSimulationHandlers().manage_liquid_cache("Domain", "Liquid Domain")

    assert result["manifest"] is None
    assert result["directory_is_manifest_owned"] is False


class _EndClampingDomainSettings(types.SimpleNamespace):
    """Domain settings whose `cache_frame_end` Blender clamps to 100, silently, as RNA does."""

    def __setattr__(self, name, value) -> None:
        if name == "cache_frame_end":
            value = min(value, 100)
        super().__setattr__(name, value)


def test_manage_liquid_cache_refuses_and_undoes_a_frame_range_blender_did_not_keep(tmp_path, monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    sys.modules["bpy"].path = types.SimpleNamespace(abspath=lambda p: p)
    monkeypatch.setattr(
        sys.modules["bpy"].context, "view_layer", types.SimpleNamespace(update=lambda: None), raising=False
    )
    obj = _FakeIdObject("Domain")
    obj.update_tag = lambda **_kwargs: None
    modifier = types.SimpleNamespace(name="Liquid Domain")
    frame = _FakeRnaProperty(prop_type="INT", minimum=-1_048_574, maximum=1_048_574)
    settings = _EndClampingDomainSettings(
        **vars(_fake_domain_settings(cache_directory=str(tmp_path))),
        bl_rna=types.SimpleNamespace(properties=_FakeRnaProperties(cache_frame_start=frame, cache_frame_end=frame)),
    )
    monkeypatch.setattr(handler.simulation, "_get_domain", lambda *_args: (obj, modifier, settings))

    with pytest.raises(ValueError, match=r"\[1, 400\]"):
        handler.simulation.LiquidSimulationHandlers().manage_liquid_cache(
            "Domain", "Liquid Domain", action="CONFIGURE", patch={"cache_frame_start": 1, "cache_frame_end": 400}
        )

    assert (settings.cache_frame_start, settings.cache_frame_end) == (1, 10)


def test_manage_liquid_cache_bake_bypasses_overwrite_confirm_for_manifest_owned_directory(
    tmp_path, monkeypatch
) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    sys.modules["bpy"].path = types.SimpleNamespace(abspath=lambda p: p)
    sys.modules["bpy"].ops.fluid = _stub_fluid_operators()
    obj = _FakeIdObject("Domain")
    obj["blendermcp_liquid_uuid"] = "known-uuid"
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = _fake_domain_settings(cache_directory=str(tmp_path))
    monkeypatch.setattr(handler.simulation, "_get_domain", lambda *_args: (obj, modifier, settings))
    handler.simulation._write_manifest_entry(str(tmp_path), "known-uuid", "BAKE_DATA", "MODULAR", [1, 10])

    def fake_run_fluid_operator(_obj, _operator):
        settings.has_cache_baked_data = True
        return {"FINISHED"}

    monkeypatch.setattr(handler.simulation, "_run_fluid_operator", fake_run_fluid_operator)

    result = handler.simulation.LiquidSimulationHandlers().manage_liquid_cache(
        "Domain", "Liquid Domain", action="BAKE_DATA", confirm_bake=True
    )

    assert result["action"] == "BAKE_DATA"
    assert result["cache_after"]["stages"]["has_cache_baked_data"] is True
    manifest = handler.simulation._read_manifest(str(tmp_path))
    assert manifest["stages"]["BAKE_DATA"]["cache_type"] == "MODULAR"


def test_manage_liquid_cache_bake_requires_overwrite_confirm_for_foreign_directory(tmp_path, monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    sys.modules["bpy"].path = types.SimpleNamespace(abspath=lambda p: p)
    sys.modules["bpy"].ops.fluid = _stub_fluid_operators()
    obj = _FakeIdObject("Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = _fake_domain_settings(cache_directory=str(tmp_path))
    monkeypatch.setattr(handler.simulation, "_get_domain", lambda *_args: (obj, modifier, settings))
    (tmp_path / "leftover_from_another_tool.abc").write_bytes(b"foreign")

    def fail_run_fluid_operator(_obj, _operator):
        raise AssertionError("bake operator must not run when the overwrite gate should block first")

    monkeypatch.setattr(handler.simulation, "_run_fluid_operator", fail_run_fluid_operator)

    with pytest.raises(ValueError, match="confirm_external_overwrite=True"):
        handler.simulation.LiquidSimulationHandlers().manage_liquid_cache(
            "Domain", "Liquid Domain", action="BAKE_DATA", confirm_bake=True
        )


def test_has_gui_window_is_false_without_a_window_manager(monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)

    assert handler.simulation._has_gui_window() is False


def test_has_gui_window_is_false_in_background_mode(monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    sys.modules["bpy"].app.background = True
    sys.modules["bpy"].context.window_manager = types.SimpleNamespace(windows=[object()])

    assert handler.simulation._has_gui_window() is False


def test_start_fluid_bake_job_falls_back_to_synchronous_without_gui_window(monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    calls = []
    monkeypatch.setattr(handler.simulation, "_has_gui_window", lambda: False)
    monkeypatch.setattr(
        handler.simulation, "_run_fluid_operator", lambda obj, op: calls.append((obj, op)) or {"FINISHED"}
    )
    obj = object()
    operator = object()

    job = handler.simulation._start_fluid_bake_job(obj, operator)

    assert job == {"mode": "SYNCHRONOUS", "result": {"FINISHED"}}
    assert calls == [(obj, operator)]


def test_job_id_is_stable_and_scoped_to_stage_and_directory(monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)

    first = handler.simulation._job_id("uuid-1", "DATA", "/cache/one")
    second = handler.simulation._job_id("uuid-1", "DATA", "/cache/one")
    different_stage = handler.simulation._job_id("uuid-1", "MESH", "/cache/one")
    different_directory = handler.simulation._job_id("uuid-1", "DATA", "/cache/two")

    assert first == second
    assert first.startswith("uuid-1:DATA:")
    assert different_stage != first
    assert different_directory != first


def test_reconcile_pending_bake_manifest_writes_once_baked_flag_flips_true(tmp_path, monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    obj = _FakeIdObject("Domain")
    obj[handler.simulation._PENDING_BAKE_KEY] = {
        "domain_uuid": "known-uuid",
        "stage_action": "BAKE_DATA",
        "baked_flag": "has_cache_baked_data",
        "cache_type": "MODULAR",
        "frame_range": [1, 10],
    }
    settings = _fake_domain_settings(has_cache_baked_data=False)

    handler.simulation._reconcile_pending_bake_manifest(obj, settings, str(tmp_path))
    assert handler.simulation._read_manifest(str(tmp_path)) is None
    assert isinstance(obj.get(handler.simulation._PENDING_BAKE_KEY), dict)

    settings.has_cache_baked_data = True
    handler.simulation._reconcile_pending_bake_manifest(obj, settings, str(tmp_path))

    manifest = handler.simulation._read_manifest(str(tmp_path))
    assert manifest["stages"]["BAKE_DATA"]["cache_type"] == "MODULAR"
    assert obj.get(handler.simulation._PENDING_BAKE_KEY) == ""


def test_reconcile_pending_bake_manifest_is_noop_without_a_pending_marker(tmp_path, monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    obj = _FakeIdObject("Domain")
    settings = _fake_domain_settings(has_cache_baked_data=True)

    handler.simulation._reconcile_pending_bake_manifest(obj, settings, str(tmp_path))

    assert handler.simulation._read_manifest(str(tmp_path)) is None


def test_manage_liquid_cache_pause_rejects_without_modular_resumable(tmp_path, monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    sys.modules["bpy"].path = types.SimpleNamespace(abspath=lambda p: p)
    sys.modules["bpy"].ops.fluid = _stub_fluid_operators()
    obj = _FakeIdObject("Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = _fake_domain_settings(
        cache_directory=str(tmp_path), is_cache_baking_any=True, cache_type="MODULAR", cache_resumable=False
    )
    monkeypatch.setattr(handler.simulation, "_get_domain", lambda *_args: (obj, modifier, settings))

    with pytest.raises(ValueError, match="cache_resumable"):
        handler.simulation.LiquidSimulationHandlers().manage_liquid_cache("Domain", "Liquid Domain", action="PAUSE")


def test_manage_liquid_cache_pause_accepts_modular_resumable_while_baking(tmp_path, monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    sys.modules["bpy"].path = types.SimpleNamespace(abspath=lambda p: p)
    sys.modules["bpy"].ops.fluid = _stub_fluid_operators()
    obj = _FakeIdObject("Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = _fake_domain_settings(
        cache_directory=str(tmp_path), is_cache_baking_any=True, cache_type="MODULAR", cache_resumable=True
    )
    monkeypatch.setattr(handler.simulation, "_get_domain", lambda *_args: (obj, modifier, settings))
    monkeypatch.setattr(handler.simulation, "_run_fluid_operator", lambda _obj, _op: {"FINISHED"})

    result = handler.simulation.LiquidSimulationHandlers().manage_liquid_cache(
        "Domain", "Liquid Domain", action="PAUSE"
    )

    assert result["action"] == "PAUSE"


def _resumable_paused_settings(tmp_path, **overrides):
    defaults = {
        "cache_directory": str(tmp_path),
        "cache_type": "MODULAR",
        "cache_resumable": True,
        "is_cache_baking_any": False,
        "is_cache_baking_data": False,
        "cache_frame_pause_data": 5,
    }
    defaults.update(overrides)
    return _fake_domain_settings(**defaults)


def test_manage_liquid_cache_resume_rejects_when_not_modular_resumable(tmp_path, monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    sys.modules["bpy"].path = types.SimpleNamespace(abspath=lambda p: p)
    obj = _FakeIdObject("Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = _resumable_paused_settings(tmp_path, cache_resumable=False)
    monkeypatch.setattr(handler.simulation, "_get_domain", lambda *_args: (obj, modifier, settings))

    with pytest.raises(ValueError, match="RESUME requires cache_type=MODULAR"):
        handler.simulation.LiquidSimulationHandlers().manage_liquid_cache(
            "Domain", "Liquid Domain", action="RESUME", stage="DATA", confirm_bake=True
        )


def test_manage_liquid_cache_resume_rejects_when_already_baking(tmp_path, monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    sys.modules["bpy"].path = types.SimpleNamespace(abspath=lambda p: p)
    obj = _FakeIdObject("Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = _resumable_paused_settings(tmp_path, is_cache_baking_data=True)
    monkeypatch.setattr(handler.simulation, "_get_domain", lambda *_args: (obj, modifier, settings))

    with pytest.raises(ValueError, match="already baking"):
        handler.simulation.LiquidSimulationHandlers().manage_liquid_cache(
            "Domain", "Liquid Domain", action="RESUME", stage="DATA", confirm_bake=True
        )


def test_manage_liquid_cache_resume_rejects_when_already_fully_baked(tmp_path, monkeypatch) -> None:
    # Blender leaves cache_frame_pause_data set to the final frame after a normal completed
    # bake too, not just an interrupted pause, so a nonzero pause frame alone can't distinguish
    # "paused" from "finished".
    _addon, handler = load_liquid_handler(monkeypatch)
    sys.modules["bpy"].path = types.SimpleNamespace(abspath=lambda p: p)
    obj = _FakeIdObject("Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = _resumable_paused_settings(tmp_path, has_cache_baked_data=True)
    monkeypatch.setattr(handler.simulation, "_get_domain", lambda *_args: (obj, modifier, settings))

    with pytest.raises(ValueError, match="already fully baked"):
        handler.simulation.LiquidSimulationHandlers().manage_liquid_cache(
            "Domain", "Liquid Domain", action="RESUME", stage="DATA", confirm_bake=True
        )


def test_manage_liquid_cache_resume_rejects_when_no_paused_state(tmp_path, monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    sys.modules["bpy"].path = types.SimpleNamespace(abspath=lambda p: p)
    obj = _FakeIdObject("Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = _resumable_paused_settings(tmp_path, cache_frame_pause_data=0)
    monkeypatch.setattr(handler.simulation, "_get_domain", lambda *_args: (obj, modifier, settings))

    with pytest.raises(ValueError, match="no paused bake to resume"):
        handler.simulation.LiquidSimulationHandlers().manage_liquid_cache(
            "Domain", "Liquid Domain", action="RESUME", stage="DATA", confirm_bake=True
        )


def test_manage_liquid_cache_resume_dispatches_when_paused_state_exists(tmp_path, monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    sys.modules["bpy"].path = types.SimpleNamespace(abspath=lambda p: p)
    sys.modules["bpy"].ops.fluid = _stub_fluid_operators()
    obj = _FakeIdObject("Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = _resumable_paused_settings(tmp_path)
    monkeypatch.setattr(handler.simulation, "_get_domain", lambda *_args: (obj, modifier, settings))

    def fake_run_fluid_operator(_obj, _op):
        settings.has_cache_baked_data = True
        return {"FINISHED"}

    monkeypatch.setattr(handler.simulation, "_run_fluid_operator", fake_run_fluid_operator)

    result = handler.simulation.LiquidSimulationHandlers().manage_liquid_cache(
        "Domain", "Liquid Domain", action="RESUME", stage="DATA", confirm_bake=True
    )

    assert result["action"] == "RESUME"
    assert result["stage"] == "DATA"
    assert result["job_mode"] == "SYNCHRONOUS"
    assert any("background" in warning for warning in result["warnings"])
    manifest = handler.simulation._read_manifest(str(tmp_path))
    assert manifest["stages"]["BAKE_DATA"]["cache_type"] == "MODULAR"


def test_manage_liquid_cache_cancel_raises_while_stage_actively_baking(tmp_path, monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    sys.modules["bpy"].path = types.SimpleNamespace(abspath=lambda p: p)
    obj = _FakeIdObject("Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = _fake_domain_settings(cache_directory=str(tmp_path), is_cache_baking_data=True)
    monkeypatch.setattr(handler.simulation, "_get_domain", lambda *_args: (obj, modifier, settings))

    with pytest.raises(ValueError, match="no scripted abort"):
        handler.simulation.LiquidSimulationHandlers().manage_liquid_cache(
            "Domain", "Liquid Domain", action="CANCEL", stage="DATA"
        )


def test_manage_liquid_cache_cancel_degrades_to_free_when_not_baking(tmp_path, monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    sys.modules["bpy"].path = types.SimpleNamespace(abspath=lambda p: p)
    sys.modules["bpy"].ops.fluid = _stub_fluid_operators()
    obj = _FakeIdObject("Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = _fake_domain_settings(
        cache_directory=str(tmp_path), is_cache_baking_data=False, has_cache_baked_data=True
    )
    monkeypatch.setattr(handler.simulation, "_get_domain", lambda *_args: (obj, modifier, settings))

    def fake_run_fluid_operator(_obj, _op):
        settings.has_cache_baked_data = False
        return {"FINISHED"}

    monkeypatch.setattr(handler.simulation, "_run_fluid_operator", fake_run_fluid_operator)

    result = handler.simulation.LiquidSimulationHandlers().manage_liquid_cache(
        "Domain", "Liquid Domain", action="CANCEL", stage="DATA", confirm_free=True
    )

    assert result["action"] == "CANCEL"
    assert result["stage"] == "DATA"
    assert result["cache_after"]["stages"]["has_cache_baked_data"] is False


def test_manage_liquid_cache_start_bake_running_modal_stores_pending_marker_and_reconciles_later(
    tmp_path, monkeypatch
) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    sys.modules["bpy"].path = types.SimpleNamespace(abspath=lambda p: p)
    sys.modules["bpy"].ops.fluid = _stub_fluid_operators()
    obj = _FakeIdObject("Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = _fake_domain_settings(cache_directory=str(tmp_path))
    monkeypatch.setattr(handler.simulation, "_get_domain", lambda *_args: (obj, modifier, settings))
    monkeypatch.setattr(
        handler.simulation,
        "_start_fluid_bake_job",
        lambda _obj, _op: {"mode": "RUNNING_MODAL", "result": {"RUNNING_MODAL"}},
    )

    result = handler.simulation.LiquidSimulationHandlers().manage_liquid_cache(
        "Domain", "Liquid Domain", action="START_BAKE", stage="DATA", confirm_bake=True
    )

    assert result["job_mode"] == "RUNNING_MODAL"
    assert any("non-blocking" in warning for warning in result["warnings"])
    pending = obj.get(handler.simulation._PENDING_BAKE_KEY)
    assert pending is not None
    assert pending["domain_uuid"]
    assert pending["stage_action"] == "BAKE_DATA"
    assert pending["baked_flag"] == "has_cache_baked_data"
    assert handler.simulation._read_manifest(str(tmp_path)) is None

    settings.has_cache_baked_data = True
    status = handler.simulation.LiquidSimulationHandlers().manage_liquid_cache("Domain", "Liquid Domain")

    assert status["pending_bake"] is None
    manifest = handler.simulation._read_manifest(str(tmp_path))
    assert manifest["stages"]["BAKE_DATA"]["cache_type"] == "MODULAR"


def test_manage_liquid_cache_resume_pending_marker_counts_as_directory_ownership(tmp_path, monkeypatch) -> None:
    _addon, handler = load_liquid_handler(monkeypatch)
    sys.modules["bpy"].path = types.SimpleNamespace(abspath=lambda p: p)
    sys.modules["bpy"].ops.fluid = _stub_fluid_operators()
    obj = _FakeIdObject("Domain")
    obj["blendermcp_liquid_uuid"] = "known-uuid"
    obj[handler.simulation._PENDING_BAKE_KEY] = {
        "domain_uuid": "known-uuid",
        "stage_action": "BAKE_DATA",
        "baked_flag": "has_cache_baked_data",
        "cache_type": "MODULAR",
        "frame_range": [1, 10],
    }
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = _resumable_paused_settings(tmp_path)
    monkeypatch.setattr(handler.simulation, "_get_domain", lambda *_args: (obj, modifier, settings))
    (tmp_path / "leftover_from_previous_stage.uni").write_bytes(b"owned by us, no manifest yet")

    def fake_run_fluid_operator(_obj, _op):
        settings.has_cache_baked_data = True
        return {"FINISHED"}

    monkeypatch.setattr(handler.simulation, "_run_fluid_operator", fake_run_fluid_operator)

    result = handler.simulation.LiquidSimulationHandlers().manage_liquid_cache(
        "Domain", "Liquid Domain", action="RESUME", stage="DATA", confirm_bake=True
    )

    assert result["action"] == "RESUME"


class _LiquidCollection(_FakeIdObject):
    def __init__(self, name, objects=()) -> None:
        super().__init__(name)
        self.objects = types.SimpleNamespace(link=lambda _obj: None)
        self.children = types.SimpleNamespace(link=lambda _child: None)
        self.all_objects = list(objects)


class _Action:
    id_type = "ACTION"
    copies = 0

    def __init__(self, name) -> None:
        self.name = name

    def copy(self):
        _Action.copies += 1
        return _Action(f"{self.name}.{_Action.copies:03d}")


class _LiquidModifiers(list):
    def get(self, name):
        return next((modifier for modifier in self if modifier.name == name), None)


class _LiquidSceneObject(_FakeIdObject):
    """An object whose copy carries its own domain settings, compared by identity as Blender IDs are."""

    def __init__(self, name, modifiers=(), action=None) -> None:
        super().__init__(name)
        self.data = None
        self.modifiers = _LiquidModifiers(modifiers)
        self.animation_data = types.SimpleNamespace(action=action) if action else None

    def copy(self):
        modifiers = [
            types.SimpleNamespace(**{**vars(modifier), "domain_settings": types.SimpleNamespace(**vars(settings))})
            if (settings := getattr(modifier, "domain_settings", None)) is not None
            else modifier
            for modifier in self.modifiers
        ]
        duplicate = _LiquidSceneObject(self.name, modifiers)
        duplicate.animation_data = self.animation_data
        return duplicate

    def animation_data_create(self) -> None:
        self.animation_data = types.SimpleNamespace(action=None)


def test_a_liquid_variant_counts_members_by_role_and_names_its_domain(tmp_path, monkeypatch) -> None:
    """57 flows and effectors are counted by role; changed_objects names the variant domain alone."""
    addon, _handler = load_liquid_handler(monkeypatch)
    delivery = sys.modules[f"{addon.__name__}.handlers.liquid.delivery"]
    bpy = sys.modules["bpy"]
    bpy.path = types.SimpleNamespace(abspath=lambda path: path)
    bpy.context.view_layer = types.SimpleNamespace(update=lambda: None)
    motion = _Action("Pour")
    flows = [_LiquidSceneObject(f"Tap {index:02d}", action=motion if index < 12 else None) for index in range(50)]
    effectors = [_LiquidSceneObject(f"Rock {index}") for index in range(7)]
    settings = types.SimpleNamespace(
        cache_directory="/source-cache",
        fluid_group=_LiquidCollection("Flows", flows),
        effector_group=_LiquidCollection("Effectors", effectors),
        force_collection=None,
        guide_parent=None,
    )
    modifier = types.SimpleNamespace(name="Fluid", type="FLUID", fluid_type="DOMAIN", domain_settings=settings)
    modifier.show_viewport = modifier.show_render = True
    source = _LiquidSceneObject("Domain", [modifier])
    bpy.data.objects = types.SimpleNamespace(get=lambda _name: None)
    bpy.data.collections = types.SimpleNamespace(get=lambda _name: None, new=_LiquidCollection)
    bpy.data.scenes = [
        types.SimpleNamespace(objects={"Domain"}, collection=_LiquidCollection("Scene Collection")),
    ]
    monkeypatch.setattr(delivery, "_get_domain", lambda *_args: (source, modifier, settings))
    monkeypatch.setattr(delivery, "_check_unique_cache_path", lambda *_args: None)
    monkeypatch.setattr(delivery, "_register_owned_objects", lambda *_args: None)
    monkeypatch.setattr(delivery, "_cache_state", lambda _settings: {})

    reply = delivery.LiquidDeliveryHandlers().duplicate_liquid_setup_variant(
        "Domain", "Fluid", "Domain Preview", "Preview Setup", "Preview", str(tmp_path)
    )

    assert reply["changed_objects"] == ["Domain Preview"]
    assert reply["variant_objects"] == {
        "total": 58,
        "by_type": {"DOMAIN": 1, "EFFECTOR": 7, "FLOW": 50},
        "limit": 10,
        "returned_count": 10,
        "truncated": True,
        "names": ["Domain Preview", *[f"Tap {index:02d} Preview" for index in range(9)]],
    }
    assert reply["animation_actions"]["total"] == 12
    assert reply["animation_actions"]["by_type"] == {"ACTION": 12}
    assert reply["animation_actions"]["truncated"] is True
