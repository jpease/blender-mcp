# ruff: file-ignore[module-import-not-at-top-of-file]
"""Run with Blender 5.1+ to smoke-test inspect_delivery against real datablocks and a real save."""

import json
import os
import sys
import tempfile

from pathlib import Path

import bpy

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

load_addon("blender_mcp_delivery_smoke")

from blender_mcp_delivery_smoke.server_core import BlenderMCPServer

server = BlenderMCPServer()
scene = bpy.context.scene

# Publication is judged against the file roots when they are set; this run starts with
# none, so the tree a path must lie in to be published whole is the shot's own directory.
os.environ.pop("BLENDERMCP_FILE_ROOTS", None)
os.environ.pop("BLENDERMCP_OUTPUT_ROOTS", None)
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
# A reduced path says so, and says why: the texture sits beside the shot's directory, not in it.
assert by_name["Loose Texture"]["path"] == "external_texture.png"
assert by_name["Loose Texture"]["path_redacted"] is True
assert by_name["Loose Texture"]["path_redaction_reason"] == "OUTSIDE_ROOTS"
assert by_name["Packed Texture"]["path_redacted"] is True

# Delete the file the image points at: the same reference must now read MISSING.
external.unlink()
after = server.inspect_delivery(scene.name)
gone = {entry["name"]: entry for entry in after["entries"]}["Loose Texture"]
assert gone["verdict"] == "MISSING", gone
assert after["portable"] is False
# Redaction and breakage are independent: the path was already a leaf while the file existed.
assert gone["path_redacted"] is True, gone
assert gone["path"] == by_name["Loose Texture"]["path"], "the leaf changed when the file went missing"

# The render output template is part of delivery, and a // template is portable.
scene.render.filepath = "//renders/sh010_"
output = [entry for entry in server.inspect_delivery(scene.name)["entries"] if entry["kind"] == "RENDER_OUTPUT"]
assert len(output) == 1, output
assert output[0]["verdict"] == "RELATIVE_OK", output[0]
assert output[0]["path"] == "//renders/sh010_"
assert output[0]["path_redacted"] is False, output[0]
assert output[0]["path_redaction_reason"] is None, output[0]

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

# The reply record the redaction finding named: a library linked by its absolute path
# from inside the shot's own directory is published whole, as a link from the shot,
# because it resolves inside the allowed tree however Blender spelled it.
library_path = shot_directory / "libs" / "canon.blend"
library_path.parent.mkdir()
bpy.ops.wm.save_as_mainfile(filepath=str(library_path), copy=True)
assert bpy.data.filepath == str(blend_path), "save_as_mainfile(copy=True) moved the open file"
# `libraries.load` is a context manager at runtime; bpy's stub declares it returning None.
with bpy.data.libraries.load(str(library_path), link=True) as (data_from, data_to):  # pyright: ignore[reportGeneralTypeIssues]
    data_to.objects = list(data_from.objects)[:1]

linked = server.get_session_info()["libraries"]
assert len(linked) == 1, linked
assert linked[0]["filepath"] == "//libs/canon.blend", linked[0]
assert linked[0]["filepath_redacted"] is False, linked[0]
assert linked[0]["filepath_redaction_reason"] is None, linked[0]
assert linked[0]["is_relative"] is False, "is_relative describes the path as Blender stored it"
assert linked[0]["is_missing"] is False, linked[0]
assert work not in json.dumps(linked), "the library layout leaked"

# A canon folder beside the shot is outside the shot's directory, so without roots it
# is withheld as OUTSIDE_ROOTS; with the work directory as the root it is inside, and
# Blender's own `..` spelling is published whole.
canon_path = Path(work) / "canon" / "set.blend"
canon_path.parent.mkdir()
bpy.ops.wm.save_as_mainfile(filepath=str(canon_path), copy=True)
bpy.data.libraries[0].filepath = "//../canon/set.blend"
beside = server.get_session_info()["libraries"][0]
assert (beside["filepath"], beside["filepath_redaction_reason"]) == ("set.blend", "OUTSIDE_ROOTS"), beside
os.environ["BLENDERMCP_FILE_ROOTS"] = work
try:
    beside = server.get_session_info()["libraries"][0]
    assert beside["filepath"] == "//../canon/set.blend", beside
    assert beside["filepath_redacted"] is False, beside
    beside_entries = [entry for entry in server.inspect_delivery(scene.name)["entries"] if entry["kind"] == "LIBRARY"]
    assert beside_entries[0]["path"] == "//../canon/set.blend", beside_entries[0]
finally:
    del os.environ["BLENDERMCP_FILE_ROOTS"]

# The same library, linked relatively, is published whole and reports no redaction.
bpy.data.libraries[0].filepath = "//libs/canon.blend"
relative_entry = server.get_session_info()["libraries"][0]
assert relative_entry["filepath"] == "//libs/canon.blend", relative_entry
assert relative_entry["filepath_redacted"] is False, relative_entry
assert relative_entry["filepath_redaction_reason"] is None, relative_entry
assert relative_entry["is_relative"] is True, relative_entry

# inspect_delivery reports the same library under its own field name.
library_entries = [entry for entry in server.inspect_delivery(scene.name)["entries"] if entry["kind"] == "LIBRARY"]
assert len(library_entries) == 1, library_entries
assert library_entries[0]["path"] == "//libs/canon.blend", library_entries[0]
assert library_entries[0]["path_redacted"] is False, library_entries[0]

print("DELIVERY_SMOKE_OK")
