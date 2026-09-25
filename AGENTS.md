# CLAUDE.md: Production Guidelines for Blender MCP

## Purpose and scope

This repository implements a **Blender Model Context Protocol (MCP)** server. It connects an MCP client to a Blender add-on over a local socket so an agent can inspect a scene, run commands, and automate Blender workflows.

Treat every scene as production work: preserve user intent and data, make changes repeatable, surface meaningful failures, and verify the result in Blender rather than assuming a command succeeded.

## Runtime contract

- **Python:** 3.13 or newer.
- **Blender:** 5.1 or newer.
- **API target:** Blender 5.1+ only. Do not write compatibility code for removed 3.x/4.x APIs unless the repository explicitly needs it.
- **Source of truth:** Real Blender 5.2 API introspection (`help`, `__doc__`, `dir`, RNA properties) over guessed operator arguments.
- **Threading:** Blender data and `bpy` operators must run on Blender's main thread. Socket/client threads may receive and queue work, but must not mutate Blender state directly.

Use modern, readable Python 3.13+ with type hints. Keep public tool inputs and results JSON-serializable, stable, and explicit.

## Production workflow for scene changes

1. **Inspect first.** Establish the active scene, object names/types, selection, mode, collection structure, units, and existing modifiers/materials before editing. Use focused inspection rather than dumping a large scene unnecessarily.
2. **Plan the smallest safe change.** Prefer specific object names and deterministic parameters. Do not rely on whichever object happens to be active or selected.
3. **Preserve context.** When an operation needs a particular active object, selection, or mode, capture the prior state and restore it with `try`/`finally`, including on failure. Use `context.temp_override` when appropriate.
4. **Keep work non-destructive.** Prefer modifiers, Geometry Nodes, linked/instanced data, and reversible operations. Do not apply modifiers, join meshes, convert object types, purge data blocks, or delete objects unless the request clearly calls for it.
5. **Organize deliberately.** Create or reuse named collections; give objects, materials, node groups, cameras, and helper objects meaningful, collision-safe names. Keep cutters, references, and generated helpers separated from final renderable assets.
6. **Validate and report.** Confirm object existence, type, parameters, and expected result after mutation. Return concise structured results that identify changed objects, retained live modifiers, warnings, and next useful actions.

### Destructive and expensive operations

- Ask for confirmation before deleting or replacing user assets, clearing scenes, applying irreversible modifiers, overwriting files, or launching a long render/export—unless the user explicitly requested that exact operation.
- Scope bulk operations to a dedicated collection or an explicit object list. Never use broad selection-based deletion as an implementation shortcut.
- Save/export only to an explicit user-provided path. Never silently overwrite a `.blend`, render, cache, or asset-library file.
- Use bounded pagination, size limits, and timeouts for scene inspection, asset downloads, geometry imports, and network work. Report partial results rather than masking them.

## Blender API and modeling standards

- Favor Blender data APIs when they are context-independent; use `bpy.ops` only when an operator is the appropriate API. Operators require valid mode, area/region, selection, and active-object context.
- Validate operator results (`{'FINISHED'}`) and turn `{'CANCELLED'}` or context errors into actionable MCP errors. Do not swallow exceptions or return success after a failed operation.
- Validate object types, mesh indices, numeric ranges, transforms, and resource existence before changing data. Reject invalid input clearly and leave the scene unchanged when possible.
- Keep transforms intentional. Distinguish local and world space, preserve parent transforms, and document the coordinate space used by a tool.
- Prefer a sensible modifier order, non-zero bevels for hard-surface assets, clean normals, and apply scale only when required by the requested asset pipeline.
- For procedural or repeated geometry, preserve editable source objects and use instances/arrays/nodes before making unique copies.

## Installed modeling add-ons

Use an add-on only after confirming it is enabled and that its operator is available. Provide a clear fallback or readiness error; never assume an optional add-on is installed.

### ND (Non-Destructive Modelling Toolkit)

- Docs: [ND Docs](https://github.com/hugemenace/nd-docs)
- Prefer for hard-surface, boolean, bevel, extrusion, replication, and utility workflows where it preserves an editable modifier-based result.
- Keep cutters and utility geometry identifiable and isolated. Never clean utility objects without explicit confirmation.

### LoopTools

- Source: [Blender Extensions – LoopTools](https://extensions.blender.org/add-ons/looptools/)
- Use `bpy.ops.mesh.looptools_*` for well-scoped topology regularization: circular loops, even edge spacing, flattening, bridging, or relaxing.
- Ensure the correct mesh is in Edit Mode and the intended elements are selected; restore the prior mode and selection afterward.

### EdgeFlow

- Source: [EdgeFlow](https://github.com/BenjaminSauder/EdgeFlow/)
- Use Set Edge Flow / Set Edge Linear for subdivision and curved surfaces where edge tension and continuity matter.
- Inspect the result for pinching or silhouette changes; do not claim topology is clean without checking it.

## MCP tool and protocol design

- Keep commands narrowly scoped and idempotent where practical. Favor dedicated, validated tools over opaque arbitrary-code paths.
- Treat all client input as untrusted: validate schemas, enums, paths, object names, numeric bounds, and optional fields at the server boundary.
- Use a consistent response shape. Successful responses should state what changed; failures should identify the operation, safe input context, and remediation without leaking secrets or large tracebacks to clients.
- One concept, one field name, across every domain. A schema is read by an agent that has just
  read a different tool's schema, so a synonym costs a failed call and a re-read: an aim written
  as `aim_at: {target_point: ...}` by analogy with `point_camera_at` was rejected by a pose tool
  that spelled the same thing `target`. The settled spellings are `target_point` for a
  world-space point, `target_object_name` for an object, and `target_bone_name` for a bone, with
  a role prefix where there are two (`pole_target_point`). `subtarget` is the exception: it is
  Blender's own RNA field name on a constraint, and a tool that writes that field uses it. Before
  adding a field, grep the tool surface for the concept and reuse the name you find; renaming one
  later is a protocol bump.
- Separate transport failures from Blender operation failures. A valid command that Blender rejects should not unnecessarily drop a healthy socket connection.
- Frame and decode socket messages defensively. Preserve UTF-8 boundaries, enforce maximum message sizes, and ensure start/stop/restart releases sockets and worker resources cleanly.
- Never log credentials, tokens, full client payloads containing secrets, or arbitrary untrusted code. Use structured, actionable logs with operation and object identifiers.
- Network-backed tools (for example asset search/import) must be explicitly opt-in, handle unavailable credentials gracefully, validate downloads before importing, and leave the scene recoverable if an import fails.

## Repository engineering standards

- Place MCP server logic in `src/blender_mcp/server/`; keep Blender runtime/add-on logic in `src/blender_mcp/bundled/addon/`. Do not import `bpy` from code that must run outside Blender.
- Keep command dispatch, validation, and Blender-side mutation small and testable. Isolate pure validation/serialization helpers from Blender-dependent code.
- Preserve backward-compatible tool schemas and response fields unless a breaking change is intentional, documented, and versioned.
- Add focused regression tests for every behavior change, especially validation, connection framing, error handling, main-thread execution, and restoration of scene state.
- Run the relevant test suite and quality checks before handing off a change.
  `just check` runs all five below, cheapest first. The four gates also run in
  CI; `just anchors` runs only here and fails when a revert-matrix row no longer
  applies to the source it quotes, so run it after editing any file a row reverts:

  ```bash
  just lint        # ruff, restricted to the lines this branch introduces
  just fmt-check   # ruff format, whole tree; clean, and must stay clean
  just anchors     # every revert-matrix row still applies to the source it quotes
  just typecheck   # basedpyright, whole tree; at zero, and must stay at zero
  just test        # pytest
  ```

  The two gates have different shapes because their histories do. ruff carries
  an inherited backlog (`just lint-all` reports it in full), so whole-tree
  cleanliness is not a hand-off precondition — but no line you write or rewrite
  may add to it, and touching a legacy file does not make you responsible for the
  findings already in it. basedpyright has no backlog left, so it is enforced
  across the whole tree; there is nothing to ratchet and a line filter would only
  weaken it.

  Do not silence a finding on a line you own without a stated reason. bpy's
  stubs widen collection elements and lose the concrete datablock, so
  `reportArgumentType` is off under `src/blender_mcp/bundled` and on everywhere
  else; a diagnostic outside that boundary is describing a state the code can
  actually reach.

  `just lint`, `just fmt-check` and `just typecheck` never touch Blender, and CI
  has no Blender either. Changes under `src/blender_mcp/bundled/addon/` are
  therefore unverified by all four gates: the unit tests drive them through a
  fake `bpy`. `just smoke` runs all 32 `tests/blender_*_smoke.py` scripts against
  a real headless Blender in about 20 seconds and is the only thing that
  exercises the real API — run it after touching an add-on handler. `just gate`
  is the heavier live-GUI rig that `just test` deliberately skips.

  If a Blender runtime change cannot be exercised in CI, state the manual Blender 5.1 verification performed or still required.

## Failure handling and completion criteria

When a command fails, inspect the traceback and current Blender state, identify the smallest root cause, and retry only after correcting the invalid assumption. Do not repeatedly execute mutations speculatively.

A scene-changing task is complete only when:

- the requested result exists and is inspectably correct;
- names, collections, modifiers, materials, and transforms remain coherent;
- any destructive, external, or deferred action is explicitly disclosed; and
- the response states what changed, what was verified, and any remaining limitation.

## Code-style

All code should follow Clean Code principles. Enforce the Single Responsibility Principle (SRP), keep code DRY, and favor clear, simple, and maintainable implementations. Avoid unnecessary abstraction and duplication.

# Blender MCP Production Audit — Rerun

Audit date: 2026-08-29

## Scope and verification

At the time of this audit the MCP exposed **45 registered tools**, and every one of them plus its
Blender-side handler was reviewed for correctness, reliability, Blender API usage, agent usability,
and production workflow coverage. **That count is historical: the surface is now 305 tools**, and a
server process registers only a subset of them.

Read the count below as "what this audit covered", never as "what a session sees". Two different
numbers are routinely mistaken for each other and for this one:

- **305** — every tool in the catalog, registered only with `BLENDER_MCP_TOOLSETS=all`.
- **36** — the default `core` bundle a process registers when `BLENDER_MCP_TOOLSETS` is unset:
  scene inspection, object editing, viewport, animation, file lifecycle/linking. Camera, rendering,
  lighting, world, character posing, texture, retopology and the simulation domains are **absent by
  design**, not missing. `shot` (81 tools) adds camera, lighting, rendering and character posing;
  see the Tool Bundles table in `README.md`.
- **301** — `get_addon_status`'s `capability_count`: Blender-side socket commands the add-on
  dispatches, which is a different surface from the MCP tools a client mounts. A full
  `capability_count` alongside a short tool list is the expected shape, not a registration fault.

A session no longer has to guess which of the three it is looking at:
`get_addon_status(mounted_tools=True)` pages the MCP tool names this process actually registered,
and `get_addon_status(tool_name=...)` still gives the verdict on one name.

- Verification is `just check` (lint, fmt-check, typecheck, pytest), `just smoke` (every `tests/blender_*_smoke.py` against a headless `--background --factory-startup` Blender) and `just matrix` (the revert matrix: each tracked regression test must fail with its fix reverted). CI (`.github/workflows/`) has not yet run on this fork.
- No live-GUI/GPU validation (`just gate`) and no live provider download was part of this audit; modifier geometry, imports and viewport capture are verified only where a smoke script or unit test exercises them.
- Code style was excluded from the audit.

## Solved findings

| Prior finding | Status | Resolution |
|---|---|---|
| Blender failures returned as successful MCP results | **Solved** | Nested `error`, `succeed=False`, and top-level failure responses now become tool errors. |
| Mode, active object, and selection corruption | **Solved** | Mutation helpers preserve and restore Blender state, including failure paths. |
| Stale Edit Mode mesh inspection | **Solved** | `get_object_info` and `get_mesh_data` call `update_from_editmode()`. |
| No way to discover topology indices | **Solved** | `get_mesh_data` provides paginated vertices, edges, faces, and loops. |
| Reusing invalid topology indices | **Solved** | Topology-changing tools explicitly warn agents to query mesh data again. |
| Unvalidated mesh indices and silent operator cancellation | **Solved** | Indices are validated before mode changes; core mesh operators require `FINISHED`. |
| ND same-object boolean and missing operator handling | **Solved** | Same-object operations are rejected and unavailable operators produce useful errors. |
| ND viewport context and modal operations | **Solved** | Operators receive a `VIEW_3D` override; unexpected modal execution is rejected. |
| Destructive ND cleanup ambiguity | **Solved** | `nd_clean_utils` requires confirmation and reports removed objects/modifiers. |
| Non-idempotent viewport toggle descriptions | **Solved** | Native overlays and ND pulse toggles are separated and accurately documented. |
| Generic operations unnecessarily tied to ND | **Solved** | Cleanup, naming, and native overlay operations were moved into general tools. |
| Misleading legacy names | **Solved** | `viewport_overlay_toggle` → `set_viewport_overlay`, `download_sketchfab_model` → `import_sketchfab_model`, `model_radial_array` → `add_radial_array_modifier`; `model_mirror`/`model_array` were folded into `manage_modifiers`. `tests/test_scene_tools.py::test_breaking_tool_names_are_absent` keeps the old names out of the registry. |
| Hyper3D/Hunyuan defects and redundant generation abstraction | **Solved by removal** | Those integrations and their unified generation tools were removed. |
| Socket message concatenation, Unicode splitting, and response correlation | **Solved** | NDJSON framing and request IDs now correctly correlate responses. |
| Client-thread use of `bpy.app.timers` and restart leakage | **Solved** | Commands are queued and drained on Blender's main thread; sockets are closed during shutdown. |
| Black viewport screenshots when Blender is obscured | **Solved** | GPU off-screen rendering is primary, with window capture as fallback. |
| Provider archive/include path traversal | **Solved** | Poly Haven includes and Sketchfab ZIP members are path-checked. |
| Live-modifier results only reporting base geometry | **Solved** | Modifier tools now include evaluated counts and world-space bounds. |
| Screenshot temporary path is not failure-safe | **Solved** | `get_viewport_screenshot` and `inspect_render_output` take a per-request `tempfile.mkstemp` path and unlink it in `finally`. The capture's own image datablock is now also removed in `finally`, so a failed `image.save()` no longer leaves an orphan `mcp_viewport` image in the user's file. |
| `validate_scene(...)["ready"]` is permanently `False` in a stock Blender | **Solved** | The engine probe read `bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items`, which returns `['BLENDER_EEVEE']` even with Cycles enabled and rendering, so `handlers/lighting/inspection.py:297-302` raised an ERROR-severity `ENGINE_UNAVAILABLE` and `ready` ("no ERROR") was false for every scene. `engine_identifiers(scene=None)` (`handlers/lighting/_shared.py:377`) now unions that enum with a recursive `bpy.types.RenderEngine.__subclasses__()` walk (`:364-370`; the dynamic-RNA trap already documented at `handlers/liquid/simulation.py:84-87`) and always includes the scene's assigned engine; `resolve_engine` (`:398`) still resolves exactly one EEVEE from the RNA enum, so an add-on engine cannot make that ambiguous. The second half of the same finding is fixed too: `ZERO_AREA_UVS` against a library-linked mesh is now a WARNING naming the library the mesh comes from, because "unwrap the listed faces" is not actionable in the linking file and one such finding held `ready` false forever. `tests/blender_scene_validate_smoke.py` asserts `ready is True` on a stock `--factory-startup` scene set to CYCLES, falsified by restoring the old probe on the same scene. |
| `add_radial_array_modifier` does not correctly rotate around an arbitrary world-space pivot | **Solved: the finding was wrong** | Disproved against real Blender, not re-argued. Blender's Array modifier composes its object offset as `offset = obj.matrix_world⁻¹ @ offset_object.matrix_world` and places copy *i* at `obj.matrix_world @ offset^i`, so the handler's `empty.matrix_world = pivot_rotation_matrix(pivot, axis, angle) @ obj.matrix_world` (`handlers/model.py:164`) telescopes to exactly `R_pivot^i @ obj.matrix_world` - the translate-to-pivot/rotate/translate-back composition the finding asked for. `tests/blender_radial_array_smoke.py` reads the evaluated depsgraph's vertices in world space and matches them against that composition, built independently from `mathutils`, for all four cases the finding named: away from the origin, rotated with non-uniform scale, parented to a moved/rotated/scaled parent, and the `radius`-derived pivot. Worst deviation 2.2e-06 m. Falsified by swapping the multiplication order, which misplaces the first copy by 2.26 m. The *GN* radial builder has a genuine pivot bug of its own (`handlers/geometry_nodes/workflows.py:952-966`), from an unset `transform_space` on its Object Info node - a different code path with a different cause, still pending below. |
| No atomic transaction or rollback contract | **Solved** | `bundled/addon/transaction.py` with `object_state.py` gives one explicit checkpoint per mutating request, identity-based tracking of created datablocks and their removal on failure, covered by 21 tests in `tests/test_mutation_transaction.py`. Every mutating command is dispatched inside it (`server_core.py:2086-2104`), and a handler that *returns* a failure shape is converted to an error inside the transaction so its partial mutation rolls back (`:2129-2134`). Remaining rollback blind spots are recorded per-domain in `.audit_tmp/01_scene_core.md`, not here. |
| `create_camera` leaves orphan datablocks when it rejects its own arguments | **Solved** | The camera datablock and object were created and linked before `_validate_optics` and `_look_quaternion` could raise, with no `try`/`except`, so a PANO/`panorama_type` mismatch or a camera coincident with its aim target left an orphan object and an orphan `<name> Data` behind the error envelope. Live dispatch rolled that back through `mutation_transaction`; a direct mixin call - which is how every `tests/blender_*_smoke.py` drives the handler - did not. `create_camera` now wraps link-through-configuration and removes both datablocks before re-raising (`handlers/camera/core.py`), the same idiom `configure_camera_dof` already used two functions away. `tests/server/tools/camera/test_tools.py::test_handler_create_camera_removes_both_datablocks_when_configuration_is_refused` is falsified by stripping the two `remove()` lines, and `tests/blender_camera_smoke.py` asserts against real Blender that a refused `panorama_type` leaves neither datablock in `bpy.data`. |
| Arbitrary Python execution (`execute_blender_code`) | **Solved by removal** | No such tool or add-on command exists (`src/blender_mcp/addon_surface.json` lists none); there is no free-form `bpy` path. The unauthenticated socket half is still pending below. |
| Retopology `apply=True` paths report success after a cancelled operator | **Solved** | `helpers.apply_modifier` (`bundled/addon/helpers.py:456-481`) now raises unless `modifier_apply` returns `FINISHED`, and it is the only apply path: the duplicate checked helpers were deleted, so every caller - including `configure_surface_projection` (`handlers/retopology/editing.py:173`), `transfer_mesh_attributes` (`handlers/retopology/production.py:129`) and the geometry-nodes apply paths (`handlers/geometry_nodes/modifiers.py:314`, `delivery.py:173`) - fails instead of reporting `applied: true`. `tests/server/tools/test_mesh_model.py::test_cancelled_modifier_apply_raises_instead_of_reporting_applied`. |
| `copy_object_transform` fails formatting quaternion/axis-angle results | **Solved** | Returns the native rotation plus `rotation_mode` and world transforms (`handlers/model.py:88-94`); no `to_euler(obj.rotation_mode)` remains. |
| Poly Haven networking and temporary-file handling | **Solved** | `bundled/addon/network.py:30-78`: connect/read timeouts, `raise_for_status`, streamed downloads, declared and streamed byte caps. Temp paths are removed in `finally` (`handlers/polyhaven.py:155-161`, `:474-482`); HDRIs download to a partial file and `os.replace` into a stable cache path (`:584-607`). No `tempfile._cleanup` remains. Main-thread execution is still pending below. |
| Poly Haven world/material/import behavior is destructive or inaccurate | **Solved** | HDRIs configure `scene.world` through the managed graph (`handlers/polyhaven.py:561-582`, `REPLACE_MANAGED`); `apply_polyhaven_texture` takes an explicit `replacement_policy` (`APPEND`/`REPLACE_SLOT`/`REPLACE_ALL`, the last confirmation-gated; `:696-741`); model import checks `FINISHED` (`:423`) and diffs `bpy.data.objects` by `session_uid` (`:443-447`); `server/tools/polyhaven.py:24-33` keeps the asset ID out of `changed_objects`/`changed_resources`. |
| Poly Haven catalog cannot be paged | **Solved** | `list_polyhaven_assets(limit, offset)` returns a sorted page with `truncated` (`handlers/polyhaven.py:512-555`). |
| Sketchfab import robustness and production limits | **Solved** | `handlers/sketchfab.py`: `target_size` finite-positive (`:307-311`); bounded archive download (`:327`) and `_validate_archive` traversal/bomb/member-count checks (`:25-29`); import `FINISHED` check and `session_uid` diff (`:342-348`); provenance (name, source URL, author, license, attribution; `:363-374`); `rmtree` in `finally` (`:401-403`). A returned error rolls imported objects back through the transaction (`server_core.py:2129-2134`). |
| Sketchfab search loses pagination information | **Solved** | `count` clamped to 1-100 and a validated `cursor` continuation URL accepted (`handlers/sketchfab.py:155-161`). |
| Inspection coordinate and evaluation contracts | **Solved** | `get_object_info` documents local transform vs world-space AABB and how to read `rotation` by `rotation_mode` (`server/tools/viewport.py:93-114`), and returns native rotation and modifiers (`bundled/addon/server_core.py:2393-2432`); `get_mesh_data` documents base-mesh local coordinates (`server/tools/viewport.py:145-149`). |
| ND cancellation produces `ok: true` and optimistic changed-object lists | **Solved** | Cancellation returns `ok: false`, `changed_objects=[]` and a warning (`server/tools/nd.py:18-43`); `nd_single_vertex` identifies its object by a before/after diff and returns `name: None` on cancel (`handlers/nd.py:207-244`). |
| Displacement input contract and cleanup | **Solved by removal** | The `texture_type`/`noise_scale` displacement tool no longer exists; displacement is the `DISPLACE` type of `manage_modifiers`, with an allowlisted field set (`server/tools/scene.py:121`). |
| Socket size cap is incomplete | **Solved** | `extract_frames` drops the connection for an oversized frame after splitting and for an oversized terminator-less remainder (`bundled/addon/server_core.py:146-191`); responses over the cap become error frames (`:1716-1717`). |

## Pending findings

| Priority | Finding | Status | Recommendation |
|---|---|---|---|
| Critical | Unauthenticated socket | **Pending** | Auto-start already defaults to off (`bundled/addon/__init__.py:52-55`, and `:105` when no scene is available), but a started server binds `localhost` (`bundled/addon/server_core.py:867`, `:946`) and authenticates nothing (`:1428-1429`), so any local process that reaches the port can send any of the 301 add-on commands, including `open_shot`, `save_shot`, `render_scene` and `relocate_library`. Authenticate each connection with a per-session secret in the handshake and refuse unauthenticated frames; loopback binding is not authentication. |
| Critical | `create_studio_lighting` validates after it mutates, and blocks its own retry | **Pending** | The tool dispatches the rig (`server/tools/lighting/construction.py:257-268`) and only then runs the mandatory preview (`:269-281`), whose argument checks live in `render_lighting_preview` (`server/tools/lighting/rendering.py:200-202`: Cycles sample gate, then `_preview_paths` absolute-`.png`/distinct-path rules at `:143-169`). A bad `preview_output_path`, or Cycles above 64 samples without `confirm_long_render`, therefore raises after three lights exist, and the handler refuses existing member names (`handlers/lighting/construction.py:419-424`), so the user deletes them by hand. Validate the preview arguments before dispatch. The handler's own validate-then-rollback is intact. |
| High | Geometry-nodes builders ignore every referenced object's world transform | **Pending** | `transform_space` is never set on any `GeometryNodeObjectInfo`/`CollectionInfo` in the handler package (`handlers/geometry_nodes/workflows.py:119-123,632,714,952,991,1355`), so every cross-object reference reads ORIGINAL coordinates. Live-confirmed: a boolean cutter yields 0 verts whether it sits on the target or 5 units away, and the radial builder puts an instance centroid at the modifier object's location rather than the documented pivot. `create_curve_generator` additionally *documents* the opposite (`workflows.py:781-783`). Set `transform_space = "RELATIVE"` where the docstring promises world space, and smoke-cover it. |
| High | `create_curve_generator`'s `radius` is inert on Blender 5.2 | **Pending** | `GeometryNodeCurveToMesh` no longer scales its profile by the curve radius attribute - the node's `Scale` input does, and the builder never sets it (`handlers/geometry_nodes/workflows.py:694-704` set the radius, `:731-742` build `CurveToMesh` without `Scale`). Live-confirmed: driving the modifier's Radius input from 0.05 to 0.5 left evaluated bounds bit-identical, so every cable/pipe/rail is ~1 unit thick regardless of the request. Wire `Scale`, and smoke-cover it (`tests/blender_geometry_nodes_advanced_smoke.py` is the domain's smoke script). |
| High | Provider network I/O runs on Blender's main thread | **Pending** | The client half is fixed: tools dispatch through `asyncio.to_thread` (`server/tools/_dispatch.py:91`). Blender side, Poly Haven (`handlers/polyhaven.py:540`, `:691`, downloads via `download_file` at `:158`, `:359`, `:604`) and Sketchfab (`handlers/sketchfab.py:319-327`) fetch inside handlers drained on the main thread, so the UI freezes for a download's length with no cancellation or progress. Fetch in a worker thread and limit the main thread to `bpy.data` changes. |
| Medium | Disabled integrations remain visible to agents | **Pending** | Registration is by `BLENDER_MCP_TOOLSETS` only (`server/bundles.py:212-231`): Poly Haven and Sketchfab ship in the `assets` bundle (`:59`) and ND in `geometry-nodes` (`:42`), mounted whether or not Blender has the integration. Availability is only reported, as `get_addon_status`'s `integrations_available` (`server/tools/core.py:227-229`). Filter the mounted tools by handshake capabilities, or attach explicit unavailable metadata. |
| Low | Non-finite vectors reach core handlers | **Pending** | Bounds now gate counts, cuts, segments, voxel size, thickness and screenshot size (`server/tools/mesh.py:108,150-151,203,336,372`; `server/tools/viewport.py:265`), and `StrictModel` refuses non-finite floats in payload models (`server/tools/_inputs.py:32`). Plain top-level `tuple[float, ...]` tool arguments do not: `create_primitive_object`'s `location`/`rotation`/`dimensions` (`server/tools/mesh.py:21-24`) accept NaN/inf, and the handler forwards them unchecked (`handlers/mesh.py:96`, `:103`). Refuse non-finite values at the tool boundary. |

## Removal and consolidation conclusion

No current tool needs removing from the production surface: `execute_blender_code` is gone and no free-form `bpy` path remains.

No current tool is clearly redundant. Native mesh tools and ND equivalents serve different destructive and non-destructive workflows. The Poly Haven import/apply split is also useful, and its result semantics and material replacement policy are now explicit.

Relative to the comparable MCPs used in the original audit, this implementation now has a stronger structured modeling and inspection surface and no arbitrary-script path. Its remaining production gaps: an authenticated transport, provider downloads off Blender's main thread, capability-gated tool registration, and geometry-nodes builders that honour world transforms and curve radius.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **blender-mcp** (6947 symbols, 15546 relationships, 560 execution flows).

> Index stale? Run `node .gitnexus/run.cjs analyze --index-only` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? Bootstrap with `npx`, `bunx`, or `pnpm dlx` — e.g. `bunx gitnexus@latest analyze` (npm 11 npx crash; #1939).

## Always Do

- **MUST run impact before editing.** Use `impact({target: "symbolName", direction: "upstream"})` or `node .gitnexus/run.cjs impact "symbolName" --direction upstream --repo .`; report callers, processes, and risk. Never substitute grep for graph analysis.
- **MUST analyze graph changes before committing.** Use `detect_changes({scope: "all"})` (MCP) or `node .gitnexus/run.cjs detect-changes --scope all --repo .` (CLI fallback). `partial: true` or `truncated: true` is not a clean check — a zero means unseen, not unaffected; re-run it. For regression review: `detect_changes({scope: "compare", base_ref: "main"})` or `node .gitnexus/run.cjs detect-changes --scope compare --base-ref "main" --repo .`.
- MUST warn on HIGH/CRITICAL `risk` pre-edit; never use `riskSharedAxes` to waive a HIGH/CRITICAL `risk` warning. Compare File/symbol: MCP File omits axes; Graph-RAG expands File.
- **MUST treat `risk: UNKNOWN` as unresolved, not as low.** An empty caller set is not evidence the symbol is unused — it can also mean the callers are not resolvable by the index (plain-object property access, dynamic dispatch, cross-language calls). `impact` pairs `UNKNOWN` with a `riskNote` saying so. Confirm with a text search before treating the symbol as safe to change or delete; do not proceed on the strength of a zero.
- **MUST use `query({search_query: "concept"})` for concepts/flows, `context({name: "symbolName"})` for a named symbol, or `impact` for blast radius, on read-only callers, dependencies, imports, or execution flow.** Graph first; text search only for empty/`UNKNOWN`/literals.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method before MCP/CLI impact analysis.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis, and never read `UNKNOWN` as an all-clear — it means the walk could not answer, which is the one verdict that requires confirming by other means.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit before MCP/CLI graph change analysis.

## Resources

| Resource | Use for |
| --- | --- |
| `gitnexus://repo/blender-mcp/context` | Codebase overview, check index freshness |
| `gitnexus://repo/blender-mcp/clusters` | All functional areas |
| `gitnexus://repo/blender-mcp/processes` | All execution flows |
| `gitnexus://repo/blender-mcp/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
| --- | --- |
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
