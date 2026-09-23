"""
Regression coverage for `sample_deformed_geometry`, the rigging surface's only evaluated readback.

Every other reader in the domain describes the file's rest state, so each test here is written
to fail if the handler ever answers from `obj.data` again: the fake mesh's base vertices stay
where they are while the evaluated mesh moves, which is exactly the shape of an armature
deforming a skin.
"""

import sys
import types

import pytest

from .rig_doubles import _Matrix, _PoseBone, _posing, _Vector


class _Vertex:
    """One evaluated or base vertex: a position and a normal, as `MeshVertex` carries them."""

    def __init__(self, co, normal=(0.0, 0.0, 1.0)) -> None:
        self.co = _Vector(co)
        self.normal = _Vector(normal)


class _Mesh:
    """A mesh datablock with only the element collections the sampler reads."""

    def __init__(self, name, coordinates) -> None:
        self.name = name
        self.vertices = [_Vertex(co) for co in coordinates]
        self.edges = []
        self.polygons = []


class _Evaluated:
    """The depsgraph's copy of an object: its own mesh, its own transform, its own cleanup."""

    def __init__(self, matrix_world, mesh) -> None:
        self.matrix_world = matrix_world
        self.bound_box = [(-1.0, -1.0, 0.0), (1.0, 1.0, 2.0)]
        self._mesh = mesh
        self.cleared = 0

    def to_mesh(self):
        """
        Hand out the evaluated mesh.

        Returns:
            _Mesh: The deformed mesh this object evaluates to.

        """
        return self._mesh

    def to_mesh_clear(self) -> None:
        """Release it again, which the handler must do on every path out."""
        self.cleared += 1


class _DeformedMesh:
    """A mesh object whose evaluated shape differs from the base mesh it is stored as."""

    def __init__(self, name, base, evaluated_per_frame, *, matrix_world=None) -> None:
        self.name = name
        self.type = "MESH"
        self.data = _Mesh(f"{name} Mesh", base)
        self.matrix_world = matrix_world or _Matrix.Identity(4)
        self.evaluated_per_frame = evaluated_per_frame
        self.frames_evaluated = []
        self.synced = 0
        self.evaluated = None

    def update_from_editmode(self) -> None:
        """Record the Edit-Mode flush; the rest reference is wrong without it."""
        self.synced += 1

    def evaluated_get(self, depsgraph):
        """
        Answer the evaluated copy for the frame the playhead is on.

        Args:
            depsgraph: The fake depsgraph, carrying the current frame.

        Returns:
            _Evaluated: The deformed copy at that frame.

        """
        self.frames_evaluated.append(depsgraph.frame)
        mesh = _Mesh(f"{self.name} Evaluated", self.evaluated_per_frame[depsgraph.frame])
        self.evaluated = _Evaluated(self.matrix_world, mesh)
        return self.evaluated


def _scene_with(monkeypatch, mesh_object):
    """
    Load the add-on against a rig plus one mesh whose evaluated shape follows the playhead.

    Args:
        monkeypatch: pytest's patcher, owned by the caller's test.
        mesh_object: The `_DeformedMesh` to put in the scene.

    Returns:
        tuple: The loaded server and the fake scene.

    """
    server, _rig, _animation, _posing_module = _posing(
        monkeypatch, [_PoseBone("CHAR1_head_jnt")], objects={mesh_object.name: mesh_object}
    )
    bpy = sys.modules["bpy"]
    scene = bpy.context.scene
    scene.frame_current = 1
    scene.frame_set = lambda frame, subframe=0.0: setattr(scene, "frame_current", int(frame))
    bpy.context.evaluated_depsgraph_get = lambda: types.SimpleNamespace(frame=scene.frame_current)
    return server, scene


# A skin at rest, and the same skin with its second vertex carried 0.25 m by a posed bone.
_REST = [(0.0, 0.0, 1.0), (0.5, 0.0, 1.5), (-0.5, 0.0, 1.5)]
_POSED = [(0.0, 0.0, 1.0), (0.5, 0.25, 1.5), (-0.5, 0.0, 1.5)]


def test_the_sample_reads_the_deformed_surface_and_measures_it_against_the_rest_mesh(monkeypatch) -> None:
    """
    The finding this tool exists for: a pose was only provable by screenshot.

    The base mesh is identical at both frames, so a handler reading `obj.data` - which is what
    `get_mesh_data` and `get_skinning_info` do - answers zero displacement here.
    """
    mesh_object = _DeformedMesh("CHAR1_body_msh", _REST, {1: _REST, 12: _POSED})
    server, scene = _scene_with(monkeypatch, mesh_object)

    reply = server.sample_deformed_geometry("CHAR1_body_msh", frame=12)

    assert reply["evaluated_deformation_included"] is True
    assert reply["index_correspondence"] == "BASE_MESH"
    assert reply["frame"] == 12
    assert mesh_object.frames_evaluated == [12]
    assert reply["displacement"]["measured"] is True
    assert reply["displacement"]["maximum_m"] == pytest.approx(0.25, abs=1e-12)
    assert reply["displacement"]["moved_vertices"] == 1
    moved = reply["vertices"]["items"][1]
    assert moved["co"] == pytest.approx([0.5, 0.25, 1.5], abs=1e-12)
    assert moved["displacement_m"] == pytest.approx(0.25, abs=1e-12)
    assert reply["vertices"]["items"][0]["displacement_m"] == pytest.approx(0.0, abs=1e-12)
    # Read-only: the playhead is where the caller left it, and the evaluated mesh is released.
    assert scene.frame_current == 1
    assert mesh_object.evaluated.cleared == 1
    assert mesh_object.synced == 1


def test_world_space_positions_carry_the_objects_own_transform(monkeypatch) -> None:
    """A deformed vertex is only comparable to a floor, a prop or another character in world space."""
    mesh_object = _DeformedMesh("CHAR1_body_msh", _REST, {1: _POSED}, matrix_world=_Matrix.Translation((2.0, 0.0, 0.0)))
    server, _scene = _scene_with(monkeypatch, mesh_object)

    world = server.sample_deformed_geometry("CHAR1_body_msh")
    local = server.sample_deformed_geometry("CHAR1_body_msh", space="LOCAL")

    assert world["coordinate_space"] == "WORLD"
    assert world["vertices"]["items"][1]["co"] == pytest.approx([2.5, 0.25, 1.5], abs=1e-12)
    assert local["coordinate_space"] == "EVALUATED_OBJECT_LOCAL"
    assert local["vertices"]["items"][1]["co"] == pytest.approx([0.5, 0.25, 1.5], abs=1e-12)
    # Displacement is a world distance under either space: one transform places both surfaces.
    assert world["displacement"] == local["displacement"]
    assert world["displacement"]["maximum_m"] == pytest.approx(0.25, abs=1e-12)


def test_a_generative_modifier_is_reported_as_a_different_numbering_rather_than_mismeasured(monkeypatch) -> None:
    """
    A Subdivision or Mirror result renumbers the mesh, so index i is not base vertex i.

    Silently pairing them would report displacement for vertices that never corresponded.
    """
    subdivided = [*_POSED, (0.25, 0.1, 1.25), (-0.25, 0.0, 1.25)]
    mesh_object = _DeformedMesh("CHAR1_body_msh", _REST, {1: subdivided})
    server, _scene = _scene_with(monkeypatch, mesh_object)

    reply = server.sample_deformed_geometry("CHAR1_body_msh")

    assert reply["index_correspondence"] == "EVALUATED_ONLY"
    assert reply["evaluated_counts"]["vertices"] == 5
    assert reply["base_counts"]["vertices"] == 3
    assert reply["displacement"] == {"measured": False, "reason": "EVALUATED_VERTEX_COUNT_DIFFERS_FROM_BASE"}
    assert "displacement_m" not in reply["vertices"]["items"][0]


def test_a_refused_request_still_puts_the_playhead_back_and_releases_the_mesh(monkeypatch) -> None:
    """Read-only means no transaction covers a half-finished sample; the `finally` blocks are all there is."""
    mesh_object = _DeformedMesh("CHAR1_body_msh", _REST, {1: _REST, 40: _POSED})
    server, scene = _scene_with(monkeypatch, mesh_object)

    with pytest.raises(ValueError, match="outside the evaluated mesh's 3 vertices"):
        server.sample_deformed_geometry("CHAR1_body_msh", frame=40, vertex_indices=[0, 9])

    assert scene.frame_current == 1
    assert mesh_object.evaluated.cleared == 1


def test_an_explicit_index_list_is_paged_in_the_order_it_was_given(monkeypatch) -> None:
    """Naming three vertices off a body mesh must not cost a walk through the ones before them."""
    mesh_object = _DeformedMesh("CHAR1_body_msh", _REST, {1: _POSED})
    server, _scene = _scene_with(monkeypatch, mesh_object)

    reply = server.sample_deformed_geometry("CHAR1_body_msh", vertex_indices=[2, 1])

    assert [item["index"] for item in reply["vertices"]["items"]] == [2, 1]
    assert reply["vertices"]["total"] == 2
    with pytest.raises(ValueError, match="Duplicate vertex_indices"):
        server.sample_deformed_geometry("CHAR1_body_msh", vertex_indices=[1, 1])


def test_a_page_reports_the_offset_to_continue_from(monkeypatch) -> None:
    """The envelope's pagination contract: a truncated page names where the next one starts."""
    mesh_object = _DeformedMesh("CHAR1_body_msh", _REST, {1: _POSED})
    server, _scene = _scene_with(monkeypatch, mesh_object)

    first = server.sample_deformed_geometry("CHAR1_body_msh", vertex_limit=2)

    assert first["vertices"]["truncated"] is True
    assert first["vertices"]["next_offset"] == 2
    second = server.sample_deformed_geometry("CHAR1_body_msh", vertex_limit=2, vertex_offset=2)
    assert [item["index"] for item in second["vertices"]["items"]] == [2]
    assert second["vertices"]["truncated"] is False
