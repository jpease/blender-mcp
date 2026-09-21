# Retopology Audit

## Scope

This audit covers the retopology domain — target creation, guide authoring, quad construction,
edge-flow editing, surface projection, quality measurement, and production handoff (attribute
transfer, UVs, cages, bakes) — via:

- **MCP-facing tools** (server): `src/blender_mcp/server/tools/retopology/` — 8 files, 1,091 lines
  (`__init__.py`, `_shared.py`, `target.py`, `construction.py`, `editing.py`, `quality.py`,
  `production.py`, `advanced.py`)
- **Add-on handlers** (Blender-facing): `src/blender_mcp/bundled/addon/handlers/retopology/` —
  8 files, 4,658 lines, composed into `RetopologyHandlersMixin`
  (`bundled/addon/handlers/retopology/__init__.py:11-19`)
- **Dispatch / safety layer**: `bundled/addon/server_core.py:1383-1409` (command table),
  `:1690-1692` (read-only set), `:1827-1856` (`_GEOMETRY_MUTATING_COMMANDS`),
  `bundled/addon/transaction.py`
- **Bundle packaging**: `src/blender_mcp/server/bundles.py:51` (`retopology`), `:86` (`asset` mode)

**No live Blender was available for this audit.** Every claim below is read from source, or from a
locally-run read-only script whose output is quoted. Claims that would require a running Blender to
settle are marked **[needs live Blender]** and are stated as unverified assumptions, not findings.
That distinction matters more here than in other domains: this is the only large domain in the
repository with no smoke script at all (see *Verification Coverage*).

## Tool Inventory

Counted mechanically: 27 `@mcp.tool()` registrations across the six topic modules, confirmed by
importing a `BLENDER_MCP_TOOLSETS=retopology` process and intersecting the registry
(`bundles.resolve_toolset_modules("retopology")` → 27 retopology tool names registered).

### Target lifecycle — `server/tools/retopology/target.py` (3)
- `create_retopology_target(source_object_names[], name?, initial_geometry, collection_name, size, grid_segments, add_mirror, add_shrinkwrap, subdivision_levels)` — `target.py:18-74`; handler `handlers/retopology/target.py:196-269`. Five starter strategies: EMPTY / SINGLE_VERTEX / PLANE / GRID / DUPLICATED_EVALUATED_SURFACE (`target.py:14`).
- `inspect_retopology(object_name, selected_vertex_indices?, adjacency_depth, limit, offset)` — `target.py:76-113`; handler `handlers/.../target.py:271-398`. The domain's index/revision oracle.
- `manage_retopology_checkpoint(action, object_name, checkpoint_name?, confirm)` — `target.py:115-145`; handler `:400-563`. CREATE/LIST/COMPARE/RESTORE/DELETE against a hidden backup collection.

### Construction — `server/tools/retopology/construction.py` (7)
- `create_retopology_guides(source_object_name, guides[], collection_name, projection_offset, max_projection_distance?)` — `construction.py:16-50`; handler `handlers/.../construction.py:638-714`.
- `create_surface_section(source_object_name, plane_origin, plane_normal, vertex_count, ...)` — `construction.py:52-79`; handler `:716-796`. Plane/surface intersection, resampled.
- `set_retopology_features(object_name, edge_indices?, detect_source_object_name?, source_dihedral_angle?, include_material_boundaries, guide_object_names?, guide_distance, apply_detected, seam?, sharp?, crease?, bevel_weight?, expected_revision?)` — `construction.py:81-113`; handler `:798-895`.
- `add_support_loops(object_name, edge_indices[], width, side, clamp, corner_policy, ...)` — `construction.py:115-148`; handler `:897-980`.
- `build_quad_patch(object_name, corners[4], u_segments, v_segments, ..., interpolation, boundary_u0/u1/v0/v1?)` — `construction.py:150-183`; handler `:186-334`. BILINEAR or Coons.
- `extend_boundary(object_name, ordered_boundary_vertex_indices[], rows, distance, mode, ...)` — `construction.py:185-216`; handler `:336-442`. Four growth modes.
- `fill_boundary_quads(object_name, boundary_edge_indices[], span, offset, use_interp_simple, ...)` — `construction.py:218-244`; handler `:569-636`. Grid Fill, quad-only.

### Editing — `server/tools/retopology/editing.py` (6)
- `configure_surface_projection(...)` — `editing.py:18-54`; handler `handlers/.../editing.py:110-181`. Named live Shrinkwrap, idempotent, optional `apply`.
- `project_mesh_elements(...)` — `editing.py:56-89`; handler `:183-281`. NEAREST/RAYCAST BVH projection with boundary and symmetry-plane holds.
- `reroute_topology(object_name, action, ...)` — `editing.py:91-117`; handler `:283-346`. CONNECT/ROTATE_DIAGONAL/COLLAPSE/DISSOLVE/SPLIT.
- `relax_topology(...)` — `editing.py:119-144`; handler `:348-410`. Tangential Laplacian, LoopTools-free (`editing.py:139`).
- `redistribute_edge_loop(...)` — `editing.py:146-169`; handler `:412-501`. Arc-length resample with corner protection.
- `configure_retopology_symmetry(...)` — `editing.py:171-198`; handler `:503-584`. Live Mirror + KD-tree seam audit.

### Quality — `server/tools/retopology/quality.py` (3)
- `analyze_surface_conformity(...)` — `quality.py:15-54`; handler `handlers/.../quality.py:65-150`. Distance statistics + optional POINT/FLOAT heat map.
- `validate_retopology(object_name, profile, ...)` — `quality.py:56-83`; handler `:152-474`. 14 named checks against CHARACTER/HARD_SURFACE/VFX/GAME thresholds.
- `test_deformation(object_name, frames[], ...)` — `quality.py:85-113`; handler `:476-626`. Multi-frame evaluated-mesh comparison.

### Production handoff — `server/tools/retopology/production.py` (4)
- `transfer_mesh_attributes(source_object_name, object_name, data_types[], ...)` — `production.py:26-83`; handler `handlers/.../production.py:35-172`.
- `unwrap_retopology_uvs(...)` — `production.py:85-110`; handler `:174-279`.
- `create_bake_cage(object_name, high_poly_object_names[], ...)` — `production.py:112-148`; handler `:281-369`.
- `bake_retopology_maps(object_name, high_poly_object_names[], map_type, output_path, ...)` — `production.py:150-184`; handler `:371-531`.

### Advanced accelerators — `server/tools/retopology/advanced.py` (4)
- `generate_quadriflow_draft(source_object_name, ...)` — `advanced.py:20-54`; handler `handlers/.../advanced.py:574-639`.
- `fit_surface_primitive(source_object_name, primitive, source_vertex_indices[], expected_source_revision, ...)` — `advanced.py:56-93`; handler `:641-727`. PLANE/CYLINDER/CONE/SPHERE least-squares fits.
- `bind_surface_deformation(object_name, action, ...)` — `advanced.py:95-128`; handler `:729-895`.
- `generate_retopology_lods(object_name, levels[], profile, ..., confirm)` — `advanced.py:130-178`; handler `:897-1030`.

**Total MCP surface: 27 tools.** No free-form BMesh/bpy escape hatch is exposed anywhere in the
domain; every mutation goes through a named, validated operation.

### An eighth handler that is not a retopology tool

`mesh_bridge` lives in the retopology handler module (`handlers/retopology/construction.py:444-567`)
but registers as an MCP tool from `server/tools/mesh.py:224`, i.e. in the `core-authoring` bundle
(`bundles.py:34`), not `retopology` (`bundles.py:51`). Verified empirically: a
`BLENDER_MCP_TOOLSETS=retopology` process registers 27 retopology tools and **not** `mesh_bridge`
(nor `mesh_remesh`). `tests/server/tools/retopology/test_tools.py:22` nonetheless lists
`mesh_bridge` inside `RETOPOLOGY_TOOL_NAMES`, and `test_all_retopology_tools_are_registered`
(`:55-58`) passes only because the test process imports `mesh` too (`:139`). A real
`shot,retopology` process therefore loses the only loop-to-loop bridge in the surface while the
test suite asserts it is present. This is a packaging bug, not a naming preference.

---

## Redundancy Audit

### No duplication
- Baking is shared across domains rather than reimplemented: `handlers/texture/baking.py:100`
  calls `self.bake_retopology_maps(...)` instead of duplicating the Cycles bake path — the same
  cross-domain reuse `04_materials_texture_uv.md:48` credits from the other side.
- UV overlap detection is shared: `handlers/texture/uv.py:10` imports `_require_finished` and
  `_uv_overlap_pairs` from `handlers/retopology/_shared.py`.
- `validate_retopology` is reused internally rather than re-inlined, by
  `generate_quadriflow_draft` (`handlers/.../advanced.py:612-617`) and per-LOD by
  `generate_retopology_lods` (`handlers/.../advanced.py:999-1004`).
- Shrinkwrap configuration exists once (`handlers/.../_shared.py:194-230`) and is called by both
  `create_retopology_target` (`handlers/.../target.py:240-256`) and `configure_surface_projection`
  (`handlers/.../editing.py:155-171`).

### Overlaps that are real but defensible
- **`project_mesh_elements` vs `configure_surface_projection`**: one is a destructive one-shot
  vertex move, the other a live modifier. Different reversibility contracts, correctly separated. ✓
- **`relax_topology` vs `redistribute_edge_loop`**: patch-interior smoothing vs 1-D arc-length
  resampling. Distinct. ✓
- **`generate_quadriflow_draft` vs `generate_retopology_lods(method=QUADRIFLOW)`**: draft from a
  source vs derivative from an approved master; `_run_quadriflow`
  (`handlers/.../advanced.py:105-120`) is shared. ✓

### Genuine inconsistency
- **Two modifier-apply paths in one domain.** `handlers/.../advanced.py:436-441` defines
  `_apply_modifier_checked`, which calls `bpy.ops.object.modifier_apply` and passes the result to
  `_require_finished`. But the two *user-facing* `apply=True` flags call the unchecked shared helper
  `helpers.apply_modifier` (`bundled/addon/helpers.py:263-266`), which discards the operator
  result entirely: `configure_surface_projection` at `handlers/.../editing.py:173` and
  `transfer_mesh_attributes` at `handlers/.../production.py:129`. See *Operator result checking*.

---

## Reliability Analysis

### Architecture: revision-gated indices ✓ (strongest pattern in the domain)

`topology_revision()` (`handlers/.../_shared.py:64-76`) is a blake2b digest over vertex/edge/polygon
counts, vertex coordinates at 9 significant figures, and edge/face connectivity. `_require_revision`
(`:79-86`) rejects a stale `expected_revision` with an error naming both hashes and telling the
agent to re-run `inspect_retopology`. Eleven mutating tools accept `expected_revision`, and
`fit_surface_primitive` makes it **mandatory** (`handlers/.../advanced.py:657-659`:
`expected_source_revision is None` raises before anything else runs). This is the cleanest answer to
"reusing invalid topology indices" anywhere in the codebase — the prior audit recorded that finding
as solved by a *warning* (`AGENTS.md:152`); this domain solved it with a checksum.

Correctly, the digest covers only connectivity and positions, so `set_retopology_features` — which
writes `sharp_edge`/`crease_edge`/`bevel_weight_edge` attributes
(`handlers/.../construction.py:876-884`) — leaves the revision unchanged, and its server tool is the
one construction tool whose `ok()` call attaches no `STALE_INDEX_WARNING`
(`server/tools/retopology/construction.py:112`, against `:146`, `:181`, `:214`, `:243`). That is
consistent, not an omission.

### Architecture: dry-run before commit ✓

`reroute_topology` performs the whole edit twice: once on a throwaway BMesh built from `obj.data` to
prove the result is manifold, duplicate-free, and introduces no unintended boundary
(`handlers/.../editing.py:298-327`), then again on the real mesh (`:329-334`). The simulation is
freed in `finally` (`:326-327`). `_validate_reroute_result` (`:94-104`) rejects >2-face edges,
duplicate face keys, and zero-area faces. This is the materials domain's copy-validate-commit
discipline (`04_materials_texture_uv.md:57-66`) applied to geometry.

### State restoration ✓ (with one gap)

- `_editable_bmesh` (`handlers/.../_shared.py:112-128`) calls `sync_from_editmode`, wraps the block
  in `preserve_mode_and_selection()`, and frees the BMesh in `finally`.
- `preserve_mode_and_selection` (`bundled/addon/helpers.py:67-95`) snapshots active object, selected
  objects, and the active object's mode, and restores all three in `finally` — including on the
  failure path.
- Every evaluated-mesh read pairs `to_mesh()` with `to_mesh_clear()` in `finally`:
  `_world_bvh` (`handlers/.../_shared.py:150-158`), `inspect_retopology`
  (`handlers/.../target.py:281-289`), `_target_preflight` (`handlers/.../advanced.py:446,512-514`),
  `generate_retopology_lods` (`handlers/.../advanced.py:931-939`), `_evaluated_mesh_snapshot`
  (`handlers/.../quality.py:29-36`), `transfer_mesh_attributes`
  (`handlers/.../production.py:134,161-162`).
- `test_deformation` restores `scene.frame_current` in `finally`
  (`handlers/.../quality.py:513,615-616`) and inserts no keyframes — matching its docstring
  (`server/tools/retopology/quality.py:95-96`).
- `bake_retopology_maps` has the most careful `finally` in the domain
  (`handlers/.../production.py:507-520`): render engine, active UV layer, every touched material's
  `use_nodes` and active node, the injected image node, and a temporary bake material are all
  restored or removed.

**Gap — in-mesh element selection is never restored.** `edit_mesh`
(`bundled/addon/helpers.py:196-230`) calls `_select_geometry` (`:114-133`), which deselects and
re-selects mesh elements inside Edit Mode, and the surrounding `preserve_mode_and_selection`
restores only *object* selection. Four retopology tools enter Edit Mode this way —
`add_support_loops` (`handlers/.../construction.py:935`), `fill_boundary_quads` (`:612`),
`mesh_bridge` (`:548`), `unwrap_retopology_uvs` (`handlers/.../production.py:202`) — so an artist
who had a working element selection loses it. `AGENTS.md:58` states the requirement explicitly for
LoopTools ("restore the prior mode **and selection** afterward"); the object-level half is met, the
element-level half is not. **[needs live Blender]** to confirm the visible effect.

### Destructive-vs-non-destructive behaviour ✓ — the source mesh survives

This is the domain's best-executed property and it holds across all 27 tools:

- **Nothing in the domain ever mutates the high-poly source.** Every source read is through
  `evaluated_get(depsgraph)` + `to_mesh()` (`handlers/.../_shared.py:146-158`), i.e. a temporary
  evaluated copy. `generate_quadriflow_draft` runs the remesh on a standalone copy produced by
  `_evaluated_mesh_copy` (`handlers/.../advanced.py:64-75`, called at `:606`), never on `source`.
- **Modifiers stay live by default.** Mirror (`handlers/.../editing.py:538-551`), Shrinkwrap
  (`handlers/.../_shared.py:211-230`), Subdivision (`handlers/.../construction.py:963-967`),
  Surface Deform (`handlers/.../advanced.py:839-861`), and Data Transfer
  (`handlers/.../production.py:83-127`) are created or updated in place and never applied unless the
  caller passes `apply=True`. `_move_before_subdivision` (`handlers/.../_shared.py:188-191`) keeps
  Mirror/Shrinkwrap ahead of SUBSURF, and `validate_retopology` audits that ordering as a
  FAIL-severity check (`handlers/.../quality.py:389-411`).
- **Irreversible operations are gated by `confirm=True`**: `generate_retopology_lods`
  (`handlers/.../advanced.py:909-910`), `bake_retopology_maps`
  (`handlers/.../production.py:389-390`), and checkpoint RESTORE/DELETE
  (`handlers/.../target.py:502-503`). This matches `AGENTS.md:30`.
- **LOD generation applies Decimate/QuadriFlow only to fresh derivative copies**
  (`handlers/.../advanced.py:947` creates the copy; `:959`/`:961` mutate it); the master's modifier
  stack is untouched and the reply reports `evaluated_master_counts` separately (`:1026`).
- **Checkpoints are a real safety net.** CREATE copies object + mesh into a hidden, non-renderable,
  non-selectable collection (`handlers/.../target.py:436-450`); COMPARE is read-only and classified
  as such at dispatch (`server_core.py:252`).

**Transaction backstop.** Every mutating retopology command runs inside `mutation_transaction`
(`server_core.py:_run_handler`, `:2000-2018`), which on any `Exception` removes datablocks the
request created (identified by `session_uid`, `transaction.py:116-164`) and restores captured
target state (`transaction.py:363-381`). Fourteen retopology commands are additionally in
`_GEOMETRY_MUTATING_COMMANDS` (`server_core.py:1840-1853`), so their target's **mesh datablock** is
backed up and swapped back on failure. That list is correctly scoped: every retopology command that
edits an existing object's mesh is in it, and the ones that are absent
(`create_retopology_target`, `create_retopology_guides`, `create_surface_section`,
`create_bake_cage`, `fit_surface_primitive`, `generate_quadriflow_draft`,
`generate_retopology_lods`, `configure_retopology_symmetry`, `bind_surface_deformation`,
`bake_retopology_maps`) only create new datablocks or add modifiers, both of which the
transaction already covers (`transaction.py:413-417`).

This backstop is what makes two in-handler error messages honest rather than false.
`fill_boundary_quads` raises `"Grid Fill produced a non-quad face; the complete edit was rolled
back"` (`handlers/.../construction.py:618-619`) *after* the Edit-Mode block has already exited, and
`add_support_loops` raises `"Support loops would leave non-manifold edges"` (`:961-962`) after the
operator ran. Neither handler rolls anything back itself; both claims are true only because
`fill_boundary_quads` and `add_support_loops` are in `_GEOMETRY_MUTATING_COMMANDS`
(`server_core.py:1846`, `:1851`) and `Transaction.rollback()` restores the mesh. The coupling is
real but undocumented at the raise site — a future move of either command out of that frozenset
would silently turn both messages into lies.

### Operator `{'FINISHED'}` checking — mostly ✓, two holes

`_require_finished` (`handlers/.../_shared.py:261-263`) raises a `RuntimeError` naming the operation
and the returned status set. It is applied to every geometry operator the domain runs:

| Operator | Site | Checked |
|---|---|---|
| `mesh.bridge_edge_loops` | `handlers/.../construction.py:549-557` | ✓ (inline `"FINISHED" not in result`) |
| `mesh.fill_grid` | `handlers/.../construction.py:613-615` | ✓ (inline) |
| `mesh.offset_edge_loops_slide` | `handlers/.../construction.py:936-947` | ✓ |
| `object.quadriflow_remesh` | `handlers/.../advanced.py:108-120` | ✓ |
| `object.surfacedeform_bind` (bind) | `handlers/.../advanced.py:860-863` | ✓ + post-state `is_bound` assert |
| `object.surfacedeform_bind` (unbind) | `handlers/.../advanced.py:763-766` | ✓ + post-state assert |
| `object.modifier_apply` (LOD / DataTransfer materialize) | `handlers/.../advanced.py:436-441` | ✓ |
| `uv.unwrap` / `average_islands_scale` / `minimize_stretch` / `pack_islands` | `handlers/.../production.py:203-215` | ✓ (all four) |
| `object.bake` | `handlers/.../production.py:498` | ✓ + post-save `path.is_file()` assert (`:500-501`) |
| **`object.modifier_apply` via `configure_surface_projection(apply=True)`** | `handlers/.../editing.py:173` → `helpers.py:263-266` | **✗** |
| **`object.modifier_apply` via `transfer_mesh_attributes(apply=True)`** | `handlers/.../production.py:129` → `helpers.py:263-266` | **✗** |

The two unchecked paths are the ones an agent reaches by asking for a destructive bake-down.
`helpers.apply_modifier` discards the operator's return value, so a `{'CANCELLED'}` apply produces
`"applied": true` (`handlers/.../editing.py:178`, `handlers/.../production.py:168`) in an `ok: true`
envelope, with `"modifier": None` claiming the modifier is gone when it is still on the stack. That
is precisely the failure mode `AGENTS.md:38` prohibits ("Do not … return success after a failed
operation"), and the domain already owns the correct helper two files away.
**[needs live Blender]** to observe an actual CANCELLED apply; the omission itself is plain in the
source.

### Input validation and numeric bounds — thorough in the handler, divergent at the boundary

The handler-side validation vocabulary is good and used consistently: `_finite`
(`handlers/.../_shared.py:40-44`) rejects NaN/inf, `_positive` (`:47-52`) enforces sign, `_vector`
(`:55-61`) enforces arity, finiteness and non-zero-ness, `_ensure_indices` (`:89-109`) rejects
bools-as-ints, out-of-range indices, and non-list inputs while de-duplicating, and `_require_name`
(`:255-258`) rejects blank strings. Enums are re-validated in the handler even though the server
already types them (`handlers/.../editing.py:136-143`, `handlers/.../advanced.py:661-662`,
`handlers/.../production.py:187-188`). Vertex-count safety limits exist where combinatorics could
explode: 250,000 for `build_quad_patch` (`handlers/.../construction.py:210-211`) and
`extend_boundary` (`:361-362`), 8 LODs (`handlers/.../advanced.py:520-521`), 100 inspection vertices
(`handlers/.../target.py:277-278`), 2,500 triangles before UV-overlap is skipped and honestly
reported as skipped (`handlers/.../_shared.py:292-293`, surfaced as `overlap_check_skipped` at
`handlers/.../production.py:271`).

**But the advertised schema and the handler disagree in at least eight places.** Read from the
generated JSON schema of the registered tools versus the handler source:

| Parameter | Server schema | Handler | Result |
|---|---|---|---|
| `relax_topology.iterations` | `1..1000` (`server/.../editing.py:124`) | `1..100` (`handlers/.../editing.py:364-365`) | 101–1000 accepted then rejected |
| `inspect_retopology.adjacency_depth` | `0..20` (`server/.../target.py:81`) | `0..8` (`handlers/.../target.py:274-275`) | 9–20 accepted then rejected |
| `fit_surface_primitive.u_segments` | `3..1000` (`server/.../advanced.py:65`) | `≤256` (`handlers/.../advanced.py:667`) | 257–1000 accepted then rejected |
| `fit_surface_primitive.v_segments` | `1..1000` (`server/.../advanced.py:66`) | `≤256` (`handlers/.../advanced.py:668`) | same |
| `bake_retopology_maps.margin` | `≥0`, no ceiling (`server/.../production.py:163`) | `0..32767` (`handlers/.../production.py:422-423`) | same |
| `create_bake_cage.offset` | plain `float` (`server/.../production.py:119`) | `_positive(..., allow_zero=True)` (`handlers/.../production.py:297`) | negative (inward) cage accepted then rejected; the docstring never says the offset must be outward |
| `inspect_retopology.limit` | `1..1000` (`server/.../target.py:82`) | silently clamped to 500 (`handlers/.../target.py:28,348`) | **silent** short page |
| `validate_retopology.issue_limit` | `1..2000` (`server/.../quality.py:66`) | silently clamped to 1000 (`handlers/.../quality.py:216`) | **silent** truncation |
| `test_deformation.issue_limit` | `1..2000` (`server/.../quality.py:96`) | silently clamped to 1000 (`handlers/.../quality.py:502`) | **silent** truncation |
| `analyze_surface_conformity.worst_limit` | `1..1000` (`server/.../quality.py:24`) | silently clamped to 500 (`handlers/.../quality.py:83`) | **silent** truncation |
| `bake_retopology_maps.map_type` | 7-value `Literal` (`server/.../production.py:155`) | 11 keys incl. COMBINED/GLOSSY/SHADOW/UV (`handlers/.../production.py:396-408`) | 4 bake types unreachable from MCP |
| `bake_retopology_maps.width`/`height` | `1..16384` (`server/.../production.py:157-158`) | `1..32768` (`handlers/.../production.py:420-421`) | handler is looser; no error, just a narrower surface |

The first six turn a documented-valid input into a runtime error; the four silent clamps are worse
in an agent context, because the reply reports fewer records than asked for without saying the limit
was reduced. `04_materials_texture_uv.md` did not have to raise this because that domain's bounds
are not duplicated on both sides.

**Non-finite floats reach vertex coordinates.** `projection_offset` / `offset` are declared as bare
`float` with no `Field` constraint on seven tools, and Pydantic accepts `inf`/`NaN` for a bare
float. Verified directly against the registered tool's arg model: validating
`project_mesh_elements` with `offset = float("nan")` succeeds and the dumped value is still NaN. The
handler then uses it without `_finite`: `project_mesh_elements`
(`server/.../editing.py:66` → `handlers/.../editing.py:257`), `relax_topology`
(`server/.../editing.py:129` → `handlers/.../editing.py:402`), `redistribute_edge_loop`
(`server/.../editing.py:155` → `handlers/.../editing.py:493`), `build_quad_patch`
(`server/.../construction.py:164` → `handlers/.../construction.py:316-318`), `extend_boundary`
(`server/.../construction.py:196` → `handlers/.../construction.py:425`), `fill_boundary_quads`
(`server/.../construction.py:227` → `handlers/.../construction.py:625-627`), `add_support_loops`
(`server/.../construction.py:125` → `handlers/.../construction.py:956-958`). Each path ends in
`_nearest_projection`'s `hit + normal.normalized() * offset` (`handlers/.../_shared.py:166`) and then
`vertex.co = inverse @ projected` (`:178`), i.e. NaN written into the mesh. Four sibling tools *do*
validate the same parameter — `create_retopology_guides` (`handlers/.../construction.py:647`),
`create_surface_section` (`:768`), `fit_surface_primitive` (`handlers/.../advanced.py:669`),
`generate_retopology_lods` (`handlers/.../advanced.py:913`) — so this is an inconsistency, not a
policy. **[needs live Blender]** to confirm what a NaN coordinate does downstream; that it is
written is plain from the source.

### Failure surfacing to an agent ✓

- `_call` (`server/tools/retopology/_shared.py:16-21`) logs and re-raises every transport/Blender
  failure as `ToolError` prefixed with the command name — no domain tool can return a success
  envelope for a failed Blender call.
- Handler errors are specific and actionable, naming the offending value and the remedy: stale
  revision quotes both hashes and names the tool to call (`handlers/.../_shared.py:82-85`); index
  errors name the valid range (`:103`); `"UV map 'X' already exists; set replace_existing=True"`
  (`handlers/.../production.py:197`); `"The low-poly mesh has no UV map; run unwrap_retopology_uvs
  first"` (`handlers/.../production.py:425`); `"Modifier 'X' is already bound; UNBIND before
  changing [...]"` (`handlers/.../advanced.py:803-805`).
- `_target_preflight` (`handlers/.../advanced.py:443-511`) checks the four conditions Blender
  documents as blocking a Surface Deform bind *before* touching the modifier, and refuses with all
  of them named (`:500-510`) rather than letting the operator fail opaquely.
- Non-fatal notices ride the envelope correctly: handlers return a `warnings` list
  (`handlers/.../advanced.py:618-622`, `:945`, `:994-995`) and `ok()` lifts it into the envelope's
  own `warnings` and drops it from `data` (`server/tools/envelope.py:319-321`), so QuadriFlow's
  "this is a draft, not animation-ready" notice and any lost-data-layer report reach the client.
- `bind_surface_deformation` distinguishes "no change" from "changed" and the server tool maps it to
  an empty `changed_objects` (`server/.../advanced.py:126`), which is exactly the correction the
  prior audit asked of the ND tools (`AGENTS.md:183`).

### Reply-size budget — **not covered at all**

The envelope shortens over-budget replies (`server/tools/envelope.py:242-282`) and `_record_pages`
(`:87-119`) will shorten any list of two or more entries, including bare index lists. But the
measurement regime that sets and polices the ceilings has no retopology entries:
`scripts/measure_reply_sizes.py` contains no retopology command or tool name, and running it for
this bundle fails outright:

```
$ .venv/bin/python scripts/measure_reply_sizes.py retopology
refusing to measure: no representative arguments for tool 'generate_quadriflow_draft'
```

So none of these tools has a measured byte figure or an asserted ceiling. That matters because
several replies are unbounded by construction: `build_quad_patch` can return up to ~250,000
`created_vertex_indices` (`handlers/.../construction.py:210-211,327`);
`configure_retopology_symmetry` returns `unmatched_vertex_indices` with no cap
(`handlers/.../editing.py:552,580`), and for the canonical half-modelled mirror workflow *every*
base vertex is unmatched, because the check compares the base mesh against itself (`:566-571`)
rather than against the mirrored result; `project_mesh_elements` returns one entry per projected
vertex (`handlers/.../editing.py:277`); `relax_topology` returns `moved_vertex_indices` +
`locked_vertex_indices` (`handlers/.../editing.py:405-406`). Shortening will fire, but on a bare
index list `_pagination_names` finds nothing (`server/tools/envelope.py:147-155`), so the agent gets
`"rerun with a narrower scope to see the rest"` (`:213`) and silently loses the indices it needs to
continue — with no offset to resume from.

### Verification coverage — **the domain's largest gap**

- **No smoke script.** `tests/blender_*_smoke.py` currently matches 24 files; none is retopology.
  `AGENTS.md:109-112` is explicit: the four CI gates never touch Blender, changes under
  `bundled/addon/` are "unverified by all four gates", `just smoke` "is the only thing that
  exercises the real API", and it must be run "after touching an add-on handler". 4,658 lines of
  Blender-facing retopology handler code have never been exercised against a real Blender by
  anything in the repository.
- **Unit tests are almost entirely wiring.** `tests/server/tools/retopology/test_tools.py` is 342
  lines of forwarding assertions — parameters reach `send_command` (`:87`, `:204`),
  `changed_objects` echoes a name (`:163`, `:216`, `:251`, `:298`), `STALE_INDEX_WARNING` is
  attached (`:120`, `:233`). By the repo's own testing bar these defend nothing a plausible bug
  would break. Only two tests touch a handler at all: `tests/test_bake_cage_inputs.py` (55 lines,
  one missing-argument guard, `:46-47` and `:54-55`) and
  `test_addon_dispatch_advertises_all_phase_two_commands`
  (`tests/server/tools/retopology/test_tools.py:322`), a registry check.
- **Consequences, all unverifiable without Blender:** every "created elements are the trailing index
  range" computation assumes Blender appends new elements at the end of the arrays — `mesh_bridge`
  (`handlers/.../construction.py:562-564`), `fill_boundary_quads` (`:617,620,632`),
  `add_support_loops` (`:949-951`), `reroute_topology` (`handlers/.../editing.py:340-342`). So does
  the `bpy.ops.mesh.offset_edge_loops_slide` macro-argument spelling (`MESH_OT_offset_edge_loops` /
  `TRANSFORM_OT_edge_slide` nested dicts, `handlers/.../construction.py:936-946`), the QuadriFlow
  keyword set (`handlers/.../advanced.py:108-119`), and the Mirror bisect-flip semantics
  (`handlers/.../editing.py:542-546`, whose own comment cites the manual rather than a test). Each
  is plausible; none is verified. **[needs live Blender]**

### Logic defect: the seam-band check is inert at its own defaults

`configure_retopology_symmetry` reports `seam_vertices_outside_tolerance` under
`abs(coordinate[axis]) <= merge_tolerance and abs(coordinate[axis]) > symmetry_tolerance`
(`handlers/.../editing.py:572`). Both parameters default to `0.001`
(`handlers/.../editing.py:512,515`; server defaults `server/.../editing.py:181,184`), and no value
can be simultaneously `<= 0.001` and `> 0.001`. At default settings the list is always empty. The
server docstring advertises the capability unconditionally — "seam vertices outside
`symmetry_tolerance`, so an agent can repair center-seam damage before continuing"
(`server/.../editing.py:193-195`) — so an agent reading an empty list will conclude the seam is
clean. The band is only non-degenerate when the caller happens to pass
`symmetry_tolerance < merge_tolerance`, which nothing documents.

### Checkpoint RESTORE silently drops modifier settings it cannot copy

RESTORE rebuilds the modifier stack by iterating `bl_rna.properties` and assigning each under
`contextlib.suppress(Exception)` (`handlers/.../target.py:526-530`). Any property that refuses
assignment is skipped without a warning, so a restored stack can differ from the checkpointed one
while the reply reports `"restored": true` (`:559`). The same pattern applies to vertex-group
weights (`:536-538`). Contrast the equally-suppressed material restore in `bake_texture_map`, which
`04_materials_texture_uv.md:71` flags as the one caveat in an otherwise clean restoration story —
the same caveat applies here, with the mesh's modifier stack as the payload. The reply carries
`modifier_order` (`:562`) but no per-property diff, so the loss is undetectable from the reply
alone. **[needs live Blender]** to enumerate which properties actually refuse assignment.

---

## Capability Assessment: what production retopology work is possible

### Possible today

| Workflow | Tools | Evidence |
|---|---|---|
| **Automatic quad remesh** | `generate_quadriflow_draft` | `server/.../advanced.py:20-54`; RATIO/EDGE/FACES sizing, deterministic seed, data-layer before/after diff (`handlers/.../advanced.py:607-611`), auto-validated, explicitly labelled a draft (`handlers/.../advanced.py:618-620`) |
| **Decimation** | `generate_retopology_lods(method="DECIMATE")` | `handlers/.../advanced.py:949-959`; COLLAPSE, symmetry, vertex-group weighting |
| **Shrinkwrap projection** | `configure_surface_projection` (live), `project_mesh_elements` (one-shot) | `server/.../editing.py:18-89`; all four wrap methods, five wrap modes, per-axis PROJECT |
| **Manual guide-based retopo** | `create_retopology_guides`, `create_surface_section`, `build_quad_patch`, `extend_boundary`, `fill_boundary_quads`, `add_support_loops` | `server/.../construction.py:16-244`; Coons patches, four boundary-growth modes, quad-only Grid Fill |
| **Edge-flow repair** | `reroute_topology`, `relax_topology`, `redistribute_edge_loop` | `server/.../editing.py:91-169` |
| **Symmetry** | `configure_retopology_symmetry`, `create_retopology_target(add_mirror=True)` | `server/.../editing.py:171-198`, `server/.../target.py:27` |
| **UV-preserving transfer** | `transfer_mesh_attributes` | `server/.../production.py:26-83`; 10 data types incl. UVs, custom normals, creases, and a manual nearest-face MATERIAL_INDICES path Data Transfer does not support (`handlers/.../production.py:130-162`) |
| **Bake-map handoff** | `create_bake_cage` → `unwrap_retopology_uvs` → `bake_retopology_maps` | `server/.../production.py:85-184`; cage topology-identity assert (`handlers/.../production.py:430-436`), enclosure validation (`:325-352`), 7 map types, normal swizzle |
| **Deformation QA** | `test_deformation` | `server/.../quality.py:85-113`; multi-frame stretch/area/volume/flip/self-intersection |
| **Conformity measurement** | `analyze_surface_conformity`, `validate_retopology` | `server/.../quality.py:15-83`; p50–p99 statistics, signed offsets, heat map |
| **Recoverable checkpoints** | `manage_retopology_checkpoint` | `handlers/.../target.py:400-563` |
| **LOD chains** | `generate_retopology_lods` | `server/.../advanced.py:130-178`; strictly-decreasing ratios enforced (`handlers/.../advanced.py:533-534`) |
| **Primitive fitting** | `fit_surface_primitive` | `server/.../advanced.py:56-93`; PCA plane, axis-disambiguated cylinder/cone, algebraic sphere, all-quad cube-sphere |

This is, by capability, the most complete retopology surface of any domain in this audit set —
notably more complete than the compositing surface (`03_rendering_compositing.md:15-16`) or the
material preset library (`04_materials_texture_uv.md:196`).

### Missing

- **No voxel remesh inside the domain.** `mesh_remesh(object_name, voxel_size)` exists
  (`server/tools/mesh.py:419`) but is in `core-authoring`, so a `shot,retopology` process has
  neither it nor `mesh_bridge` (verified empirically above). Only the `asset` mode
  (`bundles.py:86`) happens to pull both in.
- **No standalone decimate tool.** Decimation is reachable only through `generate_retopology_lods`,
  which requires `confirm=True`, a strictly-decreasing ratio list, and materializes new objects. A
  caller who wants one 50% derivative must accept the LOD machinery, or reach for
  `manage_modifiers` with a `DECIMATE` type (`server/tools/scene.py:118,416`) in a different bundle.
- **No polybuild / free vertex authoring.** `create_retopology_target(SINGLE_VERTEX)` makes one
  vertex (`server/.../target.py:14`) and then nothing in the surface can add a second one:
  `build_quad_patch` needs four corners, `extend_boundary` needs an existing boundary,
  `fill_boundary_quads` needs a closed hole. There is no "add vertex at point" or "make face from
  these vertices". The SINGLE_VERTEX starter is a dead end.
- **No edge-loop / edge-ring traversal.** `add_support_loops`, `redistribute_edge_loop`,
  `extend_boundary`, `fill_boundary_quads`, and `mesh_bridge` all demand *ordered* index chains, and
  `inspect_retopology` returns ordered vertices only for **boundary** loops
  (`handlers/.../target.py:330-337`). For interior loops the agent gets per-vertex adjacency sets
  (`:319-326`, capped at 100 vertices, depth ≤8) with no edge→(v0,v1) mapping, and must reconstruct
  chains itself by cross-referencing the separate `get_mesh_data(element_type="edges")` tool
  (`server/tools/viewport.py:150-153`, core bundle per `bundles.py:25`). This is the single biggest
  agentability tax in the domain: the tools that do the interesting work are the hardest to call.
- **No bake of a full map set.** `bake_retopology_maps` bakes one `map_type` per call
  (`server/.../production.py:155`) with `confirm=True` each time; a normal+AO+position set is three
  confirmed, synchronous calls. There is also no progress reporting for a long bake.
- **No third-party retopology add-on integration** (RetopoFlow, Quad Remesher, Instant Meshes),
  where `AGENTS.md:44-64` establishes the pattern for ND/LoopTools/EdgeFlow. `relax_topology`'s
  docstring notes it deliberately avoids LoopTools (`server/.../editing.py:139`), which is a
  defensible choice, but it means LoopTools' own topology-regularisation operators (`AGENTS.md:57`)
  are unreachable from this domain.
- **No curvature/thickness-driven density control.** Guides carry caller-declared roles
  (`server/.../construction.py:34` — the tool "never infers anatomy", which is honest) but nothing
  derives target density from source curvature.

---

## Architecture Assessment

### Strengths
1. **Revision-checksummed indices** (`handlers/.../_shared.py:64-86`) — a genuine solution to
   stale-index reuse, not a warning. Mandatory where correctness depends on it
   (`handlers/.../advanced.py:657-659`). ✓✓
2. **Non-destructive by construction** — the source mesh is only ever read through an evaluated
   temporary copy; modifiers stay live; irreversible work needs `confirm=True`. ✓✓
3. **Dry-run before commit** for the riskiest local edit (`handlers/.../editing.py:298-334`). ✓
4. **Transaction-backed rollback** for all 14 geometry-mutating commands, with a correctly-scoped
   `_GEOMETRY_MUTATING_COMMANDS` set (`server_core.py:1840-1853`). ✓
5. **Honest reporting** — `lost_data_layers` from QuadriFlow (`handlers/.../advanced.py:611,622`),
   `overlap_check_skipped` when the UV check bails (`handlers/.../production.py:271`),
   `failed_projection_vertex_indices` everywhere, `"draft, not animation-ready"` warnings,
   `topology_identical` on cages (`handlers/.../production.py:360`). The domain consistently refuses
   to claim more than it measured.
6. **Profile-driven validation** (`handlers/.../quality.py:165-206`) with 14 checks and
   caller-overridable named thresholds, rather than a blanket all-quads rule.
7. **No arbitrary-code escape hatch** — unlike `execute_blender_code` (`AGENTS.md:172`), every
   mutation here is a named validated operation.

### Weaknesses
1. **4,658 lines of add-on handler code with zero real-Blender coverage.** No smoke script, and
   every operator spelling, macro-argument shape, and created-index-range assumption is unverified. ⚠️⚠️
2. **Schema/handler bound divergence in ≥8 parameters, plus four silent clamps**, plus non-finite
   floats reaching `vertex.co` on seven tools (verified at the server boundary). ⚠️⚠️
3. **Two `apply=True` paths skip the operator-result check** (`handlers/.../editing.py:173`,
   `handlers/.../production.py:129`) while the correct helper exists in the same package
   (`handlers/.../advanced.py:436-441`). ⚠️
4. **Reply sizes unmeasured and partly unbounded** — no `measure_reply_sizes.py` entries, and
   several replies return one index per element with no page contract. ⚠️
5. **`configure_retopology_symmetry`'s seam-band check is dead at its own defaults**
   (`handlers/.../editing.py:572`), and its `unmatched_vertex_indices` flags the entire mesh for the
   normal half-modelled mirror workflow. ⚠️
6. **`mesh_bridge` is in the wrong bundle** — retopology handler, `core-authoring` tool, asserted as
   a retopology tool by a test that only passes because of an incidental import
   (`tests/server/tools/retopology/test_tools.py:22,55-58,139`). ⚠️
7. **Index-chain inputs with no traversal helper** — the surface's hardest-to-satisfy precondition
   has no tool to satisfy it.
8. **Edit-Mode element selection is not restored** (`bundled/addon/helpers.py:196-230`), against
   `AGENTS.md:58`.

---

## Recommended Immediate Actions

1. **Add `tests/blender_retopology_smoke.py`** (High). One headless pass — create target →
   build_quad_patch → extend_boundary → fill_boundary_quads → add_support_loops → project →
   validate → unwrap → cage → bake — would settle every unverified operator spelling and
   created-index-range assumption at once, and `AGENTS.md:111` already requires it for add-on
   handler changes. This is the highest-value single action in the domain.
2. **Reconcile the server schema with the handler bounds** (High). Either tighten the `Field`
   constraints to the handler's real limits (`iterations ≤ 100`, `adjacency_depth ≤ 8`,
   `u/v_segments ≤ 256`, `margin ≤ 32767`, `offset ≥ 0` on `create_bake_cage`) or widen the handler.
   Turn the four silent clamps (`inspect_retopology.limit`, both `issue_limit`s, `worst_limit`) into
   either honest ceilings in the schema or an envelope warning when the clamp fires.
3. **Route both `apply=True` paths through `_require_finished`** (High). Replace
   `helpers.apply_modifier` at `handlers/retopology/editing.py:173` and
   `handlers/retopology/production.py:129` with the domain's own `_apply_modifier_checked`
   (`handlers/.../advanced.py:436-441`), or add the check to the shared helper — the latter fixes
   every other domain that calls it too.
4. **Validate `projection_offset` / `offset` with `_finite`** on the seven tools that skip it
   (`handlers/.../editing.py:192,357,420`; `handlers/.../construction.py:199,346,577,906`), matching
   the four siblings that already do.
5. **Fix the seam-band condition** at `handlers/retopology/editing.py:572` so it reports something
   at default tolerances, and make `unmatched_vertex_indices` meaningful for a half-modelled mirror
   target (compare against the mirrored half, or bound and label the list).
6. **Add retopology payloads to `scripts/measure_reply_sizes.py`** and give the unbounded index
   lists (`created_vertex_indices`, `unmatched_vertex_indices`, `projected_vertex_indices`,
   `moved_vertex_indices`) a real `offset`/`limit`/`truncated` contract so shortening can hand back
   a resume point instead of "rerun with a narrower scope".
7. **Move `mesh_bridge` into the `retopology` bundle** (`bundles.py:51`) or remove it from
   `RETOPOLOGY_TOOL_NAMES` (`tests/server/tools/retopology/test_tools.py:22`) — the handler, the
   tool, and the test currently disagree about which domain owns it.
8. **Add an edge-loop / edge-ring traversal result to `inspect_retopology`** (or a small
   `trace_edge_loop` tool), so the ordered-chain inputs that five tools require can be produced
   without cross-referencing `get_mesh_data`.
9. **Report checkpoint RESTORE property losses** instead of suppressing them
   (`handlers/.../target.py:526-530`), so `"restored": true` means what it says.
10. **Replace the wiring tests** in `tests/server/tools/retopology/test_tools.py` with handler-level
    tests against the fake `bpy` for the things a plausible bug breaks: revision rejection,
    index-range rejection, the reroute dry-run's boundary guard, the cage topology-identity assert,
    the LOD strictly-decreasing-ratio rule.

---

## Summary: Retopology Domain Score

| Category | Score | Evidence |
|----------|-------|----------|
| **Tool Completeness** | 9/10 | 27 tools spanning drafts, guides, construction, edit-flow, projection, symmetry, QA, LODs, and full bake handoff. Gaps: no polybuild, no in-bundle voxel remesh/bridge, no standalone decimate, single-map bakes. |
| **Reliability** | 5/10 | Revision gating, dry-run commit, and transaction rollback are excellent — but zero real-Blender verification of 4,658 handler lines, two unchecked `apply` paths, and a dead seam-band check. |
| **Granularity** | 9/10 | Six clean topic modules, no duplicated logic, cross-domain reuse of the bake and UV-overlap paths. One misplaced tool (`mesh_bridge`). |
| **Validation** | 6/10 | Handler-side validation is thorough and consistent; the server/handler boundary is not. Eight bound divergences, four silent clamps, non-finite floats reaching `vertex.co`. |
| **Non-destructiveness** | 10/10 | The source mesh provably survives every one of the 27 tools; modifiers stay live; irreversible work is `confirm`-gated; checkpoints exist. |
| **Agentability** | 6/10 | Docstrings are unusually precise about coordinate space and reversibility, and errors are actionable — but five tools demand ordered index chains the surface gives no way to build, and unbounded index replies can be silently shortened without a resume offset. |
| **Verification** | 2/10 | No smoke script. 342 lines of forwarding tests plus one input guard for the whole domain. No reply-byte measurement. |

## Score estimate

**Score estimate**: 7/10

The retopology domain is the strongest *designed* surface in this audit set. Its revision-checksum
contract (`handlers/retopology/_shared.py:64-86`) is a better answer to stale indices than anything
else in the repository; its non-destructiveness is not a convention but a property that holds across
all 27 tools by construction; its dry-run-then-commit reroute (`handlers/.../editing.py:298-334`)
and its confirm-gated irreversible operations meet `AGENTS.md:24,30,38` more closely than the
materials or rendering domains do. Capability coverage is broad enough for real production
retopology: automatic draft, manual guide-driven build, projection, symmetry, QA, UV, cage, bake,
LOD.

It is not an 8 or 9 because the evidence that any of it *works* is missing. 4,658 lines of
Blender-facing code — operator macro arguments, QuadriFlow keywords, Mirror bisect semantics, and
every "new elements land at the end of the index array" assumption — have never run against a real
Blender in this repository, and `AGENTS.md:109-112` says in as many words that the four CI gates
cannot see them. On top of that sit three concrete, source-visible defects: the server schema
promises ranges the handler rejects (and passes NaN through to vertex coordinates), two destructive
`apply=True` paths can report success after a cancelled operator, and one advertised diagnostic is
mathematically dead at its own defaults. None is architectural; all are cheap to fix. The design
earns the 7; the verification debt is what caps it.

## Rubric mapping (A–J, per `.audit_tmp/COMPREHENSIVE_AUDIT_FINDINGS.md:71-152`)

| Cat. | Weight | Direction | Retopology evidence |
|---|---|---|---|
| **A. Architecture & Abstraction** | 15 | **↑ raises** | Revision-gated indices (`handlers/.../_shared.py:64-86`), dry-run-then-commit (`handlers/.../editing.py:298-334`), six clean topic modules with shared helpers and no duplication, transaction integration correctly scoped (`server_core.py:1840-1853`). The strongest architecture of any domain audited; offsets the RNA-query design flaw the Materials slice found. |
| **B. Tool Quality & Redundancy** | 15 | **→ neutral** | 27 tools with essentially no functional overlap and no free-form bpy escape hatch — better than Animation's "3 ways to keyframe". But `mesh_bridge` is in the wrong bundle (`bundles.py:51` vs `server/tools/mesh.py:224`), four `bake_retopology_maps` map types are unreachable (`server/.../production.py:155` vs `handlers/.../production.py:396-408`), and the SINGLE_VERTEX starter (`server/.../target.py:14`) leads nowhere. |
| **C. Scene & Asset Pipeline** | 10 | **↑ raises** | Completes the asset pipeline the Materials slice left open: high→low handoff via `transfer_mesh_attributes` (10 data types), `unwrap_retopology_uvs`, `create_bake_cage`, `bake_retopology_maps`, and `generate_retopology_lods`. Collection hygiene throughout (`_named_collection`, `handlers/.../_shared.py:245-252`) per `AGENTS.md:25`. |
| **D. Lighting & Camera** | 10 | **– n/a** | No retopology tool touches lights or cameras. |
| **E. Animation / Rigging / Simulation** | 10 | **↑ slightly raises** | `test_deformation` (`server/.../quality.py:85-113`) evaluates the animated modifier/armature stack across explicit frames and restores the playhead in `finally` (`handlers/.../quality.py:615-616`); `validate_retopology`'s skin-weight check (`handlers/.../quality.py:369-388`) audits deform-group normalization. Real deformation QA that the Animation slice's 29 tools did not provide. |
| **F. Rendering** | 15 | **↓ slightly lowers** | `bake_retopology_maps` forces `scene.render.engine = "CYCLES"` (`handlers/.../production.py:481`) and restores the prior engine in `finally` (`:508`) — but it never consults `runtime_engine()`, so it inherits the Cycles-availability exposure the Materials slice raised as CRITICAL (`04_materials_texture_uv.md:84-106`) without even the broken guard. Bakes are synchronous, one map per confirmed call, with no progress reporting. |
| **G. Compositing** | 5 | **– n/a** | No compositor interaction. |
| **H. Validation & Reliability** | 10 | **↓ lowers** | The heaviest negative. Zero real-Blender smoke coverage of 4,658 handler lines against `AGENTS.md:109-112`; two unchecked `modifier_apply` paths against `AGENTS.md:38`; ≥8 schema/handler bound divergences plus four silent clamps; NaN reaching `vertex.co` on seven tools; a dead seam-band condition (`handlers/.../editing.py:572`); suppressed checkpoint-restore property losses (`handlers/.../target.py:526-530`). Partly offset by `validate_retopology`'s 14 checks and the transaction backstop. |
| **I. Agentability & NL** | 5 | **→ neutral** | Docstrings state coordinate space, reversibility, and index-stability explicitly and the domain refuses to over-claim (`server/.../construction.py:34` "never infers anatomy"; `server/.../advanced.py:49` "always identified as a draft"). Against that: five tools need ordered index chains with no traversal helper, and shortened index replies give no resume offset (`server/tools/envelope.py:213`). Net wash. |
| **J. Production Completeness** | 5 | **↑ raises** | Closes the single largest capability hole the synthesis identified as unaudited (`AUDIT_SYNTHESIS.md:43`): a full high→low production path with cages, bakes, LODs, checkpoints, and profile-based acceptance criteria. Missing pieces (polybuild, third-party add-ons, multi-map bake) are conveniences, not blockers. |

**Net effect on the 100-point total**: A, C, E, J up; F and H down, with H down the most; B, D, G, I
neutral or not applicable. The domain adds more capability than it costs in reliability, but its
verification debt is the largest single unmitigated risk found in any slice so far.
