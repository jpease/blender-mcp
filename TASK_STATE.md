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
| uncommitted | Two `AGENTS.md` audit-backlog rows closed. "Remaining inaccurate names" was already done in code (`set_viewport_overlay`, `import_sketchfab_model`, `add_radial_array_modifier`; `model_mirror`/`model_array` folded into `manage_modifiers`) and is pinned by `test_breaking_tool_names_are_absent` — the row was stale, not open. "Screenshot temporary path" was likewise already per-request `mkstemp` + `finally` unlink on both image tools; what was genuinely unsafe was the Blender side, where `_render_offscreen`/`_window_grab_fallback` removed their `bpy.data.images` datablock only on the success line, so a failed `image.save()` orphaned one `mcp_viewport` image per attempt. Both now remove in `finally`, with three tests in `tests/server/tools/test_viewport.py` (the two failure-path ones falsified against the pre-fix handler). Also corrected: the stale `just smoke` count (19 → 24 with the new radial-array script) and the `model_radial_array` name in both docs. |
| uncommitted | **The four open items from §3/§4/`AGENTS.md`, closed.** (1) *Render overhead*: measured, diagnosed and mostly removed - `scenario_render_overhead.py` + `tests/test_render_overhead_gate.py`, `drain_command_queue` now returns an adaptive poll (`_poll_interval`), 54.86 → 14.52 ms/frame, which is a latency cut for every MCP command and not only renders. (2) *IK convergence*: `solve_bone_reach` gained `tolerance_m`, `converged`, `chain_reach_m`, `target_distance_m` and per-reach envelope warnings that separate a stalled solve from an unreachable target; the posing smoke asserts the reported flag against real Blender. (3) *Radial-array pivot*: the High finding was **wrong** - `tests/blender_radial_array_smoke.py` proves the evaluated geometry matches an independently built pivot composition for all four named cases (worst deviation 2.2e-06 m). (4) *Rubric*: all nine slices scored, three new domain audits written, breakdown built - **67/100**. Six subagents did the audit reading; every claim behind a Critical was re-verified here, including a direct Blender probe of the RNA engine-enum defect. |
| uncommitted | **Four fixes from the first outside agent's field report, protocol 31 → 32.** (1) *Silent argument drop*: FastMCP's generated `ArgModelBase` carries no `extra`, so all 298 tools discarded unknown top-level arguments while advertising `additionalProperties: false` - the agent's `configure_camera(patch={"focal_length": 28})` vanished and it rendered at 50mm for several iterations. `server/tools/_strict_args.py` hardens every registered arg model to `extra="forbid"` after the bundle import loop; `tests/server/test_strict_tool_args.py` sweeps the whole catalog behaviourally so an SDK upgrade cannot revert it. (2) *Capability skew*: the agent was told `solve_bone_reach` "is not supported (protocol 31)" and hand-rolled FK instead. The tool exists - it landed in `0e5c461` while the protocol last moved in `2852803`, so a genuine protocol-31 install read as current. `src/blender_mcp/addon_surface.json` (292 commands, generated by `just addon-surface`) plus `tests/test_addon_surface.py` make a surface change impossible without a bump, `handshake_addon` now diffs the live capability list against it and reports `missing_commands`/`missing_parameters` through `get_addon_status`. (3) *Action displacement*: `keyframe_character_pose(action_policy="CREATE")` displaced the action holding root motion keyed by `keyframe_object_transform`, which is why the agent's characters stood still; policy is now `ENSURE`/`CREATE`/`REUSE` defaulting to `ENSURE`, displacing an action that holds f-curves needs `confirm_displace_action`, `keyframe_object_transform` takes the same action surface, and `handlers/action_assignment.py` owns both the assignment and the single f-curve walker. (4) *Place-and-aim*: `point_camera_at` takes a parent-aware world-space `camera_location`, and `prompts.py` gained the two-character-contact recipe (world matrices via `get_character_rig_info(bone_names=…)`, one shared point, `solve_bone_reach` per wrist) the agent needed and never found. |
| uncommitted | **Character-animation quality, protocol 32 → 33** (agent `blender-2`, from the walk-cycle field report: straight knees, a planted foot that slid forward, robotic timing). Three new tools and one shared vocabulary. (1) *Sliding foot*: `keyframe_bone_reach` solves an IK reach at many frames in one call, moving the playhead to each frame first so every solve runs against the body's evaluated pose there - the same world target repeated across contact frames is what plants a foot. Previously keying a planted foot cost two calls and a 4×4 matrix round-trip per foot per frame, which is why FK rotation was the cheap path and why the foot slid. (2) *Straight knees*: reaches take an optional `hinge` - a temporary one-axis IK limit, applied and restored around the solve - so a knee cannot invert; `configure_armature_bones` is in `character-rigging`, which `shot` mode does not register, so a posing-only session previously had no way to constrain a joint at all. (3) *Robotic timing*: `server/tools/key_style.py` + `bundled/addon/handlers/key_style.py` replace three per-module allowlists with one vocabulary carrying Blender's real 13-member `Keyframe.interpolation` enum (the surface shipped 3), plus handle types and easing; `keyframe_character_pose`, `edit_keyframes` and `bake_evaluated_animation` gained `handle_left`/`handle_right`/`easing`. (4) *No loop*: `set_action_cycle` manages the Cycles F-Modifier, `REPEAT_OFFSET` by default so a walk's forward travel accumulates. (5) *Animating blind*: `set_scene_frame` moves the playhead - no tool did, so every inspection tool reported frame 1 forever and an agent authoring 24 frames could only ever look at one of them. (6) A **behaviour fix** found on the way: `_set_action_interpolation` restyled every key in the action sharing the frame, so keying a bone at frame 12 silently rewrote the root motion's keys at frame 12 - exactly what the pose tools' own docstrings tell callers to put there. Now only the keys the call wrote are styled. Also: a `character_animation_strategy` prompt and an animation paragraph in `SERVER_INSTRUCTIONS`, both asserted; four long camera/animation handlers split into named helpers (the branch owns their lines now, so their pre-existing PLR0915/PLR0914 findings became gate failures); ceilings raised to 234,000 shot / 80,500 core with per-tool attribution. |

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
| `just check` | `lint: clean` (55 changed files, 7,039 owned lines); `fmt-check: 432 files already formatted`; `typecheck: 0 errors, 0 warnings, 0 notes`; `test: 1682 passed, 4 skipped in 55s` (the skips are the live-GUI gates) | uncommitted, after the field-report fixes |
| `just smoke` | `ok` on all 25 `tests/blender_*_smoke.py` against real headless Blender, ~19s, including the new `blender_action_slots_smoke.py` (one action holds both the root-motion and pose f-curves, the rig measurably travels, an unconfirmed displacement is refused) | uncommitted, after the field-report fixes |
| `just anchors` | `rows: 631   anchors intact: 631   anchors BROKEN: 0` | `457d5b6` |
| `just matrix` (full) | `reverts run: 631`; `reverts that failed to break their own nodes: 0`; `new test nodes with no revert and no written-down reason: 0`; 267s | `457d5b6` |
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

The highest-value work left is defect 1 in the synthesis: `validate_scene(...)["ready"]` is
permanently `False` in a stock Blender, because the engine probe reads a class-level RNA
enum that returns `['BLENDER_EEVEE']` even with Cycles enabled and rendering. Reproduced
directly on Blender 5.2.2: `resolve_engine("CYCLES")` raises while the same session renders
in Cycles. The fix the materials slice proposed (query instance-level) is disproven —
`bl_rna` resolves to the type either way; use `bpy.types.RenderEngine.__subclasses__()`.

---

## 5. Next steps, in order

1. **Make `validate_scene` usable again** (synthesis defect 1, Critical): the engine probe
   at `handlers/lighting/_shared.py:344-347` reads a class-level RNA enum that cannot see
   Cycles, `handlers/lighting/inspection.py:299-312` turns that into an ERROR, and
   `handlers/scene.py:1409` turns the ERROR into `ready: False` for every scene. Switch the
   probe to `bpy.types.RenderEngine.__subclasses__()` and cover it in
   `tests/blender_scene_validate_smoke.py`, which is the only gate that would have caught
   it. Two sibling verdict bugs belong with it: a missing scene camera is a WARNING to
   `handlers/camera/inspection.py:156` and an ERROR to the other two reporters, and the
   aggregate preflight reaches 5 of 11 domains and skips non-mesh materials. The three
   together take category H from 6/10 to 8/10.
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
