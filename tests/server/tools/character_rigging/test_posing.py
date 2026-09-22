"""
Regression coverage for pose resolution, goal-directed aims, and pose keyframing.

`mathutils` is a C extension that exists only inside Blender, so the geometry here runs against
`_math`, an independent implementation of the handful of vector, matrix and quaternion
operations the handler calls. That proves the geometry the handler asks for - which column
lands on the aim direction, which bone sees its parent's fresh transform - not Blender's
numerics; `tests/blender_character_posing_smoke.py` measures those against the real API.
"""

import asyncio
import math
import sys
import types

import pytest

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError
from pydantic_core import to_json
from test_mutation_transaction import _load_addon

from blender_mcp.server.tools import _dispatch, character_rigging
from blender_mcp.server.tools.envelope import REPLY_BYTE_BUDGET, envelope_for


class _Vector:
    """A 3-component vector with the operations the pose handlers use."""

    def __init__(self, values) -> None:
        self.values = [float(value) for value in values]

    def __iter__(self):
        return iter(self.values)

    def __len__(self) -> int:
        return len(self.values)

    def __getitem__(self, index) -> float:
        return self.values[index]

    def __setitem__(self, index, value) -> None:
        self.values[index] = float(value)

    def __repr__(self) -> str:
        return f"_Vector({self.values})"

    def __sub__(self, other) -> "_Vector":
        return _Vector(left - right for left, right in zip(self.values, other, strict=True))

    def __add__(self, other) -> "_Vector":
        return _Vector(left + right for left, right in zip(self.values, other, strict=True))

    def __mul__(self, scalar) -> "_Vector":
        return _Vector(value * float(scalar) for value in self.values)

    __rmul__ = __mul__

    @property
    def length(self) -> float:
        return math.sqrt(sum(value * value for value in self.values))

    def copy(self) -> "_Vector":
        return _Vector(self.values)

    def normalize(self) -> None:
        length = self.length
        self.values = [value / length for value in self.values]

    def normalized(self) -> "_Vector":
        length = self.length
        return _Vector(value / length for value in self.values)

    def dot(self, other) -> float:
        return sum(left * right for left, right in zip(self.values, other, strict=True))

    def cross(self, other) -> "_Vector":
        x, y, z = self.values
        ox, oy, oz = list(other)
        return _Vector((y * oz - z * oy, z * ox - x * oz, x * oy - y * ox))

    def angle(self, other, fallback: float = 0.0) -> float:
        if not self.length or not _Vector(other).length:
            return fallback
        cosine = self.normalized().dot(_Vector(other).normalized())
        return math.acos(max(-1.0, min(1.0, cosine)))

    def rotation_difference(self, other) -> "_Quaternion":
        start, end = self.normalized(), _Vector(other).normalized()
        cosine = start.dot(end)
        if cosine < -1.0 + 1e-12:
            # Antiparallel: any perpendicular axis is a half turn, which is what Blender picks.
            axis = start.cross(_Vector((1.0, 0.0, 0.0)))
            if axis.length < 1e-6:
                axis = start.cross(_Vector((0.0, 1.0, 0.0)))
            return _Quaternion((0.0, *axis.normalized()))
        axis = start.cross(end)
        return _Quaternion((1.0 + cosine, *axis)).normalized()


class _Quaternion:
    """A `(w, x, y, z)` rotation, constructible from components or an axis and an angle."""

    def __init__(self, values, angle=None) -> None:
        if angle is None:
            self.values = [float(value) for value in values]
        else:
            axis = _Vector(values).normalized()
            half = float(angle) / 2.0
            self.values = [math.cos(half), *[component * math.sin(half) for component in axis]]

    def __iter__(self):
        return iter(self.values)

    def __getitem__(self, index) -> float:
        return self.values[index]

    def __repr__(self) -> str:
        return f"_Quaternion({self.values})"

    def dot(self, other) -> float:
        return sum(left * right for left, right in zip(self.values, other, strict=True))

    def normalized(self) -> "_Quaternion":
        length = math.sqrt(sum(value * value for value in self.values))
        return _Quaternion(value / length for value in self.values)

    def __matmul__(self, other) -> "_Quaternion":
        w1, x1, y1, z1 = self.values
        w2, x2, y2, z2 = list(other)
        return _Quaternion(
            (
                w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
                w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
                w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
            )
        )

    def to_matrix(self) -> "_Matrix":
        w, x, y, z = self.normalized().values
        return _Matrix(
            (
                (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
                (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
                (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
            )
        )

    def to_euler(self, order="XYZ", reference=None) -> "_Euler":
        if order != "XYZ":
            raise NotImplementedError(f"the test math only implements XYZ Euler order, not {order}")
        rows = self.to_matrix().rows
        pitch = math.asin(max(-1.0, min(1.0, -rows[2][0])))
        triple = (math.atan2(rows[2][1], rows[2][2]), pitch, math.atan2(rows[1][0], rows[0][0]))
        if reference is None:
            return _Euler(triple, order)
        alternate = (triple[0] + math.pi, math.pi - triple[1], triple[2] + math.pi)
        best = min(
            (_wrapped_near(candidate, list(reference)) for candidate in (triple, alternate)),
            key=lambda values: sum(abs(value - ref) for value, ref in zip(values, reference, strict=True)),
        )
        return _Euler(best, order)


def _wrapped_near(triple, reference) -> tuple:
    """Shift each angle by whole turns so it sits closest to the reference angle."""
    return tuple(
        value + 2.0 * math.pi * round((ref - value) / (2.0 * math.pi))
        for value, ref in zip(triple, reference, strict=True)
    )


class _Euler:
    """An XYZ Euler triple: rotations applied X, then Y, then Z."""

    def __init__(self, values, order="XYZ") -> None:
        self.values = [float(value) for value in values]
        self.order = order

    def __iter__(self):
        return iter(self.values)

    def __getitem__(self, index) -> float:
        return self.values[index]

    def __repr__(self) -> str:
        return f"_Euler({self.values})"

    def to_quaternion(self) -> _Quaternion:
        x, y, z = self.values
        about_x = _Quaternion((1.0, 0.0, 0.0), x)
        about_y = _Quaternion((0.0, 1.0, 0.0), y)
        about_z = _Quaternion((0.0, 0.0, 1.0), z)
        return about_z @ about_y @ about_x


class _Matrix:
    """A 3x3 or 4x4 matrix supporting the products, inverses and decompositions the handlers use."""

    def __init__(self, rows) -> None:
        self.rows = [[float(value) for value in row] for row in rows]

    def __iter__(self):
        return iter(_Vector(row) for row in self.rows)

    def __getitem__(self, index):
        return self.rows[index]

    def __repr__(self) -> str:
        return f"_Matrix({self.rows})"

    @property
    def size(self) -> int:
        return len(self.rows)

    @property
    def col(self) -> list:
        return [_Vector([row[index] for row in self.rows]) for index in range(len(self.rows[0]))]

    @property
    def translation(self) -> _Vector:
        return _Vector([row[3] for row in self.rows[:3]])

    def copy(self) -> "_Matrix":
        return _Matrix(self.rows)

    def identity(self) -> None:
        size = self.size
        self.rows = [[1.0 if row == column else 0.0 for column in range(size)] for row in range(size)]

    def to_3x3(self) -> "_Matrix":
        return _Matrix([row[:3] for row in self.rows[:3]])

    def normalized(self) -> "_Matrix":
        columns = [column.normalized() for column in self.to_3x3().col]
        return _Matrix([[columns[index][row] for index in range(3)] for row in range(3)])

    def to_scale(self) -> _Vector:
        return _Vector([column.length for column in self.to_3x3().col])

    def determinant(self) -> float:
        rows = self.to_3x3().rows if self.size == 4 else self.rows
        (a, b, c), (d, e, f), (g, h, i) = rows
        return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)

    def to_quaternion(self) -> _Quaternion:
        rows = self.normalized().rows
        trace = rows[0][0] + rows[1][1] + rows[2][2]
        if trace > 0.0:
            scale = math.sqrt(trace + 1.0) * 2.0
            values = (
                0.25 * scale,
                (rows[2][1] - rows[1][2]) / scale,
                (rows[0][2] - rows[2][0]) / scale,
                (rows[1][0] - rows[0][1]) / scale,
            )
        elif rows[0][0] > rows[1][1] and rows[0][0] > rows[2][2]:
            scale = math.sqrt(1.0 + rows[0][0] - rows[1][1] - rows[2][2]) * 2.0
            values = (
                (rows[2][1] - rows[1][2]) / scale,
                0.25 * scale,
                (rows[0][1] + rows[1][0]) / scale,
                (rows[0][2] + rows[2][0]) / scale,
            )
        elif rows[1][1] > rows[2][2]:
            scale = math.sqrt(1.0 + rows[1][1] - rows[0][0] - rows[2][2]) * 2.0
            values = (
                (rows[0][2] - rows[2][0]) / scale,
                (rows[0][1] + rows[1][0]) / scale,
                0.25 * scale,
                (rows[1][2] + rows[2][1]) / scale,
            )
        else:
            scale = math.sqrt(1.0 + rows[2][2] - rows[0][0] - rows[1][1]) * 2.0
            values = (
                (rows[1][0] - rows[0][1]) / scale,
                (rows[0][2] + rows[2][0]) / scale,
                (rows[1][2] + rows[2][1]) / scale,
                0.25 * scale,
            )
        return _Quaternion(values).normalized()

    def decompose(self) -> tuple:
        return self.translation, self.to_quaternion(), self.to_scale()

    def inverted(self) -> "_Matrix":
        size = self.size
        work = [
            list(row) + [1.0 if row_index == column else 0.0 for column in range(size)]
            for row_index, row in enumerate(self.rows)
        ]
        for pivot in range(size):
            best = max(range(pivot, size), key=lambda row: abs(work[row][pivot]))
            work[pivot], work[best] = work[best], work[pivot]
            divisor = work[pivot][pivot]
            work[pivot] = [value / divisor for value in work[pivot]]
            for row in range(size):
                if row == pivot:
                    continue
                factor = work[row][pivot]
                work[row] = [value - factor * other for value, other in zip(work[row], work[pivot], strict=True)]
        return _Matrix([row[size:] for row in work])

    def product(self, other: "_Matrix") -> "_Matrix":
        return _Matrix(
            [
                [
                    sum(self.rows[row][inner] * other.rows[inner][column] for inner in range(self.size))
                    for column in range(other.size)
                ]
                for row in range(self.size)
            ]
        )

    def transform(self, vector: _Vector) -> _Vector:
        size = self.size
        values = list(vector) + ([1.0] if size == 4 else [])
        product = [sum(row[index] * values[index] for index in range(size)) for row in self.rows]
        return _Vector(product[:3])

    def __matmul__(self, other):
        return self.transform(other) if isinstance(other, _Vector) else self.product(other)

    @staticmethod
    def LocRotScale(location, rotation, scale) -> "_Matrix":  # ruff: ignore[invalid-function-name] - the mathutils spelling
        basis = rotation.to_matrix() if isinstance(rotation, _Quaternion) else _Quaternion(rotation).to_matrix()
        scales = list(scale)
        location = list(location)
        return _Matrix(
            [[basis.rows[row][column] * scales[column] for column in range(3)] + [location[row]] for row in range(3)]
            + [[0.0, 0.0, 0.0, 1.0]]
        )

    @staticmethod
    def Identity(size) -> "_Matrix":  # ruff: ignore[invalid-function-name] - the mathutils spelling
        return _Matrix([[1.0 if row == column else 0.0 for column in range(size)] for row in range(size)])

    @staticmethod
    def Translation(vector) -> "_Matrix":  # ruff: ignore[invalid-function-name] - the mathutils spelling
        rows = _Matrix.Identity(4).rows
        for index, value in enumerate(vector):
            rows[index][3] = value
        return _Matrix(rows)

    @staticmethod
    def Rotation(angle, size, axis) -> "_Matrix":  # ruff: ignore[invalid-function-name] - the mathutils spelling
        basis = _Quaternion(_AXIS_VECTORS[axis], angle).to_matrix()
        if size == 3:
            return basis
        return _Matrix([[*basis.rows[row], 0.0] for row in range(3)] + [[0.0, 0.0, 0.0, 1.0]])


_AXIS_VECTORS = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}


class _BoundMatrix(_Matrix):
    """What `pose_bone.matrix_basis` hands back: an in-place `identity()` writes to the bone."""

    def __init__(self, rows, owner) -> None:
        super().__init__(rows)
        self._owner = owner

    def identity(self) -> None:
        super().identity()
        self._owner.matrix_basis = _Matrix(self.rows)


class _PoseBone:
    """
    A pose bone composing as Blender's does: pose = parent pose, then rest offset, then basis.

    The transform channels are the stored state and `matrix_basis` is derived from them, which
    is what makes a rewritten `rotation_quaternion` sign or Euler branch survive to the key -
    a matrix round trip would canonicalise both away.
    """

    def __init__(self, name, rest_relative=None, parent=None, rotation_mode="QUATERNION") -> None:
        self.name = name
        self.parent = parent
        self.rest_relative = rest_relative or _Matrix.Identity(4)
        self.rotation_mode = rotation_mode
        self.location = _Vector((0.0, 0.0, 0.0))
        self.scale = _Vector((1.0, 1.0, 1.0))
        self.rotation_quaternion = _Quaternion((1.0, 0.0, 0.0, 0.0))
        self.rotation_euler = _Euler((0.0, 0.0, 0.0), "XYZ" if rotation_mode == "QUATERNION" else rotation_mode)
        self.rotation_axis_angle = [0.0, 0.0, 1.0, 0.0]
        self.keyed = []
        self.deleted = []
        self.insert_fails = False
        self.custom_properties: dict = {}

    @property
    def parent_recursive(self) -> list:
        return [self.parent, *self.parent.parent_recursive] if self.parent else []

    @property
    def bone(self) -> types.SimpleNamespace:
        """The rest bone behind the pose bone: `matrix_local` is its armature-space rest."""
        return types.SimpleNamespace(name=self.name, matrix_local=self.rest_pose)

    @property
    def rest_pose(self) -> _Matrix:
        """The bone's rest placement in armature space."""
        return self.parent.rest_pose.product(self.rest_relative) if self.parent else self.rest_relative

    @property
    def parent_frame(self) -> _Matrix:
        """What `matrix_basis` is measured against: the posed parent plus this bone's rest offset."""
        return self.parent.matrix.product(self.rest_relative) if self.parent else self.rest_relative

    @property
    def _rotation(self) -> _Quaternion:
        if self.rotation_mode == "QUATERNION":
            return _Quaternion(self.rotation_quaternion).normalized()
        if self.rotation_mode == "AXIS_ANGLE":
            angle, *axis = list(self.rotation_axis_angle)
            return _Quaternion(axis, angle)
        return _Euler(self.rotation_euler, self.rotation_mode).to_quaternion()

    @property
    def matrix_basis(self) -> _BoundMatrix:
        return _BoundMatrix(_Matrix.LocRotScale(self.location, self._rotation, self.scale).rows, self)

    @matrix_basis.setter
    def matrix_basis(self, value) -> None:
        location, rotation, scale = _Matrix(value.rows).decompose()
        self.location = location
        self.scale = scale
        if self.rotation_mode == "QUATERNION":
            self.rotation_quaternion = rotation
        elif self.rotation_mode == "AXIS_ANGLE":
            angle = 2.0 * math.acos(max(-1.0, min(1.0, rotation[0])))
            axis = _Vector(list(rotation)[1:])
            self.rotation_axis_angle = [angle, *(axis.normalized() if axis.length else _Vector((0.0, 0.0, 1.0)))]
        else:
            self.rotation_euler = rotation.to_euler(self.rotation_mode)

    @property
    def matrix(self) -> _Matrix:
        return self.parent_frame.product(_Matrix(self.matrix_basis.rows))

    @matrix.setter
    def matrix(self, value) -> None:
        self.matrix_basis = self.parent_frame.inverted().product(value)

    def keyframe_insert(self, data_path, frame, group=None) -> bool:
        if self.insert_fails:
            return False
        # A custom property is keyed under its subscript, not as an attribute.
        value = self.custom_properties[data_path[2:-2]] if data_path.startswith("[") else list(getattr(self, data_path))
        self.keyed.append((data_path, frame, group, value))
        return True

    def keyframe_delete(self, data_path, frame) -> bool:
        self.deleted.append((data_path, frame))
        return True

    def __contains__(self, key) -> bool:
        return key in self.custom_properties

    def __getitem__(self, key):
        return self.custom_properties[key]

    def __setitem__(self, key, value) -> None:
        self.custom_properties[key] = value


class _Key:
    def __init__(self, frame, value) -> None:
        self.co = (float(frame), float(value))
        self.interpolation = "BEZIER"


class _FCurve:
    def __init__(self, data_path, array_index, keys, modifiers=()) -> None:
        self.data_path = data_path
        self.array_index = array_index
        self.keyframe_points = [_Key(frame, value) for frame, value in keys]
        self.modifiers = list(modifiers)

    def evaluate(self, frame) -> float:
        return next(point.co[1] for point in self.keyframe_points if math.isclose(point.co[0], frame, abs_tol=1e-6))


class _Action:
    def __init__(self, name) -> None:
        self.name = name
        self.fcurves = []
        self.slots = ()


class _Actions(dict):
    def new(self, name) -> _Action:
        self[name] = _Action(name)
        return self[name]


class _AnimationData:
    """One persistent animation_data block, so an assignment the handler makes stays visible."""

    def __init__(self) -> None:
        self.action = None
        self.action_slot = None
        self.action_suitable_slots = ()


def _convert_space(pose_bone, matrix, from_space, to_space, rig):
    """Relabel one matrix between the four pose spaces, as `Object.convert_space` does."""
    if from_space == to_space:
        return matrix
    to_pose = {
        "POSE": lambda value: value,
        "WORLD": lambda value: rig.matrix_world.inverted() @ value,
        "LOCAL": lambda value: pose_bone.parent_frame @ value,
        "LOCAL_WITH_PARENT": lambda value: pose_bone.rest_pose @ value,
    }
    from_pose = {
        "POSE": lambda value: value,
        "WORLD": lambda value: rig.matrix_world @ value,
        "LOCAL": lambda value: pose_bone.parent_frame.inverted() @ value,
        "LOCAL_WITH_PARENT": lambda value: pose_bone.rest_pose.inverted() @ value,
    }
    return from_pose[to_space](to_pose[from_space](matrix))


def _posing(monkeypatch, bones, *, matrix_world=None, objects=None):
    """
    Load the addon against a rig whose bones compose like Blender's and report the pieces.

    Args:
        monkeypatch: The test's monkeypatch.
        bones: The `_PoseBone`s the rig carries, parents before children.
        matrix_world: The rig's object matrix; identity when omitted.
        objects: Extra scene objects, for aim targets.

    Returns:
        tuple: the server, the rig, its animation data, and the posing module.

    """
    animation = _AnimationData()
    rig = types.SimpleNamespace(
        name="CHAR1_rig",
        type="ARMATURE",
        data=types.SimpleNamespace(name="CHAR1_rigData", pose_position="POSE", bones=[]),
        pose=types.SimpleNamespace(bones={bone.name: bone for bone in bones}),
        matrix_world=matrix_world or _Matrix.Identity(4),
        animation_data_create=lambda: animation,
    )
    rig.convert_space = lambda pose_bone, matrix, from_space, to_space: _convert_space(
        pose_bone, matrix, from_space, to_space, rig
    )
    scene_objects = {"CHAR1_rig": rig, **(objects or {})}
    addon, bpy = _load_addon(monkeypatch, data={"objects": scene_objects, "actions": _Actions()})
    bpy.context.view_layer = types.SimpleNamespace(update=lambda: None)
    bpy.context.scene.frame_current = 1
    bpy.context.scene.frame_set = lambda *_args, **_kwargs: None
    mathutils = sys.modules["mathutils"]
    for name, value in (("Matrix", _Matrix), ("Vector", _Vector), ("Quaternion", _Quaternion), ("Euler", _Euler)):
        monkeypatch.setattr(mathutils, name, value, raising=False)
    posing = sys.modules[f"{addon.__name__}.handlers.character_rigging.posing"]
    return addon.BlenderMCPServer(), rig, animation, posing


def _target_object(name, location) -> types.SimpleNamespace:
    """Build a non-armature aim target: every object carries a `type`, and a bone aim reads it."""
    return types.SimpleNamespace(name=name, type="EMPTY", matrix_world=_Matrix.Translation(location))


# A rig that is not at the origin and not axis-aligned, so armature space and world space differ
# and an aim resolved in the wrong one cannot pass by coincidence.
_RIG_WORLD = _Matrix.Translation((0.3, 0.0, 0.0)) @ _Matrix.Rotation(0.55, 4, "Z")
# The head sits 0.68 m up a neck that is itself 0.4 m up the spine, and is turned a quarter turn
# about X so its own axes are nothing like the armature's - as on CHAR1_rig, where the head bone's
# length axis points at the character's left.
_SPINE_REST = _Matrix.Translation((0.0, 0.0, 0.4))
_HEAD_REST = _Matrix.Translation((0.0, 0.0, 0.28)) @ _Matrix.Rotation(math.pi / 2, 4, "X")


def _head_rig(monkeypatch, *, objects=None, rotation_mode="QUATERNION"):
    spine = _PoseBone("CHAR1_spine03_skn_jnt", rest_relative=_SPINE_REST)
    head = _PoseBone("CHAR1_head_jnt", rest_relative=_HEAD_REST, parent=spine, rotation_mode=rotation_mode)
    server, rig, animation, posing = _posing(monkeypatch, [spine, head], matrix_world=_RIG_WORLD, objects=objects)
    return server, rig, animation, posing, spine, head


def test_aim_points_the_named_axis_at_an_object_and_leaves_position_and_scale_alone(monkeypatch) -> None:
    camera = _target_object("SH030_cam", (0.6, -4.7, 0.95))
    _server, rig, _animation, posing, _spine, head = _head_rig(monkeypatch, objects={"SH030_cam": camera})
    before = head.matrix.copy()

    aim = posing._validated_aim(
        rig,
        head,
        {"target_object_name": "SH030_cam", "track_axis": "Z", "up_axis": "-X", "up_reference": (0, 0, 1)},
    )
    matrix = posing._aim_pose_matrix(rig, head, aim)

    direction = ((rig.matrix_world.inverted() @ _Vector((0.6, -4.7, 0.95))) - before.translation).normalized()
    columns = matrix.to_3x3().col
    assert columns[2].dot(direction) == pytest.approx(1.0, abs=1e-12)
    assert columns[0].dot(direction) == pytest.approx(0.0, abs=1e-12)
    # up_axis="-X" leans the bone's -X toward world +Z, so +X leans away from it.
    assert columns[0].dot(rig.matrix_world.inverted().to_3x3() @ _Vector((0, 0, 1))) < 0.0
    # A right-handed basis orients the bone; a left-handed one would mirror it.
    assert matrix.determinant() == pytest.approx(1.0, abs=1e-12)
    assert list(matrix.translation) == pytest.approx(list(before.translation), abs=1e-12)
    assert list(matrix.to_scale()) == pytest.approx([1.0, 1.0, 1.0], abs=1e-12)


def test_aim_at_a_world_point_resolves_through_the_rig_transform(monkeypatch) -> None:
    _server, rig, _animation, posing, _spine, head = _head_rig(monkeypatch)

    aim = posing._validated_aim(rig, head, {"target_point": (0.0, -2.0, 0.75), "track_axis": "Z"})
    matrix = posing._aim_pose_matrix(rig, head, aim)

    direction = ((rig.matrix_world.inverted() @ _Vector((0.0, -2.0, 0.75))) - head.matrix.translation).normalized()
    assert matrix.to_3x3().col[2].dot(direction) == pytest.approx(1.0, abs=1e-12)


def test_aim_rejects_every_direction_it_cannot_define(monkeypatch) -> None:
    _server, rig, _animation, posing, _spine, head = _head_rig(monkeypatch)
    head_world = rig.matrix_world @ head.matrix.translation

    with pytest.raises(ValueError, match="at the head of 'CHAR1_head_jnt'"):
        posing._aim_pose_matrix(
            rig, head, posing._validated_aim(rig, head, {"target_point": tuple(head_world), "track_axis": "Z"})
        )
    forward = tuple(rig.matrix_world @ (head.matrix.translation + _Vector((0.0, 0.0, 0.5))))
    with pytest.raises(ValueError, match="parallel to the aim direction"):
        posing._aim_pose_matrix(
            rig,
            head,
            posing._validated_aim(
                rig, head, {"target_point": forward, "track_axis": "Z", "up_axis": "X", "up_reference": (0, 0, 1)}
            ),
        )
    with pytest.raises(ValueError, match="is a zero vector"):
        posing._validated_aim(
            rig, head, {"target_point": (0, -2, 0.75), "track_axis": "Z", "up_axis": "X", "up_reference": (0, 0, 0)}
        )
    with pytest.raises(ValueError, match="different bone axis than track_axis"):
        posing._validated_aim(rig, head, {"target_point": (0, -2, 0.75), "track_axis": "Z", "up_axis": "-Z"})
    with pytest.raises(ValueError, match="must be one of X, -X, Y, -Y, Z, -Z"):
        posing._validated_aim(rig, head, {"target_point": (0, -2, 0.75), "track_axis": "W"})
    with pytest.raises(ValueError, match="exactly one of target_point or target_object_name"):
        posing._validated_aim(
            rig, head, {"target_point": (0, -2, 0.75), "target_object_name": "SH030_cam", "track_axis": "Z"}
        )
    with pytest.raises(ValueError, match=r"aim_at\.target_object_name not found: SH030_cam"):
        posing._validated_aim(rig, head, {"target_object_name": "SH030_cam", "track_axis": "Z"})


def _target_armature(name, location, bone_name, head, tail) -> types.SimpleNamespace:
    """
    Build a second rig whose pose bone an aim can name.

    `PoseBone.head`/`tail` are armature-space and follow the pose, which is why the aim reads
    them through the object matrix rather than off the rest bone.

    Args:
        name: The object name.
        location: Where the rig sits in the scene.
        bone_name: The pose bone the aim may name.
        head: The bone's armature-space head.
        tail: Its armature-space tail.

    Returns:
        The stub armature object.

    """
    bone = types.SimpleNamespace(name=bone_name, head=_Vector(head), tail=_Vector(tail))
    return types.SimpleNamespace(
        name=name,
        type="ARMATURE",
        matrix_world=_Matrix.Translation(location),
        pose=types.SimpleNamespace(bones={bone_name: bone}),
    )


def _aimed_direction(rig, matrix, world_point):
    """Measure, in armature space, the direction from an aimed bone's head to a world point."""
    return ((rig.matrix_world.inverted() @ _Vector(world_point)) - matrix.translation).normalized()


# A second character standing 2 m away whose head bone sits 1.6 m up, while its object origin
# sits on the floor: aiming at the object aims at its feet.
_OTHER_ORIGIN = (2.0, 1.0, 0.0)
_OTHER_HEAD = (2.0, 1.0, 1.6)
_OTHER_TAIL = (2.0, 1.0, 1.75)


def test_an_aim_at_a_bone_looks_at_the_bone_and_not_at_the_rigs_origin(monkeypatch) -> None:
    """Two characters told to look at each other stared at each other's feet; this is why."""
    other = _target_armature("OtherRig", _OTHER_ORIGIN, "head", (0.0, 0.0, 1.6), (0.0, 0.0, 1.75))
    _server, rig, _animation, posing, _spine, head = _head_rig(monkeypatch, objects={"OtherRig": other})

    aim = posing._validated_aim(
        rig, head, {"target_object_name": "OtherRig", "target_bone_name": "head", "track_axis": "Z", "up_axis": "-X"}
    )
    matrix = posing._aim_pose_matrix(rig, head, aim)

    tracked = matrix.to_3x3().col[2]
    assert tracked.dot(_aimed_direction(rig, matrix, _OTHER_HEAD)) == pytest.approx(1.0, abs=1e-12)
    at_origin = math.degrees(math.acos(min(1.0, tracked.dot(_aimed_direction(rig, matrix, _OTHER_ORIGIN)))))
    assert at_origin > 10.0, f"aiming at the bone and at the origin differ by only {at_origin} degrees"


def test_a_bone_target_can_name_the_tail_or_the_centre_of_the_bone(monkeypatch) -> None:
    other = _target_armature("OtherRig", _OTHER_ORIGIN, "head", (0.0, 0.0, 1.6), (0.0, 0.0, 1.75))
    _server, rig, _animation, posing, _spine, head = _head_rig(monkeypatch, objects={"OtherRig": other})

    for position, expected in (("TAIL", _OTHER_TAIL), ("CENTER", (2.0, 1.0, 1.675))):
        aim = posing._validated_aim(
            rig,
            head,
            {
                "target_object_name": "OtherRig",
                "target_bone_name": "head",
                "target_bone_position": position,
                "track_axis": "Z",
                "up_axis": "-X",
            },
        )
        matrix = posing._aim_pose_matrix(rig, head, aim)
        assert matrix.to_3x3().col[2].dot(_aimed_direction(rig, matrix, expected)) == pytest.approx(1.0, abs=1e-12)


def test_a_bone_target_the_scene_cannot_supply_is_refused_by_name(monkeypatch) -> None:
    other = _target_armature("OtherRig", _OTHER_ORIGIN, "head", (0.0, 0.0, 1.6), (0.0, 0.0, 1.75))
    camera = _target_object("SH030_cam", (0.6, -4.7, 0.95))
    _server, rig, _animation, posing, _spine, head = _head_rig(
        monkeypatch, objects={"OtherRig": other, "SH030_cam": camera}
    )

    with pytest.raises(ValueError, match=r"aim_at\.target_bone_name not found on 'OtherRig': neck"):
        posing._validated_aim(
            rig, head, {"target_object_name": "OtherRig", "target_bone_name": "neck", "track_axis": "Z"}
        )
    with pytest.raises(ValueError, match="needs an armature to live on"):
        posing._validated_aim(
            rig, head, {"target_object_name": "SH030_cam", "target_bone_name": "head", "track_axis": "Z"}
        )
    with pytest.raises(ValueError, match="names a point on a bone"):
        posing._validated_aim(
            rig, head, {"target_object_name": "OtherRig", "target_bone_position": "TAIL", "track_axis": "Z"}
        )


def test_one_axis_named_twice_is_refused_with_the_axes_that_are_still_free(monkeypatch) -> None:
    """
    The tools' own instruction produced this refusal, so the refusal has to carry the remedy.

    `length_axis` is `Y` for every bone and an upright bone's `up_axis` is `Y` as well, so
    "pass both straight through" named one axis twice. Naming the two axes that are left, and
    where each points, is the difference between a rule and a next call.
    """
    _server, rig, _animation, posing, _spine, head = _head_rig(monkeypatch)

    with pytest.raises(ValueError) as refusal:
        posing._validated_aim(rig, head, {"target_point": (0.0, -2.0, 0.75), "track_axis": "Y", "up_axis": "Y"})

    message = str(refusal.value)
    assert "both the Y axis" in message
    # This bone's own X runs along world +X at rest and its Z along world -Y, through a rig that
    # is itself turned 0.55 rad about Z - the part a caller cannot read off `rest_axes`.
    assert "X points +X" in message, message
    assert "Z points -Y" in message, message


def test_posing_an_aim_lands_it_on_the_target_after_the_parent_has_moved(monkeypatch) -> None:
    camera = _target_object("SH030_cam", (0.6, -4.7, 0.95))
    server, rig, _animation, _posing, spine, head = _head_rig(monkeypatch, objects={"SH030_cam": camera})

    reply = server.set_character_pose(
        "CHAR1_rig",
        [
            {"bone_name": spine.name, "rotation_euler": (0.0, 0.4, 0.0)},
            {"bone_name": head.name, "aim_at": {"target_object_name": "SH030_cam", "track_axis": "Z", "up_axis": "-X"}},
        ],
    )

    posed = head.matrix
    direction = ((rig.matrix_world.inverted() @ _Vector((0.6, -4.7, 0.95))) - posed.translation).normalized()
    assert posed.to_3x3().normalized().col[2].dot(direction) == pytest.approx(1.0, abs=1e-12)
    assert reply["bones"][1]["channels"] == ["aim_at"]


def test_a_minimal_arc_aim_past_the_flip_angle_is_refused_rather_than_rolled_arbitrarily(monkeypatch) -> None:
    _server, rig, _animation, posing, _spine, head = _head_rig(monkeypatch)
    # The head's rest turns its +Z onto armature -Y, so a target behind him is a half turn away.
    behind = tuple(rig.matrix_world @ (head.matrix.translation + _Vector((0.0, 2.0, 0.0))))

    with pytest.raises(ValueError, match="without an up reference"):
        aim = posing._validated_aim(rig, head, {"target_point": behind, "track_axis": "Z"})
        posing._aim_pose_matrix(rig, head, aim)

    # The same swing is well defined once the roll is pinned.
    matrix = posing._aim_pose_matrix(
        rig,
        head,
        posing._validated_aim(rig, head, {"target_point": behind, "track_axis": "Z", "up_axis": "Y"}),
    )
    direction = ((rig.matrix_world.inverted() @ _Vector(behind)) - head.matrix.translation).normalized()
    assert matrix.to_3x3().col[2].dot(direction) == pytest.approx(1.0, abs=1e-12)
    assert matrix.to_3x3().col[1].dot(rig.matrix_world.inverted().to_3x3() @ _Vector((0, 0, 1))) > 0.0


def test_rotate_resolves_named_axes_and_vectors_in_degrees(monkeypatch) -> None:
    _server, _rig, _animation, posing, _spine, head = _head_rig(monkeypatch)

    named = posing._validated_rotate({"axis": "-Y", "degrees": 60.160568}, head.name)
    vector = posing._validated_rotate({"axis": (0.0, -2.0, 0.0), "degrees": 60.160568}, head.name)

    assert list(named["axis"]) == [0.0, -1.0, 0.0]
    assert list(vector["axis"]) == pytest.approx([0.0, -1.0, 0.0])
    # The runbook's elbow value is -1.05 rad about the bone's +Y, which is +1.05 about -Y.
    assert named["angle"] == pytest.approx(1.05, abs=1e-6)
    assert named["relative"] is False
    with pytest.raises(ValueError, match=r"rotate\.axis for 'CHAR1_head_jnt' must be a non-zero vector"):
        posing._validated_rotate({"axis": (0.0, 0.0, 0.0), "degrees": 10.0}, head.name)
    with pytest.raises(ValueError, match="requires degrees"):
        posing._validated_rotate({"axis": "Y"}, head.name)


def test_relative_rotate_composes_while_the_default_replaces(monkeypatch) -> None:
    server, _rig, _animation, _posing, _spine, head = _head_rig(monkeypatch)

    server.set_character_pose("CHAR1_rig", [{"bone_name": head.name, "rotate": {"axis": "Z", "degrees": 30.0}}])
    server.set_character_pose("CHAR1_rig", [{"bone_name": head.name, "rotate": {"axis": "Z", "degrees": 30.0}}])
    replaced = head.matrix_basis.to_quaternion()
    server.set_character_pose(
        "CHAR1_rig", [{"bone_name": head.name, "rotate": {"axis": "Z", "degrees": 30.0, "relative": True}}]
    )
    composed = head.matrix_basis.to_quaternion()

    assert 2.0 * math.acos(min(1.0, abs(replaced[0]))) == pytest.approx(math.radians(30.0), abs=1e-9)
    assert 2.0 * math.acos(min(1.0, abs(composed[0]))) == pytest.approx(math.radians(60.0), abs=1e-9)


def test_a_rotation_about_the_bones_own_length_axis_says_the_bone_will_not_move(monkeypatch) -> None:
    """
    The silent failure the runbook hit twice: the call succeeds, the keys land, nothing bends.

    Blender builds every bone along its own +Y, so a LOCAL rotation about Y turns the bone
    about the line through its head and tail. The evidence is in the same assertion: the
    bone's length direction and its origin come out of the roll unchanged, so its tail - and
    every child bone's head, which sits on it - is exactly where it started.
    """
    server, _rig, _animation, _posing, _spine, head = _head_rig(monkeypatch)
    rest_direction = head.matrix.to_3x3().col[1].copy()
    rest_origin = head.matrix.translation.copy()

    rolled = server.set_character_pose("CHAR1_rig", [{"bone_name": head.name, "rotate": {"axis": "-Y", "degrees": 60.0}}])
    rolled_direction = head.matrix.to_3x3().col[1].copy()
    rolled_origin = head.matrix.translation.copy()
    bent = server.set_character_pose("CHAR1_rig", [{"bone_name": head.name, "rotate": {"axis": "Z", "degrees": 60.0}}])

    assert rolled_direction.dot(rest_direction) == pytest.approx(1.0, abs=1e-12)
    assert (rolled_origin - rest_origin).length == pytest.approx(0.0, abs=1e-12)
    assert len(rolled["warnings"]) == 1
    assert "length axis" in rolled["warnings"][0]
    assert head.name in rolled["warnings"][0]
    # The same call about a perpendicular axis does move the bone, and says nothing.
    assert head.matrix.to_3x3().col[1].dot(rest_direction) == pytest.approx(math.cos(math.radians(60.0)), abs=1e-12)
    assert bent["warnings"] == []


@pytest.mark.parametrize(
    ("spec", "space", "warns"),
    [
        ({"rotation_euler": (0.0, -1.05, 0.0)}, "LOCAL", True),
        ({"rotation_axis_angle": (1.05, 0.0, 1.0, 0.0)}, "LOCAL", True),
        ({"rotation_quaternion": (math.cos(0.525), 0.0, math.sin(0.525), 0.0)}, "LOCAL", True),
        ({"rotate": {"axis": (0.0, -2.0, 0.0), "degrees": 60.0}}, "LOCAL", True),
        ({"rotate": {"axis": "-Y", "degrees": 60.0}}, "LOCAL_WITH_PARENT", True),
        # Two Euler components compose into an axis this cannot read off, so it stays quiet.
        ({"rotation_euler": (0.3, -1.05, 0.0)}, "LOCAL", False),
        # Under POSE the letter names the armature's axis, which says nothing about this bone.
        ({"rotate": {"axis": "-Y", "degrees": 60.0}}, "POSE", False),
        # Too small to be a roll anyone meant; re-applying a pose must not accuse the caller.
        ({"rotate": {"axis": "Y", "degrees": 0.1}}, "LOCAL", False),
        ({"rotate": {"axis": "Z", "degrees": 60.0}}, "LOCAL", False),
    ],
)
def test_the_inert_rotation_notice_reads_every_spelling_of_one_axis_and_only_bone_local_space(
    monkeypatch, spec, space, warns
) -> None:
    server, _rig, _animation, _posing, _spine, head = _head_rig(monkeypatch, rotation_mode="XYZ")

    reply = server.set_character_pose("CHAR1_rig", [{"bone_name": head.name, **spec}], space=space)

    named = [warning for warning in reply["warnings"] if "length axis" in warning]
    assert bool(named) is warns


@pytest.mark.parametrize(
    ("rotation_mode", "expected"),
    [("QUATERNION", "rotation_quaternion"), ("XYZ", "rotation_euler"), ("AXIS_ANGLE", "rotation_axis_angle")],
)
def test_a_resolved_rotation_keys_only_the_bones_native_channel(monkeypatch, rotation_mode, expected) -> None:
    _server, _rig, _animation, posing, _spine, head = _head_rig(monkeypatch, rotation_mode=rotation_mode)

    for spec in ({"aim_at": {}}, {"rotate": {}}, {"rotation_euler": (0, 0, 0)}):
        assert posing._pose_key_paths(head, spec) == [expected]
    assert posing._pose_key_paths(head, {"matrix": ()}) == ["location", "scale", expected]
    assert posing._pose_key_paths(head, {"aim_at": {}, "location": (0, 0, 0)}) == ["location", expected]


def test_an_absolute_space_child_is_resolved_against_the_parent_this_call_moved(monkeypatch) -> None:
    for space in ("POSE", "WORLD", "LOCAL_WITH_PARENT"):
        server, _rig, _animation, _posing, spine, head = _head_rig(monkeypatch)
        posed_head = head.matrix.copy()
        # A rotation-only entry keeps the bone where the pose puts it, so any translation left
        # in matrix_basis is compensation for a parent read before it moved - and nothing keys it.
        server.set_character_pose(
            "CHAR1_rig",
            [
                {"bone_name": spine.name, "rotation_euler": (0.0, 0.4, 0.0)},
                {"bone_name": head.name, "rotation_quaternion": tuple(posed_head.to_quaternion())},
            ],
            space=space,
        )

        assert head.matrix_basis.translation.length == pytest.approx(0.0, abs=1e-9), space
        assert list(head.matrix_basis.to_scale()) == pytest.approx([1.0, 1.0, 1.0], abs=1e-9), space


def test_a_local_space_pose_is_the_channel_value_whatever_the_parent_did(monkeypatch) -> None:
    server, _rig, _animation, _posing, spine, head = _head_rig(monkeypatch)
    turn = (0.0, 0.44, 0.0)

    server.set_character_pose(
        "CHAR1_rig",
        [
            {"bone_name": spine.name, "rotation_euler": (0.0, 0.4, 0.0)},
            {"bone_name": head.name, "rotation_euler": turn},
        ],
    )

    # LOCAL is the bone's own channel delta: the spine moving in the same call cannot touch it.
    expected = _Matrix.LocRotScale((0.0, 0.0, 0.0), _Euler(turn, "XYZ").to_quaternion(), (1.0, 1.0, 1.0))
    assert [value for row in head.matrix_basis.rows for value in row] == pytest.approx(
        [value for row in expected.rows for value in row], abs=1e-15
    )


def test_keying_leaves_the_rig_driven_by_the_action_it_authored(monkeypatch) -> None:
    server, _rig, animation, _posing, _spine, head = _head_rig(monkeypatch)

    reply = server.keyframe_character_pose(
        "CHAR1_rig", "CHAR1_sh030_motion", 1.0, [{"bone_name": head.name, "rotation_euler": (0, 0.44, 0)}]
    )

    assert animation.action.name == "CHAR1_sh030_motion"
    assert reply["assigned_action"] == "CHAR1_sh030_motion"
    assert "unassigned_action" not in reply
    # The envelope's own vocabulary: a changed resource is a plain datablock name, as it is in
    # every other handler. A record here instead was a shape the client had to special-case.
    assert reply["changed_resources"] == ["CHAR1_sh030_motion"]


def test_keying_reports_the_action_it_displaced(monkeypatch) -> None:
    server, _rig, animation, _posing, _spine, head = _head_rig(monkeypatch)
    animation.action = _Action("CHAR1_idle")

    reply = server.keyframe_character_pose(
        "CHAR1_rig", "CHAR1_sh030_motion", 1.0, [{"bone_name": head.name, "rotation_euler": (0, 0.44, 0)}]
    )

    assert reply["unassigned_action"] == "CHAR1_idle"
    assert reply["assigned_action"] == "CHAR1_sh030_motion"
    assert animation.action.name == "CHAR1_sh030_motion"


def test_a_failed_key_hands_the_rig_back_as_it_arrived(monkeypatch) -> None:
    server, _rig, animation, _posing, _spine, head = _head_rig(monkeypatch)
    head.insert_fails = True
    before = [value for row in head.matrix_basis.rows for value in row]

    with pytest.raises(RuntimeError, match="Could not insert key"):
        server.keyframe_character_pose(
            "CHAR1_rig", "CHAR1_sh030_motion", 1.0, [{"bone_name": head.name, "rotation_euler": (0, 0.44, 0)}]
        )

    assert animation.action is None
    assert [value for row in head.matrix_basis.rows for value in row] == pytest.approx(before)


def test_keying_an_aim_without_an_up_reference_is_refused(monkeypatch) -> None:
    camera = _target_object("SH030_cam", (0.6, -4.7, 0.95))
    server, _rig, animation, _posing, _spine, head = _head_rig(monkeypatch, objects={"SH030_cam": camera})

    with pytest.raises(ValueError, match="requires up_axis and up_reference when keying"):
        server.keyframe_character_pose(
            "CHAR1_rig",
            "CHAR1_sh030_motion",
            1.0,
            [{"bone_name": head.name, "aim_at": {"target_object_name": "SH030_cam", "track_axis": "Z"}}],
        )

    assert animation.action is None


def _aim_key(server, head, frame, target, mode="QUATERNION"):
    return server.keyframe_character_pose(
        "CHAR1_rig",
        "CHAR1_sh030_motion",
        frame,
        [{"bone_name": head.name, "aim_at": {"target_point": target, "track_axis": "Z", "up_axis": "-X"}}],
        action_policy="REUSE",
    )


def test_a_keyed_aim_takes_the_short_way_round_from_the_previous_key(monkeypatch) -> None:
    server, rig, animation, posing, _spine, head = _head_rig(monkeypatch)
    action = _Action("CHAR1_sh030_motion")
    sys.modules["bpy"].data.actions["CHAR1_sh030_motion"] = action
    aim = posing._validated_aim(rig, head, {"target_point": (0.0, -2.0, 0.75), "track_axis": "Z", "up_axis": "-X"})
    head.matrix = posing._aim_pose_matrix(rig, head, aim)
    reached = list(head.rotation_quaternion)
    head.matrix_basis = _Matrix.Identity(4)
    # The previous key holds the same orientation spelled with the opposite sign, which is the
    # spelling that interpolates the long way round.
    action.fcurves = [
        _FCurve(f'pose.bones["{head.name}"].rotation_quaternion', index, [(1.0, -value)])
        for index, value in enumerate(reached)
    ]

    _aim_key(server, head, 13.0, (0.0, -2.0, 0.75))

    keyed = next(values for path, frame, _group, values in head.keyed if path == "rotation_quaternion")
    assert sum(left * right for left, right in zip(keyed, [-value for value in reached], strict=True)) > 0.0
    assert keyed == pytest.approx([-value for value in reached], abs=1e-9)
    assert animation.action is action


def test_a_keyed_euler_aim_stays_on_the_previous_keys_branch(monkeypatch) -> None:
    camera = _target_object("SH030_cam", (0.6, -4.7, 0.95))
    server, rig, _animation, posing, _spine, head = _head_rig(
        monkeypatch, objects={"SH030_cam": camera}, rotation_mode="XYZ"
    )
    action = _Action("CHAR1_sh030_motion")
    sys.modules["bpy"].data.actions["CHAR1_sh030_motion"] = action
    aim = posing._validated_aim(rig, head, {"target_object_name": "SH030_cam", "track_axis": "Z", "up_axis": "-X"})
    head.matrix = posing._aim_pose_matrix(rig, head, aim)
    natural = list(head.rotation_euler)
    head.matrix_basis = _Matrix.Identity(4)
    # The previous key sits a whole turn away on every axis: the same orientation, a branch over.
    previous = [value + 2.0 * math.pi for value in natural]
    action.fcurves = [
        _FCurve(f'pose.bones["{head.name}"].rotation_euler', index, [(1.0, value)])
        for index, value in enumerate(previous)
    ]

    server.keyframe_character_pose(
        "CHAR1_rig",
        "CHAR1_sh030_motion",
        13.0,
        [{"bone_name": head.name, "aim_at": {"target_object_name": "SH030_cam", "track_axis": "Z", "up_axis": "-X"}}],
        action_policy="REUSE",
    )

    keyed = next(values for path, _frame, _group, values in head.keyed if path == "rotation_euler")
    jump = max(abs(value - reference) for value, reference in zip(keyed, previous, strict=True))
    naive = max(abs(value - reference) for value, reference in zip(natural, previous, strict=True))
    assert naive == pytest.approx(2.0 * math.pi, abs=1e-9)
    assert math.degrees(jump) < 1e-6


def _cyclic_action(bone_names, extent=(1.0, 25.0)):
    """Install an action whose rotation curves for these bones already carry a Cycles modifier."""
    action = _Action("CHAR1_sh030_motion")
    sys.modules["bpy"].data.actions["CHAR1_sh030_motion"] = action
    action.fcurves = [
        _FCurve(
            f'pose.bones["{name}"].rotation_quaternion',
            index,
            [(extent[0], 0.0), (extent[1], 0.0)],
            modifiers=[types.SimpleNamespace(type="CYCLES")],
        )
        for name in bone_names
        for index in range(4)
    ]
    return action


def test_keying_past_a_cycle_says_the_period_it_just_changed(monkeypatch) -> None:
    """
    The trap that broke a shot's arms: a later gesture silently restretched a 24-frame loop.

    The arms' curves already carried a Cycles modifier over frames 1-25 when a high-five was
    keyed at 162. Blender did exactly what it was asked - the modifier repeats its own curve's
    key extent, so the extent, and the period, became 161 frames - and the arms drifted for
    the rest of the shot instead of striding. Extending a cycle on purpose is legitimate, so
    this must warn and key, never refuse.
    """
    server, _rig, _animation, _posing, _spine, head = _head_rig(monkeypatch)
    _cyclic_action([head.name])

    reply = server.keyframe_character_pose(
        "CHAR1_rig",
        "CHAR1_sh030_motion",
        162.0,
        [{"bone_name": head.name, "rotation_euler": (0.44, 0.0, 0.0)}],
        action_policy="REUSE",
    )

    assert [frame for _path, frame, _group, _values in head.keyed] == [162.0], "the key was refused, not warned about"
    # One bone, four channels, one warning - and it names what changed, not that something did.
    assert len(reply["warnings"]) == 1, reply["warnings"]
    warning = reply["warnings"][0]
    assert f"Bone '{head.name}'" in warning
    assert "frame 162" in warning
    assert "frames 1-25" in warning
    assert "becomes 161 frames instead of 24" in warning
    # Lifted by the envelope, so it survives the page of keys being cut to fit the budget.
    assert warning in envelope_for(reply, changed_objects=[])["warnings"]


def test_keying_inside_an_existing_cycle_warns_about_nothing(monkeypatch) -> None:
    """A key at frame 12 of a 1-25 loop changes no period; warning about it would train the eye off."""
    server, _rig, _animation, _posing, _spine, head = _head_rig(monkeypatch)
    _cyclic_action([head.name])

    reply = server.keyframe_character_pose(
        "CHAR1_rig",
        "CHAR1_sh030_motion",
        12.0,
        [{"bone_name": head.name, "rotation_euler": (0.44, 0.0, 0.0)}],
        action_policy="REUSE",
    )

    assert reply["warnings"] == []


def test_a_whole_rig_keyed_past_its_cycles_counts_the_bones_it_cannot_name(monkeypatch) -> None:
    """
    Warnings are lifted whole and never paged, so one per posed bone would spend the budget.

    A 500-bone pose is a legal call. Naming the first few and counting the rest keeps the
    notice bounded while still saying which bones to look at first.
    """
    bones = [_PoseBone(f"CHAR1_bone_{index:02d}") for index in range(9)]
    server, _rig, _animation, _posing_module = _posing(monkeypatch, bones)
    _cyclic_action([bone.name for bone in bones])

    reply = server.keyframe_character_pose(
        "CHAR1_rig",
        "CHAR1_sh030_motion",
        162.0,
        [{"bone_name": bone.name, "rotation_euler": (0.44, 0.0, 0.0)} for bone in bones],
        action_policy="REUSE",
    )

    assert len(reply["warnings"]) == 5, reply["warnings"]
    assert sum(f"Bone '{bone.name}'" in reply["warnings"][0] for bone in bones) == 1
    summary = reply["warnings"][-1]
    assert summary.startswith("5 further bone(s) keyed at frame 162")
    assert "CHAR1_bone_04" in summary and "and 1 more" in summary


def _batched_aim_rig(monkeypatch):
    """
    Build a rig whose aim target moves with the playhead, so a per-frame aim differs from a stale one.

    Args:
        monkeypatch: The test's monkeypatch.

    Returns:
        tuple: the server, the rig, the head bone, the scene, and `{frame: target location}`.

    """
    camera = _target_object("SH030_cam", (0.0, 0.0, 0.0))
    server, rig, _animation, _posing_module, _spine, head = _head_rig(monkeypatch, objects={"SH030_cam": camera})
    track = {1.0: (3.0, 0.0, 0.9), 5.0: (0.0, 3.0, 0.9), 9.0: (-3.0, 0.0, 0.9)}
    scene = sys.modules["bpy"].context.scene

    def move(frame, subframe=0.0) -> None:
        scene.frame_current = frame + subframe
        camera.matrix_world = _Matrix.Translation(track[frame + subframe])

    scene.frame_set = move
    move(1.0)
    return server, rig, head, scene, track


def _aim_pose(bone_name):
    return {"bone_name": bone_name, "aim_at": {"target_object_name": "SH030_cam", "track_axis": "Z", "up_axis": "-X"}}


def test_a_batched_call_keys_every_frame_at_the_target_that_frame_holds(monkeypatch) -> None:
    """
    Thirteen keys of a stride were thirteen round trips, and each solved against one frame.

    The frames are keyed in ascending order with the playhead on each of them, so an aim at a
    moving target keys three different looks rather than three copies of the one the playhead
    happened to be showing.
    """
    server, rig, head, scene, track = _batched_aim_rig(monkeypatch)

    reply = server.keyframe_character_pose(
        "CHAR1_rig",
        "CHAR1_sh030_motion",
        keys=[{"frame": frame, "poses": [_aim_pose(head.name)]} for frame in (9.0, 1.0, 5.0)],
    )

    assert reply["keyed_frames"] == [1.0, 5.0, 9.0]
    assert reply["changed_bones"] == [head.name]
    assert [entry["frame"] for entry in reply["changed_keys"]] == [1.0, 5.0, 9.0]
    assert scene.frame_current == pytest.approx(1.0), "the playhead was left where the last key was written"
    keyed = {frame: values for _path, frame, _group, values in head.keyed}
    for frame, target in track.items():
        head.rotation_quaternion = _Quaternion(keyed[frame])
        direction = ((rig.matrix_world.inverted() @ _Vector(target)) - head.matrix.translation).normalized()
        landed = head.matrix.to_3x3().normalized().col[2].dot(direction)
        assert landed == pytest.approx(1.0, abs=1e-9), f"frame {frame} was keyed aiming somewhere else"


def test_a_batched_call_restores_the_pose_it_borrowed_for_every_frame(monkeypatch) -> None:
    server, _rig, head, _scene, _track = _batched_aim_rig(monkeypatch)
    before = [value for row in head.matrix_basis.rows for value in row]

    server.keyframe_character_pose(
        "CHAR1_rig",
        "CHAR1_sh030_motion",
        keys=[{"frame": frame, "poses": [_aim_pose(head.name)]} for frame in (1.0, 5.0, 9.0)],
    )

    assert [value for row in head.matrix_basis.rows for value in row] == pytest.approx(before)


def test_a_single_frame_call_still_reports_exactly_what_it_did_before(monkeypatch) -> None:
    """The batched form is an addition: the one-frame reply keeps its shape, plus keyed_frames."""
    server, _rig, _animation, _posing_module, _spine, head = _head_rig(monkeypatch)

    reply = server.keyframe_character_pose(
        "CHAR1_rig", "CHAR1_sh030_motion", 3.0, [{"bone_name": head.name, "location": (0.1, 0.0, 0.0)}], detail=True
    )

    assert reply["keyed_frames"] == [3.0]
    assert reply["changed_keys"] == [{"bone": head.name, "data_path": "location", "frame": 3.0}]
    assert set(reply["bones"][0]) == {"bone", "channels", "before_pose_matrix", "after_pose_matrix"}


def test_a_batched_detail_record_names_the_frame_it_describes(monkeypatch) -> None:
    """A record per bone per frame is unreadable without the frame; one frame carries its own."""
    server, _rig, head, _scene, _track = _batched_aim_rig(monkeypatch)

    reply = server.keyframe_character_pose(
        "CHAR1_rig",
        "CHAR1_sh030_motion",
        keys=[{"frame": frame, "poses": [_aim_pose(head.name)]} for frame in (1.0, 5.0)],
        detail=True,
    )

    assert [record["frame"] for record in reply["bones"]] == [1.0, 5.0]


def test_the_two_call_shapes_are_exclusive_and_named_in_the_refusal(monkeypatch) -> None:
    server, _rig, _animation, _posing_module, _spine, head = _head_rig(monkeypatch)
    pose = [{"bone_name": head.name, "location": (0.1, 0.0, 0.0)}]

    with pytest.raises(ValueError, match="exactly one of frame with poses"):
        server.keyframe_character_pose("CHAR1_rig", "CHAR1_sh030_motion")
    with pytest.raises(ValueError, match="exactly one of frame with poses"):
        server.keyframe_character_pose("CHAR1_rig", "CHAR1_sh030_motion", 1.0, pose, keys=[{"frame": 2.0, "poses": pose}])
    with pytest.raises(ValueError, match="requires both frame and poses"):
        server.keyframe_character_pose("CHAR1_rig", "CHAR1_sh030_motion", 1.0)


def test_a_batch_that_repeats_a_frame_or_outgrows_one_call_is_refused(monkeypatch) -> None:
    """A frame keyed twice in one call would key whichever pose the list happened to end on."""
    posing = _load_posing(monkeypatch)
    pose = [{"bone_name": "CHAR1_head_jnt", "location": (0.1, 0.0, 0.0)}]

    with pytest.raises(ValueError, match=r"keys names the same frame more than once: \[2.0\]"):
        posing._pose_key_requests(None, None, [{"frame": 2.0, "poses": pose}, {"frame": 2.0, "poses": pose}])
    with pytest.raises(ValueError, match="more than the 2000 one call may apply"):
        posing._pose_key_requests(None, None, [{"frame": float(index), "poses": pose * 9} for index in range(250)])
    with pytest.raises(ValueError, match=r"keys\[1\] requires at least one pose entry"):
        posing._pose_key_requests(None, None, [{"frame": 1.0, "poses": pose}, {"frame": 2.0, "poses": []}])


def test_the_batched_form_reaches_the_handler_with_its_frames_intact(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )
    pose = character_rigging.BonePose(bone_name="hand.L", location=(0.1, 0.0, 0.0))

    asyncio.run(
        character_rigging.keyframe_character_pose(
            ctx=None,
            armature_object_name="my_rig",
            action_name="Walk",
            keys=[character_rigging.PoseKeyframe(frame=frame, poses=[pose]) for frame in (1.0, 13.0)],
        )
    )

    params = calls[0][1]
    assert params["frame"] is None
    assert params["poses"] is None
    assert params["keys"] == [
        {"frame": 1.0, "poses": [{"bone_name": "hand.L", "location": (0.1, 0.0, 0.0)}]},
        {"frame": 13.0, "poses": [{"bone_name": "hand.L", "location": (0.1, 0.0, 0.0)}]},
    ]


def test_the_tool_refuses_both_call_shapes_and_a_repeated_frame() -> None:
    pose = character_rigging.BonePose(bone_name="hand.L", location=(0.1, 0.0, 0.0))
    key = character_rigging.PoseKeyframe(frame=1.0, poses=[pose])

    with pytest.raises(ToolError, match="neither form was supplied in full"):
        asyncio.run(
            character_rigging.keyframe_character_pose(ctx=None, armature_object_name="my_rig", action_name="Walk")
        )
    with pytest.raises(ToolError, match="not both"):
        asyncio.run(
            character_rigging.keyframe_character_pose(
                ctx=None, armature_object_name="my_rig", action_name="Walk", frame=1.0, poses=[pose], keys=[key]
            )
        )
    with pytest.raises(ToolError, match=r"keys names the same frame more than once: \[1.0\]"):
        asyncio.run(
            character_rigging.keyframe_character_pose(
                ctx=None, armature_object_name="my_rig", action_name="Walk", keys=[key, key]
            )
        )


def test_a_bone_position_without_a_bone_is_refused_by_the_schema() -> None:
    with pytest.raises(ValidationError, match="target_bone_position names a point on target_bone_name"):
        character_rigging.BoneAim(target_object_name="OtherRig", target_bone_position="TAIL", track_axis="Z")
    with pytest.raises(ValidationError, match="target_bone_name names a bone on target_object_name"):
        character_rigging.BoneAim(target_point=(1.0, 0.0, 0.0), target_bone_name="head", track_axis="Z")


def _override_rig(monkeypatch, *, overridden):
    """Build a rig carrying one bone with a custom property, either local or a library override."""
    bone = _PoseBone("CHAR1_head_jnt")
    bone.custom_properties["ik_blend"] = 0.0
    server, rig, _animation, _posing_module = _posing(monkeypatch, [bone])
    rig.override_library = types.SimpleNamespace(properties=[]) if overridden else None
    return server, bone


def test_a_custom_property_written_onto_a_library_override_says_it_will_not_survive(monkeypatch) -> None:
    """The write reads back correctly in-session and is the library's value again after reopen."""
    server, bone = _override_rig(monkeypatch, overridden=True)

    reply = server.set_character_pose("CHAR1_rig", [{"bone_name": bone.name, "custom_properties": {"ik_blend": 1.0}}])

    assert len(reply["warnings"]) == 1, reply["warnings"]
    assert "library override" in reply["warnings"][0]
    assert bone.name in reply["warnings"][0]


def test_a_local_rig_is_not_warned_about_its_own_custom_properties(monkeypatch) -> None:
    server, bone = _override_rig(monkeypatch, overridden=False)

    reply = server.set_character_pose("CHAR1_rig", [{"bone_name": bone.name, "custom_properties": {"ik_blend": 1.0}}])

    assert reply["warnings"] == []


def test_keying_a_custom_property_onto_an_override_is_not_warned_about(monkeypatch) -> None:
    """A keyed value lands in the action, which is local data, and reopens as written."""
    server, bone = _override_rig(monkeypatch, overridden=True)

    reply = server.keyframe_character_pose(
        "CHAR1_rig", "CHAR1_sh030_motion", 1.0, [{"bone_name": bone.name, "custom_properties": {"ik_blend": 1.0}}]
    )

    assert reply["warnings"] == []
    assert reply["changed_keys"] == [{"bone": bone.name, "data_path": '["ik_blend"]', "frame": 1.0}]


def test_rest_axes_are_reported_only_when_asked_for(monkeypatch) -> None:
    bone = types.SimpleNamespace(
        name="CHAR1_head_jnt",
        parent=None,
        use_deform=True,
        matrix_local=_Matrix.Rotation(math.pi / 2, 4, "Z"),
    )
    rig = types.SimpleNamespace(
        name="CHAR1_rig",
        type="ARMATURE",
        matrix_world=_Matrix.Identity(4),
        data=types.SimpleNamespace(name="CHAR1_rigData", bones=[bone]),
        update_from_editmode=lambda: None,
    )
    addon, _bpy = _load_addon(monkeypatch, data={"objects": {"CHAR1_rig": rig}})
    server = addon.BlenderMCPServer()

    plain = server.list_character_bones("CHAR1_rig")
    with_axes = server.list_character_bones("CHAR1_rig", rest_axes=True)

    assert "rest_axes" not in plain["bones"]["items"][0]
    assert "up_axis" not in plain["bones"]["items"][0]
    assert "length_axis" not in plain
    # A quarter turn about Z sends the bone's rest X to armature +Y and its Y to armature -X.
    assert with_axes["bones"]["items"][0]["rest_axes"] == [0.0, 1.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0, 1.0]


# The rest axes a demo runbook recorded for CHAR1_head_jnt, as the rows of a 4x4 whose columns are
# the bone's own X, Y and Z: the length runs along armature +X and the bone's own -X stands up.
_CHAR1_HEAD_REST = _Matrix([[0.0, 1.0, 0.0, 0.0], [0.0, 0.0, -1.0, 0.0], [-1.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0]])
_CHAR1_HEAD_AXES = [0.0, 0.0, -1.0, 1.0, 0.0, 0.0, 0.0, -1.0, 0.0]


def _char1_head_rig(monkeypatch, matrix_world):
    """
    Build the one bone the runbook misread, on a rig placed by `matrix_world`.

    Args:
        monkeypatch: The test's monkeypatch.
        matrix_world: Where the rig sits in the scene, which is what `up_axis` turns on.

    Returns:
        The server whose `bpy.data.objects` holds the rig.

    """
    bone = types.SimpleNamespace(name="CHAR1_head_jnt", parent=None, use_deform=True, matrix_local=_CHAR1_HEAD_REST)
    rig = types.SimpleNamespace(
        name="CHAR1_rig",
        type="ARMATURE",
        matrix_world=matrix_world,
        data=types.SimpleNamespace(name="CHAR1_rigData", bones=[bone]),
        update_from_editmode=lambda: None,
    )
    addon, _bpy = _load_addon(monkeypatch, data={"objects": {"CHAR1_rig": rig}})
    return addon.BlenderMCPServer()


def test_the_rest_axes_are_also_named_in_the_vocabulary_an_aim_takes(monkeypatch) -> None:
    """Nine numbers left the up axis to be worked out, and the derivation is what went wrong."""
    server = _char1_head_rig(monkeypatch, _Matrix.Identity(4))

    reply = server.list_character_bones("CHAR1_rig", rest_axes=True)

    assert reply["bones"]["items"][0]["rest_axes"] == _CHAR1_HEAD_AXES
    assert reply["bones"]["items"][0]["up_axis"] == "-X"
    assert reply["length_axis"] == "Y"


def test_the_up_axis_follows_the_rig_into_the_scene_where_the_nine_numbers_cannot(monkeypatch) -> None:
    """aim_at.up_reference is a world direction, and the reported axes are armature-space."""
    server = _char1_head_rig(monkeypatch, _Matrix.Rotation(1.2, 4, "X"))

    reply = server.list_character_bones("CHAR1_rig", rest_axes=True)

    # The same bone and the same nine numbers as the upright rig; only the rig has moved, and
    # a different bone axis is now the one nearest world up.
    assert reply["bones"]["items"][0]["rest_axes"] == _CHAR1_HEAD_AXES
    assert reply["bones"]["items"][0]["up_axis"] == "-Z"


def test_each_world_direction_is_given_the_bone_axis_that_already_points_that_way(monkeypatch) -> None:
    """
    The table an aim is chosen from, and the one the reply could not otherwise support.

    The rig is laid over 1.2 rad about X, so four of the six answers differ from the same bone's
    armature-space reading - which is exactly the derivation a caller would otherwise do off
    `rest_axes`, and get wrong.
    """
    server = _char1_head_rig(monkeypatch, _Matrix.Rotation(1.2, 4, "X"))

    item = server.list_character_bones("CHAR1_rig", rest_axes=True)["bones"]["items"][0]

    assert item["aim_axis_for_world"] == {"+X": "Y", "-X": "-Y", "+Y": "X", "-Y": "-X", "+Z": "-Z", "-Z": "Z"}
    # up_axis is this table's "+Z" entry, read off the same derivation rather than beside it.
    assert item["up_axis"] == item["aim_axis_for_world"]["+Z"]


def test_the_same_bone_standing_upright_names_different_axes_for_the_same_directions(monkeypatch) -> None:
    """Only the rig's object matrix differs, and it is the half of the answer rest_axes omits."""
    server = _char1_head_rig(monkeypatch, _Matrix.Identity(4))

    item = server.list_character_bones("CHAR1_rig", rest_axes=True)["bones"]["items"][0]

    assert item["rest_axes"] == _CHAR1_HEAD_AXES
    assert item["aim_axis_for_world"]["+Y"] == "-Z"
    assert item["aim_axis_for_world"]["+Z"] == "-X"


def test_a_rig_scaled_to_nothing_names_no_axis_for_any_direction(monkeypatch) -> None:
    """No axis points anywhere, so there is no answer to give and none is invented."""
    flattened = _Matrix([[0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0]])
    server = _char1_head_rig(monkeypatch, flattened)

    item = server.list_character_bones("CHAR1_rig", rest_axes=True)["bones"]["items"][0]

    assert item["up_axis"] is None
    assert set(item["aim_axis_for_world"]) == {"+X", "-X", "+Y", "-Y", "+Z", "-Z"}
    assert set(item["aim_axis_for_world"].values()) == {None}


def _rig_with_bones(monkeypatch, *names: str):
    """
    Build a stub armature carrying the named rest bones.

    Args:
        monkeypatch: The test's monkeypatch.
        *names: Bone names, in armature order.

    Returns:
        The server whose `bpy.data.objects` holds the rig.

    """
    bones = [
        types.SimpleNamespace(name=name, parent=None, use_deform=True, matrix_local=_Matrix.Rotation(0.0, 4, "Z"))
        for name in names
    ]
    rig = types.SimpleNamespace(
        name="CHAR1_rig",
        type="ARMATURE",
        matrix_world=_Matrix.Identity(4),
        data=types.SimpleNamespace(name="CHAR1_rigData", bones=bones),
        update_from_editmode=lambda: None,
    )
    addon, _bpy = _load_addon(monkeypatch, data={"objects": {"CHAR1_rig": rig}})
    return addon.BlenderMCPServer()


def test_named_bones_are_returned_in_one_page_instead_of_paged_to(monkeypatch) -> None:
    """Reading three bones' axes off a production rig took six calls; naming them takes one."""
    server = _rig_with_bones(monkeypatch, *(f"CHAR1_bone_{index:03d}" for index in range(180)))

    filtered = server.list_character_bones("CHAR1_rig", rest_axes=True, bone_names=["CHAR1_bone_177", "CHAR1_bone_004"])

    assert [item["name"] for item in filtered["bones"]["items"]] == ["CHAR1_bone_004", "CHAR1_bone_177"]
    assert filtered["bones"]["total"] == 2
    assert filtered["bones"]["truncated"] is False
    assert filtered["bones"]["next_offset"] is None
    assert all("rest_axes" in item for item in filtered["bones"]["items"])


def test_an_unknown_bone_name_is_refused_rather_than_silently_dropped(monkeypatch) -> None:
    """A caller asking for three bones and receiving two would pose the wrong one."""
    server = _rig_with_bones(monkeypatch, "CHAR1_head_jnt", "CHAR1_spine03_skn_jnt")

    with pytest.raises(ValueError, match=r"Bones not found in armature 'CHAR1_rig': \['CHAR1_hed_jnt'\]"):
        server.list_character_bones("CHAR1_rig", bone_names=["CHAR1_head_jnt", "CHAR1_hed_jnt"])


@pytest.mark.parametrize("value", [[], ["  "], [7], "CHAR1_head_jnt"])
def test_a_malformed_bone_name_filter_is_refused(monkeypatch, value) -> None:
    """The filter decides which bones are read, so a wrong shape must not read the whole rig."""
    server = _rig_with_bones(monkeypatch, "CHAR1_head_jnt")

    with pytest.raises(ValueError, match="bone_names"):
        server.list_character_bones("CHAR1_rig", bone_names=value)


def test_no_filter_still_lists_every_bone(monkeypatch) -> None:
    """The filter is opt-in: omitting it must not narrow anything."""
    server = _rig_with_bones(monkeypatch, "CHAR1_head_jnt", "CHAR1_spine03_skn_jnt")

    assert server.list_character_bones("CHAR1_rig")["bones"]["total"] == 2


def _reach_bone(name, parent=None):
    """Build a plain topology node: what `_unbranched_ancestor_chain`/`_rest_ancestor_chain` need."""
    bone = types.SimpleNamespace(name=name, parent=parent, children=[])
    if parent is not None:
        parent.children.append(bone)
    return bone


def _load_posing(monkeypatch):
    addon, _bpy = _load_addon(monkeypatch, data={"objects": {}})
    return sys.modules[f"{addon.__name__}.handlers.character_rigging.posing"]


def test_unbranched_ancestor_chain_stops_before_a_mid_chain_fork(monkeypatch) -> None:
    shoulder = _reach_bone("shoulder")
    arm_l = _reach_bone("arm_L", parent=shoulder)
    _reach_bone("arm_R", parent=shoulder)  # the fork: shoulder now has two children
    hand_l = _reach_bone("hand_L", parent=arm_l)
    posing = _load_posing(monkeypatch)

    assert [bone.name for bone in posing._unbranched_ancestor_chain(hand_l, 32)] == ["hand_L", "arm_L"]


def test_unbranched_ancestor_chain_stops_before_a_root_level_fork(monkeypatch) -> None:
    """A fork at the very root excludes the root itself, not just what is above it."""
    root = _reach_bone("root")
    a1 = _reach_bone("a1", parent=root)
    _reach_bone("a2", parent=root)  # root itself forks
    tip = _reach_bone("tip", parent=a1)
    posing = _load_posing(monkeypatch)

    assert [bone.name for bone in posing._unbranched_ancestor_chain(tip, 32)] == ["tip", "a1"]


def test_unbranched_ancestor_chain_includes_an_unforked_root(monkeypatch) -> None:
    root = _reach_bone("root")
    mid = _reach_bone("mid", parent=root)
    tip = _reach_bone("tip", parent=mid)
    posing = _load_posing(monkeypatch)

    assert [bone.name for bone in posing._unbranched_ancestor_chain(tip, 32)] == ["tip", "mid", "root"]


def test_unbranched_ancestor_chain_respects_max_length(monkeypatch) -> None:
    root = _reach_bone("root")
    mid = _reach_bone("mid", parent=root)
    tip = _reach_bone("tip", parent=mid)
    posing = _load_posing(monkeypatch)

    assert [bone.name for bone in posing._unbranched_ancestor_chain(tip, 2)] == ["tip", "mid"]


def test_unbranched_ancestor_chain_of_a_root_bone_is_just_that_bone(monkeypatch) -> None:
    lone = _reach_bone("lone")
    posing = _load_posing(monkeypatch)

    assert [bone.name for bone in posing._unbranched_ancestor_chain(lone, 32)] == ["lone"]


def test_rest_ancestor_chain_returns_the_exact_requested_length(monkeypatch) -> None:
    """An explicit chain_length walks past a fork an auto-resolve would have stopped before."""
    root = _reach_bone("root")
    a1 = _reach_bone("a1", parent=root)
    _reach_bone("a2", parent=root)
    tip = _reach_bone("tip", parent=a1)
    posing = _load_posing(monkeypatch)

    assert [bone.name for bone in posing._rest_ancestor_chain(tip, 3)] == ["tip", "a1", "root"]


def test_rest_ancestor_chain_refuses_a_length_past_the_root(monkeypatch) -> None:
    root = _reach_bone("root")
    tip = _reach_bone("tip", parent=root)
    posing = _load_posing(monkeypatch)

    with pytest.raises(ValueError, match=r"has only 2 ancestor\(s\); chain_length=3"):
        posing._rest_ancestor_chain(tip, 3)


def _rest_bone(name, head, tail, parent=None):
    bone = types.SimpleNamespace(
        name=name,
        parent=parent,
        children=[],
        head_local=_Vector(head),
        tail_local=_Vector(tail),
        length=(_Vector(tail) - _Vector(head)).length,
    )
    if parent is not None:
        parent.children.append(bone)
    return bone


def _load_posing_with_math(monkeypatch):
    posing = _load_posing(monkeypatch)
    mathutils = sys.modules["mathutils"]
    for name, value in (("Matrix", _Matrix), ("Vector", _Vector), ("Quaternion", _Quaternion), ("Euler", _Euler)):
        monkeypatch.setattr(mathutils, name, value, raising=False)
    return posing


def test_synthesize_pole_finds_the_bend_side_of_a_bent_two_bone_chain(monkeypatch) -> None:
    """
    A bent 2-bone chain, and the round-2 pole-synthesis draft it broke.

    That draft picked the pole reference as `chain[len(chain) // 2]`, which for a 2-bone
    chain is the ROOT bone itself - offset-from-root is then always zero, so it refused
    every 2-bone reach, bent or not. This fixture (upper_arm straight down, forearm bent 90
    degrees to +X - an elbow) is exactly the case that bug broke; the fixed reference is the
    walk [tip_tail, *(bone.head_local for bone in chain)]'s midpoint, which lands on the elbow.
    """
    posing = _load_posing_with_math(monkeypatch)
    upper_arm = _rest_bone("upper_arm", (0, 0, 0), (0, 0, -1))
    forearm = _rest_bone("forearm", (0, 0, -1), (0.8, 0, -1), parent=upper_arm)
    armature = types.SimpleNamespace(matrix_world=_Matrix.Identity(4))

    pole = posing._synthesize_pole(armature, [forearm, upper_arm])

    assert list(pole) == pytest.approx([-1.4055638569974545, 0.0, -2.124451085597964], abs=1e-9)


def test_synthesize_pole_refuses_a_straight_two_bone_rest_chain(monkeypatch) -> None:
    posing = _load_posing_with_math(monkeypatch)
    upper_arm = _rest_bone("upper_arm", (0, 0, 0), (0, 0, -1))
    forearm = _rest_bone("forearm", (0, 0, -1), (0, 0, -2), parent=upper_arm)
    armature = types.SimpleNamespace(matrix_world=_Matrix.Identity(4))

    with pytest.raises(ValueError, match="rest pose is straight"):
        posing._synthesize_pole(armature, [forearm, upper_arm])


def test_synthesize_pole_refuses_a_single_bone_chain(monkeypatch) -> None:
    """A single bone has no distinct middle joint, so no bend direction can be inferred."""
    posing = _load_posing_with_math(monkeypatch)
    lone = _rest_bone("lone", (0, 0, 0), (0, 0, 1))
    armature = types.SimpleNamespace(matrix_world=_Matrix.Identity(4))

    with pytest.raises(ValueError, match="rest pose is straight"):
        posing._synthesize_pole(armature, [lone])


def test_synthesize_pole_refuses_a_chain_whose_root_and_tip_coincide(monkeypatch) -> None:
    posing = _load_posing_with_math(monkeypatch)
    upper_arm = _rest_bone("upper_arm", (0, 0, 0), (0, 0, 0))
    armature = types.SimpleNamespace(matrix_world=_Matrix.Identity(4))

    with pytest.raises(ValueError, match="root and tip coincide"):
        posing._synthesize_pole(armature, [upper_arm])


def test_synthesize_pole_converts_through_the_armatures_world_matrix(monkeypatch) -> None:
    posing = _load_posing_with_math(monkeypatch)
    upper_arm = _rest_bone("upper_arm", (0, 0, 0), (0, 0, -1))
    forearm = _rest_bone("forearm", (0, 0, -1), (0.8, 0, -1), parent=upper_arm)
    armature = types.SimpleNamespace(matrix_world=_Matrix.Translation((5.0, 0.0, 0.0)))

    pole = posing._synthesize_pole(armature, [forearm, upper_arm])

    assert pole[0] == pytest.approx(-1.4055638569974545 + 5.0, abs=1e-9)


def _reach_chain_module(monkeypatch, rest_bones):
    addon, _bpy = _load_addon(monkeypatch, data={"objects": {}})
    posing = sys.modules[f"{addon.__name__}.handlers.character_rigging.posing"]
    armature = types.SimpleNamespace(data=types.SimpleNamespace(bones=rest_bones))
    return posing, armature


def test_resolve_reach_chain_refuses_an_unknown_tip_bone(monkeypatch) -> None:
    posing, armature = _reach_chain_module(monkeypatch, {})

    with pytest.raises(ValueError, match="Pose bone not found: forearm"):
        posing._resolve_reach_chain(armature, {"tip_bone": "forearm"}, {})


def test_resolve_reach_chain_reports_resolved_when_chain_length_is_omitted(monkeypatch) -> None:
    root = _reach_bone("upper_arm")
    tip = _reach_bone("forearm", parent=root)
    posing, armature = _reach_chain_module(monkeypatch, {"upper_arm": root, "forearm": tip})

    chain, source = posing._resolve_reach_chain(armature, {"tip_bone": "forearm"}, {})

    assert [bone.name for bone in chain] == ["forearm", "upper_arm"]
    assert source == "resolved"


def test_resolve_reach_chain_reports_explicit_when_chain_length_is_given(monkeypatch) -> None:
    root = _reach_bone("upper_arm")
    tip = _reach_bone("forearm", parent=root)
    posing, armature = _reach_chain_module(monkeypatch, {"upper_arm": root, "forearm": tip})

    chain, source = posing._resolve_reach_chain(armature, {"tip_bone": "forearm", "chain_length": 1}, {})

    assert [bone.name for bone in chain] == ["forearm"]
    assert source == "explicit"


def test_resolve_reach_chain_refuses_a_bone_already_claimed_by_an_earlier_reach(monkeypatch) -> None:
    root = _reach_bone("upper_arm")
    tip = _reach_bone("forearm", parent=root)
    posing, armature = _reach_chain_module(monkeypatch, {"upper_arm": root, "forearm": tip})
    already_captured = {"upper_arm": object()}

    with pytest.raises(ValueError, match=r"Bones claimed by more than one reach: \['upper_arm'\]"):
        posing._resolve_reach_chain(armature, {"tip_bone": "forearm"}, already_captured)


def test_resolved_reach_target_refuses_an_unknown_object_name(monkeypatch) -> None:
    addon, _bpy = _load_addon(monkeypatch, data={"objects": {}})
    posing = sys.modules[f"{addon.__name__}.handlers.character_rigging.posing"]

    with pytest.raises(ValueError, match="target_point object not found: SH030_cam"):
        posing._resolved_reach_target(None, "SH030_cam", "target_point")


class _FakeObjects(dict):
    """The slice of `bpy.data.objects` the reach helpers touch: `new`, `get` and `remove`."""

    def new(self, name, _data):
        self[name] = types.SimpleNamespace(name=name, location=None)
        return self[name]

    def remove(self, obj, do_unlink=True) -> None:
        del self[obj.name]


def _reach_geometry_module(monkeypatch, objects):
    addon, bpy = _load_addon(monkeypatch, data={"objects": objects})
    bpy.context.collection = types.SimpleNamespace(objects=types.SimpleNamespace(link=lambda _obj: None))
    return sys.modules[f"{addon.__name__}.handlers.character_rigging.posing"]


def test_a_reach_whose_pole_cannot_be_resolved_removes_the_targets_scratch_empty(monkeypatch) -> None:
    """The target's Empty is created before the pole is resolved, so a refusal must undo it."""
    objects = _FakeObjects()
    posing = _reach_geometry_module(monkeypatch, objects)

    with pytest.raises(ValueError, match="pole_target_point object not found: elbow_pole"):
        posing._resolve_reach_geometry(
            types.SimpleNamespace(),
            {"target_point": (0.6, -0.1, 1.1), "pole_target_object_name": "elbow_pole"},
            [],
        )

    assert list(objects) == []


class _PickyConstraint:
    """A constraint whose RNA refuses a wrong-typed value rather than coercing it, as Blender's does."""

    def __setattr__(self, name, value) -> None:
        if name == "pole_target" and not isinstance(value, types.SimpleNamespace):
            raise TypeError("bpy_struct: Constraint.pole_target expects an Object type")
        object.__setattr__(self, name, value)


def test_a_constraint_value_blender_refuses_removes_the_constraint_it_already_added(monkeypatch) -> None:
    """`constraints.new` lands the constraint on the rig before its fields are written."""
    posing = _load_posing(monkeypatch)
    constraints = []
    tip_pose_bone = types.SimpleNamespace(
        constraints=types.SimpleNamespace(
            new=lambda type: constraints.append(_PickyConstraint()) or constraints[-1],
            remove=constraints.remove,
        )
    )

    with pytest.raises(TypeError, match="expects an Object type"):
        posing._configured_reach_constraint(tip_pose_bone, {}, types.SimpleNamespace(), "not-an-object", 2)

    assert constraints == []


def test_bone_reach_requires_exactly_one_target_form() -> None:
    with pytest.raises(ValidationError, match="Supply exactly one of target_point or target_object_name"):
        character_rigging.BoneReach(tip_bone="forearm.L")
    with pytest.raises(ValidationError, match="Supply exactly one of target_point or target_object_name"):
        character_rigging.BoneReach(tip_bone="forearm.L", target_point=(1, 2, 3), target_object_name="SH030_cam")


def test_bone_reach_allows_at_most_one_pole_form() -> None:
    with pytest.raises(ValidationError, match="Supply at most one of pole_target_point or pole_target_object_name"):
        character_rigging.BoneReach(
            tip_bone="forearm.L",
            target_point=(1, 2, 3),
            pole_target_point=(0, 0, 0),
            pole_target_object_name="elbow_pole",
        )


def test_solve_bone_reach_forwards_reaches_and_omits_unset_optional_fields(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        _dispatch,
        "send_command",
        lambda command, params=None: calls.append((command, params)) or {"ok": True},
    )
    reach = character_rigging.BoneReach(tip_bone="forearm.L", target_point=(0.6, -0.1, 1.1))

    asyncio.run(character_rigging.solve_bone_reach(ctx=None, armature_object_name="CHAR1_rig", reaches=[reach]))

    command, params = calls[0]
    assert command == "solve_bone_reach"
    assert params["armature_object_name"] == "CHAR1_rig"
    assert params["reaches"] == [
        {
            "tip_bone": "forearm.L",
            "target_point": (0.6, -0.1, 1.1),
            "pole_angle_degrees": 0.0,
            "use_stretch": False,
            "iterations": 500,
        }
    ]
    assert params["detail"] is False


# --- solve_bone_reach's convergence contract ----------------------------------------------


class _ConstraintStack(list):
    """The slice of `pose_bone.constraints` a reach drives: `new`, `remove` and iteration."""

    def new(self, type) -> types.SimpleNamespace:
        self.append(types.SimpleNamespace(type=type))
        return self[-1]


class _ScratchEmpty:
    """A helper Empty whose `location` write lands in `matrix_world`, as Blender's does."""

    def __init__(self, name) -> None:
        self.name = name
        self.matrix_world = _Matrix.Identity(4)

    @property
    def location(self) -> _Vector:
        return self.matrix_world.translation

    @location.setter
    def location(self, value) -> None:
        self.matrix_world = _Matrix.Translation(tuple(value))


class _ReachObjects(dict):
    """`bpy.data.objects` for a reach: the rig, plus the scratch Empties the solve creates."""

    def new(self, name, _data) -> _ScratchEmpty:
        self[name] = _ScratchEmpty(name)
        return self[name]

    def remove(self, obj, do_unlink=True) -> None:
        del self[obj.name]


class _ReachPoseBone(_PoseBone):
    """A pose bone carrying the armature-space head/tail and constraint stack a reach reads."""

    def __init__(self, name, head, tail, parent=None) -> None:
        super().__init__(name, rest_relative=_Matrix.Translation(head), parent=parent)
        self.head = _Vector(head)
        self.tail = _Vector(tail)
        self.constraints = _ConstraintStack()


# A bent two-bone arm: a 1.0 m upper arm straight down from the origin, then a 0.8 m forearm
# out along +X. Bent, so a pole can be synthesized from it; 1.8 m of total reach, so a target
# can be placed unambiguously inside or outside it.
_ARM_SHOULDER = (0.0, 0.0, 0.0)
_ARM_ELBOW = (0.0, 0.0, -1.0)
_ARM_WRIST = (0.8, 0.0, -1.0)
_ARM_REACH_M = 1.8
_ARM_POLE = (2.0, 0.0, -1.0)


def _reach_rig(monkeypatch, *, solved_tail=_ARM_WRIST, matrix_world=None):
    """
    Load the addon against the two-bone arm, with the tip's tail already at solved_tail.

    The fake `bpy` runs no IK, which is the point: the tip's tail stays exactly where this
    fixture put it, so every reported distance is one the test chose rather than one a
    solver produced. `tests/blender_character_posing_smoke.py` measures the real solve.

    Args:
        monkeypatch: The test's monkeypatch.
        solved_tail: Where the reach's tip tail sits once the constraint has been evaluated.
        matrix_world: The rig's object matrix; identity when omitted.

    Returns:
        tuple: the server, the `bpy` stub - whose `data.objects` also holds any scratch Empty
        a reach failed to clean up - the rig, and its animation data.

    """
    shoulder_rest = _rest_bone("upper_arm", _ARM_SHOULDER, _ARM_ELBOW)
    forearm_rest = _rest_bone("forearm", _ARM_ELBOW, _ARM_WRIST, parent=shoulder_rest)
    upper_arm = _ReachPoseBone("upper_arm", _ARM_SHOULDER, _ARM_ELBOW)
    forearm = _ReachPoseBone("forearm", _ARM_ELBOW, solved_tail, parent=upper_arm)
    server, rig, animation, _posing_module = _posing(monkeypatch, [upper_arm, forearm], matrix_world=matrix_world)
    rig.data.bones = {"upper_arm": shoulder_rest, "forearm": forearm_rest}
    bpy = sys.modules["bpy"]
    bpy.data.objects = _ReachObjects(bpy.data.objects)
    bpy.context.collection = types.SimpleNamespace(objects=types.SimpleNamespace(link=lambda _obj: None))
    return server, bpy, rig, animation


def _solve(server, target, **kwargs):
    """Solve the two-bone arm's one reach at a world target, with an explicit pole."""
    reach = {"tip_bone": "forearm", "target_point": target, "pole_target_point": _ARM_POLE}
    return server.solve_bone_reach("CHAR1_rig", [reach], **kwargs)


def test_a_reach_inside_its_tolerance_reports_converged_and_says_nothing_else(monkeypatch) -> None:
    """A solve that landed is the quiet case: the caller needs no notice to act on."""
    server, _bpy, _rig, _animation = _reach_rig(monkeypatch)

    reply = _solve(server, (0.80005, 0.0, -1.0))

    entry = reply["reaches"][0]
    assert entry["achieved_error_m"] == pytest.approx(5e-5, abs=1e-12)
    assert entry["converged"] is True
    assert entry["out_of_reach"] is False
    assert reply["warnings"] == []
    # Echoed once for the whole call, not repeated per reach.
    assert reply["tolerance_m"] == pytest.approx(1e-4, abs=0.0)
    assert "tolerance_m" not in entry


def test_a_tighter_tolerance_turns_the_same_solve_into_a_miss(monkeypatch) -> None:
    """The tolerance is the caller's to state: the same geometry passes or fails on it."""
    server, _bpy, _rig, _animation = _reach_rig(monkeypatch)

    reply = _solve(server, (0.80005, 0.0, -1.0), tolerance_m=1e-6)

    assert reply["reaches"][0]["converged"] is False
    assert reply["tolerance_m"] == pytest.approx(1e-6, abs=0.0)


def test_a_reachable_target_the_solve_stalled_short_of_warns_without_blaming_the_rig(monkeypatch) -> None:
    """Naming the cause is the point: this one is worth retrying, an out-of-reach one is not."""
    server, _bpy, _rig, _animation = _reach_rig(monkeypatch)

    reply = _solve(server, (0.8, 0.0, -1.0005))

    entry = reply["reaches"][0]
    assert entry["converged"] is False
    assert entry["out_of_reach"] is False
    assert entry["target_distance_m"] < entry["chain_reach_m"]
    warning = reply["warnings"][0]
    assert "'forearm'" in warning
    assert "achieved_error_m 0.0005" in warning
    assert "tolerance_m 0.0001" in warning
    assert "within the chain's range" in warning
    assert "solve stalled short of it" in warning
    assert "out of reach" not in warning


def test_a_target_beyond_the_chains_reach_is_reported_as_unreachable(monkeypatch) -> None:
    """No pose of this chain reaches 5 m out, so retrying the solve is the wrong next move."""
    server, _bpy, _rig, _animation = _reach_rig(monkeypatch)

    reply = _solve(server, (5.0, 0.0, 0.0))

    entry = reply["reaches"][0]
    assert entry["converged"] is False
    assert entry["out_of_reach"] is True
    assert entry["chain_reach_m"] == pytest.approx(_ARM_REACH_M, abs=1e-12)
    assert entry["target_distance_m"] == pytest.approx(5.0, abs=1e-12)
    warning = reply["warnings"][0]
    assert "'forearm'" in warning
    assert "out of reach" in warning
    assert "target_distance_m 5" in warning
    assert "chain_reach_m 1.8" in warning


def test_the_chains_reach_is_measured_in_world_space_not_in_rest_bone_lengths(monkeypatch) -> None:
    """A rig scaled x2 reaches twice as far, so rest lengths alone would call a hit a miss."""
    doubled = _Matrix([[2.0, 0.0, 0.0, 0.0], [0.0, 2.0, 0.0, 0.0], [0.0, 0.0, 2.0, 0.0], [0.0, 0.0, 0.0, 1.0]])
    server, _bpy, _rig, _animation = _reach_rig(monkeypatch, matrix_world=doubled)

    # 2.5 m out: past the 1.8 m of rest bone, inside the 3.6 m the scaled rig actually spans.
    entry = _solve(server, (2.5, 0.0, 0.0))["reaches"][0]

    assert entry["chain_reach_m"] == pytest.approx(2.0 * _ARM_REACH_M, abs=1e-12)
    assert entry["out_of_reach"] is False


@pytest.mark.parametrize("tolerance", [0.0, -1e-4, float("nan"), float("inf")])
def test_a_tolerance_that_names_no_precision_is_refused_before_the_rig_is_touched(monkeypatch, tolerance) -> None:
    """A bad tolerance must not leave a half-solved pose, a live IK constraint or a stray Empty."""
    server, bpy, _rig, _animation = _reach_rig(monkeypatch)
    before = _matrix_rows(bpy.data.objects["CHAR1_rig"].pose.bones["forearm"])

    with pytest.raises(ValueError, match="tolerance_m must be"):
        _solve(server, (0.8, 0.0, -1.0), tolerance_m=tolerance)

    rig = bpy.data.objects["CHAR1_rig"]
    assert list(bpy.data.objects) == ["CHAR1_rig"]
    assert list(rig.pose.bones["forearm"].constraints) == []
    assert _matrix_rows(rig.pose.bones["forearm"]) == before


def _matrix_rows(pose_bone) -> list:
    """Read the pose bone's basis out as plain numbers, so a comparison is by value."""
    return [list(row) for row in pose_bone.matrix_basis.rows]


def _long_reach_rig(monkeypatch, length):
    """
    Load the addon against a straight chain of `length` bones, tip last.

    Args:
        monkeypatch: The test's monkeypatch.
        length: How many bones the chain carries.

    Returns:
        tuple: the server, and the tip bone's name.

    """
    rest_bones = {}
    pose_bones = []
    rest_parent = None
    pose_parent = None
    for index in range(length):
        name = f"DEF-tentacle_segment_{index:03d}"
        head = (0.0, 0.0, -0.1 * index)
        tail = (0.0, 0.0, -0.1 * (index + 1))
        rest_parent = _rest_bone(name, head, tail, parent=rest_parent)
        rest_bones[name] = rest_parent
        pose_parent = _ReachPoseBone(name, head, tail, parent=pose_parent)
        pose_bones.append(pose_parent)
    server, rig, _animation, _posing_module = _posing(monkeypatch, pose_bones)
    rig.data.bones = rest_bones
    bpy = sys.modules["bpy"]
    bpy.data.objects = _ReachObjects(bpy.data.objects)
    bpy.context.collection = types.SimpleNamespace(objects=types.SimpleNamespace(link=lambda _obj: None))
    return server, pose_bones[-1].name


def test_a_missed_reach_still_warns_after_the_envelope_has_shortened_the_reply(monkeypatch) -> None:
    """
    The notice has to outlive budget fitting, or the longest calls lose it exactly when it matters.

    A 32-bone chain overruns the reply budget, so `ok()` cuts the per-bone page down and adds
    its own shortening notice. The convergence warning rides in the handler's reply rather
    than being appended to the finished envelope, which is what keeps it in the list.
    """
    server, tip = _long_reach_rig(monkeypatch, 32)
    payload = server.solve_bone_reach(
        "CHAR1_rig", [{"tip_bone": tip, "target_point": (9.0, 0.0, 0.0), "pole_target_point": _ARM_POLE}], detail=True
    )

    reply = envelope_for(payload, changed_objects=[])

    assert len(to_json(reply, fallback=str, indent=2)) <= REPLY_BYTE_BUDGET
    kept = len(reply["data"]["reaches"][0]["bones"])
    assert 0 < kept < 32, "the reply must have been shortened for this to prove anything"
    assert any("was shortened to" in warning for warning in reply["warnings"])
    assert any(f"Reach '{tip}' did not converge" in warning for warning in reply["warnings"])
    assert "warnings" not in reply["data"]


def _slot(identifier):
    return types.SimpleNamespace(identifier=identifier)


def _rig_keying_over_root_motion(monkeypatch):
    """
    Load the two-bone arm already driven by an action holding keys, slot included.

    That is the state a reach arrives in: `keyframe_object_transform` keyed the root travel
    into one action first, and the reach names an action of its own.

    Args:
        monkeypatch: The test's monkeypatch.

    Returns:
        tuple: the server, the rig, its animation data, the root-motion action and its slot.

    """
    server, bpy, rig, animation = _reach_rig(monkeypatch)
    root_motion = bpy.data.actions.new("CHAR1_sh030_root")
    root_motion.fcurves.append(_FCurve("location", 0, [(1.0, 0.0), (24.0, 5.0)]))
    root_slot = _slot("OBCHAR1_rig")
    root_motion.slots = (root_slot,)
    animation.action = root_motion
    animation.action_slot = root_slot
    # Blender's animation_data_create() both creates the block and hangs it off the ID, which
    # is what the displacement guard reads.
    rig.animation_data = animation
    reach_action = bpy.data.actions.new("CHAR1_sh030_reach")
    reach_action.slots = (_slot("OBreach"),)
    return server, rig, animation, root_motion, root_slot


def _plant(frames, target=_ARM_WRIST):
    """One reach planting the wrist on a fixed world point across several frames."""
    return {
        "tip_bone": "forearm",
        "pole_target_point": _ARM_POLE,
        "keys": [{"frame": float(frame), "target_point": list(target)} for frame in frames],
    }


def test_a_reach_that_fails_part_way_through_hands_back_the_action_it_arrived_on(monkeypatch) -> None:
    """
    A half-keyed reach left the rig pointed at its own action, with the displacement standing.

    Nothing else covers it: the object-state snapshot does not record `animation_data.action`,
    so the root motion simply stopped driving the character and the only symptom was a shot
    that had stopped moving.
    """
    server, rig, animation, root_motion, root_slot = _rig_keying_over_root_motion(monkeypatch)
    forearm = rig.pose.bones["forearm"]
    first_frame_only = forearm.keyframe_insert
    forearm.keyframe_insert = lambda data_path, frame, group=None: (
        frame <= 1.0 and first_frame_only(data_path, frame, group)
    )

    with pytest.raises(RuntimeError, match="Could not insert key"):
        server.keyframe_bone_reach("CHAR1_rig", "CHAR1_sh030_reach", [_plant((1.0, 2.0))], confirm_displace_action=True)

    assert animation.action is root_motion
    assert animation.action_slot is root_slot
