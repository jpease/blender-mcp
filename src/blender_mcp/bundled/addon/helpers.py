import contextlib

import bmesh
import bpy
import mathutils

from . import ADDON_ID


def runtime_enum_item_name(owner, property_name, identifier):
    """
    Ask Blender whether a dynamic enum currently offers an item, by its own callback.

    The same trap `handlers/lighting/_shared.engine_identifiers` documents for `engine`:
    `owner.bl_rna.properties[name].enum_items` reads the *static* RNA definition, and an enum
    whose items come from a runtime callback is not in it. Measured on Blender 5.2.2
    `--factory-startup`: `view_settings.bl_rna.properties["view_transform"].enum_items` is
    `['NONE']`, while the live property offers AgX, Filmic, Standard and Khronos PBR Neutral.
    Reading the static list therefore finds nothing and the caller silently keeps the default.

    `UILayout.enum_item_name` runs the callback against a live instance, which is the only way
    to see what the property actually accepts right now.

    Args:
        owner: The RNA instance owning the property, not its type.
        property_name: The enum property's identifier.
        identifier: The candidate item to look for.

    Returns:
        str: The item's display name, or an empty string when it is not currently offered.

    """
    return bpy.types.UILayout.enum_item_name(owner, property_name, identifier)


def color_management_snapshot(scene):
    """
    Serialize the scene's display transform: the view settings every render and preview passes through.

    Args:
        scene: The scene whose `view_settings` to read.

    Returns:
        dict: `view_transform`, `look`, `exposure` in stops, its `exposure_multiplier`
        (`2 ** exposure`), and `gamma`.

    """
    settings = scene.view_settings
    return {
        "view_transform": settings.view_transform,
        "look": settings.look,
        "exposure": float(settings.exposure),
        "exposure_multiplier": float(2.0**settings.exposure),
        "gamma": float(settings.gamma),
    }


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


def deforming_meshes(armature, scene=None):
    """
    List the meshes whose shape an armature drives, and how each one is bound to it.

    The two ways Blender actually binds a mesh to a rig. Parent-type ARMATURE is the older
    route and is still what `Ctrl+P > With Automatic Weights` leaves behind on a proxy, so
    checking only the modifier stack would silently drop half a character. Shared rather than
    respelled per domain: camera framing and the posing surface must not disagree about which
    meshes a rig moves, or a shot is framed on a set one tool reports and another denies. Framing
    then drops the members `render_exclusion_reason` rejects, and names each with its reason.

    Narrower on purpose than `handlers/character_rigging/records._dependent_meshes`, which
    answers "what depends on this rig?" and counts a prop merely parented to it. This answers
    "whose shape does it drive?", which is the set worth framing and worth sampling.

    Args:
        armature: The armature object to resolve.
        scene: The scene to search; defaults to the active one. Objects outside it are not
            rendered and not framed, so they are not part of the answer.

    Returns:
        list[tuple]: `(object, binding, modifier_enabled)` per mesh, in scene order. `binding`
        is "MODIFIER", "PARENT" or "BOTH"; `modifier_enabled` is None for a PARENT-only bind,
        and otherwise whether any of its Armature modifiers is visible in the viewport - a
        disabled one is why a bound mesh does not move.

    """
    scene = scene if scene is not None else bpy.context.scene
    bound = []
    for obj in scene.objects:
        if obj.type != "MESH":
            continue
        modifiers = [
            modifier for modifier in obj.modifiers if modifier.type == "ARMATURE" and modifier.object == armature
        ]
        parented = obj.parent == armature and obj.parent_type == "ARMATURE"
        if not modifiers and not parented:
            continue
        binding = "BOTH" if modifiers and parented else ("MODIFIER" if modifiers else "PARENT")
        enabled = any(modifier.show_viewport for modifier in modifiers) if modifiers else None
        bound.append((obj, binding, enabled))
    return bound


def _render_paths(layer_collection, users, hidden=False, excluded=False):
    """
    Yield `(hidden, excluded)` once per layer-collection path that links one of `users` directly.

    A collection's `hide_render` hides everything beneath it in every view layer, and a layer
    collection's `exclude`, `holdout` and `indirect_only` keep everything beneath it out of the
    camera's direct view in this view layer. Both are inherited down the tree, so they are carried
    along the walk rather than read off the one collection that links the object.
    """
    collection = layer_collection.collection
    hidden = hidden or collection.hide_render
    excluded = excluded or layer_collection.exclude or layer_collection.holdout or layer_collection.indirect_only
    if collection in users:
        yield hidden, excluded
    for child in layer_collection.children:
        yield from _render_paths(child, users, hidden, excluded)


# The object's own render switches, in the order `render_exclusion_reason` reports them.
_OBJECT_EXCLUSIONS = (
    ("HIDE_RENDER", lambda obj: obj.hide_render),
    ("HOLDOUT", lambda obj: obj.is_holdout),
    ("NO_CAMERA_RAYS", lambda obj: not obj.visible_camera),
    ("DISPLAY_WIRE_OR_BOUNDS", lambda obj: obj.display_type in {"WIRE", "BOUNDS"}),
)


def _collection_exclusion(obj, scene, view_layer):
    """
    Say whether every collection path leaves `obj` out of `view_layer`'s render.

    Returns:
        str | None: COLLECTION_HIDE_RENDER, VIEW_LAYER_EXCLUDED, or None when a path renders it.

    """
    if view_layer is None:
        view_layer = bpy.context.view_layer if bpy.context.scene == scene else scene.view_layers[0]
    paths = list(_render_paths(view_layer.layer_collection, list(obj.users_collection)))
    if any(not hidden and not excluded for hidden, excluded in paths):
        return None
    if paths and all(hidden for hidden, _excluded in paths):
        return "COLLECTION_HIDE_RENDER"
    return "VIEW_LAYER_EXCLUDED"


def _armature_binding_exclusion(obj, armature):
    """
    Say whether `obj` is bound to `armature` only through render-disabled Armature modifiers.

    Returns:
        str | None: ARMATURE_MODIFIER_DISABLED, or None when the rig moves it in the render.

    """
    if obj.parent == armature and obj.parent_type == "ARMATURE":
        return None
    bindings = [modifier for modifier in obj.modifiers if modifier.type == "ARMATURE" and modifier.object == armature]
    if bindings and not any(modifier.show_render for modifier in bindings):
        return "ARMATURE_MODIFIER_DISABLED"
    return None


def render_exclusion_reason(obj, scene, view_layer=None, *, armature=None):
    """
    Say why an object adds nothing to what a camera renders, or None when it does.

    Camera framing is the consumer: a rig binds collision cages, simulation proxies and helper
    meshes as well as its skin, and this repo itself stamps such helpers `hide_render` and
    `display_type = 'WIRE'`. Framing them fits the shot around geometry the render never shows.
    Codes, checked in this order so an object hidden several ways reports the first one to clear:

    - HIDE_RENDER: the object's own Disable in Renders.
    - HOLDOUT: the object renders as a holdout, a transparent hole rather than a surface.
    - NO_CAMERA_RAYS: its camera ray visibility is off, so no camera sees it directly.
    - DISPLAY_WIRE_OR_BOUNDS: displayed as wire or bounds, the convention for helper geometry.
    - COLLECTION_HIDE_RENDER: every collection path to it in the scene is disabled in renders.
    - VIEW_LAYER_EXCLUDED: every path to it in the view layer is excluded, holdout or
      indirect-only, or the view layer does not reach it at all.
    - ARMATURE_MODIFIER_DISABLED: bound to `armature` only through Armature modifiers that are
      all disabled in renders, so the rig does not move it in the shot.

    Args:
        obj: The object to judge.
        scene: The scene it would render in.
        view_layer: The view layer to judge it in; defaults to the one the viewport evaluates when
            `scene` is the context scene, and otherwise the scene's first.
        armature: The rig the object was reached through, which enables the
            ARMATURE_MODIFIER_DISABLED check; None skips it.

    Returns:
        str | None: The first code that applies, or None when the object renders.

    """
    for code, excludes in _OBJECT_EXCLUSIONS:
        if excludes(obj):
            return code
    reason = _collection_exclusion(obj, scene, view_layer)
    if reason is None and armature is not None:
        reason = _armature_binding_exclusion(obj, armature)
    return reason


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
    # bmesh's sequence types are iterable at runtime; the stubs type `__iter__`
    # as returning None, so pyright reads them as non-iterable.
    for v in bm.verts:  # pyright: ignore[reportGeneralTypeIssues]
        v.select = not any_given
    for e in bm.edges:  # pyright: ignore[reportGeneralTypeIssues]
        e.select = not any_given
    for f in bm.faces:  # pyright: ignore[reportGeneralTypeIssues]
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
    """
    Apply one modifier to obj's mesh through `bpy.ops.object.modifier_apply`.

    Runs in Object Mode with obj as the sole selected, active object, then restores the
    caller's mode and selection. The operator reports refusal by returning a set without
    FINISHED rather than raising, so a result lacking FINISHED is raised here: a caller
    that returns after this call has an applied modifier, never a cancelled one.

    Args:
        obj: The object that owns the modifier.
        modifier: The modifier to apply; must belong to obj.

    Raises:
        RuntimeError: When the operator did not finish, naming the object, the modifier,
            and the operator's result.

    """
    name = modifier.name
    with preserve_mode_and_selection():
        set_active(obj)
        result = bpy.ops.object.modifier_apply(modifier=name)
    if "FINISHED" not in result:
        raise RuntimeError(
            f"Blender did not apply modifier '{name}' on '{obj.name}' (operator returned {sorted(result)})"
        )


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


def spread_indices(count, limit):
    """
    Choose at most `limit` evenly spread indices from `range(count)`.

    Spread rather than taken from the front: the first thousand vertices of a character are one
    body part, and a measurement that sampled only those would describe a rig that moves one
    limb the same way it describes a rig that moves nothing.

    Args:
        count: How many elements exist.
        limit: The most indices to return.

    Returns:
        list[int]: Ascending indices, every one of them when `count <= limit`.

    """
    if count <= limit:
        return list(range(count))
    step = count / limit
    return sorted({min(count - 1, int(index * step)) for index in range(limit)})


def evaluated_world_bounds(evaluated_obj):
    """
    Report an evaluated object's world-space axis-aligned bounds, corner by corner.

    Distinct from `_world_bounds` above, which measures a vertex list and answers `min`/`max`
    for `modifier_result`'s long-standing reply shape. This one reads `bound_box`, which the
    depsgraph has already computed, and answers the `minimum`/`maximum` shape the physics and
    rigging replies use. Shared so those replies cannot drift apart per domain.

    Args:
        evaluated_obj: An object from `evaluated_get`, whose `bound_box` and `matrix_world`
            describe the evaluated result.

    Returns:
        dict: `coordinate_space`, `minimum` and `maximum`.

    """
    corners = [evaluated_obj.matrix_world @ mathutils.Vector(corner) for corner in evaluated_obj.bound_box]
    return {
        "coordinate_space": "WORLD",
        "minimum": [min(corner[axis] for corner in corners) for axis in range(3)],
        "maximum": [max(corner[axis] for corner in corners) for axis in range(3)],
    }


def modifier_result(obj, modifier, applied):
    """
    Report base-mesh counts plus modifier-evaluated counts/name/bounds.

    When apply=False, mesh_counts(obj) only reflects the base mesh - the
    live modifier's effect is invisible unless it's read from the
    depsgraph-evaluated object instead.

    Args:
        obj: The object that owns (or owned) the modifier.
        modifier: The live modifier, or None when there is none to evaluate.
        applied: True only after `apply_modifier` returned for this modifier, which it does
            only when Blender's operator finished; the reply echoes it as `applied`.

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
