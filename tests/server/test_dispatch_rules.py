"""
The routing decision `_run_handler` makes before it calls a handler.

Two questions are answered from the command type and its params alone: is this
call read-only, and does it run outside `mutation_transaction`? They used to be
fifty lines of `or` inside `_run_handler`, reachable only by calling a handler
through a fake `bpy`, so the params-dependent rows - an `INSPECT` cache call, a
dry-run sewing preview, an instancer call that names no source - were never
exercised on their own. They are predicates now, so each row is one assertion.

The bypass that is *not* about read-only-ness (session swaps, the library
commands) is covered end to end in `tests/test_transaction_session_swap.py`;
what is pinned here is that the predicate agrees, including for the non-undo
commands, whose only protection is this routing.
"""

from __future__ import annotations

import pytest

from server.test_threading import BlenderMCPServer

# One row per params-dependent command: the type, params that make the call a
# read, and params that make it a write. Wherever `{}` appears it is deliberate
# - it asserts the default the handler itself applies to a missing `action`
# (or flag), which is what a client omitting the parameter will get.
_PARAMS_DECIDE = [
    ("manage_retopology_checkpoint", {"action": "list"}, {}),
    ("manage_retopology_checkpoint", {"action": "COMPARE"}, {"action": "CREATE"}),
    ("analyze_surface_conformity", {}, {"create_heat_map": True}),
    ("configure_cloth_sewing", {}, {"dry_run": False}),
    ("manage_cloth_cache", {}, {"action": "BAKE"}),
    ("manage_liquid_cache", {}, {"action": "BAKE"}),
    ("analyze_liquid_performance", {}, {"measure_replay_evaluation": True}),
    ("create_camera_markers", {"action": "LIST"}, {}),
    ("manage_rigid_body_cache", {}, {"action": "BAKE"}),
    ("analyze_rigid_body_performance", {}, {"sample_frames": [1, 24]}),
    ("manage_named_attributes", {}, {"action": "ADD"}),
    ("manage_geometry_nodes_bake", {}, {"action": "BAKE"}),
    ("manage_procedural_instances", {}, {"source_name": "Rock"}),
    ("manage_procedural_instances", {"pick_instance": None}, {"realize_instances": False}),
]


@pytest.fixture(name="server")
def _server() -> object:
    """
    Build a server, for the decision tables its class carries.

    Returns:
        object: A `BlenderMCPServer`, not started and never given a socket.
            Untyped because the class is built by exec, not imported.

    """
    return BlenderMCPServer(port=0)


@pytest.mark.parametrize(("cmd_type", "reading", "writing"), _PARAMS_DECIDE)
def test_the_params_decide_whether_these_commands_read_or_write(
    server: object, cmd_type: str, reading: dict[str, object], writing: dict[str, object]
) -> None:
    """
    Each params-dependent command is read-only for one set of params, not the other.

    Getting this wrong either wraps an inspection in a pointless snapshot and
    an undo checkpoint, or lets a real mutation run with no rollback.

    Args:
        server: The server whose tables decide.
        cmd_type: The MCP command type.
        reading: Params that make the call an inspection.
        writing: Params that make the same command mutate.

    """
    assert server.is_read_only_command(cmd_type, reading) is True
    assert server.is_read_only_command(cmd_type, writing) is False


def test_a_command_in_the_read_only_set_is_read_only_whatever_its_params(server: object) -> None:
    """The static table wins outright: no params can turn a listed command into a write."""
    assert server.is_read_only_command("list_scene_objects", {}) is True
    assert server.is_read_only_command("list_scene_objects", {"action": "BAKE"}) is True


def test_an_unlisted_command_is_a_write(server: object) -> None:
    """A command in neither table mutates, which is the safe default for a new handler."""
    assert server.is_read_only_command("create_primitive", {}) is False
    assert server.bypasses_transaction("create_primitive", {}) is False


@pytest.mark.parametrize(
    ("cmd_type", "why"),
    [
        ("list_scene_objects", "read-only, so a snapshot and a checkpoint would buy nothing"),
        ("manage_cloth_cache", "read-only for these params"),
        ("render_scene", "writes a file, not scene state, so there is nothing to roll back"),
        ("nd_pulse_viewport_toggle", "a viewport toggle is not worth an undo step"),
        ("open_shot", "a swap: after a load every id looks new and a rollback would empty the file"),
        ("reload_library", "replaces linked datablocks in place, which a rollback would delete"),
        ("unlink_libraries", "fires no handler, so this routing is the only thing protecting it"),
    ],
)
def test_these_commands_run_outside_the_transaction(server: object, cmd_type: str, why: str) -> None:
    """
    Every reason a command bypasses `mutation_transaction`, one case each.

    Args:
        server: The server whose tables decide.
        cmd_type: The MCP command type.
        why: The reason this command bypasses, for the failure message.

    """
    assert server.bypasses_transaction(cmd_type, {}) is True, why
