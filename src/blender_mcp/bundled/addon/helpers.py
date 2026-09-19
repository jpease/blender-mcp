import contextlib

import bmesh
import bpy
import mathutils

from . import ADDON_ID


def get_blendermcp_addon_preferences(context=None):
    """
    Get add-on preferences object if available.

    Args:
        context: Value for context.

    Returns:
        Result produced by the operation.

    """
    if context is None:
        context = bpy.context
    addon = context.preferences.addons.get(ADDON_ID)
    return addon.preferences if addon else None


# region Mesh/model editing helpers
def get_mesh_object(name):
    """
    Look up an object by name and require it to be a mesh.

    Args:
        name: Name to assign or look up.

    Returns:
        Result produced by the operation.

    Raises:
        ValueError: If the operation cannot be completed.

    """
    obj = bpy.data.objects.get(name)
    if not obj:
        raise ValueError(f"Object not found: {name}")
    if obj.type != "MESH":
        raise ValueError(f"Object '{name}' is not a mesh (type={obj.type})")
    return obj


def sync_from_editmode(obj) -> None:
    """
    Flush Edit-Mode's live data back into obj.data before reading it.

    Blender's API docs ("Modes and Mesh Access") warn that obj.data is out of
    sync with the edit mesh while the object is in Edit Mode - Edit-Mode owns
    its own copy and only writes it back on exit. update_from_editmode() is
    the API's escape hatch to flush it back without leaving Edit Mode. Safe
    to call unconditionally; it's a no-op when obj isn't in Edit Mode.

    Args:
        obj: Value for obj.

    """
    obj.update_from_editmode()


@contextlib.contextmanager
def preserve_mode_and_selection():
    """
    Snapshot the current mode, active object, and selection; force Object
    Mode for the wrapped block; restore the snapshot on exit - success or
    failure.

    Blender's mesh/object operators require Object Mode and a specific
    active/selected object to pass poll() (Blender API docs, "Using
    Operators": poll typically checks "the active area type, a selection or
    active object"). Code inside the `with` block is responsible for
    returning to Object Mode before the block ends (e.g. edit_mesh already
    does this in its own finally) - this only guarantees a clean Object Mode
    starting point and restores the caller's prior state afterward.
    """
    prev_active = bpy.context.view_layer.objects.active
    prev_selected = list(bpy.context.selected_objects)
    prev_mode = prev_active.mode if prev_active else "OBJECT"
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    try:
        yield
    finally:
        bpy.ops.object.select_all(action="DESELECT")
        for obj in prev_selected:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = prev_active
        if prev_active is not None and prev_mode != "OBJECT":
            bpy.ops.object.mode_set(mode=prev_mode)


def set_active(obj) -> None:
    """
    Make obj the sole selected + active object.

    Must be called from inside a `with preserve_mode_and_selection():`
    block - select_all/select_set require Object Mode to pass poll().

    Args:
        obj: Value for obj.

    """
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def _select_geometry(obj, vert_indices=None, edge_indices=None, face_indices=None) -> None:
    """
    Enter edit mode on obj and select exactly the given indices, or everything if all are omitted.

    An explicitly-passed empty list means "select none of this component type" -
    it must not be treated the same as omitting the argument (which means "all").

    Args:
        obj: Value for obj.
        vert_indices: Value for vert indices.
        edge_indices: Indices of edges to operate on.
        face_indices: Indices of faces to operate on.

    """
    set_active(obj)
    bpy.ops.object.mode_set(mode="EDIT")
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    any_given = vert_indices is not None or edge_indices is not None or face_indices is not None
    for v in bm.verts:
        v.select = not any_given
    for e in bm.edges:
        e.select = not any_given
    for f in bm.faces:
        f.select = not any_given
    mode = set()
    if vert_indices is not None:
        mode.add("VERT")
        for i in vert_indices:
            bm.verts[i].select = True
    if edge_indices is not None:
        mode.add("EDGE")
        for i in edge_indices:
            bm.edges[i].select = True
    if face_indices is not None:
        mode.add("FACE")
        for i in face_indices:
            bm.faces[i].select = True
    mode = mode or {"VERT", "EDGE", "FACE"}
    bm.select_mode = mode
    bpy.context.tool_settings.mesh_select_mode = tuple(c in mode for c in ("VERT", "EDGE", "FACE"))
    # select_flush(True) flushes only upward from vertex selection, independent
    # of select_mode - it would select any edge/face that merely shares
    # selected vertices, leaking into geometry the caller never asked to
    # touch. select_flush_mode() reconciles selection consistent with
    # select_mode instead (e.g. pushing a selected face's selection down to
    # its own edges/verts) without inventing selections among unrelated
    # elements.
    bm.select_flush_mode()
    bmesh.update_edit_mesh(obj.data)


def exit_edit_mode() -> None:
    bpy.ops.object.mode_set(mode="OBJECT")


def _validate_indices(obj, attr, indices) -> None:
    """
    Raise a clear error if any index is out of range for obj.data.<attr>, before edit mode is entered.

    Args:
        obj: Value for obj.
        attr: Value for attr.
        indices: Value for indices.

    Raises:
        ValueError: If the operation cannot be completed.

    """
    if indices is None:
        return
    total = len(getattr(obj.data, attr))
    for i in indices:
        if not (0 <= i < total):
            raise ValueError(f"Index {i} out of range for {attr} (0-{total - 1}) on '{obj.name}'")


@contextlib.contextmanager
def edit_mesh(obj, vert_indices=None, edge_indices=None, face_indices=None):
    """
    Enter edit mode on obj, select the given indices, and always exit edit mode afterward.

    Indices are validated against the base mesh before edit mode is entered, so
    an out-of-range index raises a clear ValueError instead of bmesh's bare
    IndexError - and the mode restoration in the finally block happens even if
    the caller's operator inside the `with` block raises.

    Wrapped in preserve_mode_and_selection() so entering/leaving Edit Mode
    on obj is guaranteed to start from - and return to - the caller's real
    prior mode, active object, and selection.

    Args:
        obj: Value for obj.
        vert_indices: Value for vert indices.
        edge_indices: Indices of edges to operate on.
        face_indices: Indices of faces to operate on.

    """
    sync_from_editmode(obj)
    _validate_indices(obj, "vertices", vert_indices)
    _validate_indices(obj, "edges", edge_indices)
    _validate_indices(obj, "polygons", face_indices)
    with preserve_mode_and_selection():
        _select_geometry(
            obj,
            vert_indices=vert_indices,
            edge_indices=edge_indices,
            face_indices=face_indices,
        )
        try:
            yield
        finally:
            exit_edit_mode()


def paginate(total, offset, limit, max_limit):
    """
    Clamp offset/limit against total and return (start, end, truncated, next_offset).

    Args:
        total: Value for total.
        offset: Zero-based starting position.
        limit: Maximum number of items to return.
        max_limit: Value for max limit.

    Returns:
        Result produced by the operation.

    """
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), max_limit))
    start = min(offset, total)
    end = min(start + limit, total)
    truncated = end < total
    return start, end, truncated, (end if truncated else None)


def mesh_counts(obj):
    return {
        "vertices": len(obj.data.vertices),
        "edges": len(obj.data.edges),
        "polygons": len(obj.data.polygons),
    }


def apply_modifier(obj, modifier) -> None:
    with preserve_mode_and_selection():
        set_active(obj)
        bpy.ops.object.modifier_apply(modifier=modifier.name)


def _world_bounds(matrix_world, vertices):
    """
    Compute the world-space axis-aligned bounding box of vertices.

    Args:
        matrix_world: Value for matrix world.
        vertices: Value for vertices.

    Returns:
        Result produced by the operation.

    """
    if not vertices:
        return {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0]}
    coords = [matrix_world @ v.co for v in vertices]
    xs = [c.x for c in coords]
    ys = [c.y for c in coords]
    zs = [c.z for c in coords]
    return {
        "min": [min(xs), min(ys), min(zs)],
        "max": [max(xs), max(ys), max(zs)],
    }


def modifier_result(obj, modifier, applied):
    """
    Report base-mesh counts plus modifier-evaluated counts/name/bounds.

    When apply=False, mesh_counts(obj) only reflects the base mesh - the
    live modifier's effect is invisible unless it's read from the
    depsgraph-evaluated object instead.

    Args:
        obj: Value for obj.
        modifier: Value for modifier.
        applied: Value for applied.

    Returns:
        Result produced by the operation.

    """
    base = mesh_counts(obj)
    if applied or modifier is None:
        return {
            **base,
            "applied": bool(applied),
            "modifier": None,
            "evaluated": dict(base),
            "bounds": _world_bounds(obj.matrix_world, obj.data.vertices),
        }
    depsgraph = bpy.context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(depsgraph)
    eval_mesh = eval_obj.data
    evaluated = {
        "vertices": len(eval_mesh.vertices),
        "edges": len(eval_mesh.edges),
        "polygons": len(eval_mesh.polygons),
    }
    return {
        **base,
        "applied": False,
        "modifier": modifier.name,
        "evaluated": evaluated,
        "bounds": _world_bounds(eval_obj.matrix_world, eval_mesh.vertices),
    }


def get_rotation_quaternion(obj):
    """
    Read obj's rotation as a quaternion, regardless of its rotation_mode.

    Args:
        obj: Value for obj.

    Returns:
        Result produced by the operation.

    """
    if obj.rotation_mode == "QUATERNION":
        return obj.rotation_quaternion.copy()
    if obj.rotation_mode == "AXIS_ANGLE":
        angle, x, y, z = obj.rotation_axis_angle
        return mathutils.Quaternion((x, y, z), angle)
    return obj.rotation_euler.to_quaternion()


def set_rotation_quaternion(obj, quat) -> None:
    """
    Write a quaternion to obj, converting to whatever rotation_mode it uses.

    Args:
        obj: Value for obj.
        quat: Value for quat.

    """
    if obj.rotation_mode == "QUATERNION":
        obj.rotation_quaternion = quat
    elif obj.rotation_mode == "AXIS_ANGLE":
        axis, angle = quat.to_axis_angle()
        obj.rotation_axis_angle = (angle, axis.x, axis.y, axis.z)
    else:
        obj.rotation_euler = quat.to_euler(obj.rotation_mode)


def rotation_as_native_list(obj):
    """
    Read obj's rotation in whatever representation its rotation_mode natively uses.

    Returns [x, y, z] for Euler modes, [w, x, y, z] for QUATERNION, or
    [angle, x, y, z] for AXIS_ANGLE - avoiding to_euler(), whose order
    argument only accepts the six Euler order strings, not QUATERNION/AXIS_ANGLE.

    Args:
        obj: Value for obj.

    Returns:
        Result produced by the operation.

    """
    if obj.rotation_mode == "QUATERNION":
        q = obj.rotation_quaternion
        return [q.w, q.x, q.y, q.z]
    if obj.rotation_mode == "AXIS_ANGLE":
        return list(obj.rotation_axis_angle)
    e = obj.rotation_euler
    return [e.x, e.y, e.z]


def pivot_rotation_matrix(pivot, axis, angle):
    """
    Build a world-space matrix that rotates by angle around axis, pivoting at pivot.

    Matrix.Rotation alone only rotates around the world origin - conjugating
    it with Translation(pivot)/Translation(-pivot) shifts the pivot to an
    arbitrary world point: Translate(pivot) @ Rotate(angle, axis) @ Translate(-pivot).

    Args:
        pivot: World-space point to rotate around.
        axis: 'X', 'Y', or 'Z'.
        angle: Rotation angle in radians.

    Returns:
        Result produced by the operation.

    """
    translate_to_pivot = mathutils.Matrix.Translation(pivot)
    rotate = mathutils.Matrix.Rotation(angle, 4, axis)
    translate_back = mathutils.Matrix.Translation(-pivot)
    return translate_to_pivot @ rotate @ translate_back


def select_objects(names, active_name=None):
    """
    Deselect everything, select the named objects, and set the active object.

    Args:
        names: Value for names.
        active_name: Name of the active.

    Returns:
        Result produced by the operation.

    Raises:
        ValueError: If the operation cannot be completed.

    """
    if not names:
        raise ValueError("At least one object name is required")
    objs = []
    for name in names:
        obj = bpy.data.objects.get(name)
        if not obj:
            raise ValueError(f"Object not found: {name}")
        objs.append(obj)
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objs:
        obj.select_set(True)
    active = bpy.data.objects.get(active_name) if active_name else objs[-1]
    bpy.context.view_layer.objects.active = active
    return objs


def find_view3d():
    """
    Locate a VIEW_3D area/region, needed to override bpy.context.space_data for ND's viewport operators.

    Returns:
        Result produced by the operation.

    """
    for area in bpy.context.screen.areas:
        if area.type == "VIEW_3D":
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region is not None:
                return area, region
    return None, None


@contextlib.contextmanager
def nd_view3d_override():
    """
    Override bpy.context to a real VIEW_3D area/region for ND operators that read bpy.context.space_data.

    Raises:
        RuntimeError: If no 3D viewport is open to override into.

    """
    area, region = find_view3d()
    if area is None:
        raise RuntimeError("No 3D viewport found to run this ND operator")
    with bpy.context.temp_override(area=area, region=region):
        yield


def nd_call(op_name, *args, **kwargs):
    """
    Look up and call an ND operator by name, raising a clear error if it isn't installed.

    Unlike a pre-resolved bpy.ops.nd.<op_name> reference (which raises a bare
    AttributeError if missing), resolving op_name here gives every caller the
    same clear "not available" error. Raises if the operator unexpectedly
    enters a modal state; otherwise returns (result, cancelled) so callers can
    surface a CANCELLED result (Blender's "aborted / preconditions not met"
    status) instead of treating it as indistinguishable from FINISHED.

    Args:
        op_name: Name of the bpy.ops.nd operator to call, e.g. "bool_vanilla".
        args: Positional args forwarded to the operator (e.g. a call context string).
        kwargs: Keyword args forwarded to the operator.

    Returns:
        (result, cancelled): the raw operator result set, and whether it contained CANCELLED.

    Raises:
        RuntimeError: If the operator isn't available, or enters a modal state unexpectedly.

    """
    nd_ops = getattr(bpy.ops, "nd", None)
    op = getattr(nd_ops, op_name, None) if nd_ops is not None else None
    if op is None:
        raise RuntimeError(
            f"ND operator 'nd.{op_name}' is not available - check that the ND addon is installed/enabled"
        )
    result = op(*args, **kwargs)
    if "RUNNING_MODAL" in result:
        raise RuntimeError(f"nd.{op_name} entered a modal state unexpectedly - not safe to call headlessly")
    return result, "CANCELLED" in result


def nd_configure_object_as_util(obj, util=True) -> None:
    """
    Replicate ND's lib/objects.configure_object_as_util (mark/unmark a utility object).

    Args:
        obj: Value for obj.
        util: Value for util.

    """
    obj.display_type = "WIRE" if util else "SOLID"
    obj.hide_render = util
    obj.visible_camera = not util
    obj.visible_diffuse = not util
    obj.visible_glossy = not util
    obj.visible_shadow = not util
    obj.visible_transmission = not util
    obj.visible_volume_scatter = not util


# endregion
