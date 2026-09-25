"""
Measure the reply every tool in a bundle selection sends back, for one representative shot.

`scripts/measure_catalog.py` measures the advertised `tools/list` payload: what a client
pays once per turn. This measures the other half of the same context bill - the reply a
single tool call sends back, which a client pays once per call, and which nothing in the
repository currently bounds.

Each tool is called through the real FastMCP app with the Blender connection stubbed, the
same way the tests stub it, so the measured bytes are the bytes FastMCP puts in the reply.
Every stub payload mirrors the add-on handler that produces it; `_PAYLOADS` cites the
handler for each one. The shapes and the float widths were taken from real handler output,
captured by running the bundled add-on inside headless Blender 5.2 against a representative
shot: 49 objects, 7 lights, a 23-bone rig, a 162-vertex hero mesh, 84 validation findings,
and one linked canon library of 121 datablocks.

Usage:
    python scripts/measure_reply_sizes.py shot
    python scripts/measure_reply_sizes.py shot,camera-rigs
    python scripts/measure_reply_sizes.py all

"""

import asyncio
import base64
import importlib.util
import json
import os
import sys

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import cast

from pydantic_core import to_json

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src"


def _addon_literal(relative_path: str, name: str) -> object:
    """
    Read one module-level literal out of a `bpy`-free add-on module, without importing the addon.

    The add-on package imports `bpy` on the way in, which this script has no Blender to supply,
    so the module is loaded straight from its file. Reading the shipped value is the point: a
    second copy of reply prose here would measure a reply nobody sends.

    Args:
        relative_path: Path under `src/blender_mcp/bundled/addon`.
        name: The module-level name to read.

    Returns:
        object: That module's value for `name`.

    """
    path = _SRC_ROOT / "blender_mcp" / "bundled" / "addon" / relative_path
    spec = importlib.util.spec_from_file_location(f"_reply_size_literals_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"refusing to measure: {path} could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, name)


_DEFORMED_SAMPLE_LIMITATIONS = cast(
    "tuple[str, ...]",
    _addon_literal("handlers/character_rigging/contracts.py", "DEFORMED_SAMPLE_LIMITATIONS"),
)

# The rule of thumb `catalog_metrics.BYTES_PER_TOKEN` uses, respelled: importing it would
# load `blender_mcp` before BLENDER_MCP_TOOLSETS is set. Order-of-magnitude only.
_BYTES_PER_TOKEN = 3.6

# A 1x1 greyscale PNG. The four image-returning tools read a file the add-on wrote and
# hand its bytes back as an Image block; the stub writes this one so those calls complete.
# Image blocks are excluded from every measured figure - see `_UNMEASURED`.
_ONE_PIXEL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108000000003a7e9b55"
    "0000000a4944415408d76360000000020001e221bc330000000049454e44ae426082"
)


@dataclass(frozen=True)
class SceneScale:
    """
    How big the stubbed shot is, so a reply's growth can be attributed to its inputs.

    Defaults describe the captured reference shot. The handlers cap their own pages, so a
    count above a cap grows only the `*_count` / `*_truncated` fields, exactly as in Blender.

    Attributes:
        objects: Objects in the scene; `list_scene_objects` pages 25 of them.
        lights: Light objects; `list_lights` and `inspect_lighting_setup` page 50.
        bones: Bones posed in one `set_character_pose` call; the handler pages none.
        linked_datablocks: Datablocks one library links; the handler lists 100.
        mesh_vertices: Vertices in the inspected mesh; `get_mesh_data` pages 100.
        findings: Validation findings `validate_scene` reports; it pages 300.
        override_objects: Objects in the overridden linked collection; the handler lists 100.
        cycle_frames: Frames in the keyed walk cycle; `keyframe_bone_reach` pages none.

    """

    objects: int = 49
    lights: int = 7
    bones: int = 23
    linked_datablocks: int = 121
    mesh_vertices: int = 162
    findings: int = 84
    override_objects: int = 40
    cycle_frames: int = 24


REFERENCE_SCALE = SceneScale()

# Real values out of the captured replies, reused wherever a handler returns a solved
# transform. Blender hands back full-precision floats, and their digit width is most of a
# matrix's byte cost, so a fixture that rounded them would understate every reply.
_SOLVED_FLOATS: tuple[float, ...] = (
    0.7071068286895752,
    -0.26490652561187744,
    0.6556099057197571,
    6.510471343994141,
    0.7071067094802856,
    0.2649065852165222,
    -0.6556100249290466,
    -6.510471820831299,
    -4.235250372630617e-09,
    0.9271725416183472,
    0.3746344745159149,
    4.720269680023193,
    0.9999999403953552,
    0.31726211309432983,
    0.21398906409740448,
    0.5166153907775879,
)


def _floats(count: int, offset: int = 0, *, decimals: int | None = None) -> list[float]:
    """
    Take `count` real Blender floats, so a fixture's digit widths match a real reply.

    Args:
        count: How many values to take.
        offset: Where to start in `_SOLVED_FLOATS`, so sibling fields differ.
        decimals: Round to this many decimals, for a handler that publishes rounded floats.

    Returns:
        list[float]: The values, wrapping around the pool.

    """
    values = [_SOLVED_FLOATS[(offset + index) % len(_SOLVED_FLOATS)] for index in range(count)]
    return values if decimals is None else [round(value, decimals) for value in values]


def _matrix(offset: int = 0, *, decimals: int | None = None) -> list[list[float]]:
    """
    Build one 4x4 row-major matrix, as every handler publishes matrices.

    The bottom row of an object or pose matrix is always exactly `[0, 0, 0, 1]`, so only the
    first three rows carry full-precision values; a fixture that filled all sixteen with them
    would overstate every matrix by about forty bytes.

    Args:
        offset: Where to start in `_SOLVED_FLOATS`.
        decimals: Round to this many decimals, for a handler that publishes rounded matrices.

    Returns:
        list[list[float]]: Three solved rows and the affine bottom row.

    """
    rows = (_floats(4, offset + 4 * row, decimals=decimals) for row in range(3))
    return [*rows, [0.0, 0.0, 0.0, 1.0]]


def _transform(offset: int = 0, *, local_matrix: bool = True, decimals: int | None = None) -> dict[str, object]:
    """
    Mirror `handlers/camera/_shared.py:179 _transform_info` and `lighting/_shared.py transform_snapshot`.

    Args:
        offset: Where to start in `_SOLVED_FLOATS`.
        local_matrix: True for the camera variant, which publishes a local matrix too;
            False for the light variant, which publishes only the world one.
        decimals: Round to this many decimals; the lighting variant rounds, the camera one
            does not.

    Returns:
        dict[str, object]: `local` and `world` blocks.

    """
    local: dict[str, object] = {
        "location": _floats(3, offset, decimals=decimals),
        "rotation_mode": "XYZ",
        "rotation": _floats(3, offset + 3, decimals=decimals),
        "scale": _floats(3, offset + 6, decimals=decimals),
    }
    if local_matrix:
        local["matrix"] = _matrix(offset, decimals=decimals)
    return {
        "local": local,
        "world": {
            "location": _floats(3, offset, decimals=decimals),
            "rotation_quaternion": _floats(4, offset + 12, decimals=decimals),
            "scale": _floats(3, offset + 6, decimals=decimals),
            "matrix": _matrix(offset, decimals=decimals),
        },
    }


# `handlers/camera/_shared.py:293 _camera_settings`: every optics, display and DOF field.
_CAMERA_SETTINGS: Mapping[str, object] = MappingProxyType(
    {
        "type": "PERSP",
        "sensor_width": 36.0,
        "clip_end": 1000.0,
        "shift_x": 0.0,
        "clip_start": 0.10000000149011612,
        "lens": 50.0,
        "shift_y": 0.0,
        "sensor_fit": "AUTO",
        "sensor_height": 24.0,
        "ortho_scale": 6.0,
        "panorama_type": "FISHEYE_EQUISOLID",
        "show_composition_harmony_tri_b": False,
        "show_name": False,
        "show_safe_areas": False,
        "show_composition_center_diagonal": False,
        "show_passepartout": True,
        "passepartout_alpha": 0.5,
        "show_composition_golden_tria_a": False,
        "show_composition_center": False,
        "show_mist": False,
        "show_limits": False,
        "show_composition_thirds": False,
        "show_composition_harmony_tri_a": False,
        "show_composition_golden": False,
        "show_composition_golden_tria_b": False,
        "dof": {
            "aperture_fstop": 2.799999952316284,
            "aperture_ratio": 1.0,
            "aperture_rotation": 0.0,
            "use_dof": False,
            "aperture_blades": 0,
            "focus_object": None,
            "focus_distance": 10.0,
        },
    }
)

# `handlers/lighting/_shared.py light_settings_snapshot`, AREA variant.
_LIGHT_SETTINGS: Mapping[str, object] = MappingProxyType(
    {
        "color": [1.0, 1.0, 1.0],
        "cutoff_distance": 40.0,
        "diffuse_factor": 1.0,
        "energy": 1000.0,
        "exposure": 0.0,
        "normalize": True,
        "shape": "SQUARE",
        "size": 0.25,
        "size_y": 0.25,
        "specular_factor": 1.0,
        "spread": 3.1415927410125732,
        "temperature": 6500.0,
        "transmission_factor": 1.0,
        "use_custom_distance": False,
        "use_shadow": True,
        "use_temperature": False,
        "volume_factor": 1.0,
    }
)

# `handlers/lighting/_shared.py:236 node_tree_snapshot`, as a two-node world or light tree.
_NODE_TREE: Mapping[str, object] = MappingProxyType(
    {
        "nodes": [
            {"name": "World Output", "type": "ShaderNodeOutputWorld", "label": "", "mute": False},
            {"name": "Background", "type": "ShaderNodeBackground", "label": "", "mute": False},
        ],
        "node_count": 2,
        "links": [
            {
                "from_node": "Background",
                "from_socket": "Background",
                "to_node": "World Output",
                "to_socket": "Surface",
            }
        ],
        "truncated": False,
        "external_files": [],
    }
)

# The same snapshot of a world the lighting tools have already authored: their managed nodes
# stay in the graph, so every later world reply carries all eight of them and five links.
_MANAGED_WORLD_GRAPH: Mapping[str, object] = MappingProxyType(
    {
        "nodes": [
            {"name": "World Output", "type": "ShaderNodeOutputWorld", "label": "", "mute": False},
            {"name": "Background", "type": "ShaderNodeBackground", "label": "", "mute": False},
            *(
                {"name": f"MCP Lighting {name}", "type": node_type, "label": f"MCP Lighting {name}", "mute": False}
                for name, node_type in (
                    ("Background", "ShaderNodeBackground"),
                    ("World Output", "ShaderNodeOutputWorld"),
                    ("Sky", "ShaderNodeTexSky"),
                    ("Texture Coordinate", "ShaderNodeTexCoord"),
                    ("Environment Mapping", "ShaderNodeMapping"),
                )
            ),
            {
                "name": "MCP Lighting Environment Texture",
                "type": "ShaderNodeTexEnvironment",
                "label": "MCP Lighting Environment Texture",
                "mute": False,
                "image": "studio_probe",
                "filepath": "/shots/hero/hdri/studio.exr",
            },
        ],
        "node_count": 8,
        "links": [
            {"from_node": source, "from_socket": socket, "to_node": target, "to_socket": "Surface"}
            for source, socket, target in (
                ("Background", "Background", "World Output"),
                ("MCP Lighting Background", "Background", "MCP Lighting World Output"),
                ("MCP Lighting Sky", "Color", "MCP Lighting Background"),
                ("MCP Lighting Texture Coordinate", "Generated", "MCP Lighting Environment Mapping"),
                ("MCP Lighting Environment Mapping", "Vector", "MCP Lighting Environment Texture"),
            )
        ],
        "truncated": False,
        "external_files": [{"kind": "IMAGE", "resource": "studio_probe", "path": "/shots/hero/hdri/studio.exr"}],
    }
)

# `handlers/lighting/inspection.py:62 _quality_snapshot`: both engines' allowlisted properties.
_QUALITY_SNAPSHOT: Mapping[str, object] = MappingProxyType(
    {
        "cycles": {
            "samples": 4096,
            "use_adaptive_sampling": True,
            "adaptive_threshold": 0.009999999776482582,
            "use_denoising": True,
            "light_sampling_threshold": 0.009999999776482582,
            "sample_clamp_direct": 0.0,
            "sample_clamp_indirect": 10.0,
            "max_bounces": 12,
            "diffuse_bounces": 4,
            "glossy_bounces": 4,
            "transmission_bounces": 12,
            "transparent_max_bounces": 8,
            "volume_bounces": 0,
            "device": "CPU",
        },
        "eevee": {
            "taa_render_samples": 64,
            "light_threshold": 0.009999999776482582,
            "shadow_pool_size": "512",
            "shadow_resolution_scale": 1.0,
            "shadow_ray_count": 1,
            "shadow_step_count": 6,
            "use_raytracing": False,
            "ray_tracing_method": "SCREEN",
            "use_fast_gi": True,
            "volumetric_tile_size": "8",
            "volumetric_samples": 64,
            "volumetric_ray_depth": 16,
        },
    }
)

# `handlers/rendering.py:77 _layer_info` plus `:69 _pass_info`.
_VIEW_LAYER_INFO: Mapping[str, object] = MappingProxyType(
    {
        "name": "ViewLayer",
        "use": True,
        "use_sky": True,
        "use_solid": True,
        "use_strand": True,
        "material_override": None,
        "world_override": None,
        "passes": {
            "pass_cryptomatte_depth": 6,
            "use_pass_combined": True,
            "use_pass_cryptomatte_asset": False,
            "use_pass_cryptomatte_material": False,
            "use_pass_cryptomatte_object": False,
            "use_pass_material_index": False,
            "use_pass_mist": False,
            "use_pass_normal": False,
            "use_pass_object_index": False,
            "use_pass_position": False,
            "use_pass_uv": False,
            "use_pass_vector": False,
            "use_pass_z": False,
        },
    }
)


def _render_info(engine: str, samples: int) -> dict[str, object]:
    """
    Mirror `handlers/rendering.py:90 _render_info`, the whole-scene render state.

    `configure_render_settings` returns this block three times, as `before`, `after` and
    `settings`; `inspect_render_setup` returns it once.

    Args:
        engine: Render engine id to report.
        samples: Cycles sample count to report.

    Returns:
        dict[str, object]: The render-state block.

    """
    return {
        "scene": "Scene",
        "engine": engine,
        "camera": "Camera_Hero",
        "resolution": [1920, 1080, 100],
        "pixel_aspect": [1.0, 1.0],
        "fps": 24,
        "fps_base": 1.0,
        "frame_range": [1, 250, 1],
        "film_transparent": False,
        "motion_blur": {"enabled": False, "shutter": 0.5, "position": "CENTER"},
        "film": {"transparent": False, "transparent_glass": False, "transparent_roughness": 0.10000000149011612},
        "output": {
            "filepath": "/shots/hero/render/",
            "file_format": "PNG",
            "color_mode": "RGBA",
            "color_depth": "8",
            "compression": 15,
            "quality": 90,
            "use_file_extension": True,
            "use_overwrite": True,
            "use_placeholder": False,
            "exr_codec": "ZIP",
        },
        "cycles": {"samples": samples, "use_denoising": True},
        "eevee": {
            "taa_samples": 16,
            "taa_render_samples": 128,
            "use_shadows": True,
            "use_raytracing": True,
            "ray_tracing_method": "SCREEN",
            "ray_tracing": {
                "resolution_scale": "2",
                "screen_trace_quality": 0.25,
                "screen_trace_thickness": 0.10000000149011612,
                "trace_max_roughness": 0.5,
                "use_denoise": True,
            },
        },
        "metadata": {
            "use_stamp": False,
            "use_stamp_date": True,
            "use_stamp_time": True,
            "use_stamp_render_time": True,
            "use_stamp_frame": True,
            "use_stamp_frame_range": False,
            "use_stamp_camera": True,
            "use_stamp_scene": True,
            "use_stamp_note": False,
            "stamp_note_text": "",
        },
        "multiview": {"enabled": False, "views_format": "INDIVIDUAL", "stereo_3d_format": "ANAGLYPH"},
        "view_layers": [dict(_VIEW_LAYER_INFO)],
        "compositor": {"use_nodes": True, "node_tree": None, "node_count": 0},
    }


# `handlers/lighting/_shared.py:38 TRANSFORM_DECIMALS`: every lighting transform float is rounded.
_LIGHT_DECIMALS = 6


def _light_summary(index: int) -> dict[str, object]:
    """
    Mirror `handlers/lighting/_shared.py:288 light_summary`, the default inventory record.

    Args:
        index: Which light, so names and locations differ as they do in a real scene.

    Returns:
        dict[str, object]: The trimmed light record.

    """
    return {
        "object": f"Light_{index:03d}",
        "light_data": f"Light_{index:03d} Light",
        "light_type": "AREA",
        "energy": 1000.0,
        "color": [1.0, 1.0, 1.0],
        "location_world": _floats(3, index, decimals=_LIGHT_DECIMALS),
        "hidden_viewport": False,
        "hidden_render": False,
    }


def _light_snapshot(index: int, *, include_nodes: bool = False) -> dict[str, object]:
    """
    Mirror `handlers/lighting/_shared.py:304 light_snapshot`, one light's `detail=True` record.

    Args:
        index: Which light, so names and transforms differ as they do in a real scene.
        include_nodes: True for `inspect_light`, which adds the shader tree, both animation
            snapshots and the engine-compatibility note.

    Returns:
        dict[str, object]: The light record.

    """
    record: dict[str, object] = {
        "object": f"Light_{index:03d}",
        "light_data": f"Light_{index:03d} Light",
        "light_type": "AREA",
        "transform": _transform(index, local_matrix=False, decimals=_LIGHT_DECIMALS),
        "settings": dict(_LIGHT_SETTINGS),
        "data_users": 1,
        "collections": ["Lights"],
        "hidden_viewport": False,
        "hidden_render": False,
        "target_constraints": [],
        "light_group": "",
        "light_linking": {"supported": True, "receiver": None, "blocker": None},
    }
    if include_nodes:
        record |= {
            "use_nodes": True,
            "node_tree": dict(_NODE_TREE),
            "object_animation": {"action": None, "fcurves": [], "drivers": [], "truncated": False},
            "light_animation": {"action": None, "fcurves": [], "drivers": [], "truncated": False},
            "engine_compatibility": {
                "ordinary_light": ["CYCLES", "EEVEE"],
                "arbitrary_shader_nodes": "Cycles-first; verify EEVEE support for each node",
                "ies": "Cycles-first",
                "light_linking": ["CYCLES", "EEVEE"],
            },
        }
    return record


# `handlers/file_lifecycle.py:86 _library_summary` plus `handlers/linking.py:270 _library_details`.
_LIBRARY_DETAILS: Mapping[str, object] = MappingProxyType(
    {
        "session_uid": 977,
        "name": "canon.blend",
        "filepath": "canon.blend",
        "is_relative": False,
        "is_missing": False,
        "version": [5, 2, 45],
        "needs_liboverride_resync": False,
        "users": 1,
    }
)

# Page sizes the handlers apply to their own record lists, mirrored here so a count above a cap
# grows only a reply's count and pagination fields, exactly as it does in Blender.
_MAX_LISTED_NAMES = 10  # `helpers.py MAX_LISTED_NAMES`, the default page of names a counted list carries
_MESH_ELEMENT_PAGE = 100  # `tools/viewport.py get_mesh_data` default limit
# `tools/character_rigging/posing.py sample_deformed_geometry` default limit. Smaller than
# get_mesh_data's because each record adds a displacement to a position and a normal.
_DEFORMED_VERTEX_PAGE = 50
_SCENE_OBJECT_PAGE = 25  # `tools/scene.py list_scene_objects` default limit
_VALIDATE_SCENE_PAGE = 300  # `tools/scene.py validate_scene` default max_findings
_LIGHT_PAGE = 50  # `handlers/lighting/inspection.py:204 list_lights` default limit
_BONE_PAGE = 100  # `tools/character_rigging/posing.py:58 list_character_bones` default limit

_LIBRARY_NOTE = (
    "Every datablock linked from this library now has a new session_uid; references read before "
    "this call name nothing. Re-read them with detail=true here, or with list_libraries, before the next command."
)


def _linked_datablocks(total: int) -> dict[str, object]:
    """
    Mirror `handlers/linking.py _linked_datablocks` through `helpers.py counted_page`, without `detail`.

    The reference library links materials, meshes and the collection they hang off, so the
    `by_type` histogram a real reply carries is three entries wide. `detail=true` would
    replace `names` with `handlers/linking.py _linked_entry` records; the default is
    what every call that does not ask for them pays.

    Args:
        total: How many datablocks the library links.

    Returns:
        dict[str, object]: `{"datablocks": {...}}` - the exact total, the counts by type and
        one page of names.

    """
    listed = min(total, _MAX_LISTED_NAMES)
    return {
        "datablocks": {
            "total": total,
            "by_type": {"COLLECTION": 1, "MATERIAL": total // 2, "MESH": total - 1 - total // 2},
            "limit": _MAX_LISTED_NAMES,
            "returned_count": listed,
            "truncated": total > _MAX_LISTED_NAMES,
            "names": [f"prop_{index:03d}_mat" for index in range(listed)],
        }
    }


def _counted_objects(total: int, suffix: str) -> dict[str, object]:
    """
    Mirror `helpers.py counted_page` over linked objects (`by_type` is their `id_type`), without `detail`.

    Args:
        total: How many objects the link or override brought in.
        suffix: What Blender appends to each name, e.g. `.001` for an override.

    Returns:
        dict[str, object]: The exact total, the counts by type and one page of names.

    """
    listed = min(total, _MAX_LISTED_NAMES)
    return {
        "total": total,
        "by_type": {"OBJECT": total},
        "limit": _MAX_LISTED_NAMES,
        "returned_count": listed,
        "truncated": total > _MAX_LISTED_NAMES,
        "names": [f"prop_{index:03d}{suffix}" for index in range(listed)],
    }


def _counted_libraries(total: int) -> dict[str, object]:
    """
    Mirror `handlers/file_lifecycle.py _counted_libraries`, which every session poll and swap carries.

    Args:
        total: How many libraries the open file links; the reference shot links one.

    Returns:
        dict[str, object]: The exact total, the present/missing split and up to ten summaries.

    """
    listed = min(total, _MAX_LISTED_NAMES)
    summary = {key: _LIBRARY_DETAILS[key] for key in ("session_uid", "name", "filepath")}
    return {
        "total": total,
        "by_type": {"PRESENT": total} if total else {},
        "limit": _MAX_LISTED_NAMES,
        "returned_count": listed,
        "truncated": total > _MAX_LISTED_NAMES,
        "records": [dict(summary) for _ in range(listed)],
    }


def _delivery_entries() -> list[dict[str, object]]:
    """
    Mirror `handlers/delivery.py:_entry` for a representative shot's external references.

    One canon library, a dozen textures (one of them left on an absolute path, which is the
    finding the tool exists for), a title font, the cloth and rigid-body caches, and the
    scene's own output template.

    Returns:
        list[dict[str, object]]: One page of entries in the tool's fixed order.

    """
    entries: list[dict[str, object]] = [
        {
            "kind": "LIBRARY",
            "name": "canon.blend",
            "path": "//../canon/canon.blend",
            "absolute": False,
            "verdict": "RELATIVE_OK",
            "detail": {"indirect": False, "sha256": "", "hash_skipped": ""},
        }
    ]
    for index in range(12):
        absolute = index == 0
        entries.append(
            {
                "kind": "IMAGE",
                "name": f"prop_{index:03d}_basecolor.png",
                "path": "prop_000_basecolor.png" if absolute else f"//textures/prop_{index:03d}_basecolor.png",
                "absolute": absolute,
                "verdict": "ABSOLUTE" if absolute else "RELATIVE_OK",
                "detail": {"users": 1, "dirty": False, "source": "FILE"},
            }
        )
    entries.append(
        {
            "kind": "FONT",
            "name": "TitleSans.ttf",
            "path": "//fonts/TitleSans.ttf",
            "absolute": False,
            "verdict": "RELATIVE_OK",
            "detail": {"users": 1},
        }
    )
    for owner in ("Hero_Cloak:Cache", "Scene:RigidBodyWorld"):
        entries.append(
            {
                "kind": "CACHE",
                "name": owner,
                "path": "",
                "absolute": False,
                "verdict": "RELATIVE_OK",
                "detail": {"use_disk_cache": True, "use_external": False, "is_baked": True, "is_outdated": False},
            }
        )
    entries.append(
        {
            "kind": "RENDER_OUTPUT",
            "name": "Scene",
            "path": "//renders/sh010_",
            "absolute": False,
            "verdict": "RELATIVE_OK",
            "detail": {"file_format": "OPEN_EXR_MULTILAYER"},
        }
    )
    return entries


def _bone_name(index: int) -> str:
    """
    Name one bone the way a production rig does, so name length is realistic.

    A body rig's names average around nine characters (`spine.001`, `upper_arm.L`), and every
    bone entry repeats its name, so a shorter fixture name would understate the reply.

    Args:
        index: Which bone.

    Returns:
        str: A unique bone name.

    """
    stems = ("spine", "neck", "head", "shoulder.L", "upper_arm.L", "forearm.L", "hand.L", "thigh.R", "shin.R")
    stem = stems[index % len(stems)]
    generation = index // len(stems)
    return stem if generation == 0 else f"{stem}.{generation:03d}"


# `handlers/character_rigging/posing.py:76 _POSE_MATRIX_DECIMALS`: the default pose record
# rounds its one matrix; `detail=True` publishes Blender's own precision instead.
_POSE_DECIMALS = 6


# `handlers/character_rigging/posing.py _MAX_PROBE_REFERENCES`: the handler takes at most six
# named world directions, and each one costs a signed number on every probed axis - so six
# names against six axes is the widest reply `probe_bone_axis` can send.
_PROBE_REFERENCE_NAMES = ("camera_right", "camera_up", "world_up", "facing", "stride", "lateral")


def _bone_pose_entries(bones: int) -> list[dict[str, object]]:
    """
    Mirror `handlers/character_rigging/posing.py:92 _apply_pose_specs`, one entry per posed bone.

    The default entry carries the resulting pose matrix once, rounded; `detail=True` adds the
    pre-call matrix and drops the rounding, and is not what this measures. The captured call
    posed each bone by `matrix`, which is why its keys cover location, rotation and scale.

    Args:
        bones: How many bones the call posed.

    Returns:
        list[dict[str, object]]: One record per bone.

    """
    return [
        {
            "bone": _bone_name(index),
            "channels": ["matrix"],
            "after_pose_matrix": _matrix(index + 2, decimals=_POSE_DECIMALS),
        }
        for index in range(bones)
    ]


def _reach_records(bones: int) -> list[dict[str, object]]:
    """
    Mirror `handlers/character_rigging/reach.py _solve_one_reach`, one entry per solved reach.

    A reach reports the chain it resolved, where the tip's tail landed, whether that is inside
    the requested tolerance and how the target sits against the chain's own reach, and carries
    the same per-bone pose records `set_character_pose` returns for that chain. Two reaches,
    split evenly, because the tool exists for the two-rigs-shaking-hands case: one chain per
    rig, one call. The converged case is the sample: a missed reach costs the same four fields
    plus one envelope warning, and a warning is bounded by the budget the envelope already fits.

    Args:
        bones: How many bones the call posed across every reach.

    Returns:
        list[dict[str, object]]: One record per reach.

    """
    posed = _bone_pose_entries(bones)
    first = bones // 2
    return [
        {
            "tip_bone": _bone_name(start),
            "chain_bones": [_bone_name(index) for index in range(start, start + length)],
            "chain_length": length,
            "chain_length_source": "resolved",
            "pole_source": "resolved",
            "target_world": _floats(3),
            "head_world": _floats(3, 3),
            "tail_world": _floats(3, 6),
            # What a real 500-iteration Blender IK solve converges to, measured on a bent chain.
            "achieved_error_m": 3.28369698225788e-05,
            "converged": True,
            "chain_reach_m": 0.7412000000000001,
            "target_distance_m": 0.6839274418394129,
            "out_of_reach": False,
            "bones": posed[start : start + length],
        }
        for start, length in ((0, first), (first, bones - first))
    ]


def _keyed_reach_records(frames: int) -> list[dict[str, object]]:
    """
    Mirror `handlers/character_rigging/reach.py _keyed_reach_record`, for a two-foot walk.

    Two reaches, one per foot, each a three-bone leg keyed at every frame of the cycle. This
    is the tool's whole reason to exist, so the representative call is the representative
    walk: the per-frame records are what the reply budget shortens, and `changed_bones` and
    the warnings are what stay complete.

    Args:
        frames: Frames in the cycle each foot is keyed at.

    Returns:
        list[dict[str, object]]: One record per reach.

    """
    return [
        {
            "tip_bone": f"foot.{side}",
            "chain_bones": [f"foot.{side}", f"shin.{side}", f"thigh.{side}"],
            "chain_length": 3,
            "chain_length_source": "resolved",
            "pole_source": "explicit",
            "keys": [
                {
                    "frame": float(frame),
                    "target_world": _floats(3, frame),
                    "tail_world": _floats(3, frame + 3),
                    "achieved_error_m": 3.28369698225788e-05,
                    "converged": True,
                    "chain_reach_m": 0.7412000000000001,
                    "target_distance_m": 0.6839274418394129,
                    "out_of_reach": False,
                }
                for frame in range(1, frames + 1)
            ],
        }
        for side in ("L", "R")
    ]


def _scene_finding(index: int) -> dict[str, object]:
    """
    Mirror `handlers/texture/validation.py:8 _finding`, as `validate_scene` aggregates them.

    Args:
        index: Which finding, so subjects differ.

    Returns:
        dict[str, object]: One finding record.

    """
    return {
        "domain": "pbr",
        "severity": "ERROR",
        "code": "MISSING_BASE_COLOR",
        "subject": f"set_{index:03d}",
        "message": "The material has no base-colour input.",
        "evidence": {"material": f"set_{index:03d}_mat", "inputs": []},
        "remediation": "Connect a base-colour texture or set an explicit value.",
    }


def _capability_names() -> tuple[str, ...]:
    """
    Read the add-on's advertised command names out of its own source, without importing `bpy`.

    `server_core.get_addon_info` publishes `sorted(self._build_command_handlers())`, which is
    every row of `command_registry`'s module-level `COMMANDS` registry whose spec carries no `provider` gate.
    The provider-gated rows come and go with the open .blend's scene flags and are absent
    while those integrations are disabled, which is the default.

    Returns:
        tuple[str, ...]: The sorted capability names, as a handshake really reports them.

    Raises:
        SystemExit: If the add-on's command registry cannot be found, so a silently short
            capability list cannot be mistaken for a measurement.

    """
    import ast  # ruff: ignore[import-outside-top-level] - kept local so importing this module parses nothing.

    source = (_SRC_ROOT / "blender_mcp" / "bundled" / "addon" / "command_registry.py").read_text(encoding="utf-8")
    registry = next(
        (
            node.value
            for node in ast.parse(source).body
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "COMMANDS"
        ),
        None,
    )
    if registry is None:
        raise SystemExit("refusing to measure: command_registry.COMMANDS not found")
    table = next((node for node in ast.walk(registry) if isinstance(node, ast.Dict) and node.keys), None)
    if table is None:
        raise SystemExit("refusing to measure: command_registry.COMMANDS holds no command rows")
    return tuple(
        sorted(
            key.value
            for key, spec in zip(table.keys, table.values, strict=True)
            if isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            # A provider-gated row is off unless the open .blend turns it on.
            and not any(keyword.arg == "provider" for keyword in spec.keywords)
        )
    )


_SESSION_FIELDS: Mapping[str, object] = MappingProxyType(
    {
        "session_id": "bd0400887c3f4867990b91deaddf3a0f",
        "session_epoch": 3,
        "current_filepath": "/shots/hero/shot.blend",
        "session_indeterminate": False,
    }
)

_SWAP_NOTE = (
    "The open database was replaced: session_epoch moved, and the addon's capabilities follow the "
    "file. Re-handshake (get_addon_info) before relying on a cached capability list."
)

_PROVIDER_MESSAGE = (
    "PolyHaven integration is currently disabled. To enable it:\n"
    "                            1. In the 3D Viewport, find the BlenderMCP panel in the sidebar "
    "(press N if hidden)\n"
    "                            2. Check the 'Use assets from Poly Haven' checkbox\n"
    "                            3. Restart the connection to Claude"
)


def _world_result(source: str, extra: Mapping[str, object]) -> dict[str, object]:
    """
    Mirror the shared world-authoring reply in `handlers/lighting/environment.py`.

    Args:
        source: Which world source the call installed.
        extra: The per-tool fields appended after the shared block.

    Returns:
        dict[str, object]: The world reply.

    """
    return {
        "scene": "Scene",
        "world": "World",
        "source": source,
        "managed_nodes_created": ["MCP Lighting Background", "MCP Lighting World Output"],
        "world_graph": dict(_MANAGED_WORLD_GRAPH),
        "transparent_film": False,
        "world_scene_users": ["Scene"],
        "warnings": [],
        "changed_objects": [],
        "changed_resources": ["World"],
        **extra,
    }


def _payloads() -> dict[str, Callable[[SceneScale], object]]:
    """
    Map every add-on command a `shot` tool dispatches to the payload its handler returns.

    Returns:
        dict[str, Callable[[SceneScale], object]]: Command name to payload builder.

    """
    return {
        # --- core: server_core.py -------------------------------------------------------
        # `server_core.py:910 get_addon_info`.
        "get_addon_info": lambda _scale: {
            "name": "Blender MCP",
            "addon_version": [2, 0, 0],
            "protocol_version": 31,
            "capabilities": list(_capability_names()),
            "blender_version": "5.2.2 LTS",
            "writable_output_roots": ["/shots/hero/render", "/tmp"],
            "file_roots": ["/shots"],
            "file_roots_enforced": True,
            **_SESSION_FIELDS,
        },
        "get_polyhaven_status": lambda _scale: {"enabled": False, "message": _PROVIDER_MESSAGE},
        "get_sketchfab_status": lambda _scale: {"enabled": False, "message": _PROVIDER_MESSAGE},
        "get_nd_status": lambda _scale: {"enabled": False, "message": _PROVIDER_MESSAGE},
        # --- scene inspection: handlers/scene_inspection.py:149, handlers/scene.py ------
        "get_object_info": lambda _scale: {
            "name": "Hero",
            "type": "MESH",
            "library": None,
            "is_override": False,
            "location": _floats(3),
            "rotation_mode": "XYZ",
            "rotation": _floats(3, 3),
            "scale": _floats(3, 6),
            "matrix_world": _matrix(),
            "dimensions": _floats(3, 9),
            "parent": None,
            "parent_type": "OBJECT",
            "parent_bone": None,
            "collections": ["Characters"],
            "data_name": "Hero_Mesh",
            "selected": True,
            "visible": True,
            "hide_viewport": False,
            "hide_render": False,
            "materials": ["Hero_Skin"],
            "modifiers": [],
            "world_bounding_box": [_floats(3, 1), _floats(3, 4)],
            "mesh": {"vertices": 162, "edges": 480, "polygons": 320},
            "type_data": {
                "coordinate_space": "OBJECT_LOCAL",
                "evaluated": False,
                "attributes": {
                    "total": 12,
                    "offset": 0,
                    "limit": 100,
                    "returned_count": 12,
                    "truncated": False,
                    "next_offset": None,
                    "records": [
                        {"name": f"attribute_{index:02d}", "data_type": "FLOAT2", "domain": "CORNER", "count": 960}
                        for index in range(12)
                    ],
                },
            },
        },
        "get_mesh_data": lambda scale: {
            "name": "Hero",
            "element_type": "vertices",
            "total": scale.mesh_vertices,
            "total_unfiltered": scale.mesh_vertices,
            "offset": 0,
            "limit": _MESH_ELEMENT_PAGE,
            "returned_count": min(scale.mesh_vertices, _MESH_ELEMENT_PAGE),
            "truncated": scale.mesh_vertices > _MESH_ELEMENT_PAGE,
            "next_offset": _MESH_ELEMENT_PAGE if scale.mesh_vertices > _MESH_ELEMENT_PAGE else None,
            "elements": [
                {"index": index, "co": _floats(3, index), "normal": _floats(3, index + 3), "select": True}
                for index in range(min(scale.mesh_vertices, _MESH_ELEMENT_PAGE))
            ],
        },
        "list_scene_objects": lambda scale: {
            "name": "Scene",
            "object_count": scale.objects,
            "matched_count": scale.objects,
            "search": None,
            "objects": [
                {
                    "name": f"set_{index:03d}",
                    "type": "MESH",
                    "location": _floats(3, index),
                    "parent": None,
                    "collections": ["Set"],
                    "selected": False,
                    "visible": True,
                    "hide_viewport": False,
                    "hide_render": False,
                }
                for index in range(min(scale.objects, _SCENE_OBJECT_PAGE))
            ],
            "materials_count": 12,
            "active_object": "Hero_Rig",
            "selected_count": 1,
            "mode": "OBJECT",
            "unit_settings": {"system": "METRIC", "scale_length": 1.0, "length_unit": "METERS"},
            "offset": 0,
            "limit": _SCENE_OBJECT_PAGE,
            "returned_count": min(scale.objects, _SCENE_OBJECT_PAGE),
            "truncated": scale.objects > _SCENE_OBJECT_PAGE,
            "next_offset": _SCENE_OBJECT_PAGE if scale.objects > _SCENE_OBJECT_PAGE else None,
        },
        "validate_scene": lambda scale: {
            "scene": "Scene",
            "domains_checked": ["scene", "camera", "lighting", "pbr", "cloth", "liquid"],
            "domain_summaries": {
                name: {"findings": 0, "truncated": False}
                for name in ("camera", "lighting", "pbr", "cloth", "liquid", "scene")
            },
            "findings": [_scene_finding(index) for index in range(scale.findings)],
            "summary": {"ERROR": scale.findings, "WARNING": 0, "INFO": 0},
            "total_findings": scale.findings,
            "offset": 0,
            "limit": _VALIDATE_SCENE_PAGE,
            "returned_count": min(scale.findings, _VALIDATE_SCENE_PAGE),
            "truncated": scale.findings > _VALIDATE_SCENE_PAGE,
            "next_offset": _VALIDATE_SCENE_PAGE if scale.findings > _VALIDATE_SCENE_PAGE else None,
            "domains_truncated": False,
            "ready": False,
            "limitations": [
                "Aggregates validate_pbr_asset, validate_lighting_setup, validate_cloth_setup, "
                "validate_liquid_setup, and validate_camera_rig plus scene-level checks (camera/light "
                "presence when those domains are excluded from scope, frame range consistency, unapplied "
                "scale, degenerate geometry, dirty cloth/rigid-body caches); it is a structural preflight, "
                "not a rendered-frame review."
            ],
        },
        "set_object_transform": lambda _scale: {
            "name": "Hero",
            "location": _floats(3),
            "rotation": _floats(3, 3),
            "rotation_mode": "XYZ",
            "scale": _floats(3, 6),
            "matrix_world": _matrix(),
            "world_location": _floats(3),
            "world_rotation_quaternion": _floats(4, 12),
            "world_scale": _floats(3, 6),
        },
        "manage_object_constraints": lambda _scale: {
            "object": "set_000",
            "constraint": "Copy Location",
            "type": "COPY_LOCATION",
            "stack_index": 0,
        },
        "manage_modifiers": lambda _scale: {
            "name": "Hero",
            "vertices": 162,
            "edges": 480,
            "polygons": 320,
            "applied": False,
            "modifier": "Subdivision",
            "evaluated": {"vertices": 962, "edges": 1920, "polygons": 960},
            "bounds": {"min": _floats(3, 1), "max": _floats(3, 4)},
        },
        "manage_object_hierarchy": lambda _scale: {
            "assignments": [{"child_object_name": "set_001", "parent_object_name": "Hero"}],
            "changed_objects": ["set_001"],
        },
        # `handlers/scene.py manage_scene_collections`: the collection is the change; its members
        # are counted the way `helpers.counted_page` counts objects, and a new one has none.
        "manage_scene_collections": lambda _scale: {
            "name": "Receivers",
            "objects": {
                "total": 0,
                "by_type": {},
                "limit": _MAX_LISTED_NAMES,
                "returned_count": 0,
                "truncated": False,
                "names": [],
            },
            "hide_viewport": False,
            "hide_render": False,
            "changed_objects": [],
            "changed_resources": ["Receivers"],
        },
        "duplicate_or_instance_objects": lambda _scale: {
            "source": "Hero",
            "mode": "LINKED_DATA",
            "objects": [{"name": "Hero_002", "data": "Hero_Mesh"}],
            "changed_objects": ["Hero_002"],
        },
        # `handlers/scene.py:1384 set_object_visibility`, the durable alternative to
        # `remove_scene_objects` for a library-override object: it changes a property instead of
        # removing the ID, so it survives Blender's own liboverride resync on the next file load.
        "set_object_visibility": lambda _scale: {
            "name": "Scratch_Proxy",
            "hide_render": True,
            "hide_viewport": True,
            "hide_select": False,
            "changed_objects": ["Scratch_Proxy"],
        },
        # `handlers/scene.py remove_scene_objects`, the core-surface way a session takes back a
        # scratch object it made: what went is counted, with the dependencies of the named sample
        # and each shared datablock that stayed; nothing removed is left to name as changed.
        "remove_scene_objects": lambda _scale: {
            "removed": {
                "total": 1,
                "by_type": {"MESH": 1},
                "limit": _MAX_LISTED_NAMES,
                "returned_count": 1,
                "truncated": False,
                "names": ["Scratch_Proxy"],
            },
            "selector": None,
            "dependencies": {
                "Scratch_Proxy": {
                    "children": [],
                    "collections": ["Scratch"],
                    "data": "Scratch_Proxy_Mesh",
                    "data_users_before": 1,
                    "materials": [{"name": "Hero_Skin", "users_before": 2}],
                }
            },
            "retained_shared_datablocks": [{"kind": "MATERIAL", "name": "Hero_Skin", "reason": "shared users remain"}],
            "purged_datablocks": [],
            "changed_objects": [],
        },
        "set_viewport_overlay": lambda _scale: {"toggle": "CAVITY", "enabled": True},
        "get_viewport_screenshot": lambda _scale: {
            "width": 1000,
            "height": 562,
            "method": "offscreen",
            "view_source": "live_viewport",
            "shading_mode": "SOLID",
        },
        # --- file lifecycle and linking: handlers/file_lifecycle.py, handlers/linking.py
        "get_session_info": lambda _scale: {
            **_SESSION_FIELDS,
            "last_load_error": None,
            "last_save_error": None,
            "is_dirty": True,
            "libraries": _counted_libraries(1),
        },
        # `handlers/delivery.py:inspect_delivery`. A representative shot references one canon
        # library, a dozen textures, a font, both simulation caches and its own output template.
        "inspect_delivery": lambda _scale: {
            "scene": "Scene",
            "blend_filepath": "/shots/hero/shot.blend",
            "saved": True,
            "portable": False,
            "classes": {
                "LIBRARY": {"total": 1, "unportable": 0},
                "IMAGE": {"total": 12, "unportable": 1},
                "FONT": {"total": 1, "unportable": 0},
                "SOUND": {"total": 0, "unportable": 0},
                "CACHE": {"total": 2, "unportable": 0},
                "RENDER_OUTPUT": {"total": 1, "unportable": 0},
                "PATH": {"total": 0, "unportable": 0},
            },
            "entries": _delivery_entries(),
            "limit": 50,
            "offset": 0,
            "total": 17,
            "truncated": False,
            "next_offset": None,
            "provenance": None,
            "changed_objects": [],
            "warnings": [],
            "limitations": [
                "Paths that are not // -relative are reported by leaf name only, so the reply never carries "
                "this host's directory layout.",
                "A packed image is portable; a packed image with unsaved edits (dirty) is not yet written.",
                "Verdicts describe path shape and existence on this machine, not whether the destination can "
                "read them.",
            ],
        },
        "save_shot": lambda _scale: {
            "filepath": "/shots/hero/shot.blend",
            "saved_in_place": False,
            "overwrote_existing": False,
            "created_directory": False,
            "compress": False,
            "relative_remap": False,
            "session_id": _SESSION_FIELDS["session_id"],
            "session_epoch": _SESSION_FIELDS["session_epoch"],
        },
        "open_shot": lambda scale: {
            **_SESSION_FIELDS,
            "filepath": "/shots/hero/shot.blend",
            "rehandshake_required": True,
            "note": _SWAP_NOTE,
            "discarded_unsaved_changes": False,
            "scene_name": "Scene",
            "object_count": scale.objects,
            "libraries": _counted_libraries(1),
            "capabilities_changed": False,
        },
        "reset_session": lambda _scale: {
            **_SESSION_FIELDS,
            "filepath": None,
            "rehandshake_required": True,
            "note": _SWAP_NOTE,
            "discarded_unsaved_changes": False,
            "scene_name": "Scene",
            "object_count": 0,
            "libraries": _counted_libraries(0),
            "capabilities_changed": False,
        },
        # `handlers/linking.py:830 link_canon_library`.
        "link_canon_library": lambda scale: {
            # The roots only: one set root parents every prop, which `instanced_objects` counts.
            "changed_objects": ["set_root"],
            "library": dict(_LIBRARY_DETAILS),
            "library_already_linked": False,
            "scene_name": "Scene",
            "collections": [
                {
                    "session_uid": 978,
                    "name": "CanonProps",
                    "id_type": "COLLECTION",
                    "is_library_indirect": False,
                    "is_missing": False,
                }
            ],
            "objects": [],
            "instanced_objects": _counted_objects(scale.override_objects + 1, ""),
            "world": None,
            "previous_world": None,
            "overrides": [],
        },
        # `handlers/linking.py:1011 list_libraries`.
        "list_libraries": lambda scale: {
            "libraries": [{**_LIBRARY_DETAILS, **_linked_datablocks(scale.linked_datablocks)}],
            "total": 1,
            "offset": 0,
            "limit": 25,
            "returned_count": 1,
            "truncated": False,
            "next_offset": None,
        },
        # `handlers/linking.py:1042 reload_library`.
        "reload_library": lambda scale: {
            "library": dict(_LIBRARY_DETAILS),
            **_linked_datablocks(scale.linked_datablocks),
            "note": _LIBRARY_NOTE,
        },
        # `handlers/linking.py:1098 relocate_library`.
        "relocate_library": lambda scale: {
            "library": dict(_LIBRARY_DETAILS),
            **_linked_datablocks(scale.linked_datablocks),
            "name_before": "canon.blend",
            "name_after": "canon.blend",
            "note": _LIBRARY_NOTE,
        },
        # `handlers/linking.py:960 create_override` over `:597 _override_hierarchy`.
        "create_override": lambda scale: {
            "override": {
                "session_uid": 1139,
                "name": "CanonProps.001",
                "is_override": True,
                "is_editable": True,
                "is_system_override": False,
                "reference_uid": 978,
                "hierarchy_root_uid": 1139,
            },
            "scene_name": "Scene",
            "replaced_instances": 1,
            "objects": _counted_objects(scale.override_objects + 1, ".001"),
            "changed_objects": ["set_root.001"],
        },
        "unlink_libraries": lambda scale: {
            "removed_libraries": [{key: _LIBRARY_DETAILS[key] for key in ("session_uid", "name", "filepath")}],
            "already_removed_uids": [],
            "removed_count": scale.linked_datablocks + 1,
            "removed_by_type": {"libraries": 1, "objects": 40, "collections": 1, "meshes": 40, "materials": 40},
            "removed_sample": [
                {"name": f"prop_{index:03d}_mat", "id_type": "MATERIAL"}
                for index in range(min(scale.linked_datablocks + 1, _MAX_LISTED_NAMES))
            ],
            "purged_orphans": 0,
            "purged_by_type": {},
            "other_libraries_removed": [],
        },
        # --- camera: handlers/camera/ ----------------------------------------------------
        # `handlers/camera/core.py:125 create_camera`.
        "create_camera": lambda _scale: {
            "object": "Camera_Hero",
            "camera_data": "Camera_Hero Data",
            "collection": "Cameras",
            "scene": "Scene",
            "active_scene_camera": True,
            "transform": _transform(),
            "settings": dict(_CAMERA_SETTINGS),
            "changed_objects": ["Camera_Hero"],
            "changed_resources": ["Camera_Hero Data"],
        },
        "configure_camera": lambda _scale: {
            "camera": "Camera_Hero",
            "camera_data": "Camera_Hero Data",
            "old": {"lens": 50.0},
            "new": {"lens": 85.0},
            "changed_objects": ["Camera_Hero"],
            "changed_resources": ["Camera_Hero Data"],
        },
        "configure_camera_dof": lambda _scale: {
            "camera": "Camera_Hero",
            "camera_data": "Camera_Hero Data",
            "old": {"use_dof": False, "aperture_fstop": 2.8, "focus_object": None, "focus_distance": 10.0},
            "new": {"use_dof": True, "aperture_fstop": 2.799999952316284, "focus_object": None, "focus_distance": 10.0},
            "focus_intent": "DISTANCE",
            "changed_objects": ["Camera_Hero"],
            "changed_resources": ["Camera_Hero Data"],
            "warnings": ["Depth-of-field appearance depends on the render engine and sampling settings."],
        },
        "configure_camera_render_gate": lambda _scale: {
            "scene": "Scene",
            "camera": "Camera_Hero",
            "old": {"render": {}, "border": {}, "safe_areas": {}, "guides": {"show_composition_thirds": False}},
            "new": {"render": {}, "border": {}, "safe_areas": {}, "guides": {"show_composition_thirds": True}},
            "changed_objects": ["Camera_Hero"],
            "changed_resources": ["Scene", "Camera_Hero Data"],
        },
        "set_scene_camera": lambda _scale: {
            "scene": "Scene",
            "previous_camera": "Camera",
            "camera": "Camera_Hero",
            "marker": None,
            "warnings": [],
            "changed_objects": [],
        },
        "create_camera_target": lambda _scale: {
            "target": "Camera_Hero_target",
            "created": True,
            "location_world": _floats(3),
            "source_object": None,
            "constraints": [],
            "changed_objects": ["Camera_Hero_target"],
        },
        "create_camera_markers": lambda _scale: {
            "action": "LIST",
            "camera_cuts": [],
            "warnings": [],
            "changed_objects": [],
        },
        # `handlers/camera/targeting.py:627 frame_camera_on_objects`: a rig whose helper meshes
        # outnumber the cap, so excluded_objects is full and excluded_total counts the rest.
        "frame_camera_on_objects": lambda _scale: {
            "camera": "Camera_Hero",
            "objects": ["Hero", "set_000"],
            "bone_targets": [],
            "armature_meshes": {"Hero_Rig": ["Hero_Body", "Hero_Coat", "Hero_Hair"]},
            "framed_objects": ["Hero", "set_000", "Hero_Body", "Hero_Coat", "Hero_Hair"],
            "excluded_objects": [
                {"object": f"Hero_Collision_Proxy_{index:03d}", "reason": "DISPLAY_WIRE_OR_BOUNDS"}
                for index in range(16)
            ],
            "excluded_total": 40,
            "policy": "MOVE_CAMERA",
            "margin": 0.1,
            "bounds_world": {"min": _floats(3, 1), "max": _floats(3, 4)},
            "target_point_world": _floats(3, 7),
            "limiting_axis": "VERTICAL",
            "limiting_objects": ["Hero_Body", "set_000"],
            "transform": _transform(),
            "distance": 9.930403472696543,
            "lens": 50.0,
            "warnings": [],
            "changed_objects": ["Camera_Hero"],
            "changed_resources": [],
        },
        "point_camera_at": lambda _scale: {
            "camera": "Camera_Hero",
            "target_object": "Hero",
            "target_point": _floats(3, 7),
            "transform": _transform(),
            "changed_objects": ["Camera_Hero"],
        },
        "add_camera_constraint": lambda _scale: {
            "owner": "Camera_Hero",
            "constraint": {
                "name": "Track To",
                "type": "TRACK_TO",
                "influence": 1.0,
                "mute": False,
                "is_valid": True,
                "owner_space": "WORLD",
                "target_space": "WORLD",
                "target": "Hero",
                "subtarget": "",
                "track_axis": "TRACK_NEGATIVE_Z",
                "up_axis": "UP_Y",
            },
            "created": True,
            "assigned_world_matrix": _matrix(),
            "evaluated_world_matrix": _matrix(),
            "evaluated_transform_changed": False,
            "changed_objects": ["Camera_Hero"],
        },
        "get_camera_rig_info": lambda _scale: {
            "scene": "Scene",
            "object": "Camera_Hero",
            "object_type": "CAMERA",
            "parent_hierarchy": [],
            "transform": _transform(),
            "constraints": [],
            "drivers": [],
            "rig_metadata": {},
            "active_scene_camera": True,
            "camera_markers": [],
            "render_gate": {
                "resolution_x": 1920,
                "resolution_y": 1080,
                "resolution_percentage": 100,
                "pixel_aspect_x": 1.0,
                "pixel_aspect_y": 1.0,
                "display_aspect": 1.7777777777777777,
            },
            "children": [],
            "children_total": 0,
            "children_offset": 0,
            "children_returned_count": 0,
            "children_truncated": False,
            "children_next_offset": None,
            "children_scan_capped": False,
            "animation": [],
            "animation_total": 0,
            "animation_offset": 0,
            "animation_returned_count": 0,
            "animation_truncated": False,
            "animation_next_offset": None,
            "animation_scan_capped": False,
            "camera_data": "Camera_Hero Data",
            "camera": dict(_CAMERA_SETTINGS),
        },
        "validate_camera_rig": lambda _scale: {
            "scene": "Scene",
            "objects_checked": ["Camera", "Camera_Hero", "Camera_Hero_target"],
            "sampled_frames": [1],
            "findings": [],
            "summary": {"error": 0, "warning": 0, "info": 0},
            "verification": "Structural and evaluated-transform checks only; visual correctness was not inferred.",
        },
        "keyframe_camera_rig": lambda _scale: {
            "keyframes": [{"data_path": "location", "array_index": index, "frame": 1} for index in range(3)],
            "actions": ["Camera_Hero DataAction"],
            "policy": "REPLACE",
            "changed_objects": ["Camera_Hero"],
            "changed_resources": ["Camera_Hero DataAction"],
        },
        "set_camera_interpolation": lambda _scale: {
            "object": "Camera_Hero",
            "owner": "OBJECT",
            "action": "Camera_Hero DataAction",
            "matched_curves": [{"data_path": "location", "array_index": index} for index in range(3)],
            "changed_keys": [{"array_index": index, "frame": 1.0} for index in range(3)],
            "changed_objects": ["Camera_Hero"],
            "changed_resources": ["Camera_Hero DataAction"],
        },
        "add_camera_shake": lambda _scale: {
            "camera": "Camera_Hero",
            "control": "Camera_Hero_shake",
            "action": "Camera_Hero_shakeAction",
            "noise_modifiers": [
                {"data_path": "location", "array_index": index, "strength": 0.019999999552965164, "phase": 0.0}
                for index in range(6)
            ],
            "disable": "Mute action 'Camera_Hero_shakeAction' or set each Noise modifier influence to 0.",
            "changed_objects": ["Camera_Hero_shake", "Camera_Hero"],
            "changed_resources": ["Camera_Hero_shakeAction"],
        },
        "create_focus_pull": lambda _scale: {
            "camera": "Camera_Hero",
            "mode": "DISTANCE",
            "start": {"frame": 1, "point": _floats(3), "camera_space_depth": 9.94029712677002},
            "end": {"frame": 48, "point": _floats(3, 3), "camera_space_depth": 9.295225143432617},
            "focus_control": None,
            "changed_keys": [
                {"data_path": "dof.focus_distance", "array_index": 0, "frame": float(frame)} for frame in (1, 48)
            ],
            "warnings": [],
            "changed_objects": ["Camera_Hero"],
            "changed_resources": ["Camera_Hero Data", "Camera_Hero DataAction"],
        },
        "create_dolly_zoom": lambda _scale: {
            "camera": "Camera_Hero",
            "movement_object": "Hero",
            "framing_axis": "VERTICAL",
            "solutions": [
                {
                    "frame": frame,
                    "distance": 6.0,
                    "lens": 85.0,
                    "subject_reference_size": 1.0,
                    "projected_frame_fraction": 0.5902777129077244,
                    "subject_point": _floats(3),
                    "camera_location": _floats(3, 3),
                    "evaluated_camera_location": _floats(3, 6),
                    "evaluated_distance": 9.94029580380354,
                }
                for frame in (1, 48)
            ],
            "changed_keys": [{"data_path": "location", "array_index": index % 3, "frame": 1.0} for index in range(8)],
            "approximation": "Preserves the lens-to-camera-space-distance ratio for the subject reference point.",
            "warnings": [
                "At frame 1, constraints or parenting produced distance 9.9403 instead of 6.",
                "At frame 48, constraints or parenting produced distance 9.93738 instead of 2.",
            ],
            "changed_objects": ["Camera_Hero", "Hero"],
            "changed_resources": ["Camera_Hero Data", "HeroAction", "Camera_Hero DataAction"],
        },
        # --- lighting: handlers/lighting/ ------------------------------------------------
        "create_light": lambda _scale: {
            "scene": "Scene",
            "collection": "Lights",
            "object": "Bounce",
            "light_data": "Bounce Light",
            "light_type": "AREA",
            "transform": _transform(local_matrix=False, decimals=_LIGHT_DECIMALS),
            "settings": dict(_LIGHT_SETTINGS),
            "scene_unit_scale": 1.0,
            "changed_objects": ["Bounce"],
            "changed_resources": ["Bounce Light"],
        },
        # `handlers/lighting/construction.py configure_light`: one unshared light, so its only
        # data user is itself, counted the way `helpers.counted_page` counts objects.
        "configure_light": lambda _scale: {
            "object": "Key",
            "light_data": "Key Light",
            "light_type": "AREA",
            "old": {"energy": 1000.0},
            "new": {"energy": 800.0},
            "effective": dict(_LIGHT_SETTINGS),
            "data_users": {
                "total": 1,
                "by_type": {"LIGHT": 1},
                "limit": _MAX_LISTED_NAMES,
                "returned_count": 1,
                "truncated": False,
                "names": ["Key"],
            },
            "warnings": [],
            "changed_objects": ["Key"],
            "changed_resources": ["Key Light"],
        },
        # `handlers/lighting/inspection.py:227 inspect_light`.
        "inspect_light": lambda _scale: {"scene": "Scene", **_light_snapshot(0, include_nodes=True)},
        # `handlers/lighting/inspection.py:204 list_lights`.
        "list_lights": lambda scale: {
            "scene": "Scene",
            "collection": None,
            "light_type_filter": None,
            "detail": False,
            "lights": [_light_summary(index) for index in range(min(scale.lights, _LIGHT_PAGE))],
            "total": scale.lights,
            "offset": 0,
            "limit": _LIGHT_PAGE,
            "returned_count": min(scale.lights, _LIGHT_PAGE),
            "truncated": scale.lights > _LIGHT_PAGE,
            "next_offset": None,
            "scene_unit_scale": 1.0,
        },
        # `handlers/lighting/inspection.py:233 inspect_lighting_setup`.
        "inspect_lighting_setup": lambda scale: {
            "scene": "Scene",
            "render_engine": "CYCLES",
            "available_engines": {"CYCLES": "Cycles", "BLENDER_EEVEE": "EEVEE"},
            "units": {"system": "METRIC", "scale_length": 1.0, "length_unit": "METERS"},
            "camera": "Camera_Hero",
            "color_management": {
                "view_transform": "AgX",
                "look": "None",
                "exposure": 0.0,
                "exposure_multiplier": 1.0,
                "gamma": 1.0,
            },
            "world": {
                "name": "World",
                "use_nodes": True,
                "color": [1.0, 1.0, 1.0],
                "node_tree": dict(_MANAGED_WORLD_GRAPH),
            },
            "lights_detail": False,
            "lights": [_light_summary(index) for index in range(min(scale.lights, _LIGHT_PAGE))],
            "total": scale.lights,
            "offset": 0,
            "limit": _LIGHT_PAGE,
            "returned_count": min(scale.lights, _LIGHT_PAGE),
            "truncated": scale.lights > _LIGHT_PAGE,
            "next_offset": None,
            "emissive_materials": [{"material": "Practical_Glow", "objects": ["set_012"]}],
            "volume_materials": [],
            "material_scan_truncated": False,
            "eevee_probes": [],
            "excluded_collections": [],
            "quality": dict(_QUALITY_SNAPSHOT),
        },
        "validate_lighting_setup": lambda _scale: {
            "scene": "Scene",
            "target_engine": "BOTH",
            "valid": False,
            "summary": {"ERROR": 1, "WARNING": 2, "INFO": 1},
            # `handlers/lighting/inspection.py:188 _finding`.
            "findings": [
                {
                    "severity": "WARNING",
                    "code": "SHADOWS_DISABLED",
                    "resource": f"Light_{index:03d}",
                    "message": "This light does not cast shadows.",
                    "evidence": {"use_shadow": False},
                    "remediation": "Enable shadows unless shadowless fill is intentional.",
                }
                for index in range(4)
            ],
            "total": 4,
            "offset": 0,
            "limit": 100,
            "returned_count": 4,
            "truncated": False,
            "next_offset": None,
        },
        # `handlers/lighting/construction.py:218 aim_light`.
        "aim_light": lambda _scale: {
            "light": "Key",
            "method": "STATIC_ROTATION",
            "target": _floats(3, 7, decimals=_LIGHT_DECIMALS),
            "world_direction": _floats(3, 2, decimals=_LIGHT_DECIMALS),
            "constraint": None,
            "transform": _transform(local_matrix=False, decimals=_LIGHT_DECIMALS),
            "changed_objects": ["Key"],
        },
        "configure_light_linking": lambda _scale: {
            "light": "Key",
            "before": {"supported": True, "receiver": None, "blocker": None},
            "after": {
                "supported": True,
                "receiver": {"collection": "Receivers", "objects": [], "total": 0, "truncated": False},
                "blocker": None,
            },
            "engine_support": ["CYCLES", "EEVEE"],
            "warnings": ["Render matched engine previews because linked-shadow behavior can differ by engine."],
            "changed_objects": ["Key"],
        },
        # `handlers/lighting/construction.py:384 create_studio_lighting`.
        "create_studio_lighting": lambda _scale: {
            "scene": "Scene",
            "rig_name": "Hero",
            "target_object": "Hero",
            "camera": "Camera_Hero",
            "mood": "SOFT",
            "key_ratio": 2.0,
            "collection": "Studio Lighting",
            "lights": [
                {
                    "role": role,
                    "object": f"Hero {role.title()}",
                    "light_data": f"Hero {role.title()} Light",
                    "energy": 3307.5,
                    "transform": _transform(index, local_matrix=False, decimals=_LIGHT_DECIMALS),
                }
                for index, role in enumerate(("key", "fill", "rim"))
            ],
            "changed_objects": ["Hero Key", "Hero Fill", "Hero Rim"],
            "changed_resources": ["Hero Key Light", "Hero Fill Light", "Hero Rim Light"],
        },
        "configure_world_background": lambda _scale: _world_result("BACKGROUND", {}),
        "configure_procedural_sky": lambda _scale: _world_result(
            "PROCEDURAL_SKY",
            {
                "target_engine": "BOTH",
                "sky": {"sun_elevation": 0.6000000238418579, "sky_type": "MULTIPLE_SCATTERING"},
                "background_strength": 1.0,
                "synchronized_sun": None,
            },
        ),
        "configure_hdri_environment": lambda _scale: _world_result(
            "HDRI",
            {
                "image": "studio_probe",
                "image_path": "/shots/hero/hdri/studio.exr",
                "image_size_bytes": 3746,
                "color_space": "sRGB",
                "projection": "EQUIRECTANGULAR",
                "rotation_radians": 0.0,
                "strength": 1.0,
            },
        ),
        "configure_color_management": lambda _scale: {
            "scene": "Scene",
            "before": {
                "view_transform": "Standard",
                "look": "None",
                "exposure": 0.0,
                "exposure_multiplier": 1.0,
                "gamma": 1.0,
            },
            "after": {
                "view_transform": "AgX",
                "look": "None",
                "exposure": 0.0,
                "exposure_multiplier": 1.0,
                "gamma": 1.0,
            },
            "exposure_multiplier": 1.0,
            "changed_resources": ["Scene"],
        },
        # `handlers/lighting/rendering.py:203 configure_lighting_quality`: the default reply.
        # `detail=True` returns the two `_QUALITY_SNAPSHOT` blocks instead
        # (`handlers/lighting/rendering.py:193`), which is the shape this entry measured before.
        "configure_lighting_quality": lambda _scale: {
            "scene": "Scene",
            "target_engine": "EEVEE",
            "preset": "FINAL",
            "changed": [
                "eevee.render_samples",
                "eevee.shadow_ray_count",
                "eevee.shadow_step_count",
                "eevee.volumetric_samples",
            ],
            "after": {
                "eevee.render_samples": 128,
                "eevee.shadow_ray_count": 4,
                "eevee.shadow_step_count": 16,
                "eevee.volumetric_samples": 128,
            },
            "changed_resources": ["Scene"],
        },
        # `handlers/lighting/rendering.py:265 render_lighting_preview`.
        "render_lighting_preview": lambda scale: {
            "scene": "Scene",
            "camera": "Camera_Hero",
            "frame": 1,
            "width": 512,
            "height": 512,
            "outputs": [
                {
                    "engine": "EEVEE",
                    "runtime_engine": "BLENDER_EEVEE",
                    "path": "/shots/hero/preview/eevee.png",
                    "size_bytes": 148_213,
                    "samples": 32,
                }
            ],
            # `handlers/lighting/rendering.py:129 _matched_state`.
            "matched_state": {
                "world": "World",
                "exposure": 0.0,
                "view_transform": "AgX",
                "lights": [f"Light_{index:03d}" for index in range(scale.lights)],
                "light_count": scale.lights,
            },
            "warnings": [],
            "changed_objects": [],
            "changed_resources": [],
        },
        # --- character posing: handlers/character_rigging/posing.py ---------------------
        # `handlers/character_rigging/inspection.py sample_deformed_geometry`, the domain's only
        # evaluated readback: one page of vertices, each a world position, a normal and the
        # distance it sits from the rest surface.
        "sample_deformed_geometry": lambda scale: {
            "object": "Hero",
            "frame": 24,
            "evaluated_deformation_included": True,
            "coordinate_space": "WORLD",
            "evaluated_counts": {"vertices": scale.mesh_vertices, "edges": scale.mesh_vertices, "faces": 0},
            "base_counts": {"vertices": scale.mesh_vertices, "edges": scale.mesh_vertices, "faces": 0},
            "index_correspondence": "BASE_MESH",
            "world_bounds": {
                "coordinate_space": "WORLD",
                "minimum": _floats(3, 1),
                "maximum": _floats(3, 4),
            },
            "displacement": {
                "measured": True,
                "coordinate_space": "WORLD",
                "sampled_vertices": scale.mesh_vertices,
                "complete": True,
                "maximum_m": 0.418273,
                "mean_m": 0.092841,
                "moved_vertices": scale.mesh_vertices,
                "moved_epsilon_m": 1e-06,
            },
            "vertices": {
                "items": [
                    {
                        "index": index,
                        "co": _floats(3, index),
                        "normal": _floats(3, index + 3),
                        "displacement_m": 0.031784,
                    }
                    for index in range(min(scale.mesh_vertices, _DEFORMED_VERTEX_PAGE))
                ],
                "total": scale.mesh_vertices,
                "offset": 0,
                "limit": _DEFORMED_VERTEX_PAGE,
                "truncated": scale.mesh_vertices > _DEFORMED_VERTEX_PAGE,
                "next_offset": _DEFORMED_VERTEX_PAGE if scale.mesh_vertices > _DEFORMED_VERTEX_PAGE else None,
            },
            "limitations": list(_DEFORMED_SAMPLE_LIMITATIONS),
        },
        # `handlers/character_rigging/posing.py:201 list_character_bones`, paged at 200.
        "list_character_bones": lambda scale: {
            "armature_object": "Hero_Rig",
            "bones": {
                "items": [
                    {"name": _bone_name(index), "parent": _bone_name(index - 1) if index else None, "deform": True}
                    for index in range(min(scale.bones, _BONE_PAGE))
                ],
                "total": scale.bones,
                "offset": 0,
                "limit": _BONE_PAGE,
                "truncated": scale.bones > _BONE_PAGE,
                "next_offset": _BONE_PAGE if scale.bones > _BONE_PAGE else None,
            },
        },
        # `handlers/character_rigging/posing.py probe_bone_axis`, which pages nothing: the six
        # signed axes are the whole basis, so the largest reply this tool can send is one entry
        # per axis, each carrying two world points, a travel vector and one component per named
        # reference direction. Bounded by the handler at six axes and six directions.
        "probe_bone_axis": lambda _scale: {
            "armature_object": "Hero_Rig",
            "bone": _bone_name(3),
            "space": "LOCAL",
            "degrees": 15.0,
            "bone_length_m": 0.284531,
            "witness_bone": _bone_name(9),
            "witness_bone_position": "TAIL",
            "witness_bone_source": "farthest_descendant",
            "axes": [
                {
                    "axis": axis,
                    "degrees": 15.0,
                    "witness_before_world": [0.418273, -1.203847, 1.472910],
                    "witness_after_world": [0.463812, -1.174535, 1.469238],
                    "travel_world": [0.045539, 0.029312, -0.003672],
                    "travel_m": 0.054294,
                    "reference_components_m": dict.fromkeys(_PROBE_REFERENCE_NAMES, 0.031784),
                }
                for axis in ("X", "-X", "Y", "-Y", "Z", "-Z")
            ],
        },
        # `handlers/character_rigging/posing.py:228 set_character_pose`, which pages no bones:
        # the reply budget shortens `bones`, and `changed_bones` is what stays complete.
        "set_character_pose": lambda scale: {
            "armature_object": "Hero_Rig",
            "space": "LOCAL",
            "changed_bones": [_bone_name(index) for index in range(scale.bones)],
            "bones": _bone_pose_entries(scale.bones),
            "changed_objects": ["Hero_Rig"],
        },
        # `handlers/character_rigging/posing.py:271 keyframe_character_pose`; its per-bone pose
        # records are `detail=True` only, so the default reply carries the keys and the names.
        "keyframe_character_pose": lambda scale: {
            "armature_object": "Hero_Rig",
            "action": "Hero_Action",
            "action_slot": "OBHero_Rig",
            "keying_policy": "INSERT",
            "changed_bones": [_bone_name(index) for index in range(scale.bones)],
            "changed_keys": [
                {"bone": _bone_name(index), "data_path": path, "frame": 1.0}
                for index in range(scale.bones)
                for path in ("location", "rotation_quaternion", "scale")
            ],
            "interpolation_updates": 230,
            "changed_objects": ["Hero_Rig"],
            "changed_resources": [{"type": "ACTION", "name": "Hero_Action"}],
        },
        # `handlers/character_rigging/reach.py solve_bone_reach`; per reach it adds the solver's
        # own report to the same per-bone records set_character_pose returns for that chain.
        "solve_bone_reach": lambda scale: {
            "armature_object": "Hero_Rig",
            "tolerance_m": 0.0001,
            "changed_bones": [_bone_name(index) for index in range(scale.bones)],
            "reaches": _reach_records(scale.bones),
            "changed_objects": ["Hero_Rig"],
        },
        # `handlers/character_rigging/reach.py keyframe_bone_reach`; one call keys both feet
        # across the whole cycle, so its reply is per frame where solve_bone_reach's is per pose.
        "keyframe_bone_reach": lambda scale: {
            "armature_object": "Hero_Rig",
            "action": "Hero_Action",
            "action_slot": "OBHero_Rig",
            "assigned_action": "Hero_Action",
            "tolerance_m": 0.0001,
            "keying_policy": "REPLACE",
            "changed_bones": [f"{bone}.{side}" for side in ("L", "R") for bone in ("foot", "shin", "thigh")],
            "keyed_frames": [float(frame) for frame in range(1, scale.cycle_frames + 1)],
            "changed_keys": [
                {"bone": f"{bone}.{side}", "data_path": path, "frame": float(frame)}
                for frame in range(1, scale.cycle_frames + 1)
                for side in ("L", "R")
                for bone in ("foot", "shin", "thigh")
                for path in ("location", "rotation_quaternion", "scale")
            ],
            "interpolation_updates": 1_008,
            "reaches": _keyed_reach_records(scale.cycle_frames),
            "changed_objects": ["Hero_Rig"],
            "changed_resources": [{"type": "ACTION", "name": "Hero_Action"}],
            "warnings": [],
        },
        # --- animation: handlers/animation.py, handlers/object_animation.py -------------
        "inspect_animation": lambda _scale: {
            "target": {"type": "OBJECT", "name": "Camera_Hero"},
            "action": {
                "name": "Camera_Hero DataAction",
                "users": 2,
                "is_layered": True,
                "slot": "OBCamera_Hero",
                "slots": [
                    {"identifier": "CACamera_Hero Data", "target_id_type": "CAMERA", "name": "Camera_Hero Data"},
                    {"identifier": "OBCamera_Hero", "target_id_type": "OBJECT", "name": "Camera_Hero"},
                ],
                "frame_range": [1.0, 48.0],
            },
            "keyframes": [
                {
                    "data_path": "location",
                    "array_index": index,
                    "frame": 1.0,
                    "value": 7.0,
                    "interpolation": "BEZIER",
                    "group": "Object Transforms",
                }
                for index in range(3)
            ],
            "total_keyframes": 3,
            "offset": 0,
            "limit": 200,
            "truncated": False,
            "next_offset": None,
            "drivers": [],
            "nla_tracks": [],
        },
        "manage_animation_action": lambda _scale: {
            "target": "Hero",
            "action": "HeroAction",
            "slot": "OBHero",
            "created": True,
            "changed_resources": ["Hero", "HeroAction"],
        },
        # `handlers/animation.py set_action_cycle`; one record per curve made cyclic, which for
        # a walk is every channel of every keyed bone.
        "set_action_cycle": lambda scale: {
            "action": "Hero_Action",
            "action_slot": "OBHero_Rig",
            "operation": "SET",
            "curve_count": scale.bones * 10,
            "modifiers": [
                {
                    "data_path": f'pose.bones["{_bone_name(index)}"].{path}',
                    "array_index": array_index,
                    "mode_before": "REPEAT_OFFSET",
                    "mode_after": "REPEAT_OFFSET",
                    "first_key_frame": 1.0,
                    "last_key_frame": 25.0,
                    "period_frames": 24.0,
                }
                for index in range(scale.bones)
                for path, width in (("location", 3), ("rotation_quaternion", 4), ("scale", 3))
                for array_index in range(width)
            ],
            # The disagreement notice, at the length it reaches when two limbs were keyed over
            # different extents: it is what tells the caller the arms stopped striding.
            "warnings": [
                'These curves do not share one cycle period: 24 frames (e.g. pose.bones["thigh.L"].location, '
                'keys 1-25), 161 frames (e.g. pose.bones["upper_arm.L"].location, keys 1-162). A Cycles '
                "modifier repeats its own curve's key extent, so they loop at different rates and drift apart "
                "instead of repeating together. Key them over the same frame range, or scope this call with "
                "data_path_prefix."
            ],
            "changed_resources": ["Hero_Action"],
        },
        # `handlers/scene.py set_scene_frame`; fixed-size, and called once per reviewed frame.
        "set_scene_frame": lambda _scale: {
            "scene": "Scene",
            "frame": 12,
            "subframe": 0.0,
            "frame_start": 1,
            "frame_end": 48,
            "fps": 24.0,
        },
        "manage_animation_driver": lambda _scale: {
            "target": "Hero",
            "data_path": "location",
            "array_index": 0,
            "driver_type": "SCRIPTED",
            "expression": "1.5",
            "variables": [],
            "muted": False,
            "changed_resources": ["Hero"],
        },
        "manage_nla_tracks": lambda _scale: {
            "target": "Hero_Rig",
            "track": "Base",
            "created": True,
            "changed_resources": ["Hero_Rig"],
        },
        "edit_keyframes": lambda _scale: {
            "target": "Hero",
            "action": "HeroAction",
            "slot": "OBHero",
            "changed_keyframes": [
                {"operation": "UPSERT", "data_path": "location", "array_index": 0, "frame": 1.0, "value": 1.0}
            ],
            # `handlers/animation.py _edit_cycle_warnings`: a batch keying channels outside the
            # cycle they already repeat carries the same quantitative notice the two pose paths
            # do. Warnings are lifted whole into the envelope and never paged, so the list is
            # bounded at `_MAX_CYCLE_WARNINGS` with one counted line - this is that ceiling.
            "warnings": [
                f'pose.bones["{_bone_name(index)}"].location[0] is keyed at frame 199, outside the frames 1-17 it '
                "already cycles over. A Cycles modifier repeats its own curve's key extent, so this channel's "
                "period becomes 198 frames instead of 16; under REPEAT_OFFSET each repeat then carries that much "
                "further, so a travelling root stops arriving where the cycle put it. Key it inside the cycle, or "
                "re-cycle the action deliberately."
                for index in range(4)
            ]
            + [
                "12 further channel(s) are keyed outside the cycle their own curves carry, stretching it the same "
                "way: "
                + ", ".join(f'pose.bones["{_bone_name(index)}"].location[0]' for index in range(4, 8))
                + " and 8 more."
            ],
            "changed_resources": ["Hero", "HeroAction"],
        },
        "keyframe_object_transform": lambda _scale: {
            "keyframes": [{"data_path": "location", "array_index": index, "frame": 1.0} for index in range(3)],
            "actions": ["HeroAction"],
            "policy": "REPLACE_EXISTING",
            "changed_objects": ["Hero"],
            "changed_resources": ["HeroAction"],
        },
        "bake_evaluated_animation": lambda _scale: {
            "object": "Hero",
            "action": "Evaluated Bake",
            "slot": "OBHero",
            "frame_range": [1, 48, 1],
            "sampled_key_count": 144,
            "key_count": 144,
            "curves": [
                {
                    "data_path": "location",
                    "array_index": index,
                    "sample_count": 48,
                    "key_count": 48,
                    "max_reconstruction_error": 0.0,
                }
                for index in range(3)
            ],
            "max_reconstruction_error": 0.0,
            "sample_space": "LOCAL",
            "new_non_shared_action": True,
            "warnings": [
                "Constraints remain live; mute or remove them before using baked transforms as final "
                "unconstrained motion."
            ],
            "changed_objects": ["Hero"],
            "changed_resources": ["Evaluated Bake"],
        },
        # --- rendering: handlers/rendering.py -------------------------------------------
        "inspect_render_setup": lambda _scale: {
            **_render_info("CYCLES", 256),
            "color_management": {
                "view_transform": "AgX",
                "look": "None",
                "exposure": 0.0,
                "exposure_multiplier": 1.0,
                "gamma": 1.0,
            },
            "compositor": {"use_nodes": True, "node_tree": None, "node_count": 0, "link_count": 0},
        },
        # `handlers/rendering.py:542 configure_render_settings`: the default reply carries only
        # the patched property paths and their resulting values. `detail=True` returns the two
        # full `_render_info(scene)` blocks instead (`handlers/rendering.py:534`).
        "configure_render_settings": lambda _scale: {
            "scene": "Scene",
            "changed": ["cycles_samples", "engine", "resolution_x", "resolution_y"],
            "after": {"engine": "CYCLES", "resolution_x": 1920, "resolution_y": 1080, "cycles_samples": 256},
            "changed_resources": ["Scene"],
        },
        "manage_view_layers": lambda _scale: {
            "view_layer": {**_VIEW_LAYER_INFO, "name": "Beauty"},
            "changed_resources": ["Beauty"],
        },
        # The default reply summarises: `files`/`progress` are `detail=true` only, so this shape
        # no longer grows with the frame count.
        "render_scene": lambda _scale: {
            "scene": "Scene",
            "mode": "ANIMATION",
            "filepath": "/shots/hero/render/shot_",
            "frame": 1,
            "frame_count": 120,
            "operator_result": ["FINISHED"],
            "settings_restored": True,
            "status": "COMPLETED",
            "cancelled": False,
            "cancellation_reason": None,
            "duration_seconds": 41.19302070798585,
            "render_slot_policy": "USE_ACTIVE",
            "output_persisted": False,
            "first_file": "/shots/hero/render/shot_0001.png",
            "last_file": "/shots/hero/render/shot_0120.png",
            "bytes_written": 301_688_520,
            "passes": [
                {"layer": "ViewLayer", "pass": name, "setting": f"use_pass_{name.lower()}"}
                for name in ("Combined", "Z", "Normal", "Cryptomatte")
            ],
            "pass_verification": "VIEW_LAYER_CONFIGURATION",
        },
        # `handlers/render_jobs.py _read`: a job mid-animation, without `detail`.
        "manage_render_job": lambda _scale: {
            "job_id": "3f9a1c07b2e4",
            "state": "RENDERING",
            "scene_name": "Scene",
            "mode": "ANIMATION",
            "view_layer_name": None,
            "filepath": "/shots/hero/render/shot_",
            "frame_start": 1,
            "frame_end": 120,
            "frame_step": 1,
            "frames_total": 120,
            "frames_done": 41,
            "current_frame": 42,
            "last_file": "/shots/hero/render/shot_0041.png",
            "bytes_written": 103_082_476,
            "max_duration_seconds": 3600.0,
            "created_at": 1790283621.418342,
            "started_at": 1790283622.906157,
            "finished_at": None,
            "deadline_at": 1790287221.418342,
            "pid": 48213,
            "exit_code": None,
            "engine": "CYCLES",
            "effective_cycles_device": "GPU",
            "blend_copy": "/shots/hero/render/blender_mcp_render_jobs/3f9a1c07b2e4/scene.blend",
            "log_path": "/shots/hero/render/blender_mcp_render_jobs/3f9a1c07b2e4/render.log",
            "error": None,
            "cancellation_reason": None,
            "elapsed_seconds": 1412.338,
            "heartbeat_age_seconds": 0.412,
        },
        "inspect_render_output": lambda _scale: {
            "success": True,
            "width": 1000,
            "height": 562,
            "native_width": 1920,
            "native_height": 1080,
            "filepath": "/tmp/blender_mcp_render_output_copy.png",
            "source": "output_path",
            "source_path": "/shots/hero/render/shot_0001.png",
            "frame": 1,
        },
    }


_PAYLOADS: Mapping[str, Callable[[SceneScale], object]] = MappingProxyType(_payloads())

# Arguments for one representative call of every tool `shot` registers. Chosen to reach
# dispatch: a tool that refuses an empty patch or an ambiguous orientation gets a real one.
_ARGUMENTS: Mapping[str, Mapping[str, object]] = MappingProxyType(
    {
        "add_camera_constraint": {
            "scene_name": "Scene",
            "owner_name": "Camera_Hero",
            "constraint_name": "Track To",
            "constraint_type": "TRACK_TO",
            "target_name": "Hero",
        },
        "add_camera_shake": {
            "scene_name": "Scene",
            "camera_name": "Camera_Hero",
            "collection_name": "Cameras",
            "control_name": "Camera_Hero_shake",
            "frame_start": 1,
            "frame_end": 48,
        },
        "aim_light": {"scene_name": "Scene", "light_name": "Key", "target_object_name": "Hero"},
        "bake_evaluated_animation": {
            "target": {"object_name": "Hero", "transforms": ["LOCATION"]},
            "frame_start": 1,
            "frame_end": 48,
            "confirm_bake": True,
        },
        "configure_camera": {"camera_name": "Camera_Hero", "optics": {"lens": 85.0}},
        "configure_camera_dof": {
            "scene_name": "Scene",
            "camera_name": "Camera_Hero",
            "patch": {"use_dof": True, "aperture_fstop": 2.8},
        },
        "configure_camera_render_gate": {
            "scene_name": "Scene",
            "camera_name": "Camera_Hero",
            "guides": {"show_composition_thirds": True},
        },
        "configure_color_management": {"scene_name": "Scene", "view_transform": "AgX"},
        "configure_hdri_environment": {"scene_name": "Scene", "image_path": "/shots/hero/hdri/studio.exr"},
        "configure_light": {"light_name": "Key", "patch": {"energy": 800.0}},
        "configure_light_linking": {
            "scene_name": "Scene",
            "light_name": "Key",
            "receiver_collection_name": "Receivers",
        },
        "configure_lighting_quality": {"scene_name": "Scene", "target_engine": "EEVEE", "preset": "FINAL"},
        "configure_procedural_sky": {"scene_name": "Scene", "settings": {"sun_elevation": 0.6}},
        "configure_render_settings": {
            "scene_name": "Scene",
            "patch": {"engine": "CYCLES", "resolution_x": 1920, "resolution_y": 1080, "cycles_samples": 256},
        },
        "configure_world_background": {"scene_name": "Scene", "color": [0.05, 0.05, 0.06]},
        "create_camera": {"scene_name": "Scene", "collection_name": "Cameras", "name": "Camera_Hero"},
        "create_camera_markers": {"scene_name": "Scene", "action": "LIST"},
        "create_camera_target": {
            "scene_name": "Scene",
            "collection_name": "Cameras",
            "name": "Camera_Hero_target",
            "location": [0.0, 0.0, 1.0],
        },
        "create_dolly_zoom": {
            "scene_name": "Scene",
            "camera_name": "Camera_Hero",
            "movement_object_name": "Hero",
            "start_frame": 1,
            "end_frame": 48,
            "start_distance": 6.0,
            "end_distance": 2.0,
            "subject_point": [0.0, 0.0, 1.0],
        },
        "create_focus_pull": {
            "scene_name": "Scene",
            "camera_name": "Camera_Hero",
            "start_frame": 1,
            "end_frame": 48,
            "start_point": [0.0, 0.0, 1.0],
            "end_point": [2.0, 0.5, 0.0],
        },
        "create_light": {
            "scene_name": "Scene",
            "collection_name": "Lights",
            "name": "Bounce",
            "light_type": "AREA",
            "location": [3.0, -4.0, 2.5],
        },
        "create_override": {"collection_uid": 978},
        "create_studio_lighting": {
            "scene_name": "Scene",
            "target_object_name": "Hero",
            "camera_name": "Camera_Hero",
            "frame": 1,
        },
        "duplicate_or_instance_objects": {"source_object_name": "Hero", "names": ["Hero_002"]},
        "edit_keyframes": {
            "target": {"type": "OBJECT", "name": "Hero"},
            "edits": [{"data_path": "location", "array_index": 0, "frame": 1.0, "value": 1.0}],
        },
        "frame_camera_on_objects": {
            "scene_name": "Scene",
            "camera_name": "Camera_Hero",
            "object_names": ["Hero", "set_000"],
        },
        "get_addon_status": {},
        "get_camera_rig_info": {"scene_name": "Scene", "object_name": "Camera_Hero"},
        "get_integration_status": {},
        "get_mesh_data": {"object_name": "Hero"},
        "get_object_info": {"object_name": "Hero"},
        "get_session_info": {},
        "get_viewport_screenshot": {},
        "inspect_animation": {"target": {"type": "OBJECT", "name": "Camera_Hero"}},
        "inspect_delivery": {"scene_name": "Scene"},
        "inspect_light": {"scene_name": "Scene", "light_name": "Key"},
        "inspect_lighting_setup": {"scene_name": "Scene"},
        "inspect_render_output": {"output_path": "/shots/hero/render/shot_0001.png"},
        "inspect_render_setup": {},
        "keyframe_camera_rig": {
            "keyframes": [
                {"object_name": "Camera_Hero", "data_path": "location", "value": [7.0, -7.0, 5.0], "frame": 1}
            ]
        },
        "keyframe_character_pose": {
            "armature_object_name": "Hero_Rig",
            "action_name": "Hero_Action",
            "frame": 1.0,
            "poses": [{"bone_name": "spine", "location": [0.0, 0.0, 0.0]}],
        },
        "keyframe_object_transform": {"keyframes": [{"object_name": "Hero", "frame": 1, "location": [0.0, 0.0, 0.0]}]},
        "link_canon_library": {"filepath": "/shots/canon/canon.blend"},
        "list_character_bones": {"armature_object_name": "Hero_Rig"},
        "sample_deformed_geometry": {"mesh_object_name": "Hero"},
        "list_libraries": {},
        "list_lights": {"scene_name": "Scene"},
        "list_scene_objects": {},
        "manage_animation_action": {
            "target": {"type": "OBJECT", "name": "Hero"},
            "action": "CREATE",
            "action_name": "HeroAction",
        },
        "manage_animation_driver": {
            "target": {"type": "OBJECT", "name": "Hero"},
            "action": "ADD",
            "data_path": "location",
            "driver_type": "SCRIPTED",
            "expression": "1.5",
        },
        "manage_modifiers": {
            "object_name": "Hero",
            "action": "ADD",
            "modifier": {"type": "SUBSURF", "name": "Subdivision"},
        },
        "manage_nla_tracks": {
            "target": {"type": "OBJECT", "name": "Hero_Rig"},
            "action": "CREATE_TRACK",
            "track_name": "Base",
        },
        "manage_object_constraints": {
            "object_name": "set_000",
            "action": "ADD",
            "constraint": {"name": "Copy Location", "type": "COPY_LOCATION", "target_object_name": "Hero"},
        },
        "manage_object_hierarchy": {"assignments": [{"child_object_name": "set_001", "parent_object_name": "Hero"}]},
        "manage_render_job": {"action": "READ", "job_id": "3f9a1c07b2e4"},
        "manage_scene_collections": {"action": "CREATE", "collection_name": "Receivers"},
        "manage_view_layers": {"scene_name": "Scene", "action": "CREATE", "view_layer_name": "Beauty"},
        "open_shot": {"filepath": "/shots/hero/shot.blend"},
        "point_camera_at": {"scene_name": "Scene", "camera_name": "Camera_Hero", "target_object_name": "Hero"},
        "probe_bone_axis": {"armature_object_name": "Hero_Rig", "bone_name": "spine", "axes": ["X", "Z"]},
        "reload_library": {"library_uid": 977},
        "relocate_library": {"library_uid": 977, "filepath": "/shots/canon/canon_v2.blend"},
        "remove_scene_objects": {"object_names": ["Scratch_Proxy"], "confirm_remove": True},
        "render_lighting_preview": {
            "scene_name": "Scene",
            "camera_name": "Camera_Hero",
            "frame": 1,
            "target_engine": "EEVEE",
        },
        "render_scene": {
            "scene_name": "Scene",
            "filepath": "/shots/hero/render/shot_0001.png",
            "confirm_render": True,
        },
        "reset_session": {"confirm_reset": True},
        "save_shot": {},
        "set_camera_interpolation": {
            "object_name": "Camera_Hero",
            "owner": "OBJECT",
            "data_path": "location",
            "frame_start": 1,
            "frame_end": 48,
        },
        "set_character_pose": {
            "armature_object_name": "Hero_Rig",
            "poses": [{"bone_name": "spine", "location": [0.0, 0.0, 0.0]}],
        },
        "set_object_transform": {"object_name": "Hero", "patch": {"location": [0.0, 0.0, 0.0]}},
        "set_object_visibility": {"object_name": "Scratch_Proxy", "hide_render": True, "hide_viewport": True},
        "set_scene_camera": {"scene_name": "Scene", "camera_name": "Camera_Hero"},
        "set_viewport_overlay": {"toggle": "CAVITY", "enabled": True},
        "solve_bone_reach": {
            "armature_object_name": "Hero_Rig",
            "reaches": [{"tip_bone": "hand.L", "target_point": [0.6, -0.1, 1.1]}],
        },
        "keyframe_bone_reach": {
            "armature_object_name": "Hero_Rig",
            "action_name": "Hero_Action",
            "reaches": [
                {
                    "tip_bone": "foot.L",
                    "keys": [{"frame": float(frame), "target_point": [0.1, -0.2, 0.0]} for frame in range(1, 25)],
                    "pole_target_point": [0.1, -0.8, 0.4],
                    "hinge": {"bone_name": "shin.L", "axis": "X", "min_degrees": 0.0, "max_degrees": 150.0},
                }
            ],
        },
        "set_action_cycle": {
            "target": {"type": "OBJECT", "name": "Hero_Rig"},
            "action_name": "Hero_Action",
        },
        "set_scene_frame": {"frame": 12},
        "unlink_libraries": {"library_uids": [977], "confirm_unlink": True},
        "validate_camera_rig": {"scene_name": "Scene"},
        "validate_lighting_setup": {"scene_name": "Scene"},
        "validate_scene": {"scene_name": "Scene"},
    }
)

# What this script does not put a number on, and why. Reported, never silently skipped.
_UNMEASURED: Mapping[str, str] = MappingProxyType(
    {
        "get_viewport_screenshot": (
            "image content: the reply carries a base64 PNG of the live viewport, sized by the "
            "captured pixels rather than by any handler field, and capturing one needs a GUI Blender. "
            "Its envelope item is measured below."
        ),
        "inspect_render_output": (
            "image content: the reply carries a base64 PNG of the rendered frame, sized by the frame. "
            "Its envelope item is measured below."
        ),
        "render_lighting_preview": (
            "image content: one base64 PNG per previewed engine, sized by the render. "
            "Its envelope item is measured below."
        ),
        "create_studio_lighting": (
            "image content: the preview PNG it renders through render_lighting_preview. "
            "Both of its envelope items are measured below."
        ),
    }
)


@dataclass(frozen=True)
class ReplyReport:
    """
    One tool's measured reply.

    Attributes:
        tool: The tool name.
        wire_bytes: UTF-8 bytes of the reply's text items, as FastMCP serializes them
            (indented JSON), excluding any image item.
        envelope_bytes: The same envelopes re-encoded compactly, for comparison with
            `catalog_metrics`, which measures the advertised payload the same way.
        data_bytes: Compact bytes each top-level `data` key contributes, summing with two
            braces and the separating commas to the compact size of the `data` block.
        envelopes: The reply's envelopes as parsed back from its text items, so a candidate
            rule can be applied to them and re-measured rather than estimated.
        image_items: Image items in the reply, whose bytes are excluded everywhere.

    """

    tool: str
    wire_bytes: int
    envelope_bytes: int
    data_bytes: Mapping[str, int] = field(default_factory=dict)
    envelopes: tuple[object, ...] = ()
    image_items: int = 0


def _compact_bytes(value: object) -> int:
    """
    Measure one JSON value compactly, as `catalog_metrics._json_bytes` measures the catalog.

    Args:
        value: Any JSON-serializable value.

    Returns:
        int: Bytes of its compact encoding.

    """
    return len(to_json(value, fallback=str))


def _indented_bytes(value: object) -> int:
    """
    Measure one JSON value exactly as FastMCP serializes a tool's reply.

    `func_metadata.py:558` encodes every reply with `pydantic_core.to_json(result,
    fallback=str, indent=2)`, so this calls the same encoder: `json.dumps` disagrees with it
    on non-ASCII text and on exponent notation, and a candidate rule priced with the wrong
    encoder would be priced in bytes nobody sends.

    Args:
        value: Any JSON-serializable value.

    Returns:
        int: UTF-8 bytes of the reply text FastMCP would send.

    """
    return len(to_json(value, fallback=str, indent=2))


def _data_key_bytes(data: object) -> dict[str, int]:
    """
    Split a reply's `data` block into the bytes each top-level key contributes.

    Args:
        data: The envelope's `data` value.

    Returns:
        dict[str, int]: Key to its `"key":value` compact byte count, biggest first. Empty
        when `data` is not an object, because there are then no keys to attribute.

    """
    if not isinstance(data, dict):
        return {}
    sized = {str(key): _compact_bytes({key: value}) - 2 for key, value in data.items()}
    return dict(sorted(sized.items(), key=lambda item: -item[1]))


class _StubConnection:
    """
    Stand-in for `BlenderConnection`, answering with the payload the real handler returns.

    The tests stub the same seam - `tests/conftest.py` patches
    `tools/_dispatch.get_blender_connection`, the one module every tool package resolves the
    socket through - so this reuses their mechanism across every module at once.
    """

    def __init__(self, scale: SceneScale) -> None:
        """
        Answer for a shot of the given size.

        Args:
            scale: How big the stubbed scene is.

        """
        self._scale = scale

    def send_command(self, command: str, params: Mapping[str, object] | None = None) -> object:
        """
        Return the payload the add-on's handler for `command` would return.

        A tool that hands a `.png` path to the add-on reads that file back afterwards, so the
        stub writes one wherever the directory really exists - which is the temporary path the
        tool itself just made, never the fictional shot paths in `_ARGUMENTS`. Once a handshake
        has reported a protocol that speaks inline images - measuring `get_addon_status` caches
        one - the tool asks for the bytes in the reply instead, and the stub answers the way
        `bundled/addon/image_reply.finalize_image_reply` does.

        Args:
            command: Add-on command name the tool dispatched.
            params: Parameters it sent.

        Returns:
            object: The stubbed Blender-side payload.

        Raises:
            SystemExit: For a command with no stub payload, so a missing fixture cannot be
                mistaken for a tool that replies with nothing.

        """
        builder = _PAYLOADS.get(command)
        if builder is None:
            raise SystemExit(f"refusing to measure: no stub payload for add-on command {command!r}")
        for value in (params or {}).values():
            for candidate in value.values() if isinstance(value, dict) else [value]:
                if isinstance(candidate, str) and candidate.endswith(".png"):
                    target = Path(candidate)
                    if target.parent.is_dir():
                        target.write_bytes(_ONE_PIXEL_PNG)
        payload = builder(self._scale)
        if (params or {}).get("inline") and isinstance(payload, dict):
            inline = {key: value for key, value in payload.items() if key != "filepath"}
            inline["image_base64"] = base64.b64encode(_ONE_PIXEL_PNG).decode("ascii")
            return inline
        return payload


def _install_stub(scale: SceneScale) -> None:
    """
    Point every registered tool module's `get_blender_connection` at a stub.

    Each tool module binds the function by `from ...connection import get_blender_connection`,
    so the name has to be replaced per module; patching `connection` alone would miss them.

    Args:
        scale: How big the stubbed scene is.

    Raises:
        SystemExit: If no module was patched, which would mean the tools still dispatch to a
            real Blender and every number would be wrong.

    """
    connection = _StubConnection(scale)
    patched = 0
    for name, module in list(sys.modules.items()):
        if name.startswith("blender_mcp.server") and hasattr(module, "get_blender_connection"):
            module.get_blender_connection = lambda: connection  # pyright: ignore[reportAttributeAccessIssue]
            patched += 1
    if not patched:
        raise SystemExit("refusing to measure: no tool module exposes get_blender_connection")


def _import_server(selection: str) -> object:
    """
    Import this repo's `blender_mcp` with the given bundle selection and hand back its app.

    The package registers tools when imported, reading BLENDER_MCP_TOOLSETS then, so the
    variable must be set first. Done in a function so importing this module changes neither
    `sys.path` nor the environment.

    Args:
        selection: The BLENDER_MCP_TOOLSETS value to measure; `""` means core only.

    Returns:
        object: The FastMCP app with that selection's tools registered.

    Raises:
        SystemExit: If called twice in one process, or if `blender_mcp` resolves to an
            installed copy instead of this repo.

    """
    if "blender_mcp" in sys.modules:
        raise SystemExit("refusing to measure: blender_mcp is already imported; run one selection per process")

    sys.path.insert(0, str(_SRC_ROOT))
    os.environ["BLENDER_MCP_TOOLSETS"] = selection

    import blender_mcp  # ruff: ignore[import-outside-top-level] - must follow the environment write above.

    if not Path(blender_mcp.__file__ or "").is_relative_to(_SRC_ROOT):
        raise SystemExit(f"refusing to measure: imported {blender_mcp.__file__}, expected under {_SRC_ROOT}")

    from blender_mcp.server.app import mcp  # ruff: ignore[import-outside-top-level] - same reason.

    return mcp


def _measure_tool(app: object, tool: str) -> ReplyReport:
    """
    Call one tool through the app and measure the reply it produced.

    Args:
        app: The FastMCP app.
        tool: Tool name to call.

    Returns:
        ReplyReport: Its measured reply.

    Raises:
        SystemExit: If the tool has no representative arguments, so an unmeasured tool
            cannot quietly drop out of the total.

    """
    arguments = _ARGUMENTS.get(tool)
    if arguments is None:
        raise SystemExit(f"refusing to measure: no representative arguments for tool {tool!r}")
    items = asyncio.run(app.call_tool(tool, dict(arguments)))  # pyright: ignore[reportAttributeAccessIssue]
    wire = 0
    envelope = 0
    data_bytes: dict[str, int] = {}
    envelopes: list[object] = []
    images = 0
    for item in items:
        text = getattr(item, "text", None)
        if text is None:
            images += 1
            continue
        wire += len(text.encode("utf-8"))
        payload = json.loads(text)
        envelopes.append(payload)
        envelope += _compact_bytes(payload)
        for key, size in _data_key_bytes(payload.get("data") if isinstance(payload, dict) else None).items():
            data_bytes[key] = data_bytes.get(key, 0) + size
    # Candidate rules are priced by re-serializing an edited envelope, so this script's
    # serializer has to agree with FastMCP's byte for byte before those numbers mean anything.
    rebuilt = sum(_indented_bytes(payload) for payload in envelopes)
    if rebuilt != wire:
        raise SystemExit(f"refusing to measure: {tool} re-serializes to {rebuilt} B, FastMCP sent {wire} B")
    return ReplyReport(tool, wire, envelope, data_bytes, tuple(envelopes), images)


def _measure(selection: str, scale: SceneScale) -> tuple[list[ReplyReport], object]:
    """
    Measure every tool the selection registers.

    Args:
        selection: The BLENDER_MCP_TOOLSETS value to measure.
        scale: How big the stubbed shot is.

    Returns:
        tuple[list[ReplyReport], object]: The reports, biggest reply first, and the app, so a
        caller can re-measure at another scale without importing twice.

    """
    app = _import_server(selection)
    _install_stub(scale)
    tools = asyncio.run(app.list_tools())  # pyright: ignore[reportAttributeAccessIssue]
    reports = [_measure_tool(app, tool.name) for tool in tools]
    return sorted(reports, key=lambda report: -report.wire_bytes), app


def _remeasure(app: object, tools: Sequence[str], scale: SceneScale) -> dict[str, int]:
    """
    Re-measure named tools at another scene size, for the growth-per-input figures.

    Args:
        app: The app `_measure` returned.
        tools: Tool names to re-measure.
        scale: The scene size to measure them at.

    Returns:
        dict[str, int]: Tool name to wire bytes at that scale.

    """
    _install_stub(scale)
    return {tool: _measure_tool(app, tool).wire_bytes for tool in tools}


# Which input each growing reply grows with, and a count under the handler's own page cap to
# price one more item at: above the cap an extra item costs nothing but a changed total, which
# says nothing about what the page itself costs per entry.
_GROWTH_AXES: Mapping[str, tuple[str, int, tuple[str, ...]]] = MappingProxyType(
    {
        "bones": ("posed bone", 23, ("set_character_pose", "keyframe_character_pose", "list_character_bones")),
        "linked_datablocks": (
            "linked datablock",
            50,
            ("list_libraries", "reload_library", "relocate_library", "unlink_libraries"),
        ),
        "lights": ("light", 7, ("list_lights", "inspect_lighting_setup", "render_lighting_preview")),
        "mesh_vertices": ("mesh element", 50, ("get_mesh_data", "sample_deformed_geometry")),
        "findings": ("validation finding", 84, ("validate_scene",)),
        "objects": ("scene object", 12, ("list_scene_objects",)),
        "override_objects": ("overridden object", 40, ("create_override", "link_canon_library")),
    }
)


def _print_table(reports: Sequence[ReplyReport]) -> None:
    """
    Print every measured reply, biggest first, with the `data` keys that dominate it.

    Args:
        reports: The reports to print.

    """
    print(f"\n{'tool':<32}{'wire B':>10}{'compact B':>11}  dominant data keys")
    print("-" * 110)
    for report in reports:
        keys = ", ".join(f"{key} {size:,}" for key, size in list(report.data_bytes.items())[:3])
        marker = f" +{report.image_items} img" if report.image_items else ""
        print(f"{report.tool:<32}{report.wire_bytes:>10,}{report.envelope_bytes:>11,}  {keys}{marker}")


# Below this a `data` key is a name and a flag; listing it would bury the keys that matter.
_BREAKDOWN_FLOOR = 32


def _print_breakdown(reports: Sequence[ReplyReport]) -> None:
    """
    Print the full field-level split of the ten heaviest replies.

    Args:
        reports: The reports, biggest first.

    """
    print("\nfield-level breakdown of the ten heaviest replies (compact bytes per top-level data key)")
    print("-" * 110)
    for report in reports[:10]:
        total = sum(report.data_bytes.values())
        print(f"\n  {report.tool}  ({report.wire_bytes:,} wire B, {total:,} B of data)")
        for key, size in report.data_bytes.items():
            if size < _BREAKDOWN_FLOOR:
                continue
            print(f"    {key:<28}{size:>9,} B  {100 * size / max(total, 1):>5.1f}%")


def _print_growth(app: object, reports: Sequence[ReplyReport]) -> None:
    """
    Print what one more input item costs each reply that grows with its inputs.

    Args:
        app: The app `_measure` returned.
        reports: The reference-scale reports, used for the baseline bytes.

    """
    baseline = {report.tool: report.wire_bytes for report in reports}
    print("\ngrowth per input item (wire bytes)")
    print("-" * 110)
    print(f"  {'tool':<32}{'item':<20}{'at reference':>13}{'per item':>10}{'priced at':>11}")
    for axis, (label, probe, tool_names) in _GROWTH_AXES.items():
        tools = [name for name in tool_names if name in baseline]
        if not tools:
            continue
        smaller = _remeasure(app, tools, replace(REFERENCE_SCALE, **{axis: probe}))
        larger = _remeasure(app, tools, replace(REFERENCE_SCALE, **{axis: probe + 1}))
        for tool in tools:
            marginal = larger[tool] - smaller[tool]
            print(f"  {tool:<32}{label:<20}{baseline[tool]:>13,}{marginal:>10,}{probe:>11}")
    _install_stub(REFERENCE_SCALE)


# Keys a handler fills with a page of records; a cap rule would shorten these lists.
_PAGED_KEYS = frozenset(
    {
        "bones",
        "capabilities",
        "changed_keys",
        "changed_objects",
        "datablocks",
        "elements",
        "findings",
        "items",
        "lights",
        "objects",
        "records",
    }
)


def _without_keys(data: object, drop: frozenset[str]) -> object:
    """
    Copy a `data` block without the named top-level keys.

    Args:
        data: The envelope's `data` value.
        drop: Keys to leave out.

    Returns:
        object: The copy, or `data` unchanged when it is not an object.

    """
    if not isinstance(data, dict):
        return data
    return {key: value for key, value in data.items() if key not in drop}


def _shortened_pages(value: object, limit: int) -> object:
    """
    Copy a value with every paged list shortened, at any nesting depth.

    Args:
        value: Any part of a `data` block.
        limit: Entries to keep in a paged list.

    Returns:
        object: The copy.

    """
    if isinstance(value, dict):
        shortened: dict[str, object] = {}
        for key, item in value.items():
            paged = key in _PAGED_KEYS and isinstance(item, list)
            shortened[key] = item[:limit] if paged else _shortened_pages(item, limit)
        return shortened
    if isinstance(value, list):
        return [_shortened_pages(item, limit) for item in value]
    return value


def _repriced(report: ReplyReport, rewrite: Callable[[object], object]) -> int:
    """
    Re-serialize one reply with a candidate rule applied and measure what it saved.

    Args:
        report: The measured reply.
        rewrite: Rewrites one envelope's `data` block the way the rule would.

    Returns:
        int: Wire bytes the rule would have removed from this reply.

    """
    rewritten = [
        {**envelope, "data": rewrite(envelope.get("data"))} if isinstance(envelope, dict) else envelope
        for envelope in report.envelopes
    ]
    return report.wire_bytes - sum(_indented_bytes(envelope) for envelope in rewritten)


def _reply_budget() -> int:
    """
    Read the budget the server actually enforces.

    Imported inside the function because `blender_mcp` must not load before the selection is
    set in the environment.

    Returns:
        `envelope.REPLY_BYTE_BUDGET`, in wire bytes.

    """
    from blender_mcp.server.tools.envelope import REPLY_BYTE_BUDGET  # ruff: ignore[import-outside-top-level]

    return REPLY_BYTE_BUDGET


def _rule_savings(reports: Sequence[ReplyReport]) -> list[tuple[str, int, int]]:
    """
    Price candidate budget rules against the measured replies, without choosing between them.

    Every rule but the last is priced by applying it to the measured envelopes and
    re-serializing them, so the saving is real wire bytes rather than an estimate.

    Args:
        reports: The reference-scale reports.

    Returns:
        list[tuple[str, int, int]]: Each rule, the wire bytes it would have saved across one
        call of every tool, and how many replies it touches.

    """
    rules: list[tuple[str, Callable[[object], object]]] = [
        (
            "drop the pre-change state (`before`, `old`) from mutation replies, keeping `after`/`new`",
            lambda data: _without_keys(data, frozenset({"before", "old"})),
        ),
        (
            "drop the whole-state echo (`settings`) from replies that already carry `after`",
            lambda data: _without_keys(data, frozenset({"settings"})),
        ),
        (
            "page every embedded record list at 10 entries instead of the handlers' 12-291",
            lambda data: _shortened_pages(data, 10),
        ),
    ]
    priced = []
    for rule, rewrite in rules:
        savings = [_repriced(report, rewrite) for report in reports]
        priced.append((rule, sum(savings), sum(1 for saving in savings if saving)))
    budget = _reply_budget()
    over_budget = [max(0, report.wire_bytes - budget) for report in reports]
    priced.append(
        (
            f"cap one reply at {budget:,} wire bytes and report the remainder as truncated",
            sum(over_budget),
            sum(1 for excess in over_budget if excess),
        )
    )
    return priced


def main() -> None:
    """
    Measure and print the reply sizes for the tool selection named on the command line.

    Prints the selection, the reference scene, a table of every measured reply, the
    field-level breakdown of the ten heaviest, growth per input item, the unmeasured list,
    and the candidate budget rules priced against the measured data. With `--json`, prints
    the enforced budget and each tool's reply bytes instead, for the regression test.

    """
    arguments = [argument for argument in sys.argv[1:] if argument != "--json"]
    selection = arguments[0] if arguments else ""
    reports, app = _measure(selection, REFERENCE_SCALE)
    total = sum(report.wire_bytes for report in reports)
    if "--json" in sys.argv:
        # The machine-readable form the reply-budget regression test reads.
        print(
            json.dumps(
                {
                    "budget": _reply_budget(),
                    "replies": {report.tool: report.wire_bytes for report in reports},
                }
            )
        )
        return
    print(f"selection : {selection or '(core only)'}")
    print(f"scene     : {REFERENCE_SCALE}")
    print(f"tools     : {len(reports)}")
    print(f"wire bytes: {total:,} total, {total // max(len(reports), 1):,} mean, {reports[0].wire_bytes:,} worst")
    print(f"tokens    : ~{total / _BYTES_PER_TOKEN:,.0f} for one call of each")
    _print_table(reports)
    _print_breakdown(reports)
    _print_growth(app, reports)
    print("\nunmeasured")
    print("-" * 110)
    for tool, reason in _UNMEASURED.items():
        print(f"  {tool}: {reason}")
    print(f"\ncandidate budget rules, priced by re-serializing the measured replies ({len(reports)} calls)")
    print("-" * 110)
    for rule, saving, touched in _rule_savings(reports):
        print(f"  {saving:>9,} B saved, {touched:>3} replies changed  {rule}")


if __name__ == "__main__":
    main()
