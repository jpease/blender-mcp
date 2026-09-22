"""Regression coverage for the validate_scene preflight aggregator."""

import asyncio
import types

from typing import get_type_hints

import pytest

from conftest import StubFactory
from pydantic import TypeAdapter, ValidationError
from test_mutation_transaction import FakeCollection, _load_addon

from blender_mcp.server.tools import scene


def _fake_object(name, *, type="MESH", scale=(1.0, 1.0, 1.0), polygons=(), modifiers=(), animation_data=None):
    return types.SimpleNamespace(
        name=name,
        type=type,
        scale=scale,
        data=types.SimpleNamespace(polygons=list(polygons)),
        modifiers=list(modifiers),
        animation_data=animation_data,
    )


_NO_CAMERA = object()


def _fake_scene(
    name="Scene",
    *,
    objects=(),
    camera=_NO_CAMERA,
    frame_start=1,
    frame_end=250,
    rigidbody_world=None,
):
    return types.SimpleNamespace(
        name=name,
        objects=list(objects),
        camera=None if camera is _NO_CAMERA else camera,
        frame_start=frame_start,
        frame_end=frame_end,
        rigidbody_world=rigidbody_world,
    )


# ---------------------------------------------------------------------------
# Server-side tool: registration, dispatch, and schema validation.
# ---------------------------------------------------------------------------


def test_validate_scene_is_registered_and_read_only(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()

    assert "validate_scene" in scene.mcp._tool_manager._tools
    assert "validate_scene" in server._build_command_handlers()
    assert server.command_spec("validate_scene").read_only


def test_validate_scene_dispatches_scope_and_max_findings(stub_blender_connection: StubFactory) -> None:
    connection = stub_blender_connection({"findings": []})

    asyncio.run(scene.validate_scene(ctx=None, scene_name="Scene", scope=["cloth", "liquid"], max_findings=50))

    assert connection.calls[0] == (
        "validate_scene",
        {"scene_name": "Scene", "scope": ["cloth", "liquid"], "max_findings": 50},
    )


# validate_scene's own async body performs no argument validation itself (it
# forwards straight to the addon over the socket), so - like every Literal/Field
# constraint elsewhere in this repo (see test_scene_tools.py's TypeAdapter usage
# for ModifierSpecInput) - "invalid input is rejected" is a claim about the
# declared parameter schema, checked directly against that schema, not about
# calling the plain async function (that only gets enforced through FastMCP's
# real dispatch layer, which no test in this repo exercises).
_VALIDATE_SCENE_HINTS = get_type_hints(scene.validate_scene, include_extras=True)


def test_validate_scene_scope_schema_rejects_unknown_domain() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(_VALIDATE_SCENE_HINTS["scope"]).validate_python(["not_a_domain"])


def test_validate_scene_max_findings_schema_rejects_out_of_range() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(_VALIDATE_SCENE_HINTS["max_findings"]).validate_python(0)
    with pytest.raises(ValidationError):
        TypeAdapter(_VALIDATE_SCENE_HINTS["max_findings"]).validate_python(1001)


# ---------------------------------------------------------------------------
# Addon-side orchestration: which sub-validators run, deduping, truncation.
# ---------------------------------------------------------------------------


def _server_with_scene(monkeypatch, fake_scene):
    addon, bpy = _load_addon(monkeypatch, data={"scenes": FakeCollection()})
    bpy.data.scenes[fake_scene.name] = fake_scene
    server = addon.BlenderMCPServer()
    for method in (
        "validate_camera_rig",
        "validate_lighting_setup",
        "validate_pbr_asset",
        "validate_cloth_setup",
        "validate_liquid_setup",
    ):
        monkeypatch.setattr(server, method, lambda *_a, **_k: {"findings": []}, raising=False)
    return server


def test_validate_scene_rejects_unknown_scene(monkeypatch) -> None:
    server = _server_with_scene(monkeypatch, _fake_scene())

    with pytest.raises(ValueError, match="Scene not found"):
        server.validate_scene("Nope")


def test_validate_scene_rejects_empty_or_unknown_scope(monkeypatch) -> None:
    server = _server_with_scene(monkeypatch, _fake_scene())

    with pytest.raises(ValueError, match="at least one domain"):
        server.validate_scene("Scene", scope=[])
    with pytest.raises(ValueError, match="Unknown validation scope domain"):
        server.validate_scene("Scene", scope=["not_a_domain"])


def test_validate_scene_rejects_out_of_range_max_findings(monkeypatch) -> None:
    server = _server_with_scene(monkeypatch, _fake_scene())

    with pytest.raises(ValueError, match="max_findings must be in"):
        server.validate_scene("Scene", max_findings=0)
    with pytest.raises(ValueError, match="max_findings must be in"):
        server.validate_scene("Scene", max_findings=1001)


def test_validate_scene_only_calls_domains_in_scope(monkeypatch) -> None:
    server = _server_with_scene(monkeypatch, _fake_scene())
    calls = []
    for method in ("validate_camera_rig", "validate_lighting_setup", "validate_cloth_setup", "validate_liquid_setup"):
        monkeypatch.setattr(
            server, method, lambda *_a, name=method, **_k: calls.append(name) or {"findings": []}, raising=False
        )
    monkeypatch.setattr(
        server, "validate_pbr_asset", lambda *_a, **_k: calls.append("validate_pbr_asset") or {"findings": []}
    )

    result = server.validate_scene("Scene", scope=["cloth", "liquid"])

    assert calls == ["validate_cloth_setup", "validate_liquid_setup"]
    assert result["domains_checked"] == ["cloth", "liquid"]
    assert set(result["domain_summaries"]) == {"cloth", "liquid"}


def test_validate_scene_skips_pbr_call_when_scene_has_no_meshes(monkeypatch) -> None:
    server = _server_with_scene(monkeypatch, _fake_scene(objects=[_fake_object("Light1", type="LIGHT")]))
    pbr_calls = []
    monkeypatch.setattr(
        server, "validate_pbr_asset", lambda *_a, **_k: pbr_calls.append(_k) or {"findings": []}, raising=False
    )

    result = server.validate_scene("Scene", scope=["pbr"])

    assert pbr_calls == []
    assert result["domain_summaries"]["pbr"] == {"findings": 0, "truncated": False}


def test_validate_scene_calls_pbr_with_explicit_nonempty_mesh_names(monkeypatch) -> None:
    server = _server_with_scene(
        monkeypatch,
        _fake_scene(objects=[_fake_object("Mesh1"), _fake_object("Mesh2"), _fake_object("Light1", type="LIGHT")]),
    )
    pbr_calls = []
    monkeypatch.setattr(
        server,
        "validate_pbr_asset",
        lambda *_a, **kwargs: pbr_calls.append(kwargs) or {"findings": []},
        raising=False,
    )

    server.validate_scene("Scene", scope=["pbr"])

    assert pbr_calls == [{"object_names": ["Mesh1", "Mesh2"]}]


def test_validate_scene_suppresses_duplicate_missing_camera_when_in_scope(monkeypatch) -> None:
    server = _server_with_scene(monkeypatch, _fake_scene())
    monkeypatch.setattr(
        server,
        "validate_camera_rig",
        lambda *_a, **_k: {"findings": [{"severity": "WARNING", "code": "MISSING_SCENE_CAMERA", "object": "Scene"}]},
        raising=False,
    )

    result = server.validate_scene("Scene", scope=["camera", "scene"])

    scene_domain_codes = {f["code"] for f in result["findings"] if f["domain"] == "scene"}
    camera_domain_codes = {f["code"] for f in result["findings"] if f["domain"] == "camera"}
    assert "MISSING_CAMERA" not in scene_domain_codes
    assert "MISSING_SCENE_CAMERA" in camera_domain_codes


def test_validate_scene_reports_missing_camera_when_domain_excluded(monkeypatch) -> None:
    server = _server_with_scene(monkeypatch, _fake_scene())

    result = server.validate_scene("Scene", scope=["scene"])

    codes = {f["code"] for f in result["findings"]}
    assert "MISSING_CAMERA" in codes


def test_validate_scene_truncation_and_summary(monkeypatch) -> None:
    server = _server_with_scene(monkeypatch, _fake_scene())
    monkeypatch.setattr(
        server,
        "validate_cloth_setup",
        lambda *_a, **_k: {
            "findings": [{"severity": "ERROR", "code": f"C{i}", "object": f"Obj{i}", "message": "m"} for i in range(5)],
            "truncated": False,
        },
        raising=False,
    )

    result = server.validate_scene("Scene", scope=["cloth"], max_findings=2)

    assert len(result["findings"]) == 2
    assert result["total_findings"] == 5
    assert result["truncated"] is True
    assert result["summary"] == {"ERROR": 5}
    assert result["ready"] is False


def test_validate_scene_propagates_sub_validator_truncation_flag(monkeypatch) -> None:
    server = _server_with_scene(monkeypatch, _fake_scene())
    monkeypatch.setattr(
        server,
        "validate_liquid_setup",
        lambda *_a, **_k: {"findings": [], "truncated": True},
        raising=False,
    )

    result = server.validate_scene("Scene", scope=["liquid"], max_findings=500)

    assert result["truncated"] is True


# ---------------------------------------------------------------------------
# _scene_level_findings: pure-function coverage for checks that real
# Blender's own RNA constraints make impossible to construct live (an
# inverted frame range is clamped away by frame_start/frame_end's mutual
# range callbacks), plus the checks already covered end-to-end against real
# Blender in tests/blender_scene_validate_smoke.py.
# ---------------------------------------------------------------------------


def test_scene_level_findings_flags_invalid_frame_range(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    valid_camera = types.SimpleNamespace(name="Camera", type="CAMERA")
    findings = addon.handlers.scene._scene_level_findings(
        _fake_scene(frame_start=10, frame_end=1, camera=valid_camera, objects=[_fake_object("Light1", type="LIGHT")]),
        ("scene",),
    )

    by_code = {item["code"]: item for item in findings}
    assert by_code["INVALID_FRAME_RANGE"]["evidence"] == {"frame_start": 10, "frame_end": 1}


def test_scene_level_findings_flags_invalid_scene_camera_type(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    not_a_camera = types.SimpleNamespace(name="NotACamera", type="MESH")
    findings = addon.handlers.scene._scene_level_findings(
        _fake_scene(camera=not_a_camera, objects=[_fake_object("Light1", type="LIGHT")]),
        (),
    )

    by_code = {item["code"]: item for item in findings}
    assert by_code["INVALID_SCENE_CAMERA"]["subject"] == "NotACamera"


def test_scene_level_findings_flags_dirty_cloth_and_rigidbody_caches(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    dirty_modifier = types.SimpleNamespace(
        type="CLOTH", name="Cloth", point_cache=types.SimpleNamespace(is_outdated=True)
    )
    mesh = _fake_object("Cloth Mesh", modifiers=[dirty_modifier])
    fake_scene = _fake_scene(
        camera=object(),
        objects=[mesh, _fake_object("Light1", type="LIGHT")],
        rigidbody_world=types.SimpleNamespace(point_cache=types.SimpleNamespace(is_outdated=True)),
    )

    findings = addon.handlers.scene._scene_level_findings(fake_scene, ("camera", "lighting"))

    dirty = [item for item in findings if item["code"] == "DIRTY_SIMULATION_CACHE"]
    assert {item["subject"] for item in dirty} == {"Cloth Mesh", "Scene"}


def test_normalized_domain_finding_prefers_message_and_falls_back_to_evidence_string(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    normalize = addon.handlers.scene._normalized_domain_finding

    with_message = normalize(
        "lighting",
        {"severity": "ERROR", "code": "X", "resource": "Light1", "message": "msg", "evidence": {"a": 1}},
    )
    assert with_message == {
        "domain": "lighting",
        "severity": "ERROR",
        "code": "X",
        "subject": "Light1",
        "message": "msg",
        "evidence": {"a": 1},
        "remediation": None,
    }

    string_evidence_only = normalize(
        "pbr", {"severity": "WARNING", "code": "Y", "subject": "Mesh1", "evidence": "No UV layers"}
    )
    assert string_evidence_only["message"] == "No UV layers"
    assert string_evidence_only["evidence"] is None


# ---------------------------------------------------------------------------
# _persistence_findings: what bpy.data loses at save, which no scene walk sees.
# ---------------------------------------------------------------------------


def _fake_id(name, *, users=1, use_fake_user=False, library=None):
    return types.SimpleNamespace(name=name, users=users, use_fake_user=use_fake_user, library=library)


def test_persistence_findings_flag_unreferenced_and_fake_user_only_datablocks(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch, data={"materials": FakeCollection(), "actions": FakeCollection()})
    bpy.data.materials["Orphan"] = _fake_id("Orphan", users=0)
    bpy.data.materials["Used"] = _fake_id("Used", users=2)
    bpy.data.actions["Parked"] = _fake_id("Parked", users=1, use_fake_user=True)
    bpy.data.actions["Driving"] = _fake_id("Driving", users=2, use_fake_user=True)

    findings, truncated = addon.handlers.scene._persistence_findings(300)

    by_subject = {item["subject"]: item for item in findings}
    assert set(by_subject) == {"Orphan", "Parked"}
    assert by_subject["Orphan"]["code"] == "UNREFERENCED_DATABLOCK"
    assert by_subject["Orphan"]["severity"] == "WARNING"
    assert by_subject["Orphan"]["evidence"] == {"collection": "materials"}
    assert by_subject["Parked"]["code"] == "ACTION_KEPT_BY_FAKE_USER_ONLY"
    assert by_subject["Parked"]["severity"] == "INFO"
    assert truncated is False


def test_persistence_findings_ignore_linked_datablocks(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch, data={"meshes": FakeCollection()})
    bpy.data.meshes["CanonMesh"] = _fake_id("CanonMesh", users=0, library=object())

    assert addon.handlers.scene._persistence_findings(300) == ([], False)


def test_persistence_findings_report_truncation_past_max_findings(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch, data={"materials": FakeCollection()})
    for index in range(3):
        bpy.data.materials[f"Orphan{index}"] = _fake_id(f"Orphan{index}", users=0)

    findings, truncated = addon.handlers.scene._persistence_findings(2)

    assert len(findings) == 2
    assert truncated is True


def test_validate_scene_runs_the_persistence_domain_on_request(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch, data={"scenes": FakeCollection(), "materials": FakeCollection()})
    bpy.data.scenes["Scene"] = _fake_scene()
    bpy.data.materials["Orphan"] = _fake_id("Orphan", users=0)
    server = addon.BlenderMCPServer()

    result = server.validate_scene("Scene", scope=["persistence"])

    assert result["domains_checked"] == ["persistence"]
    assert result["domain_summaries"]["persistence"] == {"findings": 1, "truncated": False}
    assert result["findings"][0]["domain"] == "persistence"
    assert result["findings"][0]["code"] == "UNREFERENCED_DATABLOCK"


# ---------------------------------------------------------------------------
# The engine probe behind ENGINE_UNAVAILABLE. Blender 5.2.2 registers Cycles
# only as a bpy.types.RenderEngine subclass: RenderSettings.engine's static
# enum still reports ['BLENDER_EEVEE'] while Cycles renders, so reading that
# enum alone reported an ERROR against a scene that was rendering fine and
# made validate_scene's `ready` unreachable in a stock install.
# ---------------------------------------------------------------------------


def _install_engine_sources(bpy, *, enum_items, registered, keep):
    """Point bpy.types at one RNA enum and one RenderEngine class tree."""
    bpy.types.RenderSettings = types.SimpleNamespace(
        bl_rna=types.SimpleNamespace(
            properties={
                "engine": types.SimpleNamespace(
                    enum_items=[types.SimpleNamespace(identifier=key, name=label) for key, label in enum_items]
                )
            }
        )
    )
    base = type("RenderEngine", (), {})
    parent = base
    for identifier, label in registered:
        # __subclasses__ holds weak references, so the classes must outlive this call.
        parent = type(f"Engine_{identifier or 'base'}", (parent,), {"bl_idname": identifier, "bl_label": label})
        keep.append(parent)
    bpy.types.RenderEngine = base
    return base


def _lighting_shared(addon):
    return addon.handlers.lighting._shared


def test_engine_probe_reports_engines_registered_only_as_render_engine_subclasses(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch, data={})
    keep: list[type] = []
    # A base class with no bl_idname sits between RenderEngine and the real engine, exactly as
    # HydraRenderEngine does; it must be walked through and never reported as an engine.
    _install_engine_sources(
        bpy,
        enum_items=[("BLENDER_EEVEE", "EEVEE")],
        registered=[("", ""), ("CYCLES", "Cycles")],
        keep=keep,
    )

    assert _lighting_shared(addon).engine_identifiers() == {"BLENDER_EEVEE": "EEVEE", "CYCLES": "Cycles"}


def test_engine_probe_keeps_the_rna_label_when_both_sources_report_one_identifier(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch, data={})
    keep: list[type] = []
    _install_engine_sources(
        bpy,
        enum_items=[("BLENDER_EEVEE", "EEVEE")],
        registered=[("BLENDER_EEVEE", "Subclass Label")],
        keep=keep,
    )

    assert _lighting_shared(addon).engine_identifiers()["BLENDER_EEVEE"] == "EEVEE"


def test_engine_probe_always_includes_the_engine_a_scene_is_already_assigned(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch, data={})
    keep: list[type] = []
    _install_engine_sources(bpy, enum_items=[("BLENDER_EEVEE", "EEVEE")], registered=[], keep=keep)
    scene = types.SimpleNamespace(name="Scene", render=types.SimpleNamespace(engine="SOME_ADDON_ENGINE"))

    available = _lighting_shared(addon).engine_identifiers(scene)

    assert available == {"BLENDER_EEVEE": "EEVEE", "SOME_ADDON_ENGINE": "SOME_ADDON_ENGINE"}


def test_resolve_engine_finds_cycles_registered_only_as_a_subclass(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch, data={})
    keep: list[type] = []
    _install_engine_sources(bpy, enum_items=[("BLENDER_EEVEE", "EEVEE")], registered=[("CYCLES", "Cycles")], keep=keep)

    assert _lighting_shared(addon).resolve_engine("CYCLES") == "CYCLES"


def test_resolve_engine_still_refuses_cycles_when_nothing_registers_it(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch, data={})
    keep: list[type] = []
    _install_engine_sources(bpy, enum_items=[("BLENDER_EEVEE", "EEVEE")], registered=[], keep=keep)

    with pytest.raises(ValueError, match="Cycles is not registered"):
        _lighting_shared(addon).resolve_engine("CYCLES")


def test_resolve_engine_resolves_exactly_one_eevee_despite_a_rival_addon_engine(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch, data={})
    keep: list[type] = []
    _install_engine_sources(
        bpy,
        enum_items=[("BLENDER_EEVEE", "EEVEE")],
        registered=[("CYCLES", "Cycles"), ("THIRD_PARTY", "Turbo EEVEE Booster")],
        keep=keep,
    )

    assert _lighting_shared(addon).resolve_engine("EEVEE") == "BLENDER_EEVEE"


def test_resolve_engine_refuses_an_ambiguous_eevee_in_the_rna_enum(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch, data={})
    keep: list[type] = []
    _install_engine_sources(
        bpy,
        enum_items=[("BLENDER_EEVEE", "EEVEE"), ("BLENDER_EEVEE_NEXT", "EEVEE Next")],
        registered=[],
        keep=keep,
    )

    with pytest.raises(ValueError, match="exactly one EEVEE"):
        _lighting_shared(addon).resolve_engine("EEVEE")


# ---------------------------------------------------------------------------
# ZERO_AREA_UVS severity: an ERROR the session cannot act on is a permanent
# `ready: false`, so linked mesh data is reported as a WARNING that names the
# file the unwrap belongs in.
# ---------------------------------------------------------------------------


class _FakeUVLayers(list):
    @property
    def active(self):
        return self[0] if self else None


def _mesh_with_zero_area_uvs(monkeypatch, addon, *, library):
    obj = types.SimpleNamespace(
        name="Prop",
        library=None,
        material_slots=[],
        data=types.SimpleNamespace(library=library, uv_layers=_FakeUVLayers([types.SimpleNamespace(name="UVMap")])),
    )
    monkeypatch.setattr(addon.handlers.texture.validation, "mesh_object", lambda _name: obj, raising=True)
    server = addon.BlenderMCPServer()
    monkeypatch.setattr(
        server,
        "inspect_uv_layout",
        lambda *_a, **_k: {"uv_maps": [{"zero_area_faces": [3, 4], "overlap_pairs": []}]},
        raising=False,
    )
    result = server.validate_pbr_asset(object_names=["Prop"])
    return next(item for item in result["findings"] if item["code"] == "ZERO_AREA_UVS")


def test_zero_area_uvs_on_a_local_mesh_stays_an_error(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})

    finding = _mesh_with_zero_area_uvs(monkeypatch, addon, library=None)

    assert finding["severity"] == "ERROR"
    assert finding["remediation"] == "Unwrap the listed faces before texturing or baking."


def test_zero_area_uvs_on_a_linked_mesh_warns_and_names_the_library(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={})
    library = types.SimpleNamespace(name="props.blend", filepath="//libs/props.blend")

    finding = _mesh_with_zero_area_uvs(monkeypatch, addon, library=library)

    assert finding["severity"] == "WARNING"
    assert "//libs/props.blend" in finding["remediation"]
    assert finding["evidence"] == [3, 4]
