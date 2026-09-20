"""
Which render property each `configure_render_settings` patch key writes, and on which owner.

A routing table, not behaviour: the engine guards, the range checks and the rollback stay in
`handlers/rendering.py`. Keeping the routes here makes them readable outside Blender, which is
what lets `scripts/render_coverage.py` report the properties no tool schema can reach without
importing `bpy`.

A section can write several owners (an `output` patch splits across `scene.render` and
`scene.render.image_settings`), so each section maps to an ordered tuple of routes. Every route
but the last claims exactly the keys its mapping names; the last takes whatever is left, using
its mapping as a translation table with an identity fallback. That is the rule the handler
implemented inline before this table existed.
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
# gate on which keys exist.
IDENTITY = None

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
