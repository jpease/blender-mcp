# ruff: file-ignore[module-import-not-at-top-of-file]
"""
Run with Blender 5.1+ to prove Poly Haven and Sketchfab fetch off Blender's main thread.

A local `http.server` on 127.0.0.1 stands in for both providers, serving fixtures this
script builds with real `bpy` (a `.blend` holding one mesh, an HDR, two PNG maps, a glTF
in a ZIP, a thumbnail), and the handlers' `API_ROOT`s are pointed at it. Every command
goes through `execute_command_internal`, the entry point the socket server calls, so the
provider gate, the registry and the import's mutation transaction are the real ones.

1. A model download's start returns while the server is still holding the file back;
   the main thread answers another command meanwhile, and the status shows the bytes
   received so far. Released, the download imports the mesh, and the fetch is consumed.
2. An HDRI lands in the (redirected) cache and lights the world.
3. A texture set becomes a material with its maps packed.
4. The catalog and categories queries return their results through the status.
5. Sketchfab: search, preview and a normalized glTF import from a ZIP.
6. Cancelling a download that never ends stops its worker and removes its directory.
7. A provider error fails the fetch with the cause, and the import refuses it.
"""

import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import time
import zipfile

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import bpy

sys.path.append(str(Path(__file__).resolve().parent))
from smoke_addon import load_addon

addon = load_addon("blender_mcp_provider_fetch_smoke")

from blender_mcp_provider_fetch_smoke.server_core import BlenderMCPServer

polyhaven = sys.modules["blender_mcp_provider_fetch_smoke.handlers.polyhaven"]
sketchfab = sys.modules["blender_mcp_provider_fetch_smoke.handlers.sketchfab"]
fetches = sys.modules["blender_mcp_provider_fetch_smoke.provider_fetches"]

# The bytes of each fixture, by URL path; filled in by `_build_fixtures`.
ROUTES: dict[str, tuple[bytes, str]] = {}
# The model download is held back half-way until the main thread sets this.
GATE = threading.Event()
GATED_PATH = "/dl/chair.blend"
ENDLESS_PATH = "/dl/endless.zip"


class _LoopbackServer(ThreadingHTTPServer):
    """A threaded HTTP server that skips `HTTPServer.server_bind`'s reverse-DNS lookup of its host."""

    def server_bind(self) -> None:
        # `socket.getfqdn("127.0.0.1")` can stall for half a minute on a host whose resolver
        # times out; nothing here reads `server_name`.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class _Provider(BaseHTTPRequestHandler):
    """Serves `ROUTES`, one gated file, one endless file, and 404 for anything else."""

    def log_message(self, *_args) -> None:
        return None

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == ENDLESS_PATH:
            self._endless()
            return
        if path not in ROUTES:
            self.send_error(404, "Not Found")
            return
        body, content_type = ROUTES[path]
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if path == GATED_PATH:
            half = len(body) // 2
            self.wfile.write(body[:half])
            self.wfile.flush()
            GATE.wait(30)
            self.wfile.write(body[half:])
        else:
            self.wfile.write(body)

    def _endless(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.end_headers()
        deadline = time.monotonic() + 30
        try:
            while time.monotonic() < deadline:
                self.wfile.write(b"\0" * 4096)
                self.wfile.flush()
                time.sleep(0.02)
        except (BrokenPipeError, ConnectionResetError):
            return


def _json(value: object) -> tuple[bytes, str]:
    return json.dumps(value).encode("utf-8"), "application/json"


def _image_bytes(work: Path, name: str, file_format: str, *, float_buffer: bool = False) -> bytes:
    """Save a tiny image with real bpy and return its bytes, leaving no datablock behind."""
    image = bpy.data.images.new(name, 4, 2, float_buffer=float_buffer)
    image.pixels.foreach_set([0.5] * (4 * 2 * 4))
    path = work / f"{name}.{file_format.lower()}"
    image.filepath_raw = str(path)
    image.file_format = file_format
    image.save()
    bpy.data.images.remove(image)
    return path.read_bytes()


def _blend_bytes(work: Path) -> bytes:
    """Write a `.blend` holding one mesh object, without linking it into this scene."""
    mesh = bpy.data.meshes.new("SmokeChairMesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    obj = bpy.data.objects.new("SmokeChair", mesh)
    path = work / "chair.blend"
    # The stubs declare ID unhashable; bpy datablocks hash at runtime and libraries.write takes a set.
    bpy.data.libraries.write(str(path), {obj})  # pyright: ignore[reportUnhashable]
    bpy.data.objects.remove(obj)
    bpy.data.meshes.remove(mesh)
    return path.read_bytes()


def _gltf_zip_bytes(work: Path) -> bytes:
    """Export a 2 x 1 x 1 box as glTF with real bpy and zip its files, then delete the box."""
    bpy.ops.mesh.primitive_cube_add(size=1.0)
    box = bpy.context.active_object
    assert box is not None, "primitive_cube_add left no active object"
    box.name = "SmokeBox"
    box.scale = (2.0, 1.0, 1.0)
    bpy.ops.object.transform_apply(scale=True)
    export_dir = work / "gltf"
    export_dir.mkdir()
    gltf_path = export_dir / "scene.gltf"
    result = bpy.ops.export_scene.gltf(filepath=str(gltf_path), export_format="GLTF_SEPARATE", use_selection=True)
    assert "FINISHED" in result, result
    bpy.data.objects.remove(box)
    archive = work / "model.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        for file in export_dir.iterdir():
            handle.write(file, f"model/{file.name}")
    return archive.read_bytes()


def _build_fixtures(work: Path, base: str) -> None:
    ROUTES[GATED_PATH] = (_blend_bytes(work), "application/octet-stream")
    ROUTES["/dl/sky_1k.hdr"] = (_image_bytes(work, "smoke_sky", "HDR", float_buffer=True), "image/vnd.radiance")
    ROUTES["/dl/bricks_diff.png"] = (_image_bytes(work, "bricks_diff", "PNG"), "image/png")
    ROUTES["/dl/bricks_rough.png"] = (_image_bytes(work, "bricks_rough", "PNG"), "image/png")
    ROUTES["/dl/model.zip"] = (_gltf_zip_bytes(work), "application/zip")
    ROUTES["/dl/thumb.png"] = (_image_bytes(work, "thumb", "PNG"), "image/png")
    ROUTES["/files/chair"] = _json({"blend": {"1k": {"blend": {"url": f"{base}{GATED_PATH}"}}}})
    ROUTES["/files/sky"] = _json({"hdri": {"1k": {"hdr": {"url": f"{base}/dl/sky_1k.hdr"}}}})
    ROUTES["/files/bricks"] = _json(
        {
            "diffuse": {"1k": {"png": {"url": f"{base}/dl/bricks_diff.png"}}},
            "rough": {"1k": {"png": {"url": f"{base}/dl/bricks_rough.png"}}},
            "blend": {"1k": {"blend": {"url": f"{base}/dl/unused.blend"}}},
        }
    )
    ROUTES["/assets"] = _json({"sky": {"type": 0}, "bricks": {"type": 1}, "chair": {"type": 2}})
    ROUTES["/categories/hdris"] = _json({"all": 1, "outdoor": 1})
    ROUTES["/v3/me"] = _json({"username": "smoke-artist"})
    ROUTES["/v3/search"] = _json({"results": [{"uid": "box"}], "next": None, "previous": None})
    ROUTES["/v3/models/box"] = _json(
        {
            "name": "Smoke Box",
            "user": {"username": "smoke-artist", "displayName": "Smoke Artist"},
            "license": {"label": "CC Attribution"},
            "thumbnails": {"images": [{"url": f"{base}/dl/thumb.png", "width": 640, "height": 360}]},
        }
    )
    ROUTES["/v3/models/box/download"] = _json({"gltf": {"url": f"{base}/dl/model.zip"}})
    ROUTES["/v3/models/endless"] = _json({"name": "Endless"})
    ROUTES["/v3/models/endless/download"] = _json({"gltf": {"url": f"{base}{ENDLESS_PATH}"}})


def _run(server, command: str, **params) -> dict:
    response = server.execute_command_internal({"type": command, "params": params})
    assert response["status"] == "success", (command, response)
    return response["result"]


def _refused(server, command: str, **params) -> str:
    response = server.execute_command_internal({"type": command, "params": params})
    assert response["status"] == "error", (command, response)
    return response["message"]


def _until(server, fetch_id: str, predicate, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        status = _run(server, "get_provider_fetch", fetch_id=fetch_id)
        if predicate(status):
            return status
        assert time.monotonic() < deadline, f"fetch never reached the state wanted: {status}"
        time.sleep(0.02)


def _finished(server, fetch_id: str) -> dict:
    return _until(server, fetch_id, lambda status: status["state"] != "RUNNING")


def case_model_downloads_off_the_main_thread(server) -> None:
    started_at = time.monotonic()
    started = _run(server, "start_polyhaven_download", asset_id="chair", asset_type="models", file_format="blend")
    assert started["state"] == "RUNNING", started
    assert time.monotonic() - started_at < 2.0, "the start command waited for the download"
    # The model's response has begun - its size is in the total - and the provider is holding half of it back.
    blend_size = len(ROUTES[GATED_PATH][0])
    midway = _until(server, started["fetch_id"], lambda status: (status["bytes_total"] or 0) >= blend_size)
    assert midway["state"] == "RUNNING" and midway["bytes_received"] < midway["bytes_total"], midway
    # The main thread is free: another command answers while the download is stalled.
    assert "objects" in _run(server, "list_scene_objects")
    time.sleep(0.2)
    assert _run(server, "get_provider_fetch", fetch_id=started["fetch_id"])["state"] == "RUNNING"
    GATE.set()
    done = _finished(server, started["fetch_id"])
    assert done["state"] == "SUCCEEDED" and done["ready_to_import"], done
    # The total is the file list's JSON plus the model.
    assert done["bytes_received"] == done["bytes_total"] > blend_size, done
    imported = _run(server, "import_polyhaven_asset", fetch_id=started["fetch_id"])
    assert imported["changed_objects"] == ["SmokeChair"], imported
    assert bpy.data.objects.get("SmokeChair") is not None
    assert "Unknown provider fetch" in _refused(server, "import_polyhaven_asset", fetch_id=started["fetch_id"])
    print("PROVIDER_FETCH_SMOKE: model imported after a stalled download; main thread stayed free", flush=True)


def case_hdri(server, cache: Path) -> None:
    started = _run(server, "start_polyhaven_download", asset_id="sky", asset_type="hdris")
    assert _finished(server, started["fetch_id"])["state"] == "SUCCEEDED"
    imported = _run(server, "import_polyhaven_asset", fetch_id=started["fetch_id"])
    image_path = Path(bpy.path.abspath(imported["image_path"]))
    assert image_path.parent == cache, imported
    assert image_path.read_bytes() == ROUTES["/dl/sky_1k.hdr"][0]
    assert bpy.context.scene.world is not None and bpy.context.scene.world.name == imported["world"]
    # Only the cached image is left beside the cache: the fetch's directory went with the import.
    assert sorted(path.name for path in cache.iterdir()) == [image_path.name], list(cache.iterdir())
    print("PROVIDER_FETCH_SMOKE: HDRI cached and lighting the world", flush=True)


def case_textures(server) -> None:
    started = _run(server, "start_polyhaven_download", asset_id="bricks", asset_type="textures", file_format="png")
    assert _finished(server, started["fetch_id"])["state"] == "SUCCEEDED"
    imported = _run(server, "import_polyhaven_asset", fetch_id=started["fetch_id"])
    material = bpy.data.materials[imported["material"]]
    assert sorted(imported["map_types"]) == ["diffuse", "rough"], imported
    assert all(bpy.data.images[name].packed_file is not None for name in imported["maps"]), imported
    assert material.get("blender_mcp_polyhaven_asset_id") == "bricks"
    print("PROVIDER_FETCH_SMOKE: texture maps packed into a material", flush=True)


def case_queries(server) -> None:
    catalog = _run(server, "start_polyhaven_catalog", asset_type="all", limit=2, offset=0)
    result = _finished(server, catalog["fetch_id"])["result"]
    assert list(result["assets"]) == ["bricks", "chair"] and result["next_offset"] == 2, result
    assert _run(server, "cancel_provider_fetch", fetch_id=catalog["fetch_id"])["discarded"] is True
    categories = _run(server, "start_polyhaven_categories", asset_type="hdris")
    assert _finished(server, categories["fetch_id"])["result"]["categories"] == {"all": 1, "outdoor": 1}
    print("PROVIDER_FETCH_SMOKE: catalog and categories answered through the status", flush=True)


def case_sketchfab(server) -> None:
    assert _run(server, "get_sketchfab_status")["enabled"] is True
    account = _run(server, "start_sketchfab_account_check")
    assert "smoke-artist" in _finished(server, account["fetch_id"])["result"]["message"]
    search = _run(server, "start_sketchfab_search", query="box")
    assert _finished(server, search["fetch_id"])["result"]["results"] == [{"uid": "box"}]
    preview = _run(server, "start_sketchfab_preview", uid="box")
    preview_result = _finished(server, preview["fetch_id"])["result"]
    assert preview_result["format"] == "png" and preview_result["model_name"] == "Smoke Box", preview_result
    download = _run(server, "start_sketchfab_download", uid="box")
    assert _finished(server, download["fetch_id"])["state"] == "SUCCEEDED"
    imported = _run(
        server, "import_sketchfab_model", fetch_id=download["fetch_id"], normalize_size=True, target_size=3.0
    )
    assert abs(max(imported["dimensions"]) - 3.0) < 1e-3, imported
    assert imported["provenance"]["author"] == "Smoke Artist", imported
    print("PROVIDER_FETCH_SMOKE: Sketchfab search, preview and normalized import", flush=True)


def case_cancel(server) -> None:
    started = _run(server, "start_sketchfab_download", uid="endless")
    _until(server, started["fetch_id"], lambda status: status["bytes_received"] > 0)
    directory = fetches.REGISTRY._fetches[started["fetch_id"]].directory
    assert directory is not None and os.path.isdir(directory)
    assert _run(server, "cancel_provider_fetch", fetch_id=started["fetch_id"])["state"] == "CANCELLING"
    fetches.REGISTRY.join(started["fetch_id"], timeout=20)
    assert started["fetch_id"] not in fetches.REGISTRY._fetches
    assert not os.path.exists(directory), directory
    print("PROVIDER_FETCH_SMOKE: cancelling an endless download stopped it and removed its files", flush=True)


def case_failure(server) -> None:
    started = _run(server, "start_polyhaven_download", asset_id="missing", asset_type="models")
    failed = _finished(server, started["fetch_id"])
    assert failed["state"] == "FAILED" and "404" in failed["failure"], failed
    assert "404" in _refused(server, "import_polyhaven_asset", fetch_id=started["fetch_id"])
    print("PROVIDER_FETCH_SMOKE: a provider 404 fails the fetch and the import refuses it", flush=True)


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="provider_fetch_smoke_"))
    cache = work / "cache"
    cache.mkdir()
    # Keep Blender's real user data directory untouched: the HDRI cache lands in `cache`.
    bpy.utils.user_resource = lambda *_args, **_kwargs: str(cache)
    os.environ["BLENDERMCP_SKETCHFAB_API_KEY"] = "smoke-key"
    http = _LoopbackServer(("127.0.0.1", 0), _Provider)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{http.server_address[1]}"
    try:
        _build_fixtures(work, base)
        polyhaven.API_ROOT = base
        sketchfab.API_ROOT = f"{base}/v3"
        addon.register()
        bpy.context.scene.blendermcp_use_polyhaven = True
        bpy.context.scene.blendermcp_use_sketchfab = True
        server = BlenderMCPServer()
        case_model_downloads_off_the_main_thread(server)
        case_hdri(server, cache)
        case_textures(server)
        case_queries(server)
        case_sketchfab(server)
        case_cancel(server)
        case_failure(server)
    finally:
        GATE.set()
        http.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    print("PROVIDER_FETCH_SMOKE_OK")


main()
