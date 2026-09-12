# Phase 1 Task State

Updated: 2026-09-11T23:00-06:00 (end of session 1)

Plan: `docs/superpowers/plans/2026-09-11-phase-1-catalog.md`
Handoff: `docs/superpowers/plans/2026-09-11-phase-1-handoff.md`
Branch: `main` (handoff §08). Scope: Tasks 1–6 only (handoff §09). No push until the end-of-phase gate.

## Baseline (measured, never estimated)

Measured 2026-09-11 at `3b27c1d` with `.venv/bin/python`, `BLENDER_MCP_TOOLSETS` set **before**
`import blender_mcp`, serialized with `exclude_none=True` and compact separators.
Imported source asserted: `src/blender_mcp/__init__.py` under this repo.

| Selection | Tools | Bytes | Tokens | How measured |
|---|---|---|---|---|
| `all` | 285 | 1,180,620 | 328K | `scripts/measure_catalog.py all` |
| core today (`CORE_MODULES`) | 40 | 119,634 | 33.2K | same |
| core-shared (core − mesh − model) | 24 | 92,029 | 25.6K | scratch ladder script |
| core-authoring (mesh + model) | 16 | 27,605 | 7.7K | same |
| core-shared after Task 3 | 21 | 63,395 | 17.6K | same |
| shot mode rung 0 (core-shared + camera + lighting + rendering) | 67 | 179,203 | 49.8K | same |
| rung 1 (− 3 scene tools) | 64 | 150,569 | 41.8K | same |
| rung 2 (− `camera.rigs`) | 58 | 142,218 | 39.5K | same |
| rung 3 (− `lighting.construction`) | 53 | 129,961 | 36.1K | same |

Per-group deltas: Task 3's three tools = 28,634 B (`create_geometry_object` 25,217 B,
`remove_scene_objects` 2,166 B, `reset_scene` 1,251 B). `camera.rigs` = 8,351 B.
`lighting.construction` = 12,257 B.

### Reconciliation with the spec and the plan

**Every tool count in spec §4.6 and in the plan is exact** — 40 = 24 + 16, the 67/64/58/53 ladder,
and core-shared landing at 21 tools / 17.6K tokens after Task 3. So is `all` at 285 / ~328K tokens,
and so is Task 3's 28,634 B delta.

**What is stale is the spec's shot-mode byte column**, and two of its three per-group deltas. The
spec puts rung 0 at 277,083 B / 77.0K tokens; measured, it is 179,203 B / 49.8K. It puts
`camera.rigs` at 18,229 B and `lighting.construction` at 27,126 B; measured, 8,351 B and 12,257 B.

**Consequence for the phase gate.** Spec §4.6 concludes that after three rungs shot mode sits at
56.4K against a 50K ceiling — "over by 6.4K" — and leans on that gap to justify variant scoping and
the typed gateway. Measured, rung 3 lands at **36.1K tokens, 13.9K under the ceiling**, and rung 0
is already marginally under it at 49.8K before any of this work. Tasks 1–6 are unaffected, but this
materially strengthens the plan's decision to defer variant scoping, MCP Resources and the gateway.

## Tasks

| # | Task | Status | Commit | Byte delta | Notes |
|---|---|---|---|---|---|
| 1 | Payload measurement harness | **done** | `27ec3aa`, `270958a`, `313740a` | n/a (adds no tools) | Plan's `measure_catalog.py` set the env var after importing `blender_mcp`; corrected to set it first. Two fix rounds: lint, then duplicate-name rejection + revision provenance. |
| 2 | Make bundle tests capable of failing | **done** | `d8163e3` | n/a (tests only) | 98/100. Canary proven: deleting `"mesh"` from `CORE_MODULES` gives 9 failed / 6 passed. `ALL_MODULES` dedup moved to Task 5, where a real failing test drives it. |
| 3 | Split `scene` authoring/destructive tools | **done** | `56ab3f0` | **−28,634 B** off the default surface | 97/100. Default 40 / 119,634 -> 37 / **91,000**. `scene-authoring` restores 40 / 119,634. Also edited `tests/test_scene_tools.py` (not in the brief) and `README.md`. |
| 4 | Split `CORE_MODULES` | **not started** | — | predicted **−27,605 B** | Next task. See predictions below. |
| 5 | Split `camera` and `texture-lighting` | **not started** | — | predicted **−20,608 B** off the shot surface | **Blocked on a design change the plan does not describe.** See "Task 5 is not what the plan thinks it is" below. |
| 6 | Pin the shot-mode payload ceiling | **not started** | — | n/a | Predicted ceiling **129,961 B** / 53 tools. |

## Where the next session picks up

**Tasks 1-3 are done, reviewed and committed. Task 4 is next.** Read this file, then
`docs/superpowers/plans/2026-09-11-phase-1-handoff.md` (still accurate except where corrected here),
then the plan. Everything below is measured on this branch at `56ab3f0`, not estimated.

### Predicted outcomes for Tasks 4-6 (derived by me at `56ab3f0`; verify, do not trust)

| After | Selection | Tools | Bytes | Tokens |
|---|---|---|---|---|
| Task 4 | default (`core-shared`) | 21 | 63,395 | 17.6K |
| Task 4 | `core-authoring` | 37 | 91,000 | 25.3K |
| Task 5 | `camera,lighting,rendering` (shot surface) | 53 | 129,961 | 36.1K |
| always | `all` | 285 | 1,180,620 | 328K |

Task 4 drops `mesh` + `model` = 16 tools / **27,605 B**. Task 5's groups measure
`camera.rigs` **8,351 B** and `lighting.construction` **12,257 B** — *not* the 18,229 B and 27,126 B
the plan's commit message cites. Task 6's ceiling should be the measured 129,961 B.

The predicted Task 4 result (21 tools / 17.6K tokens) matches spec §4.6 exactly, which is a good
independent check that the phase is tracking its design.

### Task 5 is not what the plan thinks it is — read this before starting it

The plan's Task 5 assumes `BUNDLES["camera"] = ("camera.core", "camera.targeting", ...)` will register
only those submodules. **It will not.** `camera/__init__.py` and `lighting/__init__.py` star-import
every submodule, so `importlib.import_module(".camera.core")` imports the parent package first and
registers all 23 camera tools. I verified this empirically. As written, Task 5's own tests fail and
the split saves nothing.

Deleting the star-imports is also not available: `tests/server/tools/camera/test_tools.py` reaches
~32 symbols through the package namespace (`camera.create_camera`, `camera.CameraOpticsPatch`, ...),
and handoff §03 forbids weakening existing tests.

**The intended route is a PEP 562 lazy `__getattr__` re-export** in `camera/__init__.py` and
`lighting/__init__.py`, so a dotted bundle entry registers only the named submodule while attribute
access still resolves for the existing tests. This is a design change the plan does not describe;
treat it as the substance of Task 5, not a detail. Two further corrections:

- **`camera.inspection` must be in the `camera` bundle.** The plan's entry omits it, which would
  orphan the module and drop `all` below 285. Spec §4.6 specifies the split as "`camera` -> `rigs`
  out", and the 58-tool rung only balances if every non-rig submodule is retained.
- **`ALL_MODULES` must deduplicate** (`dict.fromkeys`, order-preserving) when the deprecated
  `texture-lighting` alias lands, because the alias duplicates the `lighting.*` modules. Task 2's
  `test_all_sentinel_selects_every_module` asserts `len(resolved) == len(set(resolved))` and will
  fail — **that failure is the deliberate canary, not a regression.** The Task 2 critic confirmed it
  fires (23 != 21). Landing the dedup here also makes that test's docstring honest.

## Last successful commands

Run by the controller at `56ab3f0`, not taken from a subagent report:

- `.venv/bin/python -m pytest` -> **608 passed**
- `.venv/bin/python scripts/measure_catalog.py all` -> `tools : 285`, `bytes : 1,180,620`,
  `tokens : ~327,950`, schemas 77%
- `.venv/bin/python scripts/measure_catalog.py` -> `tools : 37`, `bytes : 91,000`
- `.venv/bin/python scripts/measure_catalog.py scene-authoring` -> `tools : 40`, `bytes : 119,634`
- `ruff check` / `ruff format --check` / `basedpyright` on every file each task created -> clean
- repo-wide `ruff check .` -> 9,868 errors, `ruff format --check .` -> 12 unformatted (both unchanged
  from the pre-phase baseline, proving Task 1 added no lint debt)

**The repo-wide lint gate in handoff §05 cannot pass and never could.** `ruff check .` reports 9,868
pre-existing errors across 300+ files, concentrated in `src/blender_mcp/bundled/addon/handlers/*`
and `tests/`; the config selects ANN/D/DOC/PL with no per-file ignores. The gate is therefore applied
to the files each task touches, and repo-wide counts are compared before and after to prove no new
debt. Cleaning the repo is not Phase 1 work.

## Known failures / blocked

None yet.

## Known issues found during pre-flight (not yet fixed)

- **Task 5's dotted-path premise is false as written.** `camera/__init__.py` and
  `lighting/__init__.py` star-import every submodule, so `importlib.import_module(".camera.core")`
  imports the parent package and registers all 23 camera tools. Verified empirically. Deleting the
  star-imports is not an option either: `tests/server/tools/camera/test_tools.py` references ~32
  symbols through the package namespace, and §03 forbids weakening existing tests. Task 5 will use a
  PEP 562 lazy `__getattr__` re-export instead.
- **Task 5's `camera` bundle omits `camera.inspection`.** Including it is required or `all` drops
  below 285. Spec §4.6 specifies the split as "`camera` → `rigs` out", and the ladder's 58-tool rung 2
  only balances if every non-rig camera submodule is retained, so this is spec-backed, not inferred.
- **Task 5's commit message cites stale byte figures** (18,229 B for `camera.rigs`, 27,126 B for
  `lighting.construction`). Measured values are 8,351 B and 12,257 B; the measured ones will be used.
- **`ALL_MODULES` does not deduplicate.** Task 5's deprecated `texture-lighting` alias would
  duplicate the `lighting.*` modules and break Task 2's no-duplicates assertion. **Dedup lands in
  Task 5, not Task 2** — in Task 2 nothing produces a duplicate, so the change would have been
  untestable, which is the vacuous-assertion bug class Task 2 exists to remove. Task 2's assertion
  is instead a deliberate canary; the Task 2 critic confirmed it fires by injecting a
  duplicate-producing alias (23 != 21).
- `CLAUDE.md` carries an unrelated uncommitted GitNexus re-index edit that predates this session. It
  is deliberately left unstaged and out of every task commit.

## Rubric scores by task (fresh-context critics, four lenses each)

### Task 2 (sonnet)

| Dimension | Score | Gate | Pass? |
|---|---|---|---|
| Capability preservation (30) | 30 | 24 | yes |
| Measurement integrity (25) | 25 | 20 | yes |
| Test integrity (25) | 24 | 20 | yes |
| Code quality (20) | 19 | 16 | yes |
| **Total (100)** | **98** | **90** | **PASS**, zero critical |

The critic settled a question I raised but deliberately did not pre-judge: `resolved == ALL_MODULES`
is *not* the tautology bug class, because `ALL_MODULES` is built independently from
`CORE_MODULES + BUNDLES` at module scope rather than echoed back from the function's own read — it
proved this by making the `all` branch return `ALL_MODULES[:-1]` and watching the test fail.

Minor parked: the docstring at `tests/server/test_bundles.py:52` says "with no duplicates", which
overstates what is enforced until Task 5 lands the dedup.

### Task 3 (opus)

| Dimension | Score | Gate | Pass? |
|---|---|---|---|
| Capability preservation (30) | 30 | 24 | yes |
| Measurement integrity (25) | 25 | 20 | yes |
| Test integrity (25) | 24 | 20 | yes |
| Code quality (20) | 18 | 16 | yes |
| **Total (100)** | **97** | **90** | **PASS**, zero critical |

The critic proved reachability per tool with real `mcp.list_tools()` subprocesses *and* by calling
each moved tool with a patched connection; AST-diffed `scene.py@2981771` against both modules
(47 = 25 + 22 symbols, zero lost/added/body-changed/duplicated); re-derived the baseline from a
`git archive` rather than trusting the controller; and ran five mutations. Mutation M2 re-introduced
the re-export trap and was caught by `test_scene_authoring_tools_are_not_in_the_core_surface` — the
test that looked weak is in fact the sole guard against the way this split would silently undo
itself. M5 (copying `_call` locally) fails two tests, so DRY is enforced by the suite.

Minors parked: `bundles.py:5` docstring says "~286 tools" against a measured 285 (**fix in Task 4**,
which edits that file); **nothing in the test suite pins `all` = 285** — the phase's headline
invariant lives only in a developer script (**recommend adding it in Task 6**); `scene_authoring.py`
imports private `_call`/`_StrictModel` across a module boundary, and that hard edge means moving
`scene` out of `CORE_MODULES` later would silently re-register all of it; `_validate_attribute_lengths`
has no docstring; nothing tests README/`BUNDLES` agreement.

### Task 1 (opus)

| Dimension | Score | Gate | Pass? |
|---|---|---|---|
| Capability preservation (30) | 30 | 24 | yes |
| Measurement integrity (25) | 21 | 20 | yes |
| Test integrity (25) | 22 | 20 | yes |
| Code quality (20) | 17 | 16 | yes |
| **Total (100)** | **90** | **90** | **PASS**, zero critical |

The critic reproduced 285 / 1,180,620 without going through the module under review, confirmed
`exclude_none=True` is what FastMCP actually serializes (`mcp/server/stdio.py:79`), proved the
source-root assertion fires against a shadowed package, and ran 8 mutations — all tests bite. It
cleared `pytest.approx(3.6, rel=0, abs=0)` as bit-exact rather than a weakened assertion (a 1-ULP
mutation fails it). Its one Important finding — duplicate tool names collapsing in `per_tool` — was
fixed in `313740a` and confirmed ADDRESSED by a scoped re-review.

### Minors deferred to the final whole-branch review

- `description_bytes` counts raw characters, not JSON-escaped bytes: 2,167 B (1.1%) low, and a
  different unit from `total_bytes`. Informational only, not a gate.
- The schema/description split is presented as a split but is not a partition (68,990 B unaccounted:
  name, annotations, JSON punctuation).
- `ensure_ascii=True` overcounts true UTF-8 wire bytes by 36 B (0.003%). **Ruled to stay** — a
  consistent bias that cancels in every delta, and the plan, spec and acceptance check all pin the
  baseline at 1,180,620.
- `test_payload_report_splits_schema_and_description:48` restates the implementation; anchored by
  literal assertions elsewhere in the file, so not the Task 2 bug class.
- No coverage for `payload_report([])`; `payload_report` re-implements `tool_bytes` inline,
  uncommented; several docstrings restate the signature.

## Rulings made during session 1

Decisions taken on the user's behalf, in the order made. Each says what it costs if wrong, so they
can be reviewed and reversed cheaply. Nothing here is pushed.

1. **`scripts/measure_catalog.py` sets `BLENDER_MCP_TOOLSETS` before importing `blender_mcp`.** The
   plan's ordering measures core-only for every selection, because `blender_mcp/__init__.py` eagerly
   imports `.server` -> `.server.tools`, which reads the variable at import time. *Cost if wrong:*
   none; Task 1's 285 / 1,180,620 check catches a wrong ordering.
2. **Every plan figure except `all` = 285 / 1,180,620 is treated as an estimate.** Measured values
   go in commit messages and this file. *Cost:* commit messages disagree with the plan's prose.
3. **The lint gate is per-file, not repo-wide.** `ruff check .` reports 9,868 pre-existing errors
   across 300+ files and `ruff format --check .` 12 unformatted; the config selects ANN/D/DOC/PL with
   no per-file ignores, so the handoff §05 gate has never passed and cannot be made to pass inside
   Phase 1. Each task's own files must be clean, and the repo-wide counts are compared before and
   after to prove no new debt. *Cost:* pre-existing lint elsewhere stays unfixed, which was already
   true.
4. **`ALL_MODULES` dedup moved from Task 2 to Task 5** (reverses my own earlier ruling). In Task 2
   nothing produces a duplicate, so the change would have been untestable — the vacuous-assertion bug
   class Task 2 exists to remove. *Cost:* Task 5 carries one extra line of production change and must
   not mistake the canary for a regression.
5. **Task 3 edits `tests/test_scene_tools.py`, which its brief did not list.** Following a symbol
   that moved is not weakening a test; all nine commands stay asserted and `<=` is intact. *Cost:* a
   test file changed in a task whose brief omitted it. The critic confirmed the ruling was honored
   and not exceeded.
6. **Task 3's `README.md` edit is accepted as scope discipline, not scope creep.** *Cost:* two doc
   lines in a task whose brief did not list the file.
7. **`ensure_ascii=True` stays in `catalog_metrics.py`,** though it overcounts true UTF-8 wire bytes
   by 36 B (0.003%). It is a consistent bias that cancels in every delta, and the plan, spec and
   acceptance check all pin the baseline at 1,180,620. *Cost:* every absolute byte figure reads 36 B
   high.
8. **Duplicate tool names raise in `payload_report` rather than being silently collapsed.** *Cost:* a
   few lines of defensive code for a case that cannot occur today.
9. **`measure_catalog.py` prints the git revision (and dirty state) it measured at.** *Cost:* three
   lines and a subprocess call in a developer script.
10. **`camera.inspection` belongs in the `camera` bundle** and **Task 5 needs a lazy `__getattr__`
    re-export**; see the Task 5 section above. *Cost if wrong:* a lazy attribute miss surfaces as
    `AttributeError` in the camera/lighting tests, which run in the per-task gate.
11. **The `test_all_sentinel_selects_every_module` docstring keeps its "with no duplicates" wording**
    even though Task 5 is what makes it true. *Cost:* one docstring reads as a present-tense
    guarantee about one task early.
12. **Work proceeded on `main` with no worktree**, per handoff §08's explicit instruction, against
    the executing-plans skill's default. Nothing is pushed. *Cost:* rollback is `git reset` on
    `main` rather than deleting a branch.

## What is NOT done

- Tasks 4, 5 and 6 are not started.
- **Nothing has been pushed.** The push is a single end-of-phase gate per handoff §03.
- The end-of-phase gate (`pytest && ruff check . && ruff format --check . && basedpyright`) has not
  been run as written, and per ruling 3 it cannot pass repo-wide. Run the per-file form plus the
  unchanged-baseline comparison instead.
- Spec §4.6's byte ladder is still stale in the spec itself. Correcting it is not Phase 1 work, but
  it materially affects Phase 3 planning — see the reconciliation section above.
