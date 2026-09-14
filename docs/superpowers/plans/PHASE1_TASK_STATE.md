# Phase 1 Task State

Updated: 2026-09-11T23:20-06:00 (end of session 1)

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
- ~~Spec §4.6's byte ladder is still stale in the spec itself.~~ **Corrected 2026-09-11** in the
  same session: §4.6's byte/token columns, its "over by 6.4K" conclusion, the `all` token figure
  (~333K -> ~328K), the schema share (76% -> 77.5%), the `$defs` share (41% -> 38%), `nd_boolean`
  (1,659 -> 1,596 B, the old figure having counted nulls), and §2.4's "shot mode is ~77K" -> "~50K".
  A dated correction block records the old values.

  **The old byte column could not be sourced to any state of `main`.** The obvious explanation — that
  it predated the 2026-09-09 schema work (`04d5f36`, `73c9c4b`, `0b052ec`) — was tested and is false:
  at `f9f51d5`, the commit before all three, rung 0 measures 249,194 B (still not 277,083), and the
  `camera.rigs` / `lighting.construction` groups measure 8,351 B and 12,257 B there too, identical to
  today. Those group sizes have never changed, so 18,229 and 27,126 were never measurements of this
  repository. What the schema work shrank was `core`, 189,625 B -> 119,634 B.

---

# Post-Task-3 Code Review Loop

Review of the **already-committed** Tasks 1-3 code (range `3b27c1d..b167c09`). No commits are
made by this loop; all changes sit in the working tree. Rubric: architecture 40 (gate 34),
DRY 35 (gate 29.75), comments 25 (gate 21.25); exit >= 90 overall with zero critical failures.

## Files under review

`src/blender_mcp/server/catalog_metrics.py`, `scripts/measure_catalog.py`,
`src/blender_mcp/server/tools/scene_authoring.py`, `src/blender_mcp/server/tools/scene.py`
(shared surface only), `src/blender_mcp/server/bundles.py`, `tests/server/test_catalog_metrics.py`,
`tests/server/test_bundles.py`, `tests/test_scene_tools.py`.
Added during repair: `src/blender_mcp/server/tools/_scene_shared.py`.

## Verified baseline (measured, not assumed)

| Check | Value at `b167c09` |
|---|---|
| `pytest` | 608 passed |
| ruff, new Task 1-3 files | 0 errors |
| ruff, `scene.py` | 10 errors (identical to `3b27c1d`; untouched regions) |
| ruff, `tests/test_scene_tools.py` | 25 errors (identical to `3b27c1d`) |
| `basedpyright`, in scope | 0 errors |
| core payload | 37 tools / 91,000 B / ~25.3K tokens |
| core + `scene-authoring` | 40 tools / **119,634 B** = the old core exactly |

The -28,634 B Task 3 claim reproduces exactly; the split is byte-for-byte lossless.
Repo-wide ruff is 9,868 errors (pre-existing debt, out of scope).

## Cycle 1 - scores

| Dimension | Score | Gate | Verdict |
|---|---|---|---|
| Clean architecture | 31/40 | 34 | fail |
| DRY | 26/35 | 29.75 | fail |
| Comments/docs | 19/25 | 21.25 | fail |
| **Total** | **76/100** | 90 | fail |

Critical failures: **none** (all three critics agreed).

### Rejected finding

**Architect #10 - "`# ruff: ignore[...]` / `# ruff: file-ignore[...]` suppress nothing."**
**False.** Disproved by execution on ruff 0.16.5: a 146-char line is reported bare, silent
under `# ruff: file-ignore[line-too-long]`, and silent under inline `# ruff: ignore[...]`.
This project's config additionally reports `noqa-comments: 'noqa' comment used instead of
'ruff: ignore'`, i.e. `ruff: ignore` is the *preferred* spelling here. The Comment critic's
opposite claim was correct. No change made.

### Cycle 1 repairs applied

| # | Finding (critics) | Repair |
|---|---|---|
| 1 | `scene_authoring` imported `_call`/`_StrictModel` from the peer tool module `scene.py`, coupling bundle membership to import membership | New `tools/_scene_shared.py` (matches the `_shared.py` convention in 7 other tool packages); both scene modules import from it |
| 2 | Tests patched `scene.get_blender_connection` to drive `scene_authoring` functions | Single `_stub_connection(monkeypatch)` helper patching the one owning module; `test_scene_validate.py` updated too |
| 3 | `_tool_count_for_toolsets` / `_tool_names_for_toolsets` duplicated ~10 lines incl. the `!r` quoting | Count helper is now `len(_tool_names_for_toolsets(...))` |
| 4 | Both helpers inherited ambient `BLENDER_MCP_TOOLSETS` from the developer's shell | Explicit `env=` passed; `None` now genuinely means absent |
| 5 | `test_all_sentinel_selects_every_module` asserted `resolved == ALL_MODULES` against `return ALL_MODULES` | Recomputes the expectation from `BUNDLES`, the authored source of truth |
| 6 | Moved-tool triple spelled twice | Hoisted `_SCENE_AUTHORING_TOOLS` |
| 7 | `description_bytes` counted raw chars while the other counters counted encoded JSON | All three route through `_compact`; `_text_bytes` added. ASCII results unchanged (baselines intact), non-ASCII now correct |
| 8 | `payload_report` re-implemented `tool_bytes` inline | Shared `_dumped_bytes`, one definition, no extra dump |
| 9 | `PayloadReport` defaults allowed a self-contradictory report | Defaults dropped; every field required |
| 10 | `BYTES_PER_TOKEN` test restated the constant; `total_tokens` untested | Replaced with a `total_tokens` divisor test plus a non-ASCII `description_bytes` test |
| 11 | `_validate_attribute_lengths` undocumented, misnamed `curve_count`, split from its allowlist | Merged into `_validate_attributes(attributes, counts, label)` beside `GeometryAttribute`; closes the `LAYER` gap |
| 12 | "one value per X" written 4x in 3 dialects | `_require_one_value_per` helper; messages preserved verbatim |
| 13 | `_call`/`_StrictModel` undocumented cross-module API | Full Google docstrings in `_scene_shared.py` |
| 14 | `scene.py` docstring still claimed "native geometry" | Rewritten; points at `scene_authoring.py` and `_scene_shared.py` |
| 15 | Blanket suppression header with no reason | Split into three reasoned groups |
| 16 | `remove_scene_objects` description omitted the `managed_rig` mode | Rewritten to state the exactly-one-of rule |
| 17 | Repo root recomputed 3x in a 99-line script | `_REPO_ROOT` / `_SRC_ROOT` / `_SELECTION` constants |
| 18 | Test helper docstrings missing; over-long `Raises` block | Added/trimmed |

Deferred by explicit judgement: reverting the eight `_CORE_TODAY` parametrize rows (both
forms defensible); deleting `payload_bytes` (the plan names it as a Task 6 consumer);
collapsing the other six repo-wide `_call` definitions (out of this change's scope).

### Post-repair verification (all re-run)

| Check | Result |
|---|---|
| `pytest` | **609 passed** |
| ruff, every in-scope file | at or below baseline (`scene.py` 10, `test_scene_tools.py` 25, rest 0) |
| `ruff format --check` | 10 files already formatted |
| `basedpyright` | 0 errors |
| core payload | 37 tools / **91,000 B** - unchanged, baseline intact |

Regression label vs. cycle 1 entry state: **improved** (one regression found and fixed during
repair: `test_scene_validate.py` also patched the old dispatch location; it failed loudly with
`AttributeError` rather than silently opening a socket, and now patches `_scene_shared`).

## Cycle 2 - scores

| Dimension | Cycle 1 | Cycle 2 | Delta | Gate | Verdict |
|---|---|---|---|---|---|
| Clean architecture | 31/40 | **34/40** | +3 | 34 | pass (at gate) |
| DRY | 26/35 | **30/35** | +4 | 29.75 | pass |
| Comments/docs | 19/25 | **18/25** | **-1** | 21.25 | fail |
| **Total** | 76/100 | **82/100** | +6 | 90 | fail |

Critical failures: **none**.

Regression label vs. cycle 1: **improved overall, regressed on documentation.** The comment
regression was self-inflicted and is the important lesson of this cycle: two statements written
*during* the cycle-1 repair were verified false.

1. `_text_bytes`' docstring contained `—` inside a non-raw `"""` literal, so the rendered
   docstring read "an em dash is six bytes as `-`, not one" - a self-contradiction, and the only
   sentence justifying the function. Fixed with `r"""`; verified by reading `__doc__` back.
2. `_StrictModel`'s docstring claimed "Both settings reach the client in the advertised JSON
   schema". Verified false: `extra="forbid"` emits `additionalProperties: false`, but
   `allow_inf_nan=False` emits **nothing**. Rewritten to say which half is advertised and which
   is server-side only.

Lesson recorded: a docstring written during a repair is new code and needs the same verification
as new logic. Rendered output must be read back, and every claim about wire behaviour must be
checked against an actual rendered schema.

### Cycle 2 repairs applied

| # | Finding | Repair |
|---|---|---|
| 1 | `_text_bytes` docstring rendered as nonsense | `r"""` raw docstring |
| 2 | `_StrictModel` docstring made a false wire-contract claim | Rewritten; `allow_inf_nan` correctly described as server-side only |
| 3 | `reset_scene` description hid `purge_orphaned_data=True`, an irreversible default | Description now names both the confirm gate and the purge |
| 4 | 10 public test functions undocumented in an edited file | All documented; `_Connection.send_command` typed and documented |
| 5 | `bundles.py` said "~286 tools"; the harness measures 285 | Real number, with the command that produces it |
| 6 | `all` documented as "the literal `all`" though matching is case-insensitive | Documented accurately (behaviour deliberately unchanged - pre-existing) |
| 7 | `scene.py` header omitted `validate_scene`, its largest remaining tool | Header rewritten around the seven tools it actually registers |
| 8 | `BYTES_PER_TOKEN = 3.6` had no provenance anywhere in the repo | Labelled an unvalidated rule of thumb; callers told to re-derive before trusting token figures |
| 9 | A test comment anchored to "Task 3", invisible from the test file | Reworded to state the invariant |
| 10 | `too-many-arguments` rationale argued *for* more advertised schema in the file that exists to cut it | Rewritten to the real reason (flat params are easier for a model to fill) |
| 11 | `_dumped_bytes` docstring claimed to be "the single definition" but the schema counter bypassed it | Collapsed `_compact` + `_dumped_bytes` into one `_json_bytes`; all four counters route through it and the claim is now true |
| 12 | `payload_bytes` was a second aggregation that diverged on the duplicate-name rule | Redefined as `payload_report(tools).total_bytes`; one rule, both entry points |
| 13 | `PayloadReport` was `frozen=True` yet its `per_tool` dict was mutable | `Mapping` + `MappingProxyType`; docstring records the derived fields and the single supported constructor |
| 14 | `ALL_MODULES` skipped the dedup the named-bundle branch applies | `dict.fromkeys`, one policy for both branches |
| 15 | Bundle tests spawned 7 server subprocesses, 4 of them duplicates | `@functools.cache` + `frozenset`; bundle+metrics suite 4.33s -> 2.39s |
| 16 | `measure_catalog.py` was not import-safe (proven: raised on a foreign `sys.argv`) | All side effects moved into `_measure()`; verified importable under `sys.argv = ['pytest', ...]` and unchanged as a script |
| 17 | `_scene_shared._call` alone omits the `ToolError` wrapper its 7 siblings have | Documented as a deliberate behaviour-neutral carry-over, with the unification named as its own change |

Deferred with reasons: the payload **budget regression test** (plan assigns it to Task 6, which
is explicitly out of this review's scope); normalising bundle-name case (pre-existing behaviour,
not introduced here - documented instead); collapsing the other seven `_call`/`_StrictModel`
copies into `envelope.py` (correct target identified, but it is its own change).

### Post-cycle-2 verification

| Check | Result |
|---|---|
| `pytest` | **609 passed** |
| ruff: `catalog_metrics`, `measure_catalog`, `scene_authoring`, `_scene_shared`, `bundles`, both new test files | **0 errors** |
| ruff: `scene.py` | 10 - equal to the `3b27c1d` baseline |
| ruff: `tests/test_scene_tools.py` | **8** - down from the 25-error baseline |
| `ruff format --check` | all in-scope files formatted |
| `basedpyright` | 0 errors |
| core payload | 37 tools / **91,000 B** - unchanged across both cycles |

## Cycle 3 - scores, and structural escalation

| Dimension | C1 | C2 | C3 | Gate | Verdict |
|---|---|---|---|---|---|
| Clean architecture | 31 | 34 | **34** | 34 | pass (flat) |
| DRY | 26 | 30 | **27** | 29.75 | fail |
| Comments/docs | 19 | 18 | **20** | 21.25 | fail |
| **Total** | 76 | 82 | **81** | 90 | fail |

Critical failures: **none** in any cycle.

Total moved 82 -> 81, i.e. **less than one point across two consecutive cycles while below the
exit threshold**, which triggers the protocol's structural escalation. Cycle 3's repairs were
therefore done as a structural pass over the whole reviewed surface rather than as spot fixes.

### Highest-severity finding of the loop (verified against the addon, not argued)

`reset_scene`'s wire description said it "purges orphaned data" in a sentence whose subject was
"one scene". The handler at `bundled/addon/handlers/scene.py:1230` runs
`bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=False, do_recursive=True)` - a
**file-wide** recursive sweep of every orphaned local datablock, including ones orphaned long
before the call and belonging to scenes the operation never touched. `scene_name=None` also
resolves to the *active* scene (`:1215`), which the description did not say. CLAUDE.md requires
destructive behaviour to be explicitly disclosed, and this is the text a model reads when
deciding whether the call is safe. Both facts are now in the description.

### Structural pass - changes

| # | Finding | Repair |
|---|---|---|
| 1 | `_measure` was silently single-shot: a second call returned the first selection's numbers under the second's name (reproduced: `37, 37` where the second should be 285) | Refuses when `blender_mcp` is already imported; verified it now raises instead of misleading |
| 2 | `test_every_bundle_module_is_a_real_tools_submodule` imported every tool module in-process, taking the global FastMCP registry 37 -> 285 for every later test | Uses `importlib.util.find_spec`; the file now states that nothing in it may import a tool module in-process |
| 3 | `PayloadReport`'s "derived from `per_tool`" invariant was enforced only by its docstring | `total_bytes` and `tool_count` are now properties; the constructor takes three fields and a report cannot disagree with itself |
| 4 | `payload_bytes` was dead public API and a second aggregation path (flagged independently by both cycle-3 critics) | **Deleted**, reversing the cycle-1 decision to keep it. Task 6 can write `payload_report(tools).total_bytes` |
| 5 | `TOOLSETS_ENV_VAR`/`ALL_SENTINEL` were private single-use constants while the literal was retyped at the four sites that matter | Made public and used in `tools/__init__.py` (the production read) and the bundle tests |
| 5a | - but `measure_catalog.py` **cannot** use them | Importing `bundles` imports `blender_mcp`, which reads the variable at import time - the very thing that must happen after the assignment. The literal there is forced, and now says so in a comment |
| 6 | `_Connection` stub and the `_scene_shared` patch target were duplicated across two test files | One `RecordingConnection` + `stub_blender_connection` fixture in `tests/conftest.py`; both files use it, and the "patch the owning module" reasoning lives in exactly one docstring |
| 7 | `test_unset_toolsets_registers_only_core_bundle` was arithmetically subsumed by the test below it | Deleted; the surviving test asserts a **set** relation (`core < cloth < all`, plus disjointness of two bundles' additions) instead of an ordering of three integers |
| 8 | The 285 in `bundles.py`'s docstring was pinned by nothing | `test_all_advertises_the_tool_count_quoted_in_bundles_docs` |
| 9 | The cost-model paragraph was authored four times and had already drifted once (the 286/285 error) | `bundles.py` is now the single statement; `catalog_metrics.py`, `tools/__init__.py` and `measure_catalog.py` point at it |
| 10 | `create_geometry_object` never stated that `rotation` is XYZ Euler **radians** | Stated (38 bytes against a 25,217-byte tool) |
| 11 | The `docstring-missing-*` file-ignore was justified only for the tools but also silenced five `@model_validator` methods | Reason widened to state exactly what else it covers and why |
| 12 | `_measure`'s `Raises:` nested a propagated ValueError inside the `SystemExit` entry, and omitted the bundle-typo failure users actually hit | Restructured; typo path documented |
| 13 | `_call`'s own docstring did not warn that Blender failures escape unwrapped | Noted at the function, where the call-site tooltip shows it |
| 14 | Degenerate assertion `total_bytes == per_tool["a"]` on a one-tool report (a property of `sum`, not of the code) | Replaced with a two-tool summation claim |
| 15 | `bundles.py` docstring had a ragged-wrap artifact orphaning `BLENDER_MCP_TOOLSETS` | Re-flowed |

### Rejected in cycle 3

**"Promote a subprocess-based `report_for_selection` into `catalog_metrics.py`."** Rejected. That
module's purity - no I/O, no subprocess, unit-testable without FastMCP or Blender - is the
property every cycle has praised and the reason `_Dumpable` exists. The cycle-3 DRY critic
independently reached the same conclusion ("the mechanisms should stay split"), since the script
must hold live tool objects a subprocess would have to serialize. The underlying single-shot bug
was fixed directly instead (#1).

### Post-structural-pass verification

| Check | Result | vs. baseline |
|---|---|---|
| `pytest` | **609 passed** | 608 at `b167c09` |
| `basedpyright` | **0 errors** | 0 |
| `ruff format --check` | 12 files formatted | clean |
| ruff: all new/reworked production + test files | **0 errors** | 0 |
| ruff: `scene.py` | 10 | 10 (unchanged) |
| ruff: `tests/test_scene_tools.py` | **9** | 25 |
| ruff: `tests/server/tools/test_scene_validate.py` | **56** | 60 |
| ruff: `tests/conftest.py` | 4 | 4 (unchanged) |
| core payload | 37 tools / **91,000 B** | unchanged across all cycles |

## Cycle 4 - scores

| Dimension | C1 | C2 | C3 | C4 | Gate | Verdict |
|---|---|---|---|---|---|---|
| Clean architecture | 31 | 34 | 34 | **34** | 34 | pass (at gate, flat 3 cycles) |
| DRY | 26 | 30 | 27 | **32** | 29.75 | pass |
| Comments/docs | 19 | 18 | 20 | **21** | 21.25 | fail (0.25 short) |
| **Total** | 76 | 82 | 81 | **87** | 90 | fail |

Critical failures: **none** in any cycle.

### Two critic claims rejected after verification

**1. "The root conftest import couples all 102 test files to bundle resolution."** The *symptom*
is real - `BLENDER_MCP_TOOLSETS=not-a-bundle pytest tests/server/test_catalog_metrics.py` fails at
collection - but the diagnosis is wrong. `src/blender_mcp/server/__init__.py:1` is
`from . import prompts, tools`, so importing **any** `blender_mcp.server.*` module runs bundle
resolution; `test_catalog_metrics.py` imports `blender_mcp.server.catalog_metrics` directly.
Verified by stashing `tests/conftest.py` entirely and reproducing the same failure. The conftest
change was still made (it removes an eager import and a suppression, and resolves the patch target
lazily) but it is **not** a fix for that coupling, which is pre-existing and structural.

**2. "`RecordingConnection`'s `kind` key is what the discriminated-union test round-trips."**
False. `tests/test_scene_tools.py:53` asserts on `connection.calls[0][1]` - the recorded
*request* - not the reply. Deleted the key and ran the full suite: 609 passed. The Architecture
and DRY critics were right that it is dead; the Comment critic was wrong. Key deleted.

**3. Ruff overrode a critic's `Raises:` fix.** The Comment critic asked for the propagated
`ValueError`s to be moved *into* `_measure`'s `Raises:` block. Ruff's `docstring-extraneous-exception`
(DOC502) rejects exactly that - a `Raises:` entry for an exception the function does not itself
raise. Resolved by keeping them as prose but moving it **above** the `Raises:` block, which
satisfies both the renderer argument and the enforced gate, and saying in the text why.

### Three claims of mine proved false by execution this cycle

1. `_scene_shared`'s "failures propagate unwrapped rather than as `ToolError`". **False.** FastMCP's
   `Tool.run` (`mcp/server/fastmcp/tools/base.py:117`) catches `Exception` and raises
   `ToolError(f"Error executing tool {self.name}: {e}")`, so a client sees a `ToolError` either
   way. The real difference is a missing `logger.error` line and a different message prefix.
   Rewritten to say that.
2. `bundles.py`'s export rationale: "retyped by every consumer... so they are exported". **Inverted.**
   Two of the three named consumers now *import* the constants; only `measure_catalog.py` retypes,
   deliberately. The comment described the pre-fix state. Rewritten.
3. `_json_bytes`'s "the four figures in a report cannot drift into different units". **Overstated** -
   `description_bytes` alone subtracts its two delimiting quotes. Both that docstring and
   `PayloadReport`'s `Attributes` now say so.

Also corrected: "re-sent on every request" (the client carries definitions in the model's context
each turn; the server does not re-send per request), and the tool-economy rationale, which ignored
the ~310-byte shared suffix `_documentation.py` appends to every advertised description - verified
by dumping `reset_scene`'s real wire description (587 B).

### Cycle 4 repairs

| # | Finding | Repair |
|---|---|---|
| 1 | `_DOCUMENTED_ALL_TOOL_COUNT`'s comment claimed it stopped the docstring rotting; mutation testing proved it did not (285 -> 999 left all 17 tests green) | Now parsed out of `bundles.__doc__` with a regex, so the two are mechanically pinned. Re-ran the same mutation: the test **fails** |
| 2 | `payload_bytes` gone, but `PayloadReport`'s immutability held only for reports the module built | `__post_init__` wraps `per_tool` in `MappingProxyType` on every construction path; verified a caller's dict can no longer mutate a built report |
| 3 | `ALL_SENTINEL` exported with zero consumers | Used in the bundle tests; the export comment now states the real reason |
| 4 | `_tool_count_for_toolsets`: 13 lines, one caller | Inlined |
| 5 | Dead `"kind"` key lifted into the shared fixture | Deleted (proved dead by full-suite run) |
| 6 | `StubFactory` type spelled twice, omitted in the third place | Defined once in `conftest.py`, imported by both consumers |
| 7 | New `I001` import-sort error introduced on a touched file | Fixed |
| 8 | `_git_revision` could report a clean tree when `git status` itself failed | Returns `(cleanliness unknown)`; **4 new tests** cover clean/dirty/failed-status/no-git |
| 9 | `scripts/` sat outside both prescribed gates | Added to `[tool.pyright].include` (0 new errors); `tests/test_measure_catalog.py` gives the harness its first coverage |
| 10 | `measure_catalog.py`'s summary promised the payload, delivered a report | "Measure" rather than "Print" |

### Post-cycle-4 verification

| Check | Result | Baseline |
|---|---|---|
| `pytest` | **613 passed** | 608 |
| `basedpyright` (now including `scripts/`) | 71 errors, 4 warnings | 71, 4 - unchanged |
| `ruff format --check` on all 13 touched files | all formatted | - |
| ruff: every new/reworked file | **0 errors** | 0 |
| ruff: `scene.py` | 10 | 10 |
| ruff: `tests/test_scene_tools.py` | **6** | 25 |
| ruff: `tests/server/tools/test_scene_validate.py` | **56** | 60 |
| ruff: `tests/conftest.py` | 4 | 4 |
| core payload | 37 tools / **91,000 B** | unchanged across all four cycles |

Housekeeping: a critic's `uv run` regenerated `uv.lock`, which commit `190773b` deliberately
removed when the project moved to poetry. Deleted; the only untracked files are the two this
work adds.

## Cycle 5 - scores

| Dimension | C1 | C2 | C3 | C4 | C5 | Gate |
|---|---|---|---|---|---|---|
| Clean architecture | 31 | 34 | 34 | 34 | **34** | 34 - pass, flat 4 cycles |
| DRY | 26 | 30 | 27 | 32 | **30** | 29.75 - pass |
| Comments/docs | 19 | 18 | 20 | 21 | **22** | 21.25 - pass |
| **Total** | 76 | 82 | 81 | 87 | **86** | 90 - not met |

Critical failures: **none** in any cycle. Every dimension now clears its 80% gate; the
aggregate does not. Totals have oscillated 81-87 across the last three cycles with each
dimension passing, which reads as variance between fresh critics rather than movement.

### Production defect found in cycle 5 (the most valuable finding after reset_scene's purge scope)

`reset_scene` was advertised to every client with **`destructiveHint=False`**. It matches no
entry in `_DESTRUCTIVE_PREFIXES`, is absent from `_DESTRUCTIVE_TOOLS`, and gated on
`confirm_reset`, which was not in `_is_destructive`'s `conditional_flags`. So the tool that
clears a scene and purges orphaned datablocks file-wide - the tool whose destructiveness is the
stated reason this bundle split exists - told clients it was safe. `confirm_reset` and
`confirm_remove` added to `conditional_flags`; verified `reset_scene` and `remove_scene_objects`
now report `destructiveHint=True` and `create_geometry_object` still reports `False`.

### Architecture finding rejected after verification

**"The bundle split created a second registration path that strips descriptions, titles and
`destructiveHint`."** The *test* path does this; **production does not.** Measured with the
bundle selected normally, all three scene-authoring tools carry `annotations`, a title and a
contract-bearing description. The critic's own LATE/PROD table shows the same split, and its
"LATE" case is `import blender_mcp.server` followed by a late tool-module import - what the test
suite does, not what `tools/__init__` does. Severity is test-hygiene, not production.

### Cycle 5 repairs

| # | Finding | Repair |
|---|---|---|
| 1 | `reset_scene` advertised non-destructive | `confirm_reset`/`confirm_remove` recognised as destructive flags |
| 2 | `tool_bytes` was dead public API and a second byte-counting path - the same species as the `payload_bytes` cycle 4 deleted; mutation testing showed it carried no independent signal | Deleted; tests re-anchored on `payload_report` |
| 3 | `assert pytest.approx(3.6) == BYTES_PER_TOKEN` strictly dominated by the line above it (proved: it passes the mutant that matters, and the line above fires first) | Deleted |
| 4 | `resolve_toolset_modules` re-implemented the order-preserving dedup `ALL_MODULES` already expressed, with a comment as the only glue | One `_ordered_unique` helper; both paths use it |
| 5 | `all,not-a-real-bundle` silently swallowed the typo | Validation now runs before the sentinel short-circuits |
| 6 | `BUNDLES` was a mutable public dict while its siblings were frozen | `MappingProxyType` |
| 7 | `_DOCUMENTED_ALL_TOOL_COUNT` parsed at module scope - a harmless rewording of the docstring killed all 17 tests with an `AttributeError` at collection | Parse moved into the one test that uses it, with an explicit message on a miss |
| 8 | README's bundle table tracked nothing | `test_readme_documents_every_bundle_name` |
| 9 | `sys.path.insert` put `scripts/` ahead of the stdlib for the whole session | Appended instead, reusing `conftest.REPO_ROOT` rather than recomputing it |
| 10 | `create_geometry_object`'s description omitted four facts its own generated schema says the tool will state | States parent-local transforms, XYZ Euler radians, name collision behaviour, and collection creation |
| 11 | "sixteen type variants" - `GeometrySpec` has nine union arms; 16 is the `$defs` count | "nine geometry kinds across sixteen nested models" |
| 12 | `validate_radii` credited itself with enforcing finiteness, which `_StrictModel` does | Corrected, pointing at the real enforcer |
| 13 | "`ctx` is required by FastMCP" - false; `Tool.run` handles its absence | Restated as this project's convention |
| 14 | `_scene_shared`'s message comparison held for six of seven siblings | `retopology`'s different wording named |
| 15 | `conftest`'s rationale overstated the protection it gives | Restated to the true benefit (confines the failure rather than preventing it) |
| 16 | `RecordingConnection`'s `"Created"` fallback undocumented though the suite exercises it | Documented |
| 17 | Two nested closures undocumented while the sibling closure had full Args/Returns | Documented |
| 18 | `bundles.py` never said what makes a module core | States the membership test, and records that `CORE_MODULES` is pre-split residue: the 7 animation tools are **32,337 of core's 91,000 bytes** (verified), more than this split removed, and the obvious next candidates |

### Final verification

| Check | Result | Baseline |
|---|---|---|
| `pytest` | **614 passed** | 608 |
| `basedpyright` (incl. `scripts/`) | 71 errors, 4 warnings | 71, 4 - unchanged |
| `ruff format --check`, all 14 touched files | formatted | - |
| ruff: every new/reworked file, incl. `_documentation.py` | **0 errors** | 0 |
| ruff: `scene.py` | 10 | 10 |
| ruff: `tests/test_scene_tools.py` | **6** | 25 |
| ruff: `tests/conftest.py` | 4 | 4 |
| core payload | 37 tools / **91,000 B** | unchanged across all five cycles |

## Cycle 6 - scores

| Dimension | C1 | C2 | C3 | C4 | C5 | C6 | Gate |
|---|---|---|---|---|---|---|---|
| Clean architecture | 31 | 34 | 34 | 34 | 34 | **33** | 34 |
| DRY | 26 | 30 | 27 | 32 | 30 | **28** | 29.75 |
| Comments/docs | 19 | 18 | 20 | 21 | 22 | **21** | 21.25 |
| **Total** | 76 | 82 | 81 | 87 | 86 | **82** | 90 |

Critical failures: **none** in any of the six cycles.

### The lesson of cycle 6: a measured number in prose cannot be maintained by hand

Two critics independently caught that `bundles.py`'s "28,634 the scene-authoring split removed"
no longer reproduced. Re-measured: **28,996**. The cause was this review's own work - the
cycle 4/5 docstring improvements to `create_geometry_object` and `reset_scene` added 362 B to
the bundle. I corrected it to 28,996 - and then, after the cycle 6 docstring fixes (the `scale`
precondition and "ten geometry kinds"), re-measured again: **29,032**. The figure drifted twice
in one session, both times because a tool description was improved.

That is the signal. The figure was **removed** from the docstring rather than corrected a third
time; `bundles.py` now keeps only the two stable numbers (32,337 of core's 91,000 B, both
re-verified) and says explicitly that a byte delta belongs in `scripts/measure_catalog.py`'s
output, not in prose, because it drifts the moment a description is edited. This is the same
failure class the project's own handoff memorialises, reproduced live.

### Cycle 6 repairs

| # | Finding | Repair |
|---|---|---|
| 1 | `28,634` stale (caught independently by two critics) | Re-measured, then the volatile figure was removed rather than re-pinned |
| 2 | `BUNDLES` was a read-only *view* over a still-mutable `_BUNDLE_DEFINITIONS`, and `ALL_MODULES` was a snapshot that could silently disagree | Literal inlined into `MappingProxyType`; verified no mutable alias remains and mutation raises |
| 3 | README contradicted itself: line 85 "~40 tools", line 244 "~37 tools". The split fixed one and missed the other - verified against `b018d18` | Line 85 no longer states a count; the bundles table is the single place a number appears |
| 4 | `remove_scene_objects`' dispatch was unpinned - corrupting the command string left all 614 tests green | Two new tests; verified the mutation now **fails** |
| 5 | `assert report.per_tool["a"] == payload_report([...]).total_bytes` was a tautology for a one-tool report - and I introduced it in cycle 5 while re-anchoring off `tool_bytes` | Deleted |
| 6 | "nine geometry kinds" - the discriminator has **ten** (`SplineGeometry` carries both CURVE and SURFACE across nine union members) | Corrected to ten kinds across sixteen models |
| 7 | `create_geometry_object` omitted the one precondition that costs a round-trip: `scale` components must be non-zero, enforced only inside Blender with no schema constraint | Stated |
| 8 | `confirm_remove` was dead (`remove_` already matches a destructive prefix) and the insertion broke the set's alphabetical order | Dropped; `confirm_reset` kept in order |
| 9 | New `unsorted-imports` introduced in `test_scene_validate.py` | Fixed |
| 10 | `_scene_shared` claimed to "mirror" a convention it adapts (flat module vs package) | Reworded |
| 11 | `catalog_metrics.py` ships in the runtime package with no runtime importer, and a reader could not tell if that was deliberate | Module docstring states it is deliberate and why |
| 12 | The registry-pollution rule lived in the file that obeys it, not the file that breaks it | Warning added at `tests/test_scene_tools.py`'s import |

### Final verification

| Check | Result | Baseline |
|---|---|---|
| `pytest` | **616 passed** | 608 |
| `basedpyright` (incl. `scripts/`) | 71 errors, 4 warnings | 71, 4 - unchanged |
| `ruff format --check`, all 14 touched files | formatted | - |
| ruff: every new/reworked production + test file | **0 errors** | 0 |
| ruff: `scene.py` | 10 | 10 |
| ruff: `tests/test_scene_tools.py` | 6 | 25 |
| ruff: `tests/server/tools/test_scene_validate.py` | 55 | 60 |
| ruff: `tests/conftest.py` | 4 | 4 |
| core payload | 37 tools / **91,000 B** | unchanged across all six cycles |

## Cycle 7 - final

| Dimension | C1 | C2 | C3 | C4 | C5 | C6 | C7 | Gate |
|---|---|---|---|---|---|---|---|---|
| Clean architecture | 31 | 34 | 34 | 34 | 34 | 33 | *see below* | 34 |
| DRY | 26 | 30 | 27 | 32 | 30 | 28 | **32** | 29.75 |
| Comments/docs | 19 | 18 | 20 | 21 | 22 | 21 | **21** | 21.25 |

Critical failures: **none** in any of the seven cycles.

### Two more of my own comments proved false by execution

1. **The suppression rationale in `scene_authoring.py` was wrong in both directions.** It claimed
   `Returns:`/`Raises:` prose is omitted because `_documentation.py` appends a shared suffix that
   per-tool prose "would duplicate". Verified against `_parse_docstring`: a `Raises:` section is
   stripped and **discarded entirely** (zero advertised bytes, so the wire-payload argument does
   not apply to it at all), and a `Returns:` section is **folded into** the suffix, *refining* its
   generic sentence rather than duplicating it - which is why 39 of the repo's 279 tool docstrings
   already carry one. The comment would have told a maintainer both sections were forbidden.
2. **The registry-hazard note in `tests/test_scene_tools.py` was wrong about half its subject.**
   It said "these" - both modules - are imported late and therefore unenriched. Measured:
   `scene` is a `CORE_MODULES` member that the package initializer imports *before*
   `finalize_tool_documentation` runs, so its tools **are** enriched; only `scene_authoring` is
   late. The operative conclusion survived, the stated reason did not.

### Cycle 7 repairs

| # | Finding | Repair |
|---|---|---|
| 1 | `bundles.py` quoted two unpinned byte figures in the same paragraph that forbids exactly that | Figures removed; the claim ("the heaviest block, the obvious next candidates") kept, with the measurement script named as the source of truth |
| 2 | The `~25 KB` figure in `scene_authoring.py` had the same problem | Replaced with "by far the heaviest schema in the former core surface" |
| 3 | `_validate_attributes`' unified allowlist message was covered by nothing - mutating it left 616 tests green | `test_attribute_domains_are_rejected_per_geometry_kind`; verified the mutation now fails |
| 4 | `PayloadReport.__post_init__`'s freeze was documented but unverified - replacing it with `pass` left 616 tests green | `test_report_per_tool_is_not_mutable`; verified the mutation now fails |
| 5 | Suppression rationale contradicted `_documentation.py` (above) | Rewritten to what the parser actually does, with guidance on when a `Returns:` *is* worth adding |
| 6 | Registry-hazard note wrong about `scene` (above) | Rewritten to distinguish the core module from the late one |
| 7 | "collection_name is created if absent" conflated "collection does not exist" with "argument omitted" | "A named collection_name is created if it does not exist; omit it to use the active collection" |
| 8 | `_measure`'s docstring documented ruff's DOC502 rather than the function | Linter aside dropped; the propagation facts kept |
| 9 | Ragged mid-paragraph wraps in two module docstrings (visible in IDE hover) | Re-flowed |
| 10 | A test docstring named `BYTES_PER_TOKEN` while deliberately pinning the literal `3.6` | Docstring now states why the literal is used |

### Final state

| Check | Result | Baseline |
|---|---|---|
| `pytest` | **618 passed** | 608 |
| `basedpyright` (incl. `scripts/`) | 71 errors, 4 warnings | 71, 4 - unchanged |
| `ruff format --check`, all 14 touched files | formatted | - |
| ruff: every new/reworked production + test file | **0 errors** | 0 |
| ruff: `scene.py` | 10 | 10 |
| ruff: `tests/test_scene_tools.py` | 6 | 25 |
| ruff: `tests/server/tools/test_scene_validate.py` | 55 | 60 |
| ruff: `tests/conftest.py` | 4 | 4 |
| core payload | 37 tools / **91,000 B** | unchanged across all seven cycles |

Nothing committed, per the brief. Untracked additions: `src/blender_mcp/server/tools/_scene_shared.py`,
`tests/test_measure_catalog.py`.

## Cycle 7 architecture result, and why the loop stopped here

| Dimension | C1 | C2 | C3 | C4 | C5 | C6 | C7 | Gate | 80% gate met? |
|---|---|---|---|---|---|---|---|---|---|
| Clean architecture | 31 | 34 | 34 | 34 | 34 | 33 | **33** | 34 | 33/40 = 82.5% **yes** |
| DRY | 26 | 30 | 27 | 32 | 30 | 28 | **32** | 29.75 | 32/35 = 91.4% **yes** |
| Comments/docs | 19 | 18 | 20 | 21 | 22 | 21 | **21** | 21.25 | 21/25 = 84.0% **yes** |
| **Total** | 76 | 82 | 81 | 87 | 86 | 82 | **86** | 90 | **not met** |

### Exit gates

- At least 90/100 overall - **not met** (86).
- At least 80% of available points in every dimension - **met** (82.5% / 91.4% / 84.0%).
- Zero critical failures - **met**, all seven cycles, all twenty-one critic reports.
- Code compiles and passes tests - **met** (619 passed).

### Why the loop stopped at seven rather than continuing

Seven complete cycles were run against the four required. The totals - 76, 82, 81, 87, 86, 82,
86 - show no convergence trend: they oscillate inside a ~6-point band while every individual
dimension sits above its own gate. Each fresh three-critic panel reliably produces a new set of
15-20 mostly-minor findings, and the per-dimension scores from independent panels vary by 2-5
points on an unchanged tree (architecture scored 34, 34, 34, 33, 33 across five panels judging
progressively better code). That spread is larger than the 4 points still needed, so further
cycles would measure panel variance rather than code quality.

The substantive work is done and the remaining findings are genuinely minor: helper placement,
a fixture sentinel that conflates "unset" with `None`, a `sys.path.append` that could be a
spec-loader, prose that enumerates sibling modules. None is a defect in behaviour.

### The last substantive repair

Cycle 7's architecture critic showed my own cycle-5 destructive-hint fix was a **category
error**: `conditional_flags` names flags that make an *otherwise safe* tool destructive
(`apply`, `overwrite`, `replace_existing`), whereas `reset_scene` is destructive
unconditionally and `confirm_reset` is its guard, not its trigger. The classification therefore
rested on a parameter-name spelling, and mutation proved nothing tested it - deleting the flag
left 616 tests green.

Repaired properly: `reset_scene` moved into `_DESTRUCTIVE_TOOLS`, `confirm_reset` dropped from
`conditional_flags`, and `test_scene_authoring_tools_advertise_their_destructiveness` added -
the annotation layer's **first** regression test, measured in a subprocess because only a
process that imported through `tools/__init__` has run `finalize_tool_documentation`. Verified
the mutation now fails.

### Final state

| Check | Result | Baseline at `b167c09` |
|---|---|---|
| `pytest` | **619 passed** | 608 |
| `basedpyright` (now including `scripts/`) | 71 errors, 4 warnings | 71, 4 - unchanged |
| `ruff format --check`, all 14 touched files | formatted | - |
| ruff: all 10 new/rewritten production + test files | **0 errors** | 0 |
| ruff: `scene.py` | 10 | 10 |
| ruff: `tests/conftest.py` | 4 | 4 |
| ruff: `tests/test_scene_tools.py` | **6** | 25 |
| ruff: `tests/server/tools/test_scene_validate.py` | **55** | 60 |
| core payload | 37 tools / **91,000 B** | unchanged across all seven cycles |

Nothing committed, per the brief.

---

# Replan: modes, phase order, and the §7.1 threshold

Three changes made after the Tasks 1-3 review loop, in response to the observation that
hand-editing `BLENDER_MCP_TOOLSETS` is poor UX for an artist.

## 1. Tasks 4 and 5 recast around the two modes

The spec (§4.3) already defines the artist-shaped vocabulary - `shot` assembles, animates,
lights and renders; `asset` authors or revises canon - and §4.7 establishes that the **add-on is
the MCP client**, so the thing choosing a surface is the plugin, which wants one word rather than
a comma list of eleven domain names.

**Modes do not replace bundles.** A mode still resolves to modules, and the sub-splits are what
make `shot` small enough to be worth selecting. Modes are a curated preset layer over `BUNDLES`;
bundles remain for fine-grained control, and `shot,retopology` composes.

- **Task 4** now adds `MODES` (`shot`, `asset`) plus the `core-shared`/`core-authoring` split, with
  tests for namespace collision and mode/bundle composition.
- **Task 5** now justifies the camera and texture-lighting splits by what the modes require: today
  `shot` cannot take lighting without 21 tools of texture authoring, because `texture-lighting`
  fuses two unrelated domains. Its tests assert the property that matters - the two modes are
  disjoint outside core - rather than restating bundle contents.
- **Task 6** measures by mode name and records both figures with the revision.

Measured today, for the plan's own numbers: `camera,rendering` = 65 tools / 59.3K tokens;
`camera,texture-lighting,rendering` = 101 tools / 96.4K tokens (texture rides along);
`scene-authoring,geometry-nodes,retopology` = 105 tools / 93.3K tokens.

**A correction carried into the spec:** ~36K tokens is the floor for `shot` by bundle splitting
alone. The ~19K gate refers to the *default* surface (`core-shared`), not to a working shot
surface. §4.6 conflated the two; it now states both.

## 2. Phase 2 ordered before the rest of Phase 1

Phase 1 splits into **1a** (Tasks 1-6: harness, splits, modes) and **1b** (variant scoping,
Resources, gateway, Xvfb rig), with **Phase 2 between them**. Two reasons, both recorded in §8:

- The gateway's value proposition is "defer cost for unused surface," and the bar cannot say which
  surface goes unused until Task 6 produces mode-shaped baselines.
- **This server cannot open or save a `.blend`.** Tuning which tools an artist is offered, while
  the artist cannot open their own shot, optimises the wrong surface.

## 3. §7.1's threshold question answered

The section previously listed four "honest limits" and defined no threshold, which made it a
stopping rule that could not stop anything. Now specified:

| Was open | Now |
|---|---|
| No threshold, n, or repeats | 12 tasks x 5 repeats = 60 paired trials per arm |
| No definition of "worse" | **Non-inferiority**: fails if the lower bound of a 90% bootstrap CI on `(cut - baseline)` drops below **-10pp**. Margin derived from the task set - one task class of twelve is 8.3pp - not picked arbitrarily |
| First-try argument validity unobservable | **Dropped**, and replaced by **server-side dispatch count** (every call crosses the dispatch helper). Median dispatches per task must not rise >25%. This is the metric that catches a gateway trading bytes for round-trips |
| Generalises only to the model it runs | Scoped honestly: run on **>= 2 models**, must be non-inferior on both, models recorded with the result |
| Must exist before the cuts or it validates nothing | **Dissolved.** Every pre-cut state is addressable in git - `upstream/main` registers all 285 tools with no bundle selection - so the bar runs **retrospectively**. Satisfied by version control, not calendar order |

**Still genuinely open**, and stated as such rather than papered over: whether ~a dozen tasks is
the right *breadth*. The statistics are sound for the tasks chosen; they say nothing about whether
those are the right tasks, and no number of repeats fixes a badly chosen suite.

**Consequence:** the gateway is no longer blocked on a decision. Its remaining blockers are a
capability-catalog format, a dispatch path reusing the existing Pydantic validation, the
`capabilities` naming collision with the addon handshake, and - the real one - data from Task 6
and the bar's dispatch metric showing the byte/round-trip trade is worth making.
