# Phase 1 Task State

Updated: 2026-09-11T22:25-06:00

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
| 2 | Make bundle tests capable of failing | not started | — | n/a | Also lands `ALL_MODULES` dedup, needed by Task 5's alias. |
| 3 | Split `scene` authoring/destructive tools | not started | — | — | |
| 4 | Split `CORE_MODULES` | not started | — | — | |
| 5 | Split `camera` and `texture-lighting` | not started | — | — | Requires lazy package re-export; see Known issues. |
| 6 | Pin the shot-mode payload ceiling | not started | — | n/a | |

## Last successful commands

Run by the controller at `313740a`, not taken from a subagent report:

- `.venv/bin/python -m pytest` -> **605 passed**
- `.venv/bin/python scripts/measure_catalog.py all` -> `revision : 313740a (dirty)`, `tools : 285`,
  `bytes : 1,180,620`, `tokens : ~327,950`, schemas 77%
- `ruff check` / `ruff format --check` / `basedpyright` on the three Task 1 files -> clean, 0 errors
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
  duplicate the `lighting.*` modules and break Task 2's no-duplicates assertion. Dedup lands in
  Task 2.
- `CLAUDE.md` carries an unrelated uncommitted GitNexus re-index edit that predates this session. It
  is deliberately left unstaged and out of every task commit.

## Current rubric score — Task 1 (fresh-context critic, opus, four lenses)

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
