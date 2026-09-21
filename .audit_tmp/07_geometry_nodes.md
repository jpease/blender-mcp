# Geometry Nodes Audit

Domain previously unassigned (`AUDIT_SYNTHESIS.md:44` lists "Geometry Nodes (if any tools exist)" as a missing domain). Tools exist: 28 of them.

## Scope

- **MCP-facing tools** (server): `src/blender_mcp/server/tools/geometry_nodes/` — 12 modules, 1,244 lines
- **Addon-side handlers** (Blender-facing): `src/blender_mcp/bundled/addon/handlers/geometry_nodes/` — 13 modules, 5,079 lines, composed in `registry.py:15-27`
- **Shared graph engine**: `src/blender_mcp/bundled/addon/handlers/node_graph.py` (196 lines, shared with the shader-graph domain) and `src/blender_mcp/server/tools/_node_graph.py` (60 lines)
- **Live coverage**: `tests/blender_geometry_nodes_advanced_smoke.py` (232 lines). No sibling geometry-nodes smoke script exists — `tests/blender_*_smoke.py` has 23 entries and this is the only one.
- **Unit coverage**: `tests/server/tools/geometry_nodes/test_tools.py` (366 lines), fake-`bpy` only.
- **Runtime introspection**: four throwaway headless probes against **Blender 5.2.2 LTS (hash d13f752e3b9c, built 2026-09-15)**, run from `/tmp` against the real handler mixins. Claims below marked **[live]** were measured that way; they are reproducible but the probe scripts are not repo files.

## Tool Inventory

### Authoring (`server/tools/geometry_nodes/authoring.py`, 3 tools)
- `create_geometry_node_group(name, execution_role, geometry_types, tool_modes, sockets, panels, description, color_tag, collision_policy, purpose)` — `authoring.py:68`; tags a stable MCP UUID + schema version + purpose (`_shared.py:64-72`), defaults a MODIFIER group to a Geometry→Geometry pass-through (`_shared.py:280-297`)
- `edit_node_group_interface(node_group_name, edits[], migration_policy)` — `authoring.py:103`; 5 operations: `ADD_SOCKET`/`ADD_PANEL`/`UPDATE`/`MOVE`/`REMOVE` (`authoring.py:54`)
- `patch_geometry_node_graph(node_group_name, operations[≤500])` — `authoring.py:128`; 8 operations: `ADD_NODE`/`UPDATE_NODE`/`SET_INPUT`/`ADD_LINK`/`REMOVE_LINK`/`MOVE_TO_FRAME`/`REMOVE_NODE`/`SET_ACTIVE_OUTPUT` (`_node_graph.py:36-45`)

### Modifier lifecycle (`modifiers.py`, 4 tools)
- `attach_geometry_nodes_modifier` (`modifiers.py:39`), `set_geometry_nodes_inputs` (`modifiers.py:81`), `manage_geometry_nodes_modifier` (`modifiers.py:101`, 7 actions), `copy_geometry_node_group` (`modifiers.py:142`)

### Inspection & validation (`inspection.py`, 5 tools)
- `list_procedural_systems` (`inspection.py:15`), `get_geometry_node_graph` (`inspection.py:36`, 6 sections), `get_geometry_node_type_info` (`inspection.py:57`), `evaluate_procedural_geometry` (`inspection.py:88`), `validate_geometry_node_graph` (`inspection.py:108`)

### Task-level builders (`workflows.py`, 7 tools)
- `create_procedural_scatter` (`workflows.py:29`), `create_curve_generator` (`:66`), `create_procedural_array` (`:91`), `create_surface_paneling` (`:118`), `create_procedural_boolean` (`:142`), `create_procedural_deformer` (`:162`), `create_volume_generator` (`:187`)

### Zones (`zones.py`, 2 tools)
- `create_repeat_zone` (`zones.py:47`), `create_simulation_zone` (`zones.py:89`)

### Attributes & instances (`attributes_and_instances.py`, 2 tools)
- `manage_named_attributes` (`:14`, 6 actions), `manage_procedural_instances` (`:51`)

### Assets, bakes, delivery, performance (5 tools)
- `run_geometry_nodes_tool` (`assets.py:14`), `publish_procedural_asset` (`assets.py:52`), `manage_geometry_nodes_bake` (`bakes.py:18`, 5 actions), `realize_procedural_output` (`delivery.py:15`, 3 modes), `analyze_procedural_performance` (`performance.py:18`)

**Total MCP surface: 28 tools** — counted from `@mcp.tool()` decorators across `src/blender_mcp/server/tools/geometry_nodes/*.py`, and pinned by `tests/server/tools/geometry_nodes/test_tools.py:13-47` (12 foundation + 11 workflow + 5 advanced), asserted registered at `test_tools.py:57-59`. That is 28 of the 294 `@mcp.tool()` decorators in the whole tool tree (~10%), making it the joint-third largest tool package (liquid 37, rigid_body 29, retopology 28, geometry_nodes 28). The toolset lives in the `asset` preset, not `shot` (`src/blender_mcp/server/bundles.py:42,86`).

## Redundancy Audit

### No duplication
- Graph mutation for Geometry Nodes and shader graphs runs through one engine: `apply_graph_operation` (`handlers/node_graph.py:93`) is called by `geometry_nodes/authoring.py:50-66` with a Geometry-family allowlist, and the same `NodeGraphEdit` request model backs both (`server/tools/_node_graph.py:35`). Genuine cross-domain reuse, not a fork. ✓
- Zone construction reuses the authoring copy-on-write helper rather than reimplementing it (`zones.py:9` imports `_apply_graph_operation, atomic_group_edit`). ✓
- `manage_procedural_instances` edits builder graphs through the same `atomic_group_edit` (`attributes_and_instances.py:341`). ✓

### Overlap worth naming
- `create_procedural_array` vs. `add_radial_array_modifier`/`manage_modifiers`: overlapping intent, but the docstring disambiguates explicitly ("Use the existing Array modifier tool for ordinary one-axis mesh repetition", `server/tools/geometry_nodes/workflows.py:108-110`). Acceptable. ✓
- `get_geometry_node_type_info(search=…)` vs. `(category=…)`: **not** two capabilities. `category` is matched against `identifier.removeprefix("GeometryNode").split("_")[0]` (`inspection.py:296-297`), and bl_idnames contain no underscores, so `category` is a case-insensitive substring match on the same string `search` already matches. **[live]** `search="mesh"` and `category="Mesh"` both return `total_count: 31`; `category="Instances"` returns 7 and **excludes `GeometryNodeInstanceOnPoints`** — the single most important instancing node — because "Instances" is not a substring of "InstanceOnPoints". The parameter advertises a taxonomy that does not exist.

## Reliability Analysis

### Architecture: copy-on-write with reference re-pointing ✓
`atomic_group_edit` (`authoring.py:34-47`) copies the group to `<name>.__MCP_WORKING__`, applies the whole edit, and only then re-points users (`_replace_group_references`, `authoring.py:20-32`) and removes the original. On exception the working copy is removed and the original is untouched.

**[live] Verified atomic**: `patch_geometry_node_graph` with a shader-only node (`ShaderNodeBsdfPrincipled`) raised `RuntimeError: Cannot add node of type ShaderNodeBsdfPrincipled to node tree 'Family Probe.__MCP_WORKING__' / Not a shader node tree`, and the original group still held exactly `["Group Input", "Group Output"]`. Nothing leaked.

**[live] Verified survivable**: a modifier input override set to `7.5` through `set_geometry_nodes_inputs` still read `7.5` after `edit_node_group_interface` added a socket, with the modifier correctly re-pointed at the replacement group. Identifier-keyed overrides survive the datablock swap.

### **CRITICAL: the datablock swap silently destroys asset publication**
`_replace_group_references` re-points only object modifiers and nested group nodes (`authoring.py:22-31`); the original datablock is then deleted (`authoring.py:45`). Anything Blender does *not* copy onto `ID.copy()` is lost.

**[live]** After `publish_procedural_asset("Identity Probe", description=…, tags=["t1"])`, then one `patch_geometry_node_graph` adding a single node:

| | before | after |
|---|---|---|
| `is_asset` | `true` | **`false`** |
| `use_fake_user` | `true` | **`false`** |
| asset tags | `["t1"]` | **`[]`** |
| `session_uid` | 137 | 138 |
| `blender_mcp_uuid` | preserved | preserved |

Role flags and `node_tool_idname` *do* survive (**[live]** second probe: `is_tool`, `is_type_mesh`, `is_mode_object`, `node_tool_idname="geometry.mcp_probe_tool"` all intact). Asset marking and fake-user do not. A published group that is then edited — the obvious workflow — is silently un-published, and with `use_fake_user` cleared a zero-user group is discarded on the next file load. `publish_procedural_asset` returns `"is_asset": True` (`assets.py:207`) and the next edit quietly falsifies it. No warning is emitted.

### **CRITICAL: every cross-object reference ignores that object's world transform**
Every builder that reads another object uses `GeometryNodeObjectInfo`/`GeometryNodeCollectionInfo` (`workflows.py:118-123`, `:632`, `:714`, `:952`, `:991`, `:1355`) and **never sets `transform_space`**, confirmed by grep across the whole handler package. **[live]** Blender 5.2's default is `ORIGINAL` (enum: `ORIGINAL`, `RELATIVE`), which yields the source's *own local* geometry — neither its scene placement nor a mapping into the modifier object's space.

**[live] Boolean proof.** Two identical setups, unit-cube target and unit-cube cutter:

| cutter placement | evaluated result |
|---|---|
| cutter object at the target's origin | 0 verts / 0 faces (full overlap → empty) |
| cutter object moved 5 units away | **0 verts / 0 faces — identical** |
| cutter's transform applied into its mesh data (offset 0.5 in X) | 8 verts / 6 faces (correct half-box) |

The Boolean itself works; object placement is simply invisible to it. An agent that positions a cutter next to a target and calls `create_procedural_boolean` gets a wrong result with no warning. The same defect reaches `create_procedural_deformer(template="PROXIMITY_PUSH")` (`workflows.py:1355-1361`) and the curve/profile sources.

**[live] Radial pivot proof.** `create_procedural_array(layout="RADIAL", pivot_object_name=…)` adds the pivot's `Location` output to an object-local position (`workflows.py:963-967`). The exposed control is documented as "Object whose world transform defines the radial pivot" (`workflows.py:960`). With the pivot at world `(10, 0, 0)` and the modifier object at world `(0, 0, 5)`, the instance centroid landed at **`(10, 0, 5)`**, not the pivot's `(10, 0, 0)`. This is the same class of defect as the pending `add_radial_array_modifier` pivot finding in `COMPREHENSIVE_AUDIT_FINDINGS.md`, reproduced independently in the Geometry Nodes builder.

`create_curve_generator` goes further and returns a claim that is the opposite of the behaviour: `result["coordinate_space"] = "Source curve coordinates are transformed into the modifier object's local space."` (`workflows.py:781-783`). **[live]** With the curve object at world `z=30` and the modifier object at `z=0`, the evaluated bounds were `z ∈ [-1, 1]` and `curve_source.transform_space == "ORIGINAL"`. The returned contract string is wrong, which is worse than silence — an agent reasoning from it will mis-place geometry.

### **CRITICAL: `create_curve_generator`'s `radius` parameter is inert**
The builder wires `GeometryNodeSetCurveRadius` and exposes it as "Radius" / "Generated curve radius" (`workflows.py:694-705`), with the server default `radius: float = 0.05` (`server/tools/geometry_nodes/workflows.py:72`). The profile is a circle hardcoded at radius 1.0 (`workflows.py:729`).

**[live]** In Blender 5.2, `GeometryNodeCurveToMesh` has inputs `["Curve", "Profile Curve", "Scale", "Fill Caps"]` — the builder sets and exposes none of `Scale`. Setting the modifier's Radius input from `0.05` to `0.5` (confirmed written: `modifier_input_state → 0.5`) left the evaluated bounds **bit-identical** (`min [-1.6424, -0.9992, -1.0]`, `max [1.0398, 1.2204, 1.0]` both times). The curve radius attribute no longer scales the profile in 5.2; the `Scale` input does. Every cable/pipe/rail this tool produces is ~1 unit thick regardless of the requested radius, and the one control the tool exposes for thickness does nothing. The tool is not smoke-covered, so nothing caught it.

### Rollback quality elsewhere ✓
- Builders remove the partially built group on any failure and never attach the modifier until construction succeeds (`workflows.py:216-219`, e.g. `:604-606`). **[live]** An unsupported interface socket type raised `Unsupported interface socket type in this Blender runtime: NodeSocketNope` and left **no** leaked group.
- `attach_geometry_nodes_modifier` removes the modifier and any group it created on failure (`modifiers.py:217-222`).
- `set_geometry_nodes_inputs` snapshots every target input and restores them in reverse on failure (`modifiers.py:103-141`, `:238-256`) — a real batch-atomicity guarantee, not just prevalidation.
- `manage_geometry_nodes_bake` restores all nine mutated bake/modifier settings if the operator throws (`bakes.py:280-290`).
- `realize_procedural_output` removes the output object and created mesh on failure and restores frame/subframe in `finally` (`delivery.py:218-227`).
- `create_volume_generator` unwinds delivery object, volume datablock, modifier, and group (`workflows.py:1672-1680`). One residue: the `.vdb` written at `workflows.py:1639-1641` is **not** unlinked if a later step fails.

## Node Types, Socket Types & Interface Operations: what is reachable

**[live] Node types.** Blender 5.2 exposes 274 `GeometryNode*` classes, 264 instantiable in a `GeometryNodeTree`; 26 of 102 `ShaderNode*` are valid there; 56 `FunctionNode*` exist.

- Authoring allowlist: `GeometryNode`, `ShaderNode`, `FunctionNode`, `NodeFrame`, `NodeGroupInput`, `NodeGroupOutput` (`authoring.py:56-63`), enforced by `validate_node_type` (`node_graph.py:74-79`), which additionally requires the class to exist in the running Blender.
- **Reachable**: effectively the whole geometry node set, including nested groups. **[live]** `ADD_NODE` of `GeometryNodeGroup` with `properties.node_tree = {"id_type": "NODE_GROUP", "name": "Inner Group"}` succeeded and bound correctly — real graph composition works, because `resolve_rna_value` (`node_graph.py:8-34`) resolves ten ID collections including `NODE_GROUP`.
- **Not reachable**: `NodeReroute`. **[live]** It *is* creatable in a geometry tree, but is absent from both the authoring allowlist and the discovery filter (`inspection.py:289`), and `patch_geometry_node_graph` rejects it: `Node type 'NodeReroute' is outside the Geometry Nodes authoring allowlist`. Cosmetic, but it means MCP-authored graphs can never be tidied the way a human would.
- Shader-only nodes pass the static allowlist and fail at `nodes.new()`; the failure is clean and atomic (see above). `get_geometry_node_type_info` pre-empts this honestly by instantiating each candidate in a disposable tree and reporting `creatable` plus `creation_error` (`inspection.py:304-338`) — the same runtime-probe pattern the materials slice praised, applied here too. ✓

**[live] Interface socket types.** Of 82 `NodeSocket*` classes, 18 are accepted by `interface.new_socket` on a geometry tree: Bool, Bundle, Closure, Collection, Color, Float, Font, Geometry, Image, Int, Material, Matrix, Menu, Object, Rotation, Sound, String, Vector. `InterfaceSocketSpec.socket_type` is pattern-validated (`server/tools/geometry_nodes/authoring.py:23`) rather than enum-capped, and the handler converts a rejected type into a clean `ValueError` (`_shared.py:187-196`). **All 18 are reachable.** ✓

**Zone state types are capped, and the cap is arbitrary.** `ZoneSocketType` is a 7-value `Literal` (`server/tools/geometry_nodes/zones.py:15`), re-asserted addon-side (`handlers/.../zones.py:11`). **[live]** Blender 5.2 accepts **16** types on `repeat_items` (adds MATRIX, STRING, OBJECT, COLLECTION, MATERIAL, IMAGE, MENU, BUNDLE, CLOSURE) and **10** on `state_items` (adds MATRIX, STRING, BUNDLE). Carrying a transform matrix or a string through a Repeat Zone — ordinary procedural work — is unreachable through MCP. This is the same hardcoded-enum-subset pattern already flagged for keyframe interpolation in `05_animation_rigging.md`.

**Interface operations.** All five (`ADD_SOCKET`, `ADD_PANEL`, `UPDATE`, `MOVE`, `REMOVE`) are implemented, including nested panels and `move_to_parent` (`authoring.py:94-117`). `ADD_SOCKET` applies only 7 properties (`_shared.py:197-205`: default_value, min/max, subtype, attribute_domain, hide_value, default_attribute_name). **[live]** A Float interface socket in 5.2 has 21 writable RNA properties; the other 14 — `default_input`, `force_non_field`, `structure_type`, `hide_in_modifier`, `is_inspect_output`, `optional_label`, `layer_selection_field`, `is_panel_toggle`, `menu_expanded`, … — are still reachable, but only as a second `UPDATE` edit, since `UPDATE` routes `changes` through `set_writable_property` (`authoring.py:100-102`, `node_graph.py:38-45`). Reachable, undocumented, two-step.

**Silently ignored parameter: `color_tag`.** Typed as a bare `str` (`server/tools/geometry_nodes/authoring.py:77`) and applied only if it is a member of the runtime enum (`_shared.py:267-268`). **[live]** The real enum is `NONE, ATTRIBUTE, COLOR, CONVERTER, DISTORT, FILTER, GEOMETRY, INPUT, MATTE, OUTPUT, SCRIPT, SHADER, TEXTURE, VECTOR, PATTERN, INTERFACE, GROUP`; passing `color_tag="BLUE"` produced `color_tag == "NONE"` with no error and no warning. The agent is given a free-form string, no enumeration of valid values anywhere in the surface, and silence on a miss.

## Can an agent build a non-trivial setup end to end?

**Yes, two ways, and both work.**

1. **Task-level**: seven builders each produce a complete, attached, parameterised system in one call, with the interface exposed as named controls and the result reporting `node_map`, `dependencies`, `estimated_instance_count`, and evaluated counts/bounds (`workflows.py:199-213`). **[live]** `create_procedural_scatter` on a cube with `density=40` produced 240 depsgraph instances matching its own `estimated_instance_count: 240`; `manage_procedural_instances(realize_instances=True)` then rebuilt the graph and the same object evaluated to 1,928 verts / 1,446 faces with 0 instances. Full round trip, no manual graph knowledge required.
2. **Low-level**: `create_geometry_node_group` → `edit_node_group_interface` → `patch_geometry_node_graph` (≤500 ordered ops, atomic) → `attach_geometry_nodes_modifier` → `set_geometry_nodes_inputs`, with `get_geometry_node_type_info` supplying real socket layouts first. Zones, nested groups, frames, and active-output selection are all reachable. This is a genuinely complete authoring surface — materially better than the compositor's read-only situation (`03_rendering_compositing.md`), and it is the *same* engine (`handlers/node_graph.py`) that a compositor-authoring tool would need.

**The caveat is which of the seven builders are correct.** Scatter, paneling, and volume are sound. Curve generator has an inert primary parameter and a false coordinate-space claim; boolean and proximity-deformer ignore object placement; radial array's pivot is in the wrong space. Three of seven task-level builders produce wrong geometry for the obvious usage, and those are precisely the ones an agent reaches for first.

## Input Validation & Identifier Handling

**Strong.** ✓
- Every request record forbids extra fields and rejects nested NaN/Inf (`server/tools/geometry_nodes/_shared.py:18-38`, mirrored in `_node_graph.py:10-31`), covering open `properties`/`value` payloads that Pydantic would otherwise wave through.
- Server-side preconditions fire before transport: inverted socket ranges (`authoring.py:33-38`), exactly-one group source (`modifiers.py:57-58`), destructive confirmations for `REMOVE`/`APPLY`/`REMOVE`+`CONVERT` attributes/tool runs/`APPLIED_MODIFIER_COPY` (`modifiers.py:118-119`, `attributes_and_instances.py:31-32`, `assets.py:32-33`, `delivery.py:34-38`), bake budget completeness (`bakes.py:47-67`), duplicate/oversized zone state (`zones.py:31-42`), unique frame lists (`performance.py:45-46`), and every builder's numeric range guard (`workflows.py:56-60`, `:85-86`, `:113-114`, `:135-136`, `:180-181`, `:203-211`).
- Addon-side re-validation is not a rubber stamp: `require_group` rejects linked/read-only groups, `require_object` rejects unsupported types (**[live]** attaching to an EMPTY raised `Object 'Just An Empty' has unsupported type EMPTY`), `require_nodes_modifier` rejects a non-NODES modifier (`_shared.py:26-59`), tool runs validate type flags, mode flags, and every element index before any mode change (`assets.py:17-44`).
- **Identifiers are handled properly** — the standout. Sockets resolve by stable `identifier` with an explicit index fallback and an ambiguity error (`node_graph.py:48-62`); modifier inputs are keyed by interface identifier (`modifiers.py:25-29`); `_node_record` reports `identifier` alongside display `name` and the docstring tells the agent display names are descriptive only (`server/.../inspection.py:45-47`). `validate_geometry_node_graph` even warns on duplicated exposed names (`inspection.py:417-423`). Blender's 5.1-vs-newer modifier-input APIs are both handled behind one accessor pair (`_shared.py:398-441`).

**Weak spots.**
- `manage_named_attributes(values=...)` is `list[Any] | None` with **no length bound** (`server/tools/geometry_nodes/attributes_and_instances.py:22`), and `_write_attribute` writes one value per element (`handlers/.../attributes_and_instances.py:71-93`). A point-domain write to a dense mesh is an unbounded request payload — the one place in this domain without a budget, in a repo that budgets replies to 8 KiB.
- `manage_named_attributes(action="LIST")` is the only inspection path with no pagination (`handlers/.../attributes_and_instances.py:159-174`) and returns Blender's internal attributes unfiltered. **[live]** A plain cube returns `UVMap, .select_vert, .select_edge, .select_poly, sharp_face, .uv_select_vert, …, position, .edge_verts, .corner_vert, .corner_edge` — the `is_internal`/`is_required` flags are present, so the noise is filterable by the caller, but it is unfiltered and unbounded on the wire.
- `create_procedural_scatter(distribution="VOLUME")` silently ignores `mask_attribute`: the mask branch lives only in the surface path (`workflows.py:433-440`), and no warning is returned. Same for `create_procedural_deformer`, where `mask_attribute` on a non-`MASK_OFFSET` template and `target_object_name` on a non-`PROXIMITY_PUSH` template are merely stored as custom properties and do nothing (`workflows.py:1445-1451`).
- `create_procedural_deformer(coordinate_space="WORLD")` is advertised in the tool signature (`server/.../workflows.py:170`) and **always** raises (`handlers/.../workflows.py:1280-1283`). **[live]** confirmed. An enum value that can never succeed is worse than an absent one.

## Instancing, Realize & Modifier Binding

**Well modelled.** ✓
- Instances stay unrealized by default across scatter/array/paneling; scatter builds a `GeometryNodeSwitch` so realization is a runtime control rather than a graph rebuild (`workflows.py:531-549`), and array/paneling build the realize node only when asked (`:1013-1017`, `:1168-1172`).
- `manage_procedural_instances` can flip realization after the fact, inserting or removing the realize node and rewiring the output inside `atomic_group_edit`, then pushing the changed control values onto every live modifier (`attributes_and_instances.py:308-350`). **[live]** Verified end to end (240 instances → 1,928 realized verts).
- Collection sources get an explicit policy (`WHOLE_COLLECTION`/`PICK_INSTANCE`/`SEPARATE_CHILDREN`) wired to `Separate Children`/`Reset Children`/`Pick Instance` (`workflows.py:483-487`), and the Boolean builder realizes *only* the cutter branch (`workflows.py:1219-1224`) — exactly the non-destructive discipline `AGENTS.md:42` asks for.
- Modifier binding checks `is_modifier`, object applicability, and stack bounds before mutating (`modifiers.py:175-180`), supports `single_user` copy-on-attach with provenance (`:177-184`), and `manage_geometry_nodes_modifier` preserves the live graph for every non-destructive action while gating `REMOVE`/`APPLY` (`:308-317`).
- `analyze_procedural_performance` flags early realization by graph distance to the active output (`performance.py:152-159`) — a real instancing-cost heuristic, not a keyword scan.

**Gap:** `create_procedural_scatter(output_type="POINTS")` creates a `FunctionNodeRandomValue` and exposes **three dead controls** — "Scale Min", "Scale Max", "Scale Seed" (`workflows.py:491-521`) — but only links the node when the output is not POINTS (`workflows.py:522-523`), and omits it from the returned `node_map` (`:586-587`). **[live]** Confirmed: the node exists with 0 outgoing links while the interface advertises `["Geometry", "Density", "Distance Min", "Seed", "Scale Min", "Scale Max", "Scale Seed", "Include Original"]`. The agent sees three knobs that do nothing and cannot tell from the result that they are dead.

## Error Surfacing

**Good at the transport and precondition layer.** `call_geometry_nodes` converts any transport/handler exception into a `ToolError` with the command name (`server/.../_shared.py:46-63`), and handler errors are specific and actionable throughout (`Bake ID 7 not found on 'X'; available IDs: [...]`, `bakes.py:96-101`; `Socket 'Rotation' occurrence 0 not found; available: [...]`, `_shared.py:157-162`). `validate_geometry_node_graph` emits 13 structured codes with severity and location (`inspection.py:406-502`), and `analyze_procedural_performance` adds 13 more heuristic codes with evidence (`performance.py:126-253`). Builder and bake warnings do reach the client: `ok()` lifts a payload's `warnings` list into the envelope's own `warnings` and drops it from `data` (`server/tools/envelope.py:312-321`).

**Three real holes.**

1. **A type-invalid link is reported as success.** **[live]** `tree.links.new()` on incompatible sockets does not raise; it creates a link with `is_valid == False`. `apply_graph_operation` calls it without a validity check (`node_graph.py:162`). A patch linking a Geometry output into a Math value input returned `{"link_count": 2, "node_count": 4, …}` with **no warnings key at all** and left an invalid link in the graph. Only a separate `validate_geometry_node_graph` call surfaces it (`INVALID_LINK`, ERROR, `inspection.py:436-444`). The atomic patch's whole value proposition is "if any operation fails, the original graph is restored" (`server/.../authoring.py:135-138`) — a link that silently does not connect is exactly the failure that proposition should cover.
2. **The tool-operator guard is dead code.** `_tool_operator` intends `getattr(module, operator_name, None)` to yield `None` for an unregistered operator and raise a clean, actionable `RuntimeError` (`assets.py:78-83`). **[live]** `getattr(bpy.ops.geometry, "definitely_not_here", None)` returns `<bpy.ops.geometry.definitely_not_here function>`, never `None`; the failure instead surfaces from `assets.py:134` as a raw `AttributeError: Calling operator "bpy.ops.geometry.not_registered_probe" error, could not be found`. The written remediation ("publish the group and let Blender refresh tool assets") can never be delivered.
3. **`validate_geometry_node_graph` warns about its own default output.** **[live]** A group straight out of `create_geometry_node_group` validates with `DUPLICATE_INTERFACE_NAME: Exposed name 'Geometry' is duplicated` — because the tool's own default interface creates an input and an output both named "Geometry" (`_shared.py:282-285`). Self-inflicted warning noise on every default group; the check should compare names within a direction, not across.

## Is the result inspectable, or blind?

**Mostly inspectable, and deliberately so.** Every mutating call returns `evaluated_summary` — world-space bounds plus evaluated vertex/edge/face counts read from the depsgraph without applying anything (`_shared.py:339-363`), used by builders (`workflows.py:209`), attach (`modifiers.py:212`), modifier management (`:327`), and delivery (`delivery.py:201`). `evaluate_procedural_geometry` adds materials, evaluated attributes, and a bounded depsgraph instance list with `total_count`/`truncated` (`inspection.py:348-404`). `realize_procedural_output` reports retained vs. lost named attributes and `topology_indices_stale` (`delivery.py:201-208`). `analyze_procedural_performance` returns min/median/max/mean timings, per-sample instance counts, nested-group depth with recursion detection, and an explicit `limitations` list stating Blender gives no trustworthy per-node timing (`performance.py:363-392`). This is the most honest instrumentation in the repo.

**Two inspection defects.**

1. **[live] Bounds and mesh counts exclude unrealized instances.** A 240-instance scatter reported `mesh_counts {vertices: 8, edges: 12, faces: 6}` and `world_bounds` covering only the 1-unit ground — the instanced geometry is invisible to both. The truth is in `instances.total_count: 240`, and the payload's `limitations` note only says non-mesh components "may not be enumerable" (`inspection.py:398-400`). An agent reading bounds to frame a camera on a scatter gets the wrong box.
2. **`get_geometry_node_graph` misreports its own pagination after the envelope shortens it.** The handler puts the records in `data["graph_items"]` and the pagination keys in `data["pagination"]` (`inspection.py:269-271`), but `_pagination_names` only looks for `truncated` *beside* the list (`envelope.py:139-158`). Running a reconstructed `_node_record`-shaped 100-node payload through the real `envelope_for`: **3 of 100 records survive** (7,628 bytes), the warning says "rerun with a narrower scope to see the rest" with **no resume offset**, and `data["pagination"]` still reads `{"returned_count": 100, "truncated": false, "next_offset": null}`. The same harness on a `list_procedural_systems`-shaped payload behaves correctly (`returned_count: 5, truncated: true, next_offset: 5, "continue with offset=5"`) because `pagination()`'s keys are hoisted to the top level there (`inspection.py:227-239`, `:341-345`). The single most important inspection tool in the domain is the one that lies about what it returned.

**No reply-budget coverage at all.** `scripts/measure_reply_sizes.py` carries no representative payload for any of the 28 tools: running it on the `geometry-nodes` selection exits with `refusing to measure: no representative arguments for tool 'run_geometry_nodes_tool'`, and on `asset` it stops even earlier. `tests/server/test_reply_budget.py:49-53` only measures the `shot` selection, which does not contain this domain (`bundles.py:85-86`). Nothing in CI would notice a geometry-nodes reply blowing the 8 KiB budget — and the `get_geometry_node_graph` defect above is exactly what that harness exists to catch.

## What the smoke script actually proves against real Blender

`tests/blender_geometry_nodes_advanced_smoke.py` genuinely exercises, with assertions:

| Proven | Evidence |
|---|---|
| Group creation and modifier attach | `:48-55` |
| Repeat Zone: 2-item state schema, iteration cap, complexity estimate, caller-supplied internal links | `:57-82` |
| Simulation Zone with a POINT-domain state item; no implicit bake | `:84-93` |
| Both zones remain paired after the graph patch, via `get_geometry_node_graph` | `:94-97` |
| Bake `INSPECT` → stable `bake_id` for the Simulation Output | `:99-106` |
| Bake `BAKE` (PACKED, 1 frame, budgets, confirmation) returns `FINISHED` | `:107-120` |
| Performance analysis runs and **restores the scene frame** | `:122-130` |
| `realize_procedural_output(REALIZED_MESH)` creates a MESH and retains the live source | `:132-140` |
| Scatter `HAIR_CURVES` output with density/selection attributes | `:146-161` |
| Volume generator with a renamed density grid (get/store named grid nodes present) | `:167-178` |
| OpenVDB delivery: file on disk, native VOLUME object, `active_voxels > 0`, `grid_class == "FOG_VOLUME"` | `:186-202` |
| OpenVDB **refuses to overwrite** without `confirm_overwrite` | `:204-213` |
| Bake `DELETE` returns `FINISHED` | `:220-227` |

That is 10 of 28 tools. **Never exercised against real Blender**: `edit_node_group_interface`, `patch_geometry_node_graph` (only indirectly, through the zones' `graph_operations`), `set_geometry_nodes_inputs`, `manage_geometry_nodes_modifier`, `copy_geometry_node_group`, `list_procedural_systems`, `get_geometry_node_type_info`, `evaluate_procedural_geometry`, `validate_geometry_node_graph`, `manage_named_attributes`, `manage_procedural_instances`, `run_geometry_nodes_tool`, `publish_procedural_asset`, `analyze_procedural_performance`'s heuristics, and **five of seven builders** — curve generator, array, paneling, boolean, deformer. All three critical defects above live in that uncovered set. The unit tests (`tests/server/tools/geometry_nodes/test_tools.py`) are good at what they cover — registration, read-only dispatch classification, confirmation gates, serialization — but they run against a fake `bpy` and cannot see any of it.

`run_geometry_nodes_tool` additionally needs a registered node-tool operator, which only appears after Blender refreshes tool assets; **[live]** a headless publish-then-run fails. That is a legitimate reason it is unsmoked, but it means the whole tool path is unproven.

## Gaps & Limitations

- **No node-group import/export or library linking** for procedural systems. `publish_procedural_asset` marks a group as an asset in the *open file only* (`assets.py:207`, `"saved_externally": False`) — and that marking does not survive the next edit.
- **No field/attribute-flow inspection.** `get_geometry_node_graph` reports sockets and links, but nothing tells an agent which sockets are fields vs. single values, so building a correct field chain still requires Blender knowledge the surface does not supply.
- **No preview.** Nothing renders or screenshots a procedural result; verification is numeric (bounds/counts) only.
- **`manage_procedural_instances` returns `nesting_depth: None` and `estimated_instance_count: None`** unconditionally (`attributes_and_instances.py:139-140`), while its docstring promises "estimated instance count, nesting depth" (`server/.../attributes_and_instances.py:70-71`). Two advertised fields are permanently null. (`analyze_procedural_performance` does compute nesting depth — `performance.py:83-113` — so the data exists, just not here.)
- **`create_volume_generator` writes files to disk but is not in `_FILE_TOOLS`** (`server/tools/_documentation.py:51-62`), so it misses the shared "this writes files" documentation sentence, unlike `manage_geometry_nodes_bake`. The runtime guards (`confirm_write`, `.vdb` extension, existing directory, `confirm_overwrite`) are all present (`workflows.py:1496-1507`) — the classification, not the safety, is missing.
- **Dead `warnings = []` locals** in the array and volume builders (`workflows.py:1036`, `:1648`) — the array one is never appended to, so the RADIAL pivot-space caveat has a channel and does not use it.

## Architecture Assessment

### Strengths
1. **Copy-validate-commit, generalised.** The shader domain's pattern applies here to graph *and* interface edits, with reference re-pointing and verified rollback. ✓
2. **Runtime schema discovery over remembered layouts.** `get_geometry_node_type_info` instantiates every candidate in a disposable tree (`inspection.py:304-338`). ✓
3. **Stable identifiers everywhere**, with display names explicitly demoted to descriptive status. ✓
4. **Budgets and confirmations are pervasive and specific** — bake frame/byte/time budgets, iteration caps, instance limits, per-action destructive gates. ✓
5. **Honest limitation reporting.** Three separate handlers return explicit `limitations`/`public_api_limitations` lists stating what Blender's public RNA cannot prove (`inspection.py:398-400`, `bakes.py:209-212`, `performance.py:386-391`). This is the right instinct and it is rare. ✓
6. **Two-layer surface** (task builders over a complete low-level authoring engine) with documented disambiguation. ✓

### Weaknesses
1. **The builders do not respect world space.** `transform_space` is never set on any Object/Collection Info node; boolean, proximity, curve, and radial-pivot results are wrong for any non-coincident source. One tool documents the opposite of what it does. ⚠️ **CRITICAL**
2. **`create_curve_generator`'s `radius` is inert on Blender 5.2** — `GeometryNodeCurveToMesh` moved to a `Scale` input the builder does not use. ⚠️ **CRITICAL**
3. **Editing a published group silently un-publishes it** and clears `use_fake_user`. ⚠️ **CRITICAL**
4. **Success is reported for graphs that are not connected** (type-invalid links) and for parameters that did nothing (`color_tag`, VOLUME-mode `mask_attribute`, POINTS-mode scale controls, non-matching deformer template inputs).
5. **`get_geometry_node_graph` misreports pagination** after envelope shortening, and no reply-budget test covers the domain.
6. **Zone state types capped at 7 of Blender's 16/10** — the hardcoded-enum-subset pattern again.
7. **Live coverage is 10 of 28 tools**; all critical defects sit in the uncovered 18.

## Recommended Immediate Actions

1. **Set `transform_space = "RELATIVE"`** on every builder-created Object/Collection Info node (`workflows.py:118-123`, `:632`, `:714`, `:952`, `:991`, `:1355`), or expose it as a documented control; fix the false `coordinate_space` string at `workflows.py:781-783`. Add live assertions that a displaced cutter actually cuts in the right place.
2. **Fix `create_curve_generator`'s radius**: drive `GeometryNodeCurveToMesh`'s `Scale` input (or scale the profile circle) instead of relying on `Set Curve Radius`, and assert in a smoke script that changing Radius changes the evaluated bounds.
3. **Preserve asset state across `atomic_group_edit`**: re-apply `asset_mark()`, asset metadata, and `use_fake_user` onto the working copy before the swap (`authoring.py:34-47`), or warn explicitly that publication was dropped.
4. **Reject invalid links inside the patch**: after `tree.links.new(...)` in `apply_graph_operation` (`node_graph.py:162`), check `link.is_valid` and raise, so the atomic restore actually covers a mis-typed connection.
5. **Hoist `get_geometry_node_graph`'s pagination keys** beside `graph_items` (as `list_procedural_systems` already does at `inspection.py:227-239`) so envelope shortening updates them, and add representative payloads for all 28 tools to `scripts/measure_reply_sizes.py`.
6. **Widen `ZoneSocketType`** to the types Blender actually accepts (16 repeat / 10 simulation), or derive it at runtime.
7. **Smaller**: make `color_tag` a `Literal` over the real enum; warn instead of ignoring `mask_attribute` in VOLUME scatter and off-template deformer inputs; skip the dead scale controls for POINTS scatter; scope `DUPLICATE_INTERFACE_NAME` per direction; replace the unreachable `operator is None` check in `_tool_operator` with a `bpy.ops` registration probe; bound `manage_named_attributes(values=...)`; populate or remove `nesting_depth`/`estimated_instance_count` in `manage_procedural_instances`; add `create_volume_generator` to `_FILE_TOOLS`.

## Score estimate

| Category | Score | Evidence |
|----------|-------|----------|
| **Tool Completeness** | 9/10 | 28 tools: full low-level authoring, 7 task builders, zones, bakes, delivery, assets, performance. Gaps are narrow (reroute, field inspection, node-group export). |
| **Reliability** | 4/10 | Exemplary rollback and batch atomicity, but three critical correctness defects proven live: inert `radius`, world-transform-blind object references, asset marking destroyed by the next edit. |
| **Granularity** | 9/10 | Two clean layers over one shared graph engine; no raw node CRUD; documented disambiguation against the Array modifier tool. |
| **Validation** | 8/10 | Strict request models, NaN/Inf rejection, prevalidate-then-mutate, 26 structured finding codes across two analyzers. Loses points for unbounded attribute payloads and a self-inflicted duplicate-name warning. |
| **Agentability** | 6/10 | Task builders + runtime schema discovery are the best "just say what you want" surface in the repo; undermined by parameters that silently do nothing and by bounds that hide instances. |
| **Inspectability** | 6/10 | Evaluated counts, bounds, instances, timings, honest limitation lists — but `get_geometry_node_graph` misreports its own pagination and bounds exclude unrealized instances. |
| **Live Verification** | 4/10 | One smoke script, 10 of 28 tools, 2 of 7 builders. Every critical defect is in the uncovered set, and no reply-budget coverage exists. |

**Score estimate**: 6/10 — the strongest *architecture* of any domain audited so far (atomic copy-on-write over a shared graph engine, runtime-probed node schemas, stable identifiers, pervasive budgets, unusually honest limitation reporting), carrying three critical behavioural defects that real Blender exposes immediately: a whole tool whose primary parameter is inert, every cross-object reference blind to world placement, and asset publication silently destroyed by the next edit. The low-level authoring path is production-ready; three of the seven task-level builders are not. All three defects are cheap to fix and, once the builders are correct and smoke-covered, this domain is an 8.

**Domain Status**: Capability complete and well-architected. **Blocked on transform-space and curve-radius correctness**, plus the publish/edit data-loss bug. Live coverage must roughly triple before the builder layer can be trusted.

## Mapping onto the 100-Point Rubric (`COMPREHENSIVE_AUDIT_FINDINGS.md:71-152`)

| Category | Direction | Why |
|---|---|---|
| **A. Architecture & Abstraction (15)** | **Raises** | Copy-validate-commit generalised from shader graphs to graph *and* interface edits (`authoring.py:34-47`), one shared engine (`handlers/node_graph.py:93`) serving two domains, 10-module SRP split (`registry.py:15-27`), stable UUID/role/provenance tagging (`_shared.py:64-72`). Partially offset: the datablock swap churns identity (`session_uid` changes, asset state lost). Net positive — this is the best-factored domain in the repo. |
| **B. Tool Quality & Redundancy (15)** | **Lowers** | No redundant tools and no raw node CRUD ✓, but several parameters do not do what they advertise: `radius` inert, `coordinate_space="WORLD"` always errors, `color_tag` silently dropped, `category` is a `search` alias, `mask_attribute` ignored in two paths, three dead scatter controls, `nesting_depth`/`estimated_instance_count` permanently null. Reinforces the existing "hardcoded enum subset" finding via `ZoneSocketType` (7 of 16). |
| **C. Scene & Asset Pipeline (10)** | **Neutral to slightly raising** | Adds real pipeline capability the audit had not counted: asset publication with catalog UUIDs/tags (`assets.py:160-208`), three delivery modes with provenance and retained/lost attribute reporting (`delivery.py:201-208`), OpenVDB export proven live (`smoke:186-202`). Cancelled out by the publish→edit data-loss bug and the absence of node-group import/export. |
| **D. Lighting & Camera (10)** | **No effect** | Out of domain. |
| **E. Animation/Rigging/Simulation (10)** | **Raises** | Simulation Zones with explicit state schemas and an honest "not baked by this operation" contract (`zones.py:259-271`), plus a full bake lifecycle with frame/byte/time budgets and rollback (`bakes.py:222-300`) — both proven live (`smoke:84-120`, `:220-227`). This is procedural simulation the rubric's 7/10 did not account for. |
| **F. Rendering (15)** | **No direct effect** | Only indirect: `realize_procedural_output` and `manage_geometry_nodes_bake` are what make a procedural scene renderable/deliverable. |
| **G. Compositing (5)** | **Raises the ceiling, not the score** | G scores 1/5 for "no node/link authoring". This domain proves the repo already owns a complete, validated, atomic node-graph authoring engine (`handlers/node_graph.py`, `server/tools/_node_graph.py`) with the exact operation set a compositor needs. G's gap is packaging, not capability — the fix is smaller than the 1/5 and "High effort" rating in `AUDIT_SYNTHESIS.md:69` implies. |
| **H. Validation & Reliability (10)** | **Mixed, net lowering** | Adds the two strongest validators in the repo (`validate_geometry_node_graph`, 13 codes; `analyze_procedural_performance`, 13 heuristic codes with evidence and explicit limitations) and genuine batch-input rollback (`modifiers.py:238-256`). But it also adds three critical correctness defects, a success-on-invalid-link path, and a pagination block that contradicts its own reply. |
| **I. Agentability & NL (5)** | **Mixed** | Seven task-level builders are the closest thing in the repo to natural-language intent ("scatter these on that", "cut this with those"), backed by runtime node-schema discovery so an agent need not remember another Blender version's sockets. Against that: three of seven produce wrong geometry for the obvious usage, and several knobs are silently inert — an agent cannot tell from any reply. |
| **J. Production Completeness (5)** | **Mixed, net neutral** | Substantial completeness added (bakes, delivery with provenance, asset publication, performance analysis, OpenVDB). Offset by live coverage of only 10 of 28 tools, zero reply-budget coverage (`test_reply_budget.py:49-53` measures `shot`; this domain is in `asset`), and the fact that the uncovered set is exactly where the critical defects live. |
