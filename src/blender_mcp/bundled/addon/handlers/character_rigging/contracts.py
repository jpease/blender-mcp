"""
Reply prose the rigging handlers publish, kept where something outside Blender can read it.

No `bpy`, no relative imports, nothing but literals: `scripts/measure_reply_sizes.py` loads
this file directly to size a representative `sample_deformed_geometry` reply, and it runs in a
plain interpreter where importing the add-on package would fail on `import bpy`. Holding the
strings here is what stops that script's copy of them drifting from the ones actually sent -
a drift that shows up as a reply-budget number measured against text nobody ships.
"""

# What a `sample_deformed_geometry` reply discloses about its own limits. Both are conclusions
# a caller would otherwise have to reach by being wrong first: that evaluated indices are not
# base-mesh indices once a generative modifier is in the stack, and that a bounded sample can
# miss motion it did not look at.
DEFORMED_SAMPLE_LIMITATIONS: tuple[str, ...] = (
    "Indices are the evaluated mesh's own. They equal base-mesh indices only while "
    "index_correspondence is BASE_MESH; a generative modifier (Subdivision, Mirror, "
    "Array) makes them a different numbering, and displacement is then unmeasurable.",
    "displacement compares the evaluated surface against the base mesh placed by the "
    "object's own transform, over an evenly spread bounded sample - a rig that moves "
    "only unsampled vertices can still report maximum_m near zero unless complete is true.",
)
