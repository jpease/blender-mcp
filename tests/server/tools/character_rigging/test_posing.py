"""
Regression coverage for pose resolution, goal-directed aims, and pose keyframing.

`mathutils` is a C extension that exists only inside Blender, so the geometry here runs against
`_math`, an independent implementation of the handful of vector, matrix and quaternion
operations the handler calls. That proves the geometry the handler asks for - which column
lands on the aim direction, which bone sees its parent's fresh transform - not Blender's
numerics; `tests/blender_character_posing_smoke.py` measures those against the real API.
"""

import math
import sys
import types

import pytest

from test_mutation_transaction import _load_addon


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

    @property
    def parent_recursive(self) -> list:
        return [self.parent, *self.parent.parent_recursive] if self.parent else []

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
        self.keyed.append((data_path, frame, group, list(getattr(self, data_path))))
        return True

    def keyframe_delete(self, data_path, frame) -> bool:
        self.deleted.append((data_path, frame))
        return True


class _Key:
    def __init__(self, frame, value) -> None:
        self.co = (float(frame), float(value))
        self.interpolation = "BEZIER"


class _FCurve:
    def __init__(self, data_path, array_index, keys) -> None:
        self.data_path = data_path
        self.array_index = array_index
        self.keyframe_points = [_Key(frame, value) for frame, value in keys]

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
    return types.SimpleNamespace(name=name, matrix_world=_Matrix.Translation(location))


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
        {"target_object": "SH030_cam", "track_axis": "Z", "up_axis": "-X", "up_reference": (0, 0, 1)},
        head.name,
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

    aim = posing._validated_aim({"target": (0.0, -2.0, 0.75), "track_axis": "Z"}, head.name)
    matrix = posing._aim_pose_matrix(rig, head, aim)

    direction = ((rig.matrix_world.inverted() @ _Vector((0.0, -2.0, 0.75))) - head.matrix.translation).normalized()
    assert matrix.to_3x3().col[2].dot(direction) == pytest.approx(1.0, abs=1e-12)


def test_aim_rejects_every_direction_it_cannot_define(monkeypatch) -> None:
    _server, rig, _animation, posing, _spine, head = _head_rig(monkeypatch)
    head_world = rig.matrix_world @ head.matrix.translation

    with pytest.raises(ValueError, match="at the head of 'CHAR1_head_jnt'"):
        posing._aim_pose_matrix(
            rig, head, posing._validated_aim({"target": tuple(head_world), "track_axis": "Z"}, head.name)
        )
    forward = tuple(rig.matrix_world @ (head.matrix.translation + _Vector((0.0, 0.0, 0.5))))
    with pytest.raises(ValueError, match="parallel to the aim direction"):
        posing._aim_pose_matrix(
            rig,
            head,
            posing._validated_aim(
                {"target": forward, "track_axis": "Z", "up_axis": "X", "up_reference": (0, 0, 1)}, head.name
            ),
        )
    with pytest.raises(ValueError, match="is a zero vector"):
        posing._validated_aim(
            {"target": (0, -2, 0.75), "track_axis": "Z", "up_axis": "X", "up_reference": (0, 0, 0)}, head.name
        )
    with pytest.raises(ValueError, match="different bone axis than track_axis"):
        posing._validated_aim({"target": (0, -2, 0.75), "track_axis": "Z", "up_axis": "-Z"}, head.name)
    with pytest.raises(ValueError, match="must be one of X, -X, Y, -Y, Z, -Z"):
        posing._validated_aim({"target": (0, -2, 0.75), "track_axis": "W"}, head.name)
    with pytest.raises(ValueError, match="exactly one of target or target_object"):
        posing._validated_aim({"target": (0, -2, 0.75), "target_object": "SH030_cam", "track_axis": "Z"}, head.name)
    with pytest.raises(ValueError, match=r"aim_at\.target_object not found: SH030_cam"):
        posing._validated_aim({"target_object": "SH030_cam", "track_axis": "Z"}, head.name)


def test_posing_an_aim_lands_it_on_the_target_after_the_parent_has_moved(monkeypatch) -> None:
    camera = _target_object("SH030_cam", (0.6, -4.7, 0.95))
    server, rig, _animation, _posing, spine, head = _head_rig(monkeypatch, objects={"SH030_cam": camera})

    reply = server.set_character_pose(
        "CHAR1_rig",
        [
            {"bone_name": spine.name, "rotation_euler": (0.0, 0.4, 0.0)},
            {"bone_name": head.name, "aim_at": {"target_object": "SH030_cam", "track_axis": "Z", "up_axis": "-X"}},
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
        posing._aim_pose_matrix(rig, head, posing._validated_aim({"target": behind, "track_axis": "Z"}, head.name))

    # The same swing is well defined once the roll is pinned.
    matrix = posing._aim_pose_matrix(
        rig,
        head,
        posing._validated_aim({"target": behind, "track_axis": "Z", "up_axis": "Y"}, head.name),
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
            [{"bone_name": head.name, "aim_at": {"target_object": "SH030_cam", "track_axis": "Z"}}],
        )

    assert animation.action is None


def _aim_key(server, head, frame, target, mode="QUATERNION"):
    return server.keyframe_character_pose(
        "CHAR1_rig",
        "CHAR1_sh030_motion",
        frame,
        [{"bone_name": head.name, "aim_at": {"target": target, "track_axis": "Z", "up_axis": "-X"}}],
        action_policy="REUSE",
    )


def test_a_keyed_aim_takes_the_short_way_round_from_the_previous_key(monkeypatch) -> None:
    server, rig, animation, posing, _spine, head = _head_rig(monkeypatch)
    action = _Action("CHAR1_sh030_motion")
    sys.modules["bpy"].data.actions["CHAR1_sh030_motion"] = action
    aim = posing._validated_aim({"target": (0.0, -2.0, 0.75), "track_axis": "Z", "up_axis": "-X"}, head.name)
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
    aim = posing._validated_aim({"target_object": "SH030_cam", "track_axis": "Z", "up_axis": "-X"}, head.name)
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
        [{"bone_name": head.name, "aim_at": {"target_object": "SH030_cam", "track_axis": "Z", "up_axis": "-X"}}],
        action_policy="REUSE",
    )

    keyed = next(values for path, _frame, _group, values in head.keyed if path == "rotation_euler")
    jump = max(abs(value - reference) for value, reference in zip(keyed, previous, strict=True))
    naive = max(abs(value - reference) for value, reference in zip(natural, previous, strict=True))
    assert naive == pytest.approx(2.0 * math.pi, abs=1e-9)
    assert math.degrees(jump) < 1e-6


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
        data=types.SimpleNamespace(name="CHAR1_rigData", bones=[bone]),
        update_from_editmode=lambda: None,
    )
    addon, _bpy = _load_addon(monkeypatch, data={"objects": {"CHAR1_rig": rig}})
    server = addon.BlenderMCPServer()

    plain = server.list_character_bones("CHAR1_rig")
    with_axes = server.list_character_bones("CHAR1_rig", rest_axes=True)

    assert "rest_axes" not in plain["bones"]["items"][0]
    # A quarter turn about Z sends the bone's rest X to armature +Y and its Y to armature -X.
    assert with_axes["bones"]["items"][0]["rest_axes"] == [0.0, 1.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0, 1.0]


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
