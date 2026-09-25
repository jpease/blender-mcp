import asyncio

from blender_mcp.server.tools import _dispatch, polyhaven
from blender_mcp.server.tools.polyhaven import _polyhaven_changed


def test_a_model_import_reports_the_roots_the_handler_named_not_every_imported_object(monkeypatch) -> None:
    """The handler counts a model's parts and names its roots; the envelope carries only the roots."""
    imported = {"total": 16, "by_type": {"EMPTY": 3, "MESH": 13}, "truncated": True, "names": ["Bench Root"]}

    class Connection:
        def send_command(self, command, params):
            assert command == "import_polyhaven_asset"
            return {"success": True, "imported_objects": imported, "changed_objects": ["Bench Root", "Bench Shadow"]}

    monkeypatch.setattr(_dispatch, "get_blender_connection", Connection)

    result = asyncio.run(polyhaven.import_polyhaven_asset(ctx=None, asset_id="bench", asset_type="models"))

    assert result["changed_objects"] == ["Bench Root", "Bench Shadow"]
    assert result["changed_resources"] == []
    assert result["data"]["imported_objects"] == imported
    assert "changed_objects" not in result["data"]


def test_textures_reports_material_and_maps_as_changed_resources() -> None:
    changed_objects, changed_resources = _polyhaven_changed(
        "textures", {"material": "Concrete", "maps": ["Concrete_diff", "Concrete_nor"]}
    )

    assert changed_objects == []
    assert changed_resources == ["Concrete", "Concrete_diff", "Concrete_nor"]


def test_hdris_reports_image_name_as_changed_resources() -> None:
    changed_objects, changed_resources = _polyhaven_changed("hdris", {"image_name": "sunset.hdr"})

    assert changed_objects == []
    assert changed_resources == ["sunset.hdr"]


def test_hdris_with_no_image_name_reports_nothing() -> None:
    changed_objects, changed_resources = _polyhaven_changed("hdris", {})

    assert changed_objects == []
    assert changed_resources == []


def test_unknown_asset_type_reports_nothing() -> None:
    changed_objects, changed_resources = _polyhaven_changed("all", {"imported_objects": ["Chair"]})

    assert changed_objects == []
    assert changed_resources == []


def test_asset_id_never_appears_in_changed_objects_or_resources() -> None:
    """Regression: the old code put the Polyhaven asset_id itself into changed_objects."""
    asset_id = "concrete_floor_02"
    result = {
        "asset_id": asset_id,
        "imported_objects": ["Floor"],
        "material": "Concrete",
        "maps": ["Concrete_diff"],
        "image_name": "concrete_floor_02.hdr",
    }

    for asset_type in ("models", "textures", "hdris"):
        changed_objects, changed_resources = _polyhaven_changed(asset_type, result)
        assert asset_id not in changed_objects
        assert asset_id not in changed_resources


def test_list_assets_forwards_pagination_and_returns_continuation(monkeypatch) -> None:
    class Connection:
        def send_command(self, command, params):
            assert command == "list_polyhaven_assets"
            assert params["limit"] == 2
            assert params["offset"] == 4
            return {
                "assets": {"asset-e": {}},
                "total_count": 9,
                "returned_count": 1,
                "offset": 4,
                "limit": 2,
                "truncated": True,
                "next_offset": 5,
            }

    monkeypatch.setattr(_dispatch, "get_blender_connection", Connection)

    result = asyncio.run(polyhaven.list_polyhaven_assets(ctx=None, limit=2, offset=4))

    assert result["data"]["next_offset"] == 5
    assert result["data"]["truncated"] is True


def test_hdri_changed_resources_include_world_and_image() -> None:
    objects, resources = _polyhaven_changed("hdris", {"image_name": "Sky.exr", "world": "Lighting World"})

    assert objects == []
    assert resources == ["Sky.exr", "Lighting World"]
