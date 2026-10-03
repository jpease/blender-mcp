import asyncio

from blender_mcp.server.tools import _dispatch, _provider_fetch, polyhaven
from blender_mcp.server.tools.polyhaven import _polyhaven_changed

_SUCCEEDED = {"fetch_id": "f1", "state": "SUCCEEDED", "stage": "succeeded", "bytes_received": 0, "bytes_total": 0}


def test_a_model_import_reports_the_roots_the_handler_named_not_every_imported_object(monkeypatch) -> None:
    """The handler counts a model's parts and names its roots; the envelope carries only the roots."""
    imported = {"total": 16, "by_type": {"EMPTY": 3, "MESH": 13}, "truncated": True, "names": ["Bench Root"]}
    calls = []

    class Connection:
        def send_command(self, command, params):
            calls.append(command)
            if command == "start_polyhaven_download":
                assert params["asset_id"] == "bench"
                return dict(_SUCCEEDED, ready_to_import=True)
            assert command == "import_polyhaven_asset"
            assert params == {"fetch_id": "f1"}
            return {"success": True, "imported_objects": imported, "changed_objects": ["Bench Root", "Bench Shadow"]}

    monkeypatch.setattr(_dispatch, "get_blender_connection", Connection)

    result = asyncio.run(polyhaven.import_polyhaven_asset(ctx=None, asset_id="bench", asset_type="models"))

    assert calls == ["start_polyhaven_download", "import_polyhaven_asset"]
    assert result["changed_objects"] == ["Bench Root", "Bench Shadow"]
    assert result["changed_resources"] == []
    assert result["data"]["imported_objects"] == imported
    assert "changed_objects" not in result["data"]


class _Context:
    """Record what a tool reports as MCP progress."""

    def __init__(self) -> None:
        self.progress: list[tuple] = []

    async def report_progress(self, progress, total=None, message=None) -> None:
        self.progress.append((progress, total))


def test_a_running_download_reports_progress_until_it_succeeds(monkeypatch) -> None:
    """A RUNNING start, then a SUCCEEDED poll: the byte counts reach the client, then the import runs."""
    monkeypatch.setattr(_provider_fetch, "_FIRST_POLL_SECONDS", 0)
    calls = []

    class Connection:
        def send_command(self, command, params):
            calls.append(command)
            if command == "start_polyhaven_download":
                return {
                    "fetch_id": "f1",
                    "state": "RUNNING",
                    "stage": "downloading",
                    "bytes_received": 3,
                    "bytes_total": 9,
                }
            if command == "get_provider_fetch":
                return dict(_SUCCEEDED, bytes_received=9, bytes_total=9)
            assert command == "import_polyhaven_asset"
            return {"success": True, "image_name": "sky.hdr", "image_path": "/cache/sky.hdr", "world": "World"}

    monkeypatch.setattr(_dispatch, "get_blender_connection", Connection)
    ctx = _Context()

    result = asyncio.run(polyhaven.import_polyhaven_asset(ctx=ctx, asset_id="sky", asset_type="hdris"))

    assert calls == ["start_polyhaven_download", "get_provider_fetch", "import_polyhaven_asset"]
    assert ctx.progress == [(3, 9), (9, 9)]
    assert result["changed_resources"] == ["sky.hdr", "World"]


def test_cancelling_the_call_cancels_the_download_in_blender(monkeypatch) -> None:
    """An MCP-side cancellation while polling sends cancel_provider_fetch for the fetch."""
    monkeypatch.setattr(_provider_fetch, "_FIRST_POLL_SECONDS", 0)
    monkeypatch.setattr(_provider_fetch, "_MAX_POLL_SECONDS", 0)
    calls = []

    class Connection:
        def send_command(self, command, params):
            calls.append((command, params))
            if command == "cancel_provider_fetch":
                return {"fetch_id": "f1", "discarded": True, "state": "CANCELLING"}
            return {"fetch_id": "f1", "state": "RUNNING", "stage": "downloading", "bytes_received": 0, "bytes_total": 0}

    monkeypatch.setattr(_dispatch, "get_blender_connection", Connection)

    async def scenario() -> None:
        task = asyncio.create_task(polyhaven.import_polyhaven_asset(ctx=None, asset_id="sky", asset_type="hdris"))
        while not any(command == "get_provider_fetch" for command, _params in calls):
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("the call was not cancelled")

    asyncio.run(scenario())

    assert ("cancel_provider_fetch", {"fetch_id": "f1"}) in calls
    assert not any(command == "import_polyhaven_asset" for command, _params in calls)


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
    calls = []

    class Connection:
        def send_command(self, command, params):
            calls.append(command)
            if command == "cancel_provider_fetch":
                return {"fetch_id": "f1", "discarded": True, "state": "SUCCEEDED"}
            assert command == "start_polyhaven_catalog"
            assert params["limit"] == 2
            assert params["offset"] == 4
            page = {
                "assets": {"asset-e": {}},
                "total_count": 9,
                "returned_count": 1,
                "offset": 4,
                "limit": 2,
                "truncated": True,
                "next_offset": 5,
            }
            return dict(_SUCCEEDED, result=page)

    monkeypatch.setattr(_dispatch, "get_blender_connection", Connection)

    result = asyncio.run(polyhaven.list_polyhaven_assets(ctx=None, limit=2, offset=4))

    assert calls == ["start_polyhaven_catalog", "cancel_provider_fetch"]
    assert result["data"]["next_offset"] == 5
    assert result["data"]["truncated"] is True


def test_hdri_changed_resources_include_world_and_image() -> None:
    objects, resources = _polyhaven_changed("hdris", {"image_name": "Sky.exr", "world": "Lighting World"})

    assert objects == []
    assert resources == ["Sky.exr", "Lighting World"]
