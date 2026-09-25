"""
Regression coverage for get_viewport_screenshot's optional ad hoc view and shading override.

ViewSpec's own validation is pure Pydantic - no bpy needed. The addon-side dispatch tests
below load the real bundled addon package (conftest's load_addon) so
handlers.viewport's relative imports (camera._shared, helpers) resolve, but replace
_look_quaternion with a recording stub: its own geometry is already proven against real
Blender (see the task brief) and mathutils has no real runtime implementation in this test
environment, so faking it would test the fake, not the addon. What belongs here is the
wiring: which branch runs, what gets created and removed, and that a temporary shading
override always restores itself.
"""

import contextlib
import sys
import types

import pytest

from conftest import load_addon
from pydantic import ValidationError

from blender_mcp.server.tools.viewport import ViewSpec


def test_view_spec_requires_exactly_one_source() -> None:
    with pytest.raises(ValidationError, match=r"Supply exactly one of camera_object or eye"):
        ViewSpec()
    with pytest.raises(ValidationError, match=r"Supply exactly one of camera_object or eye"):
        ViewSpec(camera_object="Hero", eye=(1.0, 2.0, 3.0), target_point=(0.0, 0.0, 0.0))


def test_view_spec_eye_requires_exactly_one_target_source() -> None:
    with pytest.raises(ValidationError, match=r"Supply exactly one of target_point or target_object_name"):
        ViewSpec(eye=(1.0, 2.0, 3.0))
    with pytest.raises(ValidationError, match=r"Supply exactly one of target_point or target_object_name"):
        ViewSpec(eye=(1.0, 2.0, 3.0), target_point=(4.0, 5.0, 6.0), target_object_name="Hero")


def test_view_spec_rejects_degenerate_eye_and_target() -> None:
    """Mirrors _look_quaternion's own <= 1e-16 length_squared guard, but on the raw tuples."""
    with pytest.raises(ValidationError, match=r"eye and target_point cannot occupy the same point"):
        ViewSpec(eye=(1.0, 2.0, 3.0), target_point=(1.0, 2.0, 3.0))


def test_view_spec_accepts_each_valid_source() -> None:
    assert ViewSpec(camera_object="Hero").camera_object == "Hero"
    assert ViewSpec(eye=(1.0, 2.0, 3.0), target_point=(4.0, 5.0, 6.0)).target_point == (4.0, 5.0, 6.0)
    assert ViewSpec(eye=(1.0, 2.0, 3.0), target_object_name="Hero").target_object_name == "Hero"


def test_view_spec_dumps_the_shared_target_spelling_the_addon_reads() -> None:
    """get_viewport_screenshot forwards model_dump() verbatim, so these keys are the wire contract."""
    dumped = ViewSpec(eye=(1.0, 2.0, 3.0), target_point=(4.0, 5.0, 6.0)).model_dump()

    assert dumped["target_point"] == (4.0, 5.0, 6.0)
    assert dumped["target_object_name"] is None


def test_view_spec_rejects_lens_mm_override_with_camera_object() -> None:
    with pytest.raises(ValidationError, match=r"lens_mm has no effect when camera_object is given"):
        ViewSpec(camera_object="Hero", lens_mm=35.0)


class _FakeMatrix:
    def __init__(self, tag: str) -> None:
        self.tag = tag

    def inverted(self):
        return f"view_matrix({self.tag})"


class _FakeCameraObject:
    """
    Stand-in for a bpy.types.Object carrying camera data.

    matrix_world/matrix_basis/calc_matrix_camera are fixed fakes, not real geometry: the
    wiring tests below check *which* object and *which* of its two transform matrices these
    were read off and what they were passed through as, not whether the numbers are correct
    (proven separately against real Blender).

    The two matrices are deliberately distinguishable. An object that is in no view layer is
    in no depsgraph, so real Blender leaves its matrix_world at the identity however its
    location/rotation were set, and only matrix_basis reflects them - a fake whose two
    matrices answered alike would pass whichever one the addon read.
    """

    def __init__(self, name, data=None) -> None:
        self.name = name
        self.type = "CAMERA"
        self.data = data if data is not None else object()
        self.matrix_world = _FakeMatrix(name)
        self.matrix_basis = _FakeMatrix(f"basis:{name}")
        self.location = None
        self.rotation_mode = None
        self.rotation_quaternion = None
        self.calc_matrix_camera_calls: list[tuple] = []

    def calc_matrix_camera(self, depsgraph, x, y, scale_x, scale_y):
        self.calc_matrix_camera_calls.append((depsgraph, x, y, scale_x, scale_y))
        return f"window_matrix({self.name})"


class _RecordingCollection:
    """Stand-in for one bpy.data.* collection: records every .new()/.remove() call."""

    def __init__(self, factory) -> None:
        self._factory = factory
        self._items: dict[str, object] = {}
        self.created: list[object] = []
        self.removed: list[object] = []

    def new(self, name, *args, **kwargs):
        obj = self._factory(name, *args, **kwargs)
        self._items[name] = obj
        self.created.append(obj)
        return obj

    def remove(self, db, do_unlink=True) -> None:
        self.removed.append(db)
        name = getattr(db, "name", None)
        if isinstance(name, str):
            self._items.pop(name, None)

    def get(self, name, default=None):
        return self._items.get(name, default)

    def __setitem__(self, name, db) -> None:
        self._items[name] = db

    def __getitem__(self, name):
        return self._items[name]


def _viewport_module(monkeypatch: pytest.MonkeyPatch, *, objects=None, cameras=None):
    objects = objects if objects is not None else _RecordingCollection(_FakeCameraObject)
    cameras = (
        cameras
        if cameras is not None
        else _RecordingCollection(lambda name: types.SimpleNamespace(name=name, lens=None))
    )
    addon, bpy = load_addon(monkeypatch, data={"objects": objects, "cameras": cameras})
    bpy.context.view_layer = types.SimpleNamespace(update=lambda: None)
    bpy.context.evaluated_depsgraph_get = lambda: "DEPSGRAPH"
    bpy.context.scene.render = types.SimpleNamespace(resolution_x=640, resolution_y=480)
    monkeypatch.setattr(sys.modules["mathutils"], "Vector", tuple, raising=False)
    module = sys.modules[f"{addon.__name__}.handlers.viewport"]
    return module, bpy, objects, cameras


def test_camera_object_view_uses_the_named_camera_without_creating_one(monkeypatch: pytest.MonkeyPatch) -> None:
    objects = _RecordingCollection(_FakeCameraObject)
    hero_cam = _FakeCameraObject("HeroCam")
    objects["HeroCam"] = hero_cam
    module, bpy, objects, cameras = _viewport_module(monkeypatch, objects=objects)

    view_matrix, window_matrix, view_source = module._synthetic_view_matrices(
        {"camera_object": "HeroCam"}, bpy.context.scene
    )

    assert view_source == "camera_object"
    assert view_matrix == "view_matrix(HeroCam)"
    assert window_matrix == "window_matrix(HeroCam)"
    assert hero_cam.calc_matrix_camera_calls == [("DEPSGRAPH", 640, 480, 1.0, 1.0)]
    assert objects.created == []  # no temporary camera was made
    assert cameras.created == []


def test_eye_target_view_builds_and_cleans_up_a_temporary_camera(monkeypatch: pytest.MonkeyPatch) -> None:
    module, bpy, objects, cameras = _viewport_module(monkeypatch)
    quat_calls = []
    monkeypatch.setattr(
        module, "_look_quaternion", lambda origin, target: quat_calls.append((origin, target)) or "QUAT"
    )

    view_matrix, window_matrix, view_source = module._synthetic_view_matrices(
        {"eye": (1.0, 2.0, 3.0), "target_point": (4.0, 5.0, 6.0), "lens_mm": 35.0}, bpy.context.scene
    )

    assert view_source == "eye_target"
    assert quat_calls == [((1.0, 2.0, 3.0), (4.0, 5.0, 6.0))]
    assert len(objects.created) == 1
    temp_cam = objects.created[0]
    assert temp_cam.location == (1.0, 2.0, 3.0)
    assert temp_cam.rotation_mode == "QUATERNION"
    assert temp_cam.rotation_quaternion == "QUAT"
    assert temp_cam.data.lens == pytest.approx(35.0)
    # matrix_basis, not matrix_world: the throwaway camera is in no depsgraph (see the fake).
    assert view_matrix == f"view_matrix(basis:{temp_cam.name})"
    assert window_matrix == f"window_matrix({temp_cam.name})"
    # Cleaned up before returning: nothing new is left in bpy.data.
    assert objects.removed == [temp_cam]
    assert cameras.removed == [temp_cam.data]


def test_eye_target_object_resolves_the_named_object_as_target(monkeypatch: pytest.MonkeyPatch) -> None:
    objects = _RecordingCollection(_FakeCameraObject)
    objects["Prop"] = types.SimpleNamespace(matrix_world=types.SimpleNamespace(translation=(9.0, 9.0, 9.0)))
    module, bpy, objects, _cameras = _viewport_module(monkeypatch, objects=objects)
    quat_calls = []
    monkeypatch.setattr(
        module, "_look_quaternion", lambda origin, target: quat_calls.append((origin, target)) or "QUAT"
    )

    module._synthetic_view_matrices({"eye": (0.0, 0.0, 0.0), "target_object_name": "Prop"}, bpy.context.scene)

    assert quat_calls == [((0.0, 0.0, 0.0), (9.0, 9.0, 9.0))]


def test_eye_target_temporary_camera_is_removed_even_when_calc_matrix_camera_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, bpy, objects, cameras = _viewport_module(monkeypatch)
    monkeypatch.setattr(module, "_look_quaternion", lambda origin, target: "QUAT")

    class _FailingCameraObject(_FakeCameraObject):
        def calc_matrix_camera(self, *args, **kwargs):
            raise RuntimeError("boom")

    objects._factory = _FailingCameraObject

    with pytest.raises(RuntimeError, match="boom"):
        module._synthetic_view_matrices({"eye": (1.0, 2.0, 3.0), "target_point": (4.0, 5.0, 6.0)}, bpy.context.scene)

    assert len(objects.removed) == 1
    assert len(cameras.removed) == 1


def test_shading_override_none_leaves_shading_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    module, _bpy, _objects, _cameras = _viewport_module(monkeypatch)
    space = types.SimpleNamespace(shading=types.SimpleNamespace(type="SOLID"))

    with module._shading_override(space, None) as shading_mode:
        assert shading_mode == "SOLID"
        assert space.shading.type == "SOLID"
    assert space.shading.type == "SOLID"


def test_shading_override_sets_then_restores_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    module, _bpy, _objects, _cameras = _viewport_module(monkeypatch)
    space = types.SimpleNamespace(shading=types.SimpleNamespace(type="SOLID"))

    with module._shading_override(space, "MATERIAL") as shading_mode:
        assert shading_mode == "MATERIAL"
        assert space.shading.type == "MATERIAL"
    assert space.shading.type == "SOLID"


def test_shading_override_restores_even_when_the_capture_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    module, _bpy, _objects, _cameras = _viewport_module(monkeypatch)
    space = types.SimpleNamespace(shading=types.SimpleNamespace(type="SOLID"))

    with pytest.raises(RuntimeError, match="capture failed"), module._shading_override(space, "MATERIAL"):
        raise RuntimeError("capture failed")

    assert space.shading.type == "SOLID"


class _FakeImage:
    """Image datablock whose save() can be made to fail, as an unwritable filepath makes it."""

    def __init__(self, name, *, failing=False) -> None:
        self.name = name
        self.size = (1600, 900)
        self.filepath_raw = None
        self.file_format = None
        self.pixels = types.SimpleNamespace(foreach_set=lambda _values: None)
        self._failing = failing

    def scale(self, width, height) -> None:
        self.size = (width, height)

    def save(self) -> None:
        if self._failing:
            raise RuntimeError("cannot write image")


class _RecordingImages(_RecordingCollection):
    """bpy.data.images: .new() for the offscreen path, .load() for the window grab."""

    def load(self, filepath):
        return self.new(filepath)


def _images_module(monkeypatch: pytest.MonkeyPatch, *, failing: bool):
    images = _RecordingImages(lambda name, *_args, **_kwargs: _FakeImage(name, failing=failing))
    addon, bpy = load_addon(monkeypatch, data={"images": images})
    bpy.context.view_layer = types.SimpleNamespace(update=lambda: None)
    return sys.modules[f"{addon.__name__}.handlers.viewport"], bpy, images


def _install_offscreen_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Stand in for the gpu/numpy modules _render_offscreen imports inside itself.

    Neither exists outside Blender (numpy is not even a test dependency here), and neither is
    what these two tests are about: the capture's own datablock bookkeeping is.
    """

    class _FakeOffScreen:
        def __init__(self, width, height) -> None:
            self.freed = False

        def draw_view3d(self, *_args, **_kwargs) -> None:
            return None

        @property
        def texture_color(self):
            return types.SimpleNamespace(read=lambda: types.SimpleNamespace(dimensions=0))

        def free(self) -> None:
            self.freed = True

    class _FakePixelArray:
        """Only the two operations _render_offscreen performs on the read buffer."""

        def __truediv__(self, _divisor):
            return self

        def ravel(self):
            return "PIXELS"

    gpu = types.ModuleType("gpu")
    gpu.types = types.SimpleNamespace(GPUOffScreen=_FakeOffScreen)
    numpy = types.ModuleType("numpy")
    numpy.float32 = "float32"
    numpy.asarray = lambda _buf, dtype=None: _FakePixelArray()
    monkeypatch.setitem(sys.modules, "gpu", gpu)
    monkeypatch.setitem(sys.modules, "numpy", numpy)


def test_offscreen_capture_removes_its_image_datablock_when_the_save_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed save must not leave an orphan "mcp_viewport" image in the user's file."""
    module, _bpy, images = _images_module(monkeypatch, failing=True)
    _install_offscreen_stubs(monkeypatch)
    region = types.SimpleNamespace(width=320, height=240)

    with pytest.raises(RuntimeError, match="cannot write image"):
        module._render_offscreen(None, region, "VIEW", "WINDOW", 800, "/tmp/shot.png", "png")

    assert images.removed == images.created
    assert len(images.created) == 1


def test_window_grab_removes_the_loaded_screenshot_when_the_rescale_save_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, bpy, images = _images_module(monkeypatch, failing=True)
    bpy.context.temp_override = lambda **_kwargs: contextlib.nullcontext()
    bpy.ops.screen = types.SimpleNamespace(screenshot_area=lambda filepath: None)

    with pytest.raises(RuntimeError, match="cannot write image"):
        module._window_grab_fallback(None, 800, "/tmp/shot.png", "png")

    assert images.removed == images.created
    assert len(images.created) == 1
