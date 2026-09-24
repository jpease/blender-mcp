"""
Which render property each `configure_render_settings` patch key writes, and on which owner.

A routing table, not behaviour: the engine guards, the range checks and the rollback stay in
`handlers/rendering.py`. Keeping the routes here makes them readable outside Blender, which is
what lets `scripts/render_coverage.py` report the properties no tool schema can reach without
importing `bpy`.

Every patch key is routed from here: the flat ones through `FLAT_ROUTES` and each nested
section through `NESTED_SECTIONS`. Both are the same shape, because the handler applies them
with the same function.

A section can write several owners (an `output` patch splits across `scene.render` and
`scene.render.image_settings`), so each maps to an ordered tuple of routes. Every route but
the last claims exactly the keys its mapping names; the last takes whatever is left, using its
mapping as a translation table with an identity fallback. That is the rule the handler
implemented inline before this table existed.

`configure_lighting_quality` writes render properties too, so its field tables live here as well
(`LIGHTING_CYCLES_FIELDS`, `LIGHTING_EEVEE_FIELD_MAP`): the coverage report counts what either
tool reaches, and a property one of them already sets is not reported as a gap.
"""

from collections.abc import Mapping
from types import MappingProxyType

# Patch keys written straight onto `scene.render` under their own names.
RENDER_PROPERTIES = frozenset(
    {
        "engine",
        "resolution_x",
        "resolution_y",
        "resolution_percentage",
        "pixel_aspect_x",
        "pixel_aspect_y",
        "fps",
        "fps_base",
        "film_transparent",
    }
)
# Written onto the Scene itself, not its render settings.
SCENE_PROPERTIES = frozenset({"frame_start", "frame_end", "frame_step"})
IMAGE_PROPERTY_MAPPING: Mapping[str, str] = MappingProxyType(
    {
        "image_format": "file_format",
        "color_mode": "color_mode",
        "color_depth": "color_depth",
        "compression": "compression",
        "quality": "quality",
    }
)
CYCLES_PROPERTY_MAPPING: Mapping[str, str] = MappingProxyType(
    {"cycles_samples": "samples", "cycles_use_denoising": "use_denoising"}
)

# `IDENTITY` marks a route whose patch keys are already the RNA identifiers, which is what
# `_set_supported(..., mapping=None, ...)` does today; the server-side pydantic model is the
# gate on which keys exist. Only the last route of a section may be IDENTITY: an earlier one
# has to name its keys to claim them.
IDENTITY = None


def _identity_routes(names: frozenset[str]) -> Mapping[str, str]:
    """
    Build a route mapping for patch keys that are already their own RNA identifiers.

    Args:
        names: The patch keys this route claims.

    Returns:
        Mapping[str, str]: Each name mapped to itself, so a non-final route can claim it.

    """
    return MappingProxyType({name: name for name in sorted(names)})


# The four flat groups as routes, in the order a patch writes them: the patch keys that carry
# no section of their own, routed by exactly the rule `NESTED_SECTIONS` uses. They sit beside
# that table rather than inside it because `scripts/render_coverage.py` reads it as "the
# sections a patch nests", and because SCENE_PROPERTIES' owner is the Scene itself, which has
# no owner path under it. Cycles runs last and so takes the remainder, which is what makes a
# flat key no route claims reach the handler's availability check and be refused rather than
# silently dropped.
FLAT_ROUTES: tuple[tuple[str, Mapping[str, str] | None, str], ...] = (
    ("render", _identity_routes(RENDER_PROPERTIES), "render"),
    # The empty owner path is the Scene itself, which frame_start/frame_end/frame_step live on
    # rather than on its render settings.
    ("", _identity_routes(SCENE_PROPERTIES), "scene"),
    ("render.image_settings", IMAGE_PROPERTY_MAPPING, "image output"),
    ("cycles", CYCLES_PROPERTY_MAPPING, "Cycles"),
)

# section -> ((owner path relative to the scene, {patch key: RNA identifier} | IDENTITY, label), ...)
# The label is the one the refusal message uses, so moving a route here cannot silently reword
# an error a client already reads.
NESTED_SECTIONS: Mapping[str, tuple[tuple[str, Mapping[str, str] | None, str], ...]] = MappingProxyType(
    {
        "motion_blur": (
            (
                "render",
                MappingProxyType(
                    {"enabled": "use_motion_blur", "shutter": "motion_blur_shutter", "position": "motion_blur_position"}
                ),
                "motion blur",
            ),
        ),
        "film": (
            ("render", MappingProxyType({"transparent": "film_transparent"}), "film"),
            (
                "cycles",
                MappingProxyType(
                    {
                        "transparent_glass": "film_transparent_glass",
                        "transparent_roughness": "film_transparent_roughness",
                    }
                ),
                "Cycles film",
            ),
        ),
        "output": (
            (
                "render",
                MappingProxyType(
                    {
                        "filepath": "filepath",
                        "use_file_extension": "use_file_extension",
                        "use_overwrite": "use_overwrite",
                        "use_placeholder": "use_placeholder",
                    }
                ),
                "render output",
            ),
            (
                "render.image_settings",
                MappingProxyType({**IMAGE_PROPERTY_MAPPING, "exr_codec": "exr_codec"}),
                "image output",
            ),
        ),
        "metadata": (("render", IDENTITY, "render metadata"),),
        # `enabled` is the only multiview key on scene.render and `stereo_3d_format` lives on its
        # own struct; everything else is an image-settings identifier, so that route runs last
        # and takes the remainder.
        "multiview": (
            ("render", MappingProxyType({"enabled": "use_multiview"}), "multiview"),
            (
                "render.image_settings.stereo_3d_format",
                MappingProxyType({"stereo_3d_format": "display_mode"}),
                "stereo output",
            ),
            ("render.image_settings", IDENTITY, "multiview image"),
        ),
        "cycles": (("cycles", IDENTITY, "Cycles"),),
        "eevee": (("eevee", IDENTITY, "EEVEE"),),
        "eevee.ray_tracing": (("eevee.ray_tracing_options", IDENTITY, "EEVEE ray tracing"),),
        # Persistent data, simplify and the pixel filter size: engine-independent render cost.
        "performance": (("render", IDENTITY, "render performance"),),
    }
)

# Every key `configure_render_settings` accepts at the top level: the flat properties plus the
# nested section names. `eevee.ray_tracing` is reached through `eevee`, never on its own.
RENDER_PATCH_PROPERTIES = (
    RENDER_PROPERTIES
    | SCENE_PROPERTIES
    | frozenset(IMAGE_PROPERTY_MAPPING)
    | frozenset(CYCLES_PROPERTY_MAPPING)
    | {section for section in NESTED_SECTIONS if "." not in section}
)

# `configure_lighting_quality`'s Cycles keys, which are the RNA identifiers on `scene.cycles`.
LIGHTING_CYCLES_FIELDS = frozenset(
    {
        "samples",
        "use_adaptive_sampling",
        "adaptive_threshold",
        "use_denoising",
        "light_sampling_threshold",
        "sample_clamp_direct",
        "sample_clamp_indirect",
        "max_bounces",
        "diffuse_bounces",
        "glossy_bounces",
        "transmission_bounces",
        "transparent_max_bounces",
        "volume_bounces",
        "device",
    }
)
# `configure_lighting_quality`'s EEVEE keys, translated onto `scene.eevee` RNA identifiers.
LIGHTING_EEVEE_FIELD_MAP: Mapping[str, str] = MappingProxyType(
    {
        "render_samples": "taa_render_samples",
        "light_threshold": "light_threshold",
        "shadow_pool_size": "shadow_pool_size",
        "shadow_resolution_scale": "shadow_resolution_scale",
        "shadow_ray_count": "shadow_ray_count",
        "shadow_step_count": "shadow_step_count",
        "use_raytracing": "use_raytracing",
        "ray_tracing_method": "ray_tracing_method",
        "use_fast_gi": "use_fast_gi",
        "volumetric_tile_size": "volumetric_tile_size",
        "volumetric_samples": "volumetric_samples",
        "volumetric_ray_depth": "volumetric_ray_depth",
    }
)
