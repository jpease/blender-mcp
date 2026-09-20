# ruff: file-ignore[module-import-not-at-top-of-file]
"""Run with Blender 5.1+ to smoke-test inspect_delivery against real datablocks and a real save."""

import importlib.util
import json
import sys
import tempfile

from pathlib import Path

import bpy

addon_path = Path(__file__).resolve().parents[1] / "src" / "blender_mcp" / "bundled" / "addon" / "__init__.py"
package_name = "blender_mcp_delivery_smoke"
spec = importlib.util.spec_from_file_location(
    package_name,
    addon_path,
    submodule_search_locations=[str(addon_path.parent)],
)
assert spec is not None
addon = importlib.util.module_from_spec(spec)
sys.modules[package_name] = addon
spec.loader.exec_module(addon)

from blender_mcp_delivery_smoke.server_core import BlenderMCPServer

server = BlenderMCPServer()
scene = bpy.context.scene

work = tempfile.mkdtemp(prefix="delivery_smoke_")
shot_directory = Path(work) / "shot"
shot_directory.mkdir()
blend_path = shot_directory / "sh010.blend"

# A packed image travels inside the .blend; one pointing at a file outside it does not.
packed_source = Path(work) / "packed_texture.png"
packed = bpy.data.images.new("Packed Texture", width=4, height=4)
packed.filepath_raw = str(packed_source)
packed.file_format = "PNG"
packed.save()
packed.source = "FILE"
packed.filepath = str(packed_source)
packed.reload()
packed.pack()
assert packed.packed_file is not None

external = Path(work) / "external_texture.png"
loose = bpy.data.images.new("Loose Texture", width=4, height=4)
loose.filepath_raw = str(external)
loose.file_format = "PNG"
loose.save()
assert external.is_file()
loose.source = "FILE"
loose.filepath = str(external)
loose.reload()
bpy.ops.wm.save_as_mainfile(filepath=str(blend_path), compress=False, relative_remap=False)
assert bpy.data.filepath == str(blend_path)

report = server.inspect_delivery(scene.name)
assert report["saved"] is True
assert report["blend_filepath"] == str(blend_path)
by_name = {entry["name"]: entry for entry in report["entries"]}
assert "Loose Texture" in by_name, sorted(by_name)
assert by_name["Loose Texture"]["verdict"] == "ABSOLUTE", by_name["Loose Texture"]
assert by_name["Loose Texture"]["absolute"] is True
assert by_name["Packed Texture"]["verdict"] == "PACKED", by_name["Packed Texture"]
assert report["classes"]["IMAGE"]["unportable"] == 1, report["classes"]
assert report["portable"] is False

# No published reference may carry the directories above the file: the leaf is all
# a recipient needs, and anything more is this machine's storage layout.
# `blend_filepath` is the documented exception, published absolute like
# `get_session_info.current_filepath` so a client can compare it against the roots.
serialized = json.dumps({key: value for key, value in report.items() if key != "blend_filepath"})
assert work not in serialized, "a reply carried the temporary directory"
assert str(shot_directory) not in serialized
assert by_name["Loose Texture"]["path"] == "external_texture.png"

# Delete the file the image points at: the same reference must now read MISSING.
external.unlink()
after = server.inspect_delivery(scene.name)
gone = {entry["name"]: entry for entry in after["entries"]}["Loose Texture"]
assert gone["verdict"] == "MISSING", gone
assert after["portable"] is False

# The render output template is part of delivery, and a // template is portable.
scene.render.filepath = "//renders/sh010_"
output = [entry for entry in server.inspect_delivery(scene.name)["entries"] if entry["kind"] == "RENDER_OUTPUT"]
assert len(output) == 1, output
assert output[0]["verdict"] == "RELATIVE_OK", output[0]
assert output[0]["path"] == "//renders/sh010_"

# Paging names the next page and `total` counts every reference, not the page.
full = server.inspect_delivery(scene.name)
first = server.inspect_delivery(scene.name, limit=1, offset=0)
assert first["truncated"] is True
assert first["next_offset"] == 1
assert first["total"] == full["total"] == len(full["entries"])
assert first["entries"] == full["entries"][:1]

# Hashing linked files is refused without configured roots, whatever the scene holds.
try:
    server.inspect_delivery(scene.name, hash_libraries=True)
except ValueError as error:
    assert "BLENDERMCP_FILE_ROOTS" in str(error), str(error)
else:
    raise AssertionError("hash_libraries was accepted with no file roots configured")

print("DELIVERY_SMOKE_OK")
