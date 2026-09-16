r"""
Measure what each candidate `reset_session` operator does to preferences and to an enabled add-on.

Plan Task 6's command table names `wm.read_factory_settings(use_empty=True)`.
That operator loads factory *preferences* as well as the factory startup file,
and the socket server lives inside an add-on: an operator that disables enabled
add-ons would unregister the addon answering the very command that called it.
This stages a throwaway add-on (so nothing about the real addon is assumed),
enables it, flips `use_scripts_auto_execute` on, and runs each candidate
operator from a clean start, reporting afterwards:

- whether the add-on is still enabled, and whether its `unregister` ran;
- which of `@persistent` `load_pre` / `load_post` / `load_post_fail` fired, and
  whether `load_post` is still attached;
- whether the flipped preference was reset;
- how many objects the resulting scene holds and what `bpy.data.filepath` is.

From the repository root::

    /opt/homebrew/bin/blender --background --factory-startup \
        --python scripts/blender_probes/reset_operator_side_effects.py
"""

import os
import sys
import tempfile

from collections.abc import Callable

import addon_utils
import bpy

from bpy.app.handlers import persistent

ADDON_NAME = "t6_reset_probe_addon"
ADDON_SOURCE = """
bl_info = {"name": "T6 reset probe", "blender": (5, 1, 0), "category": "Development"}
import builtins

def register():
    builtins.T6_EVENTS.append("register")

def unregister():
    builtins.T6_EVENTS.append("unregister")
"""

scripts_dir = tempfile.mkdtemp(prefix="t6_reset_addon_")
with open(os.path.join(scripts_dir, f"{ADDON_NAME}.py"), "w", encoding="utf-8") as handle:
    handle.write(ADDON_SOURCE)
sys.path.insert(0, scripts_dir)

import builtins  # ruff: ignore[module-import-not-at-top-of-file]

builtins.T6_EVENTS = []  # type: ignore[attr-defined]


@persistent
def _on_load_pre(file_path: str = "", _unused: object = None) -> None:
    """
    Record that a load began, and with which path.

    Args:
        file_path: The path Blender reports.
        _unused: Blender's second positional argument.

    """
    builtins.T6_EVENTS.append(f"load_pre({file_path!r})")  # type: ignore[attr-defined]


@persistent
def _on_load_post_fail(file_path: str = "", _unused: object = None) -> None:
    """
    Record that a load failed, and with which path.

    Args:
        file_path: The path Blender reports.
        _unused: Blender's second positional argument.

    """
    builtins.T6_EVENTS.append(f"load_post_fail({file_path!r})")  # type: ignore[attr-defined]


@persistent
def _on_load_post(file_path: str = "", _unused: object = None) -> None:
    """
    Record that a load completed, and with which path.

    Args:
        file_path: The path Blender reports.
        _unused: Blender's second positional argument.

    """
    builtins.T6_EVENTS.append(f"load_post({file_path!r})")  # type: ignore[attr-defined]


def run_case(label: str, operator: Callable[[], set[str]]) -> None:
    """
    Enable the probe add-on, flip the preference, run one reset operator and report.

    Args:
        label: What is printed for this case.
        operator: The reset call under test.

    """
    bpy.ops.wm.read_homefile(use_factory_startup=True)
    addon_utils.enable(ADDON_NAME, default_set=True)
    bpy.context.preferences.filepaths.use_scripts_auto_execute = True
    for list_name, handler in (
        ("load_pre", _on_load_pre),
        ("load_post", _on_load_post),
        ("load_post_fail", _on_load_post_fail),
    ):
        if handler not in getattr(bpy.app.handlers, list_name):
            getattr(bpy.app.handlers, list_name).append(handler)
    builtins.T6_EVENTS.clear()  # type: ignore[attr-defined]
    before = len(bpy.data.objects)
    result = operator()
    enabled = ADDON_NAME in bpy.context.preferences.addons
    print(f"\n=== {label} ===")
    print(f"  result                                  = {sorted(result)}")
    print(f"  objects before -> after                 = {before} -> {len(bpy.data.objects)}")
    print(f"  bpy.data.filepath                       = {bpy.data.filepath!r}")
    print(f"  addon still enabled                     = {enabled}")
    print(f"  events                                  = {builtins.T6_EVENTS}")  # type: ignore[attr-defined]
    print(f"  load_post handler still attached        = {_on_load_post in bpy.app.handlers.load_post}")
    print(
        "  use_scripts_auto_execute after (set True) =",
        bpy.context.preferences.filepaths.use_scripts_auto_execute,
    )
    if enabled:
        addon_utils.disable(ADDON_NAME, default_set=True)


print("=== BLENDER ===", bpy.app.version_string)
rna = bpy.ops.wm.read_homefile.get_rna_type()
print("wm.read_homefile properties:", [(p.identifier, getattr(p, "default", None)) for p in rna.properties][1:])

run_case(
    "wm.read_factory_settings(use_empty=True)",
    lambda: bpy.ops.wm.read_factory_settings(use_empty=True),
)
run_case(
    "wm.read_homefile(use_empty=True, use_factory_startup=True)",
    lambda: bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True),
)
run_case(
    "wm.read_homefile(use_empty=True)",
    lambda: bpy.ops.wm.read_homefile(use_empty=True),
)
run_case(
    "production: wm.read_homefile(use_empty=True, use_factory_startup=True, load_ui=False)",
    lambda: bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True, load_ui=False),
)
