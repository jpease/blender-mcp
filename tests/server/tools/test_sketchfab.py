"""Regression coverage for Sketchfab availability reporting."""

import types

from typing import Never

from conftest import load_addon


def _scene(sketchfab_enabled):
    return types.SimpleNamespace(
        blendermcp_use_polyhaven=False,
        blendermcp_use_sketchfab=sketchfab_enabled,
        blendermcp_use_nd=False,
    )


def test_disabled_sketchfab_does_not_report_a_saved_key_as_ready(monkeypatch) -> None:
    addon, _bpy = load_addon(monkeypatch, scene=_scene(sketchfab_enabled=False))
    server = addon.BlenderMCPServer()
    monkeypatch.setattr(server, "get_sketchfab_api_key", lambda: "saved-key")

    def request_should_not_run(*_args, **_kwargs) -> Never:
        raise AssertionError("must not validate a disabled integration")

    monkeypatch.setattr(
        addon.handlers.sketchfab.requests,
        "get",
        request_should_not_run,
        raising=False,
    )

    status = server.get_sketchfab_status()
    command = server.execute_command_internal({"type": "search_sketchfab_models"})

    assert status["enabled"] is False
    assert "currently disabled" in status["message"]
    assert command == {
        "status": "error",
        "message": "Unknown command type: search_sketchfab_models",
    }


def test_enabled_sketchfab_reports_a_valid_key_as_ready(monkeypatch) -> None:
    addon, _bpy = load_addon(monkeypatch, scene=_scene(sketchfab_enabled=True))
    server = addon.BlenderMCPServer()
    monkeypatch.setattr(server, "get_sketchfab_api_key", lambda: "saved-key")

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"username": "artist"}

    monkeypatch.setattr(addon.handlers.sketchfab.requests, "get", lambda *_args, **_kwargs: Response(), raising=False)

    assert server.get_sketchfab_status() == {
        "enabled": True,
        "message": "Sketchfab integration is enabled and ready to use. Logged in as: artist",
    }
