"""Regression coverage for the typed liquid workflow MCP surface (mesh/animation/guides/force fields/simulation/lifecycle/delivery)."""

import asyncio
import sys
import types

import pytest

from pydantic import ValidationError
from test_mutation_transaction import _load_addon

from blender_mcp.server.tools import liquid


def _run(function, **kwargs):
    return asyncio.run(function(ctx=None, **kwargs))


def _load_liquid_handler(monkeypatch):
    addon, _bpy = _load_addon(monkeypatch, data={})
    return addon, sys.modules[f"{addon.__name__}.handlers.liquid"]


def test_all_sixteen_workflow_commands_are_registered() -> None:
    names = {
        "configure_liquid_mesh",
        "configure_liquid_secondary_particles",
        "configure_liquid_diffusion",
        "animate_liquid_flow",
        "create_liquid_guide",
        "configure_liquid_force_fields",
        "create_liquid_material",
        "create_secondary_particle_render_setup",
        "sample_liquid_simulation",
        "manage_liquid_cache",
        "remove_fluid_components",
        "create_liquid_proxy_rig",
        "duplicate_liquid_setup_variant",
        "prepare_liquid_render_mesh",
        "export_liquid_simulation",
        "analyze_liquid_performance",
    }

    assert all(callable(getattr(liquid, name)) for name in names)
    assert set(liquid.mcp._tool_manager._tools) >= names


def test_workflow_models_reject_unknown_and_inconsistent_values() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        liquid.LiquidMeshPatch(smoke_only=True)  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValidationError, match="minimum must be <= maximum"):
        liquid.LiquidSecondaryParticlePatch(sndparticle_life_min=4, sndparticle_life_max=2)
    with pytest.raises(ValidationError, match="supplied together"):
        liquid.LiquidDiffusionConfig(dynamic_viscosity_pa_s=0.001)
    with pytest.raises(ValidationError, match="exactly one"):
        liquid.LiquidFlowKeyframe(frame=1, use_inflow=True, velocity_factor=1.0)
    with pytest.raises(ValidationError, match="extra_forbidden"):
        liquid.ProxyFlowSettings(smoke_density=1.0)  # pyright: ignore[reportCallIssue]
    with pytest.raises(ValidationError, match="subdivision_levels"):
        liquid.LiquidRenderFinish(subdivision_render_levels=2)
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        liquid.ProxyFlowSettings(surface_distance=-1.0)


def test_mesh_tool_serializes_only_supplied_patch(monkeypatch) -> None:
    monkeypatch.setattr(
        liquid,
        "_call",
        lambda command, params, changed_objects=None: {
            "command": command,
            "params": params,
            "changed_objects": changed_objects,
        },
    )

    result = _run(
        liquid.configure_liquid_mesh,
        domain_object_name="Domain",
        modifier_name="Liquid Domain",
        patch=liquid.LiquidMeshPatch(use_mesh=True, mesh_scale=3),
    )

    assert result["command"] == "configure_liquid_mesh"
    assert result["params"]["patch"] == {"use_mesh": True, "mesh_scale": 3}


def test_proxy_tool_serializes_explicit_typed_settings(monkeypatch) -> None:
    monkeypatch.setattr(
        liquid,
        "_call",
        lambda command, params, changed_objects=None: {
            "command": command,
            "params": params,
            "changed_objects": changed_objects,
        },
    )

    result = _run(
        liquid.create_liquid_proxy_rig,
        scene_name="Scene",
        source_object_name="Character",
        proxy_object_name="Character Liquid Proxy",
        domain_object_name="Domain",
        domain_modifier_name="Liquid Domain",
        role="EFFECTOR",
        geometry="CAPSULE",
        effector_settings=liquid.ProxyEffectorSettings(subframes=3),
        validation_frames=[1, 12],
    )

    assert result["command"] == "create_liquid_proxy_rig"
    assert result["params"]["effector_settings"] == {"subframes": 3}
    assert result["params"]["validation_frames"] == [1, 12]


def test_hollow_container_proxy_tool_serializes_new_params(monkeypatch) -> None:
    monkeypatch.setattr(
        liquid,
        "_call",
        lambda command, params, changed_objects=None: {
            "command": command,
            "params": params,
            "changed_objects": changed_objects,
        },
    )

    result = _run(
        liquid.create_liquid_proxy_rig,
        scene_name="Scene",
        source_object_name="Glass",
        proxy_object_name="Glass Liquid Proxy",
        domain_object_name="Domain",
        domain_modifier_name="Liquid Domain",
        role="EFFECTOR",
        geometry="HOLLOW_CONTAINER",
        wall_thickness=0.02,
        bottom_thickness=0.08,
        rim_axis="NEGATIVE_Z",
    )

    assert result["command"] == "create_liquid_proxy_rig"
    assert result["params"]["geometry"] == "HOLLOW_CONTAINER"
    assert result["params"]["wall_thickness"] == 0.02
    assert result["params"]["bottom_thickness"] == 0.08
    assert result["params"]["rim_axis"] == "NEGATIVE_Z"


def test_hollow_container_proxy_tool_defaults(monkeypatch) -> None:
    monkeypatch.setattr(
        liquid,
        "_call",
        lambda command, params, changed_objects=None: {"params": params},
    )

    result = _run(
        liquid.create_liquid_proxy_rig,
        scene_name="Scene",
        source_object_name="Character",
        proxy_object_name="Character Liquid Proxy",
        domain_object_name="Domain",
        domain_modifier_name="Liquid Domain",
        role="EFFECTOR",
    )

    assert result["params"]["wall_thickness"] == 0.05
    assert result["params"]["bottom_thickness"] is None
    assert result["params"]["rim_axis"] == "Z"


def test_hollow_container_validates_rim_axis_and_thickness(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    scene = types.SimpleNamespace(objects={"Source", "Domain"})
    source = types.SimpleNamespace(name="Source")
    domain = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = types.SimpleNamespace(resolution_max=32)
    monkeypatch.setattr(handler.delivery, "_get_scene", lambda _name: scene)
    monkeypatch.setattr(handler.delivery, "_get_object", lambda *_args, **_kwargs: source)
    monkeypatch.setattr(handler.delivery, "_get_domain", lambda *_args: (domain, modifier, settings))

    def make():
        return handler.LiquidHandlersMixin()

    with pytest.raises(ValueError, match="rim_axis must be one of"):
        make().create_liquid_proxy_rig(
            "Scene",
            "Source",
            "Proxy",
            "Domain",
            "Liquid Domain",
            "EFFECTOR",
            geometry="HOLLOW_CONTAINER",
            rim_axis="UP",
        )
    with pytest.raises(ValueError, match="wall_thickness must be a positive number"):
        make().create_liquid_proxy_rig(
            "Scene",
            "Source",
            "Proxy",
            "Domain",
            "Liquid Domain",
            "EFFECTOR",
            geometry="HOLLOW_CONTAINER",
            wall_thickness=0,
        )
    with pytest.raises(ValueError, match="wall_thickness must be a positive number"):
        make().create_liquid_proxy_rig(
            "Scene",
            "Source",
            "Proxy",
            "Domain",
            "Liquid Domain",
            "EFFECTOR",
            geometry="HOLLOW_CONTAINER",
            wall_thickness=True,
        )
    with pytest.raises(ValueError, match="bottom_thickness must be a positive number"):
        make().create_liquid_proxy_rig(
            "Scene",
            "Source",
            "Proxy",
            "Domain",
            "Liquid Domain",
            "EFFECTOR",
            geometry="HOLLOW_CONTAINER",
            bottom_thickness=-1.0,
        )


def test_hollow_container_geometry_detects_pour_opening_by_signed_rim_axis(monkeypatch) -> None:
    """The opening must be found at the extreme the rim_axis sign points toward, not always the max."""
    _addon, handler = _load_liquid_handler(monkeypatch)

    class FakeVector(list):
        def dot(self, other):
            return sum(a * b for a, b in zip(self, other, strict=True))

        def __neg__(self):
            return FakeVector(-value for value in self)

    class Seq(list):
        def index_update(self):
            pass

    class Vertex:
        def __init__(self, co, index):
            self.co = FakeVector(co)
            self.index = index

    class Face:
        def __init__(self, verts, normal):
            self.verts = verts
            self.normal = FakeVector(normal)

    class FakeBMesh:
        def __init__(self, verts, faces):
            self.verts = Seq(verts)
            self.faces = Seq(faces)
            self.freed = False

        def from_mesh(self, _mesh):
            pass

        def normal_update(self):
            pass

        def to_mesh(self, _mesh):
            pass

        def free(self):
            self.freed = True

    top = [Vertex((-1, -1, 1), 0), Vertex((1, 1, 1), 1)]
    bottom = [Vertex((-1, -1, -1), 2), Vertex((1, 1, -1), 3)]
    top_face = Face(top, (0, 0, 1))
    bottom_face = Face(bottom, (0, 0, -1))
    fake_bm = FakeBMesh(top + bottom, [top_face, bottom_face])

    deleted = []
    monkeypatch.setattr(handler.delivery.mathutils, "Vector", FakeVector, raising=False)
    monkeypatch.setattr(handler.delivery.bmesh, "new", lambda: fake_bm, raising=False)
    monkeypatch.setattr(
        handler.delivery.bmesh,
        "ops",
        types.SimpleNamespace(delete=lambda bm, geom, context: deleted.append((geom, context))),
        raising=False,
    )
    fake_mesh = types.SimpleNamespace(update=lambda: None)
    monkeypatch.setattr(handler.delivery, "_evaluated_mesh_copy", lambda _source, _name: fake_mesh)

    source = types.SimpleNamespace(name="Source")

    _mesh, bottom_indices = handler.delivery._hollow_container_geometry(source, "Proxy Mesh", "NEGATIVE_Z")

    assert deleted[0][0] == [bottom_face]
    assert bottom_indices == [0, 1]


def test_dynamic_viscosity_conversion_is_explicit(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)

    patch, source, evidence = handler._expand_viscosity_config(
        {"dynamic_viscosity_pa_s": 0.001, "density_kg_m3": 1000.0}
    )

    assert source == "SI_DYNAMIC_DENSITY"
    assert evidence["kinematic_viscosity_m2_s"] == pytest.approx(1e-6)
    assert patch["viscosity_base"] == pytest.approx(1.0)
    assert patch["viscosity_exponent"] == 6
    assert patch["use_diffusion"] is True


def test_particle_role_classification_never_guesses_unknown_systems(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    spray = type("System", (), {"name": "Surface Spray", "settings": type("Settings", (), {"name": "Output"})()})()
    unknown = type("System", (), {"name": "Particles", "settings": type("Settings", (), {"name": "Generic"})()})()

    assert handler._particle_role(spray) == "SPRAY"
    assert handler._particle_role(unknown) == "UNKNOWN"


def test_export_axis_validation_and_cost_helpers_are_explicit(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)

    with pytest.raises(ValueError, match="different axes"):
        handler._validate_axes("X", "NEGATIVE_X")

    assert handler._bounds_volume({"dimensions": [4.0, 2.0, 0.5]}) == pytest.approx(4.0)


def test_workflow_commands_dispatch_and_nested_targets_are_resolved(monkeypatch) -> None:
    addon, _handler = _load_liquid_handler(monkeypatch)
    domain = type("Object", (), {"name": "Domain"})()
    force = type("Object", (), {"name": "Wind"})()
    addon.bpy.data.objects = {"Domain": domain, "Wind": force}
    server = addon.BlenderMCPServer()

    commands = server._build_command_handlers()
    targets = server._resolve_targets(
        {
            "domain_object_name": "Domain",
            "fields": [{"object_name": "Wind"}],
            "targets": [{"object_name": "Domain", "modifier_name": "Liquid Domain"}],
        }
    )

    assert "configure_liquid_mesh" in commands
    assert "manage_liquid_cache" in commands
    assert [item.name for item in targets] == ["Domain", "Wind"]


def test_liquid_cache_status_dispatch_is_read_only(monkeypatch) -> None:
    addon, _handler = _load_liquid_handler(monkeypatch)
    server = addon.BlenderMCPServer()

    result = server._run_handler(
        "manage_liquid_cache",
        lambda **params: {"action": params["action"], "changed_objects": []},
        {"action": "STATUS"},
    )

    assert result == {"action": "STATUS", "changed_objects": []}


def test_dispatch_and_dynamic_read_only_classification(monkeypatch) -> None:
    addon, _handler = _load_liquid_handler(monkeypatch)
    domain = type("Object", (), {"name": "Domain"})()
    source = type("Object", (), {"name": "Source"})()
    addon.bpy.data.objects = {"Domain": domain, "Source": source}
    server = addon.BlenderMCPServer()

    assert "create_liquid_proxy_rig" in server._build_command_handlers()
    assert "analyze_liquid_performance" in server._build_command_handlers()
    targets = server._resolve_targets({"domain_object_name": "Domain", "source_object_name": "Source"})
    assert {item.name for item in targets} == {"Domain", "Source"}

    read_only_result = server._run_handler(
        "analyze_liquid_performance",
        lambda **_params: {"changed_objects": []},
        {"measure_replay_evaluation": False},
    )
    assert read_only_result == {"changed_objects": []}


def test_performance_analysis_rejects_unbounded_dependency_sets(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    obj = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = types.SimpleNamespace()
    monkeypatch.setattr(handler.delivery, "_get_domain", lambda *_args: (obj, modifier, settings))
    monkeypatch.setattr(
        handler.delivery,
        "_dependency_objects",
        lambda _settings: [(types.SimpleNamespace(name=f"Flow {index}"), "FLOW", None) for index in range(3)],
    )

    with pytest.raises(ValueError, match="max_dependency_objects=2"):
        handler.LiquidHandlersMixin().analyze_liquid_performance("Domain", "Liquid Domain", max_dependency_objects=2)


def test_alembic_particles_only_export_is_rejected_explicitly(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    scene = types.SimpleNamespace(objects={"Domain"})
    obj = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    settings = types.SimpleNamespace(
        use_mesh=True,
        has_cache_baked_mesh=True,
        has_cache_baked_particles=True,
    )
    monkeypatch.setattr(handler.delivery, "_get_scene", lambda _name: scene)
    monkeypatch.setattr(handler.delivery, "_get_domain", lambda *_args: (obj, modifier, settings))

    with pytest.raises(ValueError, match="particles-only"):
        handler.LiquidHandlersMixin().export_liquid_simulation(
            "Scene",
            "Domain",
            "Liquid Domain",
            "/tmp/liquid.abc",
            "ALEMBIC",
            1,
            2,
            include_surface=False,
            include_secondary_particles=True,
        )
