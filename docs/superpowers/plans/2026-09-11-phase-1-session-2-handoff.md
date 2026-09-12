# Phase 1 Session 2 Handoff — Tasks 4, 5, 6

Paste this whole file as the opening prompt for a fresh session.

---

## 01 — Where things stand

Session 1 completed **Tasks 1, 2 and 3** of `docs/superpowers/plans/2026-09-11-phase-1-catalog.md`.
Each was implemented by a subagent, verified independently by the controller, reviewed by a
fresh-context four-lens critic, and committed. **Nothing has been pushed.**

| Task | Commit | Critic score | Measured effect |
|---|---|---|---|
| 1 — payload measurement harness | `27ec3aa`, `270958a`, `fe7b6df` | 90/100 | none (adds no tools) |
| 2 — bundle tests made fail-capable | `2981771` | 98/100 | none (tests only) |
| 3 — scene authoring/destructive split | `b018d18` | 97/100 | default surface **−28,634 B** |
| — | `f9394c6` | — | chore: generated CLAUDE.md block |

`main` is at `f9394c6`, working tree clean, **608 tests passing**.

**Tasks 4, 5 and 6 remain.** Scope is still Tasks 1-6 only; everything the plan defers
(variant scoping, MCP Resources, the typed gateway, the Xvfb rig, the §7.1 bar) stays deferred.

## 02 — Read before doing anything

1. **`docs/superpowers/plans/PHASE1_TASK_STATE.md`** — the live state file. It carries the measured
   baselines, the predicted targets for Tasks 4-6, every ruling made in session 1 with its cost if
   wrong, the parked minor findings, and the reconciliation against the spec. **Start here.**
2. `CLAUDE.md` — project standards.
3. `docs/superpowers/plans/2026-09-11-phase-1-handoff.md` — session 1's handoff. Still accurate
   except where TASK_STATE corrects it; §05's repo-wide lint gate in particular does not hold.
4. `docs/superpowers/specs/2026-09-11-episode-consistency-architecture-design.md` §4.6 and §8.
5. The plan itself, Tasks 4-6.

Then read the files you are about to change. **Do not trust the plan's line numbers** — three tasks
have moved code since it was written.

## 03 — What session 1 learned that the plan does not say

- **Every tool COUNT in the spec and the plan is exact.** So is `all` = 285 / 1,180,620 and Task 3's
  28,634 B delta. **The spec's shot-mode BYTE ladder is stale** — it puts rung 0 at 277,083 B / 77.0K
  tokens; measured, 179,203 B / 49.8K. Two of its three per-group deltas are roughly double the
  measured values. Use measured numbers; TASK_STATE has them.
- **The repo-wide lint gate cannot pass.** 9,868 pre-existing ruff errors, 12 unformatted files. The
  gate is per-file; compare the repo-wide counts before and after to prove no new debt.
- **`blender_mcp/__init__.py` eagerly imports the server**, so `BLENDER_MCP_TOOLSETS` must be set
  before importing `blender_mcp` or you measure core-only. `scripts/measure_catalog.py` handles this
  and prints the revision it measured at.
- **A module's package `__init__.py` can silently defeat a bundle split.** This is the substance of
  Task 5 — see below.

## 04 — Task 5 is the hard one, and the plan is wrong about it

The plan assumes `BUNDLES["camera"] = ("camera.core", ...)` registers only those submodules.
**It does not.** `camera/__init__.py` and `lighting/__init__.py` star-import every submodule, so
importing `camera.core` imports the parent package and registers all 23 camera tools. Verified
empirically in session 1. As written, Task 5's own tests fail and the split saves nothing.

Deleting the star-imports is not available either: `tests/server/tools/camera/test_tools.py` reaches
~32 symbols through the package namespace, and §03 of the original handoff forbids weakening
existing tests.

**Intended route: a PEP 562 lazy `__getattr__` re-export** in `camera/__init__.py` and
`lighting/__init__.py`. Treat that as the substance of the task. Also required, and not in the plan:

- **`camera.inspection` must be in the `camera` bundle** or `all` drops below 285.
- **`ALL_MODULES` must deduplicate** (order-preserving) when the `texture-lighting` alias lands.
  Task 2's `test_all_sentinel_selects_every_module` will fail first — **that is the deliberate
  canary, not a regression.**

## 05 — Execution protocol

Unchanged from session 1's handoff §05-§07, which worked well. In short: read the real file state
yourself; dispatch one subagent per task with a self-contained brief; never take its word — re-run
every gate yourself; then a fresh-context four-lens critic (Capability / Measurement / Test integrity
/ Code quality, gates 24/20/20/16, exit >= 90); fix loop; commit with the **measured** byte delta and
fold the TASK_STATE update into the same commit by `git commit --amend`.

The controller ledger from session 1 is at
`.superpowers/sdd/2026-09-11-phase-1-catalog/progress.md` (git-ignored) along with each task's brief,
report and review. Reading it is optional; TASK_STATE is the committed summary.

**Per-task gate:**
```
.venv/bin/python -m pytest
.venv/bin/python scripts/measure_catalog.py all        # must stay 285 tools / 1,180,620 B
.venv/bin/ruff check <files you touched>
.venv/bin/ruff format --check <files you touched>
.venv/bin/basedpyright <files you touched>
.venv/bin/ruff check .            # must still be 9,868
.venv/bin/ruff format --check .   # must still be 12 unformatted
```

## 06 — Two parked findings worth acting on

Both came out of Task 3's critic review and are recorded in TASK_STATE:

- **`src/blender_mcp/server/bundles.py:5` says "~286 tools"; the measured number is 285.** Task 4
  edits that file — fix it there.
- **Nothing in the test suite pins `all` = 285.** The phase's headline invariant currently lives only
  in a developer script and in reviewers' habits. Task 6 adds a budget regression test; adding the
  tool-count invariant beside it is the natural home.

## 07 — Constraints (unchanged, non-negotiable)

No domain tools added. No tool deleted. No capability lost — `all` reports 285 after every task. All
existing tests pass unmodified; following a symbol that moved is allowed, weakening an assertion is
not. TDD with a proven-failing test. `bpy` never imported from `src/blender_mcp/server/`. No
incidental churn. **Never put `git commit` on a later line of the same Bash call as an edit script.**
Commit per task; **do not push** — the push is a single end-of-phase gate.

## 08 — Environment facts

- Work on `main`. `.venv/bin/python`, `.venv/bin/ruff`, `.venv/bin/basedpyright`, `.venv/bin/pytest`.
- The shell is fish: pass file paths as separate arguments, never through a variable.
- Blender 5.2.1 at `/opt/homebrew/bin/blender`. The addon refuses to start in `--background` and
  `bpy.app.timers` do not fire there; none of Tasks 4-6 need a live Blender.
- Re-index GitNexus with `node .gitnexus/run.cjs analyze --index-only` (not `npx`). The index goes
  stale after every commit; prefer `git diff` and `rg` over the graph for bundle work, since
  `importlib` edges are invisible to it and `CORE_MODULES`/`BUNDLES` return `UNKNOWN`.
