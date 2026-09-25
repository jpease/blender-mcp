"""
Which space each geometry-nodes builder reads a referenced object in, and what scales its tube.

An Object Info or Collection Info node reads its source either where it sits in the world
(RELATIVE) or as its own local data (ORIGINAL), and Blender's default is ORIGINAL. A cutter, path,
pivot or proximity target read that way lands at the host object's origin whatever its placement,
so a boolean cuts the same notch wherever the cutter is moved. An instance or profile read
RELATIVE is dragged by its distance from the host instead. `tests/blender_geometry_nodes_transform_smoke.py`
measures both in real Blender; these tests hold the same contract where no Blender runs.

The graph here is a fake that grows a socket the first time a builder asks for it by name, so a
builder is exercised as written without a table of every node type's sockets.
"""

import sys
import types

import pytest

from conftest import load_addon


class _Pending:
    """
    A socket name or identifier not yet asked for, which becomes whatever it is compared to.

    Blender resolves a socket by comparing names while iterating, so claiming on the first
    comparison is how a socket the builder asks for comes to exist under that name.
    """

    def __init__(self, socket, attribute: str) -> None:
        self._socket = socket
        self._attribute = attribute

    def __eq__(self, other):
        self._socket.claim(self._attribute, other)
        return True

    __hash__ = None  # pyright: ignore[reportAssignmentType]


class _Socket:
    def __init__(self, node, sockets) -> None:
        self.node = node
        self._sockets = sockets
        self.name = _Pending(self, "name")
        self.identifier = _Pending(self, "identifier")
        self.bl_idname = "NodeSocket"
        self.default_value = None

    def claim(self, attribute: str, value) -> None:
        setattr(self, attribute, value)
        if self not in self._sockets.claimed:
            self._sockets.claimed.append(self)


class _Sockets:
    """A node's inputs or outputs: every claimed socket, then two not yet asked for."""

    def __init__(self, node) -> None:
        self.node = node
        self.claimed: list[_Socket] = []

    def __iter__(self):
        # Two, because a builder may ask for the second socket of a name before the first.
        return iter([*self.claimed, _Socket(self.node, self), _Socket(self.node, self)])


class _Node:
    def __init__(self, bl_idname: str) -> None:
        self.bl_idname = bl_idname
        self.name = bl_idname
        self.inputs = _Sockets(self)
        self.outputs = _Sockets(self)
        self.is_active_output = bl_idname == "NodeGroupOutput"
        self.properties: dict[str, object] = {}

    def __setitem__(self, key, value) -> None:
        self.properties[key] = value

    def get(self, key, default=None):
        return self.properties.get(key, default)


class _Links(list):
    def new(self, from_socket, to_socket):
        self.append((from_socket, to_socket))
        return self[-1]


class _Nodes(list):
    def new(self, bl_idname: str):
        self.append(_Node(bl_idname))
        return self[-1]


class _Interface:
    def __init__(self) -> None:
        self.items_tree: list[types.SimpleNamespace] = []

    def new_socket(self, *, name, in_out, socket_type, description="", parent=None):
        item = types.SimpleNamespace(name=name, identifier=f"Socket_{len(self.items_tree)}", in_out=in_out)
        self.items_tree.append(item)
        return item


class _Group(dict):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self.nodes = _Nodes([_Node("NodeGroupInput"), _Node("NodeGroupOutput")])
        self.links = _Links()
        self.interface = _Interface()


class _Named(dict):
    def get(self, name, default=None):
        return super().get(name, default)

    def remove(self, datablock, **_kwargs) -> None:
        self.pop(datablock.name, None)


class _AnyType(types.SimpleNamespace):
    """`bpy.types`: every node type this runtime is asked about exists."""

    def __getattr__(self, name):
        return type(name, (), {})


def _load_workflows(monkeypatch, objects: dict, collections: dict):
    addon, bpy = load_addon(
        monkeypatch,
        data={"objects": _Named(objects), "collections": _Named(collections), "node_groups": _Named()},
    )
    # Read when a builder runs, not when the add-on imports, so set after the load.
    bpy.types = _AnyType()
    workflows = sys.modules[f"{addon.__name__}.handlers.geometry_nodes.workflows"]
    built: dict[str, _Group] = {}

    def prepare(object_name, group_name, _builder, _purpose):
        built[group_name] = _Group(group_name)
        return objects[object_name], built[group_name]

    # Validation, tagging, attaching and the reply are not what these tests are about, and each
    # needs a depsgraph; the graph the builder wires between them is.
    monkeypatch.setattr(workflows, "_prepare_builder", prepare)
    monkeypatch.setattr(workflows, "_attach_builder", lambda *_a, **_k: None)
    monkeypatch.setattr(workflows, "_finish_builder", lambda *_a, **_k: {})
    return workflows.GeometryNodesWorkflowHandlersMixin(), built


def _object(name: str, type_: str = "MESH"):
    return types.SimpleNamespace(name=name, type=type_, data=types.SimpleNamespace(polygons=[]))


def _spaces(group: _Group) -> dict[str, str]:
    """Each info node the builder made, by role, to the space it reads its source in."""
    info = {"GeometryNodeObjectInfo", "GeometryNodeCollectionInfo"}
    return {node.name: node.transform_space for node in group.nodes if node.bl_idname in info}


# (id, handler method, arguments, the space each referenced source must be read in)
CASES = [
    (
        "boolean-object-cutter",
        "create_procedural_boolean",
        {"cutter_source": "OBJECT", "cutter_name": "Cutter"},
        {"instance_source": "RELATIVE"},
    ),
    (
        "boolean-collection-cutters",
        "create_procedural_boolean",
        {"cutter_source": "COLLECTION", "cutter_name": "Cutters"},
        {"instance_source": "RELATIVE"},
    ),
    (
        "radial-array-pivot",
        "create_procedural_array",
        {"source_name": "Source", "layout": "RADIAL", "pivot_object_name": "Pivot"},
        {"pivot_source": "RELATIVE", "instance_source": "ORIGINAL"},
    ),
    (
        "curve-array-path",
        "create_procedural_array",
        {"source_name": "Source", "layout": "CURVE", "curve_object_name": "Path"},
        {"curve_source": "RELATIVE", "instance_source": "ORIGINAL"},
    ),
    (
        "curve-generator-path-and-profile",
        "create_curve_generator",
        {"curve_object_name": "Path", "profile_object_name": "Profile"},
        {"curve_source": "RELATIVE", "profile_source": "ORIGINAL"},
    ),
    (
        "proximity-push-target",
        "create_procedural_deformer",
        {"template": "PROXIMITY_PUSH", "target_object_name": "Target"},
        {"target_source": "RELATIVE"},
    ),
    (
        "scatter-instance-source",
        "create_procedural_scatter",
        {"source_type": "OBJECT", "source_name": "Source"},
        {"instance_source": "ORIGINAL"},
    ),
    (
        "paneling-panel-collection",
        "create_surface_paneling",
        {"source_collection_name": "Panels"},
        {"instance_source": "ORIGINAL"},
    ),
]


def _scene():
    objects = {
        "Host": _object("Host"),
        "Cutter": _object("Cutter"),
        "Source": _object("Source"),
        "Pivot": _object("Pivot", "EMPTY"),
        "Path": _object("Path", "CURVE"),
        "Profile": _object("Profile", "CURVE"),
        "Target": _object("Target"),
    }
    collections = {name: types.SimpleNamespace(name=name, objects=[]) for name in ("Cutters", "Panels")}
    return objects, collections


@pytest.mark.parametrize(("method", "arguments", "expected"), [case[1:] for case in CASES], ids=[c[0] for c in CASES])
def test_each_reference_is_read_in_the_space_its_builder_promises(monkeypatch, method, arguments, expected) -> None:
    handler, built = _load_workflows(monkeypatch, *_scene())
    getattr(handler, method)("Host", "Builder", **arguments)
    assert _spaces(built["Builder"]) == expected


def test_curve_generator_scales_its_profile_by_the_curve_radius(monkeypatch) -> None:
    """Curve to Mesh ignores the radius attribute unless its Scale input reads it."""
    handler, built = _load_workflows(monkeypatch, *_scene())
    handler.create_curve_generator("Host", "Builder", curve_object_name="Path", radius=0.5)
    scale_sources = [
        (source.node.bl_idname, source.name)
        for source, target in built["Builder"].links
        if target.node.name == "curve_to_mesh" and target.name == "Scale"
    ]
    assert scale_sources == [("GeometryNodeInputRadius", "Radius")]
