# Inherited scope metrics: `validate_pbr_asset` was over the branch/statement/local limits before
# this pass, and `scripts/lint_changed.py` attributes a whole-scope finding to any branch that
# writes inside the scope.
# ruff: file-ignore[too-many-branches, too-many-locals, too-many-statements]
"""Evidence-backed PBR readiness validation handlers."""

import bpy

from ._shared import image_path_missing, linked_principled, material_by_name, mesh_object, socket_by_names


def _finding(severity, code, subject, evidence, remediation):
    return {"severity": severity, "code": code, "subject": subject, "evidence": evidence, "remediation": remediation}


def _library_source(obj):
    """
    Name the library a mesh object's data - or the object itself - is linked from.

    Returns None when both are local. Linked data is read-only in the file that links it, so a
    finding whose remediation is "edit this mesh" is not actionable here and must say where it is.
    """
    for datablock in (getattr(obj, "data", None), obj):
        library = getattr(datablock, "library", None)
        if library is not None:
            return str(getattr(library, "filepath", "") or getattr(library, "name", "") or "an unnamed library")
    return None


class TextureValidationHandlers:
    """Audit material, image, UV, and dual-engine contracts without mutation."""

    def validate_pbr_asset(
        self, object_names=None, material_names=None, profile="BLENDER_BOTH", overlap_pair_limit=100
    ):
        profile = str(profile).upper()
        if profile not in {"BLENDER_CYCLES", "BLENDER_EEVEE", "BLENDER_BOTH"}:
            raise ValueError("Unknown PBR validation profile")
        objects = [mesh_object(name) for name in object_names] if object_names else []
        materials = [material_by_name(name) for name in material_names] if material_names else []
        if not objects and not materials:
            objects = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
        for obj in objects:
            for slot in obj.material_slots:
                if slot.material and slot.material not in materials:
                    materials.append(slot.material)
        findings = []
        for obj in objects:
            if not obj.material_slots:
                findings.append(
                    _finding("ERROR", "NO_MATERIAL", obj.name, "No material slots", "Assign a PBR material explicitly.")
                )
            if not obj.data.uv_layers:
                findings.append(
                    _finding("ERROR", "NO_UV_MAP", obj.name, "No UV layers", "Create seams and unwrap a named UV map.")
                )
            for index, slot in enumerate(obj.material_slots):
                if slot.material is None:
                    findings.append(
                        _finding(
                            "WARNING",
                            "EMPTY_MATERIAL_SLOT",
                            obj.name,
                            f"Slot {index} is empty",
                            "Assign or remove the empty slot deliberately.",
                        )
                    )
            if obj.data.uv_layers:
                active_uv = obj.data.uv_layers.active or obj.data.uv_layers[0]
                uv_report = self.inspect_uv_layout(obj.name, active_uv.name, overlap_pair_limit)
                metrics = uv_report["uv_maps"][0]
                if metrics["zero_area_faces"]:
                    # A linked mesh cannot be unwrapped from the linking file, so an ERROR here
                    # would hold `ready` false forever with no action available in this session.
                    library = _library_source(obj)
                    findings.append(
                        _finding(
                            "WARNING" if library else "ERROR",
                            "ZERO_AREA_UVS",
                            obj.name,
                            metrics["zero_area_faces"],
                            (
                                f"Mesh data is linked from {library}; unwrap the listed faces in that "
                                "source file and reload the link - it cannot be unwrapped from here."
                                if library
                                else "Unwrap the listed faces before texturing or baking."
                            ),
                        )
                    )
                if metrics["overlap_pairs"]:
                    findings.append(
                        _finding(
                            "WARNING",
                            "OVERLAPPING_UVS",
                            obj.name,
                            metrics["overlap_pairs"],
                            "Separate or intentionally document overlapping islands.",
                        )
                    )
        for material in materials:
            output, shader = linked_principled(material)
            if output is None:
                findings.append(
                    _finding(
                        "ERROR",
                        "NO_ACTIVE_OUTPUT",
                        material.name,
                        "No Material Output node",
                        "Create and activate one Material Output.",
                    )
                )
                continue
            if shader is None:
                findings.append(
                    _finding(
                        "WARNING",
                        "NON_PRINCIPLED_SURFACE",
                        material.name,
                        "Active Surface is not directly fed by Principled BSDF",
                        "Inspect the graph and verify engine compatibility manually.",
                    )
                )
            for node in material.node_tree.nodes:
                if node.bl_idname == "ShaderNodeTexImage":
                    if node.image is None:
                        findings.append(
                            _finding(
                                "ERROR",
                                "IMAGE_NODE_EMPTY",
                                material.name,
                                f"Node '{node.name}' has no image",
                                "Load and assign an image.",
                            )
                        )
                    elif image_path_missing(node.image):
                        findings.append(
                            _finding(
                                "ERROR",
                                "IMAGE_MISSING",
                                node.image.name,
                                node.image.filepath,
                                "Repath, reload, or pack the image.",
                            )
                        )
                    elif node.image.is_dirty:
                        findings.append(
                            _finding(
                                "WARNING",
                                "IMAGE_UNSAVED",
                                node.image.name,
                                "Image contains unsaved pixel edits",
                                "Save or pack the image.",
                            )
                        )
                    if node.image is not None and node.outputs.get("Color"):
                        destinations = {link.to_socket.name for link in node.outputs["Color"].links}
                        color_semantic = bool(destinations & {"Base Color", "Emission Color", "Emission"})
                        expected = "sRGB" if color_semantic else "Non-Color"
                        if destinations and node.image.colorspace_settings.name != expected:
                            findings.append(
                                _finding(
                                    "WARNING",
                                    "COLORSPACE_MISMATCH",
                                    node.image.name,
                                    {
                                        "destinations": sorted(destinations),
                                        "colorspace": node.image.colorspace_settings.name,
                                    },
                                    f"Set this image to {expected} for its current shader use.",
                                )
                            )
                if node.bl_idname in {"ShaderNodeScript", "ShaderNodeTexPointDensity"}:
                    findings.append(
                        _finding(
                            "WARNING",
                            "ENGINE_RISK_NODE",
                            material.name,
                            node.bl_idname,
                            "Bake this procedural result for portable rendering.",
                        )
                    )
            if shader:
                normal = socket_by_names(shader, ("Normal",))
                if normal.is_linked and normal.links[0].from_node.bl_idname not in {
                    "ShaderNodeNormalMap",
                    "ShaderNodeBump",
                }:
                    findings.append(
                        _finding(
                            "WARNING",
                            "RAW_NORMAL_INPUT",
                            material.name,
                            normal.links[0].from_node.bl_idname,
                            "Route tangent normals through Normal Map and heights through Bump.",
                        )
                    )
            displacement = output.inputs.get("Displacement")
            if profile in {"BLENDER_EEVEE", "BLENDER_BOTH"} and displacement and displacement.is_linked:
                findings.append(
                    _finding(
                        "WARNING",
                        "EEVEE_DISPLACEMENT_UNSUPPORTED",
                        material.name,
                        "Material Output Displacement is linked",
                        "Provide a Normal Map or Bump path for Eevee.",
                    )
                )
            if profile in {"BLENDER_EEVEE", "BLENDER_BOTH"} and shader:
                transmission = socket_by_names(shader, ("Transmission Weight", "Transmission"))
                if float(transmission.default_value) > 0 and not getattr(material, "use_raytrace_refraction", False):
                    findings.append(
                        _finding(
                            "WARNING",
                            "EEVEE_REFRACTION_DISABLED",
                            material.name,
                            "Transmission is non-zero but raytrace refraction is disabled",
                            "Enable material ray-traced refraction and validate scene ray tracing.",
                        )
                    )
        counts = {
            severity: sum(item["severity"] == severity for item in findings)
            for severity in ("ERROR", "WARNING", "INFO")
        }
        return {
            "profile": profile,
            "objects": [obj.name for obj in objects],
            "materials": [material.name for material in materials],
            "findings": findings,
            "counts": counts,
            "ready": counts["ERROR"] == 0,
            "overlap_pair_limit": int(overlap_pair_limit),
            "read_only": True,
        }
