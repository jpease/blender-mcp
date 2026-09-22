"""
The fake Blender the character-rigging tests pose.

`mathutils` is a C extension that exists only inside Blender, so the geometry in these tests
runs against `_math`, an independent implementation of the handful of vector, matrix and
quaternion operations the handlers call. That proves the geometry the handler asks for - which
column lands on the aim direction, which bone sees its parent's fresh transform - not Blender's
numerics; `tests/blender_character_posing_smoke.py` measures those against the real API.

It lives beside the tests rather than inside one of them because posing, bone listing and reach
solving all need the same rig standing in the same fake scene, and a copy per file would drift.
"""

import math
import sys
import types

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

    def __init__(
        self, name, rest_relative=None, parent=None, rotation_mode="QUATERNION", use_deform=True, length=0.1
    ) -> None:
        self.name = name
        self.use_deform = use_deform
        self.parent = parent
        self.length = float(length)
        self.children: list = []
        if parent is not None:
            parent.children.append(self)
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
    def children_recursive(self) -> list:
        return [node for child in self.children for node in (child, *child.children_recursive)]

    @property
    def head_local(self) -> _Vector:
        """The bone's rest head in armature space."""
        return self.rest_pose.translation

    @property
    def tail_local(self) -> _Vector:
        """The bone's rest tail: its own +Y runs head to tail, as Blender builds every bone."""
        return self.head_local + self.rest_pose.to_3x3().col[1].normalized() * self.length

    @property
    def bone(self) -> types.SimpleNamespace:
        """
        The rest bone behind the pose bone: `matrix_local` is its armature-space rest.

        `head_local`, `tail_local` and `children_recursive` are the rest facts the twist
        measurement reads - where the bone's length axis runs, and what hangs off it.
        """
        return types.SimpleNamespace(
            name=self.name,
            matrix_local=self.rest_pose,
            use_deform=self.use_deform,
            length=self.length,
            head_local=self.head_local,
            tail_local=self.tail_local,
            children_recursive=[child.bone for child in self.children_recursive],
        )

    @property
    def head(self) -> _Vector:
        """Where the posed bone's head sits in armature space, as `PoseBone.head` reports it."""
        return self.matrix.translation

    @property
    def tail(self) -> _Vector:
        """Where the posed bone's tail sits: its head, plus its own posed +Y over its length."""
        return self.head + self.matrix.to_3x3().col[1].normalized() * self.length

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


class _SceneObjects(dict):
    """
    `bpy.data.objects`: name lookups, and an iteration that yields the objects themselves.

    A plain dict iterates its keys, which is not what Blender's collection does - and the twist
    measurement finds the meshes bound to a rig by walking `bpy.data.objects` and reading each
    one's `type`, so a name-yielding stand-in would make that scan fail rather than find nothing.
    """

    def __iter__(self):
        return iter(self.values())


class _VertexGroups(list):
    """`Object.vertex_groups`: a sequence that also resolves a group by name."""

    def get(self, name, default=None):
        return next((group for group in self if group.name == name), default)


def _skinned_mesh(name, armature_obj, weights, *, matrix_world=None, groups=None):
    """
    Build a mesh bound to a rig, with named vertex groups weighting its vertices to bones.

    Args:
        name: The mesh object's name.
        armature_obj: The rig it is parented to, which is how `_mesh_uses_armature_data` finds it.
        weights: `{bone_name: [(x, y, z), ...]}` - the rest positions weighted to each bone.
        matrix_world: The mesh object's own matrix; identity when omitted.
        groups: Extra vertex group names the mesh carries but weights nothing to, for the case
            where a group exists and no vertex uses it.

    Returns:
        types.SimpleNamespace: The mesh object.

    """
    names = list(dict.fromkeys([*weights, *(groups or ())]))
    vertex_groups = _VertexGroups(types.SimpleNamespace(name=group, index=index) for index, group in enumerate(names))
    vertices = []
    for group, points in weights.items():
        index = names.index(group)
        for point in points:
            vertices.append(
                types.SimpleNamespace(
                    index=len(vertices),
                    co=_Vector(point),
                    groups=[types.SimpleNamespace(group=index, weight=1.0)],
                )
            )
    return types.SimpleNamespace(
        name=name,
        type="MESH",
        parent=armature_obj,
        modifiers=[],
        vertex_groups=vertex_groups,
        data=types.SimpleNamespace(vertices=vertices),
        matrix_world=matrix_world or _Matrix.Identity(4),
    )


class _Rig(types.SimpleNamespace):
    """
    The armature object, compared and hashed by identity as a Blender ID is.

    `SimpleNamespace` trades `__hash__` for value equality, and the handlers put objects in sets
    - `_armature_users` collects the rigs sharing an armature datablock that way - so a rig
    built as a bare namespace is a `TypeError` rather than a scene one of them can be found in.
    """

    def __hash__(self) -> int:
        return object.__hash__(self)

    def __eq__(self, other: object) -> bool:
        return self is other


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
    rig = _Rig(
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
    scene_objects = _SceneObjects({"CHAR1_rig": rig, **(objects or {})})
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
