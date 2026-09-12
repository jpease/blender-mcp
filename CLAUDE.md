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
- Run the relevant test suite and quality checks before handing off a change:

  ```bash
  pytest
  ruff check .
  ruff format --check .
  basedpyright
  ```

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

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **blender-mcp** (6751 symbols, 15289 relationships, 560 execution flows).

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
