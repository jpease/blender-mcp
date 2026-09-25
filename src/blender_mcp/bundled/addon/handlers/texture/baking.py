"""Cycles-backed native and semantic texture baking handlers."""

import contextlib

import bpy

from ._shared import linked_principled, mesh_object, socket_by_names


def _semantic_source_socket(shader, semantic):
    names = {
        "BASE_COLOR": ("Base Color",),
        "METALLIC": ("Metallic",),
        "OPACITY": ("Alpha",),
    }
    return socket_by_names(shader, names[semantic])


def _validate_semantic_materials(objects, semantic):
    materials = {slot.material for obj in objects for slot in obj.material_slots if slot.material}
    if not materials:
        raise ValueError("Semantic baking requires at least one source material")
    for material in materials:
        _output, shader = linked_principled(material)
        if shader is None:
            raise ValueError(
                f"Material '{material.name}' must have a Principled BSDF directly connected to its active output"
            )
        _semantic_source_socket(shader, semantic)


def _emission_material_copy(material, semantic):
    copied = material.copy()
    copied.name = f"{material.name} MCP {semantic} Bake"
    tree = copied.node_tree
    output, shader = linked_principled(copied)
    source = _semantic_source_socket(shader, semantic)
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.name = f"MCP {semantic} Bake Emission"
    if source.is_linked:
        tree.links.new(source.links[0].from_socket, emission.inputs["Color"])
    else:
        value = source.default_value
        if isinstance(value, (int, float)):
            value = (float(value), float(value), float(value), 1.0)
        emission.inputs["Color"].default_value = value
    emission.inputs["Strength"].default_value = 1.0
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return copied


class TextureBakingHandlers:
    """Bake one map while retaining the established context-restoring bake implementation."""

    def bake_texture_map(
        self,
        object_name,
        map_type,
        output_path,
        high_poly_object_names=None,
        width=2048,
        height=2048,
        uv_map_name=None,
        cage_object_name=None,
        cage_extrusion=0.0,
        max_ray_distance=0.0,
        margin=16,
        normal_space="TANGENT",
        normal_swizzle=("POS_X", "POS_Y", "POS_Z"),
        target_engine="CYCLES",
        confirm_overwrite=False,
        confirm_bake=False,
    ):
        """Generalize the production bake command to same-object and high-to-low sources."""
        requested = str(map_type).upper()
        source_objects = (
            [mesh_object(name) for name in high_poly_object_names]
            if high_poly_object_names
            else [mesh_object(object_name)]
        )
        semantic = requested in {"BASE_COLOR", "METALLIC", "OPACITY"}
        if semantic:
            _validate_semantic_materials(source_objects, requested)
        replaced_slots, temporary_materials = [], []
        try:
            if semantic:
                copies = {}
                for obj in source_objects:
                    for slot in obj.material_slots:
                        if slot.material is None:
                            continue
                        original = slot.material
                        copied = copies.get(original)
                        if copied is None:
                            copied = _emission_material_copy(original, requested)
                            copies[original] = copied
                            temporary_materials.append(copied)
                        replaced_slots.append((slot, original))
                        slot.material = copied
            result = self.bake_retopology_maps(
                object_name=object_name,
                high_poly_object_names=high_poly_object_names or [],
                map_type="EMISSION" if semantic else requested,
                output_path=output_path,
                width=width,
                height=height,
                uv_map_name=uv_map_name,
                cage_object_name=cage_object_name,
                cage_extrusion=cage_extrusion,
                max_ray_distance=max_ray_distance,
                margin=margin,
                normal_space=normal_space,
                normal_swizzle=normal_swizzle,
                confirm_overwrite=confirm_overwrite,
                confirm_bake=confirm_bake,
            )
        finally:
            for slot, original in replaced_slots:
                with contextlib.suppress(Exception):
                    slot.material = original
            for material in temporary_materials:
                with contextlib.suppress(Exception):
                    bpy.data.materials.remove(material)
        result["requested_map_type"] = requested
        result["bake_engine"] = "CYCLES"
        result["target_engine"] = target_engine
        result["changed_objects"] = [object_name]
        result["changed_resources"] = [result["image"]]
        return result
