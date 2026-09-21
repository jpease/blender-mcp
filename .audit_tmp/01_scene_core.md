# Scene/Core/Modeling/ND/Asset-Import Tools Audit

**Domain**: Scene initialization, geometry creation, mesh editing, modeling, ND workflow, asset import (PolyHaven/Sketchfab)  
**Slice Size**: 7 server tools files (6 at audit time; `scene_authoring.py` was split out of `scene.py`), 5 addon handler mixins  
**Rubric Sections Evaluated**: 1 (Scene Initialization), 3 (Asset Import & Download), 4 (Production Workflow Architecture)  
**Audit Date**: 2026-09-05  
**Blender Target**: 5.2.1 LTS (5.1+ API only)

---
> **Re-verification notice (2026-09-21).** This slice was written against the 2026-09-05 tree and
> has been re-verified claim by claim against the current tree. See **Verification pass
> (2026-09-21)** and **Score estimate** at the end of this file. Sections 1–11 are the original
> findings, with the score-bearing ones corrected in place and marked `⚠ STALE`. Parameter lists in
> §1 have drifted substantially — the `read_only` parameter shown throughout §1 exists on no tool in
> this domain, and `create_geometry_object`, `remove_scene_objects`, `reset_scene`,
> `duplicate_or_instance_objects`, `manage_scene_collections`, `manage_object_hierarchy`,
> `validate_scene`, `mesh_remesh`, `add_radial_array_modifier` and six `nd_*` tools have different
> signatures now. The verification pass lists each. Treat any §1–§11 claim it does not mark
> VERIFIED as unre-verified.

## 1. Tool Inventory & Coverage

### 1.1 Scene Composition (scene.py: 10 tools, 777 LOC)

| Tool | Purpose | Params | Reliability | Status |
|------|---------|--------|-------------|--------|
| `create_geometry_object` | Declarative typed geometry (mesh, curve, text, metaball, lattice, pointcloud, curves, greasepencil, volume) from pydantic specs | object_name, collection_name, geometry (GeometrySpec union), transform, material_slots, read_only | **HIGH** - full type validation, per-geometry schema, transform/material validation, dry_run optional | ✓ |
| `set_object_transform` | Patch transforms (location/rotation/scale or matrix) with space choice (LOCAL/WORLD) | object_name, transform (TransformPatch), space, read_only | **HIGH** - validates rotation/scale non-degeneracy, matrix 4x4, mutually-exclusive representation checks | ✓ |
| `manage_object_hierarchy` | Reparent/unparent with matrix preservation | object_names, parent_name, keep_transform, read_only | **MEDIUM** - reparents but no explicit world-transform preservation in code | ⚠ |
| `manage_scene_collections` | Create/move/link collections and objects | action (CREATE/MOVE/LINK), collection_name, target_collection, object_names, read_only | **MEDIUM** - action-based dispatcher, no rollback on partial moves | ⚠ |
| `manage_object_constraints` | Add/update/remove constraints (16 types: COPY_*, TRACK_*, LIMIT_*, STRETCH_TO, SHRINKWRAP) | action (ADD/UPDATE/REMOVE), object_name, constraint_spec or constraint_name, read_only | **MEDIUM** - whitelist-enforced, 16 constraint types supported, no influence/subtarget validation | ⚠ |
| `manage_modifiers` | Add/update/remove/reorder modifiers (32 types: ARRAY, BEVEL, BOOLEAN, ...) with typed settings | action, object_name, modifier_spec or modifier_name, priority, read_only | **MEDIUM** - pydantic ModifierSpec validates type + settings, but settings dict is **opaque**; no per-modifier setting validation | ⚠ |
| `duplicate_or_instance_objects` | Create linked instances or duplicates with optional transforms | object_names, instance_count, transforms, collection_name, linked, read_only | **HIGH** - supports pagination of transforms, explicit linked flag, collision-free naming | ✓ |
| `remove_scene_objects` | Delete objects by name with optional recursive option | object_names, recursive, read_only | **HIGH** - explicit object list, no bulk-selection deletion, recursive flag | ✓ |
| `reset_scene` | Clear all objects, restore defaults (units, gravity, playback) | preserve_world, preserve_camera, preserve_lights, read_only | **MEDIUM** - preserves world, camera, lights if requested; but no explicit undo/rollback | ⚠ |
| `validate_scene` | Inspect scene for production-readiness issues (camera, lights, frame range, scale, degenerate geometry, dirty caches) | active_domains (list of scene, camera, lighting, pbr, cloth, liquid) | **HIGH** - comprehensive checks (camera validity, light presence, frame range, unapplied scale, degenerate faces, cloth/rigbody cache status) | ✓ |

**Summary**: 10 tools covering core scene composition, transformation, hierarchy, constraints, modifiers, duplication, deletion, reset, validation. Strongest areas: geometry type unions, validation comprehensiveness, explicit naming/collision-safety. Weakest: modifier settings opaqueness, no per-domain rollback on partial collection moves.

---

### 1.2 Mesh Editing (mesh.py: 13 tools, 579 LOC)

| Tool | Purpose | Params | Reliability | Status |
|------|---------|--------|-------------|--------|
| `create_primitive_object` | Create CUBE, SPHERE, CYLINDER, CONE, TORUS, PLANE, CURVE with optional dimensions override | primitive_type, name, location, rotation, size, dimensions, purpose | **HIGH** - 7 primitives, dimensions override for footprint consistency, blockout tagging | ✓ |
| `mesh_extrude` | Extrude mesh faces by vector offset | object_name, offset (x,y,z), face_indices (optional) | **MEDIUM** - uses bpy.ops.mesh.extrude_region_move, validates FINISHED; but no pre-selection state restoration doc | ⚠ |
| `mesh_inset` | Inset faces by thickness/depth | object_name, thickness, depth, face_indices | **MEDIUM** - bpy.ops.mesh.inset; validates FINISHED | ⚠ |
| `mesh_bevel` | Bevel edges/vertices with offset/segments | object_name, offset, segments, affect (EDGES/VERTS), edge_indices, vertex_indices | **MEDIUM** - affects EDGES or VERTS; validates FINISHED; no harden_normals or angle_limit | ⚠ |
| `mesh_bridge` | Bridge two edge loops (deprecated edge_indices param, preferred: loop selection) | object_name, cuts, interpolation, smoothness, twist_offset, expected_revision (stale-check), edge_indices or loop indices | **MEDIUM** - bpy.ops.mesh.bridge_edge_loops; edge_indices deprecated; no explicit preview/cancel | ⚠ |
| `mesh_symmetrize` | Mirror geometry across 6 axes (NEGATIVE_X, POSITIVE_X, NEGATIVE_Y, ...) | object_name, direction | **MEDIUM** - bpy.ops.mesh.symmetrize; validates FINISHED; no threshold/merge tolerance | ⚠ |
| `mesh_boolean` | Boolean operation (UNION/DIFFERENCE/INTERSECT) with optional cutter retention | object_name, cutter_name, mode, keep_cutter | **HIGH** - explicit cutter retention, validates FINISHED, handles applied modifier result | ✓ |
| `mesh_subdivide` | Subdivide mesh or selected faces | object_name, cuts, face_indices | **MEDIUM** - bpy.ops.mesh.subdivide; no fractal/quad_corner_type options | ⚠ |
| `mesh_remesh` | Voxel remesh | object_name, voxel_size, smoothness (0-1), mode (SMOOTH/SHARP), apply | **MEDIUM** - calls bpy.ops.object.voxel_remesh; mode param not validated against operator capabilities | ⚠ |
| `mesh_solidify` | Thicken surface (solidify modifier, optionally applied) | object_name, thickness, apply | **MEDIUM** - applies modifier if requested; validates apply result | ⚠ |
| `clear_materials` | Strip all material slots from object | object_name | **HIGH** - idempotent, clear operation | ✓ |
| `clear_vertex_groups` | Strip all vertex groups from object | object_name | **HIGH** - idempotent, clear operation | ✓ |
| `clear_edge_marks` | Clear seam/sharp/crease/bevel marks from all edges | object_name | **HIGH** - idempotent, comprehensive mark cleanup | ✓ |

**Summary**: 13 tools covering core mesh ops (primitives, extrusion, inset, bevel, bridge, symmetry, boolean, subdivide, remesh, solidify, material/group/mark cleanup). Strongest: boolean with cutter retention, primitive dimensions consistency. Weakest: operator validation (no pre-checks for mode/threshold), edit-mode state restoration, no fractional subdivide options.

---

### 1.3 Model Transform & Array (model.py: 3 tools, 162 LOC)

| Tool | Purpose | Params | Reliability | Status |
|------|---------|--------|-------------|--------|
| `copy_object_transform` | Align object to reference (location/rotation/scale) in WORLD or LOCAL space | object_name, reference_object_name, match_location, match_rotation, match_scale, space (LOCAL/WORLD) | **HIGH** - matrix_world decomposition for WORLD space, explicit space choice, full quat/euler handling | ✓ |
| `add_radial_array_modifier` | Create radial array around pivot (object/location), with optional apply | object_name, count, radius, height_offset, pivot_object, pivot_location, apply | **MEDIUM** - ARRAY modifier with object offset; validates pivot; but no REPEAT_Y/Z or merge settings | ⚠ |
| `sync_data_name` | Batch rename data-blocks (object.data) to match object names | object_names | **HIGH** - idempotent, safe batch rename, no collision risk (uses object names as truth) | ✓ |

**Summary**: 3 specialized tools for transform operations. High reliability on copy_object_transform (world-space handling). Radial array lacks repeat/merge config.

---

### 1.4 ND (Non-Destructive) Toolkit (nd.py: 10 tools, 408 LOC)

| Tool | Purpose | Params | Reliability | Status |
|------|---------|--------|-------------|--------|
| `nd_boolean` | ND non-destructive boolean (live modifier, cutter wireframed + parented) | object_name, cutter_object_name, mode (UNION/DIFFERENCE/INTERSECT) | **MEDIUM** - wraps nd_call("bool_vanilla"); validates INVOKE_DEFAULT; cutter becomes utility but no undo on cancel | ⚠ |
| `nd_mark_as_util` | Mark objects as ND utility (wireframe display, render-hidden); optionally reparent | object_names, unmark, parent_to | **MEDIUM** - reparents while preserving world matrix; but no explicit undo mechanism on failure | ⚠ |
| `nd_clean_utils` | **DESTRUCTIVE**: Remove all ND utility objects from scene | none | **CRITICAL** - No dry-run flag; hardcoded destructive; no rollback; global operation | 🔴 |
| `nd_create_id_material` | Create material for single object with diffuse ID color | object_name, diffuse_color (RGB), color_space | **MEDIUM** - creates material slot + material; no validation of diffuse_color range | ⚠ |
| `nd_bulk_create_id_materials` | Batch ID materials (one per object, random distinct colors) | object_names, color_space | **MEDIUM** - one material per object, random RGB; no seed control | ⚠ |
| `nd_set_lod_suffix` | Rename objects with LOD suffixes (_high, _low) | object_names, lod_level (HIGH/LOW) | **MEDIUM** - simple rename; no validation that rename succeeds or collides | ⚠ |
| `nd_single_vertex` | Create sketch object (single vertex on origin) | object_name | **HIGH** - creates MESH with one vertex; simple, deterministic | ✓ |
| `nd_apply_modifiers` | Apply all modifiers in REGULAR mode (vs. non-destructive) | object_names | **MEDIUM** - applies via modifier_result helper; no selective apply or mode choice | ⚠ |
| `nd_pulse_viewport_toggle` | Toggle ND viewport overlay (CLEAR_VIEW/CUSTOM_VIEW/UTILS) | action | **LOW** - **NOT idempotent**; toggles state; side effect on viewport display; risky in automation | 🔴 |
| `nd_capture_utils` | Select/display all ND utility objects (for viewing/removal) | action (SELECT/DISPLAY) | **MEDIUM** - selects or displays utilities; but SELECT overwrites prior selection without saving state | ⚠ |

**Summary**: 10 ND tools providing non-destructive boolean, utility marking, ID materials, LOD suffixes, sketch creation, modifier apply, and viewport management. **Critical issues**: `nd_clean_utils` is destructive with no undo/dry-run, `nd_pulse_viewport_toggle` is not idempotent (violates production contract). Moderate issue: `nd_capture_utils` overwrites selection without restoration.

---

### 1.5 PolyHaven Asset Library (polyhaven.py: 4 tools, 253 LOC)

| Tool | Purpose | Params | Reliability | Status |
|------|---------|--------|-------------|--------|
| `get_polyhaven_categories` | List categories for asset_type (hdris/textures/models/all) | asset_type | **HIGH** - checks enablement status, validates asset_type, error handling | ✓ |
| `list_polyhaven_assets` | Browse asset catalog with pagination & category filter | asset_type, categories (comma-sep), limit (1-100), offset | **HIGH** - paginated, deterministic ordering by asset ID, explicit category filter | ✓ |
| `import_polyhaven_asset` | Download and import asset by ID (resolution/format options per type) | asset_id, asset_type, resolution (for textures/models), format (for textures) | **MEDIUM** - async download, network-dependent; no validation of imported object/material structure | ⚠ |
| `apply_polyhaven_texture` | Apply imported texture to material slot | asset_id, material_slot_index, replacement_policy (APPEND/REPLACE_SLOT/REPLACE_ALL), confirm_replace_all | **MEDIUM** - replacement_policy enforcement; but no prior material validation | ⚠ |

**Summary**: 4 tools for Polyhaven integration. Strong: categorical browsing, pagination, asset-type handling. Weak: no pre-download validation of object/material structure, no explicit collision detection on multi-texture replacement.

---

### 1.6 Sketchfab Asset Library (sketchfab.py: 3 tools, 207 LOC)

| Tool | Purpose | Params | Reliability | Status |
|------|---------|--------|-------------|--------|
| `search_sketchfab_models` | Search models by query, optionally filter by category/downloadability, cursor pagination | query (min_length=1), categories (comma-sep), count (1-100), downloadable (bool), cursor | **HIGH** - paginated cursor, category filter, downloadable flag, explicit count limit | ✓ |
| `get_sketchfab_model_preview` | Fetch thumbnail as Image + metadata envelope (non-standard return shape) | uid | **MEDIUM** - base64 image transport; non-standard structured_output=False return shape (Image + dict); no explicit error if model not found | ⚠ |
| `import_sketchfab_model` | Download + import model with auto-normalize (always enabled) | uid, target_size (gt 0) | **MEDIUM** - normalize_size hardcoded True; network-dependent; no preview on import size mismatch | ⚠ |

**Summary**: 3 tools for Sketchfab. Strong: search pagination, downloadable filter. Weak: preview return shape non-standard (mixing Image + envelope), normalize_size hardcoded (no choice), no target-size validation against actual import.

---

## 2. Blender 5.2 API Introspection Results

### 2.1 GreasePencil (Blender 5.2.1 LTS)

**Finding**: GreasePencil v3 confirmed in Blender 5.2.1. Code creates via `bpy.data.grease_pencils.new()`.

```
layers: GreasePencilLayer collection ✓
frames: GreasePencilFrame per-layer collection ✓
drawing: frame.drawing has strokes, attributes, color_attributes, reorder_strokes ✓
```

**Code Assumption Validity**: ✓ VALID — all GreasePencil geometry creation paths tested and confirmed.

### 2.2 Modifier Properties (REMESH, OCEAN, ARRAY, BEVEL)

**Finding**: Real Blender 5.2.1 confirms all modifier properties used in code:

- **REMESH**: `voxel_size`, `adaptivity`, `mode` (SMOOTH/SHARP/VOXEL) ✓
- **OCEAN**: geometry_mode, size, resolution, time, spectrum, bake_foam_fade, foam_layer_name ✓
- **ARRAY**: `use_object_offset`, `offset_object` for radial arrays ✓
- **BEVEL**: `affect` (EDGES/VERTS), `harden_normals` ✓

**Code Assumption Validity**: ✓ VALID — all modifier properties confirmed present in 5.2.1.

### 2.3 Operator Existence & Arguments

**Finding**: Key operators confirmed present in 5.2.1:

- `bpy.ops.mesh.bridge_edge_loops` ✓ (properties: type, use_vertex_loop, interpolation, cuts, smoothness, twist_offset)
- `bpy.ops.mesh.symmetrize` ✓ (properties: direction, threshold)
- `bpy.ops.object.voxel_remesh` ✓ (mode, voxel_size, adaptivity, use_smooth_shade)

**Code Assumption Validity**: ✓ VALID — all key mesh/object operators present and match code expectations.

### 2.4 Transform & Matrix Handling

**Finding**: World-space decomposition via `matrix_world` confirmed; rotation_mode preservation intact.

```python
world_loc, world_rot, world_scale = obj.matrix_world.decompose()
# All properties (location, rotation_mode, scale, rotation_quaternion) accessible per code
```

**Code Assumption Validity**: ✓ VALID — transform decomposition and mode preservation work as coded.

---

## 3. Reliability Assessment

### 3.1 Error Handling

**Strong Areas**:
- Geometry pydantic validation (all GeometrySpec unions type-checked before Blender-side dispatch)
- Constraint whitelist (16 types explicitly allowed, others rejected at schema level)
- Operator result validation (explicit FINISHED checks for extrude, inset, bevel, symmetrize, bridge)
- Explicit object/collection name lookups (raise ValueError if not found; no silent defaults)

**Weak Areas**:
- **Modifier settings dict is opaque** — ModifierSpec allows arbitrary `settings: dict[str, Any]` without per-type validation
  - Example: `add_modifier(object_name, type='BEVEL', settings={'nonexistent_param': 1.0})` will be accepted by server, fail at Blender side without clear error
- **No pre-operator capability checks** — `mesh_remesh` doesn't validate if `voxel_remesh` operator exists before calling
- **Operator context assumptions** — extrude/inset/bevel all assume object is in edit mode, but code doesn't explicitly set it
  - Addon-side helpers preserve mode, but transient failures (e.g., object type mismatch) can leave state corrupted

### 3.2 State Restoration

**On Success**: ✓ All mutation operations wrap in `mutation_transaction` context manager (addon-side), which:
- Snapshots scene state before mutation
- Pushes undo checkpoint on success
- Rolls back to snapshot on exception

**On Failure**: ⚠ **Partial rollback gaps**:
- `manage_scene_collections` with action="MOVE" or "LINK" doesn't guarantee atomic rollback if one move fails mid-sequence
- `nd_capture_utils` with action="SELECT" saves no prior selection, so failure leaves viewport in wrong state
- `reset_scene` has no explicit undo; relies on transaction rollback, but if mutation_transaction wasn't entered, state may persist

**Idempotency**:
- ✓ Idempotent: `create_geometry_object` (read_only=True does nothing), `clear_*`, `sync_data_name`, `set_object_transform`
- ⚠ **NOT idempotent**: `nd_pulse_viewport_toggle` (toggles state; calling twice reverts)
- ⚠ **Idempotent only under assumption**: `add_radial_array_modifier` assumes modifier doesn't already exist

---

## 4. Missing Capabilities vs. Rubric Sections

### 4.1 Section 1: Scene Initialization (10 points)

| Rubric Item | Tool(s) Provided | Gap? | Severity |
|-------------|------------------|------|----------|
| Create empty scene | `reset_scene` | None — explicitly creates blank | ✓ |
| Set unit scale | `scene_physics.py:configure_scene_physics` | None — covered | ✓ |
| Set gravity (for sims) | `scene_physics.py:configure_scene_physics` | None — covered | ✓ |
| Create/configure render camera | `create_geometry_object` (CAMERA primitive missing) | **CAM CREATION MISSING** | 🔴 HIGH |
| Configure viewport/shading | `viewport.py:set_viewport_overlay` | Overlay only; no shading mode selection | ⚠ MEDIUM |
| Bulk collection setup | `manage_scene_collections` (CREATE/MOVE/LINK) | Supports CREATE, single-level hierarchy; no recursive batch structure | ⚠ MEDIUM |
| Assign world/environment | `create_geometry_object` (WorldGeometry not in GeometrySpec union) | **WORLD/HDRI ASSIGNMENT MISSING** | 🔴 HIGH |
| Sync render settings | Not provided | **NO RENDER CONFIG TOOL** | 🔴 HIGH |

**Missing Capabilities (Section 1)**:
1. **Camera creation** — no `kind="CAMERA"` in GeometrySpec; must use manual bpy.data.cameras/objects pathway or constraint camera
2. **World environment setup** — no `kind="WORLD"` geometry; requires separate world material/shader assignment
3. **Render settings** — no tool to set Cycles/Eevee, samples, resolution, output path, etc.

### 4.2 Section 3: Asset Import & Download (5 points)

| Rubric Item | Tool(s) Provided | Gap? | Severity |
|-------------|------------------|------|----------|
| Polyhaven HDRI import | `polyhaven.py:import_polyhaven_asset` (asset_type=hdris) | ✓ Covered | ✓ |
| Polyhaven texture import | `polyhaven.py:import_polyhaven_asset` (asset_type=textures) | ✓ Covered | ✓ |
| Polyhaven model import | `polyhaven.py:import_polyhaven_asset` (asset_type=models) | ✓ Covered | ✓ |
| Sketchfab model search | `sketchfab.py:search_sketchfab_models` | ✓ Covered | ✓ |
| Sketchfab model preview | `sketchfab.py:get_sketchfab_model_preview` | ✓ Covered | ✓ |
| Sketchfab model import | `sketchfab.py:import_sketchfab_model` | ✓ Covered | ✓ |
| Local file import (FBX/GLTF/OBJ) | Not provided | **MISSING** | 🔴 HIGH |
| Import validation/repair | Not provided | **NO VALIDATION AFTER IMPORT** | ⚠ MEDIUM |
| Import to named collection | Partial — `import_polyhaven_asset` returns object names only | No collection assignment | ⚠ MEDIUM |

**Missing Capabilities (Section 3)**:
1. **Local file import** — no tool to import FBX, GLTF, OBJ, USD, etc.; requires external file path
2. **Post-import validation** — no repair of imported normals, degenerate faces, missing armatures, scale mismatches
3. **Import organization** — polyhaven/sketchfab imports don't auto-organize into collections

---

## 5. Tool Redundancy & Consolidation

### 5.1 Boolean Overlap: `mesh_boolean` vs. `nd_boolean`

| Aspect | mesh.py | nd.py | Trade-off |
|--------|---------|-------|-----------|
| **Result** | Applied modifier | Live modifier (non-destructive) | mesh: destructive, nd: reversible |
| **Cutter handling** | `keep_cutter=True` → retains cutter | Always marks cutter as utility | mesh: manual cleanup, nd: auto-organized |
| **Undo** | Via transaction | Via ND operator context | Both have undo |
| **Production use** | Finalized assets | Work-in-progress, refinement | **Complementary, not redundant** |

**Verdict**: ✓ **NOT redundant** — serve different workflows (destructive-finalization vs. non-destructive-refinement). Keep both.

### 5.2 Modifier Management: `manage_modifiers` vs. `nd_apply_modifiers`

| Aspect | manage_modifiers | nd_apply_modifiers |
|--------|-----------------|-------------------|
| **Action** | ADD/UPDATE/REMOVE/REORDER modifiers | Apply all modifiers (single action) |
| **Scope** | Individual or batch creation | Batch application only |
| **Result** | Live or removed modifiers | All modifiers applied (destructive) |
| **Overlap** | None — different operations | Complements manage_modifiers |

**Verdict**: ✓ **Complementary** — manage_modifiers is the creation interface; nd_apply_modifiers is finalization. Keep both.

### 5.3 Constraint Management: `manage_object_constraints` (only option)

**Verdict**: ✓ **Unique** — only constraint tool. Whitelist-based design is appropriate for production safety.

### 5.4 ID Material Creation: `nd_create_id_material` vs. `nd_bulk_create_id_materials`

| Aspect | single | bulk |
|--------|--------|------|
| **Input** | object_name, diffuse_color | object_names (list), color_space |
| **Color choice** | Explicit | Random (no seed) |
| **Output** | One material | One per object, distinct |
| **Overlap** | Single call vs. batch call | Same underlying operation |

**Verdict**: ⚠ **Redundant** — `nd_bulk_create_id_materials` can be replaced by batch `nd_create_id_material` calls with user-provided colors. However, random color generation is convenience; keep as-is if user doesn't have explicit color palette.

**Recommendation**: Consider merging into single tool with optional `colors: list[tuple[int,int,int]] | None` (explicit) or `auto_generate_distinct=True` (random).

---

## 6. Production Workflow Reliability

### 6.1 Dry-Run Support

**⚠ STALE (2026-09-21)**: no tool in this domain has a `dry_run` or `read_only` parameter. A search
for either identifier across `server/tools/{scene,scene_authoring,mesh,model,nd,polyhaven,sketchfab}.py`
returns nothing; the only `dry_run` parameters in the repository are in `server/tools/liquid/shot.py`
and `server/tools/cloth/configure.py`, neither of which is in this domain.

**Corrected**: **no tool in this domain supports dry-run or preview.** What exists instead is
(a) pre-mutation validation that rejects bad input before the first mutation
(`bundled/addon/helpers.py:174-192` index bounds, `bundled/addon/handlers/scene.py:1062` batch object
prevalidation, `server/tools/scene.py:424` modifier-settings validation), (b) identity-based rollback
on failure (`bundled/addon/transaction.py:407-474`), and (c) explicit confirmation gates on every
destructive action (`server/tools/scene_authoring.py:342,372`, `server/tools/scene.py:422`,
`server/tools/nd.py:148`).

**Gap (still open)**: **mesh editing operations cannot be previewed** — an agent cannot inspect an
extrude/inset/bevel/bridge result before committing it. Mitigated, not closed, by the mesh geometry
backup that is restored on failure (`bundled/addon/server_core.py:1827-1857`
`_GEOMETRY_MUTATING_COMMANDS`, plus `bundled/addon/transaction.py:412-417`).

### 6.2 Named Object Safety

**⚠ STALE (2026-09-21)**: the opposite is now true. `duplicate_or_instance_objects` takes an explicit
`names` list (there is no `instance_count`), rejects duplicates inside the batch, and rejects any name
already present in `bpy.data.objects` with `Objects already exist: [...]` before creating anything
(`bundled/addon/handlers/scene.py:997-1001`). `manage_scene_collections` action `CREATE` likewise
rejects an existing collection name (`bundled/addon/handlers/scene.py:1049-1050`), and
`create_geometry_object` rejects a name already in use (`bundled/addon/handlers/scene.py:908-909`).

**Corrected risk**: a name collision is a hard error, not a silent auto-increment, and a failed batch
removes the duplicates it had already created (`bundled/addon/handlers/scene.py:1030-1033`). The
residual risk moves to provider imports: `import_polyhaven_asset`/`import_sketchfab_model` let
Blender's own importer name the objects, and neither accepts a target collection.

### 6.3 Determinism & Reproducibility

**Deterministic**:
- ✓ `create_geometry_object` (full spec from pydantic)
- ✓ `set_object_transform` (explicit transforms)
- ✓ `manage_scene_collections` (explicit names/actions)
- ✓ All constraint/modifier specs

**Non-deterministic**:
- ⚠ `nd_bulk_create_id_materials` — still random distinct colours with no seed parameter
  (`server/tools/nd.py:222-250`)
- ⚠ **STALE (2026-09-21)** `duplicate_or_instance_objects` is now deterministic: every name is
  caller-supplied and collisions are rejected (`bundled/addon/handlers/scene.py:997-1001`)

---

## 7. Key Findings

### 7.1 Strengths

1. **Comprehensive scene composition** — GeometrySpec union covers 9 geometry types with full pydantic validation
2. **Production-grade validation** — `validate_scene` checks camera, lights, frame range, scale, degenerate faces, simulation caches
3. **Explicit naming & collision-safety** — all operations require explicit object/collection names; no silent defaults or bulk-selection deletion
4. **World-space transform handling** — `copy_object_transform` correctly decomposes/recomposes matrix_world for cross-parent alignment
5. **Non-destructive-first design** — ND tools default to live modifiers; boolean, extrude, etc. can be previewed before apply
6. **Asset library integration** — both Polyhaven and Sketchfab fully exposed with pagination, search, preview, import

### 7.2 Critical Gaps

1. ⚠ **STALE** — **camera creation exists**: `create_camera`, `configure_camera`, `set_scene_camera`,
   `configure_camera_dof` (`server/tools/camera/core.py:72,119,144,175`). Its absence from
   `GeometrySpec` is tool placement, not a missing capability: that union describes geometry
   datablocks.
2. ⚠ **STALE** — **world/environment setup exists**: `configure_world_background`,
   `configure_hdri_environment`, `configure_procedural_sky`
   (`server/tools/lighting/environment.py:45,83,129`). `import_polyhaven_asset` wires an HDRI through
   the managed World graph on `bpy.context.scene.world`, not `bpy.data.worlds[0]`
   (`bundled/addon/handlers/polyhaven.py:177-178`).
3. ⚠ **STALE** — **render configuration exists**: `inspect_render_setup`,
   `configure_render_settings`, `manage_view_layers`, `render_scene`, `inspect_render_output`
   (`server/tools/rendering.py:221,236,262,481,644`).
4. ✓ **VERIFIED, still open** — **no local file import**: nothing imports FBX/GLTF/OBJ/USD from a
   caller-supplied path. `import_scene.*`/`wm.obj_import` appear only inside the provider handlers
   (`bundled/addon/handlers/polyhaven.py:384-388`, `bundled/addon/handlers/sketchfab.py:343`), and
   `server/tools/file_lifecycle.py` covers `.blend` open/save/link/override only. Export exists
   without a matching import (`bundled/addon/handlers/rigid_body/exporting.py:108-109`).
5. ⚠ **STALE** — **modifier settings are not opaque**: 30 modifier types each carry an explicit
   setting allowlist and a generated per-type pydantic variant, validated before the call leaves the
   server (`server/tools/scene.py:111-303`, applied at `server/tools/scene.py:424`) and re-checked
   against the per-type allowlist Blender-side (`bundled/addon/handlers/scene.py:1222`). An unknown
   key is rejected rather than passed to Blender. Constraint settings are allowlisted the same way
   (`bundled/addon/handlers/scene.py:1178`), with a copy-validate-restore fallback on failure
   (`bundled/addon/handlers/scene.py:1161-1184`).

### 7.3 Reliability Risks

1. ✓ **VERIFIED — behaviour unchanged, now disclosed** — `nd_pulse_viewport_toggle` is still a pulse
   rather than a setter (`server/tools/nd.py:356-388`) because ND exposes no readable state for
   CLEAR_VIEW/CUSTOM_VIEW/UTILS. The docstring now states "it is NOT guaranteed idempotent" and
   points at the idempotent native setter `set_viewport_overlay` (`server/tools/viewport.py:60`). Not
   fixable without upstream state; correctly scoped and labelled.
2. ⚠ **STALE** — `nd_clean_utils` requires `confirm=True` (`server/tools/nd.py:148,165`), reports
   exactly what it removed by diffing the scene, and leaves one named undo step. Its docstring states
   why a true dry-run is infeasible without reimplementing ND's cleanup, and that there is no
   MCP-level rollback once the response is returned.
3. ⚠ **STALE** — mesh operator context is explicit, not assumed. `edit_mesh` validates indices
   against the base mesh *before* entering Edit Mode, enters it, and exits in a `finally`, all inside
   `preserve_mode_and_selection`, which restores mode, active object and selection on both success and
   failure (`bundled/addon/helpers.py:67-95,174-230`). Every core mesh operator asserts `FINISHED`
   (`bundled/addon/handlers/mesh.py:137,161,195,221,276,300`), and mesh commands additionally get a
   geometry backup restored on failure (`bundled/addon/server_core.py:1827-1857`
   `_GEOMETRY_MUTATING_COMMANDS`).
4. ✓ **VERIFIED, still open** — no mesh-operation dry-run; see §6.1 as corrected.
5. ⚠ **PARTIALLY STALE** — provider imports now validate the operator result and report what actually
   appeared: Sketchfab diffs `session_uid` sets before/after the import, requires `FINISHED`, and
   records author/licence/attribution provenance onto the imported objects
   (`bundled/addon/handlers/sketchfab.py:342-382`). Poly Haven reports imported object names rather
   than the `asset_id` (`server/tools/polyhaven.py:159-164`). What is still missing is geometry QA
   (normals, scale sanity, missing materials, armature validity) and import-to-collection.

---

## 8. Recommendations for Production Readiness

### 8.1 Must-Fix (Blocks Production Use)

1. **Remove or fix `nd_pulse_viewport_toggle`** — Replace with explicit `set_viewport_overlay_state(action: SET, mode: CLEAR_VIEW|CUSTOM_VIEW|UTILS)` (set only, no toggle)
2. **Add safety wrapper to `nd_clean_utils`** — Require explicit `confirm_destructive=True` parameter; add dry-run mode
3. **Add camera creation** — Extend GeometrySpec with `kind="CAMERA"` including lens type, focal length, sensor size, dof
4. **Add world creation** — Extend GeometrySpec with `kind="WORLD"` or new tool `configure_world_environment` (shader assignment, background strength, etc.)
5. **Add render configuration tool** — New tool `configure_render_engine` (engine: CYCLES/EEVEE, samples, resolution, denoiser, output path, etc.)

### 8.2 Should-Fix (Improves Production Use)

1. **Validate modifier settings per type** — ModifierSpec should have per-modifier schema validation (ARRAY → repeat_x/y/z range, etc.)
2. **Add mesh operation dry-run** — Wrap `mesh_extrude`, `mesh_inset`, `mesh_bevel`, `mesh_bridge` with optional `dry_run=True` (undo after preview)
3. **Add local file import** — New tool `import_file_model` (path: str, file_type: FBX/GLTF/OBJ, collection_name: str, normalize_size: bool)
4. **Add post-import validation** — New tool `validate_imported_model` (object_names, checks: normals, scale, materials, armature, degenerate faces)
5. **Merge ID material tools** — Combine `nd_create_id_material` + `nd_bulk_create_id_materials` into single tool with explicit or auto color palette

### 8.3 Nice-to-Have (Optimization)

1. **Add geometry duplication options** — `duplicate_or_instance_objects` could support `shallow_copy` (unlink data) vs. `linked` vs. `full_copy`
2. **Expose modifier order priorities** — `manage_modifiers` reorder is present but underdocumented; clarify precedence
3. **Batch constraint application** — `manage_object_constraints` could support applying same constraint to multiple objects in one call

---

## 9. Tool Consolidation Matrix

| Current | Consolidate With | Action | Benefit |
|---------|------------------|--------|---------|
| `nd_create_id_material` | `nd_bulk_create_id_materials` | Merge | Single interface for single/batch ID coloring |
| `mesh_boolean` | `nd_boolean` | Keep separate | Serve different workflows (destructive vs. non-destructive) |
| `manage_modifiers` | `nd_apply_modifiers` | Keep separate | Creation vs. finalization; different APIs |
| `create_primitive_object` | `create_geometry_object` | Keep separate | Primitives are special case of geometry creation |

---

## 10. Workflow Coverage: "Create Simple Product Visualization"

**Test Workflow**: Create 3D product scene with floor, product model, studio lights, render camera, HDR environment.

**⚠ STALE (2026-09-21)**: the walkthrough below assumes tools and parameters that do not exist, and
reports blockers that are not blockers. Corrected walkthrough — every tool named here is registered in
the current tree: `reset_scene(confirm_reset=True)` → `create_primitive_object("PLANE", ...)` →
`import_sketchfab_model(uid, target_size)` → `set_object_transform(object_name, patch, space)` →
`create_light` or `create_studio_lighting` → `configure_hdri_environment` → `create_camera` +
`set_scene_camera` → `configure_render_settings` → `validate_scene(scene_name, scope=[...])` →
`render_scene` → `inspect_render_output`. No step is blocked.

```
1. reset_scene() → Clear Blender
2. create_geometry_object(kind="MESH", type="PLANE", name="Floor", scale=(10,10,1)) → Floor
   ⚠ Missing: Camera creation (must skip render setup)
   ⚠ Missing: World/HDRI setup (no environment)
3. import_sketchfab_model(uid="product_id", target_size=2.0) → Import product
4. set_object_transform(object_name="product", transform={location=(0,0,1)}) → Center product
5. create_geometry_object(kind="LIGHT", type="SUN", name="KeyLight", intensity=2.0) → Sun key light
   ⚠ Missing: Light creation not supported in GeometrySpec
6. validate_scene(active_domains=["scene", "lighting"]) → Check readiness
   ⚠ Missing: Camera validation would fail (no camera created)
7. [Cannot render — no camera, no render settings tool]
```

**Blockers Identified** — ⚠ **four of five are STALE**:
- ~~No camera creation~~ → `server/tools/camera/core.py:72,144`
- ~~No world/HDRI setup~~ → `server/tools/lighting/environment.py:83`
- ~~No light creation~~ → `server/tools/lighting/construction.py:89,235`
- ~~No render configuration~~ → `server/tools/rendering.py:236`
- ~~No render tool~~ → `server/tools/rendering.py:481`

**Remaining friction (verified)**: the product model can only come from a provider or a `.blend`
library — a caller-supplied `.fbx`/`.glb`/`.obj` cannot be imported at all — and nothing assigns
imported objects to a named collection, so an import lands in whatever collection is active.

**Coverage Score**: ⚠ **STALE** — the 40/100 rested entirely on the blockers above. The workflow is
executable end to end in the current tree.

---

## 11. Summary & Verdict

### Current Status
- ⚠ **STALE** — **43 registered tools** in the seven files this domain owns: `scene.py` (7),
  `scene_authoring.py` (3), `mesh.py` (13), `model.py` (3), `nd.py` (10), `polyhaven.py` (4),
  `sketchfab.py` (3). The "52" additionally counted the adjacent inspection/meta surface
  (`viewport.py` 5, `scene_physics.py` 2, `core.py` 2) and never matched this report's own tables,
  which enumerate 43. Counting method in the verification pass below.
- **9 geometry types** in the declarative API — ✓ VERIFIED
  (`server/tools/scene_authoring.py:282-293`)
- **16 constraint types** — ✓ VERIFIED (`server/tools/scene.py:76-101`). **30 modifier types**, not
  "32+", each with an explicit setting allowlist (`server/tools/scene.py:111-257`)
- **Full asset library integration** (Poly Haven + Sketchfab) — ✓ VERIFIED, and now opt-in per
  process: both modules live in the `assets` bundle, which neither artist-facing mode selects
  (`server/bundles.py:59,83-88`)

### Production-Ready Domains
- ✓ Scene composition (objects, collections, hierarchy)
- ✓ Mesh editing (extrude, inset, bevel, boolean, symmetry, remesh)
- ✓ Transform & modeling (copy transforms, arrays, ID materials)
- ✓ Asset search & import (Polyhaven, Sketchfab)
- ✓ Scene validation (comprehensive health checks)

### Production-Blocked Domains
- ⚠ **STALE** — Camera creation & setup: `create_camera`/`set_scene_camera` exist
- ⚠ **STALE** — World/HDRI environment: `configure_hdri_environment`/`configure_world_background`
  exist
- ⚠ **STALE** — Render engine configuration: `configure_render_settings` exists
- 🔴 **VERIFIED, still blocked** — Local file import (FBX/GLTF/OBJ/USD from a caller-supplied path)
- ⚠ **STALE** — Render output execution: `render_scene`/`inspect_render_output` exist

### Risk Assessment
- 🟠 ⚠ **PARTIALLY STALE** — Idempotency: `nd_pulse_viewport_toggle` is still a pulse, but it is
  documented as such and is no longer the only route to a viewport overlay
  (`server/tools/viewport.py:60`)
- 🟠 ⚠ **STALE** — Destructiveness: every destructive path in this domain now has an explicit
  confirmation gate (`server/tools/nd.py:148`, `server/tools/scene_authoring.py:342,372`,
  `server/tools/scene.py:422`, `bundled/addon/handlers/scene.py:1086`)
- 🟠 ⚠ **STALE** — Validation: modifier and constraint settings are validated per type on both sides
  of the socket (`server/tools/scene.py:424`, `bundled/addon/handlers/scene.py:1222,1178`)
- 🟠 ✓ **VERIFIED, narrowed** — State restoration: collection membership is not among the fields
  `mutation_transaction` captures (`bundled/addon/transaction.py:412-417`), and a rollback never
  resurrects a deleted pre-existing datablock (`bundled/addon/transaction.py:427-428`), so
  `mesh_boolean(keep_cutter=False)` and `reset_scene` are unrecoverable by rollback. Batch object
  prevalidation (`bundled/addon/handlers/scene.py:1062`) removes the specific mid-sequence collection
  failure this report described
- 🟠 **NEW (verified)** — Provider downloads run synchronously inside Blender's main-thread command
  drain (`bundled/addon/handlers/polyhaven.py:168`, `bundled/addon/network.py:41-61`), so a large
  import freezes the UI with no progress and no cancellation. Bounded (connect/read timeouts, byte
  caps) but blocking

### Reliability Grade: A− (re-verified 2026-09-21; was B+)
- Pre-mutation validation on both sides of the socket; identity-based rollback with explicitly
  documented limits; exactly one named undo checkpoint per successful request
- Mode, active object, selection and mesh geometry restored on failure for every mesh operator
- Explicit caller-supplied names, rejected collisions, confirmation gates on every destructive path
- Weak points: no preview/dry-run; blocking provider I/O on Blender's main thread; rollback blind
  spots (collection membership, deleted pre-existing datablocks); `add_radial_array_modifier` pivot
  geometry still unproven against live Blender

### Recommendation
⚠ **STALE** — "60% complete", and the camera/world/render-config blockers behind that number, do not
hold; see the verification pass below. **Corrected**: this domain is production-usable. The one
capability hole is local file import (FBX/GLTF/OBJ/USD); the one operational hole is asynchronous
provider I/O with progress and cancellation; and `add_radial_array_modifier`'s arbitrary-pivot
geometry needs a live-Blender proof before its output is trusted. Mesh-operation preview and
post-import geometry QA remain worthwhile, but neither blocks production use.

---

**End Slice 1 Report**

---

## Verification pass (2026-09-21)

Method: every load-bearing claim in §1–§11 was re-read against the current working tree. No Blender
was available to this pass, so any claim that can only be settled by running Blender is marked
UNVERIFIABLE with the reason and the command that would settle it (`just smoke`, `just gate`). Paths
are relative to `src/blender_mcp/`.

`bundled/addon/server_core.py` was being edited by concurrent work while this pass ran, so every
citation into it carries the symbol name alongside the line range (e.g.
`server_core.py:2000-2018 _run_handler`); if the numbers have drifted since, resolve by symbol.
Claims about `add_radial_array_modifier`'s pivot geometry and about render-overhead behaviour are
deliberately left to the live-Blender work running in parallel rather than asserted here.

### Tool count (counted, not carried over)

Counting `@mcp.tool` registrations in the seven files this domain owns:

| File | Tools | Registration lines |
|---|---|---|
| `server/tools/scene.py` | 7 | 306, 322, 347, 367, 386, 403, 442 |
| `server/tools/scene_authoring.py` | 3 | 303, 334, 358 |
| `server/tools/mesh.py` | 13 | 22, 77, 121, 168, 223, 290, 326, 374, 418, 454, 500, 532, 562 |
| `server/tools/model.py` | 3 | 22, 76, 142 |
| `server/tools/nd.py` | 10 | 52, 99, 147, 185, 222, 253, 289, 321, 356, 391 |
| `server/tools/polyhaven.py` | 4 | 46, 80, 136, 195 |
| `server/tools/sketchfab.py` | 3 | 42, 108, 157 |
| **Total** | **43** | |

**43 tools**, not 52. `scene.py` no longer holds the authoring trio (`create_geometry_object`,
`remove_scene_objects`, `reset_scene`): those moved to `scene_authoring.py`, which is a separate
`scene-authoring` bundle (`server/bundles.py:38`). The adjacent inspection/meta surface this report
cites in §4.1 but does not own adds 9 more (`viewport.py` 5, `scene_physics.py` 2, `core.py` 2) — 52
with them, which is where the old number came from; it never matched §1's own tables, which enumerate
43. Whole-server total for context: **298 registered tools** (`server/bundles.py:6`, asserted by
`tests/server/test_bundles.py`).

### Claim-by-claim result

#### §1.1 Scene composition

| Claim | Verdict | Current truth |
|---|---|---|
| "scene.py: 10 tools, 777 LOC" | **STALE** | 7 tools; authoring trio moved to `server/tools/scene_authoring.py:303,334,358` |
| Every §1.1 tool takes `read_only` | **STALE** | No tool in this domain has `read_only` or `dry_run`; see §6.1 as corrected |
| `create_geometry_object(object_name, geometry, transform, material_slots, read_only)` | **STALE** | `(name, geometry, collection_name, location, rotation, scale)` — `server/tools/scene_authoring.py:303-311`. No `material_slots`; transforms are flat and parent-local |
| 9 geometry types, full pydantic validation | **VERIFIED** | `server/tools/scene_authoring.py:282-293` (mesh, spline, text, meta, lattice, point cloud, curves, grease pencil, volume) |
| `set_object_transform` validates rotation/scale non-degeneracy, 4x4 matrix, mutual exclusion | **VERIFIED** | `server/tools/scene.py:35-50` — rejects empty patches, >1 rotation representation, non-4x4 matrices, matrix-plus-components, zero scale |
| `manage_object_hierarchy` "no explicit world-transform preservation in code" | **STALE** | `preserve_world_transform: bool = True` (`server/tools/scene.py:371`), applied by capturing and restoring `matrix_world` (`bundled/addon/handlers/scene.py:1132-1138`). The handler also rejects self-parenting and cycles (`:1111-1130`) and validates bone parents (`:1113-1119`) |
| `manage_scene_collections` actions CREATE/MOVE/LINK | **STALE** | CREATE / LINK_OBJECTS / UNLINK_OBJECTS / SET_VISIBILITY / REMOVE (`server/tools/scene.py:350`) |
| `manage_scene_collections` "no rollback on partial moves" | **PARTIALLY STALE** | Every object name is resolved before the first mutation (`bundled/addon/handlers/scene.py:1062`), REMOVE requires `confirm_remove` and refuses a non-empty collection (`:1086-1089`), UNLINK refuses to orphan (`:1067-1074`). Residual: collection membership is not a field `mutation_transaction` restores (`bundled/addon/transaction.py:412-417`) |
| `manage_object_constraints`: 16 types, whitelist-enforced | **VERIFIED** | `server/tools/scene.py:76-101` |
| `manage_object_constraints`: "no influence/subtarget validation" | **PARTIALLY STALE** | `influence` is bounded `ge=0, le=1` (`server/tools/scene.py:76-101`) and settings are allowlisted per constraint type Blender-side (`bundled/addon/handlers/scene.py:1178`) with copy-validate-restore on failure (`:1161-1184`). `subtarget` is still a free string, validated only by Blender |
| `manage_modifiers`: 32 types | **STALE** | 30 types (`server/tools/scene.py:111-142`, enumerated in the tool docstring at `:416-419`) |
| `manage_modifiers`: "settings dict is **opaque**; no per-modifier validation" | **STALE** | Per-type setting allowlists build one pydantic variant per modifier type into a discriminated union, validated before the call leaves the server (`server/tools/scene.py:111-303`, applied `:424`), then re-checked against the per-type allowlist Blender-side (`bundled/addon/handlers/scene.py:1222`). REMOVE/APPLY additionally require `confirm_destructive` (`server/tools/scene.py:422-423`) |
| `duplicate_or_instance_objects(object_names, instance_count, …, linked)` | **STALE** | `(source_object_name, names, transforms, mode, collection_name)` with `mode` ∈ COPY / LINKED_DATA / COLLECTION_INSTANCE (`server/tools/scene.py:322-330`) |
| "collision-free naming" (auto-increment) | **STALE** | Collisions are rejected, not incremented (`bundled/addon/handlers/scene.py:997-1001`); a mid-batch failure removes the duplicates already created (`:1030-1033`) |
| `remove_scene_objects(object_names, recursive, read_only)` | **STALE** | `(object_names, managed_rig, confirm_remove)`; requires `confirm_remove=True` (`server/tools/scene_authoring.py:334-345`) |
| `reset_scene(preserve_world, preserve_camera, preserve_lights, read_only)` | **STALE** | `(confirm_reset, scene_name, purge_orphaned_data)` (`server/tools/scene_authoring.py:358-373`). No preserve flags; `purge_orphaned_data=True` purges file-wide, which the docstring states |
| `reset_scene` "no explicit undo" | **PARTIALLY STALE** | It *is* transacted and leaves one named undo checkpoint (`bundled/addon/server_core.py:2000-2018` `_run_handler`); but rollback never resurrects deleted pre-existing datablocks (`bundled/addon/transaction.py:427-428`), so a failure part-way is not recoverable at MCP level. The finding's substance survives; its stated cause does not |
| `validate_scene(active_domains=[scene, camera, lighting, pbr, cloth, liquid])` | **STALE** | `(scene_name, scope, max_findings)`; `scope` has 7 domains including `persistence`; it orchestrates `validate_pbr_asset`, `validate_lighting_setup`, `validate_cloth_setup`, `validate_liquid_setup`, `validate_camera_rig`, normalizes findings and reports `truncated`/`domain_summaries` (`server/tools/scene.py:442-476`) |

#### §1.2 Mesh editing

| Claim | Verdict | Current truth |
|---|---|---|
| `create_primitive_object`: 7 primitives, dimensions override, blockout tagging | **VERIFIED** | `server/tools/mesh.py:18-19`; operator table `bundled/addon/handlers/mesh.py:28-53`; dimensions/purpose semantics `:68-71` |
| Operator result validation (`FINISHED`) for extrude/inset/bevel/bridge/symmetrize/subdivide | **VERIFIED** | `bundled/addon/handlers/mesh.py:137,161,195,221,276,300` |
| "no pre-selection state restoration doc" / edit-mode state can be left corrupted | **STALE** | `edit_mesh` validates indices against the base mesh before entering Edit Mode, then exits in `finally`, inside `preserve_mode_and_selection`, which restores mode/active/selection on success *and* failure (`bundled/addon/helpers.py:67-95,174-230`). Mesh commands also carry a geometry backup restored on failure (`bundled/addon/server_core.py:1827-1857` `_GEOMETRY_MUTATING_COMMANDS`) |
| `mesh_bevel` `affect` is EDGES/VERTS | **STALE** | `Literal["EDGES", "VERTICES"]` (`server/tools/mesh.py:174`) |
| `mesh_bevel` has no `harden_normals`/`angle_limit` | **VERIFIED** | `server/tools/mesh.py:168-176` |
| `mesh_bridge`: `edge_indices` deprecated in favour of loop selection | **VERIFIED** | `(loop_a_edge_indices, loop_b_edge_indices, edge_indices, cuts, interpolation, smoothness, twist_offset, expected_revision)` (`server/tools/mesh.py:223-235`). `expected_revision` (`:234`) is a stale-topology guard the rest of the domain lacks |
| `mesh_symmetrize` has no threshold/merge tolerance | **VERIFIED** | `server/tools/mesh.py:291` exposes `direction` only |
| `mesh_boolean(mode, keep_cutter)` | **STALE** | Parameter is `operation`; `keep_cutter` defaults **True** (`server/tools/mesh.py:326-332`). Same-object booleans are rejected (`bundled/addon/handlers/mesh.py:245`) |
| `mesh_subdivide` has no fractal/quad_corner_type | **VERIFIED** | `server/tools/mesh.py:374-379` (`cuts` bounded 1–1000) |
| `mesh_remesh(voxel_size, smoothness, mode, apply)`; "mode not validated" | **STALE** | Signature is `(object_name, voxel_size)` only (`server/tools/mesh.py:419`). There is no `mode`, `smoothness` or `apply`, so the validation criticism has no referent; the underlying call is still `bpy.ops.object.voxel_remesh` with a `FINISHED` assert (`bundled/addon/handlers/mesh.py:299-301`) |
| `clear_edge_marks` clears "seam/sharp/crease/bevel", "comprehensive" | **STALE** | Clears `use_edge_sharp`, `use_seam`, `use_freestyle_mark` — crease and bevel weight are untouched (`bundled/addon/handlers/mesh.py:377-382`). The tool's own docstring is accurate ("sharp/seam/freestyle", `server/tools/mesh.py:565-567`); the audit over-stated it |

#### §1.3 Model transform & array

| Claim | Verdict | Current truth |
|---|---|---|
| `copy_object_transform`: HIGH, correct world-space decomposition, full quat/euler handling | **VERIFIED** | World path decomposes both matrices and recomposes with `Matrix.LocRotScale` (`bundled/addon/handlers/model.py:78-83`); the result returns the object's rotation in its *native* representation via `rotation_as_native_list`, which exists specifically to avoid `to_euler(rotation_mode)` on QUATERNION/AXIS_ANGLE (`bundled/addon/helpers.py:373-394`), and labels local vs. world explicitly (`handlers/model.py:86-95`). This closes the "High / partially solved" quaternion-formatting row in `AGENTS.md` |
| `add_radial_array_modifier(count, radius, height_offset, pivot_object, pivot_location, apply)` | **STALE** | `(count, axis, apply, pivot_object_name, pivot_location, radius)` (`server/tools/model.py:76-86`). There is no `height_offset`; `axis` is new; at most one pivot source is accepted and supplying none is an error rather than silently overlapping copies (`bundled/addon/handlers/model.py:140-159`) |
| No REPEAT_Y/Z or merge settings | **VERIFIED** | The handler sets `count`, `use_relative_offset=False`, `use_object_offset=True`, `offset_object` only (`bundled/addon/handlers/model.py:165-169`) |
| Pivot rotation is not a true world-space "rotate around this point" | **UNVERIFIABLE (code-corrected, geometry unproven)** | The helper empty is now placed at `Translate(pivot) @ Rotate(angle, axis) @ Translate(-pivot) @ obj.matrix_world` (`bundled/addon/handlers/model.py:164`, `bundled/addon/helpers.py:397-417`) — the composition the finding asked for. Whether the resulting ring is correct for non-origin, parented, rotated and scaled objects cannot be settled without Blender; that live proof is in flight separately and is **not** scored as proven here |
| `sync_data_name` idempotent batch rename | **VERIFIED** | `server/tools/model.py:142`; `bundled/addon/handlers/model.py:179-204` |

#### §1.4 ND toolkit

| Claim | Verdict | Current truth |
|---|---|---|
| `nd_boolean` "no undo on cancel" | **PARTIALLY STALE** | A cancelled ND operator now yields `ok:false`, `changed_objects=[]` and an explicit "scene is unchanged" warning through the shared `_nd_outcome` (`server/tools/nd.py:27-49`). The scene is still not rolled back if ND mutated before cancelling; the *reporting* defect is fixed |
| `nd_clean_utils`: 🔴 no dry-run, hardcoded destructive, no rollback | **STALE** | Requires `confirm=True` (`server/tools/nd.py:148,165`), reports removed objects/modifiers by before/after diff, and documents both why a true dry-run is infeasible and that there is no MCP-level rollback afterwards (`:149-169`) |
| `nd_create_id_material(object_name, diffuse_color, color_space)` | **STALE** | `(object_names, material_name)` — a batch, with no colour parameter at all (`server/tools/nd.py:185-190`) |
| `nd_bulk_create_id_materials(object_names, color_space)`; random RGB, no seed | **PARTIALLY STALE** | Signature is `(object_names)` (`server/tools/nd.py:222`); the no-seed non-determinism is **VERIFIED** and remains the only non-deterministic tool in the domain |
| `nd_set_lod_suffix(lod_level)`; "no validation that rename succeeds or collides" | **STALE** | Parameter is `mode` (`server/tools/nd.py:253-257`); the tool returns the actual post-rename names and reports those as `changed_objects` rather than the requested ones (`:282-283`) |
| `nd_single_vertex(object_name)` | **STALE** | `(location)` (`server/tools/nd.py:289-292`); the name comes back from the handler and is omitted from `changed_objects` when cancelled (`:314-315`), which closes the "must not dereference the active object after cancellation" note in `AGENTS.md` |
| `nd_apply_modifiers`: no selective apply or mode choice | **VERIFIED** | `server/tools/nd.py:321`; the docstring explains that ND's SOFT/HARD variants are driven by modifier keys and unreachable from a script (`:326-329`) |
| `nd_pulse_viewport_toggle`: 🔴 not idempotent | **VERIFIED (behaviour), disclosed** | Still a pulse (`server/tools/nd.py:356-388`), because ND exposes no readable state for these three toggles; the docstring says so and redirects to the idempotent native setter `set_viewport_overlay` (`server/tools/viewport.py:60`). It is routed around the transaction deliberately (`bundled/addon/server_core.py:1724` `_NON_UNDO_COMMANDS`) |
| `nd_capture_utils(action=SELECT/DISPLAY)`; overwrites selection without saving state | **STALE (signature) / VERIFIED (behaviour, by design)** | No `action` parameter (`server/tools/nd.py:392`); it displays *and* selects. Replacing the selection is the tool's stated purpose and the docstring says it changes selection/display state only (`:399-401`). Recorded as a documented contract, not an undisclosed gap |

#### §1.5–§1.6 Asset providers

| Claim | Verdict | Current truth |
|---|---|---|
| `get_polyhaven_categories`: checks enablement, validates asset_type | **VERIFIED** | `server/tools/polyhaven.py:46-77` |
| `list_polyhaven_assets`: paginated, deterministic by asset ID | **VERIFIED** | `server/tools/polyhaven.py:80-128`; returns `offset`, `limit`, `truncated`, `next_offset`. This closes the `AGENTS.md` "catalog cannot be paged / rename to list_*" row on both counts |
| `import_polyhaven_asset`: "async download" | **STALE** | Only the socket round-trip leaves the event loop (`asyncio.to_thread`); the download itself runs synchronously inside Blender's main-thread command drain (`bundled/addon/handlers/polyhaven.py:168,212,344`). Also the parameter is `file_format`, not `format` (`server/tools/polyhaven.py:142`) |
| Poly Haven networking unbounded / private `tempfile._cleanup()` | **STALE** | All provider HTTP goes through `bundled/addon/network.py:9-61`: `(5, 60)` connect/read timeouts, `raise_for_status`, streamed `iter_content`, declared-size pre-check and hard byte ceilings; per-asset caps at `bundled/addon/handlers/polyhaven.py:14-15`; temp dirs removed in `finally` (`:422-425`). No `tempfile._cleanup` remains anywhere |
| Destructive world handling (`bpy.data.worlds[0]`) | **STALE** | Uses `bpy.context.scene.world` and only creates a world when the scene has none (`bundled/addon/handlers/polyhaven.py:177-178`) |
| `apply_polyhaven_texture(asset_id, material_slot_index, replacement_policy, confirm_replace_all)` | **STALE (signature) / VERIFIED (policy)** | `(object_name, texture_id, replacement_policy, material_slot_index, confirm_replace_all)`; REPLACE_ALL requires confirmation and REPLACE_SLOT requires an index, both enforced before the call reaches Blender (`server/tools/polyhaven.py:195-230`) |
| "no prior material validation" on apply | **UNVERIFIABLE** | The tool reuses the graph built by import rather than rebuilding it (`server/tools/polyhaven.py:216`); whether the resulting slots are correct in a real file needs Blender |
| `search_sketchfab_models`: cursor pagination, category filter, count cap | **VERIFIED** | `server/tools/sketchfab.py:42-100`; `count` bounded 1–100, returns `next_cursor`/`previous_cursor`. This closes the `AGENTS.md` "search loses pagination information" row |
| `get_sketchfab_model_preview`: "no explicit error if model not found" | **STALE** | A provider error is raised (`server/tools/sketchfab.py:140-141`). The two-item `[Image, envelope]` return is **VERIFIED** and now documented as deliberate (`:108-117`) |
| `import_sketchfab_model`: `normalize_size` hardcoded True | **VERIFIED** | `server/tools/sketchfab.py:191` |
| "no target-size validation" | **STALE** | `target_size` is required and `gt=0` (`server/tools/sketchfab.py:161`) |
| Sketchfab import does not detect what it imported / loses provenance | **STALE** | `session_uid` before/after diff, `FINISHED` assert, and author / licence / licence-URL / attribution recorded on the imported objects (`bundled/addon/handlers/sketchfab.py:342-382`); temp dir removed in `finally` (`:401-403`) |

#### §2 Blender API introspection

| Claim | Verdict | Current truth |
|---|---|---|
| GreasePencil v3 collection/layer/frame/drawing shape confirmed | **UNVERIFIABLE** | No Blender in this pass, and none in CI (`AGENTS.md:106-113`). The code still targets `bpy.data.grease_pencils` (`bundled/addon/handlers/scene.py:464,925`). Re-prove with `just smoke` |
| REMESH/OCEAN/ARRAY/BEVEL properties exist in 5.2 | **UNVERIFIABLE** | Names are still used, and are now the allowlist itself (`server/tools/scene.py:111-257`), so a wrong name is a rejected request rather than a Blender error. Live re-proof needed |
| `bridge_edge_loops`, `symmetrize`, `voxel_remesh` present with the expected arguments | **UNVERIFIABLE** | Still called, each with a `FINISHED` assert (`bundled/addon/handlers/mesh.py:220,276,299`) |
| `matrix_world` decomposition / rotation-mode preservation work as coded | **UNVERIFIABLE (code path confirmed)** | `bundled/addon/handlers/model.py:78-85`, `bundled/addon/helpers.py:373-394` |

#### §3 Reliability assessment

| Claim | Verdict | Current truth |
|---|---|---|
| "All mutation operations wrap in `mutation_transaction`, which snapshots scene state" | **PARTIALLY STALE** | The dispatcher wraps mutating commands (`bundled/addon/server_core.py:2000-2018` `_run_handler`), but read-only, non-undo, session-swap and datablock-replacing commands deliberately bypass it (`:1954-1980` `bypasses_transaction`). And it is not a scene snapshot: rollback removes datablocks the request *created*, identified by `session_uid`, and restores a fixed field list — name, data name, local transform, parent, material slots, modifiers added during the request, and mesh geometry for geometry commands (`bundled/addon/transaction.py:410-434`). The limits are stated in the source rather than implied |
| Undo checkpoint pushed on success | **VERIFIED** | One named checkpoint per successful request, with a warning when undo was unavailable (`bundled/addon/transaction.py:383-397`, merged into the reply at `bundled/addon/server_core.py:2015-2017`) |
| No pre-operator capability checks | **VERIFIED** | Nothing in this domain probes operator availability before calling; the guard is the `FINISHED` assert afterwards. ND is the exception (`get_nd_status` reports whether `bpy.ops.nd` exists) |
| Weak area: "modifier settings opaque" | **STALE** | See §1.1 |
| Weak area: "operator context assumptions" | **STALE** | See §1.2 |

#### §5 Redundancy verdicts

| Claim | Verdict | Current truth |
|---|---|---|
| `mesh_boolean` vs `nd_boolean` not redundant | **VERIFIED** | Applied modifier + optional cutter deletion (`server/tools/mesh.py:326-332`) vs. live modifier + cutter retained as a parented utility (`server/tools/nd.py:52-58`) |
| `manage_modifiers` vs `nd_apply_modifiers`: "Overlap: None" | **STALE** | `manage_modifiers` now has an `APPLY` action gated on `confirm_destructive` (`server/tools/scene.py:407,422`; `bundled/addon/handlers/scene.py:1238-1242`). They do overlap: one named modifier vs. ND's selective all-eligible apply. Still worth keeping both, for a different reason than the one given |
| `nd_create_id_material` vs `nd_bulk_create_id_materials`: "single call vs. batch call" | **STALE** | Both take a batch. The real difference is one caller-named material for all objects vs. N random distinct ones, so the proposed merge is now a choice between an explicit `material_name` and an `auto_distinct` flag, not a single/batch merge |
| `manage_object_constraints` is the only constraint tool | **VERIFIED** | No other `@mcp.tool` in the tree manages object constraints |

#### §4 and §10–§11 capability gaps

| Claim | Verdict | Current truth |
|---|---|---|
| 🔴 No camera creation | **STALE** | `server/tools/camera/core.py:72,119,144,175` |
| 🔴 No world/HDRI assignment | **STALE** | `server/tools/lighting/environment.py:45,83,129` |
| 🔴 No render configuration / no render tool | **STALE** | `server/tools/rendering.py:221,236,262,481,644` |
| No light creation | **STALE** | `server/tools/lighting/construction.py:89,124,144,192,235` |
| Unit scale and gravity covered by `configure_scene_physics` | **VERIFIED** | `server/tools/scene_physics.py:18-34,62-78`; `scale_length` bounded 0.001–100 |
| Viewport: "overlay only; no shading mode selection" | **PARTIALLY STALE** | `get_viewport_screenshot` takes a per-capture `shading_override` (SOLID/MATERIAL) that is always restored (`server/tools/viewport.py:314-315`, `bundled/addon/handlers/viewport.py:95-116`). There is still no persistent shading setter |
| 🔴 No local file import (FBX/GLTF/OBJ/USD) | **VERIFIED — still open** | Importers appear only inside provider handlers (`bundled/addon/handlers/polyhaven.py:384-388`, `bundled/addon/handlers/sketchfab.py:343`); `server/tools/file_lifecycle.py` covers `.blend` open/save/link/override. Export exists without a matching import (`bundled/addon/handlers/rigid_body/exporting.py:108-109`) |
| No import-to-named-collection | **VERIFIED — still open** | Neither `import_polyhaven_asset` nor `import_sketchfab_model` accepts a collection (`server/tools/polyhaven.py:136-143`, `server/tools/sketchfab.py:157-162`) |
| No post-import geometry validation/repair | **VERIFIED — still open** | Operator success and object identity are checked; normals, scale sanity and material completeness are not |
| "52 tools total" | **STALE** | 43; see the count table above |
| "9 geometry types / 16 constraint types / 32+ modifier types" | **MIXED** | 9 **VERIFIED**, 16 **VERIFIED**, 30 not 32 (**STALE**) |
| Coverage score 40/100, "rendering impossible", "60% complete" | **STALE** | Rested entirely on the four capability claims above, all of which are false in the current tree |

### Domain-relevant findings outside this report's original scope

These were found while re-verifying and matter to whoever aggregates the rubric:

1. **`execute_blender_code` no longer exists.** No `@mcp.tool`, handler, or reference to it remains
   anywhere under `src/blender_mcp/`. The Critical "arbitrary Python execution over an
   unauthenticated socket" row in `AGENTS.md` is closed by removal, not merely mitigated. Removing a
   free-form escape hatch also means every capability in this domain must be reachable through a
   typed tool, which is why the local-file-import gap now bites: there is no longer a fallback path.
2. **Provider and ND tools are opt-in per process.** `polyhaven`/`sketchfab` are registered only via
   the `assets` bundle and `nd` only via `geometry-nodes` (`server/bundles.py:42,59`); neither
   artist-facing mode selects `assets` (`:83-88`). This partially closes the "disabled integrations
   remain visible to agents" row: registration is now an explicit per-process choice, though a
   selected bundle still registers its tools regardless of the open `.blend`'s capability flags.
3. **Blocking provider I/O is the domain's largest remaining operational risk.** Downloads are
   bounded (`bundled/addon/network.py:9-61`) but synchronous inside the main-thread command drain, so
   a multi-hundred-megabyte import freezes Blender's UI with no progress and no cancellation.
4. **Rollback blind spots, stated precisely.** Collection membership is not a restored field
   (`bundled/addon/transaction.py:412-417`), and a deleted pre-existing datablock is never
   resurrected (`:427-428`). The two tools that reach that limit in normal use are
   `mesh_boolean(keep_cutter=False)` and `reset_scene`.

---

## Score estimate

**Score estimate**: 8/10 (verified validation and rollback depth; one real capability hole in local
file import; blocking provider I/O; radial-array pivot geometry still unproven against live Blender)

Justification, tied only to findings verified above:

- **Raises it.** Input is validated before mutation on both sides of the socket — index bounds
  (`bundled/addon/helpers.py:174-192`), batch object names (`bundled/addon/handlers/scene.py:1062`),
  per-type modifier and constraint settings (`server/tools/scene.py:424`,
  `bundled/addon/handlers/scene.py:1222,1178`), transform representations
  (`server/tools/scene.py:35-50`). Failure paths restore state: identity-based rollback with
  documented limits (`bundled/addon/transaction.py:410-434`), mode/active/selection restoration
  (`bundled/addon/helpers.py:67-95`), mesh geometry backups
  (`bundled/addon/server_core.py:1827-1857` `_GEOMETRY_MUTATING_COMMANDS`), one named undo
  checkpoint per request
  (`bundled/addon/transaction.py:383-397`). Every destructive path has a confirmation gate. Reports
  describe what actually changed rather than what was requested — ND cancellation
  (`server/tools/nd.py:27-49`), Sketchfab's `session_uid` diff
  (`bundled/addon/handlers/sketchfab.py:342-346`), `nd_clean_utils`'s removal diff. Provider HTTP is
  bounded on every axis (`bundled/addon/network.py:9-61`), and provenance survives import
  (`bundled/addon/handlers/sketchfab.py:363-382`).
- **Holds it down.** No local file import at all, with no free-form escape hatch left to work around
  it. Provider downloads block Blender's main thread with no progress or cancellation. No preview or
  dry-run for any mesh edit. `add_radial_array_modifier`'s pivot composition is now correct on paper
  (`bundled/addon/helpers.py:397-417`) but unproven in Blender. `nd_pulse_viewport_toggle` is not
  idempotent (upstream constraint, honestly documented). Two rollback blind spots. `clear_edge_marks`
  leaves crease and bevel weight set.
- **Why 8 and not 7.** The three findings that moved this score most are all corrections: the
  camera/world/render "blockers" that produced the original 40/100 coverage score do not exist; the
  "opaque modifier settings" weakness is now a 30-type validated allowlist; and the destructive-ND
  and edit-mode-corruption risks are gated and restored respectively. Unlike the materials slice
  (7/10), no verified correctness bug remains in this domain; unlike the rendering slice (5/10), no
  whole capability class is missing except local file import.

### Rubric category bearing (A–J, per `.audit_tmp/COMPREHENSIVE_AUDIT_FINDINGS.md:71-152`)

| Cat | Weight | Direction | Why, from the verified findings |
|---|---|---|---|
| **A. Architecture & Abstraction** | 15 | **↑** | Declarative discriminated-union specs with per-type allowlists (`server/tools/scene.py:111-303`, `server/tools/scene_authoring.py:282-293`); one transaction contract owned by the dispatcher rather than re-implemented per handler (`bundled/addon/server_core.py:2000-2018` `_run_handler`); domain split into bundles so the catalog is selectable (`server/bundles.py:32-88`). Mild drag: the 10 `nd_*` tools are a thin passthrough over a third-party add-on, inheriting its non-idempotent toggle |
| **B. Tool Quality & Redundancy** | 15 | **↑** | No free-form `bpy` path exists anywhere any more (finding 1 above); pydantic validation throughout; confirmation gates. Drag: `nd_create_id_material` / `nd_bulk_create_id_materials` overlap, and `manage_modifiers` APPLY now overlaps `nd_apply_modifiers` (§5 corrections) |
| **C. Scene & Asset Pipeline** | 10 | **↓ (largest negative from this domain)** | Scene composition and provider import are strong — pagination, deterministic ordering, byte caps, provenance, diff-based reporting — but **no local file import (FBX/GLTF/OBJ/USD)** and **no import-to-collection**, and no post-import geometry QA. This is the one place where this domain removes points rather than restoring them |
| **D. Lighting & Camera** | 10 | **– (phantom blocker removed)** | This domain owns none of it, and its "no camera creation / no world setup / no light creation" blockers are false (`server/tools/camera/core.py:72`, `server/tools/lighting/environment.py:83`, `server/tools/lighting/construction.py:89`). The preliminary D score (10/10) never deducted for them; it must not start now. No new D evidence either way |
| **E. Animation/Rigging/Simulation** | 10 | **↑ (small)** | Contributes only the simulation prerequisites: `configure_scene_physics` gravity and unit scale, validated (`server/tools/scene_physics.py:18-34`), and `validate_scene`'s dirty cloth/rigid-body cache checks (`server/tools/scene.py:453-461`) |
| **F. Rendering** | 15 | **– (phantom blocker removed)** | The "no render configuration / no render tool" blocker is false (`server/tools/rendering.py:236,481`). No new F evidence; the rendering slice's own findings (video output, timeout, GPU fallback) stand unchanged |
| **G. Compositing** | 5 | **–** | This domain has no compositing surface and no bearing on the category |
| **H. Validation & Reliability** | 10 | **↑↑ (largest positive from this domain)** | Two-sided pre-mutation validation; identity-based rollback with explicitly documented limits; mode/selection/geometry restoration; `FINISHED` asserts on every core mesh operator; collision rejection; cancellation reported as `ok:false`; bounded provider networking. Against that: blocking main-thread I/O, the two rollback blind spots, and no operator-availability preflight |
| **I. Agentability & NL** | 5 | **↑ (mixed)** | Docstrings state coordinate spaces and destructive consequences; stale-index warnings ride in `warnings` (`server/tools/mesh.py` passim); `mesh_bridge`'s `expected_revision` lets an agent detect stale topology (`server/tools/mesh.py:234`); errors name the remediation. Against: no preview/dry-run anywhere, so an agent must mutate to learn; and `nd_pulse_viewport_toggle` cannot be reasoned about idempotently |
| **J. Production Completeness** | 5 | **↓** | Local file import missing; import organisation missing; post-import repair missing; `add_radial_array_modifier` not yet proven against live Blender. Scene composition, mesh editing and provider import are otherwise complete |

**Aggregation note.** The preliminary A–J scores in `COMPREHENSIVE_AUDIT_FINDINGS.md:71-152` were
recorded with this domain explicitly listed as pending (`:40-41`), so none of them ever took a
deduction for this slice's camera/world/render-config "blockers" — and none should be taken now,
because those blockers do not exist. What this domain actually adds to the 100-point total is:
a positive in **H** (verified validation and rollback depth) and **A**/**B** (typed surface, no
free-form `bpy` path left in the tree), a small positive in **E** (validated gravity/unit-scale
prerequisites and simulation-cache checks), and one genuinely new negative in **C**/**J** — no local
file import, and no collection organisation for the imports that do exist. `D` and `F` gain only the
removal of a phantom blocker, not new evidence. The `execute_blender_code` removal (finding 1 above)
also retires the Critical row at the top of `AGENTS.md`'s pending table, which bears on **H**.
