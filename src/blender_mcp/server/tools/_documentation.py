"""
Normalize agent-facing documentation for every registered MCP tool.

FastMCP 1.x exposes function docstrings as tool descriptions, but it does not
copy Google-style ``Args`` entries into JSON Schema property descriptions.  The
tool surface in this package also contains many nested Pydantic patch models,
whose constraints are useful to agents only when their purpose is explicit.

This module performs one final documentation pass after all tool modules have
registered.  It deliberately changes metadata only: call signatures, dispatch,
validation, and Blender behavior remain untouched.
"""

# The schema vocabulary is intentionally an explicit decision table.
# ruff: file-ignore[too-many-branches, too-many-return-statements]

import inspect
import re

from collections.abc import Mapping
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..app import mcp

_SECTION_RE = re.compile(r"^([A-Z][A-Za-z ]+):(?:\s+(.*))?$")
_ARG_RE = re.compile(r"^\s{4}([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")

_READ_ONLY_PREFIXES = (
    "get_",
    "list_",
    "inspect_",
    "validate_",
    "estimate_",
    "evaluate_",
    "analyze_",
    "test_",
    "search_",
)
_MUTATING_READ_PREFIXES = ("sample_",)
_EXTERNAL_TOOLS = {
    "get_polyhaven_categories",
    "list_polyhaven_assets",
    "import_polyhaven_asset",
    "search_sketchfab_models",
    "get_sketchfab_model_preview",
    "import_sketchfab_model",
}
_FILE_TOOLS = {
    "bake_retopology_maps",
    "export_cloth_simulation",
    "export_liquid_simulation",
    "export_rigid_body_animation",
    "manage_geometry_nodes_bake",
    "render_lighting_preview",
    "setup_liquid_shot",
    "render_scene",
}
# Tools that read or write a .blend file. Their shared sentence says only that:
# `reload_library` takes no path and enforces no file roots, so per-tool detail belongs in each
# tool's docstring. Not part of `_FILE_TOOLS`, whose prose says the .blend file is not saved.
_BLEND_FILE_TOOLS = {
    "open_shot",
    "save_shot",
    "link_canon_library",
    "reload_library",
    "relocate_library",
}
_IMAGE_TOOLS = {
    "get_viewport_screenshot",
    "get_sketchfab_model_preview",
    "render_lighting_preview",
    "render_pbr_material_preview",
    "inspect_render_output",
}
_DESTRUCTIVE_PREFIXES = (
    "aim_",
    "animate_",
    "bake_",
    "bind_",
    "clean_",
    "clear_",
    "configure_",
    "copy_",
    "fit_",
    "frame_",
    "keyframe_",
    "manage_",
    "match_",
    "mesh_",
    "nd_",
    "patch_",
    "prepare_",
    "project_",
    "redistribute_",
    "relax_",
    "remove_",
    "reroute_",
    "set_",
    "sync_",
    "transfer_",
)
_DESTRUCTIVE_TOOLS = {
    "reset_scene",
    "apply_liquid_quality_profile",
    "apply_polyhaven_texture",
    "assign_bone_custom_shapes",
    "bind_mesh_to_armature",
    "clean_skin_weights",
    "create_camera_markers",
    "import_sketchfab_model",
    "import_polyhaven_asset",
    "manage_cloth_cache",
    "manage_bone_collections",
    "manage_liquid_cache",
    "manage_geometry_nodes_bake",
    "manage_retopology_checkpoint",
    "manage_rigid_body_cache",
    "nd_apply_modifiers",
    "nd_clean_utils",
    "patch_armature_bones",
    "project_mesh_elements",
    "run_geometry_nodes_tool",
    "setup_liquid_shot",
    "realize_procedural_output",
    "redistribute_edge_loop",
    "relax_topology",
    "reroute_topology",
    "set_skin_weights",
    "transfer_mesh_attributes",
    "transfer_skin_weights",
    # File lifecycle and linking: no destructive prefix matches these. `save_shot` is also caught
    # by its `confirm_overwrite` flag, but should not depend on that key. `link_canon_library`
    # and `create_override` only add data, so they are not listed.
    "open_shot",
    "reset_session",
    "unlink_libraries",
    "relocate_library",
    "reload_library",
    "save_shot",
}

_ACRONYMS = {
    "dof": "DOF",
    "fk": "FK",
    "hdri": "HDRI",
    "id": "ID",
    "ik": "IK",
    "lod": "LOD",
    "mcp": "MCP",
    "nd": "ND",
    "rna": "RNA",
    "udim": "UDIM",
    "uid": "UID",
    "uv": "UV",
}

_TOOL_TITLES = {
    "apply_polyhaven_texture": "Apply Poly Haven Texture",
    "import_sketchfab_model": "Import Sketchfab Model",
    "get_polyhaven_categories": "List Poly Haven Categories",
    "import_polyhaven_asset": "Import Poly Haven Asset",
    "add_radial_array_modifier": "Add Radial Array Modifier",
    "list_polyhaven_assets": "List Poly Haven Assets",
    "set_viewport_overlay": "Set Viewport Overlay",
}

# Shared Blender/MCP vocabulary.  Tool-specific Google-style Args descriptions
# take precedence; these entries make repeated concepts consistent everywhere.
_PARAMETER_DESCRIPTIONS: dict[str, str] = {
    "action": "Operation to perform; choose exactly one advertised enum value.",
    "apply": (
        "Whether to apply the result to base data. False keeps the operation live and reversible; "
        "true may change topology irreversibly."
    ),
    "array_index": (
        "Zero-based index into the target array property (for example 0/1/2 for X/Y/Z on a vector or color "
        "channel); the tool description states the sentinel meaning 'not an array property'."
    ),
    "asset_id": "Exact Poly Haven asset identifier returned by list_polyhaven_assets.",
    "asset_type": "Provider asset class used to filter, download, or interpret the result.",
    "cache_directory": (
        "Explicit external filesystem directory for this simulation's disk cache; omitting it keeps the cache "
        "in Blender's default location alongside the .blend file."
    ),
    "camera_name": "Exact name of the existing Blender Camera object to inspect or modify.",
    "categories": "Comma-separated provider category slugs; omit to avoid category filtering.",
    "collection_name": (
        "Exact Blender collection name to use. The tool description states whether it must exist or may be created."
    ),
    "collision_layers": (
        "Rigid-body collision layer indices, each from 1 to 20, this object belongs to; two rigid bodies can "
        "collide only if they share at least one layer."
    ),
    "confirm": (
        "Explicit acknowledgement required for the consequential operation; false performs no confirmed "
        "destructive action."
    ),
    "confirm_bake": "Explicit acknowledgement that a synchronous bake may be expensive and will create cache data.",
    "confirm_commit": (
        "Explicit acknowledgement that live data will be committed into base data and cannot be reversed through MCP."
    ),
    "confirm_delete_baked_cache": (
        "Explicit acknowledgement that an existing baked cache may be deleted or invalidated."
    ),
    "confirm_destructive": (
        "Explicit acknowledgement that the requested operation may irreversibly replace or remove Blender data."
    ),
    "confirm_free": "Explicit acknowledgement that the selected simulation cache will be freed.",
    "confirm_overwrite": "Explicit acknowledgement that an existing destination may be replaced.",
    "count": "Requested number of items or instances produced or returned by this operation.",
    "ctx": "MCP request context supplied by the server; callers do not provide this value.",
    "domain_object_name": "Exact name of the mesh object containing the fluid domain modifier.",
    "edge_indices": (
        "Base-mesh edge indices from a current get_mesh_data or inspect_retopology result; refresh them after "
        "topology changes."
    ),
    "element_type": "Mesh element kind to inspect; determines the shape of each returned element record.",
    "existing_policy": (
        "How to handle an already-existing same-named resource: ERROR fails the operation; REUSE targets the "
        "existing resource instead of creating a new one."
    ),
    "expected_revision": (
        "Topology revision from the latest inspection. When supplied, stale topology is rejected before mutation."
    ),
    "face_indices": "Base-mesh face indices from a current get_mesh_data result; refresh them after topology changes.",
    "file_format": (
        "Exact supported file-format identifier. Omit to let the provider or operation select its documented default."
    ),
    "filepath": "Explicit filesystem path used by the operation; no implicit project-relative destination is assumed.",
    "frame": "Blender timeline frame at which the value or operation applies.",
    "frame_end": "Inclusive final Blender timeline frame; it must not precede frame_start.",
    "frame_start": "Inclusive first Blender timeline frame; it must not exceed frame_end.",
    "frames": (
        "Explicit Blender timeline frames to evaluate or modify; order and uniqueness requirements are stated by "
        "the tool."
    ),
    "influence": "Normalized influence: 0 disables the effect and 1 applies its full configured effect.",
    "interpolation": "Interpolation applied between generated or selected animation keys.",
    "keyframes": "Explicit typed keyframe records to validate and process as one batch.",
    "lens": "Camera focal length in millimeters for perspective projection.",
    "limit": "Maximum records returned in this page; use next_offset while truncated is true.",
    "location": "Three-component [x, y, z] position. The tool description identifies local or world space.",
    "material_name": "Exact Blender material datablock name to use or modify.",
    "modifier_name": "Exact Blender modifier name to create, reuse, inspect, or modify as described by the tool.",
    "name": "Requested Blender datablock or object name; collision handling is stated by the tool.",
    "object_name": "Exact name of the existing Blender object targeted by this operation.",
    "object_names": "Explicit Blender object names targeted as one validated batch; selection state is not used.",
    "output_path": (
        "Explicit output file path. The parent directory must exist; overwrite behavior is controlled separately."
    ),
    "overwrite": (
        "Whether an existing destination may be replaced. False preserves existing data and returns an error on "
        "collision."
    ),
    "owner_space": (
        "Coordinate space the constrained object's own transform is evaluated in before the constraint applies."
    ),
    "patch": "Strict partial update object. Omitted fields remain unchanged and unknown fields are rejected.",
    "policy": "Conflict or replacement policy controlling how existing data is handled.",
    "projection_offset": (
        "Signed offset applied along the reference surface normal when projecting or reprojecting geometry; "
        "positive moves outward, negative moves inward."
    ),
    "property_bone_name": (
        "Exact pose bone name that owns the referenced custom property; required when property_owner is "
        "POSE_BONE and ignored otherwise."
    ),
    "property_owner": "Whether the referenced custom property lives on the object itself or on one of its pose bones.",
    "rig_id": (
        "Exact identifier tag assigned when the rig's helper objects were created; selects all of that rig's "
        "generated helpers together."
    ),
    "rotation_euler": "Three XYZ Euler angles [x, y, z] in radians.",
    "rotation_quaternion": "Quaternion [w, x, y, z]; use a normalized non-zero quaternion.",
    "scene_name": "Exact name of the Blender scene to inspect or modify.",
    "selected_only": "Whether inspection is restricted to elements currently selected in Edit Mode.",
    "settings": "Strict typed settings object; omitted fields remain unchanged and unknown fields are rejected.",
    "source_object_name": (
        "Exact name of the existing Blender object used as the source; the source is not selected implicitly."
    ),
    "source_object_names": (
        "Ordered exact names of existing source objects; ordering significance is stated by the tool."
    ),
    "stack_index": (
        "Zero-based position within the existing modifier or constraint stack to target; -1 means the last entry."
    ),
    "target_object_name": "Exact name of the existing Blender object receiving or defining the operation target.",
    "target_size": "Positive target size in Blender scene units.",
    "target_space": (
        "Coordinate space the constraint target's transform is evaluated in before the constraint applies."
    ),
    "texture_id": "Exact Poly Haven texture identifier previously imported with import_polyhaven_asset.",
    "uid": "Exact Sketchfab model UID returned by search_sketchfab_models.",
    "vertex_indices": (
        "Base-mesh vertex indices from a current get_mesh_data or inspect_retopology result; refresh them after "
        "topology changes."
    ),
}


def _humanize(identifier: str) -> str:
    words = identifier.strip("_").split("_")
    return " ".join(_ACRONYMS.get(word, word) for word in words)


def _context_text(value: str) -> str:
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", value.rstrip("."))
    return words.lower()


def _title(identifier: str) -> str:
    return " ".join(_ACRONYMS.get(word, word.capitalize()) for word in identifier.split("_"))


def _parse_docstring(docstring: str) -> tuple[str, dict[str, str], str | None]:
    """
    Parse the parts of a source docstring used by MCP metadata.

    Returns:
        The narrative body, parameter descriptions, and optional return description.

    """
    lines = inspect.cleandoc(docstring or "").splitlines()
    sections: dict[str, list[str]] = {"body": []}
    current = "body"
    for line in lines:
        match = _SECTION_RE.match(line)
        if match:
            current = match.group(1)
            sections.setdefault(current, [])
            inline_content = match.group(2)
            if inline_content:
                sections[current].append(inline_content)
            if current not in {"Args", "Arguments", "Parameters", "Returns", "Raises"}:
                sections["body"].append(f"{current}:" + (f" {inline_content}" if inline_content else ""))
            continue
        sections.setdefault(current, []).append(line)
        if current not in {"body", "Args", "Arguments", "Parameters", "Returns", "Raises"}:
            sections["body"].append(line)

    arguments: dict[str, str] = {}
    arg_lines = sections.get("Args", []) + sections.get("Arguments", []) + sections.get("Parameters", [])
    current_name: str | None = None
    for line in arg_lines:
        match = _ARG_RE.match(line)
        if match:
            argument_name = match.group(1)
            current_name = argument_name
            arguments[argument_name] = match.group(2).strip()
        elif current_name is not None and line.strip():
            arguments[current_name] = f"{arguments[current_name]} {line.strip()}".strip()

    body = "\n".join(sections.get("body", [])).strip()
    returns = " ".join(line.strip() for line in sections.get("Returns", []) if line.strip()) or None
    return body, arguments, returns


def _constraint_fragments(schema: Mapping[str, Any]) -> list[str]:
    fragments: list[str] = []
    variants = [candidate for candidate in schema.get("anyOf", []) if candidate.get("type") != "null"]
    effective = variants[0] if len(variants) == 1 else schema
    enum = effective.get("enum")
    if enum:
        fragments.append("Allowed: " + ", ".join(repr(value) for value in enum) + ".")
    if "default" in schema:
        fragments.append(f"Default: {schema['default']!r}.")

    minimum = effective.get("minimum")
    exclusive_minimum = effective.get("exclusiveMinimum")
    maximum = effective.get("maximum")
    exclusive_maximum = effective.get("exclusiveMaximum")
    if minimum is not None and maximum is not None:
        fragments.append(f"Range: {minimum} to {maximum}, inclusive.")
    elif exclusive_minimum is not None and maximum is not None:
        fragments.append(f"Range: greater than {exclusive_minimum} and at most {maximum}.")
    elif minimum is not None and exclusive_maximum is not None:
        fragments.append(f"Range: at least {minimum} and less than {exclusive_maximum}.")
    elif exclusive_minimum is not None:
        fragments.append(f"Must be greater than {exclusive_minimum}.")
    elif minimum is not None:
        fragments.append(f"Must be at least {minimum}.")
    elif exclusive_maximum is not None:
        fragments.append(f"Must be less than {exclusive_maximum}.")
    elif maximum is not None:
        fragments.append(f"Must be at most {maximum}.")

    min_items = effective.get("minItems")
    max_items = effective.get("maxItems")
    if min_items is not None and max_items is not None:
        if min_items == max_items:
            fragments.append(f"Requires exactly {min_items} items.")
        else:
            fragments.append(f"Requires {min_items} to {max_items} items.")
    elif min_items is not None:
        fragments.append(f"Requires at least {min_items} items.")
    elif max_items is not None:
        fragments.append(f"Allows at most {max_items} items.")

    min_length = effective.get("minLength")
    max_length = effective.get("maxLength")
    if min_length == 1 and max_length is None:
        fragments.append("Must not be empty.")
    elif min_length is not None and max_length is not None:
        fragments.append(f"Requires {min_length} to {max_length} characters.")
    elif min_length is not None:
        fragments.append(f"Requires at least {min_length} characters.")
    elif max_length is not None:
        fragments.append(f"Allows at most {max_length} characters.")
    if pattern := effective.get("pattern"):
        fragments.append(f"Must match {pattern!r}.")
    return fragments


def _primary_type(schema: Mapping[str, Any]) -> str | None:
    direct = schema.get("type")
    if isinstance(direct, str):
        return direct
    variants = [candidate.get("type") for candidate in schema.get("anyOf", []) if candidate.get("type") != "null"]
    return variants[0] if len(variants) == 1 and isinstance(variants[0], str) else None


def _base_parameter_description(
    name: str,
    schema: Mapping[str, Any],
    *,
    model_context: str | None,
) -> str:
    if name in _PARAMETER_DESCRIPTIONS:
        return _PARAMETER_DESCRIPTIONS[name]
    human_name = _humanize(name)
    primary_type = _primary_type(schema)
    if name == "resolution":
        if primary_type == "string":
            return "Provider resolution identifier; higher resolutions use more bandwidth, memory, and time."
        return "Simulation or image resolution; higher values use more memory and processing time."
    if name == "rotation":
        if primary_type == "array":
            return "Three Euler rotation angles [x, y, z] in radians in the space stated by the tool."
        return "Rotation angle in radians."
    if name == "scale":
        if primary_type == "array":
            return "Three dimensionless scale factors [x, y, z]."
        return "Dimensionless scale factor applied by this operation."
    if name == "offset":
        return "Geometric offset in the units and coordinate space stated by the tool."
    if name.startswith("confirm_"):
        subject = human_name.removeprefix("confirm ")
        return f"Explicit safety acknowledgement for {subject}; false refuses that consequential action."
    if name.startswith("use_"):
        return f"Whether to enable {human_name.removeprefix('use ')} for this operation."
    if name.startswith("include_"):
        return f"Whether to include {human_name.removeprefix('include ')} in the operation or result."
    if name.startswith("clear_"):
        return f"Whether to clear the existing {human_name.removeprefix('clear ')} assignment."
    if name.endswith("_object_name"):
        return f"Exact name of the existing Blender object used as the {human_name.removesuffix(' object name')}."
    if name.endswith("_object_names"):
        return f"Explicit existing Blender object names used as the {human_name.removesuffix(' object names')} set."
    if name.endswith("_collection_name"):
        return f"Exact Blender collection name used for {human_name.removesuffix(' collection name')}."
    if name.endswith("_modifier_name"):
        return f"Exact Blender modifier name used for {human_name.removesuffix(' modifier name')}."
    if name.endswith("_group_name"):
        return f"Exact vertex-group name used for {human_name.removesuffix(' group name')}."
    if name.endswith("_bone_name"):
        return f"Exact armature bone name used as the {human_name.removesuffix(' bone name')}."
    if name.endswith("_indices"):
        return f"Explicit base-data {human_name}; obtain fresh indices after any topology-changing operation."
    if name.endswith("_frame"):
        return f"Blender timeline frame used for {human_name.removesuffix(' frame')}."
    if name.endswith("_frames"):
        return f"Explicit Blender timeline frames used for {human_name.removesuffix(' frames')}."
    if name.endswith(("_path", "_directory")):
        return f"Explicit filesystem {human_name}; no implicit save location is used."
    if name.endswith("_limit") or name.startswith("max_"):
        return f"Upper bound for {human_name.removeprefix('max ')}; the operation refuses or truncates work beyond it."
    if name.endswith("_offset"):
        subject = human_name.removesuffix(" offset")
        return f"Offset applied to {subject} in the units and coordinate space stated by the tool."
    if name.endswith("_policy"):
        return f"Policy controlling {human_name.removesuffix(' policy')} conflicts or ownership."
    if name.endswith("_type") or name in {"type", "mode", "method", "operation", "owner", "space"}:
        return f"Selects the {human_name} behavior for this operation."
    if name.startswith("is_"):
        return f"Whether the item is {human_name.removeprefix('is ')}."
    if name.startswith("show_"):
        return f"Whether Blender displays {human_name.removeprefix('show ')} in the viewport or camera view."
    if name.startswith("lock_"):
        return f"Whether to lock {human_name.removeprefix('lock ')} against the operation."
    if name.startswith("preserve_"):
        return f"Whether to preserve {human_name.removeprefix('preserve ')} unchanged."
    if name.startswith("create_"):
        return f"Whether to create {human_name.removeprefix('create ')} when it does not already exist."
    context = (model_context or "this operation").rstrip(".").lower()
    if name.endswith(("_point", "_vector", "_direction")):
        return f"Three-component {human_name} in the coordinate space stated for {context}."
    if name.endswith("_name"):
        subject = human_name.removesuffix(" name")
        return f"Exact Blender or provider name used as {subject} for {context}."
    if name.endswith("_names"):
        subject = human_name.removesuffix(" names")
        return f"Explicit Blender or provider names used as {subject} for {context}."
    angle_tokens = ("angle", "azimuth", "elevation", "pan", "phase", "roll", "tilt", "yaw")
    if any(token in name for token in angle_tokens):
        return f"Angle in radians controlling {human_name} for {context}."
    distance_tokens = ("distance", "height", "length", "radius", "size", "thickness", "width")
    if any(token in name for token in distance_tokens):
        return f"{_title(name)} in Blender scene units for {context}."
    if primary_type == "boolean":
        return f"Whether to enable {human_name} for {context}."
    if primary_type == "array":
        return f"Explicit ordered {human_name} entries processed by {context}."
    if primary_type == "object" or "$ref" in schema or any("$ref" in item for item in schema.get("anyOf", [])):
        return f"Strict structured {human_name} configuration for {context}; unknown fields are rejected."
    if primary_type in {"integer", "number"}:
        return f"Numeric {human_name} for {context}; units and interpretation follow the tool description."
    if primary_type == "string":
        return f"Exact {human_name} identifier or selector used by {context}."
    return f"Explicit {human_name} input for {context}."


def _describe_schema(
    schema: dict[str, Any],
    *,
    explicit: Mapping[str, str] | None = None,
    model_context: str | None = None,
) -> None:
    if schema.get("type") == "object" or "properties" in schema:
        schema.setdefault("additionalProperties", False)
    explicit = explicit or {}
    for name, property_schema in schema.get("properties", {}).items():
        if not isinstance(property_schema, dict):
            continue
        description = explicit.get(name) or property_schema.get("description")
        if not description and name == "offset" and "limit" in schema.get("properties", {}):
            description = "Zero-based index of the first record in this result page."
        if not description:
            description = _base_parameter_description(name, property_schema, model_context=model_context)
        fragments = _constraint_fragments(property_schema)
        description_lower = description.lower()
        fragments = [
            fragment
            for fragment in fragments
            if not (fragment.startswith("Default:") and "default" in description_lower)
            and not (
                fragment.startswith("Allowed:") and ("one of" in description_lower or "allowed" in description_lower)
            )
            and not (fragment.startswith("Must be greater") and "positive" in description_lower)
            and not (fragment.startswith("Must be at least") and "at least" in description_lower)
        ]
        property_schema["description"] = " ".join([description.rstrip(), *fragments]).strip()

    for definition in schema.get("$defs", {}).values():
        if not isinstance(definition, dict):
            continue
        context = _context_text(definition.get("description") or definition.get("title") or model_context or "input")
        _describe_schema(definition, model_context=context)


def _is_read_only(name: str) -> bool:
    return name.startswith(_READ_ONLY_PREFIXES) and not name.startswith(_MUTATING_READ_PREFIXES)


def _is_destructive(name: str, schema: Mapping[str, Any]) -> bool:
    conditional_flags = {
        "apply",
        "commit",
        "confirm_baked_removal",
        "confirm_delete_baked_cache",
        "confirm_free",
        "confirm_free_bake",
        "confirm_overwrite",
        "confirm_replace_weights",
        "overwrite",
        "replace_existing",
    }
    return (
        name.startswith(_DESTRUCTIVE_PREFIXES)
        or name in _DESTRUCTIVE_TOOLS
        or bool(conditional_flags.intersection(schema.get("properties", {})))
    )


def _is_idempotent(name: str, read_only: bool) -> bool:
    non_idempotent_setters = {"set_cloth_vertex_weights", "set_skin_weights"}
    return read_only or (
        name not in non_idempotent_setters and name.startswith(("configure_", "set_", "aim_", "frame_", "sync_"))
    )


def _tool_contract(name: str, *, read_only: bool, returns: str | None) -> str:
    if name in _IMAGE_TOOLS:
        output = "Returns image content followed by the standard response envelope; consume both content items."
    elif returns:
        output = f"Returns the standard response envelope; data contains {returns.rstrip('.')} ."
        output = output.replace("  ", " ").replace(" .", ".")
    else:
        output = (
            "Returns the standard response envelope with operation-specific data, warnings, and exact changed "
            "object/resource names."
        )

    if read_only and name in _EXTERNAL_TOOLS:
        effects = (
            "Read-only for Blender data, but queries an external provider and may use credentials or network access."
        )
    elif read_only:
        effects = "Read-only: does not persistently modify Blender data."
    elif name in _FILE_TOOLS:
        effects = (
            "Side effects: may write the explicit output path and may evaluate Blender data; "
            "it does not save the .blend file."
        )
    elif name in _BLEND_FILE_TOOLS:
        effects = "Side effects: reads or writes a .blend file on disk."
    elif name in _EXTERNAL_TOOLS:
        effects = (
            "Side effects: accesses an external provider and may import or replace Blender data as described above."
        )
    else:
        effects = "Side effects: mutates connected Blender state but never saves the .blend file."

    errors = (
        "Requires a compatible running add-on and valid named resources; validation or Blender failures raise "
        "MCP tool errors."
    )
    return f"{effects} {output} {errors}"


def finalize_tool_documentation(mcp: FastMCP) -> None:
    """Enrich all currently registered tools with MCP-visible documentation metadata."""
    for tool in mcp._tool_manager._tools.values():
        body, explicit_parameters, returns = _parse_docstring(tool.description)
        read_only = _is_read_only(tool.name)
        _describe_schema(
            tool.parameters,
            explicit=explicit_parameters,
            model_context=f"{_humanize(tool.name)} input",
        )
        tool.description = f"{body.rstrip()}\n\n{_tool_contract(tool.name, read_only=read_only, returns=returns)}"
        tool.title = _TOOL_TITLES.get(tool.name, _title(tool.name))
        tool.annotations = ToolAnnotations(
            title=tool.title,
            readOnlyHint=read_only,
            destructiveHint=_is_destructive(tool.name, tool.parameters),
            idempotentHint=_is_idempotent(tool.name, read_only),
            openWorldHint=(tool.name in _EXTERNAL_TOOLS or tool.name in _FILE_TOOLS or tool.name in _BLEND_FILE_TOOLS),
        )


# Imported after every registration module by tools.__init__, so this covers the
# complete exposed surface while keeping the package initializer declarative.
finalize_tool_documentation(mcp)
