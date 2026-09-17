"""Confirm bpy.app.timers still never fire under --background on 5.2.2."""

import time

import bpy

print("=== BLENDER ===", bpy.app.version_string)
fires = []


def beat() -> float:
    """
    Count a fire and ask to be called again.

    Returns:
        float: The poll interval, which Blender never honours under `--background`.

    """
    fires.append(1)
    return 0.1


bpy.app.timers.register(beat, persistent=True)
print("  registered; is_registered =", bpy.app.timers.is_registered(beat))
deadline = time.monotonic() + 3.0
while time.monotonic() < deadline:
    time.sleep(0.1)
print(f"  after 3.0 s of --background: fires = {len(fires)}")
print("  is_registered still =", bpy.app.timers.is_registered(beat))
