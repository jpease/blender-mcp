"""
`blender-mcp run-calls` without a Blender: parsing, classification, argv/env, and the tool layer.

The tool-layer tests drive a real in-process MCP client session against this server, the layer
an MCP client hits, with the socket replaced by `stub_blender_connection`. The add-on side's
`serve_in_background` is driven through the fake `bpy`.
"""

import asyncio
import json
import os
import signal
import socket
import sys
import threading

from pathlib import Path

import mcp.types as mcp_types
import pytest

from conftest import load_addon
from mcp.shared.memory import create_connected_server_and_client_session

from blender_mcp import run_calls
from blender_mcp.run_calls import (
    Call,
    CallOutcome,
    CallsFileError,
    apply_calls,
    blender_argv,
    blender_env,
    outcome_line,
    outcome_of,
    parse_calls,
    ready_port,
    summary_line,
)
from blender_mcp.server import app, cli
from blender_mcp.server.app import mcp


@pytest.fixture
def no_blender_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the session's startup find no Blender, instead of retrying a connection or reading the disk."""

    def unreachable() -> None:
        raise ConnectionError("no Blender in this test")

    monkeypatch.setattr(app, "get_blender_connection", unreachable)
    monkeypatch.setattr(app, "check_addon_status_on_startup", unreachable)


def _result(*, envelope: object = None, structured: dict | None = None, error: str | None = None):
    text = error if error is not None else json.dumps(envelope)
    content: list[mcp_types.ContentBlock] = [mcp_types.TextContent(type="text", text=text)]
    return mcp_types.CallToolResult(content=content, structuredContent=structured, isError=error is not None)


_CALL = Call(index=7, tool="t", args={}, source="f.json")


# --- parse_calls ---


def test_an_object_with_a_calls_list_yields_its_calls_and_ignores_other_keys() -> None:
    assert parse_calls('{"calls":[{"tool":"a","args":{}}],"move":"x"}', "f.json", 5) == (
        Call(index=5, tool="a", args={}, source="f.json"),
    )


def test_a_bare_list_is_numbered_from_first_index() -> None:
    calls = parse_calls('[{"tool":"a","args":{"k":1}},{"tool":"b","args":{}}]', "f.json", 3)
    assert [(call.index, call.tool, call.args) for call in calls] == [(3, "a", {"k": 1}), (4, "b", {})]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param("{", "f.json: not valid JSON:", id="invalid-json"),
        pytest.param('{"steps":[]}', 'f.json: expected an object with a "calls" list, or a list', id="no-calls-key"),
        pytest.param('"x"', 'f.json: expected an object with a "calls" list, or a list', id="string"),
        pytest.param("[1]", 'f.json: call 0: expected an object with "tool" and "args"', id="item-not-object"),
        pytest.param('[{"tool":"","args":{}}]', 'f.json: call 0: "tool" must be a non-empty string', id="empty-tool"),
        pytest.param('[{"tool":"a"}]', 'f.json: call 0: "args" must be an object', id="args-missing"),
        pytest.param('[{"tool":"a","args":[]}]', 'f.json: call 0: "args" must be an object', id="args-list"),
    ],
)
def test_a_malformed_calls_file_is_refused_with_its_source_and_position(text: str, message: str) -> None:
    with pytest.raises(CallsFileError) as raised:
        parse_calls(text, "f.json", 0)
    assert str(raised.value).startswith(message)


def test_a_call_position_in_a_message_counts_within_its_own_file() -> None:
    with pytest.raises(CallsFileError, match=r'^g\.json: call 1: "tool"'):
        parse_calls('[{"tool":"a","args":{}},{"args":{}}]', "g.json", 10)


def test_an_unreadable_calls_file_is_a_calls_file_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(CallsFileError, match=r"missing\.json: cannot read: No such file or directory"):
        run_calls.read_calls_files([missing])


def test_calls_are_numbered_across_files(tmp_path: Path) -> None:
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    first.write_text('[{"tool":"a","args":{}},{"tool":"b","args":{}}]', encoding="utf-8")
    second.write_text('{"calls":[{"tool":"c","args":{}}]}', encoding="utf-8")
    calls = run_calls.read_calls_files([first, second])
    assert [(call.index, call.tool, call.source) for call in calls] == [
        (0, "a", str(first)),
        (1, "b", str(first)),
        (2, "c", str(second)),
    ]


# --- outcome_of ---


def test_a_tool_error_is_a_failure_carrying_its_text() -> None:
    assert outcome_of(_CALL, _result(error="bad")) == CallOutcome(7, "t", ok=False, warnings=(), error="bad")


def test_an_ok_envelope_is_a_success_with_its_warnings() -> None:
    assert outcome_of(_CALL, _result(envelope={"ok": True, "warnings": ["w"]})) == CallOutcome(
        7, "t", ok=True, warnings=("w",), error=None
    )


def test_a_not_ok_envelope_is_a_failure() -> None:
    assert outcome_of(_CALL, _result(envelope={"ok": False})) == CallOutcome(
        7, "t", ok=False, warnings=(), error="tool reported ok: false"
    )


def test_structured_content_is_read_as_the_envelope_when_a_tool_declares_it() -> None:
    result = _result(envelope="not the envelope", structured={"ok": True, "warnings": ["w"]})
    assert outcome_of(_CALL, result).warnings == ("w",)


def test_the_envelope_after_an_image_is_the_one_read() -> None:
    image = mcp_types.ImageContent(type="image", data="", mimeType="image/png")
    envelope = mcp_types.TextContent(type="text", text=json.dumps({"ok": True, "warnings": []}))
    assert outcome_of(_CALL, mcp_types.CallToolResult(content=[image, envelope])).ok is True


# --- formatting, argv, env, ready line ---


def test_outcome_line_is_exact() -> None:
    outcome = CallOutcome(index=0, tool="set_scene_frame", ok=True, warnings=("w",), error=None)
    assert (
        outcome_line(outcome) == '{"index": 0, "tool": "set_scene_frame", "ok": true, "warnings": ["w"], "error": null}'
    )


def test_summary_line_is_exact() -> None:
    assert summary_line(2, 3) == '{"applied": 2, "total": 3}'


def test_blender_argv_without_open() -> None:
    assert blender_argv("blender", None, "boot.py") == ["blender", "-b", "--factory-startup", "--python", "boot.py"]


def test_blender_argv_with_open() -> None:
    assert blender_argv("blender", "shot.blend", "boot.py") == [
        "blender",
        "-b",
        "--factory-startup",
        "shot.blend",
        "--python",
        "boot.py",
    ]


_BASE_ENV = {
    "PATH": "/bin",
    "PYTHONPATH": "/elsewhere",
    "PYTHONHOME": "/home",
    "PYTHONSTARTUP": "/startup.py",
    "BLENDERMCP_FILE_ROOTS": "/inherited",
    "BLENDERMCP_RUN_CALLS_EMPTY_SCENE": "1",
}


def test_blender_env_drops_python_variables_and_sets_the_bootstrap_ones() -> None:
    env = blender_env(_BASE_ENV, file_roots=None, addon_dir="/addon", port=50123, empty_scene=True)
    assert env == {
        "PATH": "/bin",
        "BLENDERMCP_FILE_ROOTS": "/inherited",
        "BLENDERMCP_RUN_CALLS_ADDON": "/addon",
        "BLENDERMCP_RUN_CALLS_PORT": "50123",
        "BLENDERMCP_RUN_CALLS_EMPTY_SCENE": "1",
    }


def test_blender_env_sets_file_roots_when_given() -> None:
    env = blender_env(_BASE_ENV, file_roots="/a:/b", addon_dir="/addon", port=1, empty_scene=True)
    assert env["BLENDERMCP_FILE_ROOTS"] == "/a:/b"


def test_blender_env_removes_an_inherited_empty_scene_request_when_a_file_is_opened() -> None:
    env = blender_env(_BASE_ENV, file_roots=None, addon_dir="/addon", port=1, empty_scene=False)
    assert "BLENDERMCP_RUN_CALLS_EMPTY_SCENE" not in env


def test_blender_env_leaves_its_base_untouched() -> None:
    base = dict(_BASE_ENV)
    blender_env(base, file_roots="/x", addon_dir="/addon", port=1, empty_scene=False)
    assert base == _BASE_ENV


@pytest.mark.parametrize(
    ("line", "port"),
    [
        pytest.param("BLENDERMCP_RUN_CALLS_READY 50123", 50123, id="ready"),
        pytest.param("BLENDERMCP_RUN_CALLS_READY 50123\n", 50123, id="with-newline"),
        pytest.param("BLENDERMCP_RUN_CALLS_READY", None, id="no-port"),
        pytest.param("BLENDERMCP_RUN_CALLS_READY x", None, id="non-numeric"),
        pytest.param("Blender 5.2.2", None, id="other-output"),
    ],
)
def test_ready_port_reads_only_the_ready_line(line: str, port: int | None) -> None:
    assert ready_port(line) == port


# --- through the MCP tool layer ---


def _apply(calls: tuple[Call, ...]) -> tuple[int, list[str]]:
    lines: list[str] = []

    async def run() -> int:
        async with create_connected_server_and_client_session(mcp) as client:
            return await apply_calls(client.call_tool, calls, lines.append)

    return asyncio.run(run()), lines


def test_an_invalid_argument_fails_before_blender_sees_it(no_blender_at_startup, stub_blender_connection) -> None:
    recorder = stub_blender_connection()
    calls = parse_calls('[{"tool":"set_scene_frame","args":{"frame":12,"bogus":1}}]', "f.json", 0)

    applied, lines = _apply(calls)

    assert applied == 0
    assert len(lines) == 1
    line = json.loads(lines[0])
    assert line["ok"] is False
    assert "bogus" in line["error"]
    assert recorder.calls == []


def test_a_real_tool_reply_is_read_as_its_envelope(no_blender_at_startup, stub_blender_connection) -> None:
    # The tools return a bare `dict`, which the SDK sends as a JSON text block, not as
    # structured content: this is the shape `outcome_of` must read.
    recorder = stub_blender_connection({"frame": 12})
    calls = parse_calls('[{"tool":"set_scene_frame","args":{"frame":12}}]', "f.json", 0)

    applied, lines = _apply(calls)

    assert applied == 1
    assert lines == ['{"index": 0, "tool": "set_scene_frame", "ok": true, "warnings": [], "error": null}']
    assert [command for command, _params in recorder.calls] == ["set_scene_frame"]


def test_the_run_stops_at_the_first_failed_call(no_blender_at_startup, stub_blender_connection) -> None:
    recorder = stub_blender_connection({"frame": 1})
    calls = parse_calls(
        '[{"tool":"set_scene_frame","args":{"frame":1}},'
        '{"tool":"set_scene_frame","args":{"frame":"x"}},'
        '{"tool":"set_scene_frame","args":{"frame":2}}]',
        "f.json",
        0,
    )

    applied, lines = _apply(calls)

    assert applied == 1
    assert [json.loads(line)["ok"] for line in lines] == [True, False]
    assert len(recorder.calls) == 1


def _mounted_names() -> set[str]:
    async def listed() -> set[str]:
        async with create_connected_server_and_client_session(mcp) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    return asyncio.run(listed())


def test_an_unmounted_tool_fails_by_name_without_starting_blender(
    no_blender_at_startup, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `save_texture_image` is in the `asset` mode only, but an earlier test module may have
    # mounted it in this process; then any name the session does not list stands in for it.
    tool = "save_texture_image" if "save_texture_image" not in _mounted_names() else "not_a_mounted_tool"
    calls_file = tmp_path / "calls.json"
    calls_file.write_text(
        json.dumps([{"tool": tool, "args": {"image_name": "x", "output_path": "/tmp/x.png"}}]), encoding="utf-8"
    )
    # Recorded so they are put back: main points the session at the port it reserved.
    monkeypatch.setenv("BLENDER_HOST", "unused")
    monkeypatch.setenv("BLENDER_PORT", "1")

    def no_blender(_argv: list[str], _env: dict[str, str]):
        pytest.fail("Blender was started for a run that cannot succeed")

    code = run_calls.main(["--blender", "blender", str(calls_file)], start=no_blender)

    assert code == 1
    assert capsys.readouterr().out.splitlines() == [
        json.dumps(
            {
                "index": 0,
                "tool": tool,
                "ok": False,
                "warnings": [],
                "error": f"tool {tool} is not mounted under --toolsets shot",
            }
        ),
        '{"applied": 0, "total": 1}',
    ]


def test_a_malformed_calls_file_exits_2_naming_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    calls_file = tmp_path / "calls.json"
    calls_file.write_text("{", encoding="utf-8")

    def no_blender(_argv: list[str], _env: dict[str, str]):
        pytest.fail("Blender was started for an unreadable calls file")

    code = run_calls.main(["--blender", "blender", str(calls_file)], start=no_blender)

    captured = capsys.readouterr()
    assert code == 2
    assert not captured.out
    assert captured.err.startswith(f"blender-mcp run-calls: {calls_file}: not valid JSON:")


# --- starting and stopping Blender, with a stand-in process ---


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_a_blender_that_exits_before_serving_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        run_calls.start_blender(_python("import sys; sys.exit(3)"), dict(os.environ))

    assert raised.value.code == 2
    assert "blender-mcp run-calls: Blender exited with code 3 before serving" in capsys.readouterr().err


def test_a_blender_that_cannot_be_launched_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        run_calls.start_blender([str(tmp_path / "no-blender")], dict(os.environ))

    assert raised.value.code == 2
    assert capsys.readouterr().err.startswith("blender-mcp run-calls: cannot start Blender ")


def test_a_serving_blender_is_returned_and_stopped_with_its_process_group() -> None:
    proc = run_calls.start_blender(
        _python("import time; print('BLENDERMCP_RUN_CALLS_READY 1', flush=True); time.sleep(60)"), dict(os.environ)
    )
    assert proc.poll() is None

    run_calls.stop_blender(proc)

    assert proc.returncode == -signal.SIGTERM
    with pytest.raises(ProcessLookupError):
        os.killpg(proc.pid, 0)


# --- the CLI's re-execution ---


def test_an_unknown_toolset_exits_2_naming_it_without_re_executing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.os, "execve", lambda *_args: pytest.fail("re-executed with a bad toolset"))

    code = cli._exec_run_calls(["--blender", "blender", "--toolsets", "shot,rendring", "calls.json"])

    assert code == 2
    err = capsys.readouterr().err
    assert err.startswith("blender-mcp run-calls: ")
    assert "rendring" in err


def test_run_calls_re_executes_with_its_toolsets_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    executed: list[tuple[str, list[str], dict[str, str]]] = []
    monkeypatch.setattr(cli.os, "execve", lambda path, argv, env: executed.append((path, argv, env)))
    argv = ["--blender", "blender", "--toolsets", "asset", "calls.json"]

    cli._exec_run_calls(argv)

    [(path, exec_argv, env)] = executed
    assert path == cli.sys.executable
    assert exec_argv == [cli.sys.executable, "-m", "blender_mcp.run_calls", *argv]
    assert env["BLENDER_MCP_TOOLSETS"] == "asset"


# --- the add-on's background serving ---


def test_serve_in_background_is_refused_outside_background_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    addon, _bpy = load_addon(monkeypatch)
    server = addon.BlenderMCPServer(host="127.0.0.1", port=0)

    with pytest.raises(RuntimeError, match=r"^serve_in_background is for blender -b; use start\(\)$"):
        server.serve_in_background(lambda _port: pytest.fail("ready was called"))
    assert server.running is False
    assert server.socket is None


def test_start_still_refuses_background_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    addon, bpy = load_addon(monkeypatch)
    bpy.app.background = True
    registered: list[object] = []
    bpy.app.timers.register = lambda callback, **_kwargs: registered.append(callback)
    server = addon.BlenderMCPServer(host="127.0.0.1", port=0)

    server.start()

    assert server.running is False
    assert server.socket is None
    assert registered == []


def test_serve_in_background_serves_on_the_calling_thread_until_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    addon, bpy = load_addon(monkeypatch)
    bpy.app.background = True
    server = addon.BlenderMCPServer(host="127.0.0.1", port=0)
    main_thread = threading.get_ident()
    executed_on: list[int] = []

    def execute(command: dict) -> dict:
        executed_on.append(threading.get_ident())
        return {"status": "success", "result": {"echo": command.get("type")}}

    server.execute_command = execute
    replies: list[dict] = []

    def client(port: int) -> None:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                sock.sendall(json.dumps({"type": "ping"}).encode() + b"\n")
                replies.append(json.loads(sock.makefile().readline()))
        finally:
            server.running = False

    ports: list[int] = []

    def ready(port: int) -> None:
        ports.append(port)
        threading.Thread(target=client, args=(port,), daemon=True).start()

    server.serve_in_background(ready)

    assert ports and ports[0] > 0
    assert [(reply["status"], reply["result"]) for reply in replies] == [("success", {"echo": "ping"})]
    assert executed_on == [main_thread]
    assert server.socket is None
