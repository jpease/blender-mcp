"""
The invariants the one `COMMANDS` registry now makes assertable.

A command's identity used to be spread over the 346-line `_build_command_handlers`
plus nine separate name-keyed sets, and nothing coupled any of them to the dispatch
keys. A name misspelled in - or missing from - `_GEOMETRY_MUTATING_COMMANDS` silently
turned mesh backup off for it, and nothing failed. One registry makes both halves of
that checkable: every row names a real handler, and every classification is read from
that row and nowhere else.
"""

import ast
import sys

import pytest

from conftest import ROOT_ADDON
from test_mutation_transaction import _load_addon

SERVER_CORE = ROOT_ADDON.parent / "server_core.py"

# The `CommandSpec` fields that classify a command, as opposed to gating it.
CLASSIFICATION_FIELDS = (
    "read_only",
    "read_only_when",
    "non_undo",
    "non_undo_when",
    "geometry",
    "session_swap",
    "datablock_replacing",
    "tick_ending",
    "indeterminate_safe",
)


@pytest.fixture
def server_core(monkeypatch: pytest.MonkeyPatch):
    """
    Load the add-on's `server_core` against the shared `bpy` stubs.

    Args:
        monkeypatch: Fixture the add-on loader installs its stubs through.

    Returns:
        ModuleType: The loaded `server_core`.

    """
    addon, _bpy = _load_addon(monkeypatch, data={})
    return sys.modules[f"{addon.__name__}.server_core"]


def test_every_registered_command_resolves_to_a_handler(server_core) -> None:
    """
    A row naming a method this class does not have would be an undispatchable command.

    This is the invariant the old two-table arrangement could not state: the
    dispatch table listed `self.<handler>` by hand, so a name only in a
    classification set (`get_nd_status` was one) reached nothing.
    """
    server = server_core.BlenderMCPServer()

    missing = sorted(name for name in server_core.COMMANDS if not callable(getattr(server, name, None)))

    assert not missing, f"registered commands with no handler behind them: {missing}"


def test_the_whole_dispatch_table_comes_from_the_registry(server_core) -> None:
    """
    Enabling every provider must advertise exactly the registry, no more and no less.

    An extra key would be a command classified by `_UNCLASSIFIED`'s safe defaults
    rather than by a row someone wrote; a missing one would be a row that never
    dispatches.
    """
    server = server_core.BlenderMCPServer()
    scene = server_core.bpy.context.scene
    for flag in ("blendermcp_use_polyhaven", "blendermcp_use_sketchfab", "blendermcp_use_nd"):
        setattr(scene, flag, True)

    assert set(server._build_command_handlers()) == set(server_core.COMMANDS)


def test_a_disabled_provider_withholds_exactly_its_own_commands(server_core) -> None:
    """The gate is the spec's `provider` field, so no second list can disagree with it."""
    server = server_core.BlenderMCPServer()

    assert set(server._build_command_handlers()) == {
        name for name, spec in server_core.COMMANDS.items() if spec.provider is None
    }


def test_no_classification_set_survives_outside_the_registry() -> None:
    """
    The ten tables the registry replaced must not grow back beside it.

    Asserted against the source rather than the loaded module because a
    reintroduced table would most likely be a class attribute, and the point is
    that no second declaration of a command's identity exists at all.
    """
    retired = {
        "_READ_ONLY_COMMANDS",
        "_READ_ONLY_WHEN",
        "_NON_UNDO_COMMANDS",
        "_NON_UNDO_WHEN",
        "_GEOMETRY_MUTATING_COMMANDS",
        "_SESSION_SWAP_COMMANDS",
        "_DATABLOCK_REPLACING_COMMANDS",
        "_TICK_ENDING_COMMANDS",
        "_INDETERMINATE_SAFE_COMMANDS",
    }
    tree = ast.parse(SERVER_CORE.read_text(encoding="utf-8"))

    assigned = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    } | {
        node.target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }

    assert not assigned & retired, f"a retired classification table came back: {sorted(assigned & retired)}"


def test_an_unregistered_command_is_classified_as_an_ordinary_mutation(server_core) -> None:
    """
    The safe default: a name with no row mutates, is transacted and is refused when indeterminate.

    `command_spec` is total, so every caller gets an answer for a name a client
    invented; that answer must never be one that skips protection.
    """
    spec = server_core.BlenderMCPServer.command_spec("no_such_command")

    assert not any(bool(getattr(spec, field)) for field in CLASSIFICATION_FIELDS)
    assert spec.provider is None


def test_ping_answers_read_only_without_a_live_blender(server_core) -> None:
    """
    `ping` used to be answered by an if-chain ahead of the dispatch table.

    That special case is gone, so the two properties it quietly relied on now have
    to hold as an ordinary registered command: the reply needs no server state and
    no `bpy` (calling it off the class, with no instance and no scene, proves it),
    and it is classified read-only so it never opens a transaction. A ping that
    touched the database could not isolate a transport fault from a data fault,
    which is the only reason the command exists.
    """
    assert server_core.BlenderMCPServer.ping() == {"pong": True}
    assert server_core.BlenderMCPServer.command_spec("ping").read_only
    assert "ping" in server_core.BlenderMCPServer()._build_command_handlers()


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        pytest.param({}, [], id="nothing-named"),
        pytest.param({"object_name": "Hero"}, ["Hero"], id="scalar-key"),
        pytest.param({"name": "Hero"}, [], id="name-is-a-new-object-not-a-target"),
        pytest.param({"object_names": ["A", "B"]}, ["A", "B"], id="list-key"),
        pytest.param({"object_names": ["A", 7, None]}, ["A"], id="non-strings-in-a-list-are-skipped"),
        pytest.param({"object_name": "A", "camera_name": "A"}, ["A"], id="duplicates-collapse"),
        pytest.param(
            {"camera_name": "Cam", "object_name": "Hero"},
            ["Hero", "Cam"],
            id="order-follows-the-table-not-the-params",
        ),
        pytest.param({"targets": [{"object_name": "T"}, "junk"]}, ["T"], id="records-in-a-list"),
        pytest.param(
            {"assignments": [{"child_object_name": "C", "parent_object_name": "P"}]},
            ["C", "P"],
            id="two-name-keys-in-one-record",
        ),
        pytest.param({"constraint": {"target_object_name": "Rig"}}, ["Rig"], id="a-lone-record"),
        pytest.param(
            {"bodies": [{"object_name": "B", "convex_source_object_name": "S"}]},
            ["B", "S"],
            id="rigid-body-record-keys",
        ),
        pytest.param({"object_name": 7}, [], id="a-non-string-scalar-is-not-a-name"),
    ],
)
def test_target_names_reads_the_naming_convention(server_core, params: dict, expected: list[str]) -> None:
    """
    Rollback protection is decided by parameter naming, and this is that decision.

    Pure and free of `bpy`: a command whose object parameter is spelled outside
    these tables gets no state captured and so no rollback, which is exactly the
    silent failure worth pinning.

    Args:
        server_core: The loaded add-on module under test.
        params: One command's params.
        expected: The names that must be protected, in capture order.

    """
    assert server_core.target_names(params) == expected
