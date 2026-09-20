"""
List every writable render/eevee/cycles/view-settings property, so unreachable ones can be named.

`scripts/render_coverage.py` diffs this transcript against the routes in
`src/blender_mcp/bundled/addon/render_properties.py`. Owners are printed in a fixed order and
their identifiers sorted, so a Blender upgrade shows up as added or removed lines rather than
as reordering. An owner this build does not have prints `(absent)` instead of vanishing.

`frame_range` records Blender's factory-startup default, which `render_scene`'s guard treats as
"nobody chose this range". Nothing else in the repository proves that pair.
"""

import bpy

_OWNERS = (
    "scene.render",
    "scene.render.image_settings",
    "scene.eevee",
    "scene.eevee.ray_tracing_options",
    "scene.cycles",
    "scene.view_settings",
)

print("=== BLENDER ===", bpy.app.version_string)
scene = bpy.context.scene
print(f"frame_range: {scene.frame_start} {scene.frame_end}")

for owner_key in _OWNERS:
    print(f"owner: {owner_key}")
    owner = scene
    for part in owner_key.split(".")[1:]:
        owner = getattr(owner, part, None)
        if owner is None:
            break
    if owner is None or not hasattr(owner, "bl_rna"):
        print("  (absent)")
        continue
    identifiers = sorted(
        prop.identifier
        for prop in owner.bl_rna.properties
        if not prop.is_readonly and not prop.is_hidden and prop.identifier != "rna_type"
    )
    if not identifiers:
        print("  (absent)")
        continue
    for identifier in identifiers:
        print(f"  {identifier}")
