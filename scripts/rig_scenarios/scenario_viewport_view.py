r"""
Prove `get_viewport_screenshot`'s `view`/`shading_override` against a live GUI Blender.

This is the one part of that tool no other gate can reach. `gpu.types.GPUOffScreen` cannot
be constructed under `--background` at all (it needs an initialized GPU context), so
`tests/server/tools/test_viewport.py` can only check the wiring and `just smoke` cannot
touch the draw call. What is unproven without a window is exactly the interesting claim:
that `offscreen.draw_view3d` accepts a synthetic, never-navigated `view_matrix`/
`window_matrix` pair - one built from a camera object, or from an unlinked throwaway camera
for the ad hoc eye/target mode - and rasterizes *that* view rather than the viewport's own.

The framing assertion is theme-independent by construction. Two cameras sit at the same
distance from one cube: one looking at it, one looking away. The away-facing capture is the
background reference, and the pixels where the looking-at capture differs from it are the
cube's silhouette, whatever the user's theme, gradient or overlay settings are. A capture
that ignored its matrices and drew the live viewport instead would produce two identical
images and fail here; one that drew a black or empty frame would produce an empty
silhouette and fail too.

PNG decoding is stdlib-only (`zlib` plus the five PNG filter types): `numpy` is not in this
project's virtualenv, and the rig's scenario runs outside Blender, so `bpy`'s image API is
not available to it either.

Run it:

    just rig scripts/rig_scenarios/scenario_viewport_view.py

or, as the permanent gate that skips itself without a live Blender:

    just gate
"""

import zlib

from pathlib import Path
from typing import Protocol


class Rig(Protocol):
    """The part of `BlenderRig` this scenario uses (a Protocol: the rig loads scenarios by path)."""

    work_dir: Path

    def send(self, command_type: str, params: dict | None = None) -> dict:
        """
        Send one addon command and return its decoded response.

        Args:
            command_type: The addon command name.
            params: Command parameters; omitted means none.

        Returns:
            dict: The decoded response.

        """
        ...


REQUIRED_COMMANDS = ("get_viewport_screenshot", "create_primitive", "create_camera")

_COLLECTION = "RigViewport"
_CUBE = "RigViewportCube"
_CAMERA_TOWARD = "RigViewportCamToward"
_CAMERA_AWAY = "RigViewportCamAway"
# Far enough that the cube is fully inside a 50 mm frame, close enough that its silhouette
# is a large fraction of it, so the centroid assertion has a real signal to measure.
_CAMERA_DISTANCE = 6.0
_MAX_SIZE = 400
# A silhouette this small would mean the cube was nearly off-frame, or that the capture drew
# something other than what its matrices asked for.
_MIN_SILHOUETTE_FRACTION = 0.02
# The cube is centred in both aimed views, so its silhouette's centroid must land near the
# middle of the frame. A capture of the live viewport (an unaimed user navigation) would not.
_MAX_CENTROID_OFFSET = 0.15
# Per-channel 0-255 difference a pixel must exceed to count as part of the silhouette, so
# dithering or a one-bit rounding difference in the background is not mistaken for geometry.
_BACKGROUND_TOLERANCE = 8

# PNG's five scanline filter types (RFC 2083 §6) and the sample layouts this decoder reads.
_FILTER_NONE = 0
_FILTER_SUB = 1
_FILTER_UP = 2
_FILTER_AVERAGE = 3
_FILTER_PAETH = 4
_PNG_BIT_DEPTH = 8
_PNG_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}
# Compare RGB only; these captures are fully opaque, so an alpha difference is not geometry.
_COLOUR_CHANNELS = 3


def _paeth(a: int, b: int, c: int) -> int:
    """
    Return the PNG Paeth predictor for the three neighbouring bytes.

    Args:
        a: Byte to the left.
        b: Byte above.
        c: Byte above-left.

    Returns:
        int: Whichever of a, b, c is closest to a + b - c.

    """
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _unfilter(raw: bytes, width: int, height: int, channels: int) -> list[bytes]:
    """
    Reverse PNG's per-scanline filtering.

    Args:
        raw: The decompressed IDAT stream: one filter byte per scanline, then its bytes.
        width: Image width in pixels.
        height: Image height in pixels.
        channels: Bytes per pixel (8-bit samples only).

    Returns:
        list[bytes]: One unfiltered scanline per row, top row first.

    Raises:
        SystemExit: If the stream is truncated or names an unknown filter type.

    """
    stride = width * channels
    expected = height * (stride + 1)
    if len(raw) != expected:
        raise SystemExit(f"PNG IDAT is {len(raw)} bytes, expected {expected} for {width}x{height}x{channels}")
    rows: list[bytes] = []
    previous = bytes(stride)
    offset = 0
    for _row_index in range(height):
        filter_type = raw[offset]
        line = bytearray(raw[offset + 1 : offset + 1 + stride])
        offset += stride + 1
        if filter_type == _FILTER_NONE:
            pass
        elif filter_type == _FILTER_SUB:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filter_type == _FILTER_UP:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif filter_type == _FILTER_AVERAGE:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == _FILTER_PAETH:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                upper_left = previous[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(left, previous[i], upper_left)) & 0xFF
        else:
            raise SystemExit(f"unknown PNG filter type {filter_type}")
        rows.append(bytes(line))
        previous = rows[-1]
    return rows


def _decode_png(path: Path) -> tuple[int, int, int, list[bytes]]:
    """
    Decode an 8-bit PNG with stdlib only.

    Args:
        path: The PNG file the addon just wrote.

    Returns:
        tuple: (width, height, channels, unfiltered scanlines).

    Raises:
        SystemExit: If the file is not an 8-bit non-interlaced PNG this decoder handles.

    """
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SystemExit(f"{path} is not a PNG ({data[:8]!r})")
    offset = 8
    header: tuple[int, int, int, int] | None = None
    compressed = bytearray()
    while offset < len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        kind = data[offset + 4 : offset + 8]
        body = data[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            width = int.from_bytes(body[0:4], "big")
            height = int.from_bytes(body[4:8], "big")
            header = (width, height, body[8], body[9])
            if body[12] != 0:
                raise SystemExit("interlaced PNGs are not decoded by this scenario")
        elif kind == b"IDAT":
            compressed += body
        elif kind == b"IEND":
            break
    if header is None:
        raise SystemExit(f"{path} has no IHDR chunk")
    width, height, depth, colour_type = header
    if depth != _PNG_BIT_DEPTH:
        raise SystemExit(f"{path} has bit depth {depth}; this scenario decodes 8-bit PNGs")
    channels = _PNG_CHANNELS.get(colour_type)
    if channels is None:
        raise SystemExit(f"{path} has PNG colour type {colour_type}, which this scenario does not decode")
    return width, height, channels, _unfilter(zlib.decompress(bytes(compressed)), width, height, channels)


def _differing_pixels(
    rows: list[bytes], ref_rows: list[bytes], width: int, height: int, channels: int
) -> tuple[int, int, int]:
    """
    Count the pixels two same-sized captures disagree on, and sum their coordinates.

    Args:
        rows: Unfiltered scanlines of the capture under test.
        ref_rows: Unfiltered scanlines of the reference capture.
        width: Image width in pixels.
        height: Image height in pixels.
        channels: Bytes per pixel.

    Returns:
        tuple: (differing pixel count, sum of their x coordinates, sum of their y).

    """
    differing = 0
    sum_x = 0
    sum_y = 0
    # Colour channels only: alpha is 255 everywhere in these captures, and an alpha-only
    # difference would not be a visible silhouette.
    compared = min(channels, _COLOUR_CHANNELS)
    for y in range(height):
        row, ref_row = rows[y], ref_rows[y]
        for x in range(width):
            base = x * channels
            delta = max(abs(row[base + c] - ref_row[base + c]) for c in range(compared))
            if delta > _BACKGROUND_TOLERANCE:
                differing += 1
                sum_x += x
                sum_y += y
    return differing, sum_x, sum_y


def _silhouette(shot: Path, reference: Path) -> tuple[int, float, float, float]:
    """
    Measure where one capture differs from a same-sized capture of the background alone.

    Args:
        shot: Capture that should contain the cube.
        reference: Capture of the same scene from a view the cube is behind.

    Returns:
        tuple: (differing pixel count, that count as a fraction of the frame, centroid x,
        centroid y). The centroid coordinates are normalised to 0..1 across the frame, and
        are the frame's centre when nothing differs. The count is reported alongside the
        fraction so "nothing differed at all" is an exact integer test, not a float compare.

    Raises:
        SystemExit: If the two captures are not the same size and format.

    """
    width, height, channels, rows = _decode_png(shot)
    ref_width, ref_height, ref_channels, ref_rows = _decode_png(reference)
    if (width, height, channels) != (ref_width, ref_height, ref_channels):
        raise SystemExit(
            f"{shot.name} is {width}x{height}x{channels} but {reference.name} is "
            f"{ref_width}x{ref_height}x{ref_channels}; they cannot be compared"
        )
    differing, sum_x, sum_y = _differing_pixels(rows, ref_rows, width, height, channels)
    if differing == 0:
        return 0, 0.0, 0.5, 0.5
    return differing, differing / (width * height), sum_x / differing / width, sum_y / differing / height


def _capture(rig: Rig, name: str, params: dict) -> tuple[Path, dict]:
    """
    Take one screenshot into the rig's work dir and return its file and reply.

    Args:
        rig: The live rig.
        name: File stem for this capture, so a failed run leaves named evidence behind.
        params: Extra `get_viewport_screenshot` parameters (`view`, `shading_override`, ...).

    Returns:
        tuple: (the written PNG, the command's result dict).

    Raises:
        SystemExit: If the command failed or wrote nothing.

    """
    path = rig.work_dir / f"{name}.png"
    response = rig.send(
        "get_viewport_screenshot",
        {"max_size": _MAX_SIZE, "filepath": str(path), "format": "png", **params},
    )
    if response["status"] != "success":
        raise SystemExit(f"capture {name!r} failed: {response}")
    result = response["result"]
    if "error" in result:
        raise SystemExit(f"capture {name!r} was refused: {result['error']}")
    if not path.is_file() or path.stat().st_size == 0:
        raise SystemExit(f"capture {name!r} reported success but wrote no image to {path}")
    print(
        f"RIG: {name}: {result['width']}x{result['height']} via {result['method']}, "
        f"view_source={result['view_source']}, shading_mode={result['shading_mode']}, "
        f"{path.stat().st_size} bytes",
        flush=True,
    )
    return path, result


def _build_scene(rig: Rig) -> str:
    """
    Put one cube and two opposed cameras in the live scene.

    Args:
        rig: The live rig.

    Returns:
        str: The scene's name.

    Raises:
        SystemExit: If any construction command failed.

    """
    listing = rig.send("list_scene_objects")
    if listing["status"] != "success":
        raise SystemExit(f"list_scene_objects failed: {listing}")
    scene_name = listing["result"]["name"]

    cube = rig.send("create_primitive", {"primitive_type": "CUBE", "name": _CUBE, "size": 2.0})
    if cube["status"] != "success":
        raise SystemExit(f"create_primitive failed: {cube}")

    for name, look_at in (
        (_CAMERA_TOWARD, (0.0, 0.0, 0.0)),
        # Same position, aimed at a point twice as far away in the same direction, so the
        # cube is directly behind this camera and cannot appear in its frame.
        (_CAMERA_AWAY, (0.0, -2.0 * _CAMERA_DISTANCE, 0.0)),
    ):
        created = rig.send(
            "create_camera",
            {
                "scene_name": scene_name,
                "collection_name": _COLLECTION,
                "name": name,
                "location": (0.0, -_CAMERA_DISTANCE, 0.0),
                "look_at_point": look_at,
            },
        )
        if created["status"] != "success":
            raise SystemExit(f"create_camera {name!r} failed: {created}")
    print(f"RIG: scene {scene_name!r} holds {_CUBE}, {_CAMERA_TOWARD}, {_CAMERA_AWAY}", flush=True)
    return scene_name


def _check_camera_object_view(rig: Rig) -> None:
    """
    Prove a camera-object view is drawn from that camera, not from the live viewport.

    Args:
        rig: The live rig.

    Raises:
        AssertionError: If the capture did not come from the offscreen path, did not report
            the camera_object source, or does not frame the cube where that camera sees it.

    """
    toward, toward_result = _capture(rig, "camera_toward", {"view": {"camera_object": _CAMERA_TOWARD}})
    away, away_result = _capture(rig, "camera_away", {"view": {"camera_object": _CAMERA_AWAY}})

    for result in (toward_result, away_result):
        assert result["view_source"] == "camera_object", result
        assert result["method"] == "offscreen", f"a synthetic view must not fall back to a window grab: {result}"

    _count, fraction, centre_x, centre_y = _silhouette(toward, away)
    print(f"RIG: camera_object silhouette {fraction:.3f} of frame at ({centre_x:.3f}, {centre_y:.3f})", flush=True)
    assert fraction >= _MIN_SILHOUETTE_FRACTION, (
        f"the camera aimed at {_CUBE} drew a frame only {fraction:.4f} different from the one aimed away "
        "from it; draw_view3d did not honour the view matrix"
    )
    assert abs(centre_x - 0.5) <= _MAX_CENTROID_OFFSET and abs(centre_y - 0.5) <= _MAX_CENTROID_OFFSET, (
        f"the cube's silhouette centroid is ({centre_x:.3f}, {centre_y:.3f}), not near the centre of the "
        "frame the camera was aimed at"
    )

    stable, _fraction, _x, _y = _silhouette(away, away)
    assert stable == 0, "a capture must be byte-stable against itself"


def _check_eye_target_view(rig: Rig) -> None:
    """
    Prove the ad hoc eye/target view uses its unlinked throwaway camera's own transform.

    This is the case the unit tests can only wire up: the camera exists in no view layer,
    so its `matrix_world` is never evaluated and only `matrix_basis` describes where it is.

    Args:
        rig: The live rig.

    Raises:
        AssertionError: If the capture is not framed from the requested eye.

    """
    side, side_result = _capture(
        rig,
        "eye_side",
        {"view": {"eye": (_CAMERA_DISTANCE, 0.0, 0.0), "target": (0.0, 0.0, 0.0)}},
    )
    side_away, side_away_result = _capture(
        rig,
        "eye_side_away",
        {"view": {"eye": (_CAMERA_DISTANCE, 0.0, 0.0), "target": (2.0 * _CAMERA_DISTANCE, 0.0, 0.0)}},
    )
    above, _above_result = _capture(
        rig,
        "eye_above",
        {"view": {"eye": (0.0, 0.0, _CAMERA_DISTANCE), "target": (0.0, 0.0, 0.0)}},
    )

    for result in (side_result, side_away_result):
        assert result["view_source"] == "eye_target", result
        assert result["method"] == "offscreen", result

    _count, fraction, centre_x, centre_y = _silhouette(side, side_away)
    print(f"RIG: eye_target silhouette {fraction:.3f} of frame at ({centre_x:.3f}, {centre_y:.3f})", flush=True)
    assert fraction >= _MIN_SILHOUETTE_FRACTION, (
        f"an eye at +X aimed at the origin drew a frame only {fraction:.4f} different from one aimed away; "
        "the synthetic camera's transform did not reach draw_view3d"
    )
    assert abs(centre_x - 0.5) <= _MAX_CENTROID_OFFSET and abs(centre_y - 0.5) <= _MAX_CENTROID_OFFSET, (
        f"the cube's silhouette centroid is ({centre_x:.3f}, {centre_y:.3f}) for an eye aimed at it"
    )

    # A different eye must produce a different image. Identical bytes here would mean the
    # eye/target fields were accepted and then ignored.
    changed, _fraction, _x, _y = _silhouette(above, side)
    assert changed > 0, "captures from +X and from +Z are byte-identical; the eye was ignored"

    _assert_no_scratch_camera(rig)


def _assert_no_scratch_camera(rig: Rig) -> None:
    """
    Prove the eye/target path's throwaway camera is gone from `bpy.data`, not just unlinked.

    `list_scene_objects` cannot see it: the synthetic camera is deliberately never linked to
    a collection, so it would be absent from the scene whether it leaked or not.
    `get_object_info` resolves through `find_object(bpy.data.objects, ...)`, which is exactly
    where a leak would still be visible.

    Args:
        rig: The live rig.

    Raises:
        AssertionError: If the scratch camera datablock survived a capture.

    """
    probe = rig.send("get_object_info", {"name": "_mcp_synthetic_view"})
    assert probe["status"] == "error", f"the throwaway camera the eye/target path builds is still in bpy.data: {probe}"


def _check_shading_override_is_restored(rig: Rig) -> None:
    """
    Prove `shading_override` applies to one capture and leaves the live shading as it was.

    Args:
        rig: The live rig.

    Raises:
        AssertionError: If the override did not take effect, or did not restore.

    """
    _before, before_result = _capture(rig, "live_before", {})
    original = before_result["shading_mode"]
    assert before_result["view_source"] == "live_viewport", before_result

    _forced, forced_result = _capture(rig, "live_material", {"shading_override": "MATERIAL"})
    assert forced_result["shading_mode"] == "MATERIAL", forced_result

    _after, after_result = _capture(rig, "live_after", {})
    assert after_result["shading_mode"] == original, (
        f"the live viewport's shading is {after_result['shading_mode']!r} after a MATERIAL override, "
        f"not the {original!r} it started as"
    )

    refused = rig.send(
        "get_viewport_screenshot",
        {"max_size": _MAX_SIZE, "filepath": str(rig.work_dir / "refused.png"), "shading_override": "RENDERED"},
    )
    assert "error" in refused["result"], f"RENDERED must be refused, not rendered: {refused}"
    print(f"RIG: shading override applied and restored to {original!r}; RENDERED refused", flush=True)


def _check_the_live_viewport_was_never_moved(rig: Rig) -> None:
    """
    Prove the synthetic captures left the human's own navigation untouched.

    Args:
        rig: The live rig.

    Raises:
        AssertionError: If two live captures taken around the synthetic ones differ.

    """
    first, _first_result = _capture(rig, "live_navigation_before", {})
    _capture(rig, "live_navigation_probe", {"view": {"camera_object": _CAMERA_TOWARD}})
    second, _second_result = _capture(rig, "live_navigation_after", {})

    changed, fraction, _x, _y = _silhouette(first, second)
    assert changed == 0, (
        f"{changed} pixels ({fraction:.4f} of the frame) of the live viewport's own capture changed across "
        "a camera_object capture; aiming a screenshot must not navigate the viewport"
    )
    print("RIG: the live viewport's own capture is byte-identical before and after", flush=True)


def run(rig: Rig) -> None:
    """
    Drive the whole viewport-view gate against a live GUI Blender.

    Args:
        rig: The live rig, already serving on its socket.

    Raises:
        SystemExit: If the addon does not advertise the commands this scenario needs.

    """
    info = rig.send("get_addon_info")
    if info["status"] != "success":
        raise SystemExit(f"get_addon_info failed: {info}")
    capabilities = info["result"]["capabilities"]
    missing = [name for name in REQUIRED_COMMANDS if name not in capabilities]
    if missing:
        raise SystemExit(f"the addon does not advertise {missing}")

    _build_scene(rig)
    _check_camera_object_view(rig)
    _check_eye_target_view(rig)
    _check_shading_override_is_restored(rig)
    _check_the_live_viewport_was_never_moved(rig)
