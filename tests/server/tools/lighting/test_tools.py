"""Server-boundary, packaging, and dispatch coverage for production lighting tools."""

import asyncio
import inspect
import sys
import types

from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import StubFactory, load_addon
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from blender_mcp.server.tools import _dispatch, lighting

LIGHTING_COMMANDS = {
    "list_lights",
    "inspect_light",
    "inspect_lighting_setup",
    "validate_lighting_setup",
    "create_light",
    "configure_light",
    "aim_light",
    "configure_light_linking",
    "create_studio_lighting",
    "configure_world_background",
    "configure_hdri_environment",
    "configure_procedural_sky",
    "configure_lighting_quality",
    "configure_color_management",
    "render_lighting_preview",
}
READ_ONLY_LIGHTING_COMMANDS = {
    "list_lights",
    "inspect_light",
    "inspect_lighting_setup",
    "validate_lighting_setup",
}


class StubConnection:
    """Record commands sent through a lighting tool."""

    def __init__(self, result=None) -> None:
        self.result = result or {"status": "ok"}
        self.calls = []

    def send_command(self, command, params):
        """Record and return one synthetic Blender response."""
        self.calls.append((command, params))
        return self.result


def run_tool(function, **kwargs):
    """Run one async FastMCP tool function without a request context."""
    return asyncio.run(function(ctx=None, **kwargs))


def test_all_lighting_commands_are_public_and_grouped() -> None:
    assert all(callable(getattr(lighting, name)) for name in LIGHTING_COMMANDS)
    assert {"construction.py", "environment.py", "inspection.py", "rendering.py"}.issubset(
        {path.name for path in Path(lighting.__file__).parent.iterdir()}
    )


def test_a_polyhaven_hdri_lights_the_scene_through_the_managed_environment(monkeypatch, tmp_path) -> None:
    """
    A Poly Haven HDRI reaches the world through `configure_hdri_environment`, like a user's own HDRI.

    That handler replaces only the graph it manages, so an import cannot overwrite a world the user
    authored. The fake `bpy.data` has no `worlds`, so an import that reached for one directly fails.
    """
    addon, bpy = load_addon(monkeypatch, data={})
    polyhaven = sys.modules[f"{addon.__name__}.handlers.polyhaven"]
    files = {"hdri": {"1k": {"hdr": {"url": "https://dl.polyhaven.org/x/sky_1k.hdr"}}}}
    monkeypatch.setattr(polyhaven, "get_json", lambda *_args, **_kwargs: files)
    monkeypatch.setattr(polyhaven, "download_file", lambda _url, path, **_kwargs: Path(path).write_bytes(b"hdr"))
    bpy.utils = types.SimpleNamespace(user_resource=lambda *_args, **_kwargs: str(tmp_path))
    bpy.context.scene = types.SimpleNamespace(name="Shot", world=types.SimpleNamespace(name="Authored"))
    server = addon.BlenderMCPServer()
    calls = []

    def configure_hdri_environment(**kwargs):
        calls.append(kwargs)
        return {"image": "sky_1k.hdr", "image_path": kwargs["image_path"], "world": "Authored"}

    monkeypatch.setattr(server, "configure_hdri_environment", configure_hdri_environment)

    result = server.import_polyhaven_asset("sky", "hdris")

    assert result.get("success") is True, result
    [call] = calls
    assert (call["scene_name"], call["replacement_policy"], call["create_world"]) == ("Shot", "REPLACE_MANAGED", False)
    assert result["image_path"] == call["image_path"]
    assert Path(call["image_path"]).read_bytes() == b"hdr", "the world must point at the downloaded image"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["sky_1k.hdr"], "the partial download was left behind"


def test_light_models_reject_unknown_fields_and_invalid_ranges() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        lighting.LightPatch(arbitrary_rna=1)  # pyright: ignore[reportCallIssue]
    with pytest.raises(ValidationError):
        lighting.LightSettings(color=(1.2, 0.5, 0.5))
    with pytest.raises(ValidationError):
        lighting.ProceduralSkySettings(sun_size=0)
    with pytest.raises(ValidationError):
        lighting.ProceduralSkySettings(sun_size=2)
    with pytest.raises(ValidationError):
        lighting.ProceduralSkySettings(sun_intensity=1001)
    with pytest.raises(ValidationError):
        lighting.ProceduralSkySettings(altitude=100001)


def test_preview_dispatch_is_async_and_paths_are_distinct() -> None:
    assert inspect.iscoroutinefunction(lighting.render_lighting_preview)
    with pytest.raises(ToolError, match="distinct output path"):
        run_tool(
            lighting.render_lighting_preview,
            scene_name="Scene",
            camera_name="Camera",
            frame=1,
            target_engine="BOTH",
            cycles_output_path="/tmp/shared.png",
            eevee_output_path="/tmp/shared.png",
        )


def test_configure_light_sends_only_explicit_patch_fields(monkeypatch) -> None:
    connection = StubConnection({"object": "Key", "changed_resources": ["Key Light"]})
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    result = run_tool(
        lighting.configure_light,
        light_name="Key",
        patch=lighting.LightPatch(energy=750, use_shadow=False),
    )

    assert connection.calls == [
        ("configure_light", {"light_name": "Key", "patch": {"energy": 750.0, "use_shadow": False}})
    ]
    assert result["changed_objects"] == ["Key"]
    assert result["changed_resources"] == ["Key Light"]


def test_aim_light_rejects_ambiguous_target_before_dispatch(monkeypatch) -> None:
    connection = StubConnection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    with pytest.raises(ToolError, match="exactly one"):
        run_tool(
            lighting.aim_light,
            scene_name="Scene",
            light_name="Key",
            target_point=(0, 0, 0),
            target_object_name="Subject",
        )

    assert connection.calls == []


def test_create_studio_lighting_dispatches_rig_then_preview(monkeypatch) -> None:
    connection = StubConnection({"lights": [], "changed_objects": []})
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    result = run_tool(
        lighting.create_studio_lighting,
        scene_name="Scene",
        target_object_name="Product",
        camera_name="Camera",
        frame=1,
        preview_output_path="/tmp/studio_preview.png",
    )

    assert [call[0] for call in connection.calls] == ["create_studio_lighting", "render_lighting_preview"]
    rig_command, preview_command = connection.calls
    assert rig_command[1] == {
        "scene_name": "Scene",
        "target_object_name": "Product",
        "camera_name": "Camera",
        "mood": "SOFT",
        "key_ratio": None,
        "rig_name": None,
        "collection_name": "Studio Lighting",
    }
    assert preview_command[1]["camera_name"] == "Camera"
    assert preview_command[1]["target_engine"] == "EEVEE"
    assert preview_command[1]["output_paths"] == {"EEVEE": "/tmp/studio_preview.png"}
    assert [item["data"] for item in result] == [{"lights": []}, {"lights": []}]


@pytest.mark.parametrize(
    "preview",
    [
        pytest.param({"preview_output_path": "studio_preview.png"}, id="relative-path"),
        pytest.param({"preview_output_path": "/tmp/studio_preview.jpg"}, id="not-png"),
        pytest.param({"preview_engine": "CYCLES", "preview_samples": 65}, id="unconfirmed-cycles-samples"),
        pytest.param({"frame": 1_048_575}, id="frame-beyond-blender"),
    ],
)
def test_create_studio_lighting_refuses_a_bad_preview_before_building_the_rig(
    stub_blender_connection: StubFactory, preview: dict
) -> None:
    """
    A preview that was always going to be refused must not leave three lights behind first.

    The rig's handler refuses member names that already exist, so a rig built before its preview
    was refused blocked the corrected retry until the lights were deleted by hand.
    """
    connection = stub_blender_connection({"lights": []})
    request = {"scene_name": "Scene", "target_object_name": "Product", "camera_name": "Camera", "frame": 1}

    with pytest.raises(ToolError):
        run_tool(lighting.create_studio_lighting, **{**request, **preview})

    assert connection.calls == []


def test_lighting_quality_expands_strict_agent_payload(monkeypatch) -> None:
    connection = StubConnection({"changed_resources": ["Scene"]})
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    run_tool(
        lighting.configure_lighting_quality,
        scene_name="Scene",
        target_engine="BOTH",
        cycles=lighting.CyclesLightingQuality(samples=128),
        eevee=lighting.EeveeLightingQuality(render_samples=64, use_fast_gi=True),
    )
    run_tool(
        lighting.configure_lighting_quality,
        scene_name="Scene",
        target_engine="EEVEE",
        preset="FINAL",
        detail=True,
    )

    assert connection.calls == [
        (
            "configure_lighting_quality",
            {
                "scene_name": "Scene",
                "target_engine": "BOTH",
                "preset": None,
                "cycles": {"samples": 128},
                "eevee": {"render_samples": 64, "use_fast_gi": True},
                "detail": False,
            },
        ),
        (
            "configure_lighting_quality",
            {
                "scene_name": "Scene",
                "target_engine": "EEVEE",
                "preset": "FINAL",
                "cycles": None,
                "eevee": None,
                "detail": True,
            },
        ),
    ]


def test_hdri_requires_an_absolute_hdr_or_exr_path_before_dispatch(monkeypatch) -> None:
    connection = StubConnection()
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    with pytest.raises(ToolError, match="absolute"):
        run_tool(lighting.configure_hdri_environment, scene_name="Scene", image_path="studio.hdr")
    with pytest.raises(ToolError, match=".hdr or .exr"):
        run_tool(lighting.configure_hdri_environment, scene_name="Scene", image_path="/tmp/studio.png")

    assert connection.calls == []


def test_dispatch_advertises_lighting_and_marks_only_inspection_read_only(monkeypatch) -> None:
    addon, _bpy = load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()

    commands = server._build_command_handlers()

    assert LIGHTING_COMMANDS.issubset(commands)
    assert all(server.command_spec(name).read_only for name in READ_ONLY_LIGHTING_COMMANDS)
    assert not {name for name in LIGHTING_COMMANDS - READ_ONLY_LIGHTING_COMMANDS if server.command_spec(name).read_only}


# A float wider than float32 carries, so a rounded field is distinguishable from an unrounded one.
_UNROUNDED = 0.12345678912345678
_ROUNDED = 0.123457


class FakeMatrix:
    """A 4x4 world matrix exposing the three members the lighting snapshots read."""

    def __init__(self, location: list[float], rotation_quaternion: list[float], scale: list[float]) -> None:
        self.translation = location
        self._decomposed = (location, rotation_quaternion, scale)
        self._rows = [[*location, 1.0], [*rotation_quaternion[1:], 0.0], [*scale, 0.0], [0.0, 0.0, 0.0, 1.0]]

    def __iter__(self) -> Iterator[list[float]]:
        """Yield the matrix rows, as serializing a Blender matrix does."""
        return iter(self._rows)

    def decompose(self) -> tuple[list[float], list[float], list[float]]:
        """Return the (location, rotation quaternion, scale) triple Blender's matrices return."""
        return self._decomposed


def fake_light(name="Key Light", *, energy=1000.0, light_type="AREA"):
    """Build the minimum light object the bundled lighting snapshots read."""
    location = [_UNROUNDED, -1.2345678912345678, 5.0]
    data = types.SimpleNamespace(
        name=f"{name} Light",
        type=light_type,
        energy=energy,
        color=[_UNROUNDED, 1.0, 1.0],
        users=1,
        size=0.25,
        use_shadow=True,
    )
    return types.SimpleNamespace(
        name=name,
        type="LIGHT",
        data=data,
        location=location,
        scale=[1.0, 1.0, 1.0],
        rotation_mode="XYZ",
        rotation_euler=types.SimpleNamespace(x=_UNROUNDED, y=0.0, z=0.0),
        matrix_world=FakeMatrix(location, [1.0, _UNROUNDED, 0.0, 0.0], [1.0, 1.0, 1.0]),
        hide_viewport=False,
        hide_render=False,
        constraints=[],
        users_collection=[types.SimpleNamespace(name="Lighting")],
        lightgroup="",
        light_linking=None,
    )


def load_lighting_handlers(monkeypatch):
    """Import the bundled lighting handler modules against the stub bpy module."""
    addon, _bpy = load_addon(monkeypatch, data={})
    package = f"{addon.__name__}.handlers.lighting"
    return sys.modules[f"{package}._shared"], sys.modules[f"{package}.inspection"], sys.modules[f"{package}.rendering"]


def test_default_light_record_is_identity_plus_the_facts_a_listing_is_asked_for(monkeypatch) -> None:
    shared, _inspection, _rendering = load_lighting_handlers(monkeypatch)

    record = shared.light_summary(fake_light())

    assert set(record) == {
        "object",
        "light_data",
        "light_type",
        "energy",
        "color",
        "location_world",
        "hidden_viewport",
        "hidden_render",
    }
    assert record["object"] == "Key Light"
    assert record["light_data"] == "Key Light Light"
    assert record["light_type"] == "AREA"
    assert record["location_world"] == [_ROUNDED, -1.234568, 5.0]
    assert record["color"] == [_ROUNDED, 1.0, 1.0]


def test_detail_records_carry_the_state_the_default_record_omits(monkeypatch) -> None:
    shared, _inspection, _rendering = load_lighting_handlers(monkeypatch)
    light = fake_light()

    summary = shared.light_summary(light)
    full = shared.light_snapshot(light)

    assert {"transform", "settings", "light_linking", "collections", "target_constraints"} <= set(full)
    assert full["object"] == summary["object"]
    assert full["settings"]["energy"] == summary["energy"]
    assert full["transform"]["world"]["location"] == summary["location_world"]


def test_light_transform_floats_are_rounded_to_six_decimals(monkeypatch) -> None:
    shared, _inspection, _rendering = load_lighting_handlers(monkeypatch)

    transform = shared.transform_snapshot(fake_light())

    assert transform["world"]["matrix"][0] == [_ROUNDED, -1.234568, 5.0, 1.0]
    assert transform["world"]["matrix"][3] == [0.0, 0.0, 0.0, 1.0]
    assert transform["world"]["rotation_quaternion"] == [1.0, _ROUNDED, 0.0, 0.0]
    assert transform["local"]["location"] == [_ROUNDED, -1.234568, 5.0]
    assert transform["local"]["rotation"] == [_ROUNDED, 0.0, 0.0]


def test_light_inventories_trim_by_default_and_restore_full_records_with_detail(monkeypatch) -> None:
    _shared_module, inspection, _rendering = load_lighting_handlers(monkeypatch)
    scene = types.SimpleNamespace(name="Scene", unit_settings=types.SimpleNamespace(scale_length=1.0))
    lights = [fake_light("Key Light"), fake_light("Rim Light")]
    monkeypatch.setattr(inspection, "scene_by_name", lambda _name: scene)
    monkeypatch.setattr(inspection, "_scene_lights", lambda *_args, **_kwargs: lights)
    handler = inspection.LightingInspectionHandlers()

    trimmed = handler.list_lights("Scene")
    detailed = handler.list_lights("Scene", detail=True)

    assert [record["object"] for record in trimmed["lights"]] == ["Key Light", "Rim Light"]
    assert [record["object"] for record in detailed["lights"]] == ["Key Light", "Rim Light"]
    assert trimmed["detail"] is False
    assert detailed["detail"] is True
    assert "transform" not in trimmed["lights"][0]
    assert "settings" not in trimmed["lights"][0]
    assert detailed["lights"][0]["transform"]["world"]["matrix"][0] == [_ROUNDED, -1.234568, 5.0, 1.0]
    assert trimmed["total"] == len(lights)
    assert trimmed["returned_count"] == len(lights)


@pytest.mark.parametrize("bound", ["limit", "offset"])
def test_a_light_listing_refuses_a_whole_float_page_bound(monkeypatch, bound) -> None:
    """A page bound takes an int: a float, even a whole one, is refused like a bool."""
    _shared_module, inspection, _rendering = load_lighting_handlers(monkeypatch)
    scene = types.SimpleNamespace(name="Scene", unit_settings=types.SimpleNamespace(scale_length=1.0))
    monkeypatch.setattr(inspection, "scene_by_name", lambda _name: scene)
    monkeypatch.setattr(inspection, "_scene_lights", lambda *_args, **_kwargs: [fake_light()])
    handler = inspection.LightingInspectionHandlers()

    with pytest.raises(ValueError, match=bound):
        handler.list_lights("Scene", **{bound: 1.0})


def test_preview_matched_state_names_its_lights_instead_of_embedding_them(monkeypatch) -> None:
    _shared_module, _inspection, rendering = load_lighting_handlers(monkeypatch)
    scene = types.SimpleNamespace(
        objects=[fake_light("Rim Light"), types.SimpleNamespace(name="Cube", type="MESH"), fake_light("Key Light")],
        world=types.SimpleNamespace(name="Studio World"),
        view_settings=types.SimpleNamespace(exposure=0.5, view_transform="AgX"),
    )

    state = rendering._matched_state(scene)

    assert state["lights"] == ["Key Light", "Rim Light"]
    assert state["light_count"] == len(state["lights"])
    assert state["world"] == "Studio World"


def test_light_inventory_tools_forward_the_detail_flag(monkeypatch) -> None:
    connection = StubConnection({"lights": []})
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    run_tool(lighting.list_lights, scene_name="Scene")
    run_tool(lighting.list_lights, scene_name="Scene", detail=True)
    run_tool(lighting.inspect_lighting_setup, scene_name="Scene", detail=True)

    assert [(command, params["detail"]) for command, params in connection.calls] == [
        ("list_lights", False),
        ("list_lights", True),
        ("inspect_lighting_setup", True),
    ]


class _ObjectsByName(dict):
    """`bpy.data.objects`: name lookups, and an iteration that yields the objects themselves."""

    def __iter__(self):
        return iter(self.values())


def test_configuring_a_widely_shared_light_counts_its_users_and_names_only_the_light(monkeypatch) -> None:
    """A datablock shared by a dozen lamps is counted and sampled, and only the named light is the change."""
    shared_data = types.SimpleNamespace(name="Practical Bulb", type="POINT", energy=100.0)
    lamps = [
        types.SimpleNamespace(name=f"Practical_{index:02d}", type="LIGHT", data=shared_data) for index in range(12)
    ]
    unshared = types.SimpleNamespace(
        name="Key", type="LIGHT", data=types.SimpleNamespace(name="Key Light", type="POINT", energy=5.0)
    )
    mesh = types.SimpleNamespace(name="Lampshade", type="MESH", data=shared_data)
    objects = _ObjectsByName({obj.name: obj for obj in [*reversed(lamps), unshared, mesh]})
    addon, _bpy = load_addon(monkeypatch, data={"objects": objects})

    result = addon.BlenderMCPServer().configure_light("Practical_05", {"energy": 40.0})

    assert all(lamp.data.energy == pytest.approx(40.0) for lamp in lamps)
    assert result["data_users"] == {
        "total": 12,
        "by_type": {"LIGHT": 12},
        "limit": 10,
        "returned_count": 10,
        "truncated": True,
        "names": [f"Practical_{index:02d}" for index in range(10)],
    }
    assert result["changed_objects"] == ["Practical_05"]
    assert result["changed_resources"] == ["Practical Bulb"]
