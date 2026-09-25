"""Regression coverage for ND (HugeMenace) availability reporting and related handlers."""

import contextlib
import sys
import types

import pytest

from conftest import load_addon


class FakeModifier:
    def __init__(self, name, type_) -> None:
        self.name = name
        self.type = type_


class FakeMatrix:
    def __init__(self, value) -> None:
        self.value = value

    def copy(self):
        return FakeMatrix(self.value)

    def inverted(self):
        return FakeMatrix(f"inv({self.value})")

    def __eq__(self, other):
        return isinstance(other, FakeMatrix) and self.value == other.value


class FakeObject:
    def __init__(self, name, modifiers=None, object_type="MESH") -> None:
        self.name = name
        self.type = object_type
        self.modifiers = list(modifiers or [])
        self.parent = None
        self.matrix_world = FakeMatrix(f"{name}-world")

    def select_set(self, _value) -> None:
        pass


class FakeObjectsCollection(dict):
    def get(self, name, default=None):
        return super().get(name, default)

    def __iter__(self):
        return iter(list(self.values()))


class FakeOverlay:
    def __init__(self) -> None:
        self.show_wireframes = False
        self.show_face_orientation = False


class FakeShading:
    def __init__(self) -> None:
        self.show_cavity = False


class FakeArea:
    def __init__(self) -> None:
        self.type = "VIEW_3D"
        self.regions = [types.SimpleNamespace(type="WINDOW")]
        self.spaces = types.SimpleNamespace(active=types.SimpleNamespace(overlay=FakeOverlay(), shading=FakeShading()))


@contextlib.contextmanager
def _temp_override(**_kwargs):
    yield


def _load_nd_addon(monkeypatch, scene, nd_installed=False, objects=None):
    addon, bpy = load_addon(
        monkeypatch,
        scene=scene,
        data={"objects": objects if objects is not None else FakeObjectsCollection()},
    )
    bpy.context.screen = types.SimpleNamespace(areas=[FakeArea()])
    bpy.context.temp_override = _temp_override
    bpy.context.mode = "OBJECT"
    bpy.context.selected_objects = []
    bpy.context.view_layer = types.SimpleNamespace(objects=types.SimpleNamespace(active=None))
    bpy.ops.object = types.SimpleNamespace(
        select_all=lambda **_kw: None,
        mode_set=lambda **_kw: None,
    )
    if nd_installed:
        bpy.ops.nd = types.SimpleNamespace(
            bool_vanilla=lambda *_a, **_k: {"FINISHED"},
            clean_utils=lambda *_a, **_k: {"FINISHED"},
            capture_utils=lambda *_a, **_k: {"FINISHED"},
            toggle_clear_view=lambda *_a, **_k: {"FINISHED"},
            toggle_custom_view=lambda *_a, **_k: {"FINISHED"},
            toggle_utils=lambda *_a, **_k: {"FINISHED"},
        )
    return addon


def _scene(nd_enabled):
    return types.SimpleNamespace(
        blendermcp_use_polyhaven=False,
        blendermcp_use_sketchfab=False,
        blendermcp_use_nd=nd_enabled,
    )


def test_disabled_nd_is_absent_from_dispatch(monkeypatch) -> None:
    addon = _load_nd_addon(monkeypatch, _scene(nd_enabled=False), nd_installed=True)
    server = addon.BlenderMCPServer()

    status = server.get_nd_status()
    command = server.execute_command_internal({"type": "nd_boolean"})

    assert status["enabled"] is False
    assert "currently disabled" in status["message"]
    assert command == {
        "status": "error",
        "message": "Unknown command type: nd_boolean",
    }


def test_enabled_nd_without_addon_installed_is_reported_as_not_ready(monkeypatch) -> None:
    addon = _load_nd_addon(monkeypatch, _scene(nd_enabled=True), nd_installed=False)
    server = addon.BlenderMCPServer()

    status = server.get_nd_status()

    assert status["enabled"] is False
    assert "does not appear to be" in status["message"]


def test_enabled_nd_with_addon_installed_is_ready(monkeypatch) -> None:
    addon = _load_nd_addon(monkeypatch, _scene(nd_enabled=True), nd_installed=True)
    server = addon.BlenderMCPServer()

    status = server.get_nd_status()

    assert status == {
        "enabled": True,
        "message": "ND integration is enabled and the ND addon is installed and ready to use.",
    }


def test_set_viewport_overlay_cavity_sets_overlay_property_idempotently(monkeypatch) -> None:
    addon = _load_nd_addon(monkeypatch, _scene(nd_enabled=False), nd_installed=False)
    server = addon.BlenderMCPServer()
    shading = sys.modules["bpy"].context.screen.areas[0].spaces.active.shading

    result = server.set_viewport_overlay(toggle="cavity", enabled=True)

    assert result == {"toggle": "CAVITY", "enabled": True}
    assert shading.show_cavity is True

    result_again = server.set_viewport_overlay(toggle="CAVITY", enabled=True)

    assert result_again == {"toggle": "CAVITY", "enabled": True}
    assert shading.show_cavity is True


def test_set_viewport_overlay_face_orientation_can_be_turned_off(monkeypatch) -> None:
    addon = _load_nd_addon(monkeypatch, _scene(nd_enabled=False), nd_installed=False)
    server = addon.BlenderMCPServer()
    overlay = sys.modules["bpy"].context.screen.areas[0].spaces.active.overlay
    overlay.show_face_orientation = True

    result = server.set_viewport_overlay(toggle="FACE_ORIENTATION", enabled=False)

    assert result == {"toggle": "FACE_ORIENTATION", "enabled": False}
    assert overlay.show_face_orientation is False


def test_set_viewport_overlay_rejects_unknown_toggle(monkeypatch) -> None:
    addon = _load_nd_addon(monkeypatch, _scene(nd_enabled=False), nd_installed=False)
    server = addon.BlenderMCPServer()

    with pytest.raises(ValueError, match="Invalid toggle"):
        server.set_viewport_overlay(toggle="SILHOUETTE", enabled=True)


def test_nd_pulse_viewport_toggle_clear_view_routes_through_nd_operator(monkeypatch) -> None:
    addon = _load_nd_addon(monkeypatch, _scene(nd_enabled=True), nd_installed=True)
    server = addon.BlenderMCPServer()
    calls = []
    monkeypatch.setattr(
        sys.modules["bpy"].ops.nd,
        "toggle_clear_view",
        lambda *_a, **_k: calls.append("called") or {"FINISHED"},
    )

    result = server.nd_pulse_viewport_toggle(toggle="clear_view")

    assert result == {"toggle": "CLEAR_VIEW", "cancelled": False}
    assert calls == ["called"]


def test_nd_pulse_viewport_toggle_surfaces_cancelled(monkeypatch) -> None:
    addon = _load_nd_addon(monkeypatch, _scene(nd_enabled=True), nd_installed=True)
    server = addon.BlenderMCPServer()
    monkeypatch.setattr(
        sys.modules["bpy"].ops.nd,
        "toggle_utils",
        lambda *_a, **_k: {"CANCELLED"},
    )

    result = server.nd_pulse_viewport_toggle(toggle="UTILS")

    assert result == {"toggle": "UTILS", "cancelled": True}


def test_nd_pulse_viewport_toggle_rejects_unknown_toggle(monkeypatch) -> None:
    addon = _load_nd_addon(monkeypatch, _scene(nd_enabled=True), nd_installed=True)
    server = addon.BlenderMCPServer()

    with pytest.raises(ValueError, match="Invalid toggle"):
        server.nd_pulse_viewport_toggle(toggle="SILHOUETTE")


def test_nd_call_raises_clear_error_when_operator_not_available(monkeypatch) -> None:
    addon = _load_nd_addon(monkeypatch, _scene(nd_enabled=True), nd_installed=False)
    server = addon.BlenderMCPServer()

    with pytest.raises(RuntimeError, match="nd.capture_utils' is not available"):
        server.nd_capture_utils()


def test_nd_boolean_rejects_same_object(monkeypatch) -> None:
    addon = _load_nd_addon(monkeypatch, _scene(nd_enabled=True), nd_installed=True)
    server = addon.BlenderMCPServer()

    with pytest.raises(ValueError, match="must differ from object_name"):
        server.nd_boolean(object_name="Thing", cutter_object_name="Thing")


def test_nd_mark_as_util_parent_to_preserves_world_transform(monkeypatch) -> None:
    objects = FakeObjectsCollection()
    child = FakeObject("Cutter")
    parent = FakeObject("Target")
    objects["Cutter"] = child
    objects["Target"] = parent

    addon = _load_nd_addon(monkeypatch, _scene(nd_enabled=True), nd_installed=True, objects=objects)
    server = addon.BlenderMCPServer()

    result = server.nd_mark_as_util(object_names=["Cutter"], parent_to="Target")

    assert result == {"names": ["Cutter"], "marked_as_util": True, "parent": "Target"}
    assert child.parent is parent
    assert child.matrix_world == FakeMatrix("Cutter-world")


def test_nd_mark_as_util_rejects_parent_to_with_unmark(monkeypatch) -> None:
    addon = _load_nd_addon(monkeypatch, _scene(nd_enabled=True), nd_installed=True)
    server = addon.BlenderMCPServer()

    with pytest.raises(ValueError, match="cannot be combined with unmark"):
        server.nd_mark_as_util(object_names=["Cutter"], unmark=True, parent_to="Target")


def test_nd_clean_utils_requires_confirm(monkeypatch) -> None:
    addon = _load_nd_addon(monkeypatch, _scene(nd_enabled=True), nd_installed=True)
    server = addon.BlenderMCPServer()

    with pytest.raises(ValueError, match="confirm_clean=True"):
        server.nd_clean_utils()


def test_nd_clean_utils_reports_removed_objects_and_modifiers(monkeypatch) -> None:
    objects = FakeObjectsCollection()
    kept = FakeObject("Kept", modifiers=[FakeModifier("Array", "ARRAY")])
    orphan = FakeObject("UtilCutter")
    objects["Kept"] = kept
    objects["UtilCutter"] = orphan

    def fake_clean_utils(*_a, **_k):
        del objects["UtilCutter"]
        kept.modifiers = []
        return {"FINISHED"}

    addon = _load_nd_addon(monkeypatch, _scene(nd_enabled=True), nd_installed=True, objects=objects)
    monkeypatch.setattr(sys.modules["bpy"].ops.nd, "clean_utils", fake_clean_utils)
    server = addon.BlenderMCPServer()

    result = server.nd_clean_utils(confirm_clean=True)

    assert result["status"] == "cleaned"
    assert result["removed_objects"] == {
        "total": 1,
        "by_type": {"MESH": 1},
        "limit": 10,
        "returned_count": 1,
        "truncated": False,
        "names": ["UtilCutter"],
    }
    assert result["removed_modifiers"] == {
        "total": 1,
        "by_type": {"ARRAY": 1},
        "limit": 10,
        "returned_count": 1,
        "truncated": False,
        "records": [{"object": "Kept", "modifier": "Array", "type": "ARRAY"}],
    }
    assert result["changed_objects"] == ["Kept"]
    assert result["cancelled"] is False


def test_a_large_cleanup_counts_what_went_and_names_only_the_objects_that_lost_a_modifier(monkeypatch) -> None:
    """Thirty cutters and twenty-four orphaned modifiers are counts and samples; the hosts are the next target."""
    objects = FakeObjectsCollection()
    hosts = [
        FakeObject(f"Panel {index}", modifiers=[FakeModifier(f"Cut {cut}", "BOOLEAN") for cut in range(8)])
        for index in range(3)
    ]
    cutters = [FakeObject(f"Cutter {index:02d}", object_type="EMPTY" if index % 3 else "MESH") for index in range(30)]
    for obj in [*hosts, *cutters]:
        objects[obj.name] = obj

    def fake_clean_utils(*_a, **_k):
        for cutter in cutters:
            del objects[cutter.name]
        for host in hosts:
            host.modifiers = []
        return {"FINISHED"}

    addon = _load_nd_addon(monkeypatch, _scene(nd_enabled=True), nd_installed=True, objects=objects)
    monkeypatch.setattr(sys.modules["bpy"].ops.nd, "clean_utils", fake_clean_utils)

    result = addon.BlenderMCPServer().nd_clean_utils(confirm_clean=True)

    assert result["removed_objects"] == {
        "total": 30,
        "by_type": {"EMPTY": 20, "MESH": 10},
        "limit": 10,
        "returned_count": 10,
        "truncated": True,
        "names": [f"Cutter {index:02d}" for index in range(10)],
    }
    modifiers = result["removed_modifiers"]
    assert (modifiers["total"], modifiers["by_type"], modifiers["truncated"]) == (24, {"BOOLEAN": 24}, True)
    assert modifiers["records"] == [
        {"object": "Panel 0", "modifier": f"Cut {cut}", "type": "BOOLEAN"} for cut in range(8)
    ] + [{"object": "Panel 1", "modifier": f"Cut {cut}", "type": "BOOLEAN"} for cut in range(2)]
    assert result["changed_objects"] == ["Panel 0", "Panel 1", "Panel 2"]


def test_nd_clean_utils_reports_nothing_removed_when_scene_is_already_clean(monkeypatch) -> None:
    objects = FakeObjectsCollection()
    objects["Solo"] = FakeObject("Solo", modifiers=[FakeModifier("Bevel", "BEVEL")])

    addon = _load_nd_addon(monkeypatch, _scene(nd_enabled=True), nd_installed=True, objects=objects)
    server = addon.BlenderMCPServer()

    result = server.nd_clean_utils(confirm_clean=True)

    assert result == {
        "status": "cleaned",
        "removed_objects": {
            "total": 0,
            "by_type": {},
            "limit": 10,
            "returned_count": 0,
            "truncated": False,
            "names": [],
        },
        "removed_modifiers": {
            "total": 0,
            "by_type": {},
            "limit": 10,
            "returned_count": 0,
            "truncated": False,
            "records": [],
        },
        "cancelled": False,
        "changed_objects": [],
    }


def test_nd_clean_utils_surfaces_cancelled(monkeypatch) -> None:
    addon = _load_nd_addon(monkeypatch, _scene(nd_enabled=True), nd_installed=True)
    monkeypatch.setattr(sys.modules["bpy"].ops.nd, "clean_utils", lambda *_a, **_k: {"CANCELLED"})
    server = addon.BlenderMCPServer()

    result = server.nd_clean_utils(confirm_clean=True)

    assert result["cancelled"] is True
