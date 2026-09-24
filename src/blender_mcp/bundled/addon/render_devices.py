"""
Report what Cycles will actually render on, read from Preferences rather than from the scene.

`scene.cycles.device` only asks for a GPU. Whether Cycles gets one is decided by the Cycles add-on's
preferences - its `compute_device_type` backend and which of that backend's devices are ticked - and
when they yield no usable GPU, Cycles renders on the CPU without saying so. A reply that states a
device reads it from here, so none reports the request as if it were the device used.

Nothing here raises: a build without the Cycles add-on, or a driver that fails to enumerate, reads
as "no GPU backend", which is also what Cycles itself falls back to.
"""

from contextlib import suppress

import bpy

# The GPU backends `CyclesPreferences.refresh_devices` walks on Blender 5.2.
_GPU_BACKENDS = ("CUDA", "OPTIX", "HIP", "METAL", "ONEAPI")
# `available_devices` rides in the handshake and in `get_addon_status`, under the 8 KiB reply cap.
MAX_REPORTED_DEVICES = 16


def _cycles_preferences():
    """
    Find the Cycles add-on's preferences.

    Returns:
        The `CyclesPreferences` struct, or None when Cycles is not enabled or the context has no
        preferences to read.

    """
    with suppress(Exception):
        return getattr(bpy.context.preferences.addons.get("cycles"), "preferences", None)
    return None


def _backend(prefs):
    """
    Read the compute backend Preferences selected, the way Cycles reads it.

    Args:
        prefs: The Cycles preferences, or None.

    Returns:
        str: "NONE", "CUDA", "OPTIX", "HIP", "METAL" or "ONEAPI"; an unset value is "NONE".

    """
    with suppress(Exception):
        return str(getattr(prefs, "compute_device_type", "") or "NONE")
    return "NONE"


def _backend_devices(prefs, backend):
    """
    List the device entries Cycles matches for one backend: that backend's devices, then the CPU.

    `get_devices_for_type` also registers devices this machine has that Preferences has not yet
    listed, which is what the Preferences UI does when it draws; it is idempotent.

    Args:
        prefs: The Cycles preferences, or None.
        backend: A GPU backend identifier.

    Returns:
        list: The entries, empty when the backend has no device here or enumeration failed.

    """
    if prefs is None or backend == "NONE":
        return []
    with suppress(Exception):
        return list(prefs.get_devices_for_type(backend))
    return []


def _record(entry, *, with_use=False):
    """
    Serialize one Preferences device entry.

    Args:
        entry: A `CyclesDeviceSettings` entry.
        with_use: Also report whether the entry is ticked.

    Returns:
        dict: "name" and "type", plus "use" when asked.

    """
    record = {"name": str(getattr(entry, "name", "")), "type": str(getattr(entry, "type", ""))}
    if with_use:
        record["use"] = bool(getattr(entry, "use", False))
    return record


def _ticked_devices(prefs, backend):
    """
    List the ticked entries a GPU render uses: the backend's devices, and the CPU when ticked beside one.

    Args:
        prefs: The Cycles preferences, or None.
        backend: The selected backend.

    Returns:
        list: The ticked `CyclesDeviceSettings` entries, empty for backend "NONE".

    """
    return [entry for entry in _backend_devices(prefs, backend) if getattr(entry, "use", False)]


def cycles_device_report():
    """
    Describe the render devices Preferences gives Cycles on this machine.

    Returns:
        dict: "compute_device_type" (the selected backend, "NONE" when there is none or Cycles is
        not enabled); "enabled_devices" (the ticked devices a GPU render uses, as "name"/"type");
        and "available_devices" (every GPU backend's devices this machine has, plus the CPU, as
        "name"/"type"/"use"). Both lists hold at most MAX_REPORTED_DEVICES records.

    """
    prefs = _cycles_preferences()
    backend = _backend(prefs)
    available = []
    seen = set()
    for gpu_backend in _GPU_BACKENDS:
        for entry in _backend_devices(prefs, gpu_backend):
            key = (getattr(entry, "type", None), getattr(entry, "id", None))
            if key in seen:
                continue
            seen.add(key)
            available.append(_record(entry, with_use=True))
    return {
        "compute_device_type": backend,
        "enabled_devices": [_record(entry) for entry in _ticked_devices(prefs, backend)[:MAX_REPORTED_DEVICES]],
        "available_devices": available[:MAX_REPORTED_DEVICES],
    }


def effective_cycles_device(scene):
    """
    Resolve the device Cycles will render this scene on, and say so when it is not the one asked for.

    Cycles honours `cycles.device == "GPU"` only when Preferences selects a backend and ticks at
    least one of that backend's devices; otherwise it renders on the CPU. This mirrors that rule
    rather than echoing the request.

    Args:
        scene: The scene whose render engine and Cycles device request are read.

    Returns:
        tuple[str | None, str | None]: The effective device ("CPU" or "GPU"), None when the scene's
        engine is not Cycles; and a warning when a GPU request falls back to the CPU, else None.

    """
    if getattr(getattr(scene, "render", None), "engine", None) != "CYCLES":
        return None, None
    if getattr(getattr(scene, "cycles", None), "device", "CPU") != "GPU":
        return "CPU", None
    prefs = _cycles_preferences()
    backend = _backend(prefs)
    if any(getattr(entry, "type", None) == backend for entry in _ticked_devices(prefs, backend)):
        return "GPU", None
    selected = "select no GPU backend" if backend == "NONE" else f"enable no {backend} device"
    return "CPU", (
        f"cycles.device is GPU, but Preferences on this machine {selected}, so Cycles renders on the CPU "
        "here. Tick a device under Preferences > System > Cycles Render Devices (get_addon_status lists "
        "them as render_devices), or set cycles.device to CPU with configure_lighting_quality."
    )
