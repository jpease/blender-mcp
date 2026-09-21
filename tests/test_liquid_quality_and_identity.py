"""Regression coverage for newly exposed liquid settings, quality profiles, and stable identities."""

import asyncio
import types

import pytest

from pydantic import ValidationError
from server.tools.liquid.test_tools import _load_liquid_handler

from blender_mcp.server.tools import _dispatch, liquid


def _run(function, **kwargs):
    return asyncio.run(function(ctx=None, **kwargs))


def _record_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"changes": {}},
    )
    return calls


class _TogglingFlipSettings:
    """Mimic Blender 5.2.1, where assigning use_flip_particles toggles instead of setting."""

    def __init__(self, *, enabled: bool = False) -> None:
        self._enabled = enabled
        self.writes: list[bool] = []

    @property
    def use_flip_particles(self) -> bool:
        return self._enabled

    @use_flip_particles.setter
    def use_flip_particles(self, value: bool) -> None:
        self.writes.append(bool(value))
        self._enabled = not self._enabled


# --- item 13: newly exposed solver, mesh, particle, and cache settings ---------------------------


def test_solver_patch_accepts_newly_exposed_domain_fields() -> None:
    patch = liquid.LiquidSolverPatch(delete_in_obstacle=True, use_flip_particles=False, sys_particle_maximum=500_000)

    assert patch.model_dump(exclude_none=True, exclude_unset=True) == {
        "delete_in_obstacle": True,
        "use_flip_particles": False,
        "sys_particle_maximum": 500_000,
    }


def test_solver_tool_forwards_newly_exposed_fields(monkeypatch) -> None:
    calls = _record_calls(monkeypatch)

    _run(
        liquid.configure_liquid_solver,
        domain_object_name="Domain",
        modifier_name="Liquid Domain",
        patch=liquid.LiquidSolverPatch(delete_in_obstacle=True, sys_particle_maximum=10),
    )

    assert calls[0][1]["patch"] == {"delete_in_obstacle": True, "sys_particle_maximum": 10}


def test_flip_particles_setter_is_idempotent_despite_blender_toggling(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    settings = _TogglingFlipSettings(enabled=False)

    first = handler.inspection_and_setup._set_flip_particles(settings, True)
    second = handler.inspection_and_setup._set_flip_particles(settings, True)

    assert first == {"old": False, "new": True}
    # The second call must not write at all; a second write would toggle the feature back off.
    assert second == {"old": True, "new": True}
    assert settings.writes == [True]
    assert settings.use_flip_particles is True


def test_flip_particles_field_is_excluded_from_the_generic_patch_allowlist(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)

    assert "use_flip_particles" not in handler.inspection_and_setup._DOMAIN_FIELDS
    assert handler.inspection_and_setup._FLIP_PARTICLES_FIELD == "use_flip_particles"


def test_solver_handler_reports_flip_particles_only_when_it_changed(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    module = handler.inspection_and_setup
    settings = _TogglingFlipSettings(enabled=True)
    settings.timesteps_min = 1
    settings.timesteps_max = 4
    settings.particle_min = 8
    settings.particle_max = 16
    obj = types.SimpleNamespace(name="Domain")
    modifier = types.SimpleNamespace(name="Liquid Domain")
    monkeypatch.setattr(module, "_get_domain", lambda *_args: (obj, modifier, settings))
    monkeypatch.setattr(module, "_reject_baked", lambda _settings: None)
    monkeypatch.setattr(module, "_patch_rna", lambda *_args: {})
    monkeypatch.setattr(module.bpy.context, "view_layer", types.SimpleNamespace(update=lambda: None), raising=False)
    handlers = handler.LiquidHandlersMixin()
    monkeypatch.setattr(handlers, "estimate_liquid_resources", lambda *_a: {"estimated_grid": {}})

    unchanged = handlers.configure_liquid_solver("Domain", "Liquid Domain", {"use_flip_particles": True})
    changed = handlers.configure_liquid_solver("Domain", "Liquid Domain", {"use_flip_particles": False})

    assert "use_flip_particles" not in unchanged["changes"]
    assert changed["changes"]["use_flip_particles"] == {"old": True, "new": False}
    assert settings.use_flip_particles is False


def test_mesh_patch_rejects_inverted_concave_thresholds() -> None:
    with pytest.raises(ValidationError, match="mesh_concave_lower"):
        liquid.LiquidMeshPatch(mesh_concave_lower=2.0, mesh_concave_upper=1.0)


def test_mesh_handler_rechecks_concave_thresholds_against_current_values(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    module = handler.mesh_and_materials
    settings = types.SimpleNamespace(mesh_concave_lower=0.4, mesh_concave_upper=3.5)
    monkeypatch.setattr(
        module,
        "_get_domain",
        lambda *_args: (types.SimpleNamespace(name="Domain"), types.SimpleNamespace(name="Liquid Domain"), settings),
    )
    monkeypatch.setattr(module, "_reject_baked", lambda _settings: None)

    with pytest.raises(ValueError, match="mesh_concave_lower must be <= mesh_concave_upper"):
        handler.LiquidHandlersMixin().configure_liquid_mesh("Domain", "Liquid Domain", {"mesh_concave_upper": 0.1})


def test_secondary_particle_patch_bounds_potential_radius_to_verified_rna_range() -> None:
    assert liquid.LiquidSecondaryParticlePatch(sndparticle_potential_radius=4).sndparticle_potential_radius == 4
    for invalid in (0, 5):
        with pytest.raises(ValidationError, match="sndparticle_potential_radius"):
            liquid.LiquidSecondaryParticlePatch(sndparticle_potential_radius=invalid)


def test_cache_patch_pins_openvdb_identifiers_verified_against_blender() -> None:
    patch = liquid.LiquidCachePatch(openvdb_cache_compress_type="ZIP", openvdb_data_depth="16")

    assert patch.openvdb_cache_compress_type == "ZIP"
    assert patch.openvdb_data_depth == "16"
    with pytest.raises(ValidationError):
        liquid.LiquidCachePatch(openvdb_data_depth=16)  # pyright: ignore[reportArgumentType]


def test_openvdb_fields_are_rejected_when_no_stage_uses_openvdb(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    module = handler.simulation
    settings = types.SimpleNamespace(cache_data_format="UNI", cache_mesh_format="BOBJECT", cache_particle_format="UNI")

    with pytest.raises(ValueError, match="only affect OpenVDB caches"):
        module._reject_unused_openvdb_fields(settings, {"openvdb_data_depth": "16"})

    # Supplying the format in the same patch is enough; it does not have to be set already.
    module._reject_unused_openvdb_fields(settings, {"openvdb_data_depth": "16", "cache_data_format": "OPENVDB"})
    settings.cache_mesh_format = "OPENVDB"
    module._reject_unused_openvdb_fields(settings, {"openvdb_cache_compress_type": "ZIP"})


def test_openvdb_data_depth_setter_checks_the_verified_identifiers(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    module = handler.simulation
    settings = types.SimpleNamespace(openvdb_data_depth=32)

    assert module._set_openvdb_data_depth(settings, "16") == {"old": "32", "new": "16"}
    with pytest.raises(ValueError, match=r"must be one of \['8', '16', '32'\]"):
        module._set_openvdb_data_depth(settings, "64")


def test_cache_state_normalizes_the_numeric_openvdb_depth_to_a_writable_identifier(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    module = handler.simulation
    settings = types.SimpleNamespace(openvdb_data_depth=16)
    monkeypatch.setattr(
        module,
        "mantaflow_cache_info",
        lambda *_args: {"configuration": {"openvdb_data_depth": 16}, "stages": {}},
    )
    monkeypatch.setattr(module, "_CACHE_FLAGS", ())

    assert module._cache_state(settings)["configuration"]["openvdb_data_depth"] == "16"


# --- item 14: particle-size emission is smoke-only ------------------------------------------------


def test_flow_patch_rejects_smoke_only_particle_size_fields() -> None:
    with pytest.raises(ValidationError, match="smoke/fire emission"):
        liquid.LiquidFlowPatch(use_particle_size=True)
    with pytest.raises(ValidationError, match="smoke/fire emission"):
        liquid.LiquidFlowPatch(particle_size=1.0)


def test_flow_handler_also_rejects_smoke_only_particle_size_fields(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    flow = types.SimpleNamespace(flow_behavior="INFLOW", use_inflow=False)
    obj = types.SimpleNamespace(name="Pour", vertex_groups=types.SimpleNamespace(get=lambda _name: None))

    with pytest.raises(ValueError, match="smoke/fire emission"):
        handler.inspection_and_setup.LiquidInspectionAndSetupHandlers._configure_flow_settings(
            obj, flow, {"particle_size": 1.0}
        )


# --- item 15: named quality profiles -------------------------------------------------------------


def test_every_quality_profile_validates_through_the_existing_patch_models() -> None:
    for name in liquid.QUALITY_PROFILES:
        solver_patch, mesh_patch = liquid.profile_patches(name)
        assert solver_patch and mesh_patch
        assert liquid.LiquidSolverPatch(**solver_patch)
        assert liquid.LiquidMeshPatch(**mesh_patch)


def test_quality_profiles_increase_resolution_monotonically() -> None:
    resolutions = [liquid.profile_patches(name)[0]["resolution_max"] for name in ("PREVIEW", "BALANCED", "FINAL")]

    assert resolutions == sorted(resolutions)
    assert len(set(resolutions)) == 3


def test_unknown_quality_profile_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown quality profile"):
        liquid.profile_patches("ULTRA")


def test_quality_profile_tool_forwards_both_resolved_patches(monkeypatch) -> None:
    calls = _record_calls(monkeypatch)

    result = _run(
        liquid.apply_liquid_quality_profile,
        domain_object_name="Domain",
        modifier_name="Liquid Domain",
        profile="FINAL",
    )

    assert result["changed_objects"] == ["Domain"]
    command, params = calls[0]
    assert command == "apply_liquid_quality_profile"
    assert params["profile"] == "FINAL"
    assert params["solver_patch"]["resolution_max"] == 192
    assert params["mesh_patch"]["use_speed_vectors"] is True


def test_quality_profile_tool_can_apply_one_section_only(monkeypatch) -> None:
    calls = _record_calls(monkeypatch)

    _run(
        liquid.apply_liquid_quality_profile,
        domain_object_name="Domain",
        modifier_name="Liquid Domain",
        profile="PREVIEW",
        apply_mesh=False,
    )

    assert calls[0][1]["mesh_patch"] is None
    assert calls[0][1]["solver_patch"]["resolution_max"] == 48


def test_quality_profile_tool_requires_at_least_one_section() -> None:
    with pytest.raises(ValueError, match="At least one of apply_solver or apply_mesh"):
        _run(
            liquid.apply_liquid_quality_profile,
            domain_object_name="Domain",
            modifier_name="Liquid Domain",
            apply_solver=False,
            apply_mesh=False,
        )


def test_quality_profile_command_is_registered_as_a_mutating_command(monkeypatch) -> None:
    addon, _handler = _load_liquid_handler(monkeypatch)
    server = addon.BlenderMCPServer()

    assert "apply_liquid_quality_profile" in server._build_command_handlers()
    assert not server.command_spec("apply_liquid_quality_profile").read_only


def test_quality_profile_handler_merges_both_sub_results(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    monkeypatch.setattr(
        handler.quality,
        "_get_domain",
        lambda *_args: (
            types.SimpleNamespace(name="Domain"),
            types.SimpleNamespace(name="Liquid Domain"),
            types.SimpleNamespace(),
        ),
    )
    handlers = handler.LiquidHandlersMixin()
    monkeypatch.setattr(
        handlers, "configure_liquid_solver", lambda *_a: {"changes": {"resolution_max": {"old": 64, "new": 192}}}
    )
    monkeypatch.setattr(
        handlers,
        "configure_liquid_mesh",
        lambda *_a: {"changes": {"mesh_scale": {"old": 1, "new": 2}}, "data_rebake_required": True},
    )

    result = handlers.apply_liquid_quality_profile(
        "Domain", "Liquid Domain", "FINAL", solver_patch={"resolution_max": 192}, mesh_patch={"mesh_scale": 2}
    )

    assert result["applied_sections"] == ["solver", "mesh"]
    assert set(result["changes"]) == {"resolution_max", "mesh_scale"}
    assert result["next_required_bake_stage"] == "DATA"
    assert result["retained_live_modifier"] is True


def test_quality_profile_handler_requires_a_patch(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)

    with pytest.raises(ValueError, match="at least a solver or a mesh patch"):
        handler.LiquidHandlersMixin().apply_liquid_quality_profile("Domain", "Liquid Domain", "FINAL")


# --- item 16: secondary-particle roles come from a recorded property, not a name ------------------


class _FakeIdBlock:
    """A named Blender ID block whose custom properties behave like Blender's mapping interface."""

    def __init__(self, name, properties=None) -> None:
        self.name = name
        self.store = dict(properties or {})

    def get(self, key, default=None):
        return self.store.get(key, default)

    def keys(self):
        return self.store.keys()

    def __contains__(self, key) -> bool:
        return key in self.store

    def __getitem__(self, key):
        return self.store[key]

    def __setitem__(self, key, value) -> None:
        self.store[key] = value

    def __delitem__(self, key) -> None:
        del self.store[key]


def _fake_particle_system(name, settings_name, recorded=None):
    return types.SimpleNamespace(name=name, settings=_FakeIdBlock(settings_name, recorded))


def test_particle_role_is_derived_from_blender_labels_only_until_recorded(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    module = handler.mesh_and_materials
    system = _fake_particle_system("Spray", "SprayParticleSettings")

    assert module._particle_role(system) == "SPRAY"

    obj = types.SimpleNamespace(particle_systems=[system])
    assert module._tag_particle_roles(obj) == [
        {"system": "Spray", "settings": "SprayParticleSettings", "role": "SPRAY"}
    ]
    # A later rename must not reclassify the system, because the role is now recorded.
    system.name = "Renamed By Artist"
    system.settings.name = "Also Renamed"
    assert module._particle_role(system) == "SPRAY"
    assert module._tag_particle_roles(obj) == []


def test_particle_role_is_recorded_on_settings_because_systems_reject_id_properties(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    module = handler.mesh_and_materials
    system = _fake_particle_system("Foam", "FoamParticleSettings")

    module._tag_particle_roles(types.SimpleNamespace(particle_systems=[system]))

    assert system.settings.store == {"blendermcp_particle_role": "FOAM"}
    assert not hasattr(system, "_store")


def test_flip_particle_system_is_not_classified_as_a_secondary_system(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    module = handler.mesh_and_materials

    assert module._particle_role(_fake_particle_system("Liquid", "LiquidParticleSettings")) == "UNKNOWN"


def test_combined_particle_labels_report_every_matching_role(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    module = handler.mesh_and_materials

    assert module._particle_role(_fake_particle_system("Spray + Foam", "SprayFoamSettings")) == "SPRAY+FOAM"


# --- item 17: stable UUIDs, roles, and manifest object registry -----------------------------------


def test_tagging_assigns_a_uuid_and_role_and_rolls_both_back(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    module = handler.inspection_and_setup
    obj = _FakeIdBlock("Pour")

    token = module._tag_liquid_object(obj, "FLOW")

    assert token["role"] == "FLOW"
    assert obj.store["blendermcp_liquid_uuid"] == token["uuid"]
    assert obj.store["blendermcp_liquid_role"] == "FLOW"

    module._untag_liquid_object(token)

    assert obj.store == {}


def test_retagging_keeps_the_existing_uuid_and_restores_the_previous_role(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    module = handler.inspection_and_setup
    obj = _FakeIdBlock("Proxy", {"blendermcp_liquid_uuid": "kept", "blendermcp_liquid_role": "EFFECTOR"})

    token = module._tag_liquid_object(obj, "EFFECTOR_PROXY")

    assert token["uuid"] == "kept"
    assert obj.store["blendermcp_liquid_role"] == "EFFECTOR_PROXY"

    module._untag_liquid_object(token)

    assert obj.store == {"blendermcp_liquid_uuid": "kept", "blendermcp_liquid_role": "EFFECTOR"}


def test_unknown_roles_are_rejected_before_anything_is_written(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    obj = _FakeIdBlock("Mystery")

    with pytest.raises(ValueError, match="Unknown liquid object role"):
        handler.inspection_and_setup._tag_liquid_object(obj, "SOMETHING_ELSE")

    assert obj.store == {}


def test_identity_reporting_never_assigns_an_identity(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    module = handler.inspection_and_setup
    untagged = _FakeIdBlock("Plain")

    assert module._liquid_object_identity(untagged) == {"uuid": None, "role": None}
    assert untagged.store == {}

    tagged = _FakeIdBlock("Domain", {"blendermcp_liquid_uuid": "abc", "blendermcp_liquid_role": "DOMAIN"})
    assert module._liquid_object_identity(tagged) == {"uuid": "abc", "role": "DOMAIN"}


def test_objects_resolve_by_uuid_and_ambiguity_is_an_error(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    module = handler.inspection_and_setup
    first = _FakeIdBlock("Domain", {"blendermcp_liquid_uuid": "abc"})
    second = _FakeIdBlock("Domain Copy", {"blendermcp_liquid_uuid": "abc"})
    third = _FakeIdBlock("Other", {"blendermcp_liquid_uuid": "def"})
    monkeypatch.setattr(module.bpy, "data", types.SimpleNamespace(objects=[first, third]), raising=False)

    assert module._find_object_by_liquid_uuid("abc") is first
    with pytest.raises(ValueError, match="No object carries liquid UUID"):
        module._find_object_by_liquid_uuid("missing")
    with pytest.raises(ValueError, match="must be a non-empty string"):
        module._find_object_by_liquid_uuid("")

    monkeypatch.setattr(module.bpy, "data", types.SimpleNamespace(objects=[first, second]), raising=False)
    with pytest.raises(ValueError, match="recorded on 2 objects"):
        module._find_object_by_liquid_uuid("abc")


def test_manifest_registry_is_keyed_by_uuid_so_renames_do_not_orphan_entries(monkeypatch, tmp_path) -> None:
    manifest = _load_liquid_handler(monkeypatch)[1].manifest
    directory = str(tmp_path)

    manifest.write_stage_entry(directory, "domain-uuid", "DATA", "REPLAY", (1, 10))
    manifest.register_objects(directory, "domain-uuid", [("flow-uuid", "Pour", "FLOW")])
    written = manifest.register_objects(directory, "domain-uuid", [("flow-uuid", "Pour Renamed", "FLOW")])

    assert written is not None
    assert written["objects"] == {
        "flow-uuid": {
            "name": "Pour Renamed",
            "role": "FLOW",
            "updated_at": written["objects"]["flow-uuid"]["updated_at"],
        }
    }
    # Registering objects must not disturb the bake-stage bookkeeping written by the cache tool.
    assert written["stages"]["DATA"]["frame_range"] == [1, 10]
    assert manifest.read_manifest(directory) == written


def test_manifest_registration_skips_entries_without_a_uuid(monkeypatch, tmp_path) -> None:
    manifest = _load_liquid_handler(monkeypatch)[1].manifest

    assert manifest.register_objects(str(tmp_path), "domain-uuid", [(None, "Pour", "FLOW")]) is None
    assert manifest.read_manifest(str(tmp_path)) is None


def test_manifest_registration_absorbs_an_unwritable_directory(monkeypatch, tmp_path) -> None:
    manifest = _load_liquid_handler(monkeypatch)[1].manifest

    assert manifest.register_objects(str(tmp_path / "absent"), "domain-uuid", [("u", "Pour", "FLOW")]) is None


def test_owned_object_registry_is_reported_from_the_cache_manifest(monkeypatch, tmp_path) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    module = handler.inspection_and_setup
    monkeypatch.setattr(module.bpy, "path", types.SimpleNamespace(abspath=lambda path: path), raising=False)
    handler.manifest.register_objects(str(tmp_path), "domain-uuid", [("flow-uuid", "Pour", "FLOW")])

    assert module._owned_object_registry(types.SimpleNamespace(cache_directory=None)) is None
    registry = module._owned_object_registry(types.SimpleNamespace(cache_directory=str(tmp_path)))

    assert registry is not None
    assert registry["flow-uuid"]["role"] == "FLOW"


def test_simulation_info_tool_forwards_a_domain_uuid(monkeypatch) -> None:
    calls = _record_calls(monkeypatch)

    _run(liquid.get_liquid_simulation_info, domain_uuid="abc")

    assert calls[0][1]["domain_uuid"] == "abc"
    assert calls[0][1]["domain_object_name"] is None


def test_simulation_info_rejects_a_uuid_that_disagrees_with_the_supplied_name(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)
    module = handler.inspection_and_setup
    monkeypatch.setattr(module, "_find_object_by_liquid_uuid", lambda _uuid: types.SimpleNamespace(name="Domain"))

    with pytest.raises(ValueError, match="resolves to 'Domain', not 'Renamed'"):
        handler.LiquidHandlersMixin().get_liquid_simulation_info(domain_object_name="Renamed", domain_uuid="abc")


def test_simulation_info_requires_at_least_one_selector(monkeypatch) -> None:
    _addon, handler = _load_liquid_handler(monkeypatch)

    with pytest.raises(ValueError, match="scene_name, domain_object_name, domain_uuid"):
        handler.LiquidHandlersMixin().get_liquid_simulation_info()
