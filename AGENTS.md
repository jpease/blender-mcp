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
  `just check` runs all four, cheapest first, and the same four run in CI:

  ```bash
  just lint        # ruff, restricted to the lines this branch introduces
  just fmt-check   # ruff format, whole tree; clean, and must stay clean
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
  fake `bpy`. `just smoke` runs all 24 `tests/blender_*_smoke.py` scripts against
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

The current MCP exposes **45 registered tools**. The earlier count of 44 omitted one registered tool. All current public tools and their Blender-side handlers were reviewed for correctness, reliability, Blender API usage, agent usability, and production workflow coverage.

- `poetry run pytest -q`: **133 passed**
- No live Blender/GPU validation was completed; modifier geometry, imports, and viewport rendering are code/test verified only.
- The socket framing changes reviewed here are currently uncommitted working-tree changes.
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

## Pending and partially solved findings

| Priority | Finding | Status | Recommendation |
|---|---|---|---|
| Critical | `validate_scene(...)["ready"]` is permanently `False` in a stock Blender | **Pending** | The engine probe reads `bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items` (`handlers/lighting/_shared.py:344-347`), which returns `['BLENDER_EEVEE']` even with Cycles enabled and rendering; `handlers/lighting/inspection.py:299-312` turns the failed probe into an ERROR-severity `ENGINE_UNAVAILABLE`, and `handlers/scene.py:1409` computes `ready` as "no ERROR". Reproduced on Blender 5.2.2 `--factory-startup`: `addon_utils.check("cycles") == (True, True)` while `resolve_engine("CYCLES")` raises. Querying the enum instance-level does **not** help (`bl_rna` resolves to the type either way, measured) - use `bpy.types.RenderEngine.__subclasses__()`, the same trap this codebase already documents at `handlers/liquid/simulation.py:84-88`. Cover it in `tests/blender_scene_validate_smoke.py`. |
| Critical | `create_studio_lighting` validates after it mutates, and blocks its own retry | **Pending** | The tool dispatches the rig (`server/tools/lighting/construction.py:263-275`) and only then runs a mandatory preview whose argument checks live at `server/tools/lighting/rendering.py:142-166,197-198`. A bad `preview_engine`/`preview_samples`/path therefore raises after three lights exist, and the handler refuses existing member names (`handlers/lighting/construction.py:419-424`), so the user deletes them by hand. Validate the preview arguments before dispatch. The handler's own validate-then-rollback is intact; the tool wrapper is what regressed. |
| Critical | Arbitrary Python execution over an unauthenticated auto-started socket | **Partially solved** | The arbitrary-code half is gone: `execute_blender_code` no longer exists anywhere under `src/blender_mcp`, and there is no free-form `bpy` path left. Still open, and still Critical: the socket auto-starts and authenticates nothing, so any local process can drive the full 298-tool surface. Disable auto-start by default and authenticate each connection with a per-session secret; loopback binding is not authentication. |
| High | `copy_object_transform` fails while formatting results for quaternion and axis-angle objects | **Partially solved** | Copying is rotation-mode aware, but `rotation_quat.to_euler(obj.rotation_mode)` passes invalid Euler orders for `QUATERNION` and `AXIS_ANGLE`. Return the native representation, or use a fixed documented Euler order such as `XYZ`. Label returned transforms as local or return both local and world transforms. |
| High | ~~`add_radial_array_modifier` does not correctly rotate around an arbitrary world-space pivot~~ | **Solved: the finding was wrong** | Disproved against real Blender, not re-argued. Blender's Array modifier composes its object offset as `offset = obj.matrix_world⁻¹ @ offset_object.matrix_world` and places copy *i* at `obj.matrix_world @ offset^i`, so the handler's `empty.matrix_world = pivot_rotation_matrix(pivot, axis, angle) @ obj.matrix_world` (`handlers/model.py:164`) telescopes to exactly `R_pivot^i @ obj.matrix_world` - the translate-to-pivot/rotate/translate-back composition the finding asked for. `tests/blender_radial_array_smoke.py` reads the evaluated depsgraph's vertices in world space and matches them against that composition, built independently from `mathutils`, for all four cases the finding named: away from the origin, rotated with non-uniform scale, parented to a moved/rotated/scaled parent, and the `radius`-derived pivot. Worst deviation 2.2e-06 m. Falsified by swapping the multiplication order, which misplaces the first copy by 2.26 m. Note for anyone reading the geometry-nodes slice: the *GN* radial builder has a genuine pivot bug of its own (`handlers/geometry_nodes/workflows.py:960`), from an unset `transform_space` on its Object Info node - a different code path with a different cause. |
| High | Blender's main thread and the MCP event loop are blocked by synchronous I/O | **Pending** | Run Poly Haven and Sketchfab network/download work in worker threads, limiting Blender data changes to the main thread. Run blocking client socket calls through `asyncio.to_thread` or an async transport. Add cancellation and progress reporting for long downloads. |
| High | ~~No atomic transaction or rollback contract~~ | **Solved** | `bundled/addon/transaction.py` with `object_state.py` gives one explicit checkpoint per mutating request, identity-based tracking of created datablocks and their removal on failure, covered by 35 tests in `tests/test_mutation_transaction.py`. Every liquid orchestration call runs inside it (`server_core.py:1554`, `:1946-1967`). Remaining rollback blind spots are recorded per-domain in `.audit_tmp/01_scene_core.md`, not here. |
| High | Geometry-nodes builders ignore every referenced object's world transform | **Pending** | `transform_space` is never set on any `GeometryNodeObjectInfo`/`CollectionInfo` in the handler package (`handlers/geometry_nodes/workflows.py:118-123,632,714,952,991,1355`), so every cross-object reference reads ORIGINAL coordinates. Live-confirmed: a boolean cutter yields 0 verts whether it sits on the target or 5 units away, and the radial builder puts an instance centroid at the modifier object's location rather than the documented pivot. `create_curve_generator` additionally *documents* the opposite (`workflows.py:781-783`). Set `transform_space = "RELATIVE"` where the docstring promises world space, and smoke-cover it. |
| High | `create_curve_generator`'s `radius` is inert on Blender 5.2 | **Pending** | `GeometryNodeCurveToMesh` no longer scales its profile by the curve radius attribute - the node's `Scale` input does, and the builder never touches it (`handlers/geometry_nodes/workflows.py:694-705,729`). Live-confirmed: driving the modifier's Radius input from 0.05 to 0.5 left evaluated bounds bit-identical, so every cable/pipe/rail is ~1 unit thick regardless of the request. Wire `Scale`, and add the domain's first smoke script. |
| High | Two retopology `apply=True` paths report success after a cancelled operator | **Pending** | `configure_surface_projection` (`handlers/retopology/editing.py:173`) and `transfer_mesh_attributes` (`handlers/retopology/production.py:129`) call `helpers.apply_modifier`, which discards `bpy.ops.object.modifier_apply`'s return value, so a CANCELLED apply yields `ok: true` with `"applied": true` and `"modifier": None`. The domain already owns the checked helper two files away (`handlers/retopology/advanced.py:436-441`); use it. Contradicts this document's own operator-result rule. |
| High | `create_camera` leaves orphan datablocks when it rejects its own arguments | **Pending** | The object and camera datablock are created and linked (`handlers/camera/core.py:107-109`) before `_validate_optics` (`:111`) and `_look_quaternion` (`:122`) can raise, and both are reachable (a PANO/`panorama_type` mismatch, a camera coincident with its aim target). There is no `try`/`except`, so the client gets an error envelope plus an orphan object and an orphan `<name> Data`. Validate before creating, or roll back in a `finally`. |
| High | Poly Haven networking and temporary-file handling | **Pending** | Add connect/read timeouts to every request, call `raise_for_status`, stream downloads, enforce byte limits, and clean paths in `finally`. Replace private `tempfile._cleanup()` with explicit cleanup. Pack HDRIs or retain a stable source path before deleting their temporary file. |
| High | Poly Haven world/material/import behavior is destructive or inaccurate | **Pending** | Use `bpy.context.scene.world`, not `bpy.data.worlds[0]`; preserve or explicitly replace the selected world's nodes. Do not silently delete every material slot in `apply_polyhaven_texture`; accept an explicit replacement policy or target slot. Detect imported objects by diffing `bpy.data.objects`, validate operator completion, and return actual imported names. Do not report `asset_id` as a changed object for materials or models. |
| Medium | Poly Haven catalog cannot be paged | **Pending** | Replace the hard-coded first 20 entries with deterministic `limit`/`offset` pagination and return `truncated` and `next_offset`. Because the endpoint has no text query, rename `search_polyhaven_assets` to `list_polyhaven_assets` or retain the old name only as a compatibility alias. |
| High | Sketchfab import robustness and production limits | **Pending** | Validate `target_size > 0`; stream downloads with compressed and uncompressed size limits; clean temporary directories in `finally`; detect imports with a before/after object diff; locate nested GLTF files safely; check the import operator result; and roll back imported objects when normalization fails. Preserve license, author, source URL, and attribution metadata in the result. |
| Medium | Sketchfab search loses pagination information | **Pending** | Expose provider pagination/cursor parameters and return `next`/`previous` or a normalized continuation token. Clamp `count` to the provider-supported range. |
| Medium | Inspection coordinate and evaluation contracts remain unclear | **Partially solved** | Edit Mode synchronization and pagination are fixed, but `get_mesh_data` returns base-mesh local coordinates/normals without documenting that. `get_object_info` claims to include modifiers but does not, reports Euler fields for quaternion/axis-angle objects, and mixes local transforms with a world-space bounding box. Document spaces explicitly and either return modifiers/evaluated geometry or remove those promises. |
| Medium | ND cancellation still produces `ok: true` and optimistic changed-object lists | **Partially solved** | Represent cancellation as a structured non-success outcome or return `changed_objects=[]` unless a before/after diff confirms mutations. `nd_single_vertex` must not dereference or report the active object after cancellation. Apply actual-change reporting to all ND tools. |
| Medium | Disabled integrations remain visible to agents | **Partially solved** | Blender-side capabilities are dynamic, but FastMCP registers every Poly Haven, Sketchfab, and ND tool unconditionally. Filter the MCP tool list using handshake capabilities, or attach explicit unavailable metadata. Refresh capabilities whenever Blender integration settings change. |
| Medium | Displacement input contract and cleanup | **Partially solved** | The default legacy `NOISE` texture supports `noise_scale`, but `texture_type` accepts any string and the implementation sets that property unconditionally. Restrict the parameter to verified procedural texture types, validate positive scale, and remove created textures/subdivision modifiers if later work fails. State that this uses Blender's legacy Texture datablock API, not shader Noise Texture nodes. |
| Medium | Input validation is inconsistent | **Pending** | Validate counts, subdivision levels, dimensions, voxel size, thickness, screenshot size, and finite numeric vectors before mutation. Validate every object name in batch operations before changing the first object. This prevents Blender property clamping and partial list mutations from becoming undocumented behavior. |
| Medium | Socket size cap is incomplete | **Partially solved** | Framing and IDs are correct, but limits are checked only while no newline exists. Reject an oversized `line` after splitting and reject receive buffers as soon as the first frame exceeds the cap. Apply the equivalent check to responses. |

## Removal and consolidation conclusion

The only current tool that should be removed from the default production surface is `execute_blender_code`. It may remain as an explicitly enabled development capability.

No other current tool is clearly redundant. Native mesh tools and ND equivalents serve different destructive and non-destructive workflows. The Poly Haven import/apply split is also useful, but its result semantics and material replacement behavior require correction.

Relative to the comparable MCPs used in the original audit, this implementation now has a stronger structured modeling and inspection surface and relies less on arbitrary scripts. Its remaining production gap is operational safety: trusted transport, asynchronous provider work, transactional recovery, accurate import provenance, and runtime-tested modifier geometry.

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
