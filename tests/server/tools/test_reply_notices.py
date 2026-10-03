"""
A notice the add-on puts on a reply's `warnings` reaches the client's envelope, whichever way a tool builds it.

The add-on attaches its undo/redo and outside-edit notice to the next reply it sends, of whatever
command (`bundled/addon/scene_watch.py`), and then forgets it. A tool that dropped the reply's
`warnings` would therefore lose that notice for good, so every shape of tool is covered: the
read-only and mutating tools `call_blender` envelopes, and each tool that builds its envelope by
hand from the reply's fields or from several replies.
"""

import asyncio
import base64
import types

from collections.abc import Callable, Mapping
from typing import Any

import pytest

from mcp.server.fastmcp.exceptions import ToolError

from blender_mcp.server.tools import _dispatch, _image_transport, core, polyhaven, rendering, scene, sketchfab, viewport

NOTICE = "The scene was undone once since the last command - re-inspect anything you read before."


class _Replies:
    """A connection answering each command with its canned reply, plus that command's own notice."""

    def __init__(self, replies: Mapping[str, dict]) -> None:
        """
        Hold the canned replies.

        Args:
            replies: Command -> reply, before the notice is added.

        """
        self._replies = replies
        self.calls: list[str] = []

    def send_command(self, command: str, _params: dict) -> dict:
        """
        Answer one command.

        Args:
            command: The command.
            _params: Its parameters; unused.

        Returns:
            dict: The reply, carrying `NOTICE` tagged with the command's name.

        """
        self.calls.append(command)
        return {**self._replies[command], "warnings": [f"{command}: {NOTICE}"]}


def _envelope(content: Any) -> dict:
    """
    Pick the envelope out of a tool's return: itself, or the last item after an image.

    Args:
        content: What the tool returned.

    Returns:
        dict: The envelope.

    """
    return content[-1] if isinstance(content, list) else content


def _inline_images(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Make the image tools take the inline transport, so no file is involved.

    Args:
        monkeypatch: Restores the handshake lookup afterwards.

    """
    monkeypatch.setattr(
        _image_transport,
        "get_last_handshake",
        lambda: types.SimpleNamespace(protocol_version=_image_transport.INLINE_IMAGE_PROTOCOL_VERSION),
    )


_PNG = base64.b64encode(b"png").decode("ascii")


def _fetched(result: dict) -> dict:
    """
    Answer a provider `start_*` command with a fetch that already finished.

    Args:
        result: The query's result.

    Returns:
        dict: A SUCCEEDED status carrying it, so the tool sends no poll before releasing it.

    """
    return {"fetch_id": "f1", "state": "SUCCEEDED", "stage": "succeeded", "bytes_received": 0, "result": result}


# The reply to the release every provider query sends once it has read its result.
_RELEASED = {"fetch_id": "f1", "discarded": True, "state": "SUCCEEDED"}

# (tool call, command -> reply). Each case names every command the tool sends.
_CASES: dict[str, tuple[Callable[[], Any], dict[str, dict]]] = {
    "read-only call_blender": (
        lambda: viewport.list_scene_objects(ctx=None),
        {"list_scene_objects": {"objects": [], "total_count": 0}},
    ),
    "mutating call_blender": (
        lambda: scene.set_object_visibility(ctx=None, object_name="Cube", hide_render=True),
        {"set_object_visibility": {"object_name": "Cube", "changed_objects": ["Cube"]}},
    ),
    "get_polyhaven_categories": (
        lambda: polyhaven.get_polyhaven_categories(ctx=None),
        {
            "get_polyhaven_status": {"enabled": True},
            "start_polyhaven_categories": _fetched({"categories": {"sky": 3}}),
            "cancel_provider_fetch": _RELEASED,
        },
    ),
    "list_polyhaven_assets": (
        lambda: polyhaven.list_polyhaven_assets(ctx=None),
        {
            "start_polyhaven_catalog": _fetched(
                {
                    "assets": {},
                    "total_count": 0,
                    "returned_count": 0,
                    "offset": 0,
                    "limit": 20,
                    "truncated": False,
                    "next_offset": None,
                }
            ),
            "cancel_provider_fetch": _RELEASED,
        },
    ),
    "search_sketchfab_models": (
        lambda: sketchfab.search_sketchfab_models(ctx=None, query="tree"),
        {"start_sketchfab_search": _fetched({"results": []}), "cancel_provider_fetch": _RELEASED},
    ),
    "get_sketchfab_model_preview": (
        lambda: sketchfab.get_sketchfab_model_preview(ctx=None, uid="abc"),
        {
            "start_sketchfab_preview": _fetched({"image_data": _PNG, "format": "png", "model_name": "Tree"}),
            "cancel_provider_fetch": _RELEASED,
        },
    ),
    "import_polyhaven_asset": (
        lambda: polyhaven.import_polyhaven_asset(ctx=None, asset_id="bricks", asset_type="textures"),
        {
            "start_polyhaven_download": {"fetch_id": "f1", "state": "SUCCEEDED", "ready_to_import": True},
            "import_polyhaven_asset": {"success": True, "message": "imported", "material": "Bricks", "maps": []},
        },
    ),
    "import_sketchfab_model": (
        lambda: sketchfab.import_sketchfab_model(ctx=None, uid="abc", target_size=1.0),
        {
            "start_sketchfab_download": {"fetch_id": "f1", "state": "SUCCEEDED", "ready_to_import": True},
            "import_sketchfab_model": {"success": True, "imported_objects": ["Box"]},
        },
    ),
    "get_integration_status (sketchfab key check)": (
        lambda: core.get_integration_status(ctx=None, provider="sketchfab"),
        {
            "get_sketchfab_status": {"enabled": True, "message": "checking", "verify_api_key": True},
            "start_sketchfab_account_check": _fetched({"enabled": True, "message": "Logged in as: artist"}),
            "cancel_provider_fetch": _RELEASED,
        },
    ),
    "get_viewport_screenshot": (
        lambda: viewport.get_viewport_screenshot(ctx=None),
        {"get_viewport_screenshot": {"image_base64": _PNG, "width": 4, "height": 4}},
    ),
    "inspect_render_output": (
        lambda: rendering.inspect_render_output(ctx=None),
        {"inspect_render_output": {"image_base64": _PNG, "width": 4, "height": 4}},
    ),
    "get_integration_status (all)": (
        lambda: core.get_integration_status(ctx=None),
        {
            "get_polyhaven_status": {"enabled": False, "message": "off"},
            "get_sketchfab_status": {"enabled": False, "message": "off"},
            "get_nd_status": {"enabled": True, "message": "on"},
        },
    ),
}


@pytest.mark.parametrize("case", list(_CASES))
def test_every_replys_notice_reaches_the_envelope_once_and_leaves_the_data(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    call, replies = _CASES[case]
    connection = _Replies(replies)
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)
    _inline_images(monkeypatch)

    envelope = _envelope(asyncio.run(call()))

    assert sorted(connection.calls) == sorted(replies), "the case does not name every command the tool sends"
    expected = [f"{command}: {NOTICE}" for command in replies]
    assert sorted(w for w in envelope["warnings"] if NOTICE in w) == sorted(expected)
    assert NOTICE not in repr(envelope["data"])


def test_a_refusal_built_from_the_status_probe_still_carries_its_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _Replies({"get_polyhaven_status": {"enabled": False}})
    monkeypatch.setattr(_dispatch, "get_blender_connection", lambda: connection)

    with pytest.raises(ToolError, match="integration is disabled") as refusal:
        asyncio.run(polyhaven.get_polyhaven_categories(ctx=None))

    assert f"get_polyhaven_status: {NOTICE}" in str(refusal.value)
