"""Regression coverage for the mesh_*/model_* geometry-editing addon handlers."""

import math
import sys
import types

import pytest

from conftest import load_addon_package


# region Minimal vector/quaternion/matrix math for the mathutils fake
def _cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _quat_mul(a, b):
    w = a.w * b.w - a.x * b.x - a.y * b.y - a.z * b.z
    x = a.w * b.x + a.x * b.w + a.y * b.z - a.z * b.y
    y = a.w * b.y - a.x * b.z + a.y * b.w + a.z * b.x
    z = a.w * b.z + a.x * b.y - a.y * b.x + a.z * b.w
    return FakeQuaternion((w, x, y, z))


def _quat_conjugate(q):
    return FakeQuaternion((q.w, -q.x, -q.y, -q.z))


def _quat_rotate_vec(q, v):
    qv = (q.x, q.y, q.z)
    vv = (v.x, v.y, v.z)
    t = _cross(qv, vv)
    t = (2 * t[0], 2 * t[1], 2 * t[2])
    c2 = _cross(qv, t)
    return FakeVector(
        vv[0] + q.w * t[0] + c2[0],
        vv[1] + q.w * t[1] + c2[1],
        vv[2] + q.w * t[2] + c2[2],
    )


def _euler_to_quat(euler):
    x, y, z = euler.x, euler.y, euler.z
    cx, sx = math.cos(x / 2), math.sin(x / 2)
    cy, sy = math.cos(y / 2), math.sin(y / 2)
    cz, sz = math.cos(z / 2), math.sin(z / 2)
    qx = FakeQuaternion((cx, sx, 0.0, 0.0))
    qy = FakeQuaternion((cy, 0.0, sy, 0.0))
    qz = FakeQuaternion((cz, 0.0, 0.0, sz))
    return _quat_mul(_quat_mul(qz, qy), qx)


def _quat_to_euler(q, _order="XYZ"):
    r20 = 2 * (q.x * q.z - q.w * q.y)
    r21 = 2 * (q.y * q.z + q.w * q.x)
    r22 = 1 - 2 * (q.x * q.x + q.y * q.y)
    r10 = 2 * (q.x * q.y + q.w * q.z)
    r00 = 1 - 2 * (q.y * q.y + q.z * q.z)
    ey = math.asin(max(-1.0, min(1.0, -r20)))
    ex = math.atan2(r21, r22)
    ez = math.atan2(r10, r00)
    return FakeVector(ex, ey, ez)


def _quat_to_axis_angle(q):
    w = max(-1.0, min(1.0, q.w))
    angle = 2 * math.acos(w)
    s = math.sqrt(max(0.0, 1 - w * w))
    if s < 1e-8:
        return FakeVector(1.0, 0.0, 0.0), 0.0
    return FakeVector(q.x / s, q.y / s, q.z / s), angle


def _compose(a, b):
    """Compose two TRS matrices: a's transform applied to b (a is the outer/parent transform)."""
    scaled_b_loc = FakeVector(b.loc.x * a.scale.x, b.loc.y * a.scale.y, b.loc.z * a.scale.z)
    rotated = _quat_rotate_vec(a.rot, scaled_b_loc)
    new_loc = FakeVector(rotated.x + a.loc.x, rotated.y + a.loc.y, rotated.z + a.loc.z)
    new_rot = _quat_mul(a.rot, b.rot)
    new_scale = FakeVector(a.scale.x * b.scale.x, a.scale.y * b.scale.y, a.scale.z * b.scale.z)
    return FakeMatrix(new_loc, new_rot, new_scale)


# endregion


class FakeVector:
    def __init__(self, x=0.0, y=0.0, z=0.0) -> None:
        self.x, self.y, self.z = x, y, z

    def copy(self):
        return FakeVector(self.x, self.y, self.z)

    def to_quaternion(self):
        return _euler_to_quat(self)

    def __neg__(self):
        return FakeVector(-self.x, -self.y, -self.z)

    def __iter__(self):
        return iter((self.x, self.y, self.z))

    def __eq__(self, other):
        return tuple(self) == tuple(other)


_EULER_ORDERS = {"XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX"}


class FakeQuaternion:
    def __init__(self, *args) -> None:
        if not args:
            self.w, self.x, self.y, self.z = 1.0, 0.0, 0.0, 0.0
        elif len(args) == 1:
            self.w, self.x, self.y, self.z = args[0]
        elif len(args) == 2:
            axis, angle = args
            ax, ay, az = axis
            norm = math.sqrt(ax * ax + ay * ay + az * az) or 1.0
            ax, ay, az = ax / norm, ay / norm, az / norm
            s = math.sin(angle / 2)
            self.w, self.x, self.y, self.z = math.cos(angle / 2), ax * s, ay * s, az * s
        else:
            raise TypeError("Quaternion accepts 0, 1, or 2 positional args")

    def copy(self):
        return FakeQuaternion((self.w, self.x, self.y, self.z))

    def to_euler(self, order="XYZ"):
        if order not in _EULER_ORDERS:
            raise ValueError(f"Euler order {order!r} not in {sorted(_EULER_ORDERS)}")
        return _quat_to_euler(self, order)

    def to_axis_angle(self):
        return _quat_to_axis_angle(self)

    def __eq__(self, other):
        return (self.w, self.x, self.y, self.z) == (other.w, other.x, other.y, other.z)


class FakeMatrix:
    def __init__(self, loc, rot, scale) -> None:
        self.loc, self.rot, self.scale = loc, rot, scale

    @property
    def translation(self):
        return self.loc.copy()

    def decompose(self):
        return self.loc.copy(), self.rot.copy(), self.scale.copy()

    def __matmul__(self, other):
        if isinstance(other, FakeMatrix):
            return _compose(self, other)
        point = other
        scaled = FakeVector(point.x * self.scale.x, point.y * self.scale.y, point.z * self.scale.z)
        rotated = _quat_rotate_vec(self.rot, scaled)
        return FakeVector(rotated.x + self.loc.x, rotated.y + self.loc.y, rotated.z + self.loc.z)

    @staticmethod
    def LocRotScale(loc, rot, scale):
        return FakeMatrix(loc, rot, scale)

    @staticmethod
    def Translation(vec):
        return FakeMatrix(FakeVector(vec.x, vec.y, vec.z), FakeQuaternion(), FakeVector(1.0, 1.0, 1.0))

    @staticmethod
    def Rotation(angle, size, axis):
        axis_vec = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}[axis]
        return FakeMatrix(FakeVector(0.0, 0.0, 0.0), FakeQuaternion(axis_vec, angle), FakeVector(1.0, 1.0, 1.0))


class FakeVertex:
    def __init__(self, co) -> None:
        self.co = co


class FakeMeshData:
    def __init__(self, n_verts=8, n_edges=12, n_polys=6) -> None:
        self.vertices = [FakeVertex(FakeVector(float(i), 0.0, 0.0)) for i in range(n_verts)]
        self.edges = [object() for _ in range(n_edges)]
        self.polygons = [object() for _ in range(n_polys)]
        self.remesh_voxel_size = 0.1


class FakeModifier:
    def __init__(self, name, type) -> None:
        self.name = name
        self.type = type


class FakeModifiers(list):
    def new(self, name, type):
        mod = FakeModifier(name, type)
        self.append(mod)
        return mod

    def get(self, name):
        return next((m for m in self if m.name == name), None)


class FakeObject:
    def __init__(self, name, obj_type, selected_objects=None) -> None:
        self._name = name
        self._collection = None
        self.type = obj_type
        self.location = FakeVector()
        self.rotation_euler = FakeVector()
        self.rotation_mode = "XYZ"
        self.rotation_quaternion = FakeQuaternion()
        self.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)
        self.scale = FakeVector(1.0, 1.0, 1.0)
        self._dimensions = FakeVector(1.0, 1.0, 1.0)
        self.parent = None
        self.data = FakeMeshData() if obj_type == "MESH" else None
        self.modifiers = FakeModifiers()
        self.material_slots = []
        self._custom = {}
        self.selected = False
        self.mode = "OBJECT"
        self._selected_objects = selected_objects
        self.editmode_sync_calls = 0

    def update_from_editmode(self) -> bool:
        self.editmode_sync_calls += 1
        return False

    def _local_matrix(self):
        if self.rotation_mode == "QUATERNION":
            rot = self.rotation_quaternion.copy()
        elif self.rotation_mode == "AXIS_ANGLE":
            angle, x, y, z = self.rotation_axis_angle
            rot = FakeQuaternion((x, y, z), angle)
        else:
            rot = _euler_to_quat(self.rotation_euler)
        return FakeMatrix(self.location.copy(), rot, self.scale.copy())

    @property
    def matrix_world(self):
        local = self._local_matrix()
        if self.parent is None:
            return local
        return _compose(self.parent.matrix_world, local)

    @matrix_world.setter
    def matrix_world(self, mat) -> None:
        if self.parent is None:
            loc, rot, scale = mat.decompose()
        else:
            a = self.parent.matrix_world
            diff = FakeVector(mat.loc.x - a.loc.x, mat.loc.y - a.loc.y, mat.loc.z - a.loc.z)
            unscaled = FakeVector(diff.x / a.scale.x, diff.y / a.scale.y, diff.z / a.scale.z)
            a_conj = _quat_conjugate(a.rot)
            loc = _quat_rotate_vec(a_conj, unscaled)
            rot = _quat_mul(a_conj, mat.rot)
            scale = FakeVector(mat.scale.x / a.scale.x, mat.scale.y / a.scale.y, mat.scale.z / a.scale.z)
        self.location = loc
        self.scale = scale
        if self.rotation_mode == "QUATERNION":
            self.rotation_quaternion = rot
        elif self.rotation_mode == "AXIS_ANGLE":
            axis, angle = _quat_to_axis_angle(rot)
            self.rotation_axis_angle = (angle, axis.x, axis.y, axis.z)
        else:
            self.rotation_euler = _quat_to_euler(rot, self.rotation_mode)

    @property
    def dimensions(self):
        return self._dimensions

    @dimensions.setter
    def dimensions(self, value) -> None:
        self._dimensions = FakeVector(*value)

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value) -> None:
        if self._collection is not None and self._name in self._collection:
            del self._collection[self._name]
            self._collection[value] = self
        self._name = value

    def evaluated_get(self, _depsgraph):
        return self

    def select_set(self, value) -> None:
        self.selected = value
        if self._selected_objects is None:
            return
        if value:
            if self not in self._selected_objects:
                self._selected_objects.append(self)
        elif self in self._selected_objects:
            self._selected_objects.remove(self)

    def visible_get(self) -> bool:
        return True

    def __setitem__(self, key, value) -> None:
        self._custom[key] = value

    def __getitem__(self, key):
        return self._custom[key]


class FakeObjectsCollection(dict):
    def __init__(self, selected_objects) -> None:
        super().__init__()
        self._selected_objects = selected_objects

    def __iter__(self):
        # Real bpy_prop_collection iteration yields datablocks, not their names/keys.
        return iter(list(self.values()))

    def get(self, name, default=None):
        return dict.get(self, name, default)

    def new(self, name, data):
        obj = FakeObject(name, "MESH" if data is not None else "EMPTY", selected_objects=self._selected_objects)
        obj._collection = self
        self[obj.name] = obj
        return obj

    def remove(self, obj, do_unlink=True) -> None:
        self.pop(obj.name, None)
        if obj in self._selected_objects:
            self._selected_objects.remove(obj)


class FakeTexturesCollection(dict):
    def get(self, name, default=None):
        return dict.get(self, name, default)

    def new(self, name, type):
        tex = types.SimpleNamespace(name=name, type=type, noise_scale=0.0)
        self[name] = tex
        return tex

    def remove(self, tex, do_unlink=True) -> None:
        self.pop(tex.name, None)


class FakeBMElem:
    def __init__(self) -> None:
        self.select = False


class FakeBMElemSeq(list):
    def ensure_lookup_table(self) -> None:
        pass


class FakeBMesh:
    def __init__(self, mesh_data) -> None:
        self.verts = FakeBMElemSeq(FakeBMElem() for _ in mesh_data.vertices)
        self.edges = FakeBMElemSeq(FakeBMElem() for _ in mesh_data.edges)
        self.faces = FakeBMElemSeq(FakeBMElem() for _ in mesh_data.polygons)

    def select_flush(self, _value) -> None:
        pass

    def select_flush_mode(self) -> None:
        pass


def _load_addon(monkeypatch):
    scene = types.SimpleNamespace(
        blendermcp_use_polyhaven=False,
        blendermcp_use_sketchfab=False,
        cursor=types.SimpleNamespace(location=FakeVector()),
    )

    selected_objects = []
    objects = FakeObjectsCollection(selected_objects)
    textures = FakeTexturesCollection()
    primitive_counter = {"n": 0}

    def _make_primitive_op(prefix, obj_type="MESH"):
        def op(**kwargs):
            primitive_counter["n"] += 1
            name = f"{prefix}.{primitive_counter['n']:03d}"
            obj = FakeObject(name, obj_type, selected_objects=selected_objects)
            obj._collection = objects
            location = kwargs.get("location", (0, 0, 0))
            rotation = kwargs.get("rotation", (0, 0, 0))
            obj.location = FakeVector(*location)
            obj.rotation_euler = FakeVector(*rotation)
            objects[name] = obj
            bpy.context.view_layer.objects.active = obj
            bpy.context.active_object = obj
            return {"FINISHED"}

        return op

    def _noop(**_kwargs):
        return {"FINISHED"}

    def _mode_set(mode) -> None:
        bpy.context.mode = mode
        active = bpy.context.view_layer.objects.active
        if active is not None:
            active.mode = mode

    def _modifier_apply(modifier) -> None:
        obj = bpy.context.view_layer.objects.active
        mod = obj.modifiers.get(modifier)
        if mod is not None:
            obj.modifiers.remove(mod)

    def _select_all(action="SELECT") -> None:
        if action == "DESELECT":
            for obj in list(selected_objects):
                obj.select_set(False)
        elif action == "SELECT":
            for obj in objects.values():
                obj.select_set(True)

    bpy = types.ModuleType("bpy")
    bpy.data = types.SimpleNamespace(
        objects=objects,
        textures=textures,
    )
    bpy.context = types.SimpleNamespace(
        scene=scene,
        mode="OBJECT",
        selected_objects=selected_objects,
        view_layer=types.SimpleNamespace(objects=types.SimpleNamespace(active=None)),
        active_object=None,
        collection=types.SimpleNamespace(objects=types.SimpleNamespace(link=lambda _obj: None)),
        evaluated_depsgraph_get=object,
        tool_settings=types.SimpleNamespace(mesh_select_mode=(True, False, False)),
    )
    bpy.types = types.SimpleNamespace(
        AddonPreferences=object,
        Operator=object,
        Panel=object,
        Scene=type("Scene", (), {}),
    )
    bpy.ops = types.SimpleNamespace(
        mesh=types.SimpleNamespace(
            primitive_cube_add=_make_primitive_op("Cube"),
            primitive_uv_sphere_add=_make_primitive_op("Sphere"),
            primitive_cylinder_add=_make_primitive_op("Cylinder"),
            primitive_cone_add=_make_primitive_op("Cone"),
            primitive_torus_add=_make_primitive_op("Torus"),
            primitive_plane_add=_make_primitive_op("Plane"),
            extrude_region_move=_noop,
            inset_faces=_noop,
            bevel=_noop,
            bridge_edge_loops=_noop,
            subdivide=_noop,
            symmetrize=_noop,
        ),
        curve=types.SimpleNamespace(
            primitive_bezier_curve_add=_make_primitive_op("BezierCurve", obj_type="CURVE"),
        ),
        object=types.SimpleNamespace(
            select_all=_select_all,
            mode_set=_mode_set,
            modifier_apply=_modifier_apply,
            shade_smooth=_noop,
            voxel_remesh=_noop,
        ),
        nd=types.SimpleNamespace(),
    )

    props = types.ModuleType("bpy.props")
    for name in (
        "BoolProperty",
        "EnumProperty",
        "FloatProperty",
        "IntProperty",
        "StringProperty",
    ):
        setattr(props, name, lambda **_kwargs: None)
    bpy.props = props

    handlers = types.ModuleType("bpy.app.handlers")
    handlers.persistent = lambda fn: fn
    handlers.undo_post = []
    handlers.redo_post = []
    handlers.depsgraph_update_post = []

    app = types.ModuleType("bpy.app")
    app.version = (4, 2, 0)
    app.version_string = "4.2.0"
    app.background = False
    app.handlers = handlers
    app.timers = types.SimpleNamespace(
        is_registered=lambda *_a, **_k: False,
        register=lambda *_a, **_k: None,
        unregister=lambda *_a, **_k: None,
    )
    bpy.app = app

    bmesh = types.ModuleType("bmesh")
    bmesh.from_edit_mesh = FakeBMesh
    bmesh.update_edit_mesh = lambda _mesh_data: None

    mathutils = types.ModuleType("mathutils")
    mathutils.Vector = lambda seq: FakeVector(*seq)
    mathutils.Quaternion = FakeQuaternion
    mathutils.Matrix = types.SimpleNamespace(
        LocRotScale=FakeMatrix.LocRotScale,
        Translation=FakeMatrix.Translation,
        Rotation=FakeMatrix.Rotation,
    )

    monkeypatch.setitem(sys.modules, "bpy", bpy)
    monkeypatch.setitem(sys.modules, "bpy.props", props)
    monkeypatch.setitem(sys.modules, "bpy.app", app)
    monkeypatch.setitem(sys.modules, "bpy.app.handlers", handlers)
    monkeypatch.setitem(sys.modules, "mathutils", mathutils)
    monkeypatch.setitem(sys.modules, "bmesh", bmesh)

    requests = types.ModuleType("requests")
    requests.utils = types.SimpleNamespace(default_headers=dict)
    requests.exceptions = types.SimpleNamespace(Timeout=TimeoutError)
    monkeypatch.setitem(sys.modules, "requests", requests)

    addon = load_addon_package(monkeypatch, "blender_mcp_addon_mesh_model_test")
    return addon, bpy


def _new_mesh_object(bpy, name):
    obj = bpy.data.objects.new(name, object())
    return obj


def _new_empty_object(bpy, name):
    obj = bpy.data.objects.new(name, None)
    return obj


@pytest.mark.parametrize(
    "primitive_type,expected_obj_type",
    [
        ("CUBE", "MESH"),
        ("SPHERE", "MESH"),
        ("CYLINDER", "MESH"),
        ("CONE", "MESH"),
        ("TORUS", "MESH"),
        ("PLANE", "MESH"),
        ("CURVE", "CURVE"),
        ("cube", "MESH"),
    ],
)
def test_create_primitive_dispatches_to_the_right_op(monkeypatch, primitive_type, expected_obj_type) -> None:
    addon, _bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()

    result = server.create_primitive(primitive_type=primitive_type, name=f"obj_{primitive_type}")

    assert result["name"] == f"obj_{primitive_type}"
    assert result["type"] == expected_obj_type


def test_create_primitive_rejects_unknown_type(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()

    with pytest.raises(ValueError, match="Unknown primitive_type"):
        server.create_primitive(primitive_type="DODECAHEDRON")


MESH_HANDLER_CALLS = [
    ("mesh_extrude", {}),
    ("mesh_inset", {}),
    ("mesh_bevel", {}),
    ("mesh_bridge", {"edge_indices": [0, 1]}),
    ("mesh_subdivide", {}),
    ("mesh_remesh", {}),
    ("mesh_solidify", {}),
    ("mesh_symmetrize", {}),
    ("add_radial_array_modifier", {"radius": 2.0}),
]


@pytest.mark.parametrize("handler_name,extra_kwargs", MESH_HANDLER_CALLS)
def test_mesh_handlers_reject_missing_object(monkeypatch, handler_name, extra_kwargs) -> None:
    addon, _bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()

    with pytest.raises(ValueError, match="Object not found"):
        getattr(server, handler_name)(object_name="does_not_exist", **extra_kwargs)


@pytest.mark.parametrize("handler_name,extra_kwargs", MESH_HANDLER_CALLS)
def test_mesh_handlers_reject_non_mesh_object(monkeypatch, handler_name, extra_kwargs) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_empty_object(bpy, "empty_obj")

    with pytest.raises(ValueError, match="is not a mesh"):
        getattr(server, handler_name)(object_name="empty_obj", **extra_kwargs)


def test_mesh_extrude_rejects_out_of_range_face_index_before_entering_edit_mode(
    monkeypatch,
) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_mesh_object(bpy, "F")

    mode_calls = []
    monkeypatch.setattr(bpy.ops.object, "mode_set", mode_calls.append)

    with pytest.raises(ValueError, match="out of range"):
        server.mesh_extrude(object_name="F", face_indices=[999])

    # Validation happens before edit mode is entered - mode_set is never called.
    assert mode_calls == []


def test_mesh_extrude_restores_object_mode_when_operator_fails(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_mesh_object(bpy, "F2")

    mode_calls = []
    monkeypatch.setattr(bpy.ops.object, "mode_set", lambda mode: mode_calls.append(mode))
    monkeypatch.setattr(bpy.ops.mesh, "extrude_region_move", lambda **kwargs: {"CANCELLED"})

    with pytest.raises(RuntimeError, match="did not finish"):
        server.mesh_extrude(object_name="F2")

    # Edit mode is entered, the operator fails, but exit still happens via finally.
    assert mode_calls == ["EDIT", "OBJECT"]


def test_mesh_extrude_restores_prior_active_selection_and_mode(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    other = _new_mesh_object(bpy, "Other")
    _new_mesh_object(bpy, "F3")

    other.select_set(True)
    bpy.context.view_layer.objects.active = other
    other.mode = "EDIT"
    bpy.context.mode = "EDIT"

    server.mesh_extrude(object_name="F3")

    assert bpy.context.view_layer.objects.active is other
    assert other.selected is True
    assert other.mode == "EDIT"
    assert bpy.context.mode == "EDIT"


def test_create_primitive_normalizes_to_object_mode_first(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    other = _new_mesh_object(bpy, "Other")
    bpy.context.view_layer.objects.active = other
    other.mode = "EDIT"
    bpy.context.mode = "EDIT"

    mode_calls = []
    monkeypatch.setattr(bpy.ops.object, "mode_set", lambda mode: mode_calls.append(mode))

    server.create_primitive(primitive_type="CUBE", name="new_cube")

    # The wrapped block normalizes to Object Mode before the primitive-add op runs.
    assert mode_calls[0] == "OBJECT"


def test_mesh_boolean_rejects_invalid_operation(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_mesh_object(bpy, "target")
    _new_mesh_object(bpy, "cutter")

    with pytest.raises(ValueError, match="Invalid operation"):
        server.mesh_boolean(object_name="target", cutter_object_name="cutter", operation="XOR")


def test_mesh_boolean_rejects_same_object(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_mesh_object(bpy, "self_obj")

    with pytest.raises(ValueError, match="must differ"):
        server.mesh_boolean(object_name="self_obj", cutter_object_name="self_obj")

    assert bpy.data.objects.get("self_obj") is not None


def test_mesh_boolean_keeps_cutter_by_default(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_mesh_object(bpy, "target")
    _new_mesh_object(bpy, "cutter")

    result = server.mesh_boolean(object_name="target", cutter_object_name="cutter")

    assert result["name"] == "target"
    assert bpy.data.objects.get("cutter") is not None


def test_mesh_boolean_deletes_cutter_when_requested(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_mesh_object(bpy, "target2")
    _new_mesh_object(bpy, "cutter2")

    server.mesh_boolean(object_name="target2", cutter_object_name="cutter2", keep_cutter=False)

    assert bpy.data.objects.get("cutter2") is None


def test_copy_object_transform_copies_only_flagged_components(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    obj = _new_mesh_object(bpy, "A")
    obj.location = FakeVector(1, 2, 3)
    obj.rotation_euler = FakeVector(0, 0, 0)
    obj.scale = FakeVector(1, 1, 1)
    ref = _new_mesh_object(bpy, "B")
    ref.location = FakeVector(4, 5, 6)
    ref.rotation_euler = FakeVector(0.1, 0.2, 0.3)
    ref.scale = FakeVector(2, 2, 2)

    result = server.copy_object_transform(
        object_name="A",
        reference_object_name="B",
        match_location=True,
        match_rotation=False,
        match_scale=True,
    )

    assert result["location"] == pytest.approx([4, 5, 6])
    assert obj.rotation_euler.x == pytest.approx(0)
    assert obj.rotation_euler.y == pytest.approx(0)
    assert obj.rotation_euler.z == pytest.approx(0)
    assert result["scale"] == pytest.approx([2, 2, 2])


def test_copy_object_transform_rejects_invalid_space(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_mesh_object(bpy, "A")
    _new_mesh_object(bpy, "B")

    with pytest.raises(ValueError, match="Invalid space"):
        server.copy_object_transform(object_name="A", reference_object_name="B", space="OBJECT")


def test_copy_object_transform_local_space_copies_quaternion_directly(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    obj = _new_mesh_object(bpy, "A")
    obj.rotation_mode = "QUATERNION"
    ref = _new_mesh_object(bpy, "B")
    ref.rotation_mode = "QUATERNION"
    ref.rotation_quaternion = FakeQuaternion((0.5, 0.5, 0.5, 0.5))

    result = server.copy_object_transform(
        object_name="A",
        reference_object_name="B",
        match_location=False,
        match_rotation=True,
        match_scale=False,
        space="LOCAL",
    )

    assert obj.rotation_quaternion == FakeQuaternion((0.5, 0.5, 0.5, 0.5))
    # Regression: formatting the result used to call to_euler("QUATERNION"),
    # which real Blender rejects - it must return the native [w, x, y, z] form.
    assert result["rotation_mode"] == "QUATERNION"
    assert result["rotation"] == pytest.approx([0.5, 0.5, 0.5, 0.5])


def test_copy_object_transform_world_space_returns_native_rotation_for_quaternion_mode(
    monkeypatch,
) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    obj = _new_mesh_object(bpy, "A")
    obj.rotation_mode = "QUATERNION"
    ref = _new_mesh_object(bpy, "B")
    ref.rotation_mode = "QUATERNION"
    ref.rotation_quaternion = FakeQuaternion((0.5, 0.5, 0.5, 0.5))

    # Regression: the WORLD branch recomposes matrix_world via LocRotScale,
    # then reused the same to_euler(obj.rotation_mode) formatting - the crash
    # reproduces on this path too, not just LOCAL.
    result = server.copy_object_transform(
        object_name="A",
        reference_object_name="B",
        match_rotation=True,
        space="WORLD",
    )

    assert result["rotation_mode"] == "QUATERNION"
    assert result["rotation"] == pytest.approx([0.5, 0.5, 0.5, 0.5])


def test_copy_object_transform_returns_native_rotation_for_axis_angle_mode(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    obj = _new_mesh_object(bpy, "A")
    obj.rotation_mode = "AXIS_ANGLE"
    ref = _new_mesh_object(bpy, "B")
    ref.rotation_mode = "AXIS_ANGLE"
    ref.rotation_axis_angle = (1.2, 0.0, 0.0, 1.0)

    result = server.copy_object_transform(
        object_name="A",
        reference_object_name="B",
        match_location=False,
        match_rotation=True,
        match_scale=False,
        space="LOCAL",
    )

    assert result["rotation_mode"] == "AXIS_ANGLE"
    assert result["rotation"] == pytest.approx([1.2, 0.0, 0.0, 1.0])


def test_copy_object_transform_world_space_differs_from_local_across_parenting(
    monkeypatch,
) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()

    parent_a = _new_mesh_object(bpy, "ParentA")
    parent_a.location = FakeVector(100, 0, 0)
    obj = _new_mesh_object(bpy, "A")
    obj.location = FakeVector(0, 0, 0)
    obj.parent = parent_a

    parent_b = _new_mesh_object(bpy, "ParentB")
    parent_b.location = FakeVector(0, 0, 0)
    ref = _new_mesh_object(bpy, "B")
    ref.location = FakeVector(5, 5, 5)
    ref.parent = parent_b

    # ref's world position is (5, 5, 5); WORLD-space matching must land obj
    # there too, even though obj is parented 100 units away on X.
    result = server.copy_object_transform(
        object_name="A",
        reference_object_name="B",
        match_rotation=False,
        match_scale=False,
        space="WORLD",
    )

    world_loc = obj.matrix_world.loc
    assert [world_loc.x, world_loc.y, world_loc.z] == pytest.approx([5, 5, 5])
    # The returned/local location differs from world location because of the
    # parent offset - this is the whole point of matching in WORLD space.
    assert result["location"] == pytest.approx([-95, 5, 5])
    # The new world_location field labels the world-space equivalent, so a
    # caller doesn't have to guess which space "location" is in.
    assert result["world_location"] == pytest.approx([5, 5, 5])


def test_create_primitive_blockout_dimensions_consistent_across_primitive_types(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()

    cube_result = server.create_primitive(
        name="blockout_cube",
        primitive_type="CUBE",
        dimensions=(2, 2, 2),
        purpose="blockout",
    )
    sphere_result = server.create_primitive(
        name="blockout_sphere",
        primitive_type="SPHERE",
        dimensions=(2, 2, 2),
        purpose="blockout",
    )

    assert cube_result["dimensions"] == pytest.approx([2, 2, 2])
    assert sphere_result["dimensions"] == pytest.approx([2, 2, 2])
    assert bpy.data.objects["blockout_cube"]["blockout"] is True


def test_add_radial_array_modifier_requires_a_pivot(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_mesh_object(bpy, "R")

    with pytest.raises(ValueError, match="pivot"):
        server.add_radial_array_modifier(object_name="R", count=4, axis="Z")


def test_add_radial_array_modifier_rejects_multiple_pivot_options(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_mesh_object(bpy, "R")

    with pytest.raises(ValueError, match="at most one"):
        server.add_radial_array_modifier(object_name="R", count=4, radius=2.0, pivot_location=(1, 0, 0))


def _pivot_rotation_matrix(pivot, axis, angle):
    # The fake mirrors mathutils.Matrix, whose `@` yields a Matrix or a Vector
    # depending on the operand, so the chained product reads as a union.
    return FakeMatrix.Translation(pivot) @ FakeMatrix.Rotation(angle, 4, axis) @ FakeMatrix.Translation(-pivot)  # pyright: ignore[reportOperatorIssue]


def _assert_matrices_close(a, b) -> None:
    assert (a.loc.x, a.loc.y, a.loc.z) == pytest.approx((b.loc.x, b.loc.y, b.loc.z))
    assert (a.rot.w, a.rot.x, a.rot.y, a.rot.z) == pytest.approx((b.rot.w, b.rot.x, b.rot.y, b.rot.z))
    assert (a.scale.x, a.scale.y, a.scale.z) == pytest.approx((b.scale.x, b.scale.y, b.scale.z))


def test_add_radial_array_modifier_with_radius_offsets_pivot(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    obj = _new_mesh_object(bpy, "R")

    result = server.add_radial_array_modifier(object_name="R", count=4, axis="Z", radius=3.0)

    assert result["applied"] is False
    empty = bpy.data.objects.get("R_radial_pivot")
    assert empty is not None
    # axis="Z" offsets along its perpendicular axis, X (see _RADIAL_AXIS_PERP).
    pivot = FakeVector(-3.0, 0.0, 0.0)
    angle = 2 * math.pi / 4
    expected = _pivot_rotation_matrix(pivot, "Z", angle) @ obj.matrix_world
    _assert_matrices_close(empty.matrix_world, expected)


def test_add_radial_array_modifier_with_radius_uses_world_space_pivot_for_parented_object(
    monkeypatch,
) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()

    parent = _new_mesh_object(bpy, "Parent")
    parent.location = FakeVector(100, 0, 0)
    obj = _new_mesh_object(bpy, "R")
    obj.location = FakeVector(0, 0, 0)
    obj.parent = parent

    # obj's world location is (100, 0, 0) even though its local location is
    # (0, 0, 0) - the pivot must be offset from the world position, not the
    # parent-local one, or every rotated copy would land on top of the parent.
    server.add_radial_array_modifier(object_name="R", count=4, axis="Z", radius=3.0)

    empty = bpy.data.objects.get("R_radial_pivot")
    assert empty is not None
    pivot = FakeVector(97.0, 0.0, 0.0)
    angle = 2 * math.pi / 4
    expected = _pivot_rotation_matrix(pivot, "Z", angle) @ obj.matrix_world
    _assert_matrices_close(empty.matrix_world, expected)


def test_add_radial_array_modifier_with_explicit_pivot_location(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    obj = _new_mesh_object(bpy, "R")

    server.add_radial_array_modifier(object_name="R", count=6, axis="Z", pivot_location=(5, 5, 5))

    empty = bpy.data.objects.get("R_radial_pivot")
    assert empty is not None
    pivot = FakeVector(5, 5, 5)
    angle = 2 * math.pi / 6
    expected = _pivot_rotation_matrix(pivot, "Z", angle) @ obj.matrix_world
    _assert_matrices_close(empty.matrix_world, expected)


def test_add_radial_array_modifier_with_pivot_object(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    obj = _new_mesh_object(bpy, "R")
    pivot_obj = _new_mesh_object(bpy, "Pivot")
    pivot_obj.location = FakeVector(1, 2, 3)

    server.add_radial_array_modifier(object_name="R", count=6, pivot_object_name="Pivot")

    empty = bpy.data.objects.get("R_radial_pivot")
    assert empty is not None
    pivot = FakeVector(1, 2, 3)
    angle = 2 * math.pi / 6
    expected = _pivot_rotation_matrix(pivot, "Z", angle) @ obj.matrix_world
    _assert_matrices_close(empty.matrix_world, expected)


def test_add_radial_array_modifier_rotates_a_rotated_scaled_object_about_an_arbitrary_pivot(
    monkeypatch,
) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    obj = _new_mesh_object(bpy, "R")
    obj.location = FakeVector(10, 0, 0)
    obj.rotation_euler = FakeVector(0.0, 0.0, math.radians(30))
    obj.scale = FakeVector(2.0, 2.0, 2.0)

    server.add_radial_array_modifier(object_name="R", count=4, axis="Z", pivot_location=(0, 0, 0))

    empty = bpy.data.objects.get("R_radial_pivot")
    assert empty is not None
    pivot = FakeVector(0.0, 0.0, 0.0)
    angle = 2 * math.pi / 4
    expected = _pivot_rotation_matrix(pivot, "Z", angle) @ obj.matrix_world
    _assert_matrices_close(empty.matrix_world, expected)


def test_add_radial_array_modifier_rejects_unknown_pivot_object(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_mesh_object(bpy, "R")

    with pytest.raises(ValueError, match="Pivot object not found"):
        server.add_radial_array_modifier(object_name="R", pivot_object_name="missing")


def test_add_radial_array_modifier_cleans_up_helper_empty_when_applied(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    _new_mesh_object(bpy, "R2")

    server.add_radial_array_modifier(object_name="R2", count=6, axis="Z", apply=True, radius=2.0)

    assert bpy.data.objects.get("R2_radial_pivot") is None


def test_nd_single_vertex_reports_the_created_object_by_diff(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()

    def _single_vertex(**_kwargs):
        obj = bpy.data.objects.new("Vertex.001", object())
        obj.location = FakeVector(*bpy.context.scene.cursor.location)
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        return {"FINISHED"}

    monkeypatch.setattr(bpy.ops.nd, "single_vertex", _single_vertex, raising=False)

    result = server.nd_single_vertex(location=(1.0, 2.0, 3.0))

    assert result["name"] == "Vertex.001"
    assert result["location"] == [1.0, 2.0, 3.0]
    assert result["cancelled"] is False


def test_nd_single_vertex_cancelled_with_no_active_object_returns_none_without_raising(
    monkeypatch,
) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()

    monkeypatch.setattr(bpy.ops.nd, "single_vertex", lambda **_kwargs: {"CANCELLED"}, raising=False)

    # No pre-existing active/selected object - the old active-object dereference would raise here.
    result = server.nd_single_vertex(location=(1.0, 2.0, 3.0))

    assert result == {"name": None, "location": None, "cancelled": True}


def test_nd_single_vertex_cancelled_does_not_report_stale_active_object(monkeypatch) -> None:
    addon, bpy = _load_addon(monkeypatch)
    server = addon.BlenderMCPServer()
    prior = _new_mesh_object(bpy, "Prior")
    prior.select_set(True)
    bpy.context.view_layer.objects.active = prior

    monkeypatch.setattr(bpy.ops.nd, "single_vertex", lambda **_kwargs: {"CANCELLED"}, raising=False)

    result = server.nd_single_vertex(location=(1.0, 2.0, 3.0))

    assert result["name"] is None
    assert result["cancelled"] is True
    # The pre-existing active object is restored, not reported as the new vertex.
    assert bpy.context.view_layer.objects.active is prior
