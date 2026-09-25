"""Regression coverage for list_scene_objects pagination and the new get_mesh_data tool."""

import sys
import types

import pytest

from conftest import load_addon


class FakeVector:
    def __init__(self, x=0.0, y=0.0, z=0.0) -> None:
        self.x, self.y, self.z = x, y, z

    def __iter__(self):
        return iter((self.x, self.y, self.z))


class FakeVertex:
    def __init__(self, index, co=(0.0, 0.0, 0.0), normal=(0.0, 0.0, 1.0), select=False) -> None:
        self.index = index
        self.co = FakeVector(*co)
        self.normal = FakeVector(*normal)
        self.select = select


class FakeEdge:
    def __init__(self, index, vertices=(0, 1), select=False) -> None:
        self.index = index
        self.vertices = vertices
        self.select = select


class FakePolygon:
    def __init__(
        self,
        index,
        vertices=(0, 1, 2, 3),
        normal=(0.0, 0.0, 1.0),
        select=False,
        material_index=0,
        loop_start=0,
        loop_total=4,
    ) -> None:
        self.index = index
        self.vertices = vertices
        self.normal = FakeVector(*normal)
        self.select = select
        self.material_index = material_index
        self.loop_start = loop_start
        self.loop_total = loop_total

    @property
    def loop_indices(self):
        return range(self.loop_start, self.loop_start + self.loop_total)


class FakeLoop:
    def __init__(self, index, vertex_index=0, edge_index=0, normal=(0.0, 0.0, 1.0)) -> None:
        self.index = index
        self.vertex_index = vertex_index
        self.edge_index = edge_index
        self.normal = FakeVector(*normal)


class FakeMeshData:
    """A minimal but structurally-real mesh: n_polys quads, 4 loops each."""

    def __init__(self, n_verts=8, n_edges=12, n_polys=6) -> None:
        self.vertices = [FakeVertex(i, co=(float(i), 0.0, 0.0)) for i in range(n_verts)]
        self.edges = [FakeEdge(i, vertices=(i % n_verts, (i + 1) % n_verts)) for i in range(n_edges)]
        loops_per_poly = 4
        self.polygons = [
            FakePolygon(
                i,
                vertices=(0, 1, 2, 3),
                loop_start=i * loops_per_poly,
                loop_total=loops_per_poly,
            )
            for i in range(n_polys)
        ]
        self.loops = [
            FakeLoop(
                i,
                vertex_index=i % n_verts,
                edge_index=i % n_edges,
            )
            for i in range(n_polys * loops_per_poly)
        ]

    def calc_normals_split(self) -> None:
        pass


class FakeObjectsCollection(dict):
    def get(self, name, default=None):
        return dict.get(self, name, default)

    def new(self, name, data):
        obj = types.SimpleNamespace(
            name=name,
            type="MESH" if data is not None else "EMPTY",
            location=FakeVector(),
            rotation_euler=FakeVector(),
            rotation_mode="XYZ",
            rotation_quaternion=types.SimpleNamespace(w=1.0, x=0.0, y=0.0, z=0.0),
            rotation_axis_angle=(0.0, 0.0, 0.0, 1.0),
            scale=FakeVector(1.0, 1.0, 1.0),
            matrix_world=((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0)),
            dimensions=(0.0, 0.0, 0.0),
            parent=None,
            parent_type="OBJECT",
            parent_bone="",
            users_collection=[],
            hide_viewport=False,
            hide_render=False,
            material_slots=[],
            modifiers=[],
            data=data,
            editmode_sync_calls=0,
        )
        obj.visible_get = lambda: True
        obj.select_get = lambda: False

        def _update_from_editmode():
            obj.editmode_sync_calls += 1
            return False

        obj.update_from_editmode = _update_from_editmode
        self[name] = obj
        return obj


def _load_inspection_addon(monkeypatch):
    objects = FakeObjectsCollection()
    materials = []

    scene = types.SimpleNamespace(
        name="Scene",
        objects=objects.values(),
        blendermcp_use_polyhaven=False,
        blendermcp_use_sketchfab=False,
        unit_settings=types.SimpleNamespace(system="NONE", scale_length=1.0, length_unit="METERS"),
    )

    addon, bpy = load_addon(monkeypatch, scene=scene, data={"objects": objects, "materials": materials})
    bpy.context.selected_objects = []
    bpy.context.mode = "OBJECT"
    bpy.context.view_layer = None
    bpy.ops.mesh = types.SimpleNamespace()
    bpy.ops.object = types.SimpleNamespace()
    bpy.ops.curve = types.SimpleNamespace()
    return addon, bpy, objects, scene


def _new_mesh_object(bpy, name, **mesh_kwargs):
    obj = bpy.data.objects.new(name, FakeMeshData(**mesh_kwargs))
    return obj


def _new_empty_object(bpy, name):
    return bpy.data.objects.new(name, None)


# region list_scene_objects pagination
def test_list_scene_objects_default_returns_everything_under_the_default_limit(monkeypatch) -> None:
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    for i in range(5):
        _new_mesh_object(bpy, f"obj{i}")

    result = server.list_scene_objects()

    assert result["object_count"] == 5
    assert result["returned_count"] == 5
    assert len(result["objects"]) == 5
    assert result["truncated"] is False
    assert result["next_offset"] is None


def test_list_scene_objects_paginates_and_reports_truncation(monkeypatch) -> None:
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    for i in range(7):
        _new_mesh_object(bpy, f"obj{i}")

    page1 = server.list_scene_objects(limit=3, offset=0)
    assert page1["object_count"] == 7
    assert page1["returned_count"] == 3
    assert page1["truncated"] is True
    assert page1["next_offset"] == 3

    page2 = server.list_scene_objects(limit=3, offset=3)
    assert page2["returned_count"] == 3
    assert page2["truncated"] is True
    assert page2["next_offset"] == 6

    page3 = server.list_scene_objects(limit=3, offset=6)
    assert page3["returned_count"] == 1
    assert page3["truncated"] is False
    assert page3["next_offset"] is None

    seen = {o["name"] for p in (page1, page2, page3) for o in p["objects"]}
    assert seen == {f"obj{i}" for i in range(7)}


def test_list_scene_objects_offset_past_end_returns_empty_page(monkeypatch) -> None:
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_mesh_object(bpy, "only_obj")

    result = server.list_scene_objects(limit=10, offset=50)

    assert result["returned_count"] == 0
    assert result["truncated"] is False
    assert result["next_offset"] is None


def test_list_scene_objects_search_pages_over_the_matches_only(monkeypatch) -> None:
    """A name filter narrows what is paged, so offsets count matches, not the whole scene."""
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    for name in ("CHAR1_Body_geo", "Wall_geo", "char1_rig", "Door_grp", "CHAR1_Eye_geo"):
        _new_mesh_object(bpy, name)

    first = server.list_scene_objects(limit=2, offset=0, search="Char1_")
    second = server.list_scene_objects(limit=2, offset=first["next_offset"], search="Char1_")

    assert first["object_count"] == 5
    assert first["matched_count"] == 3
    assert first["truncated"] is True
    assert second["truncated"] is False
    names = [record["name"] for page in (first, second) for record in page["objects"]]
    assert names == ["CHAR1_Body_geo", "CHAR1_Eye_geo", "char1_rig"]


# endregion


# region get_mesh_data
def test_get_mesh_data_rejects_missing_object(monkeypatch) -> None:
    addon, _bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()

    with pytest.raises(ValueError, match="Object not found"):
        server.get_mesh_data(object_name="does_not_exist")


def test_get_mesh_data_rejects_non_mesh_object(monkeypatch) -> None:
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_empty_object(bpy, "empty_obj")

    with pytest.raises(ValueError, match="is not a mesh"):
        server.get_mesh_data(object_name="empty_obj")


def test_get_mesh_data_rejects_invalid_element_type(monkeypatch) -> None:
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_mesh_object(bpy, "obj")

    with pytest.raises(ValueError, match="Invalid element_type"):
        server.get_mesh_data(object_name="obj", element_type="normals")


def test_get_mesh_data_vertices_default_page(monkeypatch) -> None:
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_mesh_object(bpy, "obj", n_verts=8)

    result = server.get_mesh_data(object_name="obj", element_type="vertices")

    assert result["element_type"] == "vertices"
    assert result["total"] == 8
    assert result["total_unfiltered"] == 8
    assert result["returned_count"] == 8
    assert result["truncated"] is False
    first = result["elements"][0]
    assert first["index"] == 0
    assert first["co"] == [0.0, 0.0, 0.0]
    assert first["normal"] == [0.0, 0.0, 1.0]
    assert first["select"] is False


def test_get_mesh_data_paginates_and_reports_truncation(monkeypatch) -> None:
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_mesh_object(bpy, "obj", n_verts=10)

    page1 = server.get_mesh_data(object_name="obj", element_type="vertices", limit=4, offset=0)
    assert [e["index"] for e in page1["elements"]] == [0, 1, 2, 3]
    assert page1["truncated"] is True
    assert page1["next_offset"] == 4

    page2 = server.get_mesh_data(object_name="obj", element_type="vertices", limit=4, offset=page1["next_offset"])
    assert [e["index"] for e in page2["elements"]] == [4, 5, 6, 7]
    assert page2["truncated"] is True
    assert page2["next_offset"] == 8

    page3 = server.get_mesh_data(object_name="obj", element_type="vertices", limit=4, offset=page2["next_offset"])
    assert [e["index"] for e in page3["elements"]] == [8, 9]
    assert page3["truncated"] is False
    assert page3["next_offset"] is None


def test_get_mesh_data_edges_shape(monkeypatch) -> None:
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_mesh_object(bpy, "obj", n_edges=3)

    result = server.get_mesh_data(object_name="obj", element_type="edges")

    assert result["total"] == 3
    assert result["elements"][0] == {"index": 0, "vertices": [0, 1], "select": False}


def test_get_mesh_data_faces_shape(monkeypatch) -> None:
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_mesh_object(bpy, "obj", n_polys=2)

    result = server.get_mesh_data(object_name="obj", element_type="faces")

    assert result["total"] == 2
    face = result["elements"][0]
    assert face["index"] == 0
    assert face["vertices"] == [0, 1, 2, 3]
    assert face["normal"] == [0.0, 0.0, 1.0]
    assert face["material_index"] == 0


def test_get_mesh_data_loops_shape_and_face_index_mapping(monkeypatch) -> None:
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_mesh_object(bpy, "obj", n_polys=2)

    result = server.get_mesh_data(object_name="obj", element_type="loops")

    assert result["total"] == 8  # 2 faces * 4 loops
    loops = result["elements"]
    # First 4 loops belong to face 0, next 4 to face 1
    assert [loop["face_index"] for loop in loops[:4]] == [0, 0, 0, 0]
    assert [loop["face_index"] for loop in loops[4:8]] == [1, 1, 1, 1]
    assert set(loops[0]) == {"index", "vertex_index", "edge_index", "face_index", "normal"}


def test_get_mesh_data_loops_rejects_selected_only(monkeypatch) -> None:
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_mesh_object(bpy, "obj")

    with pytest.raises(ValueError, match="selected_only"):
        server.get_mesh_data(object_name="obj", element_type="loops", selected_only=True)


def test_get_mesh_data_selected_only_filters_before_paging(monkeypatch) -> None:
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    obj = _new_mesh_object(bpy, "obj", n_verts=6)
    for i in (1, 3, 5):
        obj.data.vertices[i].select = True

    result = server.get_mesh_data(object_name="obj", element_type="vertices", selected_only=True)

    assert result["total"] == 3
    assert result["total_unfiltered"] == 6
    assert [e["index"] for e in result["elements"]] == [1, 3, 5]
    assert all(e["select"] for e in result["elements"])


def test_get_mesh_data_limit_is_clamped_to_max(monkeypatch) -> None:
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_mesh_object(bpy, "obj", n_verts=5)

    result = server.get_mesh_data(object_name="obj", element_type="vertices", limit=999999)

    # A huge requested limit is silently clamped server-side (max 1000), so a
    # 5-vertex mesh still returns everything in one page rather than erroring.
    assert result["returned_count"] == 5
    assert result["truncated"] is False


def test_get_mesh_data_syncs_from_editmode_before_reading(monkeypatch) -> None:
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    obj = _new_mesh_object(bpy, "obj")

    server.get_mesh_data(object_name="obj", element_type="vertices")

    assert obj.editmode_sync_calls == 1


# endregion


# region get_object_info
def test_get_object_info_syncs_from_editmode_before_reading(monkeypatch) -> None:
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    obj = _new_empty_object(bpy, "empty_obj")

    server.get_object_info("empty_obj")

    assert obj.editmode_sync_calls == 1


def test_get_object_info_says_whether_it_resolved_an_override_or_a_linked_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a library override a name has two objects; the result says which one was read."""
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_empty_object(bpy, "local_obj")
    override = _new_empty_object(bpy, "override_obj")
    override.override_library = object()
    linked = _new_empty_object(bpy, "linked_obj")
    linked.library = types.SimpleNamespace(name="/studio/canon.blend")

    local_info = server.get_object_info("local_obj")
    override_info = server.get_object_info("override_obj")
    linked_info = server.get_object_info("linked_obj")

    assert (local_info["library"], local_info["is_override"]) == (None, False)
    assert (override_info["library"], override_info["is_override"]) == (None, True)
    assert (linked_info["library"], linked_info["is_override"]) == ("canon.blend", False)


class _OverriddenShotObjects(dict):
    """`bpy.data.objects` after an override: two objects share a name; `(name, None)` keys the local one."""

    def get(self, key: object, default: object = None) -> object:
        if isinstance(key, tuple):
            name, filepath = key
            matches = [obj for obj in self.values() if obj.name == name and getattr(obj, "library", None) is filepath]
            return matches[0] if matches else default
        return next((obj for obj in self.values() if obj.name == key), default)


def test_object_name_lookups_resolve_to_the_override_even_when_the_linked_original_is_listed_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`get_object_info` and every scene tool's `_object` pick the override, not whichever Blender listed first."""
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    linked = _new_empty_object(bpy, "linked_key")
    linked.name = "HeroCam"
    linked.library = types.SimpleNamespace(name="canon.blend")
    override = _new_empty_object(bpy, "override_key")
    override.name = "HeroCam"
    override.override_library = object()
    bpy.data.objects = _OverriddenShotObjects(bpy.data.objects)

    assert server.get_object_info("HeroCam")["is_override"] is True
    assert sys.modules[f"{addon.__name__}.handlers.scene"]._object("HeroCam") is override


def test_the_transaction_snapshots_the_same_object_the_handler_mutates_after_an_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    `_resolve_targets` feeds `mutation_transaction`, which writes state back on a failure.

    Resolving targets by Blender's list order while the handler resolves by `find_object` would
    restore state onto the linked original and leave the override holding a partial edit.
    """
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    linked = _new_empty_object(bpy, "linked_key")
    linked.name = "HeroCam"
    linked.library = types.SimpleNamespace(name="canon.blend")
    override = _new_empty_object(bpy, "override_key")
    override.name = "HeroCam"
    override.override_library = object()
    bpy.data.objects = _OverriddenShotObjects(bpy.data.objects)

    targets = server._resolve_targets({"object_name": "HeroCam"})

    assert targets == [override]
    assert targets[0] is sys.modules[f"{addon.__name__}.handlers.scene"]._object("HeroCam")


def test_an_ambiguous_target_name_is_skipped_rather_than_raising_out_of_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name linked from two libraries is skipped here; the handler's own call raises the refusal."""
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    for index in range(2):
        obj = _new_empty_object(bpy, f"prop_key{index}")
        obj.name = "Prop"
        obj.library = types.SimpleNamespace(name=f"canon{index}.blend")
    bpy.data.objects = _OverriddenShotObjects(bpy.data.objects)

    assert server._resolve_targets({"object_name": "Prop"}) == []


def test_get_object_info_reports_default_euler_rotation(monkeypatch) -> None:
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_empty_object(bpy, "empty_obj")

    info = server.get_object_info("empty_obj")

    assert info["rotation_mode"] == "XYZ"
    assert info["rotation"] == [0.0, 0.0, 0.0]


def test_get_object_info_reports_quaternion_rotation(monkeypatch) -> None:
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    obj = _new_empty_object(bpy, "empty_obj")
    obj.rotation_mode = "QUATERNION"
    obj.rotation_quaternion = types.SimpleNamespace(w=0.5, x=0.5, y=0.5, z=0.5)

    info = server.get_object_info("empty_obj")

    assert info["rotation_mode"] == "QUATERNION"
    assert info["rotation"] == [0.5, 0.5, 0.5, 0.5]


def test_get_object_info_reports_axis_angle_rotation(monkeypatch) -> None:
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    obj = _new_empty_object(bpy, "empty_obj")
    obj.rotation_mode = "AXIS_ANGLE"
    obj.rotation_axis_angle = (1.2, 0.0, 0.0, 1.0)

    info = server.get_object_info("empty_obj")

    assert info["rotation_mode"] == "AXIS_ANGLE"
    assert info["rotation"] == [1.2, 0.0, 0.0, 1.0]


def test_get_object_info_reports_modifiers(monkeypatch) -> None:
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    obj = _new_empty_object(bpy, "empty_obj")
    obj.modifiers = [
        types.SimpleNamespace(name="Bevel", type="BEVEL", show_viewport=True, show_render=False),
        types.SimpleNamespace(name="Subsurf", type="SUBSURF", show_viewport=True, show_render=True),
    ]

    info = server.get_object_info("empty_obj")

    assert info["modifiers"] == [
        {"name": "Bevel", "type": "BEVEL", "show_viewport": True, "show_render": False},
        {"name": "Subsurf", "type": "SUBSURF", "show_viewport": True, "show_render": True},
    ]


def _emitter(bpy, system_count):
    """Make an Empty with `system_count` particle systems, the one `type_data` page every object type has."""
    obj = _new_empty_object(bpy, "emitter")
    obj.particle_systems = [
        types.SimpleNamespace(name=f"system_{index}", settings=None, particles=[], seed=index)
        for index in range(system_count)
    ]
    return obj


def test_get_object_info_resumes_its_type_data_pages_from_the_offset_it_was_given(monkeypatch) -> None:
    """A page's `next_offset` leads to the records after it, not back to the first page."""
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _emitter(bpy, 5)

    first = server.get_object_info("emitter", sections=["PARTICLES"], limit=2)["type_data"]["particle_systems"]
    second = server.get_object_info("emitter", sections=["PARTICLES"], limit=2, offset=first["next_offset"])

    page = second["type_data"]["particle_systems"]
    assert [record["name"] for record in first["records"]] == ["system_0", "system_1"]
    assert [record["name"] for record in page["records"]] == ["system_2", "system_3"]
    assert (page["offset"], page["truncated"], page["next_offset"]) == (2, True, 4)


def test_get_object_info_reports_the_page_size_it_applied_not_the_one_asked_for(monkeypatch) -> None:
    """A limit past the ceiling is cut to it, and the page names the size it was cut to."""
    addon, bpy, _objects, _scene = _load_inspection_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _emitter(bpy, 3)
    ceiling = server._OBJECT_INFO_MAX_LIMIT

    info = server.get_object_info("emitter", sections=["PARTICLES"], limit=ceiling + 1)

    page = info["type_data"]["particle_systems"]
    assert (page["limit"], page["returned_count"]) == (ceiling, 3)


# endregion
