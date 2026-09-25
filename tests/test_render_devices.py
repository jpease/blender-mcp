"""
Regression coverage for the device Cycles actually renders on, as every reply that states one reports it.

`scene.cycles.device = "GPU"` is a request. Cycles honours it only when the Cycles add-on's
Preferences select a backend and tick one of its devices; otherwise it renders on the CPU in
silence, and a reply echoing the request told an agent it had a GPU render it never got.
"""

import importlib
import types

import pytest

from conftest import load_addon


def _device(name, device_type, *, use, device_id=None):
    return types.SimpleNamespace(name=name, type=device_type, id=device_id or f"{device_type}_{name}", use=use)


class _CyclesPreferences:
    """`CyclesPreferences` as its Python API behaves on 5.2: a backend's devices, then the CPU."""

    def __init__(self, backend, devices, *, broken=False):
        self.compute_device_type = backend
        self.devices = devices
        self.broken = broken

    def get_devices_for_type(self, backend):
        if self.broken:
            raise RuntimeError("the driver failed to enumerate")
        matching = [device for device in self.devices if device.type == backend]
        if not matching:
            return []
        return [*matching, *(device for device in self.devices if device.type == "CPU")]


def _devices_module(monkeypatch, preferences):
    addon, fake_bpy = load_addon(monkeypatch, data={})
    addons = {} if preferences is None else {"cycles": types.SimpleNamespace(preferences=preferences)}
    fake_bpy.context.preferences = types.SimpleNamespace(addons=addons)
    return importlib.import_module(f"{addon.__name__}.render_devices")


def _scene(engine="CYCLES", device="GPU"):
    return types.SimpleNamespace(
        render=types.SimpleNamespace(engine=engine), cycles=types.SimpleNamespace(device=device)
    )


def _cpu(use=False):
    return _device("Host CPU", "CPU", use=use)


def test_a_gpu_request_with_no_backend_selected_renders_on_the_cpu_and_says_so(monkeypatch) -> None:
    devices = _devices_module(monkeypatch, _CyclesPreferences("NONE", [_device("RTX", "OPTIX", use=True), _cpu()]))

    effective, warning = devices.effective_cycles_device(_scene())

    assert effective == "CPU"
    assert warning is not None
    assert "select no GPU backend" in warning
    assert "renders on the CPU" in warning


def test_a_gpu_request_with_no_ticked_device_of_the_backend_renders_on_the_cpu(monkeypatch) -> None:
    """A ticked CPU beside an unticked GPU is a CPU render, which is Cycles' own `has_active_device` rule."""
    preferences = _CyclesPreferences("OPTIX", [_device("RTX", "OPTIX", use=False), _cpu(use=True)])
    devices = _devices_module(monkeypatch, preferences)

    effective, warning = devices.effective_cycles_device(_scene())

    assert effective == "CPU"
    assert warning is not None
    assert "enable no OPTIX device" in warning


def test_a_gpu_request_with_a_ticked_backend_device_is_a_gpu_render(monkeypatch) -> None:
    devices = _devices_module(monkeypatch, _CyclesPreferences("METAL", [_device("M4", "METAL", use=True), _cpu()]))

    assert devices.effective_cycles_device(_scene()) == ("GPU", None)


def test_a_cpu_request_never_warns_even_without_a_gpu(monkeypatch) -> None:
    devices = _devices_module(monkeypatch, _CyclesPreferences("NONE", []))

    assert devices.effective_cycles_device(_scene(device="CPU")) == ("CPU", None)


def test_another_engine_has_no_cycles_device(monkeypatch) -> None:
    devices = _devices_module(monkeypatch, _CyclesPreferences("NONE", []))

    assert devices.effective_cycles_device(_scene(engine="BLENDER_EEVEE")) == (None, None)


@pytest.mark.parametrize(
    "preferences",
    [None, _CyclesPreferences("CUDA", [_device("RTX", "CUDA", use=True)], broken=True)],
    ids=["cycles-disabled", "driver-failure"],
)
def test_no_readable_device_is_a_cpu_render_and_never_an_error(monkeypatch, preferences) -> None:
    devices = _devices_module(monkeypatch, preferences)

    report = devices.cycles_device_report()
    effective, warning = devices.effective_cycles_device(_scene())

    assert report["enabled_devices"] == []
    assert report["available_devices"] == []
    assert effective == "CPU"
    assert warning is not None


def test_the_report_names_the_backend_and_its_ticked_devices(monkeypatch) -> None:
    preferences = _CyclesPreferences(
        "CUDA", [_device("RTX A", "CUDA", use=True), _device("RTX B", "CUDA", use=False), _cpu(use=True)]
    )
    devices = _devices_module(monkeypatch, preferences)

    report = devices.cycles_device_report()

    assert report["compute_device_type"] == "CUDA"
    assert report["enabled_devices"] == [{"name": "RTX A", "type": "CUDA"}, {"name": "Host CPU", "type": "CPU"}]
    assert report["available_devices"] == [
        {"name": "RTX A", "type": "CUDA", "use": True},
        {"name": "RTX B", "type": "CUDA", "use": False},
        {"name": "Host CPU", "type": "CPU", "use": True},
    ]


def test_the_report_lists_each_device_once_and_stays_bounded(monkeypatch) -> None:
    """Every backend's list repeats the CPU; a render node's many GPUs must not overrun the reply cap."""
    gpus = [_device(f"GPU {index}", "CUDA", use=True) for index in range(20)]
    preferences = _CyclesPreferences("CUDA", [*gpus, _device("Arc", "ONEAPI", use=False), _cpu()])
    devices = _devices_module(monkeypatch, preferences)

    report = devices.cycles_device_report()

    assert len(report["enabled_devices"]) == devices.MAX_REPORTED_DEVICES
    assert len(report["available_devices"]) == devices.MAX_REPORTED_DEVICES
    keys = [(record["type"], record["name"]) for record in report["available_devices"]]
    assert len(keys) == len(set(keys))


def _lighting_quality_handler(monkeypatch, preferences, *, device):
    addon, fake_bpy = load_addon(monkeypatch, data={"scenes": {}})
    fake_bpy.context.preferences = types.SimpleNamespace(
        addons={"cycles": types.SimpleNamespace(preferences=preferences)}
    )
    module = importlib.import_module(f"{addon.__name__}.handlers.lighting.rendering")
    # Engine discovery walks RenderEngine subclasses, which a stub runtime has none of.
    monkeypatch.setattr(module, "resolve_engine", lambda *_args, **_kwargs: "CYCLES")
    device_enum = types.SimpleNamespace(
        type="ENUM", enum_items=[types.SimpleNamespace(identifier="CPU"), types.SimpleNamespace(identifier="GPU")]
    )
    cycles = types.SimpleNamespace(
        samples=64,
        device=device,
        bl_rna=types.SimpleNamespace(properties={"device": device_enum, "samples": types.SimpleNamespace(type="INT")}),
    )
    scene = types.SimpleNamespace(name="Shot", render=types.SimpleNamespace(engine="CYCLES"), cycles=cycles)
    fake_bpy.data.scenes["Shot"] = scene
    return module.LightingRenderHandlers()


@pytest.mark.parametrize("detail", [False, True])
def test_configure_lighting_quality_writes_a_gpu_request_but_reports_the_cpu_render(monkeypatch, detail) -> None:
    """A .blend may be bound for a GPU farm, so the request is written - and the fallback is said aloud."""
    handler = _lighting_quality_handler(monkeypatch, _CyclesPreferences("NONE", []), device="CPU")

    reply = handler.configure_lighting_quality("Shot", "CYCLES", cycles={"device": "GPU"}, detail=detail)

    assert reply["changed"] == ["cycles.device"]
    assert reply["effective_cycles_device"] == "CPU"
    assert len(reply["warnings"]) == 1
    assert "renders on the CPU" in reply["warnings"][0]


def test_configure_lighting_quality_warns_on_any_cycles_patch_while_a_gpu_request_falls_back(monkeypatch) -> None:
    """A FINAL preset on a GPU-less machine is a long CPU render; the notice must not wait for a device patch."""
    handler = _lighting_quality_handler(monkeypatch, _CyclesPreferences("NONE", []), device="GPU")

    reply = handler.configure_lighting_quality("Shot", "CYCLES", cycles={"samples": 128})

    assert reply["effective_cycles_device"] == "CPU"
    assert len(reply["warnings"]) == 1


def test_configure_lighting_quality_is_silent_about_a_gpu_it_can_use(monkeypatch) -> None:
    preferences = _CyclesPreferences("METAL", [_device("M4", "METAL", use=True)])
    handler = _lighting_quality_handler(monkeypatch, preferences, device="CPU")

    reply = handler.configure_lighting_quality("Shot", "CYCLES", cycles={"device": "GPU"})

    assert reply["effective_cycles_device"] == "GPU"
    assert "warnings" not in reply
