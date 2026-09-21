# Blender MCP — Production Audit Synthesis

**Status**: Complete. Nine domain slices, all scored; the 100-point breakdown below is
derived from them and is the only aggregate anyone may quote.

**Synthesis date**: 2026-09-21. The five original slices were written 2026-08-29 and the
tree has moved a long way since, so each pre-existing slice carries a dated
`## Verification pass` section recording, claim by claim, what is still true. Several
headline findings did not survive that pass; they are listed under *Retired claims* below.
A score resting on an unverified 2026-08-29 claim would be fabrication, so none does.

## Domain slices

| Slice | File | Score | Verified |
|---|---|---|---|
| Scene / core / modeling / import | `01_scene_core.md` | **8/10** | 43 tools |
| Camera / lighting | `02_camera_lighting.md` | **7/10** | 38 tools (32 in default bundles) |
| Rendering / colour / compositing | `03_rendering_compositing.md` | **5/10** | 9 tools |
| Materials / shading / UV / texturing | `04_materials_texture_uv.md` | **7/10** | 21 tools |
| Animation / rigging | `05_animation_rigging.md` | **7/10** | 29 tools |
| Retopology | `06_retopology.md` | **7/10** | 27 tools |
| Geometry nodes | `07_geometry_nodes.md` | **6/10** | 28 tools |
| Validation / reliability | `08_validation.md` | **6/10** | 11 validators, 10 inspectors |
| Liquid / fluid / simulation | `09_liquid_fluid_simulation.md` | **8/10** | 12 tools (6 capabilities) |

The registered surface is **298 tools** (`src/blender_mcp/server/bundles.py:6`, asserted by
`tests/server/test_bundles.py`). Per-slice counts overlap where a tool serves two domains
and are each stated with the counting method used, so they do not sum to 298.

## 100-point rubric

Rubric categories and weights are unchanged from
`COMPREHENSIVE_AUDIT_FINDINGS.md:71-152`; the scores below supersede the preliminary ones
there, which were taken with three domains unaudited.

| Category | Weight | Preliminary | **Final** | What moved it |
|---|---|---|---|---|
| A. Architecture & Abstraction | 15 | 11 | **12** | Up: one dispatcher-owned mutation transaction with identity-based rollback (`bundled/addon/transaction.py`, 35 tests), typed discriminated-union modifier/constraint specs (`server/tools/scene.py:111-303`), selectable bundles. Down: ten `nd_*` passthrough tools and per-domain builders that re-implement shared geometry. |
| B. Tool Quality & Redundancy | 15 | 12 | **12** | Up: no free-form Blender execution exists anywhere (`execute_blender_code` is gone from `src/`, verified by search) — the audit's own Critical row is retired. Down: duplication grew with the surface (12 liquid tools over 6 capabilities; `manage_modifiers` APPLY vs `nd_apply_modifiers`; `mesh_bridge` registered into the wrong bundle while its test asserts otherwise), and ≥8 retopology parameters accept at the schema what the handler then rejects. |
| C. Scene & Asset Pipeline | 10 | 7 | **6** | Down: there is no local file import at all. Importers exist only inside the two provider handlers, `.blend` is covered by `file_lifecycle`, export exists with no matching import, and neither provider import accepts a target collection. With no arbitrary-code escape hatch left, an agent cannot bring a user's own OBJ/FBX/glTF into a shot. |
| D. Lighting & Camera | 10 | 10 | **7** | Down: the preliminary 10/10 rested on this slice before it was scored. Of its four supporting bullets, 2.5 survive. `create_studio_lighting` regressed into validate-after-mutate (preview arguments are checked at `server/tools/lighting/rendering.py:142-166,197-198`, i.e. after three lights exist, and the handler then refuses the retry); `create_camera` links object and data before `_validate_optics` can raise; `panorama_type` is the domain's one un-enumerated enum; the `dust_density` → `aerosol_density` rename has zero test coverage. |
| E. Animation / Rigging / Simulation | 10 | 7 | **8** | Up: the liquid domain scores 8/10 on real orchestration with dry-run, transactional rollback, gated baking and live smoke coverage; `solve_bone_reach` now states a convergence contract (`converged`, `chain_reach_m`, `target_distance_m`) instead of returning a bare error float. Still down: keyframe interpolation exposes 3 of Blender's 13 enum values. |
| F. Rendering | 15 | 9 | **10** | Up: `mode="ANIMATION"` now reports per-frame progress and cancels between frames, proven against a live MCP client, and the per-frame cost of that orchestration is measured and largely removed (54.86 → 14.52 ms/frame). Unchanged and still decisive: no video/FFmpeg output, no GPU-availability check, no real STILL timeout. |
| G. Compositing | 5 | 1 | **1** | Unchanged: inspection only, no node or link authoring. Reinforced from two directions — Cycles light groups are readable but not authorable (`handlers/lighting/_shared.py:323` is the only `lightgroup` in `src/`), so the AOV half of a compositing workflow cannot be set up either. |
| H. Validation & Reliability | 10 | 7 | **6** | Net down, from a much wider base. Up: the transaction/rollback contract the backlog still lists as Pending is implemented and tested; ~170 finding codes carry severity, code, evidence and remediation. Down: `validate_scene(...)["ready"]` is permanently `False` in a stock Blender (below), a missing scene camera is a WARNING to one reporter and an ERROR to two others, and the aggregate preflight reaches 5 of 11 domains and skips non-mesh materials. Two of those are a few lines each; fixing them justifies 8/10. |
| I. Agentability & NL | 5 | 3 | **3** | Up: documented coordinate spaces, stale-index warnings carried in `warnings`, `next_actions` handoffs, `dry_run` on `setup_liquid_shot`. Down: no dry-run anywhere else, `get_geometry_node_graph` misreports its own pagination after envelope shortening, four retopology limits clamp silently, and one geometry-nodes reply documents a coordinate space that the live builder does not produce. |
| J. Production Completeness | 5 | 2 | **2** | Unchanged: no video output, no compositor authoring, no local import, no pose library, four material presets. |

**TOTAL: 67/100.**

The preliminary figure was 69/100 with three domains unaudited and camera/lighting scored
at a placeholder 10/10. Auditing them lowered the total; that is the audit working, not a
regression in the product. Two of the three newly audited domains (retopology 7, geometry
nodes 6) are large, competent tool sets whose weakness is verification rather than
capability — between them, 9,737 lines of add-on handler code with no `just smoke` coverage
at all.

## Critical defects

| # | Defect | Component | Evidence |
|---|---|---|---|
| 1 | **`validate_scene(...)["ready"]` is permanently `False` in a stock Blender.** The engine probe asks `bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items`, which returns `['BLENDER_EEVEE']` even with Cycles enabled and rendering; `validate_lighting_setup` turns the failed probe into an ERROR-severity `ENGINE_UNAVAILABLE` on every default call, and `ready` is `not any ERROR`. | Lighting / validation | Reproduced independently on Blender 5.2.2 LTS, `--factory-startup`: `addon_utils.check("cycles") == (True, True)`, class-level **and** instance-level enum both `['BLENDER_EEVEE']`, and `resolve_engine("CYCLES")` raises `ValueError: Cycles is not registered in this Blender runtime`. `handlers/lighting/_shared.py:344-347`, `handlers/lighting/inspection.py:299-312`, `handlers/scene.py:1409`. |
| 2 | **No video/FFmpeg output.** Image sequences only; Blender's `FFMPEG` format is rejected by the MCP validator. | Rendering | `03_rendering_compositing.md` |
| 3 | **Compositor is read-only.** No node or link authoring, no way to create a compositor node group. | Rendering | `03_rendering_compositing.md` |
| 4 | **`create_studio_lighting` validates after mutating, and the retry is blocked.** Preview-argument errors are raised after the rig's lights exist, and the handler refuses existing member names, so recovery is manual. | Lighting | `02_camera_lighting.md`, executed against a stub connection |
| 5 | **`create_curve_generator`'s `radius` is inert on Blender 5.2.** `GeometryNodeCurveToMesh` no longer scales its profile by the curve radius attribute — the `Scale` input does, and the builder never sets it. Every cable/pipe/rail comes out ~1 unit thick. | Geometry nodes | Live probe: modifier Radius 0.05 → 0.5 left evaluated bounds bit-identical |

The fix the materials slice proposed for defect 1 — query the enum instance-level — is
**disproven**: `bl_rna` resolves to the type either way. The working query is
`bpy.types.RenderEngine.__subclasses__()`, and this codebase already documents the same
dynamic-enum trap at `handlers/liquid/simulation.py:84-88`.

## Retired claims

Each of these justified part of a preliminary score and does not survive verification:

| Retired claim | Why |
|---|---|
| `add_radial_array_modifier` does not rotate around an arbitrary world-space pivot (High) | Disproved against the evaluated depsgraph. Blender composes the array's object offset as `obj.matrix_world⁻¹ @ offset_object.matrix_world` and places copy *i* at `obj.matrix_world @ offset^i`, so the handler's assignment telescopes to exactly `R_pivot^i @ obj.matrix_world`. `tests/blender_radial_array_smoke.py` matches evaluated world vertices against an independently built pivot composition for all four named cases; worst deviation 2.2e-06 m. |
| Arbitrary Python execution over an unauthenticated socket (Critical) | `execute_blender_code` no longer exists under `src/blender_mcp`. The transport is still unauthenticated, which remains a real finding, but the arbitrary-code half of it is gone. |
| No atomic transaction or rollback contract (High) | Implemented: `bundled/addon/transaction.py` + `object_state.py`, one checkpoint per mutating request, identity-based rollback of created datablocks, 35 tests. |
| Redundant `fluid.py` duplicating `liquid/`, and a GAS surface that is only a pass-through | `fluid.py` was removed in `e422311`; GAS has dedicated domain/flow/effector code and live smoke coverage. The residual truth is tool-pair duplication, not a duplicate module. |
| Camera, world/HDRI, light and render-settings capability gaps marked "production-blocked" | All four exist and are registered; the slice's 40/100 coverage verdict rested on them. |
| "No overlaps in the camera domain" | All nine `_CAMERA_GUIDES` fields are a subset of `_CAMERA_DISPLAY`; two tools write the same nine properties with no cross-reference. |

## What the audit does not cover

- **No slice has GPU/render-farm scale evidence.** Every render finding is measured on
  64px Workbench frames or read from code.
- **Retopology and geometry nodes have no real-Blender regression coverage**
  (9,737 handler lines, zero `tests/blender_*_smoke.py`), so their scores describe code
  that has been read, not code that has been run — except where a slice ran a live probe
  and says so.
- **No domain has reply-budget coverage for its widest replies** in retopology or geometry
  nodes; `scripts/measure_reply_sizes.py` refuses both for want of representative
  arguments, while `build_quad_patch` can return ~250,000 vertex indices.
