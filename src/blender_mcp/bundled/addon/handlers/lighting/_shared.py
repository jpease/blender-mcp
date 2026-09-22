# pyright: reportGeneralTypeIssues=false, reportOptionalSubscript=false
"""Validation, lookup, serialization, and managed-node helpers shared by lighting handlers."""

import math
import os

import bpy
import mathutils

from ...helpers import paginate, rotation_as_native_list

COMMON_LIGHT_FIELDS = {
    "energy",
    "exposure",
    "normalize",
    "color",
    "use_temperature",
    "temperature",
    "use_shadow",
    "diffuse_factor",
    "specular_factor",
    "transmission_factor",
    "volume_factor",
    "use_custom_distance",
    "cutoff_distance",
}
TYPE_LIGHT_FIELDS = {
    "AREA": {"shape", "size", "size_y", "spread"},
    "POINT": {"shadow_soft_size", "use_soft_falloff"},
    "SPOT": {"shadow_soft_size", "use_soft_falloff", "spot_size", "spot_blend", "show_cone"},
    "SUN": {"angle"},
}
LIGHT_TYPES = frozenset(TYPE_LIGHT_FIELDS)
MANAGED_OWNER = "blender-mcp"
MAX_NODE_SUMMARY = 100
# Blender stores transforms as float32, so publishing a matrix at repr precision spends bytes on
# digits the data never carried. Six decimals is finer than float32 resolves at scene scale.
TRANSFORM_DECIMALS = 6


def finite_number(value, label):
    """Return a finite float or raise a precise validation error."""
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def finite_vector(value, label, *, length=3):
    """Validate a fixed-length numeric vector."""
    if value is None or len(value) != length:
        raise ValueError(f"{label} must contain exactly {length} numbers")
    return tuple(finite_number(component, f"{label}[{index}]") for index, component in enumerate(value))


def required_name(value, label):
    """Validate a non-empty Blender resource name."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def bounded_page(total, offset, limit):
    """Validate and calculate a standard lighting page."""
    if isinstance(offset, bool) or int(offset) != offset or not 0 <= int(offset) <= 9_999:
        raise ValueError("offset must be an integer in [0, 9999]")
    if isinstance(limit, bool) or int(limit) != limit or not 1 <= int(limit) <= 200:
        raise ValueError("limit must be an integer in [1, 200]")
    return paginate(total, int(offset), int(limit), 200)


def scene_by_name(name):
    """Resolve one exact scene."""
    scene = bpy.data.scenes.get(required_name(name, "scene_name"))
    if scene is None:
        raise ValueError(f"Scene not found: {name}")
    return scene


def object_in_scene(scene, name):
    """Resolve one exact object and require scene membership."""
    obj = bpy.data.objects.get(required_name(name, "object_name"))
    if obj is None:
        raise ValueError(f"Object not found: {name}")
    if obj.name not in scene.objects:
        raise ValueError(f"Object '{name}' is not linked to scene '{scene.name}'")
    return obj


def light_object(name, *, scene=None):
    """Resolve one exact light object."""
    obj = bpy.data.objects.get(required_name(name, "light_name"))
    if obj is None:
        raise ValueError(f"Light object not found: {name}")
    if scene is not None and obj.name not in scene.objects:
        raise ValueError(f"Light '{name}' is not linked to scene '{scene.name}'")
    if obj.type != "LIGHT" or obj.data is None:
        raise ValueError(f"Object '{name}' is not a light (type={obj.type})")
    return obj


def collection_in_scene(scene, name):
    """Resolve an existing collection and require it in the scene tree."""
    collection = bpy.data.collections.get(required_name(name, "collection_name"))
    if collection is None:
        raise ValueError(f"Collection not found: {name}")
    if not collection_is_in_tree(scene.collection, collection):
        raise ValueError(f"Collection '{name}' is not linked to scene '{scene.name}'")
    return collection


def collection_is_in_tree(root, wanted):
    """Return whether a collection is the root or one of its recursive children."""
    if root == wanted:
        return True
    return any(collection_is_in_tree(child, wanted) for child in root.children)


def ensure_collection(scene, name):
    """Create or link one named scene collection after inputs are validated."""
    required_name(name, "collection_name")
    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
    if not collection_is_in_tree(scene.collection, collection):
        scene.collection.children.link(collection)
    return collection


def plain(value):
    """Convert a small Blender value into JSON-safe data."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if hasattr(value, "name"):
        return value.name
    try:
        return [plain(item) for item in value]
    except TypeError:
        return str(value)


def rounded(values):
    """Serialize a float sequence at TRANSFORM_DECIMALS, the precision float32 data actually carries."""
    return [round(float(value), TRANSFORM_DECIMALS) for value in values]


def matrix_list(matrix):
    """Serialize a 4x4 matrix."""
    return [rounded(row) for row in matrix]


def update_view_layer():
    """Flush pending dependency-graph updates so evaluated matrices are current."""
    view_layer = getattr(bpy.context, "view_layer", None)
    if view_layer is not None:
        view_layer.update()


def transform_snapshot(obj):
    """Serialize local and evaluated world transforms without changing scene context."""
    update_view_layer()
    world_location, world_rotation, world_scale = obj.matrix_world.decompose()
    return {
        "local": {
            "location": rounded(obj.location),
            "rotation_mode": obj.rotation_mode,
            "rotation": rounded(rotation_as_native_list(obj)),
            "scale": rounded(obj.scale),
        },
        "world": {
            "location": rounded(world_location),
            "rotation_quaternion": rounded(world_rotation),
            "scale": rounded(world_scale),
            "matrix": matrix_list(obj.matrix_world),
        },
    }


def collection_member_names(collection, maximum=2_000):
    """Return bounded recursive object membership for one collection."""
    names = sorted({obj.name for obj in collection.all_objects})
    return {
        "collection": collection.name,
        "objects": names[:maximum],
        "total": len(names),
        "truncated": len(names) > maximum,
    }


def light_linking_snapshot(obj):
    """Serialize receiver and blocker collections and their effective members."""
    linking = getattr(obj, "light_linking", None)
    if linking is None:
        return {"supported": False, "receiver": None, "blocker": None}
    receiver = getattr(linking, "receiver_collection", None)
    blocker = getattr(linking, "blocker_collection", None)
    return {
        "supported": True,
        "receiver": collection_member_names(receiver) if receiver else None,
        "blocker": collection_member_names(blocker) if blocker else None,
    }


def constraint_snapshot(constraint):
    """Serialize targeting-relevant constraint state."""
    result = {
        "name": constraint.name,
        "type": constraint.type,
        "mute": bool(constraint.mute),
        "influence": float(constraint.influence),
        "is_valid": bool(constraint.is_valid),
    }
    for field in ("target", "subtarget", "track_axis", "up_axis", "owner_space", "target_space"):
        if hasattr(constraint, field):
            result[field] = plain(getattr(constraint, field))
    return result


def animation_snapshot(owner, maximum=100):
    """Return bounded action and driver summaries for an Object or Light datablock."""
    animation = getattr(owner, "animation_data", None)
    if animation is None:
        return {"action": None, "fcurves": [], "drivers": [], "truncated": False}
    action = getattr(animation, "action", None)
    curves = list(getattr(action, "fcurves", ())) if action is not None else []
    drivers = list(getattr(animation, "drivers", ()))
    records = [
        {"data_path": curve.data_path, "array_index": curve.array_index, "keyframes": len(curve.keyframe_points)}
        for curve in curves[:maximum]
    ]
    driver_records = [
        {"data_path": curve.data_path, "array_index": curve.array_index, "expression": curve.driver.expression}
        for curve in drivers[:maximum]
    ]
    return {
        "action": action.name if action else None,
        "fcurves": records,
        "drivers": driver_records,
        "truncated": len(curves) > maximum or len(drivers) > maximum,
    }


def light_settings_snapshot(data):
    """Serialize common and actual-type-specific light values."""
    fields = sorted(COMMON_LIGHT_FIELDS | TYPE_LIGHT_FIELDS.get(data.type, set()))
    return {field: plain(getattr(data, field)) for field in fields if hasattr(data, field)}


def node_tree_snapshot(node_tree, maximum=MAX_NODE_SUMMARY):
    """Summarize a shader tree and external dependencies without returning an unbounded graph."""
    if node_tree is None:
        return {"nodes": [], "node_count": 0, "links": [], "truncated": False, "external_files": []}
    nodes = list(node_tree.nodes)
    records = []
    dependencies = []
    for node in nodes[:maximum]:
        record = {"name": node.name, "type": node.bl_idname, "label": node.label, "mute": bool(node.mute)}
        image = getattr(node, "image", None)
        if image is not None:
            filepath = bpy.path.abspath(image.filepath) if image.filepath else ""
            record["image"] = image.name
            record["filepath"] = filepath
            dependencies.append({"kind": "IMAGE", "resource": image.name, "path": filepath})
        filepath = getattr(node, "filepath", "")
        if filepath:
            absolute = bpy.path.abspath(filepath)
            record["filepath"] = absolute
            dependencies.append({"kind": "IES", "resource": node.name, "path": absolute})
        records.append(record)
    links = [
        {
            "from_node": link.from_node.name,
            "from_socket": link.from_socket.name,
            "to_node": link.to_node.name,
            "to_socket": link.to_socket.name,
        }
        for link in list(node_tree.links)[:maximum]
    ]
    return {
        "nodes": records,
        "node_count": len(nodes),
        "links": links,
        "truncated": len(nodes) > maximum or len(node_tree.links) > maximum,
        "external_files": dependencies,
    }


def light_summary(obj):
    """Build the default inventory record: what identifies one light plus what a listing is asked for."""
    data = obj.data
    update_view_layer()
    return {
        "object": obj.name,
        "light_data": data.name,
        "light_type": data.type,
        "energy": float(data.energy),
        "color": rounded(data.color),
        "location_world": rounded(obj.matrix_world.translation),
        "hidden_viewport": bool(obj.hide_viewport),
        "hidden_render": bool(obj.hide_render),
    }


def light_snapshot(obj, *, include_nodes=False):
    """Build the full record light_summary trims: every transform, setting, and link one light carries."""
    data = obj.data
    targets = [
        constraint_snapshot(item)
        for item in obj.constraints
        if item.type in {"TRACK_TO", "DAMPED_TRACK", "LOCKED_TRACK"}
    ]
    result = {
        "object": obj.name,
        "light_data": data.name,
        "light_type": data.type,
        "transform": transform_snapshot(obj),
        "settings": light_settings_snapshot(data),
        "data_users": data.users,
        "collections": sorted(collection.name for collection in obj.users_collection),
        "hidden_viewport": bool(obj.hide_viewport),
        "hidden_render": bool(obj.hide_render),
        "target_constraints": targets,
        "light_group": getattr(obj, "lightgroup", ""),
        "light_linking": light_linking_snapshot(obj),
    }
    if include_nodes:
        result.update(
            {
                "use_nodes": bool(data.use_nodes),
                "node_tree": node_tree_snapshot(data.node_tree),
                "object_animation": animation_snapshot(obj),
                "light_animation": animation_snapshot(data),
                "engine_compatibility": {
                    "ordinary_light": ["CYCLES", "EEVEE"],
                    "arbitrary_shader_nodes": "Cycles-first; verify EEVEE support for each node",
                    "ies": "Cycles-first",
                    "light_linking": ["CYCLES", "EEVEE"],
                },
            }
        )
    return result


def _rna_engine_identifiers():
    """Read the engine identifiers the static RenderSettings.engine enum advertises."""
    prop = bpy.types.RenderSettings.bl_rna.properties["engine"]
    return {item.identifier: item.name for item in prop.enum_items}


def _registered_engine_identifiers():
    """
    Read the engine identifiers registered as bpy.types.RenderEngine subclasses.

    Verified against Blender 5.2.2: with Cycles enabled and actively rendering,
    RenderSettings.engine's enum_items still reports only ['BLENDER_EEVEE'], because that enum
    carries the built-in engines while every externally registered engine - Cycles included -
    exists solely as a RenderEngine subclass carrying bl_idname/bl_label. This is the same
    dynamic-RNA trap documented for openvdb_data_depth in handlers/liquid/simulation.py: the
    static enum is not the list of what the runtime accepts. Intermediate base classes that only
    exist to be derived from (HydraRenderEngine) carry no bl_idname and are skipped.
    """
    found = {}
    seen = set()
    pending = list(bpy.types.RenderEngine.__subclasses__())
    while pending:
        engine = pending.pop(0)
        if id(engine) in seen:
            continue
        seen.add(id(engine))
        pending.extend(engine.__subclasses__())
        identifier = str(getattr(engine, "bl_idname", "") or "")
        if identifier:
            found.setdefault(identifier, str(getattr(engine, "bl_label", "") or identifier))
    return found


def engine_identifiers(scene=None):
    """
    Report every render engine this runtime can use, mapped to its display label.

    Unions both registration sources - the static RenderSettings.engine enum and the registered
    RenderEngine subclasses - because neither alone is complete, keeping RNA's label when both
    report the same identifier. An engine a scene is already assigned is always included: the
    assignment only succeeds for an engine the runtime accepts, so whichever source failed to
    advertise it cannot make it unavailable. Pass the scene under inspection to scope that last
    rule to it; by default every scene in the file contributes its assigned engine.
    """
    available = _registered_engine_identifiers()
    available.update(_rna_engine_identifiers())
    scenes = [scene] if scene is not None else list(getattr(bpy.data, "scenes", []) or [])
    for item in scenes:
        assigned = str(getattr(getattr(item, "render", None), "engine", "") or "")
        if assigned:
            available.setdefault(assigned, assigned)
    return available


def resolve_engine(target, scene=None):
    """Resolve an MCP engine label to a runtime Blender 5.1+ engine identifier."""
    available = engine_identifiers(scene)
    if target == "CYCLES":
        if "CYCLES" not in available:
            raise ValueError("Cycles is not registered in this Blender runtime")
        return "CYCLES"
    if target == "EEVEE":
        # EEVEE is built in, so the RNA enum resolves it on its own; consulting the wider union
        # first would let any registered add-on engine merely labelled "... EEVEE ..." make a
        # resolution ambiguous that is not actually in doubt. The union is only a fallback for a
        # runtime whose RNA enum names no EEVEE at all.
        for candidates in (_rna_engine_identifiers(), available):
            matches = sorted(
                identifier
                for identifier, name in candidates.items()
                if "EEVEE" in identifier.upper() or "EEVEE" in str(name).upper()
            )
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                break
        raise ValueError(f"Could not resolve exactly one EEVEE engine from this runtime: {sorted(available)}")
    raise ValueError("target engine must be CYCLES or EEVEE")


def rna_enum_identifiers(owner, field):
    """Read valid enum identifiers for a runtime RNA property."""
    return {item.identifier for item in owner.bl_rna.properties[field].enum_items}


def patch_properties(owner, patch, allowed):
    """Patch allowlisted properties atomically and return JSON-safe before/after values."""
    patch = dict(patch or {})
    unknown = set(patch) - set(allowed)
    if unknown:
        raise ValueError(f"Unsupported fields: {sorted(unknown)}")
    unavailable = [field for field in patch if not hasattr(owner, field)]
    if unavailable:
        raise ValueError(f"Running Blender does not support fields: {unavailable}")
    old_native = {field: getattr(owner, field) for field in patch}
    old = {field: plain(value) for field, value in old_native.items()}
    assigned = []
    try:
        for field, value in patch.items():
            setattr(owner, field, value)
            assigned.append(field)
    except Exception:
        for field in reversed(assigned):
            setattr(owner, field, old_native[field])
        raise
    return old, {field: plain(getattr(owner, field)) for field in patch}


def validate_light_patch(light_type, patch):
    """Validate field applicability and numeric contracts before mutation."""
    if light_type not in LIGHT_TYPES:
        raise ValueError(f"Unsupported light type: {light_type}")
    allowed = COMMON_LIGHT_FIELDS | TYPE_LIGHT_FIELDS[light_type]
    unknown = set(patch) - allowed
    if unknown:
        raise ValueError(f"Fields do not apply to {light_type}: {sorted(unknown)}")
    boolean_fields = {
        "normalize",
        "use_temperature",
        "use_shadow",
        "use_custom_distance",
        "use_soft_falloff",
        "show_cone",
    }
    invalid_booleans = [field for field in patch.keys() & boolean_fields if not isinstance(patch[field], bool)]
    if invalid_booleans:
        raise ValueError(f"Boolean light fields require true or false: {sorted(invalid_booleans)}")
    if "shape" in patch and patch["shape"] not in {"SQUARE", "RECTANGLE", "DISK", "ELLIPSE"}:
        raise ValueError("shape must be SQUARE, RECTANGLE, DISK, or ELLIPSE")
    if "energy" in patch and finite_number(patch["energy"], "energy") <= 0:
        raise ValueError("energy must be positive")
    for field in ("diffuse_factor", "specular_factor", "transmission_factor", "volume_factor"):
        if field in patch and finite_number(patch[field], field) < 0:
            raise ValueError(f"{field} must be non-negative")
    if "exposure" in patch and not -32 <= finite_number(patch["exposure"], "exposure") <= 32:
        raise ValueError("exposure must be in [-32, 32]")
    if "color" in patch:
        color = finite_vector(patch["color"], "color")
        if any(channel < 0 or channel > 1 for channel in color):
            raise ValueError("color channels must be in [0, 1]")
    for field in ("temperature", "cutoff_distance", "size", "size_y"):
        if field in patch and finite_number(patch[field], field) <= 0:
            raise ValueError(f"{field} must be positive")
    if "temperature" in patch:
        temperature = finite_number(patch["temperature"], "temperature")
        if not 800 <= temperature <= 20_000:
            raise ValueError("temperature must be in [800, 20000]")
    if "spread" in patch and not 0 <= finite_number(patch["spread"], "spread") <= math.pi:
        raise ValueError("spread must be in [0, pi]")
    if "spot_size" in patch and not math.radians(1) <= finite_number(patch["spot_size"], "spot_size") <= math.pi:
        raise ValueError("spot_size must be in [1 degree, pi radians]")
    if "shadow_soft_size" in patch and finite_number(patch["shadow_soft_size"], "shadow_soft_size") < 0:
        raise ValueError("shadow_soft_size must be non-negative")
    if "spot_blend" in patch and not 0 <= finite_number(patch["spot_blend"], "spot_blend") <= 1:
        raise ValueError("spot_blend must be in [0, 1]")
    if "angle" in patch and not 0 <= finite_number(patch["angle"], "angle") <= math.pi:
        raise ValueError("angle must be in [0, pi]")
    return allowed


def world_for_edit(scene, world_name, create_world):
    """Resolve the scene world, creating and assigning one only when explicitly requested."""
    if scene.world is not None:
        if world_name is not None and scene.world.name != world_name:
            existing = bpy.data.worlds.get(required_name(world_name, "world_name"))
            if existing is None:
                if not create_world:
                    raise ValueError(f"World not found: {world_name}")
                existing = bpy.data.worlds.new(world_name)
            scene.world = existing
        return scene.world
    if not create_world:
        raise ValueError("Scene has no world; provide world_name and set create_world=true")
    world = bpy.data.worlds.get(required_name(world_name, "world_name")) or bpy.data.worlds.new(world_name)
    scene.world = world
    return world


def managed_node(nodes, node_type, role):
    """Reuse or create one tagged MCP world node."""
    matches = [
        node for node in nodes if node.get("mcp_lighting_owner") == MANAGED_OWNER and node.get("mcp_role") == role
    ]
    if len(matches) > 1:
        raise ValueError(f"World contains multiple managed nodes for role '{role}'")
    if matches:
        node = matches[0]
        if node.bl_idname != node_type:
            raise ValueError(f"Managed role '{role}' has incompatible node type {node.bl_idname}")
        return node, False
    node = nodes.new(node_type)
    node.name = f"MCP Lighting {role.replace('_', ' ').title()}"
    node.label = node.name
    node["mcp_lighting_owner"] = MANAGED_OWNER
    node["mcp_role"] = role
    return node, True


def replace_input_link(node_tree, from_socket, to_socket):
    """Replace one input link and return the prior endpoints for rollback."""
    old_links = [(link.from_socket, link.to_socket) for link in list(to_socket.links)]
    for link in list(to_socket.links):
        node_tree.links.remove(link)
    node_tree.links.new(from_socket, to_socket)
    return old_links


def restore_input_links(node_tree, to_socket, old_links):
    """Restore links captured by replace_input_link."""
    for link in list(to_socket.links):
        node_tree.links.remove(link)
    for from_socket, destination in old_links:
        node_tree.links.new(from_socket, destination)


def external_file_findings(tree):
    """Return missing image/IES dependency records for one node tree."""
    return [
        item
        for item in node_tree_snapshot(tree)["external_files"]
        if not item["path"] or not os.path.isfile(item["path"])
    ]


def evaluated_object_bounds(obj):
    """Return the (minimum, maximum) corners of an object's evaluated world-space AABB."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    corners = [evaluated.matrix_world @ mathutils.Vector(corner) for corner in evaluated.bound_box]
    minimum = mathutils.Vector(tuple(min(point[index] for point in corners) for index in range(3)))
    maximum = mathutils.Vector(tuple(max(point[index] for point in corners) for index in range(3)))
    return minimum, maximum


def evaluated_bounds_point(obj, position):
    """Return center/top/bottom of an object's evaluated world-space bounds."""
    minimum, maximum = evaluated_object_bounds(obj)
    center = (minimum + maximum) * 0.5
    if position == "TOP":
        center.z = maximum.z
    elif position == "BOTTOM":
        center.z = minimum.z
    return center
