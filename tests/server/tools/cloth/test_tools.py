"""Server-boundary regression coverage for the typed cloth MCP surface."""

import asyncio
import sys
import types

import pytest

from conftest import load_addon
from pydantic import ValidationError

from blender_mcp.server.tools import _dispatch, cloth


def _run(function, **kwargs):
    return asyncio.run(function(ctx=None, **kwargs))


def test_cloth_patch_models_forbid_unrestricted_rna_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        cloth.ClothMaterialPatch(arbitrary_rna=1)  # pyright: ignore[reportCallIssue] - rejection is the assertion


@pytest.mark.parametrize("weight", [-0.01, 1.01])
def test_vertex_weight_assignment_rejects_out_of_range_weights(weight) -> None:
    with pytest.raises(ValidationError):
        cloth.VertexWeightAssignment(vertex_index=0, weight=weight)


def test_configure_material_serializes_only_explicit_patch_fields(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: (
            calls.append((command, params)) or {"mass": {"old": 0.3, "new": 0.42}, "changed_objects": ["Cape"]}
        ),
    )

    result = _run(
        cloth.configure_cloth,
        object_name="Cape",
        modifier_name="Cape Cloth",
        patch=cloth.ClothPatch(
            material=cloth.ClothMaterialSection(preset="COTTON", patch=cloth.ClothMaterialPatch(mass=0.42))
        ),
    )

    assert result["ok"] is True
    assert result["changed_objects"] == ["Cape"]
    assert result["data"] == {"material": {"mass": {"old": 0.3, "new": 0.42}}}
    assert calls == [
        (
            "configure_cloth_material",
            {
                "object_name": "Cape",
                "modifier_name": "Cape Cloth",
                "patch": {"mass": 0.42},
                "preset": "COTTON",
            },
        )
    ]


def test_configure_cloth_dispatches_multiple_sections_in_fixed_order_and_merges_metadata(monkeypatch) -> None:
    calls = []

    def fake_call(command, params=None):
        calls.append((command, params))
        if command == "configure_cloth_pinning":
            return {
                "pin_stiffness": {"old": 1.0, "new": 5.0},
                "warnings": ["pin group already existed"],
                "changed_objects": ["Cape"],
            }
        return {
            "uniform_pressure_force": {"old": 0.0, "new": 2.0},
            "warnings": ["pressure enabled"],
            "changed_objects": ["Cape"],
            "changed_resources": ["Cape Cloth"],
        }

    monkeypatch.setattr(_dispatch, "send_command", fake_call)

    result = _run(
        cloth.configure_cloth,
        object_name="Cape",
        modifier_name="Cape Cloth",
        patch=cloth.ClothPatch(
            pinning=cloth.ClothPinningSection(group_name="Pins", patch=cloth.ClothPinningPatch(pin_stiffness=5.0)),
            pressure=cloth.ClothPressurePatch(use_pressure=True, uniform_pressure_force=2.0),
        ),
    )

    assert [call[0] for call in calls] == ["configure_cloth_pinning", "configure_cloth_pressure"]
    assert result["ok"] is True
    assert result["data"] == {
        "pinning": {"pin_stiffness": {"old": 1.0, "new": 5.0}},
        "pressure": {"uniform_pressure_force": {"old": 0.0, "new": 2.0}},
    }
    assert result["warnings"] == ["pin group already existed", "pressure enabled"]
    assert result["changed_objects"] == ["Cape"]
    assert result["changed_resources"] == ["Cape Cloth"]


def test_empty_cloth_patch_is_rejected() -> None:
    with pytest.raises(ValidationError):
        cloth.ClothPatch()


def test_cloth_patch_forbids_unknown_section() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        cloth.ClothPatch(bogus_section={"foo": "bar"})  # pyright: ignore[reportCallIssue] - rejection is the assertion


def test_set_weights_serializes_typed_assignments(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )

    result = _run(
        cloth.set_cloth_vertex_weights,
        object_name="Cape",
        modifier_name="Cloth",
        role="PIN_MASS",
        group_name="Shoulders",
        assignments=[cloth.VertexWeightAssignment(vertex_index=3, weight=0.75)],
        operation="REPLACE",
    )

    assert result["changed_objects"] == ["Cape"]
    assert calls[0] == (
        "set_cloth_vertex_weights",
        {
            "object_name": "Cape",
            "modifier_name": "Cloth",
            "role": "PIN_MASS",
            "group_name": "Shoulders",
            "assignments": [{"vertex_index": 3, "weight": 0.75}],
            "operation": "REPLACE",
        },
    )


def test_read_only_cloth_tool_does_not_report_changes(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: (
            calls.append((command, params)) or {"objects": [], "dependencies": [], "changed_objects": []}
        ),
    )

    result = _run(cloth.get_cloth_simulation_info, scene_name="Scene")

    assert result["changed_objects"] == []
    assert calls[0][0] == "get_cloth_simulation_info"


def test_resource_estimate_forwards_bounded_object_page(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"estimates": []},
    )

    _run(
        cloth.estimate_cloth_resources,
        scene_name="Scene",
        cloth_object_names=["Cape"],
        object_limit=10,
        object_offset=20,
    )

    assert calls == [
        (
            "estimate_cloth_resources",
            {
                "scene_name": "Scene",
                "collection_name": None,
                "cloth_object_names": ["Cape"],
                "object_limit": 10,
                "object_offset": 20,
            },
        )
    ]


def test_handler_supplied_change_and_warning_metadata_wins(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: (
            calls.append((command, params))
            or {"changed_objects": ["Cape", "BodyProxy"], "warnings": ["cache invalidated"]}
        ),
    )

    result = _run(
        cloth.add_cloth_collider,
        object_name="BodyProxy",
        registrations=[
            cloth.ClothColliderRegistration(
                cloth_object_name="Cape",
                cloth_modifier_name="Cloth",
                collection_name="Cloth Colliders",
            )
        ],
    )

    assert result["changed_objects"] == ["Cape", "BodyProxy"]
    assert result["warnings"] == ["cache invalidated"]


def test_all_nineteen_public_commands_are_registered() -> None:
    names = {
        "get_cloth_simulation_info",
        "get_cloth_object_info",
        "add_cloth_simulation",
        "configure_cloth",
        "set_cloth_vertex_weights",
        "add_cloth_collider",
        "estimate_cloth_resources",
        "validate_cloth_setup",
        "animate_cloth_parameters",
        "create_cloth_attachment",
        "create_character_cloth_setup",
        "sample_cloth_simulation",
        "manage_cloth_cache",
        "remove_cloth_components",
        "create_cloth_proxy_rig",
        "duplicate_cloth_setup_variant",
        "prepare_cloth_render_surface",
        "export_cloth_simulation",
        "analyze_cloth_performance",
    }

    assert all(callable(getattr(cloth, name)) for name in names)
    assert set(cloth.mcp._tool_manager._tools) >= names


def _load_cloth_handler(monkeypatch):
    addon, _bpy = load_addon(monkeypatch, data={})
    return addon, sys.modules[f"{addon.__name__}.handlers.cloth"]


def test_handler_maps_every_public_weight_role(monkeypatch) -> None:
    _addon, handler = _load_cloth_handler(monkeypatch)

    assert set(handler._WEIGHT_ROLES) == {
        "PIN_MASS",
        "STRUCTURAL_STIFFNESS",
        "SHEAR_STIFFNESS",
        "BENDING_STIFFNESS",
        "SHRINK",
        "PRESSURE",
        "INTERNAL_SPRINGS",
        "OBJECT_COLLISION_EXCLUSION",
        "SELF_COLLISION_EXCLUSION",
    }
    assert handler._WEIGHT_ROLES["OBJECT_COLLISION_EXCLUSION"] == (
        "collision_settings",
        "vertex_group_object_collisions",
    )


def test_handler_uses_only_official_blender_51_material_presets(monkeypatch) -> None:
    _addon, handler = _load_cloth_handler(monkeypatch)

    assert set(handler._MATERIAL_PRESETS) == {"COTTON", "SILK", "DENIM", "LEATHER", "RUBBER"}
    assert handler._MATERIAL_PRESETS["COTTON"]["mass"] == pytest.approx(0.3)
    assert handler._MATERIAL_PRESETS["LEATHER"]["bending_stiffness"] == pytest.approx(150.0)


def test_default_in_memory_caches_have_no_shared_identity(monkeypatch) -> None:
    _addon, handler = _load_cloth_handler(monkeypatch)
    cache = types.SimpleNamespace(
        use_external=False,
        use_disk_cache=False,
        filepath="",
        name="",
        index=0,
    )

    assert handler._shared_cache_identity(cache) is None


def test_external_cache_identity_uses_resolved_path(monkeypatch) -> None:
    _addon, handler = _load_cloth_handler(monkeypatch)
    handler.bpy.path = types.SimpleNamespace(abspath=lambda path: f"/project/{path.removeprefix('//')}")
    cache = types.SimpleNamespace(
        use_external=True,
        use_disk_cache=True,
        filepath="//cache/cloth",
        name="Cape",
        index=2,
    )

    assert handler._shared_cache_identity(cache) == ("EXTERNAL", "/project/cache/cloth", "Cape", 2)


def test_external_cache_path_requires_the_directory_itself(monkeypatch) -> None:
    _addon, handler = _load_cloth_handler(monkeypatch)
    handler.bpy.path = types.SimpleNamespace(abspath=lambda _path: "/project/cache/cloth")
    monkeypatch.setattr(handler.os.path, "isdir", lambda path: path == "/project/cache/cloth")
    cache = types.SimpleNamespace(filepath="//cache/cloth")

    assert handler._external_cache_path_status(cache) == {
        "filepath": "//cache/cloth",
        "resolved": "/project/cache/cloth",
        "valid_directory": True,
    }


def test_keyed_motion_reports_largest_per_frame_channel_delta(monkeypatch) -> None:
    _addon, handler = _load_cloth_handler(monkeypatch)
    location_curve = types.SimpleNamespace(
        data_path="location",
        keyframe_points=[
            types.SimpleNamespace(co=(1.0, 0.0)),
            types.SimpleNamespace(co=(3.0, 8.0)),
        ],
    )
    rotation_curve = types.SimpleNamespace(
        data_path="rotation_euler",
        keyframe_points=[
            types.SimpleNamespace(co=(1.0, 0.0)),
            types.SimpleNamespace(co=(2.0, 100.0)),
        ],
    )
    obj = types.SimpleNamespace(
        animation_data=types.SimpleNamespace(action=types.SimpleNamespace(fcurves=[rotation_curve, location_curve]))
    )

    assert handler._max_keyed_location_delta(obj) == pytest.approx(4.0)


def test_baked_cache_refusal_names_every_blocking_modifier(monkeypatch) -> None:
    _addon, handler = _load_cloth_handler(monkeypatch)
    pairs = [
        (
            types.SimpleNamespace(name="Cape"),
            types.SimpleNamespace(name="Cloth", point_cache=types.SimpleNamespace(is_baked=True)),
        ),
        (
            types.SimpleNamespace(name="Skirt"),
            types.SimpleNamespace(name="Cloth", point_cache=types.SimpleNamespace(is_baked=True)),
        ),
    ]

    with pytest.raises(ValueError, match="Cape:Cloth, Skirt:Cloth"):
        handler._reject_baked(pairs)


def test_layered_action_uses_owner_slot_channelbag(monkeypatch) -> None:
    _addon, handler = _load_cloth_handler(monkeypatch)
    slot = object()
    curves = [types.SimpleNamespace(data_path="location")]
    strip = types.SimpleNamespace(type="KEYFRAME", channelbag=lambda requested: types.SimpleNamespace(fcurves=curves))
    action = types.SimpleNamespace(layers=[types.SimpleNamespace(strips=[strip])])
    animation = types.SimpleNamespace(action=action, action_slot=slot)

    assert handler._action_fcurves(animation) == curves


def test_high_resolution_collider_threshold(monkeypatch) -> None:
    _addon, handler = _load_cloth_handler(monkeypatch)
    cloth_obj = types.SimpleNamespace(data=types.SimpleNamespace(polygons=[None] * 3_000))
    collider = types.SimpleNamespace(data=types.SimpleNamespace(polygons=[None] * 12_001))

    assert handler._is_high_resolution_collider(cloth_obj, collider) is True


def test_build_modifier_is_treated_as_animated_topology(monkeypatch) -> None:
    _addon, handler = _load_cloth_handler(monkeypatch)

    assert handler._modifier_is_animated(types.SimpleNamespace(), types.SimpleNamespace(type="BUILD")) is True


def test_dispatch_advertises_cloth_and_marks_only_inspection_read_only(monkeypatch) -> None:
    addon, _bpy = load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()

    commands = server._build_command_handlers()

    assert "get_cloth_simulation_info" in commands
    assert "create_character_cloth_setup" in commands
    assert "manage_cloth_cache" in commands
    assert server.command_spec("validate_cloth_setup").read_only
    assert not server.command_spec("configure_cloth_solver").read_only


def test_sewing_dry_run_forwards_exact_pairs_without_reporting_changes(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: (
            calls.append((command, params)) or {"dry_run": True, "analysis": {"pairs": 1}, "changed_objects": []}
        ),
    )

    result = _run(
        cloth.configure_cloth,
        object_name="Garment",
        modifier_name="Cloth",
        patch=cloth.ClothPatch(
            sewing=cloth.ClothSewingSection(
                seam_pairs=[cloth.SewingPair(source_vertex=4, target_vertex=9)],
                sewing_force_max=25.0,
            )
        ),
    )

    assert result["changed_objects"] == []
    assert result["data"] == {"sewing": {"dry_run": True, "analysis": {"pairs": 1}}}
    assert calls == [
        (
            "configure_cloth_sewing",
            {
                "object_name": "Garment",
                "modifier_name": "Cloth",
                "seam_pairs": [{"source_vertex": 4, "target_vertex": 9}],
                "sewing_force_max": 25.0,
                "create_missing_edges": False,
                "dry_run": True,
                "max_pair_distance": None,
            },
        )
    ]


def test_animation_records_and_cache_index_are_strictly_serialized(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )

    _run(
        cloth.animate_cloth_parameters,
        object_name="Balloon",
        cloth_modifier_name="Cloth",
        keyframes=[
            cloth.ClothAnimationKeyframe(
                owner="CLOTH_SETTINGS",
                property_name="uniform_pressure_force",
                value=3.5,
                frame=12,
                interpolation="LINEAR",
            )
        ],
    )
    _run(
        cloth.manage_cloth_cache,
        object_name="Balloon",
        modifier_name="Cloth",
        action="CONFIGURE",
        patch=cloth.PointCachePatch(index=3, frame_start=1, frame_end=80),
    )

    assert calls[0][1]["keyframes"] == [
        {
            "owner": "CLOTH_SETTINGS",
            "property_name": "uniform_pressure_force",
            "value": 3.5,
            "frame": 12.0,
            "target_name": None,
            "array_index": -1,
            "interpolation": "LINEAR",
        }
    ]
    assert calls[1][1]["patch"] == {"frame_start": 1, "frame_end": 80, "index": 3}


def test_character_setup_forwards_explicit_modifier_names(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )

    _run(
        cloth.create_character_cloth_setup,
        garment_object_name="Coat",
        armature_object_name="Rig",
        body_collider_object_names=["Torso Proxy"],
        pin_group_name="Shoulders",
        collision_collection_name="Character Colliders",
        collider_modifier_name="Body Cloth Collision",
        subdivision_modifier_name="Render Subdivision",
        solidify_modifier_name="Render Thickness",
        rest_frame=10,
        cache_frame_start=10,
        cache_frame_end=120,
    )

    params = calls[0][1]
    assert params["collider_modifier_name"] == "Body Cloth Collision"
    assert params["subdivision_modifier_name"] == "Render Subdivision"
    assert params["solidify_modifier_name"] == "Render Thickness"
    assert params["rest_frame"] == 10


def test_prospective_external_cache_identity_uses_patch_values(monkeypatch) -> None:
    _addon, handler = _load_cloth_handler(monkeypatch)
    handler.bpy.path = types.SimpleNamespace(abspath=lambda path: f"/project/{path.removeprefix('//')}")
    cache = types.SimpleNamespace(
        use_external=False,
        filepath="",
        name="Old",
        index=0,
    )

    identity = handler._prospective_cache_identity(
        cache,
        {"use_external": True, "filepath": "//cache/coat", "name": "Coat", "index": 4},
    )

    assert identity == ("EXTERNAL", "/project/cache/coat", "Coat", 4)


def test_dynamic_read_only_classifies_sewing_dry_run_and_cache_inspection(monkeypatch) -> None:
    addon, _bpy = load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()
    calls = []

    server._resolve_targets = lambda _params: (_ for _ in ()).throw(AssertionError("must remain read-only"))
    sewing = server._run_handler(
        "configure_cloth_sewing",
        lambda **params: calls.append(params) or {"dry_run": True},
        {"dry_run": True},
    )
    cache = server._run_handler(
        "manage_cloth_cache",
        lambda **params: calls.append(params) or {"point_cache": {}},
        {"action": "INSPECT"},
    )

    assert sewing == {"dry_run": True}
    assert cache == {"point_cache": {}}
    assert calls == [{"dry_run": True}, {"action": "INSPECT"}]


def test_sewing_plan_reports_duplicate_loose_edges(monkeypatch) -> None:
    _addon, handler = _load_cloth_handler(monkeypatch)

    class Vector:
        def __init__(self, values):
            self.values = values

        def __sub__(self, other):
            return Vector(tuple(first - second for first, second in zip(self.values, other.values, strict=True)))

        @property
        def length(self):
            return sum(value * value for value in self.values) ** 0.5

    obj = types.SimpleNamespace(
        data=types.SimpleNamespace(
            vertices=[types.SimpleNamespace(co=Vector((index, 0, 0))) for index in range(6)],
            polygons=[types.SimpleNamespace(vertices=(0, 1, 2)), types.SimpleNamespace(vertices=(3, 4, 5))],
            edges=[types.SimpleNamespace(vertices=(1, 3), index=6), types.SimpleNamespace(vertices=(1, 3), index=7)],
        )
    )

    plan = handler._sewing_plan(obj, [{"source_vertex": 1, "target_vertex": 3}], None)

    assert plan["duplicate_requested_mesh_edges"] == 1
    assert plan["pairs"][0]["edge_indices"] == [6, 7]


def test_point_cache_operator_override_contains_exact_cache(monkeypatch) -> None:
    _addon, handler = _load_cloth_handler(monkeypatch)
    scene = object()
    view_layer = object()
    obj = object()
    cache = object()
    monkeypatch.setattr(handler._cache_helpers, "_scene_context_for_object", lambda _obj: (scene, view_layer))

    _scene, _view_layer, override = handler._point_cache_context(obj, cache)

    assert override["point_cache"] is cache
    assert override["object"] is obj
    assert override["scene"] is scene
    assert override["view_layer"] is view_layer


def test_owned_membership_lookup_is_exact(monkeypatch) -> None:
    _addon, handler = _load_cloth_handler(monkeypatch)
    obj = {
        "blendermcp_cloth_component_a": (
            '{"owned": true, "role": "collision_membership", "collection": "Character Colliders"}'
        ),
        "blendermcp_cloth_component_b": ('{"owned": true, "role": "collision_membership", "collection": "Other"}'),
    }

    record = handler._owned_membership_record(obj, "Character Colliders")

    assert record["collection"] == "Character Colliders"
    assert handler._owned_membership_record(obj, "Missing") is None


def test_p1_dispatch_targets_and_geometry_capture_are_declared(monkeypatch) -> None:
    addon, _bpy = load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()
    target_names = sys.modules[f"{addon.__name__}.server_core"].target_names

    assert target_names({"cloth_object_name": "Cape"}) == ["Cape"]
    assert target_names({"garment_object_name": "Skirt"}) == ["Skirt"]
    assert target_names({"body_collider_object_names": ["Torso", "Arm"]}) == ["Torso", "Arm"]
    assert server.command_spec("configure_cloth_sewing").geometry


def test_field_strength_is_the_only_direct_field_animation_control(monkeypatch) -> None:
    _addon, handler = _load_cloth_handler(monkeypatch)

    assert handler._ANIMATABLE_FIELDS["FIELD_SETTINGS"] == {"strength"}


def test_proxy_rig_serializes_explicit_topology_permission(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )

    _run(
        cloth.create_cloth_proxy_rig,
        render_object_name="Cape Render",
        proxy_object_name="Cape Proxy",
        proxy_source_policy="DECIMATE_RENDER",
        allow_topology_change=True,
        decimate_ratio=0.2,
        validation_frames=[1, 12, 24],
    )

    command, params = calls[0]
    assert command == "create_cloth_proxy_rig"
    assert params["allow_topology_change"] is True
    assert params["decimate_ratio"] == pytest.approx(0.2)
    assert params["validation_frames"] == [1, 12, 24]


def test_variant_requires_explicit_dependency_policies(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )

    _run(
        cloth.duplicate_cloth_setup_variant,
        source_object_name="Cape Preview",
        variant_object_name="Cape Final",
        variant_collection_name="Cape Final Setup",
        name_suffix=" Final",
        mesh_data_policy="COPY",
        material_policy="SHARE",
        animation_policy="SHARE",
        collider_policy="SHARE",
        force_field_policy="SHARE",
        render_surface_policy="DUPLICATE",
    )

    command, params = calls[0]
    assert command == "duplicate_cloth_setup_variant"
    assert params["mesh_data_policy"] == "COPY"
    assert params["render_surface_policy"] == "DUPLICATE"


class _Links(list):
    """A collection's `objects`/`children`: linkable, and `in` tests a name as Blender's does."""

    def link(self, item) -> None:
        self.append(item)

    def __contains__(self, name) -> bool:
        return any(item.name == name for item in self)


class _Collection:
    def __init__(self, name, objects=()) -> None:
        self.name = name
        self.children = _Links()
        self.objects = _Links(objects)
        self.all_objects = self.objects


class _Mesh:
    id_type = "MESH"

    def __init__(self, name) -> None:
        self.name = name

    def copy(self):
        return _Mesh(f"{self.name}.001")


class _Modifier:
    def __init__(self, modifier_type, collision_collection=None, effector_collection=None) -> None:
        self.name = modifier_type.title()
        self.type = modifier_type
        self.point_cache = types.SimpleNamespace(is_baked=False, is_baking=False)
        self.collision_settings = types.SimpleNamespace(collection=collision_collection)
        self.settings = types.SimpleNamespace(effector_weights=types.SimpleNamespace(collection=effector_collection))

    def copy(self):
        return _Modifier(self.type, self.collision_settings.collection, self.settings.effector_weights.collection)


class _Modifiers(list):
    def get(self, name):
        return next((modifier for modifier in self if modifier.name == name), None)


class _ClothSceneObject:
    """An object with custom properties, modifiers and `copy()`, compared by identity as Blender IDs are."""

    def __init__(self, name, object_type="MESH", modifiers=(), data=None, field=None) -> None:
        self.name = name
        self.type = object_type
        self.modifiers = _Modifiers(modifiers)
        self.data = data
        self.field = field
        self.animation_data = None
        self._properties = {}

    def copy(self):
        duplicate = _ClothSceneObject(
            self.name, self.type, [modifier.copy() for modifier in self.modifiers], self.data, self.field
        )
        duplicate._properties = dict(self._properties)
        return duplicate

    def keys(self):
        return self._properties.keys()

    def __setitem__(self, key, value) -> None:
        self._properties[key] = value

    def __delitem__(self, key) -> None:
        del self._properties[key]


def test_a_cloth_variant_counts_its_setup_and_names_the_variant(monkeypatch) -> None:
    """57 duplicated dependencies are counted by type and role; changed_objects names the variant alone."""
    colliders = [
        _ClothSceneObject(
            f"Collider {index:02d}", modifiers=[_Modifier("COLLISION")], data=_Mesh(f"Collider {index:02d}")
        )
        for index in range(50)
    ]
    effectors = [
        _ClothSceneObject(f"Wind {index}", "EMPTY", field=types.SimpleNamespace(type="WIND")) for index in range(7)
    ]
    cloth_modifier = _Modifier("CLOTH", _Collection("Colliders", colliders), _Collection("Effectors", effectors))
    source = _ClothSceneObject("Cape", modifiers=[cloth_modifier], data=_Mesh("Cape"))
    created_collections = []

    def new_collection(name):
        created_collections.append(_Collection(name))
        return created_collections[-1]

    existing = {obj.name: obj for obj in [source, *colliders, *effectors]}
    addon, _bpy = load_addon(
        monkeypatch,
        data={
            "objects": types.SimpleNamespace(get=existing.get),
            "collections": types.SimpleNamespace(get=lambda _name: None, new=new_collection),
        },
    )
    variants = sys.modules[f"{addon.__name__}.handlers.cloth.variants"]
    scene = types.SimpleNamespace(collection=_Collection("Scene Collection"), objects=list(existing.values()))
    monkeypatch.setattr(variants, "get_object", lambda _name, _types: source)
    monkeypatch.setattr(variants, "sync_from_editmode", lambda _obj: None)
    monkeypatch.setattr(
        variants, "_scene_context_for_object", lambda _obj: (scene, types.SimpleNamespace(update=lambda: None))
    )
    monkeypatch.setattr(variants, "_configure_independent_cache", lambda *_args: None)
    monkeypatch.setattr(variants, "_shared_cache_identity", lambda _cache: None)
    monkeypatch.setattr(variants, "_cache_info", lambda _cache: {})

    reply = variants.ClothVariantHandlers().duplicate_cloth_setup_variant(
        "Cape", "Cape Final", "Cape Final Setup", " Final", "COPY", "SHARE", "SHARE", "DUPLICATE", "DUPLICATE", "OMIT"
    )

    assert reply["changed_objects"] == ["Cape Final"]
    assert reply["changed_resources"] == [
        "Cape Final Setup",
        "Cape Final Setup Colliders",
        "Cape Final Setup Effectors",
    ]
    assert reply["dependencies"]["colliders"] == {
        "total": 50,
        "by_type": {"MESH": 50},
        "limit": 10,
        "returned_count": 10,
        "truncated": True,
        "names": [f"Collider {index:02d}" for index in range(10)],
    }
    assert reply["dependencies"]["force_fields"]["names"] == [f"Wind {index}" for index in range(7)]
    assert reply["dependencies"]["force_fields"]["truncated"] is False
    assert reply["ownership"] == {
        "total": 58,
        "by_type": {"cloth_variant": 1, "variant_collider": 50, "variant_effector": 7},
        "limit": 10,
        "returned_count": 10,
        "truncated": True,
        "names": ["Cape Final", *[f"Collider {index:02d} Final" for index in range(9)]],
    }
    assert reply["copied_datablocks"]["total"] == 51
    assert reply["copied_datablocks"]["by_type"] == {"MESH": 51}
    assert reply["copied_datablocks"]["returned_count"] == 10


def test_render_surface_serializes_typed_modifier_patches(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )

    _run(
        cloth.prepare_cloth_render_surface,
        object_name="Cape",
        cloth_modifier_name="Cloth",
        subdivision=cloth.ClothSubdivisionPatch(levels=1, render_levels=2),
        solidify=cloth.ClothSolidifyPatch(thickness=0.003, use_even_offset=True),
    )

    command, params = calls[0]
    assert command == "prepare_cloth_render_surface"
    assert params["subdivision"] == {"levels": 1, "render_levels": 2}
    assert params["solidify"] == {"thickness": 0.003, "use_even_offset": True}
    assert params["weighted_normal"] is None
    assert params["rest_frame"] == 1


def test_cloth_envelope_lifts_changed_resources(monkeypatch) -> None:
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: {
            "changed_objects": ["Cape Final"],
            "changed_resources": ["Cape Final Mesh", "Cape Final Material"],
        },
    )

    result = _run(
        cloth.analyze_cloth_performance,
        object_name="Cape",
        modifier_name="Cloth",
        frames=[1],
    )

    assert result["changed_objects"] == ["Cape Final"]
    assert result["changed_resources"] == ["Cape Final Mesh", "Cape Final Material"]


def test_export_forwards_complete_delivery_contract(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"filepath": str(tmp_path / "cape.usdc")},
    )

    _run(
        cloth.export_cloth_simulation,
        scene_name="Shot",
        filepath=str(tmp_path / "cape.usdc"),
        file_format="USD",
        object_names=["Cape"],
        frame_start=1,
        frame_end=24,
        frame_step=2,
        coordinate_space="WORLD",
        units="METERS",
        forward_axis="NEGATIVE_Z",
        up_axis="Y",
        topology_policy="REQUIRE_STABLE",
        evaluation_policy="REQUIRE_BAKED",
        overwrite=True,
    )

    command, params = calls[0]
    assert command == "export_cloth_simulation"
    assert params["scene_name"] == "Shot"
    assert params["object_names"] == ["Cape"]
    assert params["frame_step"] == 2
    assert params["overwrite"] is True


def test_phase_two_validation_helpers_reject_duplicate_frames_and_parallel_axes(monkeypatch) -> None:
    _addon, handler = _load_cloth_handler(monkeypatch)

    with pytest.raises(ValueError, match="duplicates"):
        handler._validate_frames([1, 1], maximum=4)
    with pytest.raises(ValueError, match="different axes"):
        handler._validate_distinct_axes("NEGATIVE_Z", "Z")


def test_phase_two_commands_are_dispatched_and_transaction_targets_are_declared(monkeypatch) -> None:
    addon, _bpy = load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()
    commands = server._build_command_handlers()

    assert "create_cloth_proxy_rig" in commands
    assert "duplicate_cloth_setup_variant" in commands
    assert "prepare_cloth_render_surface" in commands
    assert "export_cloth_simulation" in commands
    assert "analyze_cloth_performance" in commands
    assert sys.modules[f"{addon.__name__}.server_core"].target_names(
        {"render_object_name": "Cape_render", "source_object_name": "Cape"}
    ) == ["Cape_render", "Cape"]
