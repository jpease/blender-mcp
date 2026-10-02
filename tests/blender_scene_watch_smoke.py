# ruff: file-ignore[module-import-not-at-top-of-file]
"""
Run with Blender 5.1+ to prove each reply says when the scene changed between two commands.

Every command goes through `execute_command_internal`, the entry point the socket server calls,
and the handlers are the ones `register()` attaches. Headless Blender has no event loop, so the
re-evaluation a window would run after a command or an edit is `view_layer.update()` here.

1. An undo between commands is reported.
2. A direct edit after the quiet period is reported, naming the object.
3. A mutating command's own re-evaluation right after it answers is not.
4. `set_scene_frame` is not.
5. `open_shot` is not, and resets a pending notice; the handlers survive the load.
"""

import sys
import tempfile
import time

from pathlib import Path

import bpy

from bpy.app.handlers import persistent

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

addon = load_addon("blender_mcp_scene_watch_smoke")

from blender_mcp_scene_watch_smoke.server_core import BlenderMCPServer

watch = addon.scene_watch
UNDO_NOTICE = "The scene was undone once since the last command - re-inspect anything you read before."

# How many times `depsgraph_update_post` fired, so a "no warning" case can prove an update
# actually happened outside the command rather than passing because nothing fired.
_fired = {"count": 0}


@persistent
def _count_updates(_scene, _depsgraph) -> None:
    _fired["count"] += 1


def _run(server, command, **params):
    response = server.execute_command_internal({"type": command, "params": params})
    assert response["status"] == "success", (command, response)
    return response["result"]


def _notices(server):
    """Ask a read-only question and return the change notices its reply carries."""
    result = _run(server, "get_object_info", name="Cube")
    return [warning for warning in result.get("warnings", []) if "since the last command" in warning]


def _wait_out_the_quiet_period() -> None:
    time.sleep(watch.QUIET_PERIOD_SECONDS + 0.1)


def _mutate_with_an_undo_step(server, location) -> None:
    """
    Move the Cube through a command, bracketed by undo steps.

    Background Blender starts with undo disabled and the add-on skips its checkpoint there, so
    the steps a window's undo stack would hold are pushed here.
    """
    bpy.ops.ed.undo_push(message="before")
    _run(server, "set_object_transform", object_name="Cube", patch={"location": location})
    bpy.ops.ed.undo_push(message="after")


def case_undo(server) -> None:
    _mutate_with_an_undo_step(server, [1.0, 0.0, 0.0])
    assert _notices(server) == []
    assert bpy.ops.ed.undo() == {"FINISHED"}
    assert _notices(server) == [UNDO_NOTICE]
    assert _notices(server) == [], "the notice is given once"


def case_direct_edit(server) -> None:
    _notices(server)
    _wait_out_the_quiet_period()
    bpy.data.objects["Cube"].location.z += 2.0
    bpy.context.view_layer.update()
    assert _notices(server) == [
        "The scene was edited outside this session since the last command (Cube) - "
        "re-read them before relying on earlier values."
    ]


def case_own_reevaluation(server) -> None:
    _notices(server)
    _wait_out_the_quiet_period()
    before = _fired["count"]
    # A command that does not push an undo step leaves its edit for the next evaluation, which
    # a window runs a fraction of a millisecond after the command answered.
    _run(server, "set_object_visibility", object_name="Cube", hide_render=True)
    bpy.context.view_layer.update()
    assert _fired["count"] > before, "nothing re-evaluated after the command, so this case proves nothing"
    assert _notices(server) == []


def case_set_scene_frame(server) -> None:
    _notices(server)
    cube = bpy.data.objects["Cube"]
    cube.keyframe_insert("location", frame=1)
    cube.location.x += 3.0
    cube.keyframe_insert("location", frame=20)
    bpy.context.view_layer.update()
    _wait_out_the_quiet_period()
    _notices(server)
    before = _fired["count"]
    _run(server, "set_scene_frame", frame=10)
    # Even an evaluation long after the command finds nothing: `frame_set` evaluates the new
    # frame through the frame-change path, which fires no `depsgraph_update_post`.
    _wait_out_the_quiet_period()
    bpy.context.view_layer.update()
    assert abs(bpy.data.objects["Cube"].matrix_world.translation.x) > 1e-6, "the frame did not move the Cube"
    assert _fired["count"] == before, "set_scene_frame left an update behind for the next evaluation"
    assert _notices(server) == []


def case_open_shot(server, blend_path: Path) -> None:
    _run(server, "save_shot", filepath=str(blend_path))
    _mutate_with_an_undo_step(server, [0.0, 4.0, 0.0])
    assert bpy.ops.ed.undo() == {"FINISHED"}
    opened = _run(server, "open_shot", filepath=str(blend_path), discard_unsaved=True)
    assert not [w for w in opened.get("warnings", []) if "since the last command" in w], opened
    assert _notices(server) == []
    # The handlers are persistent: the reopened file is still watched.
    _mutate_with_an_undo_step(server, [0.0, 5.0, 0.0])
    assert bpy.ops.ed.undo() == {"FINISHED"}
    assert _notices(server) == [UNDO_NOTICE]


def main() -> None:
    addon.register()
    bpy.app.handlers.depsgraph_update_post.append(_count_updates)
    server = BlenderMCPServer()
    work = Path(tempfile.mkdtemp(prefix="scene_watch_smoke_"))
    case_undo(server)
    case_direct_edit(server)
    case_own_reevaluation(server)
    case_set_scene_frame(server)
    case_open_shot(server, work / "sh050.blend")
    print("SCENE_WATCH_SMOKE_OK")


main()
