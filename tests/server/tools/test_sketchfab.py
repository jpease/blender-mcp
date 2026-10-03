"""Regression coverage for Sketchfab availability reporting."""

import sys
import types

from typing import Never

from conftest import load_addon


def _scene(sketchfab_enabled):
    return types.SimpleNamespace(
        blendermcp_use_polyhaven=False,
        blendermcp_use_sketchfab=sketchfab_enabled,
        blendermcp_use_nd=False,
    )


def _request_should_not_run(*_args, **_kwargs) -> Never:
    raise AssertionError("the status command must not touch the network")


def test_disabled_sketchfab_does_not_report_a_saved_key_as_ready(monkeypatch) -> None:
    addon, _bpy = load_addon(monkeypatch, scene=_scene(sketchfab_enabled=False))
    server = addon.BlenderMCPServer()
    monkeypatch.setattr(server, "get_sketchfab_api_key", lambda: "saved-key")
    monkeypatch.setattr(addon.handlers.sketchfab.requests, "get", _request_should_not_run, raising=False)

    status = server.get_sketchfab_status()
    command = server.execute_command_internal({"type": "start_sketchfab_search"})

    assert status["enabled"] is False
    assert "currently disabled" in status["message"]
    assert command == {
        "status": "error",
        "message": "Unknown command type: start_sketchfab_search",
    }


def test_enabled_sketchfab_status_asks_for_a_key_check_without_network(monkeypatch) -> None:
    addon, _bpy = load_addon(monkeypatch, scene=_scene(sketchfab_enabled=True))
    server = addon.BlenderMCPServer()
    monkeypatch.setattr(server, "get_sketchfab_api_key", lambda: "saved-key")
    monkeypatch.setattr(addon.handlers.sketchfab.requests, "get", _request_should_not_run, raising=False)

    status = server.get_sketchfab_status()

    assert status["enabled"] is True
    assert status["verify_api_key"] is True


def test_account_check_reports_a_valid_key_as_ready(monkeypatch) -> None:
    addon, _bpy = load_addon(monkeypatch, scene=_scene(sketchfab_enabled=True))
    server = addon.BlenderMCPServer()
    monkeypatch.setattr(server, "get_sketchfab_api_key", lambda: "saved-key")

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"username": "artist"}

    monkeypatch.setattr(addon.handlers.sketchfab.requests, "get", lambda *_args, **_kwargs: Response(), raising=False)
    registry = sys.modules[f"{addon.__name__}.provider_fetches"].REGISTRY

    started = server.start_sketchfab_account_check()
    registry.join(started["fetch_id"], timeout=5)
    status = server.get_provider_fetch(started["fetch_id"])

    assert status["state"] == "SUCCEEDED"
    assert status["result"] == {
        "enabled": True,
        "message": "Sketchfab integration is enabled and ready to use. Logged in as: artist",
    }
