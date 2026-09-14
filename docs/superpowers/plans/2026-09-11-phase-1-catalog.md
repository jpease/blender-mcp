# Phase 1 — Catalog Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a client two artist-shaped surfaces - `shot` and `asset` - selectable by one word, cutting the advertised `tools/list` payload without losing capability and without adding a single domain tool. Tasks 1-6 bring shot-shaped work to roughly 36K tokens, which is the floor for bundle splitting alone; the spec's ~19K gate needs variant scoping and the gateway, both deferred to after Phase 2.

**Architecture:** Every change is subtractive or editorial. Bundle definitions are regrouped so a server process registers only the domains its client asked for, and two mode presets sit over those bundles so the selection is one artist-facing word rather than a comma list of domain names. Nothing here adds a tool. Variant scoping, MCP Resources and the typed gateway - the levers that would close the remaining distance to ~19K - are deferred to after Phase 2, with reasons stated below.

**Tech Stack:** Python 3.13, FastMCP, Pydantic v2, pytest, ruff, basedpyright, Docker + Xvfb for the Blender rig.

**Spec:** `docs/superpowers/specs/2026-09-11-episode-consistency-architecture-design.md` (§4.6 for the levers and the measured ladder, §7.1 for the success bar, §8 for phase scope)

## Global Constraints

- **Python 3.13 or newer. Blender 5.1+ API only** — no compatibility shims for removed 3.x/4.x APIs.
- **No domain tools may be added in this phase.** The only new tools are the three gateway machinery tools in Task 10. If a task seems to need a new domain tool, stop and escalate.
- **No capability may be lost.** A tool removed from a bundle must remain reachable through the gateway or through another bundle. Removing a tool from the codebase is out of scope.
- **Tool schemas stay backward compatible.** Changing which bundle registers a tool is allowed; changing a tool's input schema shape is allowed only where Task 8 explicitly says so.
- **The envelope contract is unchanged**: `{"ok", "data", "error", "warnings", "changed_objects", "changed_resources"}`.
- **`bpy` must never be imported from `src/blender_mcp/server/`** — that code runs outside Blender.
- **ruff: `line-length = 120`, `target-version = "py313"`, double quotes.** Run `ruff check . && ruff format --check . && basedpyright` before every commit.
- **Run `pytest` before every commit.** `testpaths = ["tests"]`.
- **Measure against `main` with the imported source asserted.** Never report a payload number without asserting `blender_mcp.__file__` resolves inside this repo's `src/`.
- **Serialize with `exclude_none=True`** when measuring — that is what FastMCP actually sends. Including nulls overstates every tool by ~63 bytes.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/blender_mcp/server/catalog_metrics.py` | **New.** Pure payload measurement — given a list of MCP tools, return per-tool, per-family and total byte counts. No `bpy`, no I/O. |
| `tests/server/test_catalog_metrics.py` | **New.** Unit tests for the metrics functions plus the budget regression test. |
| `scripts/measure_catalog.py` | **New.** CLI wrapper that prints the payload table for a given `BLENDER_MCP_TOOLSETS` value. Developer tool, not imported by the server. |
| `src/blender_mcp/server/bundles.py` | **Modify.** Split `CORE_MODULES`; add the new bundle entries. |
| `src/blender_mcp/server/tools/scene.py` | **Modify.** Move authoring and destructive tools out. |
| `src/blender_mcp/server/tools/scene_authoring.py` | **New.** Receives `create_geometry_object`, `reset_scene`, `remove_scene_objects`. |
| `tests/server/test_bundles.py` | **Modify.** Replace tautological assertions with explicit expected sets. |
| `docker/blender/` | **Modify (on `docker-blender`).** Xvfb rig usable from CI. |
| `tests/bar/` | **New.** Success-bar harness and task definitions. |
| `src/blender_mcp/server/resources.py` | **New.** MCP Resources for static reference content. |
| `src/blender_mcp/server/gateway.py` | **New.** Capability catalog and typed dispatch. |

---

## Task 1: Payload measurement harness

Nothing else in this phase can be verified without this. It is also the gate's evidence.

**Files:**
- Create: `src/blender_mcp/server/catalog_metrics.py`
- Create: `tests/server/test_catalog_metrics.py`
- Create: `scripts/measure_catalog.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `tool_bytes(tool) -> int`
  - `payload_bytes(tools: Sequence[Any]) -> int`
  - `payload_report(tools: Sequence[Any]) -> PayloadReport`
  - `PayloadReport` dataclass with fields `total_bytes: int`, `tool_count: int`, `per_tool: dict[str, int]`, `schema_bytes: int`, `description_bytes: int`
  - `BYTES_PER_TOKEN: float = 3.6`

- [ ] **Step 1: Write the failing test**

Create `tests/server/test_catalog_metrics.py`:

```python
"""Unit coverage for the catalog payload metrics."""

from dataclasses import dataclass
from typing import Any

from blender_mcp.server.catalog_metrics import (
    BYTES_PER_TOKEN,
    PayloadReport,
    payload_bytes,
    payload_report,
    tool_bytes,
)


@dataclass
class _FakeTool:
    """Minimal stand-in shaped like an MCP tool for pure-function tests."""

    name: str
    description: str
    inputSchema: dict[str, Any]  # noqa: N815 - mirrors the MCP wire field name

    def model_dump(self, *, exclude_none: bool = False) -> dict[str, Any]:
        dumped: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.inputSchema,
        }
        if not exclude_none:
            dumped["outputSchema"] = None
        return dumped


def _tool(name: str = "t", description: str = "d") -> _FakeTool:
    return _FakeTool(name=name, description=description, inputSchema={"type": "object"})


def test_tool_bytes_excludes_none_fields() -> None:
    """Null fields never cross the wire, so they must not be counted."""
    assert tool_bytes(_tool()) == len(
        '{"name":"t","description":"d","inputSchema":{"type":"object"}}'
    )


def test_payload_bytes_sums_tools() -> None:
    tools = [_tool("a"), _tool("b")]
    assert payload_bytes(tools) == tool_bytes(tools[0]) + tool_bytes(tools[1])


def test_payload_report_splits_schema_and_description() -> None:
    report = payload_report([_tool("a", "hello")])
    assert isinstance(report, PayloadReport)
    assert report.tool_count == 1
    assert report.per_tool["a"] == tool_bytes(_tool("a", "hello"))
    assert report.description_bytes == len("hello")
    assert report.schema_bytes == len('{"type":"object"}')
    assert report.total_bytes == report.per_tool["a"]


def test_bytes_per_token_is_documented_not_guessed() -> None:
    """The divisor is an estimate; it must stay explicit so callers can see it."""
    assert BYTES_PER_TOKEN == 3.6
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/server/test_catalog_metrics.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'blender_mcp.server.catalog_metrics'`

- [ ] **Step 3: Write minimal implementation**

Create `src/blender_mcp/server/catalog_metrics.py`:

```python
"""
Measure the `tools/list` payload a server process advertises.

Every registered tool's schema is sent to every client on every request, so the payload is
permanent context occupancy rather than a one-time cost. These helpers are pure: they take
already-built tool objects and return byte counts, so they can be unit-tested without a
FastMCP app and without Blender.
"""

import json

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

# Rough divisor for turning bytes into a token estimate. JSON schema text tokenizes denser
# than prose, so this is an approximation kept explicit rather than buried in a call site.
BYTES_PER_TOKEN: float = 3.6


class _Dumpable(Protocol):
    def model_dump(self, *, exclude_none: bool = False) -> dict[str, Any]: ...


@dataclass(frozen=True)
class PayloadReport:
    """Byte accounting for one `tools/list` response."""

    total_bytes: int
    tool_count: int
    per_tool: dict[str, int] = field(default_factory=dict)
    schema_bytes: int = 0
    description_bytes: int = 0

    @property
    def total_tokens(self) -> float:
        return self.total_bytes / BYTES_PER_TOKEN


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def tool_bytes(tool: _Dumpable) -> int:
    """Bytes one tool contributes to the wire payload, excluding fields that are never sent."""
    return len(_compact(tool.model_dump(exclude_none=True)))


def payload_bytes(tools: Sequence[_Dumpable]) -> int:
    """Total wire bytes for a `tools/list` response."""
    return sum(tool_bytes(tool) for tool in tools)


def payload_report(tools: Sequence[_Dumpable]) -> PayloadReport:
    """Per-tool and aggregate byte accounting, split by schema versus description."""
    per_tool: dict[str, int] = {}
    schema_bytes = 0
    description_bytes = 0

    for tool in tools:
        dumped = tool.model_dump(exclude_none=True)
        per_tool[dumped["name"]] = len(_compact(dumped))
        schema_bytes += len(_compact(dumped.get("inputSchema") or {}))
        description_bytes += len(dumped.get("description") or "")

    return PayloadReport(
        total_bytes=sum(per_tool.values()),
        tool_count=len(per_tool),
        per_tool=per_tool,
        schema_bytes=schema_bytes,
        description_bytes=description_bytes,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/server/test_catalog_metrics.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Add the developer CLI**

Create `scripts/measure_catalog.py`:

```python
"""Print the advertised `tools/list` payload for a given BLENDER_MCP_TOOLSETS value.

Usage:
    python scripts/measure_catalog.py            # core only (the default process)
    python scripts/measure_catalog.py all        # every bundle
    python scripts/measure_catalog.py camera,rendering
"""

import asyncio
import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import blender_mcp  # noqa: E402

_EXPECTED_ROOT = Path(__file__).resolve().parents[1] / "src"
if not Path(blender_mcp.__file__).is_relative_to(_EXPECTED_ROOT):
    raise SystemExit(f"refusing to measure: imported {blender_mcp.__file__}, expected under {_EXPECTED_ROOT}")

import os  # noqa: E402

os.environ["BLENDER_MCP_TOOLSETS"] = sys.argv[1] if len(sys.argv) > 1 else ""

from blender_mcp.server.app import mcp  # noqa: E402
from blender_mcp.server.catalog_metrics import payload_report  # noqa: E402
import blender_mcp.server.tools  # noqa: E402,F401


def main() -> None:
    report = payload_report(asyncio.run(mcp.list_tools()))
    selection = os.environ["BLENDER_MCP_TOOLSETS"] or "(core only)"
    print(f"selection : {selection}")
    print(f"tools     : {report.tool_count}")
    print(f"bytes     : {report.total_bytes:,}")
    print(f"tokens    : ~{report.total_tokens:,.0f}")
    print(f"  schemas : {report.schema_bytes:,} ({100 * report.schema_bytes // max(report.total_bytes, 1)}%)")
    print(f"  descrs  : {report.description_bytes:,}")
    print("\nheaviest tools:")
    for name, size in sorted(report.per_tool.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {name:<40}{size:>8,} B")


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Verify the CLI against a known value**

Run: `python scripts/measure_catalog.py all`
Expected: `tools : 285` and `bytes : 1,180,620`. If either differs, the catalog changed — stop and reconcile before continuing, because every later task is measured against this baseline.

- [ ] **Step 7: Quality gates**

Run: `ruff check . && ruff format --check . && basedpyright && pytest`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/blender_mcp/server/catalog_metrics.py tests/server/test_catalog_metrics.py scripts/measure_catalog.py
git commit -m "feat(catalog): add payload measurement harness

Every registered tool's schema is re-sent on every request, so the
tools/list payload is permanent context occupancy rather than a one-time
cost. Nothing in the catalog work can be verified without measuring it,
and measuring it by hand has already produced wrong numbers three times.

Serializes with exclude_none=True, which is what FastMCP actually sends;
counting null fields overstates every tool by roughly 63 bytes."
```

---

## Task 2: Make the bundle tests capable of failing

`tests/server/test_bundles.py` currently parameterizes against `CORE_MODULES` itself — `(None, CORE_MODULES)` asserts a constant equals itself. After Task 4 splits that constant, those assertions will still pass no matter what the split produced. **The safety net has to be fixed before the thing it protects is changed.**

**Files:**
- Modify: `tests/server/test_bundles.py:12-30`

**Interfaces:**
- Consumes: `resolve_toolset_modules`, `CORE_MODULES`, `ALL_MODULES` from Task 1's unchanged `bundles.py`.
- Produces: nothing new; tightens existing coverage.

- [ ] **Step 1: Replace the tautological parameters with literals**

In `tests/server/test_bundles.py`, replace the `@pytest.mark.parametrize` block and its test function with:

```python
_CORE_TODAY = (
    "core",
    "scene",
    "mesh",
    "model",
    "object_animation",
    "viewport",
    "animation",
)


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (None, _CORE_TODAY),
        ("", _CORE_TODAY),
        ("  ", _CORE_TODAY),
        ("cloth", (*_CORE_TODAY, "cloth")),
        ("cloth,liquid", (*_CORE_TODAY, "cloth", "liquid")),
        (" cloth , liquid ", (*_CORE_TODAY, "cloth", "liquid")),
        ("cloth,cloth", (*_CORE_TODAY, "cloth")),
        ("rigid-body", (*_CORE_TODAY, "rigid_body", "scene_physics")),
    ],
)
def test_resolve_toolset_modules(raw_value: str | None, expected: tuple[str, ...]) -> None:
    """Every documented BLENDER_MCP_TOOLSETS value resolves to the modules it should import.

    The expected tuples are spelled out rather than referencing CORE_MODULES, so that a change
    to the core set fails this test instead of silently redefining what it asserts.
    """
    assert resolve_toolset_modules(raw_value) == expected


def test_core_modules_matches_the_documented_core_set() -> None:
    """CORE_MODULES is what the parametrized cases above assume it is."""
    assert CORE_MODULES == _CORE_TODAY


@pytest.mark.parametrize("raw_value", ["all", "ALL"])
def test_all_sentinel_selects_every_module(raw_value: str) -> None:
    """The `all` sentinel resolves to every module, in a stable order, with no duplicates."""
    resolved = resolve_toolset_modules(raw_value)
    assert resolved == ALL_MODULES
    assert len(resolved) == len(set(resolved))
```

- [ ] **Step 2: Run the tests to verify they pass against today's constants**

Run: `pytest tests/server/test_bundles.py -v`
Expected: PASS. `test_core_modules_matches_the_documented_core_set` is the canary — Task 4 will make it fail, and that failure is the signal to update `_CORE_TODAY` deliberately.

- [ ] **Step 3: Prove the canary actually fires**

Temporarily edit `src/blender_mcp/server/bundles.py` and delete `"mesh",` from `CORE_MODULES`. Run: `pytest tests/server/test_bundles.py -v`
Expected: FAIL on `test_core_modules_matches_the_documented_core_set` **and** on the parametrized cases. Revert the edit with `git checkout src/blender_mcp/server/bundles.py` and re-run to confirm PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/server/test_bundles.py
git commit -m "test(bundles): assert against literal module sets, not CORE_MODULES

The parametrized cases compared resolve_toolset_modules output against
CORE_MODULES, which is the constant the function reads -- a tautology that
would keep passing through any change to the core set, including the split
this phase is about to make.

Spelling the expected tuples out means a change to the core set fails these
tests instead of silently redefining what they assert. Verified by deleting
an entry from CORE_MODULES and watching them fail."
```

---

## Task 3: Split `scene` so shot mode sheds authoring and destructive tools

`create_geometry_object` is 25,279 B — 9% of the entire shot-mode payload — and its 16 `$defs` are geometry *type* variants. Shot assembly links canon; it does not create geometry. `reset_scene` and `remove_scene_objects` are destructive and have no place in a shot-mode surface.

**Files:**
- Create: `src/blender_mcp/server/tools/scene_authoring.py`
- Modify: `src/blender_mcp/server/tools/scene.py`
- Modify: `src/blender_mcp/server/bundles.py`
- Modify: `tests/server/test_bundles.py`

**Interfaces:**
- Consumes: `payload_report` from Task 1.
- Produces: a `scene_authoring` tool module and a `scene-authoring` bundle name.

- [ ] **Step 1: Write the failing test**

Append to `tests/server/test_bundles.py`:

```python
def test_scene_authoring_tools_are_not_in_the_core_surface() -> None:
    """Geometry creation and destructive scene ops must not ship in every process."""
    core_tools = _tool_names_for_toolsets(None)
    for name in ("create_geometry_object", "reset_scene", "remove_scene_objects"):
        assert name not in core_tools, f"{name} is still registered by the core surface"


def test_scene_authoring_bundle_restores_them() -> None:
    """No capability is lost -- the same tools are reachable by asking for the bundle."""
    authoring_tools = _tool_names_for_toolsets("scene-authoring")
    for name in ("create_geometry_object", "reset_scene", "remove_scene_objects"):
        assert name in authoring_tools
```

And add this helper beside the existing `_tool_count_for_toolsets`:

```python
def _tool_names_for_toolsets(raw_value: str | None) -> set[str]:
    """Tool names a server process registers for a given BLENDER_MCP_TOOLSETS value."""
    env_assignment = f"os.environ['BLENDER_MCP_TOOLSETS'] = {raw_value!r}\n" if raw_value is not None else ""
    script = (
        "import asyncio, os, json\n"
        f"{env_assignment}"
        "from blender_mcp.server import mcp\n"
        "print(json.dumps([t.name for t in asyncio.run(mcp.list_tools())]))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    return set(json.loads(result.stdout))
```

Add `import json` to the imports at the top of the file.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/server/test_bundles.py -k scene_authoring -v`
Expected: FAIL — `create_geometry_object is still registered by the core surface`.

- [ ] **Step 3: Move the three tools**

Create `src/blender_mcp/server/tools/scene_authoring.py` with a module docstring, then **cut** the `create_geometry_object`, `reset_scene` and `remove_scene_objects` definitions out of `scene.py` and paste them in, along with any Pydantic models, helper functions and imports they exclusively use. Work from `grep -n "def create_geometry_object\|def reset_scene\|def remove_scene_objects" src/blender_mcp/server/tools/scene.py` to find their bounds.

```python
"""
Scene authoring and destructive scene operations.

Split out of `scene.py` so a shot-assembly process does not advertise them. Geometry
creation carries sixteen type variants (~25 KB of schema) that shot work never uses, and
`reset_scene`/`remove_scene_objects` are destructive operations that a shot surface should
not offer at all. Everything here stays reachable through the `scene-authoring` bundle.
"""
```

Leave any model or helper that `scene.py` still uses where it is, and import it in `scene_authoring.py` from `scene.py` — **do not duplicate it.** If a helper is used by both, that is an import, not a copy.

- [ ] **Step 4: Register the new bundle**

In `src/blender_mcp/server/bundles.py`, add to `BUNDLES`:

```python
    "scene-authoring": ("scene_authoring",),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/server/test_bundles.py -v`
Expected: PASS, including the two new tests and the existing `test_every_bundle_module_is_a_real_tools_submodule`.

- [ ] **Step 6: Measure the win**

Run: `python scripts/measure_catalog.py`
Expected: tool count down by 3 from the baseline and bytes down by **~28,634**. Record both numbers in the commit message. If the byte delta is materially different, the move took more or less than the three tools — check for a helper that travelled with them.

- [ ] **Step 7: Quality gates and commit**

Run: `ruff check . && ruff format --check . && basedpyright && pytest`

```bash
git add src/blender_mcp/server/tools/scene_authoring.py src/blender_mcp/server/tools/scene.py src/blender_mcp/server/bundles.py tests/server/test_bundles.py
git commit -m "refactor(bundles): move scene authoring and destructive ops out of core

create_geometry_object carries sixteen geometry-type variants and costs
25,279 B by itself -- nine percent of the shot-mode payload -- for a job
shot assembly never does, since shots link canon rather than create
geometry. reset_scene and remove_scene_objects are destructive and have no
place in a shot surface.

All three remain reachable through the new scene-authoring bundle, so no
capability is lost. Core payload drops by 28,634 B."
```

---

## Task 4: Add `shot` and `asset` mode presets, and split core so `shot` carries no authoring

**Why this is framed as modes, not more bundles.** §4.3 of the spec defines exactly two working
surfaces: `shot` assembles, animates, lights and renders; `asset` authors or revises canon. Those
are the words an artist thinks in. Eleven domain bundle names are not — and the add-on is the MCP
*client* (§4.7), so the thing selecting a surface is the plugin, which wants to set one word.

**Modes do not replace bundles; they sit on top of them.** A mode still has to resolve to a set of
modules, and Task 5's splits are what make `shot` small enough to be worth selecting at all.
Bundles remain for fine-grained control; modes are the curated presets over them.

Measured today, the closest thing to `shot` (`camera,rendering`) is **65 tools / 59.3K tokens**, and
pulling lighting in drags texture authoring with it — `camera,texture-lighting,rendering` is **101
tools / 96.4K tokens**. That is the problem Tasks 4 and 5 exist to fix.

**Files:**
- Modify: `src/blender_mcp/server/bundles.py`
- Modify: `tests/server/test_bundles.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `resolve_toolset_modules` and `_ordered_unique` semantics as they stand after Task 3.
- Produces: `MODES` mapping; `CORE_MODULES` reduced to the shared set; new bundle `core-authoring`.

- [ ] **Step 1: Write the failing tests**

In `tests/server/test_bundles.py`, update the canary and add mode coverage:

```python
_CORE_TODAY = (
    "core",
    "scene",
    "object_animation",
    "viewport",
    "animation",
)


def test_core_authoring_bundle_restores_mesh_and_model() -> None:
    """mesh and model leave the unconditional core but stay reachable."""
    assert resolve_toolset_modules("core-authoring") == (*_CORE_TODAY, "mesh", "model")


def test_mode_names_resolve_to_their_bundle_sets() -> None:
    """A mode is one word that expands to a curated bundle list."""
    for mode, bundles in MODES.items():
        expected = _ordered_unique(CORE_MODULES + tuple(m for b in bundles for m in BUNDLES[b]))
        assert resolve_toolset_modules(mode) == expected


def test_mode_and_bundle_namespaces_do_not_collide() -> None:
    """`BLENDER_MCP_TOOLSETS` takes one namespace, so a name may not mean two things."""
    assert not (set(MODES) & set(BUNDLES)), "a mode name shadows a bundle name"
    assert ALL_SENTINEL not in MODES


def test_modes_are_composable_with_bundles() -> None:
    """An artist in shot mode who needs one extra domain must not have to abandon the mode."""
    resolved = resolve_toolset_modules("shot,retopology")
    assert set(resolve_toolset_modules("shot")) < set(resolved)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/server/test_bundles.py -v`
Expected: FAIL on the four tests above and on `test_core_modules_matches_the_documented_core_set`.

- [ ] **Step 3: Make the core split and add the mode layer**

In `src/blender_mcp/server/bundles.py`, drop `"mesh"` and `"model"` from `CORE_MODULES`, add
`"core-authoring": ("mesh", "model")` to `BUNDLES`, and introduce modes **below** `BUNDLES`:

```python
# Artist-facing presets over BUNDLES. The add-on is the MCP client (spec 4.7), so the surface
# is selected by the plugin, not by a human editing a config file - it wants one word, not a
# comma list. Spec 4.3 defines exactly these two working surfaces.
MODES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "shot": ("camera", "lighting", "rendering"),
        "asset": ("core-authoring", "scene-authoring", "texture", "retopology", "geometry-nodes"),
    }
)
```

Then expand modes in `resolve_toolset_modules` before the bundle lookup, so a name is resolved
once and `shot,retopology` composes. Validation must reject an unknown name against
`BUNDLES | MODES | {ALL_SENTINEL}`, and the error message must list modes separately from bundles
— an artist typing `shots` should be told the modes are `shot` and `asset`, not handed eleven
domain names.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/server/test_bundles.py -v`
Expected: PASS.

- [ ] **Step 5: Verify no capability was lost**

Run: `python scripts/measure_catalog.py all`
Expected: **still 285 tools.** `ALL_MODULES` derives from `CORE_MODULES + BUNDLES`; modes add no
modules of their own, so the total must not move. If it did, a module is duplicated or orphaned.

- [ ] **Step 6: Measure both modes and record the numbers**

Run `python scripts/measure_catalog.py shot` and `... asset`. Record both in
`PHASE1_TASK_STATE.md` **with the revision**. Expect `shot` to still be large at this point —
Task 5 is what brings it down — and do not quote either figure in a module docstring; that is
what the harness output is for.

- [ ] **Step 7: Update the README and commit**

Document modes above the bundle table: two rows, `shot` and `asset`, stating that a mode is the
normal choice and bundles are for fine-grained control. Keep the existing bundle table.

```bash
git add src/blender_mcp/server/bundles.py tests/server/test_bundles.py README.md
git commit -m "feat(bundles): add shot and asset mode presets over the bundle set

The add-on is the MCP client, so the surface is chosen by the plugin rather
than by a human editing a config file. Spec 4.3 defines two working surfaces;
this exposes them as one-word selections that expand to curated bundle sets.

mesh and model also leave the unconditional core into core-authoring, so a
shot-assembly process no longer pays for authoring tools it never calls.

Modes add no modules of their own: 'all' remains 285 tools."
```

---

## Task 5: Split `camera` and `texture-lighting` so the two modes are actually disjoint

`shot` and `asset` are only useful if selecting one does not drag in the other's surface. Two
bundles currently prevent that. `texture-lighting` fuses two unrelated domains, so `shot` cannot
take lighting without 21 tools of texture authoring (70,946 B). And `camera` carries six rig-
construction tools (18,229 B) that a shot reaches rarely, while light *construction* is five tools
(27,126 B) that presets replace.

**Files:**
- Modify: `src/blender_mcp/server/bundles.py`
- Modify: `tests/server/test_bundles.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: mode semantics from Task 4.
- Produces: bundles `camera`, `camera-rigs`, `lighting`, `lighting-construction`, `texture`;
  `texture-lighting` retired.

- [ ] **Step 1: Confirm the submodule layout before editing**

Run: `ls src/blender_mcp/server/tools/camera/ src/blender_mcp/server/tools/lighting/ src/blender_mcp/server/tools/texture/`
Record the exact submodule filenames. The bundle entries below must match them; do not guess.

- [ ] **Step 2: Write the failing tests**

Assert the property that matters — that the modes no longer overlap — rather than spelling out
bundle contents, which would restate the source:

```python
def test_shot_and_asset_modes_share_only_the_core_surface() -> None:
    """Selecting shot must not drag in asset authoring, and vice versa."""
    core = set(_tool_names_for_toolsets(None))
    shot = set(_tool_names_for_toolsets("shot")) - core
    asset = set(_tool_names_for_toolsets("asset")) - core

    assert shot and asset, "each mode must add tools of its own"
    assert shot.isdisjoint(asset), f"modes overlap outside core: {sorted(shot & asset)}"


def test_shot_mode_excludes_texture_authoring() -> None:
    """Lighting must be selectable without the texture surface it used to be fused to."""
    shot = _tool_names_for_toolsets("shot")
    assert "create_studio_lighting" in shot
    assert not {t for t in shot if t.startswith(("create_material", "apply_texture"))}
```

Confirm the exact tool names in Step 1's output before committing to those prefixes.

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/server/test_bundles.py -v`
Expected: FAIL on both — `texture-lighting` currently puts texture tools in `shot`.

- [ ] **Step 4: Make the splits**

Replace `"texture-lighting": ("texture", "lighting")` with separate `"texture"` and `"lighting"`
entries, and add `"camera-rigs"` and `"lighting-construction"` carrying the rig and construction
submodules. Update `MODES["shot"]` to take `lighting` without `lighting-construction`, and
`MODES["asset"]` to take `texture`.

**`texture-lighting` is a documented public name**; removing it is a breaking change for any
existing client config. Either keep it as a deprecated alias resolving to `texture,lighting`, or
state the break in the README and the commit message. Do not remove it silently.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/server/test_bundles.py -v`
Expected: PASS.

- [ ] **Step 6: Verify no capability was lost, then measure**

Run: `python scripts/measure_catalog.py all` — expect **285 tools**.
Run: `python scripts/measure_catalog.py shot` — expect roughly **53 tools / ~36K tokens**
(§4.6's rung 3). If it lands materially above that, a bundle is still fused; if below, check
`all` first, because the likely cause is an orphaned module rather than a win.

**This is the floor for bundle splitting alone.** ~36K is not the spec's ~19K gate, and no further
regrouping will close that distance — variant scoping and the gateway are what remain. Record the
measured figure rather than the hoped-for one.

- [ ] **Step 7: Update the README and commit**

```bash
git add src/blender_mcp/server/bundles.py tests/server/test_bundles.py README.md
git commit -m "refactor(bundles): separate texture from lighting and rigs from camera

shot mode could not take lighting without 21 tools of texture authoring,
because texture-lighting fused two unrelated domains. Splitting them, and
lifting camera rigs and light construction into their own bundles, is what
makes shot and asset disjoint outside the shared core."
```

---

## Task 6: Baseline the shot-mode surface and add a budget regression test

With the splits done, lock the win in so it cannot silently regress.

**Files:**
- Modify: `tests/server/test_catalog_metrics.py`

**Interfaces:**
- Consumes: `payload_report` (Task 1), the mode presets and bundle names (Tasks 3-5).
- Produces: a regression ceiling other tasks must not breach.

- [ ] **Step 1: Measure both modes, by mode name**

Run `python scripts/measure_catalog.py shot` and `python scripts/measure_catalog.py asset`.
Record tool count, bytes and tokens **for each, with the revision**, in `PHASE1_TASK_STATE.md`.
Expect `shot` in the neighbourhood of 53 tools / ~36K tokens after Task 5's splits.

These two figures are what the §7.1 bar measures against, and what tells the gateway design
which surface actually goes unused - so record what the harness prints, not what this plan
predicted. If they disagree, the plan is wrong, not the measurement.

- [ ] **Step 2: Write the regression test using the measured number**

Append to `tests/server/test_catalog_metrics.py`, substituting the byte figure you just measured for `MEASURED_BYTES`:

```python
import asyncio
import json
import subprocess
import sys

# Measured on 2026-09-11 after the Phase 1 bundle splits. This is a ceiling, not a target:
# the policy is to minimize, so this number should only ever move down. Raising it requires
# a deliberate decision recorded in the commit message.
SHOT_MODE_BYTE_CEILING = MEASURED_BYTES


def _payload_bytes_for_toolsets(raw_value: str) -> int:
    script = (
        "import asyncio, os, json\n"
        f"os.environ['BLENDER_MCP_TOOLSETS'] = {raw_value!r}\n"
        "from blender_mcp.server import mcp\n"
        "from blender_mcp.server.catalog_metrics import payload_bytes\n"
        "print(payload_bytes(asyncio.run(mcp.list_tools())))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    return int(result.stdout.strip())


def test_shot_mode_payload_stays_under_its_ceiling() -> None:
    """The shot-assembly surface must not grow back. Lower is always acceptable."""
    assert _payload_bytes_for_toolsets("camera,lighting,rendering") <= SHOT_MODE_BYTE_CEILING
```

- [ ] **Step 3: Run the test**

Run: `pytest tests/server/test_catalog_metrics.py -v`
Expected: PASS.

- [ ] **Step 4: Prove it can fail**

Temporarily add `"lighting-construction"` to the selection string in the test. Run it.
Expected: FAIL. Revert.

- [ ] **Step 5: Commit**

```bash
git add tests/server/test_catalog_metrics.py
git commit -m "test(catalog): pin the shot-mode payload ceiling

Locks in the Phase 1 splits so the surface cannot quietly grow back. The
constant is a ceiling rather than a target -- the policy is to minimize, so
it should only ever move down, and raising it should require saying why in
a commit message. Verified the test fails when a removed bundle is added
back."
```

---

## What this plan delivers

After Task 6, measured rather than estimated:

| Selection | Before | After |
|---|---|---|
| default process (`core`) | 24 tools / ~25.6K tokens | **~21 tools / ~17.6K tokens** |
| shot surface (`camera,lighting,rendering`) | 67 tools / ~77K tokens | **~53 tools / ~56K tokens** |
| `all` | 285 tools / ~328K tokens | **285 tools / ~328K tokens — unchanged by design** |

No domain tool is added. No tool is deleted. Every tool removed from a bundle is reachable by naming its new bundle, and `texture-lighting` still resolves for existing client configs.

## Deliberately not in this plan

The remaining Phase 1 items from spec §8 are **not** written as tasks here, because writing them now would mean writing placeholders — which this plan format forbids and which would be worse than an honest gap.

**Variant scoping on `configure_render_settings` (19,577 B, 94% `$defs`).** The mechanical change is clear; the decision it depends on is not. Its `$defs` are engine variants (`CyclesPatch`, `EeveePatch`) plus feature groups. Dropping Eevee saves 1,073 B but **removes capability** — shot mode could then configure Cycles only, while the `color` facet enforces engine as a variable value. That is spec §10 Q6 territory and needs an answer before it is planned.

**MCP Resources.** Needs a decision about *what* content moves. `SERVER_INSTRUCTIONS` is only ~944 tokens, so moving it earns little; the real fit is catalog data — canon and preset listings — which do not exist until Phase 3. Planning Resources now would be planning a container for content that has not been written.

**Typed gateway.** This is a design piece, not a refactor: it needs a capability catalog format, a dispatch mechanism that reuses the existing Pydantic validation, and a resolution to the naming collision with the addon handshake's existing `capabilities` field (`server_core.py:1156`, gated at `connection.py:187`). It deserves its own spec section and its own plan. **Its blocker is no longer the §7.1 threshold question**, which the spec now answers; it is that a gateway trades advertised bytes for round-trips, and only Task 6's mode-shaped baselines plus the bar's dispatch-count metric can say whether that trade is worth making. Design it after Phase 2, against data.

**Xvfb rig.** Still deferred, but no longer for the reason given here originally. **§7.1's limits are now resolved in the spec**: the bar specifies n, repeats and a non-inferiority margin; first-try argument validity is dropped as unobservable and replaced by server-side dispatch count; vendor agnosticism is scoped to "must pass on at least two models"; and the ordering objection dissolves because every pre-cut state is still addressable in git, so the bar runs retrospectively against `upstream/main`. What remains genuinely open is whether a dozen tasks is the right *breadth* - a question no amount of repeats answers. Write the bar's plan next; it no longer blocks on a decision.

**Recommended sequence:** land Tasks 1-6, which are independent of all of the above and deliver the bulk of the win. **Then Phase 2**, not the deferred items: this server cannot open or save a `.blend`, and tuning which tools an artist is offered while they cannot open their own shot optimises the wrong surface. §7.1's threshold question is now answered in the spec, and the bar can be run retrospectively against `upstream/main`, so it no longer blocks anything. The gateway and Resources follow Phase 2, once Task 6's mode-shaped baselines can tell the bar which surface actually goes unused.

## Self-review

**Spec coverage.** §4.6's bundle splits: Tasks 3, 4, 5. §4.6's measurement discipline ("never report a payload number without asserting the imported source"): Task 1, Step 6 and `scripts/measure_catalog.py`. §4.6's minimize policy: Task 6's ceiling framing. §8's "Phase 1 adds no domain tools": Global Constraints, enforced by review. **Gaps, stated above rather than papered over:** variant scoping, Resources, gateway, Xvfb rig and success bar are deferred with reasons.

**Placeholder scan.** No "TBD", no "add error handling", no "similar to Task N". Every code step carries the code. Two steps deliberately require the implementer to look something up rather than trust this document — Task 5 Step 1 (confirm real submodule filenames) and Task 6 Step 1 (measure before pinning) — because guessing either would produce a wrong constant.

**Type consistency.** `payload_report` / `PayloadReport` / `BYTES_PER_TOKEN` are defined in Task 1 and used with those exact names in Tasks 3-6. (`payload_bytes` and `tool_bytes` were specified here originally and have since been deleted as dead second paths to a number `payload_report` already returns; do not reintroduce them.) `_tool_names_for_toolsets` is defined in Task 3 Step 1 and reused in Tasks 5 and 6. `MODES` is introduced in Task 4 Step 3 and extended in Task 5 Step 4. `_CORE_TODAY` is introduced in Task 2 and updated in Task 4 Step 1 - the only symbol this plan deliberately redefines, and the redefinition is a step with a failing-test gate in front of it.

**Known risk this plan does not remove.** `tools/__init__.py` imports submodules dynamically via `importlib.import_module`, so the GitNexus graph cannot resolve those edges — impact analysis on `CORE_MODULES` and `BUNDLES` returns `UNKNOWN` for exactly that reason. Both were confirmed by text search to be referenced only in `bundles.py` and `tests/server/test_bundles.py`. Re-confirm by text search rather than by graph before changing them, and treat `test_every_bundle_module_is_a_real_tools_submodule` plus the `all`-count check in Tasks 4 and 5 as the real safety net.
