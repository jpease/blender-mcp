"""
Serve the add-on's socket from a background Blender, for `blender-mcp run-calls`.

Run by Blender, never imported: `blender -b --factory-startup [file.blend] --python <this file>`.
Blender's interpreter cannot import the installed `blender_mcp`, so this uses only the standard
library and `bpy`, and loads the add-on package from the directory `run-calls` names.

Read from the environment `run-calls` builds:

- `BLENDERMCP_RUN_CALLS_EMPTY_SCENE`: `1` empties the scene before anything else runs.
- `BLENDERMCP_RUN_CALLS_ADDON`: the add-on package directory.
- `BLENDERMCP_RUN_CALLS_PORT`: the loopback port to serve on.

Prints `BLENDERMCP_RUN_CALLS_READY <port>` once the socket listens, then serves on Blender's
main thread until the process is stopped.
"""

import importlib
import importlib.util
import os
import sys

from types import ModuleType

import bpy

ADDON_MODULE = "blender_mcp_run_calls"
READY_PREFIX = "BLENDERMCP_RUN_CALLS_READY"


def empty_scene() -> None:
    """
    Replace the factory startup scene with an empty one, as `reset_session` does.

    `read_homefile` with factory startup, not `read_factory_settings`, which would also
    unregister add-ons. Raises `RuntimeError` on failure, which ends Blender before it serves.
    """
    bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True, load_ui=False)


def load_server_core(addon_dir: str) -> ModuleType:
    """
    Load the add-on package from `addon_dir` and return its `server_core` module.

    Args:
        addon_dir: The add-on package directory, holding its `__init__.py`.

    Returns:
        The add-on's `server_core` module.

    Raises:
        RuntimeError: If `addon_dir` holds no loadable package.

    """
    spec = importlib.util.spec_from_file_location(
        ADDON_MODULE, os.path.join(addon_dir, "__init__.py"), submodule_search_locations=[addon_dir]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the add-on package at {addon_dir}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[ADDON_MODULE] = module
    spec.loader.exec_module(module)
    return importlib.import_module(f"{ADDON_MODULE}.server_core")


def announce_ready(port: int) -> None:
    """Tell `run-calls` the socket is listening."""
    print(f"{READY_PREFIX} {port}", flush=True)


def main() -> None:
    """Empty the scene when asked, load the add-on, and serve until stopped."""
    if os.environ.get("BLENDERMCP_RUN_CALLS_EMPTY_SCENE") == "1":
        empty_scene()
    server_core = load_server_core(os.environ["BLENDERMCP_RUN_CALLS_ADDON"])
    server = server_core.BlenderMCPServer(host="127.0.0.1", port=int(os.environ["BLENDERMCP_RUN_CALLS_PORT"]))
    server.serve_in_background(announce_ready)


main()
