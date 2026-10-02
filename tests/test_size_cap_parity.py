"""
Every list cap the server advertises is one the add-on enforces, at the same number.

The add-on cannot import the server package, so each cap is written down twice: a pydantic
`max_length` on the server, and one row of the add-on's `list_caps.LIST_CAPS`, which the
dispatcher enforces before any handler runs. The socket is the boundary an unvalidated caller
reaches, so the add-on's copy is the one that actually protects Blender's main thread - and two
numbers that drift apart are either a schema promising calls the add-on refuses after the round
trip, or an add-on accepting work the schema said was too much.

The sweep below reads every registered tool's argument model, nested payload models included,
so a capped list added to the server without its add-on row fails here rather than shipping
unenforced. Measured out-of-process, like `test_strict_tool_args.py`: the catalog registers as
an import side effect of `BLENDER_MCP_TOOLSETS`, and importing every tool module here would pin
that selection for every later test.
"""

import inspect
import json
import os
import subprocess
import sys

import pytest

from conftest import load_addon, load_addon_source_module

from blender_mcp.server.bundles import ALL_SENTINEL, TOOLSETS_ENV_VAR
from blender_mcp.server.tools.character_rigging import posing

# Prints `[[tool, path, [caps...]], ...]`: every list-, tuple- or mapping-typed field carrying a
# `max_length`, by tool and by the dotted path its handler receives it under.
_SWEEP = """
import json
import typing

import annotated_types
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from blender_mcp.server import mcp

CONTAINERS = (list, tuple, set, frozenset, dict)


def parts(node):
    if isinstance(node, FieldInfo):
        return [*node.metadata, node.annotation]
    return [*typing.get_args(node), *getattr(node, "__metadata__", ())]


def caps(node):
    if isinstance(node, annotated_types.MaxLen):
        return [node.max_length]
    return [cap for part in parts(node) for cap in caps(part)]


def containers(node):
    if typing.get_origin(node) in CONTAINERS:
        return True
    return any(containers(part) for part in parts(node) if not isinstance(part, annotated_types.MaxLen))


def models(node):
    if isinstance(node, type) and issubclass(node, BaseModel):
        return [node]
    return [model for part in parts(node) for model in models(part)]


def walk(tool, model, prefix, seen, found):
    for name, field in model.model_fields.items():
        path = prefix + name
        if containers(field):
            found.extend([tool, path, [cap]] for cap in caps(field))
        for nested in models(field):
            if nested not in seen:
                walk(tool, nested, path + ".", seen | {nested}, found)


found = []
for tool in mcp._tool_manager.list_tools():
    walk(tool.name, tool.fn_metadata.arg_model, "", frozenset(), found)
print(json.dumps(found))
"""


@pytest.fixture(scope="module")
def advertised_caps() -> dict[tuple[str, str], set[int]]:
    """
    Every list cap the whole catalog advertises, read in a child process.

    Returns:
        dict[tuple[str, str], set[int]]: `(tool, path)` to the caps stated there - one, unless
        two models in a union disagree.

    """
    env = {**os.environ, TOOLSETS_ENV_VAR: ALL_SENTINEL}
    completed = subprocess.run([sys.executable, "-c", _SWEEP], capture_output=True, text=True, env=env, check=True)
    caps: dict[tuple[str, str], set[int]] = {}
    for tool, path, (cap,) in json.loads(completed.stdout):
        caps.setdefault((tool, path), set()).add(cap)
    return caps


@pytest.fixture
def list_caps():
    """Load the add-on's cap table straight from its `bpy`-free module."""
    return load_addon_source_module("list_caps.py", "size_cap_parity_list_caps")


def test_the_sweep_reaches_top_level_nested_and_mapping_caps(advertised_caps) -> None:
    """A sweep that stopped descending would pass by finding nothing; these shapes must be seen."""
    assert advertised_caps["edit_keyframes", "edits"] == {1000}
    assert advertised_caps["keyframe_character_pose", "keys.poses"] == {500}
    assert advertised_caps["bake_evaluated_animation", "target.properties.array_indices"] == {64}
    assert advertised_caps["set_skin_weights", "normalized_vertices.weights"] == {256}


def test_every_advertised_list_cap_is_one_the_addon_enforces_at_the_same_number(advertised_caps, list_caps) -> None:
    """A cap stated on one side only is a call that fails after the round trip, or work nothing bounds."""
    conflicting = {key: caps for key, caps in advertised_caps.items() if len(caps) > 1}
    assert not conflicting, f"one path, several caps - give each its own field: {conflicting}"
    advertised = {key: next(iter(caps)) for key, caps in advertised_caps.items()}

    missing = {key: cap for key, cap in advertised.items() if key not in list_caps.LIST_CAPS}
    drifted = {
        key: (cap, list_caps.LIST_CAPS[key])
        for key, cap in advertised.items()
        if key in list_caps.LIST_CAPS and list_caps.LIST_CAPS[key] != cap
    }
    stale = sorted(set(list_caps.LIST_CAPS) - set(advertised))

    assert not missing, f"add these to bundled/addon/list_caps.py: {missing}"
    assert not drifted, f"(server, add-on) caps disagree: {drifted}"
    assert not stale, f"list_caps.py rows no tool advertises any more: {stale}"


def test_every_cap_row_names_a_command_and_a_parameter_its_handler_takes(monkeypatch, list_caps) -> None:
    """A row keyed by a name the dispatcher never sees enforces nothing."""
    addon, _bpy = load_addon(monkeypatch, data={})
    commands = addon.command_registry.COMMANDS
    for command, path in list_caps.LIST_CAPS:
        assert command in commands, command
        assert path.split(".")[0] in inspect.signature(getattr(addon.BlenderMCPServer, command)).parameters, (
            command,
            path,
        )


@pytest.mark.parametrize(
    ("command", "params", "message"),
    [
        (
            "edit_keyframes",
            {"edits": [{}] * 1001},
            r"edit_keyframes: edits carries 1,001 entries, more than the 1,000 ",
        ),
        (
            "keyframe_character_pose",
            {"keys": [{"poses": [{}]}, {"poses": [{}] * 501}]},
            r"keys\[1\]\.poses carries 501 entries, more than the 500 ",
        ),
        (
            "bake_evaluated_animation",
            {"target": {"transforms": ["LOCATION"] * 4}},
            r"target\.transforms carries 4 entries, more than the 3 ",
        ),
        (
            "set_skin_weights",
            {"normalized_vertices": [{"weights": {f"g{index}": 0.1 for index in range(257)}}]},
            r"normalized_vertices\[0\]\.weights carries 257 entries",
        ),
    ],
    ids=["top-level", "nested-through-a-list", "nested-in-a-mapping", "mapping-leaf"],
)
def test_a_list_over_its_cap_is_refused(list_caps, command, params, message) -> None:
    with pytest.raises(ValueError, match=message):
        list_caps.refuse_oversized_lists(command, params)


def test_a_list_at_its_cap_or_absent_passes(list_caps) -> None:
    list_caps.refuse_oversized_lists("edit_keyframes", {"edits": [{}] * 1000})
    list_caps.refuse_oversized_lists("keyframe_character_pose", {"keys": None, "poses": [{}] * 500})
    list_caps.refuse_oversized_lists("bake_evaluated_animation", {"target": {}})
    list_caps.refuse_oversized_lists("get_scene_info", {"limit": 10_000})


def test_dispatch_refuses_an_oversized_list_before_the_handler_runs(monkeypatch) -> None:
    """The add-on's whole enforcement is this call: no handler holds a copy of the number."""
    addon, _bpy = load_addon(monkeypatch, data={})
    server = addon.BlenderMCPServer()
    ran = []
    monkeypatch.setattr(server, "_build_command_handlers", lambda: {"edit_keyframes": lambda **_: ran.append(True)})

    response = server.execute_command_internal({"type": "edit_keyframes", "params": {"edits": [{}] * 1001}})

    assert response["status"] == "error"
    assert "edits carries 1,001 entries, more than the 1,000 one call accepts" in response["message"]
    assert ran == []


def test_the_batched_pose_total_is_the_one_the_addon_enforces(monkeypatch) -> None:
    """Not a field's `max_length` but a total across `keys`, so it is stated as a constant on both sides."""
    addon, _bpy = load_addon(monkeypatch, data={})

    assert sys.modules[f"{addon.__name__}.handlers.character_rigging.posing"]._MAX_KEYED_POSE_ENTRIES == (
        posing._MAX_BATCHED_POSE_ENTRIES
    )
