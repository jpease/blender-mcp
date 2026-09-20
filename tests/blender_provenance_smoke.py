# ruff: file-ignore[module-import-not-at-top-of-file]
"""Run with Blender 5.1+ to prove the provenance block survives a real save and reopen."""

import importlib.util
import json
import sys
import tempfile

from pathlib import Path

import bpy

addon_path = Path(__file__).resolve().parents[1] / "src" / "blender_mcp" / "bundled" / "addon" / "__init__.py"
package_name = "blender_mcp_provenance_smoke"
spec = importlib.util.spec_from_file_location(
    package_name,
    addon_path,
    submodule_search_locations=[str(addon_path.parent)],
)
assert spec is not None
addon = importlib.util.module_from_spec(spec)
sys.modules[package_name] = addon
spec.loader.exec_module(addon)

from blender_mcp_provenance_smoke.server_core import BlenderMCPServer

server = BlenderMCPServer()
addon.session.register_handlers()

work = Path(tempfile.mkdtemp(prefix="provenance_smoke_"))
blend_path = work / "sh030.blend"

# Author an action the way a command would: create it, then record it in the ledger the
# dispatch path writes to. The save must carry that authorship into the file.
action = bpy.data.actions.new("Smoke Authored Action")
addon.authored.record([{"collection": "actions", "name": action.name}])

saved = server.save_shot(filepath=str(blend_path))
assert saved["provenance_written"] is True, saved
assert saved["provenance_ingredients"] == 0, saved
assert blend_path.is_file()

# The reopen is the point: everything in this plan's context passed because nothing ever
# read the artefact back.
bpy.ops.wm.open_mainfile(filepath=str(blend_path), load_ui=False, use_scripts=False)
assert bpy.data.filepath == str(blend_path)

stored = bpy.context.scene["blender_mcp"]
assert isinstance(stored, str), type(stored)
block = json.loads(stored)
assert block["claim_generator"].startswith("blender-mcp/"), block
assert block["blender_version"] == bpy.app.version_string
assert block["actions"][0]["action"] == "c2pa.edited"
assert block["actions"][0]["datablocks"] == ["actions:Smoke Authored Action"], block["actions"][0]
assert block["ingredients"] == []

# Loading replaced the database, so the ledger must no longer claim the old file's work.
assert addon.authored.snapshot() == []

# inspect_delivery reads the same block back out of the reopened file.
report = server.inspect_delivery(bpy.context.scene.name)
assert report["provenance"]["present"] is True
assert report["provenance"]["valid"] is True
assert report["provenance"]["claim_generator"] == block["claim_generator"]
assert report["provenance"]["actions"][0]["datablocks"] == ["actions:Smoke Authored Action"]

# A block a stranger could have written is reported as invalid rather than repeated verbatim.
bpy.context.scene["blender_mcp"] = "not json at all"
hostile = server.inspect_delivery(bpy.context.scene.name)["provenance"]
assert hostile == {"present": True, "valid": False, "reason": "unparseable"}, hostile
assert json.dumps(server.inspect_delivery(bpy.context.scene.name))

# Declining the block writes nothing, which is what a byte-stable canon publish needs. The
# scene is cleared first, so "absent after the reopen" can only mean the save wrote none.
del bpy.context.scene["blender_mcp"]  # pyright: ignore[reportArgumentType] - bpy's stub types __delitem__ for sequence indexing and loses the ID custom-property protocol
plain_path = work / "sh030_plain.blend"
plain = server.save_shot(filepath=str(plain_path), write_provenance=False)
assert plain["provenance_written"] is False
assert "provenance_ingredients" not in plain
bpy.ops.wm.open_mainfile(filepath=str(plain_path), load_ui=False, use_scripts=False)
assert "blender_mcp" not in bpy.context.scene
assert server.inspect_delivery(bpy.context.scene.name)["provenance"] is None

print("PROVENANCE_SMOKE_OK")
