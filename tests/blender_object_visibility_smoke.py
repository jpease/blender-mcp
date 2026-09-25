"""
Run with Blender 5.1+ to smoke-test set_object_visibility as the durable alternative to remove_scene_objects.

Deleting a library-override object does not survive Blender's own liboverride resync: the next
time a file linking the same library opens, Blender recreates any override object still present
in the linked source but missing locally, silently undoing the deletion. set_object_visibility
changes a property instead of removing an ID, so it survives that resync. This script proves both
halves against a real linked-and-overridden object, not a fake.
"""

import sys
import tempfile

from pathlib import Path

import bpy

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

load_addon("blender_mcp_object_visibility_smoke")

from blender_mcp_object_visibility_smoke.handlers.linking import (  # ruff: ignore[module-import-not-at-top-of-file]
    LinkingHandlersMixin,
)
from blender_mcp_object_visibility_smoke.handlers.scene import (  # ruff: ignore[module-import-not-at-top-of-file]
    SceneHandlersMixin,
)


def _triangle_geometry():
    return {"kind": "MESH", "vertices": [(0, 0, 0), (1, 0, 0), (0, 1, 0)], "edges": [], "faces": [(0, 1, 2)]}


def main() -> None:
    """Prove set_object_visibility survives a liboverride resync where remove_scene_objects does not."""
    handler = SceneHandlersMixin()

    # A plain (non-override) object: set_object_visibility just flips the three flags, and
    # remove_scene_objects carries on removing it with no override warning.
    created = handler.create_geometry_object("Visibility Smoke Plain", _triangle_geometry())
    result = handler.set_object_visibility(created["name"], hide_render=True, hide_viewport=True)
    assert result == {
        "name": created["name"],
        "hide_render": True,
        "hide_viewport": True,
        "hide_select": False,
        "changed_objects": [created["name"]],
    }
    obj = bpy.data.objects[created["name"]]
    assert obj.hide_render is True
    assert obj.hide_viewport is True
    assert obj.hide_select is False

    try:
        handler.set_object_visibility(created["name"])
    except ValueError as exc:
        assert "at least one" in str(exc)
    else:
        raise AssertionError("set_object_visibility accepted zero flags")

    removed_plain = handler.remove_scene_objects([created["name"]], confirm_remove=True)
    assert removed_plain["warnings"] == []
    assert bpy.data.objects.get(created["name"]) is None

    # Build a tiny library source: one collection holding one object, saved to disk.
    library_collection = bpy.data.collections.new("Visibility Smoke Library Collection")
    bpy.context.scene.collection.children.link(library_collection)
    mesh = bpy.data.meshes.new("Visibility Smoke Library Mesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    mesh.update()
    library_object = bpy.data.objects.new("Visibility Smoke Library Object", mesh)
    library_collection.objects.link(library_object)
    library_collection_name = library_collection.name
    library_object_name = library_object.name

    with tempfile.TemporaryDirectory() as tmp:
        library_path = Path(tmp).resolve() / "visibility_smoke_library.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(library_path), copy=True)

        # A fresh scene: the file just saved above becomes an external library to link and
        # override, the same shape a real session's canon library takes.
        bpy.ops.wm.read_homefile(use_empty=True)
        LinkingHandlersMixin.link_canon_library(
            filepath=str(library_path),
            collections=[library_collection_name],
            as_override=True,
        )
        overridden = bpy.data.objects[library_object_name]
        assert overridden.override_library is not None, "expected a library-override object"

        # The guard: deleting it outright is refused without the extra confirmation, and nothing
        # about the scene changes when it is.
        try:
            handler.remove_scene_objects([library_object_name], confirm_remove=True)
        except ValueError as exc:
            assert "confirm_override_removal" in str(exc)
        else:
            raise AssertionError("remove_scene_objects deleted a library-override object unconfirmed")
        assert bpy.data.objects.get(library_object_name) is not None, "a refused removal must change nothing"

        # The durable alternative: hiding it works, and it is still there afterward.
        hidden = handler.set_object_visibility(library_object_name, hide_render=True, hide_viewport=True)
        assert hidden["hide_render"] is True
        assert bpy.data.objects[library_object_name].hide_render is True

        # Confirmed, the deletion is allowed -- and the reply says the deletion will not last.
        removed_override = handler.remove_scene_objects(
            [library_object_name], confirm_remove=True, confirm_override_removal=True
        )
        assert library_object_name in removed_override["removed"]["names"]
        assert any("liboverride resync" in warning for warning in removed_override["warnings"])
        # The linked (non-override) source datablock is untouched and may still answer to this
        # name; only the local override itself must be gone, which is the object this test made.
        survivor = bpy.data.objects.get(library_object_name)
        assert survivor is None or survivor.override_library is None, "the override was not removed"

    print("OBJECT_VISIBILITY_SMOKE_OK")


if __name__ == "__main__":
    main()
