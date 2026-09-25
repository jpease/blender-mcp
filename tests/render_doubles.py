"""
The scene the rendering and render-job suites drive the add-on's render handlers against.

It carries every `RenderSettings`, Cycles, EEVEE and view-layer field those handlers
read, at Blender's factory values.
"""

import types


class FakeScene(types.SimpleNamespace):
    """
    A scene stub that also holds Blender ID custom properties.

    `configure_render_settings` records an authored frame range as `scene[...]` and
    `render_scene` reads it back, so a stub without the mapping protocol would make the
    guard untestable.
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.__dict__["_custom_properties"] = {}

    def get(self, key: str, default: object = None) -> object:
        return self._custom_properties.get(key, default)

    def __getitem__(self, key: str) -> object:
        return self._custom_properties[key]

    def __setitem__(self, key: str, value: object) -> None:
        self._custom_properties[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self._custom_properties

    def __delitem__(self, key: str) -> None:
        del self._custom_properties[key]


def fake_view_layer(handlers, name="ViewLayer"):
    layer = types.SimpleNamespace(
        name=name,
        material_override=None,
        world_override=None,
        # `_render_pass_info` walks this; an empty list is "no passes reported", which is
        # exactly what a stub can honestly claim.
        bl_rna=types.SimpleNamespace(properties=[]),
    )
    for prop in handlers._VIEW_LAYER_PROPERTIES:
        setattr(layer, prop, 8 if prop == "pass_cryptomatte_depth" else True)
    return layer


def fake_scene(handlers, name="Scene"):
    image_settings = types.SimpleNamespace(
        file_format="PNG",
        color_mode="RGBA",
        color_depth="8",
        compression=15,
        quality=90,
        exr_codec="ZIP",
        views_format="INDIVIDUAL",
        stereo_3d_format=types.SimpleNamespace(display_mode="ANAGLYPH"),
    )
    render = types.SimpleNamespace(
        engine="BLENDER_EEVEE",
        resolution_x=1920,
        resolution_y=1080,
        resolution_percentage=100,
        pixel_aspect_x=1.0,
        pixel_aspect_y=1.0,
        fps=24,
        fps_base=1.0,
        film_transparent=False,
        filepath="/tmp/render/",
        file_extension=".png",
        use_file_extension=True,
        use_overwrite=True,
        use_placeholder=False,
        use_motion_blur=False,
        motion_blur_shutter=0.5,
        motion_blur_position="CENTER",
        use_multiview=False,
        use_stamp=False,
        stamp_note_text="",
        image_settings=image_settings,
        use_persistent_data=False,
        use_simplify=False,
        simplify_subdivision_render=6,
        filter_size=1.5,
    )
    return FakeScene(
        name=name,
        camera=None,
        frame_start=1,
        frame_end=250,
        frame_step=1,
        use_nodes=False,
        node_tree=None,
        compositing_node_group=None,
        render=render,
        cycles=types.SimpleNamespace(
            samples=128,
            use_denoising=True,
            device="CPU",
            pixel_filter_type="BLACKMAN_HARRIS",
            filter_width=1.5,
            film_transparent_glass=False,
            film_transparent_roughness=0.1,
        ),
        eevee=types.SimpleNamespace(
            taa_samples=16,
            taa_render_samples=64,
            use_shadows=True,
            use_raytracing=False,
            ray_tracing_method="SCREEN",
            ray_tracing_options=types.SimpleNamespace(
                resolution_scale="2",
                screen_trace_quality=0.25,
                screen_trace_thickness=0.1,
                trace_max_roughness=0.5,
                use_denoise=True,
            ),
        ),
        view_layers=[fake_view_layer(handlers)],
        view_settings=types.SimpleNamespace(view_transform="AgX", look="None", exposure=0.0, gamma=1.0),
    )
