# Audit: Liquid/Fluid Simulation Domain (Rubric Section 11)

**Scope**: Audit the Mantaflow-backed liquid and gas simulation tools for production-grade readiness to handle natural-language requests like "fill this glass with realistic water" / "simulate liquid being poured into it."

**Test Prompt**: Fill a glass with water via a pour source, with domain auto-sizing and collision detection.

---

## 1. Tool Inventory

### A. Canonical Cross-Domain Tools (`fluid.py`)

Located: `src/blender_mcp/server/tools/fluid.py` (261 lines)

These 6 tools accept a `domain_type: Literal["LIQUID", "GAS"]` parameter and forward to shared addon handlers. **All forward to `_call()` in `liquid/_shared.py`, making them a unified transport layer.**

| Tool | Line | Purpose | Params | Abstraction | Notes |
|------|------|---------|--------|-------------|-------|
| `inspect_fluid_simulation` | 95 | List domains/cache state | domain_type, scene_name, domain_object_name, limit, offset | **Low**: Read-only inspection; delegates to addon | Pagination included |
| `create_fluid_domain` | 118 | Create LIQUID/GAS domain box or on mesh | domain_type, scene_name, cache_directory, dimensions, resolution_max, cache_type | **Low**: Direct Blender modifier creation, no orchestration | Validates cache_frame_end >= cache_frame_start client-side |
| `configure_fluid_solver` | 148 | Patch solver settings | domain_type, FluidSolverPatch (validates timesteps_min ≤ timesteps_max) | **Medium**: Patch model validation; rejects finite=False values for gas | Shared model; both LIQUID and GAS fields present |
| `add_fluid_flow` | 170 | Register mesh as flow source | domain_type, gas_flow_type ("SMOKE"/"FIRE"/"BOTH"), FluidFlowPatch | **Low**: Thin wrapper around modifier attachment | `gas_flow_type` only applies when domain_type="GAS" |
| `add_fluid_effector` | 200 | Register collision/guide effector | domain_type, EffectorType ("COLLISION"/"GUIDE"), LiquidEffectorPatch | **Low**: Attaches modifier to object; no collision proxy logic | Uses shared liquid-side patch model |
| `manage_fluid_cache` | 228 | Inspect/bake/free cache stages | domain_type, action (STATUS/BAKE_DATA/BAKE_ALL/CANCEL/PAUSE/RESUME/FREE_*), LiquidCachePatch | **Low**: Dispatcher; bake runs synchronously or as WM job | Forwards to addon's `handle_manage_liquid_cache` (despite name, supports GAS too) |

**Observation**: All 6 tools use `liquid/_shared.py:_call()` as transport, not a separate gas-specific handler module. The domain_type parameter is purely informational to Blender; there is **no separate GAS handler surface** in the addon (only `liquid/` subdirectory observed).

---

### B. Liquid-Specific Setup & Inspection Tools

Located: `src/blender_mcp/server/tools/liquid/inspection_and_setup.py` (493 lines)

| Tool | Line | Purpose | Params | Abstraction | Overlap with fluid.py |
|------|------|---------|--------|-------------|----------------------|
| `get_liquid_simulation_info` | 124 | Inspect domains + dependencies | scene_name, domain_uuid (stable custom property), limit, offset | **Medium**: Adds UUID-stable resolution and dependency discovery | **OVERLAPS** `inspect_fluid_simulation` (domain_type="LIQUID"); this adds UUID/dependency info |
| `get_fluid_object_info` | 163 | Inspect one domain/flow/effector | object_name | **Low**: Single object bounds/transforms | **NEW**: no fluid.py equivalent |
| `create_liquid_domain` | 174 | Create domain with solver defaults | scene_name, cache_directory, dimensions, location, SimulationMethod, time_scale, timesteps_min/max, cfl_condition | **Medium**: Presets liquid-specific defaults (FLIP method, time_scale=1.0, adaptive timesteps) | **OVERLAPS** `fluid:create_fluid_domain` but LIQUID-only; adds solver defaults |
| `fit_liquid_domain` | 235 | Auto-size unbaked domain from motion | source_object_names, collider_object_names, sample_frame_start/end (max 32 frames), padding, expected_travel, splash_height | **HIGH**: Solves "size the domain to the glass" problem; samples up to 32 frames, restores current frame, pads/predicts motion | **NEW**: Unique liquid-only orchestration; no gas equivalent |
| `configure_liquid_solver` | 287 | Patch liquid solver | LiquidSolverPatch (27 liquid-specific fields: particle_randomness, use_fractions, fractions_threshold, use_flip_particles with RNA quirk) | **Medium**: Validates ranges; documents use_flip_particles toggle semantics | **OVERLAPS** `fluid:configure_fluid_solver` but liquid-only; more fields |
| `add_liquid_flow` | 307 | Register liquid mesh flow | object_name, domain_object_name, modifier_name, FlowBehavior, LiquidFlowPatch (rejects use_particle_size/"smoke-only fields") | **Medium**: Rejects incompatible smoke-only fields at schema level | **OVERLAPS** `fluid:add_fluid_flow` (domain_type="LIQUID"); adds explicit rejection of smoke fields |
| `configure_liquid_flow` | 339 | Patch flow settings post-creation | object_name, modifier_name, domain_object_name, LiquidFlowPatch | **Medium**: Patch model; LiquidFlowPatch documents Use Flow toggle semantics | **NEW**: no fluid.py equivalent (fluid.py is post-creation only) |
| `add_liquid_effector` | 366 | Register collision/guide effector | object_name, domain_object_name, EffectorType, LiquidEffectorPatch | **LOW**: Thin wrapper | **OVERLAPS** `fluid:add_fluid_effector` (domain_type="LIQUID") |
| `configure_liquid_effector` | 397 | Patch effector settings post-creation | object_name, modifier_name, domain_object_name, LiquidEffectorPatch | **Medium**: Patch model | **NEW**: no fluid.py equivalent |
| `configure_liquid_scope_and_boundaries` | 422 | Set domain collection scope + open/close faces | domain_object_name, modifier_name, flow/effector/force_collection_name, boundaries (LiquidBoundaryPatch: front/back/left/right/top/bottom as bool) | **Medium**: Collection management + per-face collision toggle | **NEW**: unique to liquid |
| `estimate_liquid_resources` | 463 | Estimate grid dimensions and relative cost | domain_object_name, modifier_name | **MEDIUM**: Calculates cell count, relative cost index = base_cells × (1.0 + particle_factor×0.35 + mesh_multiplier×0.2 + secondary_count×0.3) × frames. **Does NOT return absolute disk/memory/time estimates.** | **NEW**: no fluid.py equivalent |
| `validate_liquid_setup` | 473 | Preflight check: domains, dependencies, cache readiness | scene_name, domain_object_names (optional), max_findings | **HIGH**: Non-mutating validation; checks object existence, thin walls, cache directory writable, collection scopes, flow/effector registration | **NEW**: no fluid.py equivalent; critical pre-bake readiness |

---

### C. Liquid Delivery & Proxy Tools

Located: `src/blender_mcp/server/tools/liquid/delivery.py` (282 lines)

| Tool | Line | Purpose | Abstraction |
|------|------|---------|-------------|
| `create_liquid_proxy_rig` | 69 | Create low-cost proxy (BOX/CAPSULE/CONVEX_HULL/DECIMATED/HOLLOW_CONTAINER/SUPPLIED) that drives a flow or effector | **HIGH**: Solves "don't emit from the actual pour object, use a lightweight stand-in." Geometry="HOLLOW_CONTAINER" creates a live Solidify modifier, removes rim cap, auto-detects wall/bottom thickness. Supports COPY_TRANSFORMS or PARENT driver. |
| `duplicate_liquid_setup_variant` | 132 | Clone complete domain setup with remapped members, independent cache | **MEDIUM**: Clones domain + all flows/effectors/guides/forces; one domain disabled; mesh/material/animation copy/link policies selectable. |
| `prepare_liquid_render_mesh` | 170 | Add reversible post-fluid modifiers (Subdivision/Smooth/Laplacian) or create explicit current-frame delivery mesh | **MEDIUM**: Applies LiquidRenderFinish (smooth shading, subdivision levels, laplacian smoothing). Optional "delivery mesh" for playback-time frame evaluation (REPLAY domains only). |
| `export_liquid_simulation` | 208 | Atomically export baked surface ± secondary particles to Alembic or USD | **MEDIUM**: Frame range, coordinate space (WORLD/LOCAL), units, axis conventions, material inclusion. Max 500 frames per call; overwrite policy. |
| `analyze_liquid_performance` | 258 | Report structural cost + optional frame-evaluation timings | **LOW**: Bounded structural evidence (object count, cache entries); optional measured replay performance (30s timeout). |

---

### D. Animation & Time-Keying Tools

Located: `src/blender_mcp/server/tools/liquid/animation.py` (58 lines)

| Tool | Line | Purpose |
|------|------|---------|
| `animate_liquid_flow` | 36 | Key flow settings (use_inflow, velocity_factor, velocity_normal, velocity_random) with per-keyframe interpolation (CONSTANT/LINEAR/BEZIER) and merge policy (INSERT_ONLY/REPLACE_EXISTING) | Validates exactly one property per keyframe |

---

### E. Force Fields & Guides

Located: `src/blender_mcp/server/tools/liquid/force_fields.py` (85 lines) + `guides.py` (59 lines)

| Tool | Lines | Purpose |
|------|-------|---------|
| `configure_liquid_force_fields` | 61 | Create or scope force fields (FORCE/WIND/VORTEX/TURBULENCE/DRAG) to a domain; set effector weights (gravity/force/wind/etc. per 0–200 range) | Validates distance_min ≤ distance_max |
| `create_liquid_guide` | 19 | Create effector-based guide OR link one liquid domain as another domain's guide source | Supports source="EFFECTOR" or "DOMAIN"; guide_alpha/beta/vel_factor parameters for Mantaflow guide coupling |

---

### F. Mesh, Materials, Quality Profiles

Located: `src/blender_mcp/server/tools/liquid/mesh_and_materials.py` (230 lines) + `quality.py` (139 lines)

| Tool | Line | Purpose |
|------|------|---------|
| `configure_liquid_mesh` | 127 | Patch mesh generation (use_mesh, mesh_scale, particle_radius, smoothing, concavity bounds, speed vectors, cache format) | Validates mesh_concave_lower ≤ mesh_concave_upper |
| `configure_liquid_secondary_particles` | 144 | Patch spray/foam/bubble/tracer generation; life ranges, potential thresholds, buoyancy/drag | Validates min ≤ max for all ranges; sndparticle_potential/update_radius are int cell counts (1–4) not float |
| `configure_liquid_diffusion` | 157 | Set viscosity + surface tension from presets (WATER/OIL/HONEY/MOLTEN/STYLIZED), direct values, or SI inputs (dynamic_viscosity_pa_s + density_kg_m3) | Validates one viscosity source only |
| `create_liquid_material` | 170 | Create Principled transparent liquid material (presets: WATER/GLASS/OIL/TINTED); assign to domain mesh | Supports APPEND or REPLACE_SLOT assignment |
| `create_secondary_particle_render_setup` | 198 | Configure baked Mantaflow particle systems for bounded object instancing (max 16 systems) | Optional sphere creation; display percentage 1–100 |
| `apply_liquid_quality_profile` | 109 (quality.py) | Convenience wrapper: apply PREVIEW/BALANCED/FINAL preset (solver + mesh patch pair) via existing configure_liquid_solver/configure_liquid_mesh | Non-mutating wrapper; profiles defined as static tuples of LiquidSolverPatch/LiquidMeshPatch |

---

### G. Lifecycle & Removal

Located: `src/blender_mcp/server/tools/liquid/lifecycle.py` (39 lines)

| Tool | Line | Purpose |
|------|------|---------|
| `remove_fluid_components` | 22 | Remove exact fluid modifiers and optionally MCP-owned helper objects after cache-orphan preflight | Rejects removal if it would orphan on-disk bake unless accept_orphaned_cache=True |

---

### H. Simulation Caching & Evaluation

Located: `src/blender_mcp/server/tools/liquid/simulation.py` (161 lines)

| Tool | Line | Purpose |
|------|------|---------|
| `sample_liquid_simulation` | 64 | Evaluate up to 32 cached/replay frames; return mesh vertices, particle counts, bounds | For REPLAY, steps from cache_frame_start in order (required for Blender correctness); for MODULAR/ALL, jumps directly (rejects out-of-range). Rejects preroll > max_preroll_frames (default 250). |
| `manage_liquid_cache` | 99 | Inspect, configure, bake, pause/resume, cancel, or free Mantaflow cache stages | STATUS/CONFIGURE/BAKE_*/START_BAKE/RESUME/CANCEL/PAUSE/FREE_* actions. START_BAKE is non-blocking (WM job) when GUI window available; falls back to synchronous under --background. |

---

### I. Result Validation & Shot Orchestration

Located: `src/blender_mcp/server/tools/liquid/result_validation.py` (66 lines) + `shot.py` (167 lines)

| Tool | Line | Purpose |
|------|------|---------|
| `validate_liquid_result` | 18 | Measure baked liquid against fill/spill/penetration targets; grid-sample evaluated mesh against CONTAINER_VOLUME/SPILL_VOLUME boxes (created by setup_liquid_shot). Per-frame: fill volume, fill fraction, spill volume, wall-penetration volume, escaped volume, mesh connectivity. Rejects frames > max_preroll_frames if REPLAY. | Target-driven validation (fill_fraction vs deadline_frame, overflow_policy). |
| `setup_liquid_shot` | 74 | **ORCHESTRATOR**: Build complete liquid shot in one call. Accepts list of containers (ShotContainer: object_name, collision_proxy, effector_type, rim_axis, wall_thickness, proxy_object_name) and sources (ShotSource: object_name, behavior, enabled_seconds, flow_settings). Internally: create_liquid_domain → add effectors/flows (proxy rig for HOLLOW_CONTAINER) → fit_liquid_domain → apply_liquid_quality_profile → validation_volumes → validate_liquid_setup. dry_run=True validates without mutating. | **HIGHEST ABSTRACTION**: Solves the "fill glass with water" request end-to-end via declarative container/source specs. Returns simulation_id for later validate_liquid_result. |

---

## 2. Runtime Blender 5.2.1 API Validation

**Command executed**: `/opt/homebrew/bin/blender --background --python-expr "[fluid modifier introspection]"` (Blender 5.2.1 LTS, build 2026-08-25)

**Properties verified**:
```json
{
  "domain_type": "LIQUID",
  "resolution_max": 32,
  "cache_type": "REPLAY",
  "use_mesh": true,
  "use_spray_particles": false,
  "use_foam_particles": false,
  "use_bubble_particles": false,
  "viscosity_value": 0.05000000074505806,
  "surface_tension": 0.0,
  "simulation_method": "FLIP",
  "flip_ratio": 0.9700000286102295
}
```

**Assessment**: ✅ **ALL verified properties match tool assumptions**. No mismatches, no deprecations detected. Blender 5.2.1 API surface is stable and tool expectations are correct.

---

## 3. Reliability Analysis

### A. Cache Management & Bake Failure Handling

**File**: `src/blender_mcp/bundled/addon/handlers/liquid/simulation.py` (766 lines)

**Bake Failure Modes**:
- Line 123: `except Exception: pass` in `_cache_state()` — swallows exceptions silently (potential hidden errors)
- Line 211, 245: `contextlib.suppress(OSError)` when walking cache directory or writing manifest — tolerates I/O errors gracefully
- Line 160: `_run_fluid_operator()` validates operator result is `{"FINISHED"}` or `{"RUNNING_MODAL"}`; raises `RuntimeError` if operator returns other states (e.g., `{"CANCELLED"}`)

**Disk Space Handling**:
- **NOT IMPLEMENTED**: No explicit disk-space preflight check in `manage_liquid_cache()` before BAKE_* actions
- `_cache_directory_evidence()` (line 198) walks existing cache to report file count and bytes scanned, but does **not** call `shutil.disk_usage()` or `os.statvfs()` to check available space on the target filesystem
- **Gap**: A multi-hour bake can fail silently if the target filesystem runs out of space mid-bake; no pre-flight alert

**Invalid Domain Bounds**:
- Line 1686 in `estimate_liquid_resources`: `if longest <= 0: raise ValueError("Domain has zero world-space extent")` — validates domain has non-zero size
- Line 657–659 in `configure_liquid_scope_and_boundaries`: Validates collection names are present or createable

**Verdict**: ✅ **Acceptable** error handling for operator failures and I/O exceptions. ⚠️ **Gap**: No disk-space preflight check; bake can fail with no early warning.

### B. Resource Estimation

**File**: `src/blender_mcp/bundled/addon/handlers/liquid/inspection_and_setup.py` (line 1680–1733)

**Function**: `estimate_liquid_resources()`

**What it returns**:
- `estimated_grid`: resolution_max, cell_size, cells_xyz, base_cell_count
- `relative_cost_index` = base_cells × (1.0 + particle_factor×0.35 + mesh_multiplier×0.2 + secondary_count×0.3) × frames
- **Recommendations**: preview_resolution_max, final_resolution_max, disclaimer note

**What it does NOT return**:
- Absolute memory in GB/MB (only relative index)
- Absolute disk space in GB (only relative index)
- Estimated bake time in seconds (only relative index)

**Docstring** (line 463–469 in server/tools/liquid/inspection_and_setup.py):
> "Estimate grid dimensions and conservative relative cache cost without changing the domain."

**Verdict**: ⚠️ **Limited utility**. The tool is honest ("relative cost"), but an agent cannot use it to predict "will this bake fit in 16GB RAM?" or "will this bake finish in 1 hour?". The docstring says "conservative" but the disclaimers acknowledge "occupancy, motion, compression, hardware, and solver behavior dominate actual memory, disk, and bake time." **This is accurate but leaves the agent without concrete pre-flight guidance.**

---

## 4. Critical Workflow Test: "Fill This Glass With Water"

### Scenario
- Scene contains a glass mesh (a container) and a pour source mesh (water emitter)
- Agent must: size domain, set up collider, set up flow, configure solver, bake, validate result

### Ideal Tool Sequence (Using Highest Abstraction)

**Option A: setup_liquid_shot (Recommended for Agent)**

```python
# Single declarative call
setup_liquid_shot(
  scene_name="Scene",
  cache_directory="/tmp/liquid_cache",
  containers=[
    ShotContainer(
      object_name="Glass_Mesh",
      collision_proxy="HOLLOW_CONTAINER",  # ← Agent must know: proxy or direct effector?
      effector_type="COLLISION",
      rim_axis="Z",  # ← Agent must know: which axis is the rim?
      wall_thickness=0.05,
    )
  ],
  sources=[
    ShotSource(
      object_name="Pour_Source",
      behavior="INFLOW",
      flow_settings=LiquidFlowPatch(use_inflow=True, surface_distance=1.5)
    )
  ],
  quality="BALANCED",
  cache_type="REPLAY",
  cache_frame_start=1,
  cache_frame_end=250,
  padding=(0.25, 0.25, 0.25),
  expected_travel=(0.0, 0.0, 0.5),  # ← Agent must estimate upward water travel
  splash_height=0.25,
  create_validation_volumes=True,
)
```

**Then**:
```python
manage_liquid_cache(
  domain_object_name="Liquid_Domain",
  modifier_name="Liquid Domain",
  action="START_BAKE",
  stage="BAKE_ALL",
  confirm_bake=True,
  max_bake_frames=250,
)
```

**Then poll / wait**:
```python
# Poll until bake completes
manage_liquid_cache(action="STATUS")  # → has_cache_baked_any=True
```

**Then validate**:
```python
validate_liquid_result(
  domain_object_name="Liquid_Domain",
  modifier_name="Liquid Domain",
  frames=[1, 50, 100, 150, 200, 250],
  target_fill_fraction=0.8,
  deadline_frame=250,
  overflow_policy="FORBID",
)
```

### Failure Points for a Non-Expert Agent

1. **Collision Proxy Choice**: HOLLOW_CONTAINER vs direct COLLISION effector. `setup_liquid_shot` accepts both, but agent must know: HOLLOW_CONTAINER removes the rim cap → emitter can pour in; direct COLLISION → glass stays solid. **Document required.**

2. **Rim Axis**: Agent must identify which axis points "up" for the glass. `rim_axis` in HOLLOW_CONTAINER mode removes the cap perpendicular to this axis. **Likely requires object inspection or agent heuristic** (e.g., try Z first).

3. **Padding & Splash Height**: `setup_liquid_shot` auto-fits the domain from sampled motion, but agent must supply `expected_travel` and `splash_height` as hints. Without them, domain may be too small if water bounces/splashes high. **Requires fluid-sim domain knowledge.**

4. **Cache Directory**: Must be explicit, unshared, and writable. `setup_liquid_shot` does NOT create the directory (addon rejects non-existent paths). **Agent must ensure it exists or call fails.**

5. **Bake Completion Polling**: `START_BAKE` returns a job_id under GUI; agent must poll `manage_liquid_cache(action="STATUS")` until stage's `has_cache_baked_*` flag is true. **Under `--background` (no GUI), START_BAKE falls back to synchronous, blocking for hours; agent must know this behavior.**

6. **Frame Range Validity**: Sample/validate frames must fall within cache_frame_start:cache_frame_end. Agent must track this. **No auto-bounds detection.**

### Achievability

**Verdict**: ✅ **Achievable in 4 sequential tool calls** (`setup_liquid_shot` + `manage_liquid_cache START_BAKE` + `manage_liquid_cache STATUS` poll-loop + `validate_liquid_result`), **BUT requires domain expertise for**:
- Choosing HOLLOW_CONTAINER vs COLLISION
- Guessing rim_axis, padding, splash_height
- Ensuring cache directory exists
- Handling async vs synchronous bake under GUI vs --background
- Polling until completion

**Agent would benefit from**:
- A higher-level convenience tool wrapping the polling loop
- Clearer documentation on proxy vs collision choice
- Auto-detection of axis orientation from mesh bounds

---

## 5. Redundancy & Over-Fragmentation Audit

### Finding: Parallel API Duplication

**fluid.py** (6 tools): Generic LIQUID/GAS surface via `domain_type` parameter
- `inspect_fluid_simulation(domain_type, ...)`
- `create_fluid_domain(domain_type, ...)`
- `configure_fluid_solver(domain_type, FluidSolverPatch)`
- `add_fluid_flow(domain_type, ...)`
- `add_fluid_effector(domain_type, ...)`
- `manage_fluid_cache(domain_type, ...)`

**liquid/inspection_and_setup.py** (12 tools): Liquid-specific
- `get_liquid_simulation_info(...)` — OVERLAPS fluid:inspect_fluid_simulation; adds UUID/dependency discovery
- `create_liquid_domain(...)` — OVERLAPS fluid:create_fluid_domain; adds liquid solver defaults
- `configure_liquid_solver(...)` — OVERLAPS fluid:configure_fluid_solver; liquid-specific fields (27 vs shared 13)
- `add_liquid_flow(...)` — OVERLAPS fluid:add_fluid_flow; rejects smoke-only fields
- `add_liquid_effector(...)` — OVERLAPS fluid:add_liquid_effector
- `configure_liquid_flow(...)` — NEW
- `configure_liquid_effector(...)` — NEW
- Other new tools: fit, boundaries, estimate, validate

**Observation**: All fluid.py tools use `liquid/_shared.py:_call()` transport. **There is no separate `gas/` module or gas-specific handlers found.** The gas simulation surface appears to be purely theoretical or unused.

### Consolidation Verdict

| Candidate | KEEP/MERGE/REMOVE | Rationale |
|-----------|-------------------|-----------|
| `fluid.py:inspect_fluid_simulation` vs `liquid:get_liquid_simulation_info` | **MERGE** | Get the liquid-specific version (adds UUID/dependency); `fluid.py` is now redundant for LIQUID. If GAS is unused, remove `fluid.py` entirely. If GAS is active elsewhere (smoke/fire tools), consolidate to `gas.py` and remove generic `fluid.py`. |
| `fluid.py:create_fluid_domain` vs `liquid:create_liquid_domain` | **MERGE** | Liquid version adds solver defaults; keep it. Remove from `fluid.py` unless GAS variant exists. |
| `fluid.py:configure_fluid_solver` vs `liquid:configure_liquid_solver` | **MERGE** | Liquid version is strictly more capable (27 fields); deprecate `fluid.py` variant. Remove from `fluid.py` unless GAS has separate settings. |
| `fluid.py:add_fluid_flow` vs `liquid:add_liquid_flow` | **MERGE** | Liquid version validates smoke-only fields explicitly; keep. Remove generic variant. |
| `fluid.py:add_fluid_effector` | **MERGE** | Liquid version suffices (shared model). Remove generic. |
| `fluid.py:manage_fluid_cache` | **MERGE or KEEP** | If bake state is domain-type-agnostic (likely), consolidate to single tool accepting optional domain_type or remove it and only expose `liquid:manage_liquid_cache`. If GAS uses different cache semantics, split into `gas:manage_gas_cache`. **Current state: one tool per domain, both route to same addon handler.** |
| **Overall recommendation** | **REMOVE `fluid.py` OR split into `gas.py`** | **Gap**: No gas-specific tools or handlers found. If gas simulation is in scope, create `src/blender_mcp/server/tools/gas/` module with GAS-only tools (configure_gas_solver, etc.) and delete `fluid.py`. If gas is out of scope, delete `fluid.py` entirely and rename `liquid/` to `simulation/` for clarity. **Current state is confusing: a "canonical cross-domain" module that only routes to liquid handlers.** |

### Module Fragmentation

**Liquid submodules**: 10 files (+ `_shared.py`, `__init__.py`)
- `inspection_and_setup.py` (493): Core domain/flow/effector setup + validation
- `simulation.py` (161): Cache lifecycle
- `mesh_and_materials.py` (230): Mesh, particles, diffusion, materials
- `delivery.py` (282): Proxies, export, performance
- `shot.py` (167): Orchestrator
- `animation.py` (58): Flow keying
- `result_validation.py` (66): Baked output validation
- `force_fields.py` (85): Force field scoping
- `guides.py` (59): Guide creation
- `lifecycle.py` (39): Component removal
- `quality.py` (139): Quality profile convenience

**Assessment**: ✅ **Reasonable fragmentation**. Each module is task-scoped (mesh generation, animation, delivery, etc.) and under 300 lines except `delivery.py` (282). The split enables clarity and testability. **Not over-fragmented** for the domain's scope.

### Verdict Summary

**Single biggest redundancy**: `fluid.py`'s 6 generalized domain_type tools duplicate `liquid/` variants with no evidence of active GAS support. **Action**: Investigate whether gas simulation is in scope; if not, delete `fluid.py` and consolidate all tools into `liquid/` (rename to `simulation/` or keep as `liquid/` for backward compatibility). If gas IS active elsewhere, move `fluid.py` logic into `src/blender_mcp/server/tools/gas/` and `src/blender_mcp/bundled/addon/handlers/gas/`, then delete cross-domain `fluid.py`.

---

## 6. Summary

| Dimension | Finding |
|-----------|---------|
| **Tool Count** | ~41 tools across `fluid.py` (6) + `liquid/*` (35); 6,960 lines in addon handlers |
| **Abstraction Ladder** | Low (inspect/configure individual properties) → Medium (patch models, validation) → High (`setup_liquid_shot` orchestrator) |
| **Critical Workflow** | "Fill glass with water" achievable via `setup_liquid_shot` + bake + validate in 4 calls; requires domain-expertise for proxy choice, axis orientation, padding heuristics |
| **API Stability** | ✅ Blender 5.2.1 runtime introspection confirms all tool assumptions (use_spray_particles, simulation_method, flip_ratio, etc.) |
| **Reliability** | ✅ Error handling for operator failures & I/O exceptions; ⚠️ No disk-space preflight check before long bakes |
| **Resource Estimation** | ⚠️ `estimate_liquid_resources` returns only relative cost index, not absolute memory/disk/time predictions |
| **Redundancy** | 🔴 **CRITICAL**: `fluid.py` (6 cross-domain tools) duplicates `liquid/` variants with no active GAS module found; consolidation recommended |
| **Over-Fragmentation** | ✅ 10 `liquid/` submodules well-scoped; not excessive |
| **Production Readiness** | ✅ For liquid-only workflows. ⚠️ Gas support is ambiguous; requires clarification and possible refactoring. |

---

## Appendix: Tool Coverage by Workflow Phase

**Pre-flight**: `validate_liquid_setup`, `estimate_liquid_resources`, `get_liquid_simulation_info`
**Domain Setup**: `setup_liquid_shot` (orchestrator) or manual: `create_liquid_domain` → `fit_liquid_domain` → `add_liquid_flow` → `add_liquid_effector` → `configure_liquid_solver` → `apply_liquid_quality_profile`
**Animation**: `animate_liquid_flow` (time-key flow settings)
**Delivery**: `create_liquid_proxy_rig`, `configure_liquid_force_fields`, `create_liquid_guide`
**Baking**: `manage_liquid_cache` (START_BAKE, STATUS, PAUSE, RESUME, CANCEL)
**Post-Bake**: `sample_liquid_simulation` (evaluate frames), `validate_liquid_result` (measure fill/spill), `prepare_liquid_render_mesh` (finishing), `export_liquid_simulation` (write to file)
**Cleanup**: `remove_fluid_components`, `duplicate_liquid_setup_variant`

---

**Audit completed**: 2026-09-05
**Blender version tested**: 5.2.1 LTS (build 2026-08-25)
**Recommendation**: Consolidate or remove `fluid.py`; clarify GAS simulation scope.

---

## Verification pass (2026-09-21)

Every claim below was re-checked against the working tree at commit `8185248`. The slice above was
written 2026-08-29/09-05; the module layout has since changed, so its `file:line` references are
largely stale even where its conclusions hold. Method: static re-read of the current sources plus a
live registry count through `.venv/bin/python` (importing each bundle's modules and diffing
`mcp._tool_manager._tools`). No source file was modified by this pass.

### Headline claims

**1. `setup_liquid_shot` is the domain's high-level orchestrator — VERIFIED.**

- Server tool: `src/blender_mcp/server/tools/liquid/shot.py:73-148`; declarative `ShotContainer` /
  `ShotSource` models at `:29-70`, bounded at `_MAX_SHOT_OBJECTS = 16` (`:26`).
- Handler entry `src/blender_mcp/bundled/addon/handlers/liquid/shot.py:335-403`; the real work is
  `_execute_shot` at `:451-521`, which composes in this order: `create_liquid_domain` (`:454`),
  containers/colliders and proxy rigs (`:468`), timed sources (`:469`), `fit_liquid_domain`
  (`:470`), `apply_liquid_quality_profile` (`:480`), validation volumes (`:487`),
  `validate_liquid_setup` (`:490`). Exactly the chain the slice described at line 129.
- `dry_run` still resolves a plan without mutating: `shot.py:401-402`, `_dry_run_report` `:419-449`.
- Stronger than audited: role-conflict rejection before any mutation (`:406-417`), a shot identity
  property stamped on every touched object (`SHOT_ID_PROPERTY`, `:25` and `:467`), and an explicit
  `next_actions` handoff naming the bake call and the `simulation_id` to pass to
  `validate_liquid_result` (`:515-519`). The tool still never bakes (`:364-365`).
- **New since the audit**: the whole orchestration runs inside one transaction.
  `setup_liquid_shot` is registered as a mutating command (`server_core.py:1554`), mutating dispatch
  is wrapped in `mutation_transaction` (`server_core.py:1946-1967`), and that context manager
  snapshots/diffs/removes datablocks created by a failed command and pushes one undo checkpoint
  (`transaction.py:264-482`, tracked collections `:22-45`). The handler's own docstring relies on it
  (`handlers/liquid/shot.py:452`). This retires the global "No atomic transaction or rollback
  contract" finding (`AGENTS.md:176`) *for this domain*.

**2. "A redundant `fluid.py` duplicating `liquid/` with no active GAS module" — STALE in both halves.**

- *(a) The module is gone.* No `fluid.py` exists anywhere under `src/`. Commit
  `e422311 refactor: consolidate fluid.py into liquid module` folded the six cross-domain tools into
  the liquid package: `inspect_fluid_simulation`, `create_fluid_domain`, `add_fluid_flow`,
  `add_fluid_effector` at `src/blender_mcp/server/tools/liquid/inspection_and_setup.py:545, 568,
  598, 628`, and `configure_fluid_solver`, `manage_fluid_cache` at
  `src/blender_mcp/server/tools/liquid/simulation.py:187, 209`. One package, one bundle
  (`src/blender_mcp/server/bundles.py:40`). The slice's §5 recommendation ("REMOVE `fluid.py` OR
  split into `gas.py`", line 339) and the closing recommendation (line 394) are both obsolete.
- *(b) GAS is not "purely theoretical or unused".* There are dedicated GAS code paths, not just a
  pass-through discriminator:
  - GAS domain construction and conversion, with its own warning:
    `handlers/liquid/inspection_and_setup.py:2213-2226`.
  - A GAS solver allowlist `_GAS_DOMAIN_FIELDS` of 12 real smoke/fire properties (`vorticity`,
    `burning_rate`, `flame_smoke`, `flame_vorticity`, `use_noise`, `noise_scale`, plus the shared
    timestep set) at `:50-63`, enforced at `:2236-2239`, which rejects liquid-only properties by name.
  - GAS flow fields `_GAS_FLOW_FIELDS` = shared flow set plus `density`, `fuel_amount`,
    `smoke_color`, `temperature` (`:99`), with `flow_type` SMOKE/FIRE/BOTH applied at `:2293-2298`.
  - A GAS effector path at `:2337-2356`.
  - A domain-type-aware cache lifecycle: `BAKE_NOISE`/`FREE_NOISE` and stage `NOISE` are GAS-only,
    `GUIDES`/`MESH`/`PARTICLES` are LIQUID-only (`handlers/liquid/simulation.py:483-492`).
  - Live coverage: `tests/blender_liquid_phase0_smoke.py:148-177` creates a GAS domain, adds a gas
    flow, patches the gas solver, and inspects gas cache state under real headless Blender.
- *What survives of the finding:* **tool-surface** duplication, not module duplication. Both halves
  of each pair are registered in the same bundle, so an agent is offered `create_fluid_domain` and
  `create_liquid_domain`, `configure_fluid_solver` and `configure_liquid_solver`,
  `manage_fluid_cache` and `manage_liquid_cache`, `add_fluid_flow`/`add_liquid_flow`,
  `add_fluid_effector`/`add_liquid_effector`, `inspect_fluid_simulation`/`get_liquid_simulation_info`
  — 12 registered tools covering 6 capabilities, inside a 37-tool bundle.
- *New, statically verified GAS gaps* (these replace "no GAS module" as the accurate criticism):
  - GAS coverage is **partial**. Nothing in `src/blender_mcp` mentions `use_dissolve_smoke`,
    `dissolve_speed`, buoyancy `alpha`/`beta`, `use_adaptive_domain`/`adapt_margin`,
    `noise_strength`/`noise_pos_scale`/`noise_time_anim`, or `flame_max_temp`; a repo-wide search for
    `dissolve|buoyanc|adapt_margin|use_adaptive_domain|noise_strength|flame_max_temp` matches only
    `sndparticle_bubble_buoyancy` (`handlers/liquid/mesh_and_materials.py:61`) and an unrelated
    retopology `DISSOLVE` action. Smoke dissolve, buoyancy and adaptive-domain are unreachable.
  - Facade parity gap: `manage_fluid_cache` exposes no `max_existing_cache_bytes`
    (`server/tools/liquid/simulation.py:209-239`) while its liquid twin does (`:121-183`), so a GAS
    caller silently inherits the handler default `10_000_000_000` (`handlers/liquid/simulation.py:455`).
  - `FluidSolverPatch` (`server/tools/liquid/inspection_and_setup.py:126-142`) carries the
    liquid-only `simulation_method` and `flip_ratio`, which the GAS handler rejects at
    `handlers/liquid/inspection_and_setup.py:2236-2238`. The schema accepts a combination the domain
    refuses. It fails cleanly and before mutation (`_reject_baked` at `:2233`, patch applied only at
    `:2239`), so this is a usability wart, not a correctness bug.

**3. No disk-space preflight before multi-hour bakes — VERIFIED, and repo-wide.**

- No `shutil.disk_usage` and no `os.statvfs` call exists anywhere under `src/` or `tests/`.
- The bake gate is `handlers/liquid/simulation.py:612-642` and checks: `confirm_bake` (`:614`),
  `max_bake_frames` vs the cache range (`:616-617`), REPLAY rejection (`:618-619`), cache-type and
  stage prerequisites (`:620-629`), cache directory exists **and is writable** (`:630-632`), the
  existing-cache byte bound (`:633-634`), and manifest ownership vs
  `confirm_external_overwrite` (`:635-642`). Nothing measures **free** space.
- `_cache_directory_evidence` (`:199-224`) reports `bytes_scanned` for files already present and a
  `writable` flag; it has no `free`/`available` field, so the reply carries no capacity evidence
  either.
- `validate_liquid_setup` raises `MISSING_CACHE_PATH`, `INVALID_CACHE_PARENT` and
  `CACHE_NOT_WRITABLE` (`handlers/liquid/inspection_and_setup.py:1833-1855`) but has no capacity
  finding, so the domain's own preflight tool cannot answer it.
- Same gap in the sibling simulation domains: rigid body's `_external_cache_evidence`
  (`handlers/rigid_body/simulation.py:78-100`) likewise reports only `bytes_scanned`.
- Correctly disclosed adjacent risk: a bake cannot be rolled back, and the reply says so
  (`handlers/liquid/simulation.py:710-714`), as does the synchronous-under-`--background` warning
  (`:735-740`).

**4. `estimate_liquid_resources` returns only a relative cost index — VERIFIED.**

- `handlers/liquid/inspection_and_setup.py:1693-1746` returns `estimated_grid` (`:1720-1726`),
  `frame_count` (`:1728`), particle/mesh/cache descriptors, and `relative_cost_index` (`:1737`).
  No bytes, no seconds, no memory figure. The zero-extent guard the slice cited at "line 1686" is
  now `:1698-1699`.
- The disclaimer is intact and honest: "occupancy, motion, compression, hardware, and solver
  behavior dominate actual memory, disk, and bake time" (`:1741-1744`); tool docstring at
  `server/tools/liquid/inspection_and_setup.py:512`.
- **Context that lowers the severity**: this is a deliberate, domain-wide stance, not a liquid
  oversight. Cloth returns the same shape — log-scaled 0-100 `cpu`/`memory`/`cache` indices
  (`handlers/cloth/diagnostics.py:173-180`) under the explicit disclaimer "Relative deterministic
  indices, not byte, memory, or bake-duration promises." (`:229`).
- **Context that raises it**: combined with claim 3, *nothing* in the product can answer "will this
  bake fit on this disk / in this RAM / in this hour". The two findings are one gap, not two.

### Secondary claims re-checked

| Slice claim | Result | Current evidence |
|---|---|---|
| "Line 123: `except Exception: pass` in `_cache_state()` swallows exceptions" (line 165) | **STALE** | `_cache_state` has no `try`/`except` at all (`handlers/liquid/simulation.py:99-105`). Line 123 is now inside `_update_or_restore`, which restores the RNA values and re-raises (`:119-125`). |
| "`_run_fluid_operator()` validates `FINISHED`/`RUNNING_MODAL`" (line 167) | **VERIFIED**, moved | `handlers/liquid/simulation.py:139-161`; the acceptance set is at `:158-160` and `RUNNING_MODAL` is now opt-in per call rather than always accepted (`:139`, `:189-195`). |
| "`contextlib.suppress(OSError)` when walking the cache dir or writing the manifest" (line 166) | **VERIFIED**, moved, and now a real asymmetry | Directory walk still suppresses `OSError` around `getsize` (`:212-213`), and the async-bake manifest reconcile still writes under `contextlib.suppress(OSError)` with no reply channel to report it (`:247-254`). The synchronous bake path, by contrast, surfaces the same failure as a warning (`:741-750`). So an async (GUI) bake can silently end up with an unwritten ownership manifest, which is what `confirm_external_overwrite` later keys off (`:636-642`); a sync bake reports it. Worth unifying. |
| "Forwards to addon's `handle_manage_liquid_cache`" (line 24) | **STALE** naming | The handler is `manage_liquid_cache` (`handlers/liquid/simulation.py:443`) with a thin `manage_fluid_cache` wrapper (`:768-770`). No `handle_`-prefixed handler exists. |
| Tool inventory (names, §1 tables A-I) | **VERIFIED complete** | All 37 tools the live registry reports for the `liquid` bundle appear by name in the slice's tables, and the slice names no tool that is absent from the registry. No liquid tool was added or removed since the audit. |
| Every `file:line` in the §1 tables | **STALE** | The module moved and the files grew: `get_liquid_simulation_info` 124 → 172, `create_liquid_domain` 174 → 222, `fit_liquid_domain` 235 → 283, `configure_liquid_solver` 287 → 335, `estimate_liquid_resources` 463 → 511, `validate_liquid_setup` 473 → 521; the six `fluid.py` rows (95/118/148/170/200/228) are now `liquid/inspection_and_setup.py:545/568/598/628` and `liquid/simulation.py:187/209`. |
| Line counts in §1 headers and §6 | **STALE** | `server/tools/liquid/inspection_and_setup.py` 493 → 652, `simulation.py` 161 → 241, `quality.py` 139 → 141, `animation.py` 58 → 57, `force_fields.py` 85 → 84, `guides.py` 59 → 58. Addon liquid handlers 6,960 → 6,984 lines. |
| "~41 tools across `fluid.py` (6) + `liquid/*` (35)" (line 368) | **WRONG THEN AND NOW** | The slice's own §1 tables enumerate 31 liquid-specific tools + 6 cross-domain = **37**, which is exactly what the registry reports. The 35/41 summary figure never matched the slice's own inventory. |
| §2 "Runtime Blender 5.2.1 API validation: all verified properties match" (lines 133-154) | **UNVERIFIABLE this pass** | Not re-run: the live Blender is in use for the render-overhead measurement, and a competing `--background` Blender would contend for CPU and skew those timings. The standing mechanism is `just smoke` (`justfile:97-111`, globbing `tests/blender_*_smoke.py` at `:108`), which includes three liquid scripts. Nothing in the static re-read contradicts the recorded result. |
| §5 "Reasonable fragmentation; not over-fragmented" (line 356) | **VERIFIED** | 13 files in `server/tools/liquid/` (11 tool modules + `_shared.py` + `__init__.py`), each still task-scoped; largest is `inspection_and_setup.py` at 652 lines, next `delivery.py` at 282. |

### Freshly counted tool total

Counted from the live FastMCP registry, not by reading tables: import each bundle's modules and diff
`mcp._tool_manager._tools`.

| Bundle | `bundles.py` modules | Tools |
|---|---|---|
| `liquid` | `liquid` (`bundles.py:40`) | **37** (31 liquid-specific + 6 canonical LIQUID/GAS facade) |
| `cloth` | `cloth` (`bundles.py:39`) | **19** |
| `rigid-body` | `rigid_body`, `scene_physics` (`bundles.py:41`) | **31** (29 in `tools/rigid_body/` + `configure_scene_physics`, `get_scene_physics_info`) |
| **Simulation total** | | **87**, or **85** excluding the two `scene_physics` tools |
| Whole registry (`all`) | | **298**, matching the count quoted in `bundles.py:6` |

**What I counted and why.** I counted rigid body and cloth in addition to liquid. No other slice owns
them: `01_scene_core.md:201-202` audits only `scene_physics` (unit scale and gravity), and the rubric
itself defers them — "Rigid body/cloth/particles: (awaiting Scene/Core audit)"
(`COMPREHENSIVE_AUDIT_FINDINGS.md:111`) — while placing them in category E, which this slice feeds.
The two `scene_physics` tools are reported separately above so category E and the scene/core slice
cannot double-count them. The audit-era global figure of 45 registered tools (`AGENTS.md:137`) is
long obsolete at 298.

Source volume, recounted: liquid 6,984 addon-handler lines + 2,091 tool lines; cloth 6,325 + 1,465;
rigid body 5,267 + 1,797.

### New finding: cloth has no live-Blender coverage

`just smoke` runs every `tests/blender_*_smoke.py` against real headless Blender (`justfile:97-111`,
glob at `:108`) and is the only thing that exercises the real API — none of `just lint`,
`just fmt-check`, `just typecheck` or `just test` touches Blender (`AGENTS.md:106-112`). Of the 24
scripts present, liquid has three (`tests/blender_liquid_phase0_smoke.py`,
`blender_liquid_workflows_smoke.py`, `blender_liquid_multiframe_smoke.py`) and rigid body has three
plus `blender_scene_physics_smoke.py`. **There is no cloth smoke script.** Cloth's 19 tools and
6,325 handler lines are verified only against the fake `bpy` in
`tests/server/tools/cloth/test_tools.py`. (Unrelated doc drift noticed in passing, not edited here:
`AGENTS.md:109` says "all 23" smoke scripts; there are 24.)

### Scoped test evidence

```
.venv/bin/python -m pytest tests/server/tools/liquid/test_tools.py \
  tests/server/tools/liquid/test_workflows.py tests/test_liquid_shot.py \
  tests/test_liquid_result_validation.py tests/test_liquid_quality_and_identity.py \
  tests/test_fluid_tools.py -q
→ 185 passed in 3.64s
```

(An earlier run of the same command reported 85 failures, all
`NameError: name '_DEFAULT_REACH_TOLERANCE_M' is not defined` from a concurrent edit in
`handlers/character_rigging/posing.py` — unrelated to this domain, and clean once that landed at
`posing.py:777`. Recorded so the number above is not mistaken for a domain regression.)

---

## Score estimate

**Score estimate**: 8/10 (strongest simulation domain: real orchestration, transactional rollback,
identity/manifest discipline; no capacity preflight anywhere, duplicated canonical/liquid tool pairs,
partial GAS surface)

**What earns the 8.** The verified findings, not the audit's prose:

- One declarative call builds a whole shot and then validates it, with a non-mutating `dry_run` and
  an explicit handoff to bake and to `validate_liquid_result`
  (`handlers/liquid/shot.py:451-521`, `:419-449`, `:515-519`).
- The orchestration is atomic: created datablocks are removed and one undo checkpoint is pushed when
  a mutating command fails (`transaction.py:264-482`, `server_core.py:1946-1967`).
- Baking is genuinely gated, not nominally: confirmation, frame ceiling, cache-type and stage
  prerequisites, directory existence/writability, an existing-cache byte bound, and
  manifest-ownership vs explicit overwrite consent (`handlers/liquid/simulation.py:612-642`), with a
  UUID + on-disk manifest so a setup survives renames and reloads
  (`handlers/liquid/inspection_and_setup.py:80-95`).
- Failures are real failures: operator results are checked (`handlers/liquid/simulation.py:158-160`),
  patches are restored on failure (`:119-125`, `:565-569`), and the unpreventable ones are disclosed
  in `warnings` rather than hidden — non-rollbackable frees, synchronous bakes under `--background`,
  unscriptable bake cancellation (`:710-714`, `:735-740`, `:585-592`).
- Preflight and result validation both exist and neither mutates
  (`handlers/liquid/inspection_and_setup.py:1748-2169`; `server/tools/liquid/result_validation.py:18`).
- GAS is real, with its own field allowlists and stage rules, and it is exercised against live
  Blender (`handlers/liquid/inspection_and_setup.py:50-63, 99, 2213-2356`;
  `handlers/liquid/simulation.py:483-492`; `tests/blender_liquid_phase0_smoke.py:148-177`).

**What holds it to 8, and what moved it there.**

- Moved it **down** from 9: no capacity preflight exists anywhere in the product
  (claim 3 — verified, repo-wide) and estimation is deliberately relative-only (claim 4 — verified).
  For a tool whose flagship operation is a multi-hour write to disk, "will this fit" is
  unanswerable, and the domain's own preflight tool has no capacity finding
  (`handlers/liquid/inspection_and_setup.py:1833-1855`). One free-space read in
  `_cache_directory_evidence` plus a `WARNING` finding in `validate_liquid_setup` would close it.
- Also down: 12 registered tools cover 6 capabilities, so the agent-visible surface of a 37-tool
  bundle is inflated by twins whose only difference is a discriminator (claim 2, residual).
- Also down: the GAS half of the "canonical" surface cannot reach smoke dissolve, buoyancy, or
  adaptive domain, and its cache facade is narrower than the liquid one
  (`server/tools/liquid/simulation.py:209-239`).
- Minor, but real: an async (GUI) bake's ownership manifest write is suppressed silently
  (`handlers/liquid/simulation.py:247-254`) while the synchronous path warns (`:741-750`), and the
  manifest is what the next bake's overwrite consent keys off (`:636-642`).
- Moved it **up** from the 7 the original slice's tone implied: the two loudest findings behind that
  tone — the redundant `fluid.py` and the phantom GAS surface — are both stale, and the transactional
  rollback the audit listed as globally Pending is now in force for every mutating call in this
  domain.

### Mapping onto the 100-point rubric (`COMPREHENSIVE_AUDIT_FINDINGS.md:71-152`)

| Cat | Weight | Prelim | Direction from this slice | Evidence |
|---|---|---|---|---|
| **A** Architecture & Abstraction | 15 | 11 | **↑ 12/15** | Orchestrator composes single-purpose handlers instead of reimplementing them (`handlers/liquid/shot.py:362-365, 451-490`); per-domain bundles keep a 298-tool catalog out of one client context (`bundles.py:1-16, 39-41`); one transaction boundary at dispatch rather than per-handler ad hoc cleanup (`server_core.py:1946-1967`). Against: canonical/liquid twins. |
| **B** Tool Quality & Redundancy | 15 | 12 | **hold 12/15, evidence line must be rewritten** | `COMPREHENSIVE_AUDIT_FINDINGS.md:86` cites "fluid.py duplicates" — the module no longer exists (claim 2a). The redundancy is real but is *tool-pair* duplication inside one bundle, and it is bounded at 6 pairs. Strict input models remain (`_StrictModel` extra=forbid, `server/tools/liquid/_shared.py:15-24`). |
| **C** Scene & Asset Pipeline | 10 | 7 | **hold 7/10** | Simulation adds no deficit here and closes its own delivery path: bounded Alembic/USD export, render-mesh finishing, setup variants (`server/tools/liquid/delivery.py:208, 170, 132`), plus `export_cloth_simulation`, `export_rigid_body_animation`, `bake_rigid_bodies_to_keyframes`. C's stated gaps (material presets, video output) belong to other domains. |
| **D** Lighting & Camera | 10 | 10 | unchanged (out of scope) | — |
| **E** Animation/Rigging/Simulation | 10 | 7 | **↑ 8/10** | The placeholder "Rigid body/cloth/particles: (awaiting Scene/Core audit)" (`:111`) resolves to a large, real surface: **87 simulation tools** counted from the live registry across the `liquid`/`cloth`/`rigid-body` bundles (`bundles.py:39-41`) — liquid 37, rigid body 31, cloth 19 — each domain end-to-end setup → bake → sample → validate → export, with proxy rigs, force fields, variants and cache lifecycles. Held off 9 by cloth having no live-Blender script (`justfile:108`) and by the capacity-preflight gap shared across all three. |
| **F** Rendering | 15 | 9 | unchanged (out of scope) | — |
| **G** Compositing | 5 | 1 | unchanged (out of scope) | — |
| **H** Validation & Reliability | 10 | 7 | **↑ 8/10** | Up: datablock rollback + undo checkpoint now exist (`transaction.py:264-482`), operator results are validated (`handlers/liquid/simulation.py:158-160`), and all three simulation preflights are classified non-mutating by the dispatcher itself — `validate_rigid_body_setup`, `validate_cloth_setup`, `validate_liquid_setup` in `_READ_ONLY_COMMANDS` (`server_core.py:1633, 1646, 1651`); the slice's `except Exception: pass` claim is stale. Down-pressure retained: the "disk-space preflight missing" item at `:133` is **confirmed and is repo-wide**, and cloth is unverified against real Blender. The RNA-enum item pinning this category belongs to materials, not this slice. |
| **I** Agentability & NL | 5 | 3 | **hold 3/5** | Up: `setup_liquid_shot` + `next_actions` + `simulation_id` handoff; bundles/modes make the surface selectable. Down, and it cancels out: the canonical/liquid twin pairs are exactly the "which of these do I call?" confusion this category penalizes, the pour shot still needs `rim_axis`/`padding`/`splash_height` domain judgement (`server/tools/liquid/shot.py:73-95`), and bake completion is still a manual STATUS poll. |
| **J** Production Completeness | 5 | 2 | **hold 2/5, no longer blocked from here** | Liquid, rigid body and cloth are each complete end-to-end; the residue is partial GAS (no dissolve/buoyancy/adaptive domain) and absent capacity prediction. J's stated blockers (video output, compositor, pose library) are other domains'. |

Net effect on the preliminary total of 69/100: **+3 → 72/100** (A +1, E +1, H +1), with `:86`'s
"fluid.py duplicates" evidence line and `:111`'s "awaiting Scene/Core audit" placeholder both needing
replacement by the verified statements above.

---

**Verification pass completed**: 2026-09-21 against commit `8185248`
**Method**: static re-read of the current tree + live FastMCP registry count; no live Blender
(reserved for the concurrent render-overhead measurement), no source file modified
