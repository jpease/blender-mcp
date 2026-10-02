"""Run with Blender 5.1+ to smoke-test render_contact_sheet's grid, its state restore, and its cleanup."""

from __future__ import annotations

import os
import struct
import sys
import tempfile

from pathlib import Path

import bpy

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

load_addon("blender_mcp_contact_sheet_smoke")

from blender_mcp_contact_sheet_smoke.handlers.lighting import (  # ruff: ignore[module-import-not-at-top-of-file]
    LightingHandlers,
    rendering,
)

CELL_WIDTH = 96
CELL_HEIGHT = 64


def _png_size(path: str) -> tuple[int, int]:
    """Read a PNG's width and height from its IHDR chunk, without loading it into bpy.data."""
    with open(path, "rb") as handle:
        header = handle.read(24)
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    return struct.unpack(">II", header[16:24])


def _scene_state(scene) -> dict:
    """Everything a contact sheet overrides and must put back."""
    render = scene.render
    return {
        "camera": scene.camera.name,
        "frame": scene.frame_current,
        "engine": render.engine,
        "resolution": (render.resolution_x, render.resolution_y, render.resolution_percentage),
        "filepath": render.filepath,
        "format": (render.image_settings.file_format, render.image_settings.color_mode),
        "eevee_samples": scene.eevee.taa_render_samples,
    }


def _scratch_directories() -> set[str]:
    """List the contact sheet's own cell scratch directories now in the temp directory."""
    return {
        name
        for name in os.listdir(tempfile.gettempdir())
        if name.startswith("blender_mcp_contact_sheet_") and not name.startswith("blender_mcp_contact_sheet_smoke_")
    }


def _cell_alphas(path: str, columns: int, rows: int) -> list[float]:
    """Mean alpha of each grid cell, row 0 at the top; the probe image is removed again."""
    image = bpy.data.images.load(path, check_existing=False)
    try:
        width = image.size[0]
        pixels = list(image.pixels)
    finally:
        bpy.data.images.remove(image)
    alphas = []
    for row in range(rows):
        bottom = (rows - 1 - row) * CELL_HEIGHT
        for column in range(columns):
            left = column * CELL_WIDTH
            values = [
                pixels[((bottom + y) * width + left + x) * 4 + 3] for y in range(CELL_HEIGHT) for x in range(CELL_WIDTH)
            ]
            alphas.append(sum(values) / len(values))
    return alphas


def main() -> None:
    """Render a 2x2 sheet of three cells from two cameras, then a sheet that fails mid-batch."""
    handler = LightingHandlers()
    scene = bpy.context.scene
    front = scene.camera
    side_data = bpy.data.cameras.new("Side Camera")
    side = bpy.data.objects.new("Side Camera", side_data)
    scene.collection.objects.link(side)
    side.location = (0.0, -9.0, 1.5)
    side.rotation_euler = (1.45, 0.0, 0.0)
    scene.frame_set(3)
    before = _scene_state(scene)
    images_before = {image.name for image in bpy.data.images}
    scratch_before = _scratch_directories()
    cells = [
        {"camera_name": front.name, "frame": 1},
        {"camera_name": side.name, "frame": 1},
        {"camera_name": side.name, "frame": 5},
    ]

    try:
        handler.render_contact_sheet(
            scene.name, [*cells, {"camera_name": "Cube", "frame": 2}], "EEVEE", "/tmp/never_written.png"
        )
    except ValueError as exc:
        assert "is not a camera" in str(exc), exc
    else:
        raise AssertionError("A non-camera cell must refuse the whole sheet")
    assert _scene_state(scene) == before
    assert not os.path.exists("/tmp/never_written.png")

    with tempfile.TemporaryDirectory(prefix="blender_mcp_contact_sheet_smoke_") as directory:
        path = os.path.join(directory, "sheet.png")
        reply = handler.render_contact_sheet(
            scene.name, cells, "EEVEE", path, cell_width=CELL_WIDTH, cell_height=CELL_HEIGHT, samples=1
        )
        assert reply["grid"] == {
            "width": 2 * CELL_WIDTH,
            "height": 2 * CELL_HEIGHT,
            "columns": 2,
            "rows": 2,
            "cell_width": CELL_WIDTH,
            "cell_height": CELL_HEIGHT,
        }, reply["grid"]
        assert [(cell["camera"], cell["frame"], cell["row"], cell["column"]) for cell in reply["cells"]] == [
            (front.name, 1, 0, 0),
            (side.name, 1, 0, 1),
            (side.name, 5, 1, 0),
        ], reply["cells"]
        # Factory startup already holds an empty Render Result, which Blender exposes with no
        # pixels: like a preview, the sheet cannot put it back and must say so, not add another.
        if "Render Result" in images_before:
            assert len(reply["warnings"]) == 1, reply["warnings"]
            assert "could not be restored" in reply["warnings"][0], reply["warnings"]
        else:
            assert reply["warnings"] == [], reply["warnings"]
        assert _png_size(path) == (2 * CELL_WIDTH, 2 * CELL_HEIGHT)
        assert reply["size_bytes"] == os.path.getsize(path)
        assert _scene_state(scene) == before, (_scene_state(scene), before)
        assert {image.name for image in bpy.data.images} == images_before, bpy.data.images.keys()
        rendered_alpha = _cell_alphas(path, 2, 2)
        assert all(alpha > 0.99 for alpha in rendered_alpha[:3]), rendered_alpha
        assert rendered_alpha[3] < 1e-6, "the empty fourth cell must stay transparent"

        try:
            handler.render_contact_sheet(scene.name, cells, "EEVEE", path, cell_width=CELL_WIDTH, cell_height=64)
        except ValueError as exc:
            assert "confirm_overwrite" in str(exc), exc
        else:
            raise AssertionError("An existing output must not be replaced unconfirmed")

        # A cell that fails mid-batch: everything is still put back, and nothing is written.
        real_render_preview = rendering._render_preview
        calls = []

        def fail_second_cell(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                raise RuntimeError("simulated cell failure")
            return real_render_preview(*args, **kwargs)

        rendering._render_preview = fail_second_cell
        failed_path = os.path.join(directory, "failed.png")
        try:
            handler.render_contact_sheet(
                scene.name, cells, "EEVEE", failed_path, cell_width=CELL_WIDTH, cell_height=CELL_HEIGHT, samples=1
            )
        except RuntimeError as exc:
            assert "simulated cell failure" in str(exc), exc
        else:
            raise AssertionError("A failed cell must fail the sheet")
        finally:
            rendering._render_preview = real_render_preview
        assert len(calls) == 2
        assert not os.path.exists(failed_path)
        assert _scene_state(scene) == before, (_scene_state(scene), before)
        assert {image.name for image in bpy.data.images} == images_before, bpy.data.images.keys()
    assert _scratch_directories() == scratch_before, "cell scratch directories were left behind"

    print("CONTACT_SHEET_SMOKE_OK")


main()
