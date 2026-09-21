# Validation & Reliability Audit

> **Re-verification note.** This slice was written on 2026-09-21 against the working tree, not
> against the 2026-08-29 audit. Every claim reused from an earlier slice was re-read in current
> code, and the RNA-enum defect inherited from `04_materials_texture_uv.md` was re-tested against
> a live headless **Blender 5.2.2 LTS** with the Cycles add-on enabled. Where this slice
> contradicts an existing one, the contradiction is called out explicitly.
>
> **Line anchors.** `server_core.py` and `scripts/measure_reply_sizes.py` were being edited by
> other work while this slice was written; every `file:line` in it was re-resolved against the
> working tree after the text was complete. Anchor on the quoted symbol if a number has since
> moved again.

## Scope

This audit covers the read-only validation/inspection surface and the reliability machinery that
backs every mutating call:

- **MCP-facing validators**: 11 `validate_*` tools across `src/blender_mcp/server/tools/`
- **MCP-facing inspectors**: 10 `inspect_*` tools, plus the wider read-only command registry
  (`src/blender_mcp/bundled/addon/server_core.py:1638-1700`, 59 commands)
- **Blender-side handlers** for each validator
- **Reliability**: `src/blender_mcp/bundled/addon/transaction.py` (550 lines),
  `src/blender_mcp/bundled/addon/object_state.py` (254 lines), the dispatch routing in
  `server_core.py:1932-2018`
- **Size budgeting**: `src/blender_mcp/server/tools/envelope.py`, `scripts/measure_reply_sizes.py`,
  `tests/server/test_reply_budget.py`
- **Live-Blender proof**: `tests/blender_scene_validate_smoke.py` and the 23 sibling
  `tests/blender_*_smoke.py` scripts
- **Runtime introspection**: real Blender 5.2.2 LTS headless probes driving the repository's own
  handler code through the same `importlib` loader the smoke scripts use
  (`tests/blender_scene_validate_smoke.py:16-28`)

The registered surface is **298 tools** (enumerated via `mcp.list_tools()` with
`BLENDER_MCP_TOOLSETS=all`), against the 45 recorded in `AGENTS.md:137` on 2026-08-29. The
codebase has roughly 6.6x'd; stale findings were assumed guilty until re-read.

## Tool Inventory

### Validators (11)

| Tool | Server | Handler | Bounding | Verdict key |
|---|---|---|---|---|
| `validate_scene` | `tools/scene.py:443` | `handlers/scene.py:1333` | `max_findings` 1-1000 (**cap only**) | `ready` |
| `validate_pbr_asset` | `tools/texture/validation.py:15` | `handlers/texture/validation.py:15` | **none** (`overlap_pair_limit` bounds evidence only) | `ready` |
| `validate_lighting_setup` | `tools/lighting/inspection.py:107` | `handlers/lighting/inspection.py:268` | `limit`+`offset` (**true pagination**) | *(none)* |
| `validate_camera_rig` | `tools/camera/inspection.py:49` | `handlers/camera/inspection.py:138` | **none** (`sample_frames` ≤ 24, `object_names` ≤ 500) | *(none)* |
| `validate_cloth_setup` | `tools/cloth/diagnostics.py:45` | `handlers/cloth/diagnostics.py:232` | `max_findings` (**cap only**) | *(none)* |
| `validate_liquid_setup` | `tools/liquid/inspection_and_setup.py:521` | `handlers/liquid/inspection_and_setup.py:1748` | `max_findings` (**cap only**) | `passed` |
| `validate_liquid_result` | `tools/liquid/result_validation.py:18` | `handlers/liquid/result_validation.py:308` | `frames` ≤ 32, sample cap | `passed` |
| `validate_rigid_body_setup` | `tools/rigid_body/inspection_and_setup.py:603` | `handlers/rigid_body/inspection_and_setup.py:1626` | `max_findings` (**cap only**) | *(none)* |
| `validate_character_rig` | `tools/character_rigging/foundation.py:794` | `handlers/character_rigging/foundation.py:2589` | `issue_limit`+`issue_offset` (**true pagination**) | `valid` |
| `validate_geometry_node_graph` | `tools/geometry_nodes/inspection.py:108` | `handlers/geometry_nodes/inspection.py:406` | **none** | `valid` |
| `validate_retopology` | `tools/retopology/quality.py:57` | `handlers/retopology/quality.py:152` | `issue_limit` per check's `element_indices` | `status` |

### Inspectors (10)

`inspect_animation` (`tools/animation.py:229`), `inspect_delivery`
(`tools/file_lifecycle.py:353`), `inspect_fluid_simulation`
(`tools/liquid/inspection_and_setup.py:545`), `inspect_light`
(`tools/lighting/inspection.py:59`), `inspect_lighting_setup`
(`tools/lighting/inspection.py:75`), `inspect_material` (`tools/texture/materials.py:142`),
`inspect_render_output` (`tools/rendering.py:644`), `inspect_render_setup`
(`tools/rendering.py:221`), `inspect_retopology` (`tools/retopology/target.py:77`),
`inspect_uv_layout` (`tools/texture/uv.py:106`).

Eight of ten carry `limit`/`offset`. `inspect_light` returns one bounded light record;
`inspect_uv_layout` bounds only `overlap_pair_limit`, with no offset over UV layers.

### Adjacent read-only surface

Validation does not stop at the two prefixes. The addon's read-only registry
(`server_core.py:1638-1700`) names **59 commands** that never mutate `bpy.data`, adding
`get_object_info`, `get_mesh_data`, `evaluate_procedural_geometry`, `test_deformation`,
`analyze_surface_conformity`, `estimate_cloth_resources`, `estimate_liquid_resources`,
`plan_render_animation`, and the `get_*_info` family. Six more are read-only *conditionally*,
resolved per-params by `_READ_ONLY_WHEN` (`server_core.py:251-266`) — e.g. a cache `INSPECT`
action, a `dry_run` sewing preview, a conformity analysis with `create_heat_map=False`. That
parameter-sensitive classification is good design: it is the difference between a snapshot the
dispatcher can skip and one it must take.

**Total named validation/inspection surface: 21 tools (11 + 10) of 298 registered.**

## Redundancy Audit

### Genuine aggregation, not duplication

`validate_scene` calls the five domain validators in-process rather than reimplementing them
(`handlers/scene.py:1357-1386`) and normalizes their heterogeneous finding shapes through one
adapter, `_normalized_domain_finding` (`handlers/scene.py:41-54`). That adapter is doing real
work — it exists because the sub-validators disagree about whether the human-readable text lives
in `message` or in a string `evidence` (`handlers/scene.py:44-45`). Aggregating rather than
duplicating is correct. ✓

### The dedup rule is real and proven

Three code paths report a missing camera. `_scene_level_findings` suppresses its own
`MISSING_CAMERA` whenever the camera or lighting domain is in scope
(`handlers/scene.py:73-89`), because both already report it. This is the only cross-validator
dedup in the codebase, and it is exercised against real Blender
(`tests/blender_scene_validate_smoke.py:49-54, 59-67`). ✓

### Inconsistency, not duplication

- **Three names and two severities for one condition.** A scene with no camera produces
  `MISSING_CAMERA`/**ERROR** from `_scene_level_findings` (`handlers/scene.py:76-81`),
  `MISSING_CAMERA`/**ERROR** from lighting (`handlers/lighting/inspection.py:288-298`), and
  `MISSING_SCENE_CAMERA`/**WARNING** from camera (`handlers/camera/inspection.py:153-163`). The
  camera validator — the one whose job this most obviously is — is the one that downgrades it.
- **Two severity vocabularies.** Ten validators use `ERROR`/`WARNING`/`INFO`.
  `validate_retopology` alone uses `PASS`/`WARN`/`FAIL` on per-check `status` fields
  (`handlers/retopology/quality.py:219-228`) with a rolled-up `status`
  (`handlers/retopology/quality.py:463-468`). Verified at runtime: `P8` probe returned
  `['PASS']`.
- **Five verdict spellings, three absences.** `ready` (scene, pbr), `passed` (liquid setup,
  liquid result), `valid` (character rig, geometry nodes), `status` (retopology), and **no
  verdict key at all** for `validate_camera_rig`, `validate_cloth_setup`, and
  `validate_rigid_body_setup` — those return only counts. Confirmed by reading each handler's
  return dict: `handlers/cloth/diagnostics.py:640`,
  `handlers/rigid_body/inspection_and_setup.py:1903`,
  `handlers/liquid/inspection_and_setup.py:2150`,
  `handlers/character_rigging/foundation.py:3109`,
  `handlers/geometry_nodes/inspection.py:505`, `handlers/camera/inspection.py:414-423`.
- **Three count spellings.** `counts` (pbr, `handlers/texture/validation.py:194`), `summary`
  (camera/liquid/rigid/character/geometry), `severity_counts` (cloth).
- **Two truncation spellings.** `truncated`+`total_findings` (scene, liquid) vs.
  `truncated`+`omitted_findings` (cloth, rigid) vs. nothing at all (pbr, camera, geometry).

An agent cannot write one `did_validation_pass(result)` helper against this surface. It needs a
per-tool table. That is an agentability tax paid on every call.

## Reliability Analysis

### The mutation transaction is genuinely strong — and the 2026-08-29 finding is stale

`AGENTS.md:176` still lists *"No atomic transaction or rollback contract — **Pending**"* as a
High finding. **That finding is solved.** `transaction.py` implements identity-based rollback:

- Pre-mutation `session_uid` snapshot across 20+ tracked `bpy.data` collections
  (`transaction.py:17-45`, `_snapshot_ids` at `:116`), so a *renamed* pre-existing datablock is
  never mistaken for a created one.
- Created-datablock removal in dependency order — objects first, libraries last, each reversed
  (`transaction.py:167-190`, `_remove_datablocks` at `:193`).
- Existing-object state restore before removal, so a slot or parent pointing at a
  to-be-removed datablock is repointed first (`transaction.py:374-378`).
- Exactly one named undo checkpoint per successful mutating request, with the *unavailability*
  of that checkpoint surfaced as a client warning rather than swallowed
  (`transaction.py:229-261`).
- Invalidation on database replacement: a `load_post` or library reload disarms the transaction
  so a rollback cannot delete the new file's contents, and the client is told nothing was undone
  via `ROLLBACK_SKIPPED_WARNING` (`transaction.py:286-299, 363-381, 469-474`).
- `Exception`, deliberately not `BaseException`: an Esc-driven `KeyboardInterrupt` leaves the
  partial mutation alone rather than risk an interrupted rollback
  (`transaction.py:436-440, 464-474`).

Routing is explicit and parameter-aware: `bypasses_transaction`
(`server_core.py:1954-1980`) excludes read-only calls, non-undo toggles, session swaps
(`open_shot`/`reset_session`), and the three library-replacing commands. `_run_handler`
(`server_core.py:1982-2018`) converts a handler that *returns* a failure shape into a raised
`HandlerReportedError` **inside** the transaction, so a soft failure rolls back too — that is the
subtle case most implementations miss.

Test coverage is real, not nominal: **20 tests** in `tests/test_mutation_transaction.py` and
**15** in `tests/test_transaction_session_swap.py`, including two named regression guards that
assert the *absence* of the catastrophic behaviours
(`tests/test_mutation_transaction.py:640, 669`).

### State restoration

`object_state.py` captures name, data name, `matrix_basis`, parent/parent-type/parent-bone/
`matrix_parent_inverse`, collection membership, material-slot assignments, the pre-request
modifier name set, and optionally a detached mesh backup (`object_state.py:32-59`). Each field
restores in isolation so one failure does not abort the rest (`object_state.py:61-90`), and
`_restore_materials` refuses to reassign by index when the slot count changed rather than bind
the wrong material (`object_state.py:120-130`). Non-coverage is documented in the module
docstring, not left implicit (`object_state.py:22-26`).

Validators that step the timeline restore it in `finally`, and this is proven live:
`validate_camera_rig` (`handlers/camera/inspection.py:359-406`) is asserted to leave
`scene.frame_current == 7` after sampling frames `[1, 20]`
(`tests/blender_camera_smoke.py:158-161`). `validate_liquid_result` routes through
`_evaluate_frames`, which restores frame *and* subframe in `finally`
(`handlers/liquid/_frame_evaluation.py:102-121`) and reports `timeline_restored`
(`handlers/liquid/result_validation.py:385`). ✓

`validate_liquid_result` is correctly the **one** validator excluded from
`_READ_ONLY_COMMANDS` — it can populate a REPLAY point cache, so it is transacted. Verified: it
is absent from `server_core.py:1638-1700` while the other ten are present.

### Reliability gaps

1. **Transaction target capture is a hand-maintained name table.** `_TARGET_NAME_PARAMS` /
   `_TARGET_NAMES_PARAMS` (`server_core.py:1780-1825`) list 42 parameter spellings.
   `_resolve_targets` (`server_core.py:1859-1930`) also walks `targets`, `fields`, `keyframes`,
   `assignments`, `constraint`, `sources`, `mappings`, and `bodies` records. A survey of the
   server tool signatures finds 30 further `*object_name(s)` spellings outside the table
   (`cage_object_name`, `mirror_object_name`, `pivot_object_name`, `shape_object_name`,
   `surface_object_name`, `high_poly_object_names`, …). Spot checks show those name *referenced*
   or *newly created* objects rather than mutated existing ones — e.g.
   `handlers/retopology/editing.py:529-534` only reads the mirror object,
   `handlers/liquid/mesh_and_materials.py:460-464` creates the helper — so today's coverage looks
   correct. But nothing enforces it: the only guards are five hand-written membership assertions
   for cloth (`tests/server/tools/cloth/test_tools.py:729-731, 894-895`). A future mutating
   parameter with a new spelling gets no rollback and no failing test.
2. **Rollback covers objects and created datablocks, not scene-level settings.** Restoring
   render settings, world, view-layer, or collection-tree changes is per-handler. Where it
   exists it is done properly — `configure_render_settings` snapshots and reverses every applied
   property on exception (`handlers/rendering.py:636-703`), `patch_properties` reverses in
   assignment order (`handlers/lighting/_shared.py:384-391`), `manage_object_constraints` and
   `manage_modifiers` restore or remove (`handlers/scene.py:1169-1184, 1218-1228`) — but there
   is no systematic guarantee, only a per-handler habit.
3. **`_remove_datablocks` and `ObjectState.restore` both suppress every exception**
   (`transaction.py:204-206`, `object_state.py:74-90`). A rollback that itself fails is silent:
   the client is told the command failed, never that the undo of it also failed. The invalidated
   path has a warning (`ROLLBACK_SKIPPED_WARNING`); the partially-failed-rollback path has none.

## Validator Correctness: what the names promise vs. what the code checks

### `validate_scene` — the flagship, and the weakest link

The tool's summary line reads *"aggregating every domain validator"* (`tools/scene.py:454`). It
aggregates **five of eleven**: pbr, lighting, cloth, liquid, camera (`handlers/scene.py:15`,
`:1357-1386`). Excluded: `validate_rigid_body_setup`, `validate_character_rig`,
`validate_geometry_node_graph`, `validate_retopology`, `validate_liquid_result`. The next two
sentences of the docstring do enumerate the five, and the addon's own `limitations` string is
accurate (`handlers/scene.py:1410-1418`), so this is an overstated summary rather than a lie —
but the summary line is the one an agent scanning a 298-tool list actually reads.

Scene-level checks it owns directly (`handlers/scene.py:57-171`): camera presence/type (scoped),
light presence, frame-range inversion, action-outside-playback-range, unapplied scale,
degenerate base-mesh faces, dirty cloth and rigid-body caches. The persistence domain
(`handlers/scene.py:174-220`) reports zero-user local datablocks the save will discard and
fake-user-only actions — a genuinely novel check that no comparable MCP offers.

**No validator anywhere covers render settings.** There is no `validate_render_*` tool
(`tools/rendering.py` exposes only `inspect_render_setup`, `configure_render_settings`,
`manage_view_layers`, `render_scene`, `inspect_render_output`). No preflight checks output-path
writability, output-directory existence, or free disk space. `os.access(..., W_OK)` appears
fourteen times across the add-on — every one of them for a *simulation cache* or export
directory, never for the render output root (`file_paths.py:177` enforces the save target at
write time, not as a finding). `statvfs`/`shutil.disk_usage` appear nowhere in
`src/blender_mcp/`. The `AGENTS.md` H-category note *"disk-space preflight missing"* is still
accurate.

### Can a validator pass a scene a render would fail on? **Yes — verified two ways.**

**(a) Scope-dependent `ready`.** `ready` is `not any(severity == "ERROR")`
(`handlers/scene.py:1409`). Because the camera validator reports a missing camera as a
**WARNING** (`handlers/camera/inspection.py:156`) and the scene-level ERROR is suppressed
whenever the camera domain is in scope (`handlers/scene.py:73`), scoping to the camera domain
alone makes a camera-less scene report ready. Probed against real Blender on an emptied scene:

```
P1 camera-only-scope ready: True codes: ['MISSING_SCENE_CAMERA']
```

`bpy.ops.render.render()` on that scene cannot produce a frame. The aggregate said it was ready.

**(b) The pbr domain is blind to every non-mesh object.** `validate_scene` hands the pbr domain
only `obj.type == "MESH"` names (`handlers/scene.py:1366`), and short-circuits to zero findings
when the list is empty (`handlers/scene.py:1370-1371`). Probed with a TEXT object carrying a
material whose image file does not exist on disk:

```
Q1 pbr-scope on text-only scene: {'pbr': {'findings': 0, 'truncated': False}} findings: 0
Q2 direct validate_pbr_asset on the same material: [('ERROR', 'IMAGE_MISSING')]
```

The same material, addressed directly, is an ERROR. Routed through the aggregate preflight it is
invisible. Curves, text, surfaces, metaballs, volumes and grease pencil are all outside the pbr
domain's reach.

### Can a validator fail a scene a render would succeed on? **Yes — and it does so on every call.**

This is the re-verification of the inherited RNA-enum finding, and the answer is worse than
`04_materials_texture_uv.md` recorded.

`validate_lighting_setup` probes engine availability with `resolve_engine`
(`handlers/lighting/inspection.py:299-312`), which reads `engine_identifiers()`
(`handlers/lighting/_shared.py:344-347`) — a **class-level** RNA enum query. When the probe
raises, the validator emits an **ERROR**-severity `ENGINE_UNAVAILABLE` finding. The default
`target_engine` is `BOTH`, so both engines are probed on every default call. Probed against real
Blender 5.2.2 LTS with the Cycles add-on confirmed enabled
(`addon_utils.check("cycles") == (True, True)`) on a scene with camera, sun, lit cube, material
and UV map:

```
P2 lighting ERRORs:        [('ENGINE_UNAVAILABLE', 'ERROR')]
P3 full-scope ready:       False
P3 ERROR findings:         [('lighting', 'ENGINE_UNAVAILABLE')]
P4 CYCLES-only ERRORs:     [('ENGINE_UNAVAILABLE', 'Cycles is not registered in this Blender runtime')]
P5 cycles render engine:   CYCLES   file: True
```

`P5` set `scene.render.engine = "CYCLES"` and rendered a frame to disk in the same session. The
engine the validator calls unavailable rendered the shot.

**Because `validate_scene` includes the lighting domain by default and `ready` is
`not any ERROR`, `validate_scene(...)["ready"]` is `False` for every scene in a stock Blender,
forever.** The flagship pre-render preflight's single boolean is a constant. And in the Q1/Q3
probe above, the one genuine ERROR (a missing texture) was missed while the one reported ERROR
was false — the gate is wrong in both directions simultaneously.

### RNA-enum defect: verdict

**The defect still exists in the current tree.** Direct runtime evidence, Blender 5.2.2 LTS:

```
BLENDER_VERSION:                5.2.2 LTS
CYCLES_ADDON_ENABLED:           (True, True)
CLASS_LEVEL_ENGINE:             ['BLENDER_EEVEE']
INSTANCE_LEVEL_ENGINE:          ['BLENDER_EEVEE']
RenderEngine.__subclasses__():  ['CYCLES', 'HydraRenderEngine']
SET_CYCLES:                     CYCLES
CLASS_LEVEL_VIEW_TRANSFORM:     ['NONE']
INSTANCE_LEVEL_VIEW_TRANSFORM:  ['NONE']
```

Driving the repository's own functions through the real API:

```
RAISED texture.runtime_engine(CYCLES):   ValueError: Render engine 'CYCLES' is unavailable; runtime engines=['BLENDER_EEVEE']
RAISED texture.validate_engine(CYCLES):  ValueError: Render engine 'CYCLES' is unavailable; runtime engines=['BLENDER_EEVEE']
RESULT texture.runtime_engine(EEVEE):    'BLENDER_EEVEE'
RAISED lighting.resolve_engine(CYCLES):  ValueError: Cycles is not registered in this Blender runtime
RESULT lighting.resolve_engine(EEVEE):   'BLENDER_EEVEE'
```

Two corrections to the inherited finding:

1. **`04_materials_texture_uv.md:106` leaves open whether instance-level queries work. They do
   not.** `scene.render.bl_rna.properties["engine"].enum_items` returns the same
   `['BLENDER_EEVEE']`. `bl_rna` resolves to the type either way, so it never carries the RNA
   pointer a dynamic `itemf` needs. The recommended fix in
   `04_materials_texture_uv.md:253-256` — *"use instance-level"* — **would not work**. The
   codebase already knows this in one place: `handlers/liquid/simulation.py:84-88` documents the
   identical dynamic-enum trap for `openvdb_data_depth` and works around it with a pinned
   `Literal`. `bpy.types.RenderEngine.__subclasses__()` is the query that actually reports CYCLES.
2. **The blast radius is larger than "breaks Cycles-rendering fallback."** Beyond
   `runtime_engine` (`handlers/texture/_shared.py:74-83`) gating
   `create_pbr_material`/`configure_pbr_material`/`patch_shader_graph`
   (`handlers/texture/materials.py:455, 499, 648`) and `render_pbr_material_preview`
   (`handlers/texture/previews.py:57, 132`), it makes `validate_lighting_setup` emit a false
   ERROR and pins `validate_scene(...).ready` to `False` scene-wide.

The `ColorManagedViewSettings` half is confirmed too (`['NONE']` at both levels), but
`handlers/texture/previews.py:116-124` guards every assignment with a membership test, so it
degrades to "no AgX" rather than raising. That half is a cosmetic-quality bug, not a gate.

**Why CI cannot see any of this.** Zero tests reference `ENGINE_UNAVAILABLE`, `runtime_engine`,
or `resolve_engine` (grep over `tests/` returns nothing). Unit tests drive handlers through a
fake `bpy` (`AGENTS.md:107-110`), so a real-RNA behavioural defect is structurally invisible.
And the one live-Blender test of the lighting validator dodges the broken path exactly:
`tests/blender_lighting_smoke.py:251-252` calls `validate_lighting_setup(scene.name, "EEVEE")` —
never `BOTH`, never `CYCLES` — and asserts only `"findings" in validated`. The texture smoke test
is the same shape: `validate_pbr_asset(...)` then `assert "findings" in validation`
(`tests/blender_texture_smoke.py:188-189`). Both are presence assertions, not behaviour
assertions. Nothing pins the buggy behaviour either, so a fix is unblocked.

### Finding structure: actionable, not boolean ✓

This is the surface's clear strength. The canonical finding is
`{severity, code, subject, message, evidence, remediation}`
(`handlers/scene.py:41-54`, `handlers/texture/validation.py:8-9`), with stable machine-readable
codes and a concrete next action. Representative code inventories (extracted from the handlers):

- `validate_camera_rig` — 17 codes incl. `PARENT_CYCLE`, `AIM_TARGET_BEHIND_CAMERA`,
  `FOCUS_TARGET_BEHIND_CAMERA`, `NEGATIVE_PARENT_SCALE`, `BROKEN_DRIVER_TARGET`,
  `OVERLAPPING_CAMERA_MARKERS`, `SHARED_CAMERA_DATA`
- `validate_lighting_setup` — 14 codes incl. `NONPOSITIVE_LIGHT_ENERGY`, `COINCIDENT_LIGHTS`,
  `LIGHT_AIMED_AWAY`, `MISSING_LIGHT_DEPENDENCY`, `LINK_COLLECTION_OUTSIDE_SCENE`,
  `EXPENSIVE_VOLUME_PROBE`, `CROSS_ENGINE_DIFFERENCES`
- `validate_pbr_asset` — 14 codes (`handlers/texture/validation.py:29-193`)
- `validate_cloth_setup` — ~30 codes incl. `ALL_VERTICES_PINNED`, `INITIAL_COLLIDER_INTERSECTION`,
  `INTERSECTION_CHECK_INCOMPLETE`, `DEFORMER_AFTER_CLOTH`, `INCONSISTENT_NORMAL_WINDING`
- `validate_rigid_body_setup` — ~25 codes incl. `ACTIVE_CONCAVE_MESH`, `EXTREME_MASS_RATIO`,
  `INITIAL_INTERPENETRATION`, `DISCONNECTED_COLLISION_LAYERS`
- `validate_liquid_setup` — ~30 codes incl. `CLOSED_INFLOW_DOMAIN`, `FLOW_BELOW_GRID_SCALE`,
  `FLOW_NORMALS_LIKELY_INWARD`, `CACHE_NOT_WRITABLE`, `NON_MANIFOLD_FLUID_GEOMETRY`
- `validate_character_rig` — ~30 codes incl. `BONE_HIERARCHY_CYCLE`,
  `CONSTRAINT_DEPENDENCY_CYCLE`, `BROKEN_DRIVER_BONE_TARGET`, `EXCESSIVE_INFLUENCES`

Several validators also refuse to over-claim, which is rarer and more valuable than the checks
themselves: `validate_camera_rig` returns
`"verification": "Structural and evaluated-transform checks only; visual correctness was not
inferred."` (`handlers/camera/inspection.py:423`, asserted live at
`tests/blender_camera_smoke.py:161`); `validate_scene` ships a two-item `limitations` list
(`handlers/scene.py:1410-1418`); `validate_liquid_result` states that its volumes are
grid-sampled estimates (`handlers/liquid/result_validation.py:386-391`);
`validate_character_rig`'s one-line docstring says outright *"this does not certify artistic
deformation quality"* (`tools/character_rigging/foundation.py:804`).

### Noise that will train agents to ignore findings

`SHADOWS_DISABLED` fires on every deliberately shadowless fill light
(`handlers/lighting/inspection.py:355-364`). `UNAPPLIED_SCALE` fires on every mesh whose scale is
not 1.0 (`handlers/scene.py:131-139`) — near-universal mid-production and harmless to a render.
Both are WARNING so neither flips `ready`, but they dominate the finding list on a real shot and
compete for the byte budget with findings that matter. `validate_scene` also has no
`profile`/`target_engine` parameter (`tools/scene.py:443-451`) and calls the pbr and lighting
validators at their `BOTH` defaults (`handlers/scene.py:1362, 1368`), so a Cycles-only delivery
is permanently told its displacement links are `EEVEE_DISPLACEMENT_UNSUPPORTED`
(`handlers/texture/validation.py:171-181`).

## Pagination & Size Budgeting

The envelope enforces an **8 KiB wire budget** on every reply
(`tools/envelope.py:72`), measured in the bytes FastMCP actually sends
(`to_json(..., indent=2)`, `tools/envelope.py:83-84`). When a reply is over, `_fit_budget`
(`tools/envelope.py:242-282`) finds every list of ≥2 records anywhere in the payload, sorts by
encoded size, and bisects each down until the reply fits, walking on to the next-largest page
when one cut is not enough. It writes the truncation warning and pagination keys *before*
measuring so the final reply cannot be pushed back over by its own metadata
(`tools/envelope.py:183-188, 214-220`), and it withholds a resume offset when every page is down
to one record, rather than sending the agent round a loop of over-budget replies
(`tools/envelope.py:158-161, 278-282`). This is careful, well-reasoned code.

**But it is a backstop, not pagination, and only 2 of 11 validators can actually be paged.**
`_pagination_names` (`tools/envelope.py:129-155`) only updates `truncated`/`next_offset` when the
payload already carries them. The unpaginated validators do not. Measured directly against the
shipped `ok()` with a realistic 120-finding `validate_pbr_asset` payload:

```
raw bytes:              41058
after ok bytes:          7935   (budget 8192)
findings kept:             21 of 120
counts still say:  {'ERROR': 0, 'WARNING': 120, 'INFO': 0}   ready: True
truncated key present: False  | next_offset present: False
WARNING: findings was shortened to 21 of 120 records to stay within the 8192-byte reply
         budget; rerun with a narrower scope to see the rest.
```

99 of 120 findings are unreachable, and `data.truncated` is absent — a client keying on that
field sees nothing. The mitigation is real: `counts` and `ready` are computed addon-side over the
full set, so the *verdict* stays honest even when the *evidence* is cut, and the warning is
explicit. But "rerun with a narrower scope" is the only recovery, and for
`validate_geometry_node_graph` (scoped to one node group already) or `validate_camera_rig`
(scoped to one scene) there may be no narrower scope to rerun with.

The four `max_findings` validators are worse than they look: a cap without an offset means
findings past the cap are **permanently** unreachable, not merely on the next page. Raising
`max_findings` to its ceiling just moves the loss from the handler to the envelope shortener.

**Regression coverage is thin.** `tests/server/test_reply_budget.py:49-53` asserts no reply
exceeds the budget — but only for the `shot` toolset
(`camera`, `lighting`, `lighting-construction`, `rendering`, `character-posing` + core, per
`src/blender_mcp/server/bundles.py:85`). `scripts/measure_reply_sizes.py` carries sample payloads
for **3 of 11** validators: `validate_scene` (`:948`), `validate_camera_rig` (`:1296`),
`validate_lighting_setup` (`:1448`). The other eight have no ceiling. The harness is also honest
about the cap-only design, annotating the growth axis *"Validation findings `validate_scene`
reports; **it pages none**"* (`scripts/measure_reply_sizes.py:66`, growth axis at `:2326` priced
at 84 findings).

## Live-Blender Proof

`tests/blender_scene_validate_smoke.py` (176 lines) is the strongest validation smoke script in
the repository and is genuine behavioural proof, not a presence assertion:

- Strips the factory scene to empty and asserts the full default domain list runs
  (`:43-45`)
- Asserts the cross-validator dedup rule in both directions — suppressed when camera/lighting are
  in scope, restored when they are not (`:49-54`, `:59-67`)
- Asserts input rejection for unknown scope, empty scope, out-of-range `max_findings`, and
  unknown scene (`:69-96`)
- Asserts findings clear after the defect is fixed (`:106-109`)
- Asserts `ANIMATION_OUTSIDE_FRAME_RANGE`, `UNAPPLIED_SCALE`, `DEGENERATE_GEOMETRY` fire with
  correct subjects, and that three WARNINGs still leave `ready == True` (`:133-142`)
- Asserts truncation semantics (`:144-147`)
- Asserts the persistence domain distinguishes `UNREFERENCED_DATABLOCK` from
  `ACTION_KEPT_BY_FAKE_USER_ONLY` (`:156-174`)
- Documents *why* `INVALID_FRAME_RANGE` is covered by a unit test instead — Blender's RNA range
  callbacks make an inverted range unconstructible through the API (`:111-117`). Explaining a
  coverage gap rather than faking it is exactly right.

Against that, the other validators' live coverage is thin: `validate_lighting_setup` and
`validate_pbr_asset` get `assert "findings" in result` and nothing more
(`tests/blender_lighting_smoke.py:251-252`, `tests/blender_texture_smoke.py:188-189`), and the
lighting one deliberately passes `"EEVEE"` — the one argument that avoids the broken path.
`validate_camera_rig` is the good counter-example, asserting playhead restoration and the
no-over-claim contract (`tests/blender_camera_smoke.py:158-161`). Six validators
(`validate_cloth_setup`, `validate_rigid_body_setup`, `validate_character_rig`,
`validate_retopology`, `validate_geometry_node_graph`, `validate_liquid_result`) have no
dedicated validation assertions in any of the 24 smoke scripts.

## Gaps & Limitations

- **No render-settings validator.** Nothing preflights output path writability, output directory
  existence, free disk space, resolution/sample sanity, or engine-feature availability. The
  largest single class of "the render failed at 2am" causes is unvalidated.
- **No GPU/device preflight.** Still open from 2026-08-29; a silent CPU fallback is undetectable.
- **`validate_scene` covers 5 of 11 domains.** Rigid body, character rig, geometry nodes,
  retopology and liquid-result findings never reach the aggregate.
- **pbr domain is mesh-only** (`handlers/scene.py:1366`) — verified blind to text/curve/volume
  materials.
- **No `profile`/`target_engine` on `validate_scene`** — always audits against `BOTH`.
- **Rollback has no failure channel.** A rollback that partially fails is silent.
- **Nine of eleven validators cannot be paged**; eight have no measured byte ceiling.
- **Five verdict spellings and three validators with no verdict at all.**

## Architecture Assessment

### Strengths

1. **Identity-based transaction with an explicit, documented contract** — including what it does
   *not* guarantee (`transaction.py:427-440`), backed by 35 tests. This is the single best piece
   of engineering in the surface. ✓
2. **Parameter-sensitive read-only classification** (`server_core.py:251-266, 1932-1952`) — the
   dispatcher knows a cache `INSPECT` is free and a cache `BAKE` is not. ✓
3. **Structured, remediable findings with stable codes** across ~170 distinct codes. ✓
4. **Universal, well-reasoned byte-budget backstop** with correct measure-then-write ordering and
   a deliberate refusal to hand back a useless resume offset. ✓
5. **Validators that state their own limits** rather than implying visual certification. ✓
6. **The persistence domain** — reporting what a save will silently discard is a check no
   comparable MCP performs. ✓

### Weaknesses

1. **The aggregate gate is broken in production.** `validate_scene(...).ready` is permanently
   `False` in stock Blender via a false `ENGINE_UNAVAILABLE` ERROR. ⚠️ **CRITICAL**
2. **`ready` is scope-dependent and can be `True` for an unrenderable scene.** ⚠️
3. **Five verdict vocabularies, two severity vocabularies, three count spellings.** No single
   client-side pass/fail predicate is possible. ⚠️
4. **Pagination is the exception, not the rule** — 2 of 11.
5. **CI is structurally blind to real-RNA defects**, and the live smoke tests that could catch
   them assert presence rather than behaviour.

## Recommended Immediate Actions

1. **Fix engine detection (CRITICAL, low effort).** Replace both class-level enum queries
   (`handlers/lighting/_shared.py:344-347`, `handlers/texture/_shared.py:74-83`) with
   `bpy.types.RenderEngine.__subclasses__()` plus the built-in EEVEE identifier — verified to
   report `['CYCLES', 'HydraRenderEngine']` where the enum reports `['BLENDER_EEVEE']`. Do **not**
   apply `04_materials_texture_uv.md:253-256`'s instance-level fix; it is disproven above. Add a
   live smoke assertion that `validate_lighting_setup(scene, "BOTH")` yields **no**
   `ENGINE_UNAVAILABLE` finding, and change
   `tests/blender_lighting_smoke.py:251` from `"EEVEE"` to the default so the path is exercised.
2. **Make `ready` scope-independent (CRITICAL, low effort).** Raise
   `MISSING_SCENE_CAMERA` to ERROR in `handlers/camera/inspection.py:156` to match the other two
   reporters, or compute `ready` from a fixed set of render-blocking conditions rather than from
   whichever domains happened to be in scope.
3. **Unify the result contract (high value, medium effort).** One verdict key, one severity
   vocabulary, one count key, one truncation key across all eleven. Migrate
   `validate_retopology`'s `PASS`/`WARN`/`FAIL` into `ERROR`/`WARNING`/`INFO` and give the three
   verdict-less validators a verdict.
4. **Widen the pbr domain beyond meshes** (`handlers/scene.py:1366`) — every object type with
   `material_slots`.
5. **Add `limit`/`offset` to the nine unpaginated validators**, and sample payloads for the eight
   unmeasured ones in `scripts/measure_reply_sizes.py` so they get a byte ceiling.
6. **Add a `render` domain to `validate_scene`**: output-path writability, output-directory
   existence, free disk space, and device availability.
7. **Give rollback a failure channel** — replace the blanket `suppress(Exception)` in
   `transaction.py:204-206` / `object_state.py:74-90` with per-field capture that surfaces a
   `ROLLBACK_INCOMPLETE` warning.
8. **Guard the transaction target table** with a test that fails when a mutating command exposes
   an object-name parameter spelling absent from `_TARGET_NAME_PARAMS`/`_TARGET_NAMES_PARAMS`.
9. **Correct `AGENTS.md:176`** — "No atomic transaction or rollback contract" is **Solved**, not
   Pending. Leaving a solved High finding in the register misdirects future work.

## Summary: Validation & Reliability Domain Score

| Category | Score | Evidence |
|---|---|---|
| **Coverage breadth** | 8/10 | 11 validators, 10 inspectors, 59 read-only commands, ~170 finding codes. Gaps: no render-settings validator, no GPU/disk preflight, `validate_scene` covers 5 of 11 domains. |
| **Correctness of verdicts** | 3/10 | `validate_scene.ready` permanently `False` via false `ENGINE_UNAVAILABLE` (verified, Blender 5.2.2); `ready: True` for an unrenderable camera-less scene under a camera-only scope (verified); pbr domain blind to non-mesh materials (verified). |
| **Result structure** | 8/10 | Severity + stable code + subject + evidence + remediation throughout; explicit `limitations`/`verification` fields refusing to over-claim. Docked for 5 verdict spellings, 2 severity vocabularies, 3 validators with no verdict. |
| **Pagination / size budget** | 5/10 | Universal 8 KiB backstop with correct bisection and honest warnings; but only 2 of 11 truly pageable, 4 cap-only-unreachable, 8 with no measured ceiling. |
| **Reliability (transaction/rollback)** | 9/10 | Identity-based rollback, ordered removal, object-state restore, one undo checkpoint, session-swap invalidation, documented non-guarantees, 35 tests. Docked for silent rollback failure and the unguarded target-name table. |
| **State restoration** | 9/10 | Every frame-stepping path restores in `finally`, proven live (`tests/blender_camera_smoke.py:158-161`); per-handler property snapshots where the transaction does not reach. |
| **Live-Blender proof** | 5/10 | `blender_scene_validate_smoke.py` is exemplary behavioural proof; the other validators get `assert "findings" in result`, and the lighting smoke deliberately passes the one argument that avoids the broken path. |

**Domain Status**: The reliability half is production-grade and the prior audit's "no transaction
contract" finding is closed. The validation half has excellent breadth and an excellent finding
*format* wrapped around a verdict that is currently wrong in both directions. Two low-effort
fixes (engine detection, camera severity) move this domain from "cannot be trusted as a gate" to
"can be trusted as a gate."

## Score estimate

**Score estimate**: 6/10

The surface earns high marks for breadth (21 named tools, ~170 stable finding codes, every
finding carrying evidence and remediation), for a mutation-transaction contract that is genuinely
among the best-engineered code in this repository, and for a validator culture that states its
own limits rather than implying certification. It loses four points because the flagship
aggregate preflight's single boolean is wrong: `validate_scene(...).ready` is pinned to `False`
in every stock Blender by a false ERROR, and is `True` for a scene that cannot render at all
under a plausible scope — both verified by running the repository's own handlers against a live
headless Blender 5.2.2 LTS. A gate that is wrong in both directions is not a gate. The supporting
deductions are the five-way verdict-key split that prevents any single client-side pass/fail
predicate, and pagination that reaches only 2 of 11 validators.

### Mapping onto the 100-point rubric (`COMPREHENSIVE_AUDIT_FINDINGS.md:71-152`)

| Cat | Current | Impact from this slice | Recommended |
|---|---|---|---|
| **A. Architecture & Abstraction (15)** | 11/15 | The RNA design flaw is confirmed and its blast radius is wider than recorded (it now poisons the aggregate validator, not just material tools). Offsetting it: `transaction.py` + `object_state.py` + the parameter-sensitive dispatch routing are a first-class abstraction the 2026-08-29 audit did not see. Also new: five verdict vocabularies across one tool family is an abstraction failure. | **11/15** (unchanged — new strength and new weakness cancel) |
| **B. Tool Quality & Redundancy (15)** | 12/15 | No duplicated validators; `validate_scene` aggregates rather than reimplements. But its summary promises "every domain validator" and delivers 5 of 11 (`tools/scene.py:454` vs `handlers/scene.py:15`), and three validators ship no verdict key. | **11/15** (−1) |
| **C. Scene & Asset Pipeline (10)** | 7/10 | The persistence domain (`handlers/scene.py:174-220`) — reporting what a save will discard — is a real pipeline capability not credited in the preliminary score. | **8/10** (+1) |
| **D. Lighting & Camera (10)** | 10/10 | `validate_lighting_setup` emits a false ERROR on every default call; `validate_camera_rig` downgrades a missing camera to WARNING, breaking the aggregate. A 10/10 is not sustainable with the domain's own validator broken. | **9/10** (−1) |
| **E. Animation/Rigging/Simulation (10)** | 7/10 | `validate_character_rig`, `validate_cloth_setup`, `validate_rigid_body_setup`, `validate_liquid_setup`, `validate_liquid_result` are substantial and honest about their limits, but none has live-Blender behavioural proof. | **7/10** (unchanged) |
| **F. Rendering (15)** | 9/15 | No render-settings validator exists; no output-path, disk-space, or device preflight anywhere in `src/blender_mcp/`. Confirms the existing deduction. | **9/15** (unchanged) |
| **G. Compositing (5)** | 1/5 | Out of scope for this slice; `inspect_render_setup` includes a paginated compositor section (`handlers/rendering.py:620-624`), consistent with "inspection-only". | **1/5** (unchanged) |
| **H. Validation & Reliability (10)** | 7/10 | See recommendation below. | **6/10** (−1) |
| **I. Agentability & NL (5)** | 3/5 | Findings are agent-shaped (code + evidence + remediation), but five verdict spellings, two severity vocabularies and three count keys mean no single "did it pass?" helper is writable. | **3/5** (unchanged — the good format offsets the inconsistency) |
| **J. Production Completeness (5)** | 2/5 | No render-settings preflight is a production-completeness gap as much as a rendering one. | **2/5** (unchanged) |

### Category H recommendation: **6/10** (down from the preliminary 7/10)

The preliminary 7 rested partly on *"RNA enum bug breaks Cycles rendering"*. That claim is
**re-verified as still true, and understated**: the bug does not merely break a fallback inside
the material tools, it makes `validate_lighting_setup` emit a false ERROR on every default call
and pins `validate_scene(...).ready` to `False` for every scene in a stock Blender with Cycles
enabled. Proven by running the repository's handlers against live Blender 5.2.2 LTS, in the same
session in which `scene.render.engine = "CYCLES"` rendered a frame to disk.

Scoring the ten points:

- **+3 reliability.** The transaction contract that `AGENTS.md:176` still lists as *Pending* is
  fully implemented and tested (`transaction.py`, `object_state.py`, 35 tests). State restoration
  is correct and proven live. This is a clear improvement the preliminary score predates.
- **+3 validation breadth and result quality.** 11 validators, ~170 finding codes, universal
  severity/code/evidence/remediation structure, explicit `limitations` fields.
- **−2 the false `ENGINE_UNAVAILABLE` ERROR.** A preflight whose verdict is a constant is worse
  than no preflight, because agents will learn to ignore it.
- **−1 scope-dependent `ready`.** `validate_scene(scope=["camera"])` returns `ready: True` for a
  camera-less scene.
- **−1 contract inconsistency and pagination.** Five verdict spellings; 9 of 11 validators
  unpageable; 8 of 11 with no measured byte ceiling.

Net **6/10**. The two −2/−1 deductions for verdict correctness are each a few lines of work
(`handlers/lighting/_shared.py:344-347` and `handlers/camera/inspection.py:156`); fixing both
would justify **8/10** immediately, which would make H the strongest category in the rubric
rather than a mid-table one. The disk-space and GPU-fallback items noted in the preliminary
justification remain open and are the difference between 8 and 10.

### The three most severe findings

1. **CRITICAL — `validate_scene(...).ready` is permanently `False` in stock Blender.**
   Class-level RNA enum queries (`handlers/lighting/_shared.py:344-347`,
   `handlers/texture/_shared.py:74-83`) report `['BLENDER_EEVEE']` while
   `RenderEngine.__subclasses__()` reports `['CYCLES', 'HydraRenderEngine']` and
   `scene.render.engine = "CYCLES"` renders successfully. `validate_lighting_setup` turns the
   failed probe into an **ERROR** (`handlers/lighting/inspection.py:299-312`), and `ready` is
   `not any ERROR` (`handlers/scene.py:1409`). The inherited fix recommendation
   (`04_materials_texture_uv.md:253-256`, "query instance-level") is disproven — instance-level
   returns the same truncated enum.
2. **HIGH — `ready: True` for a scene that cannot render.** `validate_camera_rig` reports a
   missing camera as WARNING (`handlers/camera/inspection.py:156`) while the two other reporters
   call it ERROR, and the scene-level ERROR is suppressed whenever the camera domain is in scope
   (`handlers/scene.py:73`). Verified: `validate_scene(scene, scope=["camera"])` on an emptied
   scene returns `ready: True` with only `MISSING_SCENE_CAMERA`.
3. **HIGH — the aggregate preflight is blind to non-mesh materials and to 6 of 11 domains.**
   `handlers/scene.py:1366` scopes the pbr domain to `obj.type == "MESH"`. Verified: a TEXT
   object whose material references a missing image file yields **0** pbr findings from
   `validate_scene`, while `validate_pbr_asset` on the same material yields `ERROR
   IMAGE_MISSING`. Rigid body, character rig, geometry nodes, retopology and liquid-result
   findings never reach the aggregate at all, despite a summary line promising "every domain
   validator" (`tools/scene.py:454`).
