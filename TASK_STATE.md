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
| `just check` | `lint: clean, 28 changed files, 2688 owned lines`; `fmt-check: 417 files already formatted`; `typecheck: 0 errors, 0 warnings, 0 notes`; `test: 1648 passed, 2 skipped in 52s` (the 2 skips are the live-GUI gates) | `457d5b6` |
| `just smoke` | `ok` on all 23 `tests/blender_*_smoke.py` against real headless Blender 5.2.2, ~18s | after the six plan commits |
| `just anchors` | `rows: 631   anchors intact: 631   anchors BROKEN: 0` | `457d5b6` |
| `just matrix` (full) | `reverts run: 631`; `reverts that failed to break their own nodes: 0`; `new test nodes with no revert and no written-down reason: 0`; 267s | `457d5b6` |
| `.venv/bin/python scripts/blender_rig.py --work-dir <tmp> --scenario scripts/rig_scenarios/scenario_viewport_view.py` | `RIG PASSED` in 3.2s. `camera_object` silhouette 0.560 of frame at (0.499, 0.498); `eye_target` silhouette 0.560 at (0.499, 0.498); every synthetic capture `method=offscreen`; `_mcp_synthetic_view` absent from `bpy.data`; `MATERIAL` applied then restored to `SOLID`; `RENDERED` refused; live capture byte-identical before/after an aimed one | this session |

Useful narrower commands:

```bash
.venv/bin/python -m pytest tests/server/tools/test_viewport.py -q            # 13 passed
.venv/bin/python -m pytest tests/test_rendering_tools.py -q                  # 47 passed (34 before #5)
.venv/bin/python -m pytest tests/server/tools/character_rigging -q           # 80 passed
.venv/bin/python -m pytest tests/test_capability_introspection.py -q         # 5 passed
BLENDERMCP_LIVE_RIG=1 .venv/bin/python -m pytest -m phase2_gate -q           # the two live-GUI gates (`just gate`)
```

---

## 3. Known failures and unproven claims

Nothing in the suite is failing. These are the gaps, not breakages:

1. **No live-MCP-client proof for `render_scene` orchestration (#5).** A real
   `progressToken` progress notification, a real `CancelledNotification` mid-animation, and
   the real per-frame round-trip overhead are all untested; only fixtures cover the
   aggregation and the cancellation checkpoint placement. No harness for a live client
   exists in this repo yet — that is the build cost of closing it.
2. **`just matrix` is green as of `457d5b6`** (631 rows, 0 survivors, 0 uncovered). It takes
   ~4.5 minutes and rewrites source files in place, so run it from a clean tree, run nothing
   else against the checkout meanwhile, and check `git status` afterwards in case a crash
   skipped a row's restore.
3. **One historical row was already broken before this work** (`server tools: the shot
   ceiling …`, anchored on a stale ceiling literal) and is now fixed; if `just anchors`
   reports a broken row again, it is almost always a row whose anchor text was moved by a
   refactor — repoint it rather than deleting it.
4. **IK convergence is rig-dependent.** `solve_bone_reach` reports `achieved_error_m` for
   exactly that reason. `tests/blender_character_posing_smoke.py` measures 3.28e-05 m
   against `REACH_TOLERANCE = 1e-4`; a different rig may need that constant loosened.
5. **CI has no Blender.** `just check` never touches it, so anything under
   `src/blender_mcp/bundled/addon/` is only exercised by a fake `bpy` there. `just smoke`
   (headless) and `just gate` (live GUI, macOS) are the only real-API proofs.

---

## 4. Current rubric score

The rubric is the 100-point production audit in `.audit_tmp/` (categories A–H). It is
**unfinished**: 5 of 8 domain slices exist, three preliminary domain scores were recorded,
and `.audit_tmp/AUDIT_SYNTHESIS.md` still lists "Build 100-point score breakdown" as an open
task. There is therefore **no single aggregate score to quote**; quoting one would be
fabrication. What exists:

| Slice | Score estimate | Source |
|---|---|---|
| Materials & shading + UV/texturing | 7/10 | `.audit_tmp/04_materials_texture_uv.md` |
| Rendering + colour management + compositing | 5/10 | `.audit_tmp/03_rendering_compositing.md` |
| Animation + rigging | 7/10 | `.audit_tmp/05_animation_rigging.md` |
| Scene/core/modeling, camera/lighting, liquid/fluid | not scored | slices exist, no score line |
| Retopology, geometry nodes, validation | not audited | listed as missing domains |

Effect of this work on those numbers: `solve_bone_reach` addresses part of the animation
slice's "no reach/IK primitive" friction, and #5 addresses the rendering slice's "no timeout
/ no progress on long renders" reliability finding. Neither score has been re-derived —
do that as part of finishing the rubric, not by adjusting a number here.

The audit's own critical findings are still open and are the highest-value work left; see
§5 and the pending table in `AGENTS.md`.

---

## 5. Next steps, in order

1. **Live-client harness for progress/cancellation** (gap §3.1). Smallest useful version: a
   script that speaks MCP over stdio to this server, sends `render_scene(mode="ANIMATION")`
   with a `progressToken`, asserts one notification per frame, then sends
   `CancelledNotification` and asserts the frames already written are exactly the ones on
   disk. Nothing in the repo speaks MCP as a client yet, so this is a new harness, not an
   extension of `scripts/blender_rig.py` (which speaks the addon's own socket protocol).
2. **Finish the rubric** (§4): score the three unscored slices, audit the three missing
   domains, then build the 100-point breakdown the synthesis file asks for.
3. **Standing audit backlog from `AGENTS.md`**, highest severity first:
   `execute_blender_code` on an unauthenticated auto-started socket (Critical); RNA enum
   queries at class level breaking the Cycles fallback check (Critical, from the audit);
   no video/FFmpeg output and no compositor authoring (Critical capability gaps);
   `model_radial_array`'s arbitrary-pivot rotation (High); synchronous provider I/O blocking
   Blender's main thread and the MCP event loop (High — same class as #5, one layer up);
   no transaction/rollback contract (High); Poly Haven timeouts, `raise_for_status`,
   temp-file cleanup and its destructive world/material handling (High).

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
