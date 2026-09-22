# TASK_STATE

Resume file for a session picking this work up mid-flight. Facts only: what ran, what it
said, what is still unproven. Update it in the same commit as the work it describes.

**Last updated:** 2026-09-21
**Branch:** `main` (see `git log --oneline -8`)
**Working tree:** clean apart from untracked `uv.lock` (pre-existing, not part of this work)

---

## 1. What is done

### Shipped: all six items of `docs/superpowers/plans/2026-09-20-blender-mcp-tool-fixes.md`

| Commit | Item | One line |
|---|---|---|
| `04c7ddf` | #1 preflight parameter gate | `get_addon_info` publishes `capability_params`; `send_command` refuses an unsupported parameter before the round trip |
| `da21c2b` | #6 doc convention note | `SERVER_INSTRUCTIONS` states the `<noun>_type` / explicit `collection_name` / nested `patch` conventions |
| `0e5c461` | #3 + #2 | `_selected_bones` shared from `character_rigging/foundation.py`, `get_character_rig_info(bone_names=…)`, and the new `solve_bone_reach` |
| `42a0eaa` | #4 viewport view/shading | `get_viewport_screenshot(view=…, shading_override=…)`, reply gains `view_source`/`shading_mode` |
| `c95a688` | #5 render orchestration | new read-only `plan_render_animation`; ANIMATION driven server-side per frame with progress + cancellation, `orchestrate_animation=false` keeps the old path |

The plan's own status table (top of that file) now records outcomes and residual risk per
item, and its P2/P3 design sections are marked superseded where the shipped code differs.

### Then, this session

| Commit | What |
|---|---|
| `4efb47b` | `scripts/rig_scenarios/scenario_viewport_view.py` + `tests/test_viewport_view_gate.py`: the live-GUI gate that closes the one unproven runtime claim from #4 (the offscreen GPU draw with synthetic matrices). Stdlib-only PNG decode; framing asserted by silhouette diff against a same-scene reference capture, so it is theme-independent. |
| `457d5b6` | Revert-matrix work: 9 new rows for the preflight gate and `capability_introspection`, 2 documented `NOT_INDIVIDUALLY_FALSIFIABLE` nodes, `tests/test_capability_introspection.py` added to `NEW_TEST_FILES` with the curation rule written down. |
| `48bfc5a` | `scripts/rig_scenarios/scenario_render_progress.py` + `tests/test_render_progress_gate.py`: the first live **MCP client** path in this repo. Spawns the shipped `blender-mcp` console script over stdio against the rig's Blender and proves per-frame progress notifications and a real `notifications/cancelled` landing between frames. |
| uncommitted | Two `AGENTS.md` audit-backlog rows closed. "Remaining inaccurate names" was already done in code (`set_viewport_overlay`, `import_sketchfab_model`, `add_radial_array_modifier`; `model_mirror`/`model_array` folded into `manage_modifiers`) and is pinned by `test_breaking_tool_names_are_absent` — the row was stale, not open. "Screenshot temporary path" was likewise already per-request `mkstemp` + `finally` unlink on both image tools; what was unsafe was the Blender side, where `_render_offscreen`/`_window_grab_fallback` removed their `bpy.data.images` datablock only on the success line, so a failed `image.save()` orphaned one `mcp_viewport` image per attempt. Both now remove in `finally`, with three tests in `tests/server/tools/test_viewport.py` (the two failure-path ones falsified against the pre-fix handler). Also corrected: the stale `just smoke` count (19 → 24 with the new radial-array script) and the `model_radial_array` name in both docs. |
| uncommitted | **The four open items from §3/§4/`AGENTS.md`, closed.** (1) *Render overhead*: measured, diagnosed and mostly removed - `scenario_render_overhead.py` + `tests/test_render_overhead_gate.py`, `drain_command_queue` now returns an adaptive poll (`_poll_interval`), 54.86 → 14.52 ms/frame, which is a latency cut for every MCP command and not only renders. (2) *IK convergence*: `solve_bone_reach` gained `tolerance_m`, `converged`, `chain_reach_m`, `target_distance_m` and per-reach envelope warnings that separate a stalled solve from an unreachable target; the posing smoke asserts the reported flag against real Blender. (3) *Radial-array pivot*: the High finding was **wrong** - `tests/blender_radial_array_smoke.py` proves the evaluated geometry matches an independently built pivot composition for all four named cases (worst deviation 2.2e-06 m). (4) *Rubric*: all nine slices scored, three new domain audits written, breakdown built - **67/100**. Six subagents did the audit reading; every claim behind a Critical was re-verified here, including a direct Blender probe of the RNA engine-enum defect. |
| uncommitted | **Four fixes from the first outside agent's field report, protocol 31 → 32.** (1) *Silent argument drop*: FastMCP's generated `ArgModelBase` carries no `extra`, so all 298 tools discarded unknown top-level arguments while advertising `additionalProperties: false` - the agent's `configure_camera(patch={"focal_length": 28})` vanished and it rendered at 50mm for several iterations. `server/tools/_strict_args.py` hardens every registered arg model to `extra="forbid"` after the bundle import loop; `tests/server/test_strict_tool_args.py` sweeps the whole catalog behaviourally so an SDK upgrade cannot revert it. (2) *Capability skew*: the agent was told `solve_bone_reach` "is not supported (protocol 31)" and hand-rolled FK instead. The tool exists - it landed in `0e5c461` while the protocol last moved in `2852803`, so a protocol-31 install read as current. `src/blender_mcp/addon_surface.json` (292 commands, generated by `just addon-surface`) plus `tests/test_addon_surface.py` make a surface change impossible without a bump, `handshake_addon` now diffs the live capability list against it and reports `missing_commands`/`missing_parameters` through `get_addon_status`. (3) *Action displacement*: `keyframe_character_pose(action_policy="CREATE")` displaced the action holding root motion keyed by `keyframe_object_transform`, which is why the agent's characters stood still; policy is now `ENSURE`/`CREATE`/`REUSE` defaulting to `ENSURE`, displacing an action that holds f-curves needs `confirm_displace_action`, `keyframe_object_transform` takes the same action surface, and `handlers/action_assignment.py` owns both the assignment and the single f-curve walker. (4) *Place-and-aim*: `point_camera_at` takes a parent-aware world-space `camera_location`, and `prompts.py` gained the two-character-contact recipe (world matrices via `get_character_rig_info(bone_names=…)`, one shared point, `solve_bone_reach` per wrist) the agent needed and never found. |
| uncommitted | **Character-animation quality, protocol 32 → 33** (agent `blender-2`, from the walk-cycle field report: straight knees, a planted foot that slid forward, robotic timing). Three new tools and one shared vocabulary. (1) *Sliding foot*: `keyframe_bone_reach` solves an IK reach at many frames in one call, moving the playhead to each frame first so every solve runs against the body's evaluated pose there - the same world target repeated across contact frames is what plants a foot. Previously keying a planted foot cost two calls and a 4×4 matrix round-trip per foot per frame, which is why FK rotation was the cheap path and why the foot slid. (2) *Straight knees*: reaches take an optional `hinge` - a temporary one-axis IK limit, applied and restored around the solve - so a knee cannot invert; `configure_armature_bones` is in `character-rigging`, which `shot` mode does not register, so a posing-only session previously had no way to constrain a joint at all. (3) *Robotic timing*: `server/tools/key_style.py` + `bundled/addon/handlers/key_style.py` replace three per-module allowlists with one vocabulary carrying Blender's real 13-member `Keyframe.interpolation` enum (the surface shipped 3), plus handle types and easing; `keyframe_character_pose`, `edit_keyframes` and `bake_evaluated_animation` gained `handle_left`/`handle_right`/`easing`. (4) *No loop*: `set_action_cycle` manages the Cycles F-Modifier, `REPEAT_OFFSET` by default so a walk's forward travel accumulates. (5) *Animating blind*: `set_scene_frame` moves the playhead - no tool did, so every inspection tool reported frame 1 forever and an agent authoring 24 frames could only ever look at one of them. (6) A **behaviour fix** found on the way: `_set_action_interpolation` restyled every key in the action sharing the frame, so keying a bone at frame 12 silently rewrote the root motion's keys at frame 12 - exactly what the pose tools' own docstrings tell callers to put there. Now only the keys the call wrote are styled. Also: a `character_animation_strategy` prompt and an animation paragraph in `SERVER_INSTRUCTIONS`, both asserted; four long camera/animation handlers split into named helpers (the branch owns their lines now, so their pre-existing PLR0915/PLR0914 findings became gate failures); ceilings raised to 234,000 shot / 80,500 core with per-tool attribution. |
| uncommitted | **Second live-rehearsal pass, protocol 34 → 35.** A whole runbook was driven over the live socket and reported eight frictions; all eight are closed, plus two defects found while closing them. (1) *Axis instruction refused verbatim*: both pose tools told callers to pass `length_axis` as `aim_at.track_axis`, which the tool then refuses - Blender builds every bone along its own Y, so `length_axis` is `"Y"` for every bone and collides with the `up_axis` of any upright one. `list_character_bones(rest_axes=True)` now reports `aim_axis_for_world`, the measured signed bone axis for each of the six world directions (`up_axis` is literally its `"+Z"` entry, one derivation, not two), the docstrings say to read it instead of deriving it, and the handler-side refusal names the remaining axes. (2) *Aim at a bone*: `BoneAim` gained `target_bone`/`target_bone_position`; "look at each other" had been aiming at armature origins on the floor. (3) *Pose keying does not batch*: `keyframe_character_pose(keys=[{frame, poses}, ...])` keys up to 250 frames per call, each solved at its own frame after `_place_playhead`, everything validated before the first key, one `finally` restoring pose and playhead; a 13-key stride was 13 round trips. (4) *`validate_scene` could never say `ready: true`*: the engine probe read `RenderSettings.bl_rna…enum_items`, which reports only `BLENDER_EEVEE` even while Cycles is registered and rendering, so a stock scene carried an ERROR-severity `ENGINE_UNAVAILABLE` forever; it now unions `bpy.types.RenderEngine.__subclasses__()` and the scene's assigned engine, and `ZERO_AREA_UVS` on a library-linked mesh drops to WARNING naming the library, because "unwrap these faces" is not actionable in the linking file. (5) *`set_action_cycle`*: `mode_before` now defaults to `NONE` (a forward loop was silently also an infinite backward one), `expected_period_frames` refuses a curve whose extent is not what the caller believes before any modifier is touched, and a restricted range (`frame_start`/`frame_end`/`blend_in`/`blend_out`) bounds where the modifier applies; the docstring states that the period is the curve's own key extent and names `manage_nla_tracks` as the route for looping part of a shot. (6) *Unreadable library paths*: a leaf-reduced path was indistinguishable from a broken one, so every publisher now emits `<field>_redacted` plus a stable `<field>_redaction_reason` from one helper. (7) *Framing on a bone*: `frame_camera_on_objects(bone_targets=[{object_name, bone_name, radius_m}])` frames a rig bone's evaluated head-tail segment; `point_camera_at(subtarget=…)` was verified correct against real Blender and documented rather than changed. (8) *Envelope drift*: the two handlers returning `changed_resources` as dicts now return plain names, matching the other 94 sites. Found while closing those: a **library-override custom-property** contract, measured in four headless probes - a bare ID-property write on an override is dropped at reopen (a property the source defines reverts to the library's value; registering the override property by hand changes nothing), while a **keyed** value survives, because the action is local data and `animation_data` is itself an override property. `_override_property_warning` says so from `set_character_pose` and `configure_armature_bones`, never from the keying path that is the remedy, and `tests/blender_character_rigging_phase0_smoke.py` proves both halves against a real linked-and-overridden rig. Also an intermittent crash: `handlers/texture/uv.py` held a `MeshUVLoopLayer` across `edit_mesh`, whose Edit-Mode round trip reallocates custom-data layers, so `optimize_uv_layout` raised `bpy_prop_collection[index]: internal error …` on roughly one run in three (`tests/blender_texture_smoke.py` failed 1 of 3 before, 6 of 6 after); layers are now resolved by name at the point of use, and a read-only audit of every other mode-changing site found no second occurrence. |
| uncommitted | **Fifth live-rehearsal pass (Blender 5.2.2, `shot`), protocol 38 → 39.** Five findings plus two carried forward; five verified as reported, one refuted in half, all seven closed. (1) *`validate_scene` promised pagination its schema did not expose*: one `OVERLAPPING_UVS` finding carried a per-face-pair `evidence` list that ate the whole 8 KiB budget, and the envelope - which declares a list paged on the strength of a sibling `truncated` key alone - told the caller to "continue with offset=1" on a tool that had no `offset`. The tool now takes one (sub-validators are asked for `offset + max_findings` so a resumed page is not empty), the reply carries `offset`/`limit`/`returned_count`/`next_offset`, `truncated` no longer aliases two facts (`domains_truncated` is the other), and `_normalized_domain_finding` caps a list `evidence` at 12 with `evidence_omitted` - not named `evidence_truncated`, which the envelope would have read as another offset it cannot honour. (2) *`create_camera` aimed at an armature's origin*, i.e. the floor, so a close-up rendered the top of the head: it takes `target_bone_name` now, the resolver moved to `camera/_shared.py`, and `point_camera_at`'s divergent `subtarget` spelling was renamed to match (`subtarget` stays only where it writes Blender's own RNA field). The audit's orphan-datablock row fell out of the same edit and is marked Solved. (3) *A cycle's period was observable only by destroying the cycle*: `set_action_cycle` gained a read-only `INSPECT` that reports every selected curve, including the ones REMOVE silently omits, with `has_cycles_modifier` and the live modifier state. The reporter's second half was **refuted** - `keyframe_object_transform` and `keyframe_character_pose` have warned about a key landing outside an existing cycle since protocol 33 - but the third key-writing path, `edit_keyframes`, genuinely did not, and now does. (4) *The twist warning trained callers to ignore warnings*: it measured nothing about the rig at all - one dot product on the request - and asserted in prose that the tail and every child head stay put. It now measures witness travel (`2·r·sin(θ/2)` over descendant bone ends plus a bounded scan of the vertices weighted to the bone) and fires only when travel is below 1% of the bone's length, so the head yaw that moves a face 6.47 cm is silent. (5) *No generic axis probe*: `probe_bone_axis` applies a trial rotation inside `restored_bone_pose`, measures the witness joint's travel decomposed against caller-named world directions, and hands the pose back - which is what the nine rest numbers cannot answer and what this pipeline was caching from a sibling repo's headless script. Carried forward: `remove_scene_objects` moved into the core surface (every mode could create scratch objects and never delete one), and `get_addon_status(mounted_tools=True)` pages the tool names this process registered, so a tool absent from a live build no longer has to be found by calling it. |

Earlier in the same push, 10 pre-existing rows were repointed after code moved (5
bone-filter rows → `character_rigging/foundation.py`, 3 rendering rows → the extracted
validation helpers, 2 payload-ceiling rows → the re-measured values); those went out with
the commits that moved the code.

---

## 2. Last successful commands, with what they reported

Run from the repo root. `.venv/bin/python` is the interpreter; `just check` is the
hand-off gate (`lint`, `fmt-check`, `typecheck`, `test`).

| Command | Result | When |
|---|---|---|
| `just check` | `lint: clean` (50 changed files, 4,215 owned lines); `fmt-check: 462 files already formatted`; `typecheck: 0 errors, 0 warnings, 0 notes`; `test: 1923 passed, 4 skipped in 63s` (the skips are the live-GUI gates) | uncommitted, after the fifth rehearsal pass |
| `just smoke` | `ok` on all 28 `tests/blender_*_smoke.py` against real headless Blender 5.2.2, ~21s, including the new `create_camera(target_bone_name=…)` aim and its orphan-datablock refusal in `blender_camera_smoke.py`, the `probe_bone_axis` section and the measured twist notices in `blender_character_posing_smoke.py`, `set_action_cycle(operation="INSPECT")` and the `edit_keyframes` out-of-cycle warning in `blender_cycle_period_smoke.py`, and the resumed findings page in `blender_scene_validate_smoke.py` | uncommitted, after the fifth rehearsal pass |
| `just anchors` | `rows: 789   anchors intact: 789   anchors BROKEN: 0` | uncommitted, after the fifth rehearsal pass |
| `just matrix` (full) | `reverts run: 789`; `reverts that failed to break their own nodes: 0`; `new test nodes with no revert and no written-down reason: 0`; 326s. Closed a pre-existing SURVIVOR on the way: `rehandshake: a failed re-handshake clears the staleness signal permanently` named a node that had been renamed away to a test of the *raising* path, so the row ran nothing; `tests/server/test_connection_framing.py` gained the test of the guard itself (a re-handshake that completes carrying half a session pair) and the raising path got its own row. 50 further dead `{POSET}` ids left by the reach/bone-listing split were repointed at the same time - 14 of them were hidden SURVIVORs for the same reason. | uncommitted, after the fifth rehearsal pass |
| `.venv/bin/python scripts/blender_rig.py --work-dir <tmp> --scenario scripts/rig_scenarios/scenario_viewport_view.py` | `RIG PASSED` in 3.2s. `camera_object` silhouette 0.560 of frame at (0.499, 0.498); `eye_target` silhouette 0.560 at (0.499, 0.498); every synthetic capture `method=offscreen`; `_mcp_synthetic_view` absent from `bpy.data`; `MATERIAL` applied then restored to `SOLID`; `RENDERED` refused; live capture byte-identical before/after an aimed one | `4efb47b` |
| `.venv/bin/python scripts/blender_rig.py --work-dir <tmp> --scenario scripts/rig_scenarios/scenario_render_progress.py` | `RIG PASSED` in 5.4s. 37 tools served over stdio; progress `[(1,3,'Rendered frame 7 (1/3)'), (2,3,'…8 (2/3)'), (3,3,'…9 (3/3)')]` matching `frame_count=3` and 3 PNGs; a hand-sent `notifications/cancelled` after frame 1 of 24 ended the call as `McpError: Request cancelled` with 1 frame on disk at cancel and 2 after a 3s settle; session and addon both still serving afterwards | `48bfc5a` |
| `BLENDERMCP_LIVE_RIG=1 pytest -m phase2_gate -q` (`just gate`) | `3 passed, 1648 deselected in 11.8s` | `48bfc5a` |

Useful narrower commands:

```bash
.venv/bin/python -m pytest tests/server/tools/test_viewport.py -q            # 13 passed
.venv/bin/python -m pytest tests/test_rendering_tools.py -q                  # 47 passed (34 before #5)
.venv/bin/python -m pytest tests/server/tools/character_rigging -q           # 80 passed
.venv/bin/python -m pytest tests/test_capability_introspection.py -q         # 5 passed
BLENDERMCP_LIVE_RIG=1 .venv/bin/python -m pytest -m phase2_gate -q           # the three live-GUI gates (`just gate`)
```

---

## 3. Known failures and unproven claims

Nothing in the suite is failing. These are the gaps, not breakages:

1. **Per-frame orchestration overhead is now measured, and most of it is gone.**
   `scripts/rig_scenarios/scenario_render_overhead.py` (gate
   `tests/test_render_overhead_gate.py`) renders the same 240-frame range both ways through
   a real MCP client and differences the wall clock. First measurement: **54.86 ms/frame**,
   13.2 s on a ten-second shot whose frames took 2.5 ms each. The cause was not the socket:
   `drain_command_queue` returned a flat 50 ms poll, and a client that sends again as soon
   as it reads a reply always misses the tick that just ran, so *every* MCP command paid it.
   The drain poll now follows the traffic (`_ACTIVE_POLL_SECONDS = 0.005` while a session is
   live, `_IDLE_POLL_SECONDS = 0.05` after `_ACTIVE_WINDOW_SECONDS = 5.0` of quiet), which
   measures **14.52 ms/frame** — 3.8x better, and the same figure the whole MCP surface's
   latency floor improved by. What remains is *not* the poll: a 1 ms poll measures 14.60
   ms/frame and `verify_outputs=False` measures 14.54, so the residual is the per-frame
   command round trip itself (server-side envelope/dispatch and Blender's per-command render
   entry), and attributing it further is unstarted work. At 14.5 ms a frame the default is
   defensible for real renders (a 1 s frame pays 1.5%) and still heavy for cheap preview
   sweeps, where `orchestrate_animation=false` remains the escape hatch.
2. **`just matrix` is green as of `457d5b6`** (631 rows, 0 survivors, 0 uncovered). It takes
   ~4.5 minutes and rewrites source files in place, so run it from a clean tree, run nothing
   else against the checkout meanwhile, and check `git status` afterwards in case a crash
   skipped a row's restore.
3. **One historical row was already broken before this work** (`server tools: the shot
   ceiling …`, anchored on a stale ceiling literal) and is now fixed; if `just anchors`
   reports a broken row again, it is almost always a row whose anchor text was moved by a
   refactor — repoint it rather than deleting it.
4. **IK convergence is rig-dependent, and the tool now says so itself.** `solve_bone_reach`
   takes `tolerance_m` (default 1e-4) and reports `converged`, plus `chain_reach_m` and
   `target_distance_m` so a caller can tell a stalled solve from a target no pose of that
   chain can reach; a missed reach rides back as an envelope warning naming both numbers.
   `tests/blender_character_posing_smoke.py` asserts the reported flag rather than its own
   constant, and covers an out-of-reach target against real Blender.
5. **CI has no Blender.** `just check` never touches it, so anything under
   `src/blender_mcp/bundled/addon/` is only exercised by a fake `bpy` there. `just smoke`
   (headless) and `just gate` (live GUI, macOS) are the only real-API proofs.

---

## 4. Current rubric score

The rubric is the 100-point production audit in `.audit_tmp/` (categories A–J, weighted
15/15/10/10/10/15/5/10/5/5). It is **finished**: all nine domain slices are scored, the
three missing domains were audited, and `.audit_tmp/AUDIT_SYNTHESIS.md` carries the
breakdown. The quotable figure is **67/100**.

| Slice | Score | Source |
|---|---|---|
| Scene/core/modeling/import | 8/10 | `.audit_tmp/01_scene_core.md` |
| Camera + lighting | 7/10 | `.audit_tmp/02_camera_lighting.md` |
| Rendering + colour + compositing | 5/10 | `.audit_tmp/03_rendering_compositing.md` |
| Materials & shading + UV/texturing | 7/10 | `.audit_tmp/04_materials_texture_uv.md` |
| Animation + rigging | 7/10 | `.audit_tmp/05_animation_rigging.md` |
| Retopology | 7/10 | `.audit_tmp/06_retopology.md` (new) |
| Geometry nodes | 6/10 | `.audit_tmp/07_geometry_nodes.md` (new) |
| Validation & reliability | 6/10 | `.audit_tmp/08_validation.md` (new) |
| Liquid/fluid/simulation | 8/10 | `.audit_tmp/09_liquid_fluid_simulation.md` |

Category totals: A 12/15, B 12/15, C 6/10, D 7/10, E 8/10, F 10/15, G 1/5, H 6/10, I 3/5,
J 2/5. The preliminary figure was 69/100 with three domains unaudited and camera/lighting
standing at a placeholder 10/10; auditing them lowered the total. Each pre-existing slice
now carries a dated `## Verification pass` section, because the originals were written
2026-08-29 and several of their headline claims did not survive re-reading the current
tree — the synthesis lists those under *Retired claims*.

Defect 1 in the synthesis is closed: `validate_scene(...)["ready"]` could never be `true` in a
stock Blender, because the engine probe read a class-level RNA enum that returns
`['BLENDER_EEVEE']` even with Cycles enabled and rendering. `engine_identifiers` now unions that
enum with `bpy.types.RenderEngine.__subclasses__()` and the scene's assigned engine, and
`ZERO_AREA_UVS` on a library-linked mesh is a WARNING naming the library rather than an ERROR no
shot can act on. `tests/blender_scene_validate_smoke.py` asserts `ready is True` on a stock
`--factory-startup` scene set to CYCLES, and the assertion was falsified by restoring the old
probe (`ready: False [ENGINE_UNAVAILABLE]`).

---

## 5. Next steps, in order

1. **Finish `validate_scene`'s verdict consistency** (the two siblings of synthesis defect 1;
   the engine probe itself is fixed, see §3): a missing scene camera is a WARNING to
   `handlers/camera/inspection.py:156` and an ERROR to the other two reporters, and the
   aggregate preflight reaches 5 of 11 domains and skips non-mesh materials. The two together
   take category H from 7/10 to 8/10.
2. **Undo the `create_studio_lighting` regression** (synthesis defect 4, Critical): preview
   arguments are validated at `server/tools/lighting/rendering.py:142-166,197-198`, after
   the rig's three lights exist, and the handler refuses the retry — so a typo'd
   `preview_engine` leaves the user deleting lights by hand. Validate before dispatch.
3. **Smoke-cover retopology and geometry nodes**: 9,737 lines of add-on handler code with
   no `tests/blender_*_smoke.py` at all, which is where both slices lost their points.
   `create_curve_generator`'s inert `radius` (synthesis defect 5) and the
   `transform_space` omission on every cross-object reference are both live-confirmed and
   would have been caught by one smoke script each.
4. **Standing audit backlog from `AGENTS.md`**, highest severity first: no video/FFmpeg
   output and no compositor authoring (Critical capability gaps); the socket is still
   unauthenticated, though the arbitrary-code half of that finding is gone with
   `execute_blender_code`; synchronous provider I/O blocking Blender's main thread and the
   MCP event loop (High); Poly Haven timeouts, `raise_for_status`, temp-file cleanup and
   its destructive world/material handling (High). The transaction/rollback row and the
   radial-array pivot row are both retired — see the synthesis.

---

## 6. Conventions a resuming session must not rediscover the hard way

- `just lint` is line-scoped (`scripts/lint_changed.py --base origin/main`): an inherited
  finding in a file you touch is not yours, but no line you write may add one. `typecheck`
  is whole-tree and at zero — keep it there.
- Every new behaviour test wants a revert-matrix row unless a written-down reason says why
  no single revert can break it. Rows live in `scripts/revert_matrix.py`; `just anchors` is
  the cheap check, `just matrix --only <prefix>` the real one.
- Adding an MCP tool or widening a schema moves the advertised-catalog byte ceilings in
  `tests/server/test_bundles.py` and needs a reply fixture plus representative arguments in
  `scripts/measure_reply_sizes.py`, or `tests/server/test_reply_budget.py` fails.
- `bpy` must never be imported from `src/blender_mcp/server/`. Inside
  `src/blender_mcp/bundled/addon/handlers/viewport.py`, `gpu`/`numpy` must stay
  function-local (neither exists in this virtualenv); `pyproject.toml` carries the narrow
  per-file ignore that makes that legal.
- The live rig launches a real windowed Blender from `/opt/homebrew/bin/blender` and writes
  only inside its `--work-dir`. Scenarios are plain modules exposing `run(rig)`; they fail
  by raising, and the rig prints `RIG PASSED` on a clean return.
