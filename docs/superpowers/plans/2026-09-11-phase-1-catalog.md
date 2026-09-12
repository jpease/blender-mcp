# Phase 1 — Catalog Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut the advertised `tools/list` payload from ~328K tokens to ~19K without losing capability and without adding a single domain tool.

**Architecture:** Every change is subtractive or editorial. Bundle definitions are regrouped so a server process registers only the domains its client asked for; the heaviest schemas shed variants they do not need; static reference content moves to MCP Resources; and a typed gateway makes everything that is no longer advertised still reachable. Three gateway machinery tools are the only additions, and they exist to stop advertising 264 others.

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

## Task 4: Split `CORE_MODULES` into `core-shared` and `core-authoring`

`core` is unconditional and carries 24 tools after Task 3. `mesh` (13 tools) and `model` (3) are authoring surfaces that every process pays for.

**Files:**
- Modify: `src/blender_mcp/server/bundles.py:10-20,35`
- Modify: `tests/server/test_bundles.py`

**Interfaces:**
- Consumes: `resolve_toolset_modules` semantics from Task 2.
- Produces: `CORE_MODULES` reduced to the shared set; new bundle `core-authoring`.

- [ ] **Step 1: Update the canary and add the new expectation**

In `tests/server/test_bundles.py`, change `_CORE_TODAY` to the post-split set and add a test:

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/server/test_bundles.py -v`
Expected: FAIL on `test_core_modules_matches_the_documented_core_set` and on `test_core_authoring_bundle_restores_mesh_and_model`.

- [ ] **Step 3: Make the split**

In `src/blender_mcp/server/bundles.py`, change `CORE_MODULES` to drop `"mesh"` and `"model"`, and add to `BUNDLES`:

```python
    "core-authoring": ("mesh", "model"),
```

Update the module docstring to say that `core` is now the shared surface and that mesh/model authoring is opt-in.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/server/test_bundles.py -v`
Expected: PASS.

- [ ] **Step 5: Verify no capability was lost**

Run: `python scripts/measure_catalog.py all`
Expected: **still 285 tools.** `ALL_MODULES` is derived from `CORE_MODULES + BUNDLES`, so a mistake here shows up as a changed total. If the count moved, a module is either duplicated or orphaned.

- [ ] **Step 6: Measure and commit**

Run: `python scripts/measure_catalog.py` — expect ~21 tools.

```bash
git add src/blender_mcp/server/bundles.py tests/server/test_bundles.py
git commit -m "refactor(bundles): split CORE_MODULES into shared and authoring

mesh and model are authoring surfaces that every server process paid for
unconditionally, including shot-assembly processes that never model. They
move to an opt-in core-authoring bundle.

ALL_MODULES still resolves to every module, so 'all' remains 285 tools and
no capability is lost."
```

---

## Task 5: Split `camera` and `texture-lighting`

Camera rigs are six tools worth 18,229 B that a shot reaches rarely. Light *construction* is five tools worth 27,126 B that presets replace. `texture-lighting` bundles two unrelated domains, so shot mode cannot take lighting without texture authoring.

**Files:**
- Modify: `src/blender_mcp/server/bundles.py:22-33`
- Modify: `tests/server/test_bundles.py`

**Interfaces:**
- Consumes: bundle semantics from Task 4.
- Produces: bundles `camera` (core/targeting/animation/shots), `camera-rigs`, `lighting`, `lighting-construction`, `texture`.

- [ ] **Step 1: Confirm the submodule layout before editing**

Run: `ls src/blender_mcp/server/tools/camera/ src/blender_mcp/server/tools/lighting/ src/blender_mcp/server/tools/texture/`
Record the exact submodule filenames. The bundle entries below must match them; do not guess.

- [ ] **Step 2: Write the failing tests**

Append to `tests/server/test_bundles.py`:

```python
_CAMERA_RIG_TOOLS = {
    "create_camera_path_rig",
    "create_crane_camera_rig",
    "create_dolly_camera_rig",
    "create_orbit_camera_rig",
    "duplicate_camera_rig",
    "match_camera_transform",
}

_LIGHT_CONSTRUCTION_TOOLS = {
    "create_light",
    "configure_light",
    "aim_light",
    "configure_light_linking",
    "create_studio_lighting",
}


def test_camera_bundle_excludes_rigs() -> None:
    """The camera bundle covers framing and targeting; rigs are opt-in."""
    assert _tool_names_for_toolsets("camera") & _CAMERA_RIG_TOOLS == set()


def test_camera_rigs_bundle_restores_them() -> None:
    assert _CAMERA_RIG_TOOLS <= _tool_names_for_toolsets("camera-rigs")


def test_lighting_bundle_excludes_construction() -> None:
    """Shot mode gets lighting inspection and environment; construction is opt-in."""
    assert _tool_names_for_toolsets("lighting") & _LIGHT_CONSTRUCTION_TOOLS == set()


def test_lighting_construction_bundle_restores_them() -> None:
    assert _LIGHT_CONSTRUCTION_TOOLS <= _tool_names_for_toolsets("lighting-construction")


def test_texture_is_separable_from_lighting() -> None:
    """Selecting lighting must not drag in the texture-authoring surface."""
    lighting_only = _tool_names_for_toolsets("lighting")
    texture_only = _tool_names_for_toolsets("texture")
    assert texture_only - lighting_only, "texture bundle adds nothing beyond lighting"
    assert "manage_uv_maps" not in lighting_only or "manage_uv_maps" in texture_only
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/server/test_bundles.py -k "camera or lighting or texture" -v`
Expected: FAIL — `Unknown BLENDER_MCP_TOOLSETS bundle(s): camera-rigs`.

- [ ] **Step 4: Rewrite the bundle table**

In `src/blender_mcp/server/bundles.py`, replace the `camera` and `texture-lighting` entries using the real submodule names from Step 1. The shape:

```python
    "camera": ("camera.core", "camera.targeting", "camera.animation", "camera.shots"),
    "camera-rigs": ("camera.rigs",),
    "lighting": ("lighting.inspection", "lighting.environment", "lighting.rendering"),
    "lighting-construction": ("lighting.construction",),
    "texture": ("texture",),
```

**This requires `resolve_toolset_modules` and `tools/__init__.py` to accept dotted submodule paths.** `importlib.import_module(f".{name}", package=__name__)` already handles `"camera.core"` — verify with a quick REPL check before relying on it. Keep `texture-lighting` as a deprecated alias mapping to `("texture", "lighting.inspection", "lighting.environment", "lighting.rendering", "lighting.construction")` so existing client configs keep working; the Global Constraints forbid breaking them.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/server/test_bundles.py -v`
Expected: PASS. `test_every_bundle_module_is_a_real_tools_submodule` also validates the dotted paths import.

- [ ] **Step 6: Verify 'all' is still complete**

Run: `python scripts/measure_catalog.py all`
Expected: **285 tools.** A dotted-path typo shows up here as a missing module.

- [ ] **Step 7: Commit**

```bash
git add src/blender_mcp/server/bundles.py tests/server/test_bundles.py
git commit -m "refactor(bundles): separate camera rigs, light construction and texture

Three bundles conflated surfaces a shot process needs with surfaces it does
not. Camera rigs are six tools and 18,229 B reached rarely enough for a
gateway round trip; light construction is five tools and 27,126 B that
presets replace; and texture-lighting forced a shot process to take texture
authoring in order to get lighting.

texture-lighting is kept as a deprecated alias so existing client configs
continue to resolve. 'all' still registers 285 tools."
```

---

## Task 6: Baseline the shot-mode surface and add a budget regression test

With the splits done, lock the win in so it cannot silently regress.

**Files:**
- Modify: `tests/server/test_catalog_metrics.py`

**Interfaces:**
- Consumes: `payload_report` (Task 1), the new bundle names (Tasks 3–5).
- Produces: a regression ceiling other tasks must not breach.

- [ ] **Step 1: Measure the post-split shot surface**

Run: `python scripts/measure_catalog.py camera,lighting,rendering`
Record tool count, bytes and tokens. Expected in the neighbourhood of 53 tools / ~203,000 B / ~56K tokens per the spec's ladder.

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

**Typed gateway.** This is a design piece, not a refactor: it needs a capability catalog format, a dispatch mechanism that reuses the existing Pydantic validation, and a resolution to the naming collision with the addon handshake's existing `capabilities` field (`server_core.py:1156`, gated at `connection.py:187`). It deserves its own spec section and its own plan.

**Xvfb rig and the §7.1 success bar.** §7.1 states three unresolved limits in the spec itself: no threshold or baseline is defined, "first-try argument validity" is not observable from inside this project because schema-invalid calls are rejected host-side, and a bar tuned on one runtime may not generalize under vendor agnosticism. **A stopping rule with no threshold is not a stopping rule**, and writing tasks against it would encode that gap rather than close it.

**Recommended sequence:** land Tasks 1–6, which are independent of all of the above and deliver the bulk of the win. Then close the §7.1 threshold question and write the bar's plan; the gateway and Resources follow once the bar can tell you whether a cut cost anything.

## Self-review

**Spec coverage.** §4.6's bundle splits: Tasks 3, 4, 5. §4.6's measurement discipline ("never report a payload number without asserting the imported source"): Task 1, Step 6 and `scripts/measure_catalog.py`. §4.6's minimize policy: Task 6's ceiling framing. §8's "Phase 1 adds no domain tools": Global Constraints, enforced by review. **Gaps, stated above rather than papered over:** variant scoping, Resources, gateway, Xvfb rig and success bar are deferred with reasons.

**Placeholder scan.** No "TBD", no "add error handling", no "similar to Task N". Every code step carries the code. Two steps deliberately require the implementer to look something up rather than trust this document — Task 5 Step 1 (confirm real submodule filenames) and Task 6 Step 1 (measure before pinning) — because guessing either would produce a wrong constant.

**Type consistency.** `payload_report` / `payload_bytes` / `tool_bytes` / `PayloadReport` / `BYTES_PER_TOKEN` are defined in Task 1 and used with those exact names in Tasks 3 and 6. `_tool_names_for_toolsets` is defined in Task 3 Step 1 and reused in Task 5. `_CORE_TODAY` is introduced in Task 2 and updated in Task 4 Step 1 — the only symbol this plan deliberately redefines, and the redefinition is a step with a failing-test gate in front of it.

**Known risk this plan does not remove.** `tools/__init__.py` imports submodules dynamically via `importlib.import_module`, so the GitNexus graph cannot resolve those edges — impact analysis on `CORE_MODULES` and `BUNDLES` returns `UNKNOWN` for exactly that reason. Both were confirmed by text search to be referenced only in `bundles.py` and `tests/server/test_bundles.py`. Re-confirm by text search rather than by graph before changing them, and treat `test_every_bundle_module_is_a_real_tools_submodule` plus the `all`-count check in Tasks 4 and 5 as the real safety net.
