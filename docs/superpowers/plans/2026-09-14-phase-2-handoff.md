# Phase 2 Handoff — Primitives (file lifecycle and linking)

Paste this whole file as the opening prompt for a fresh session.

---

## 01 — Role

You are a senior Python engineer with expertise in clean architecture, MCP servers, and the Blender Python API.
Produce clean, human-readable, DRY code with Google-style docstrings that render in an IDE.

**Research the actual system rather than reasoning about it.** This project has been burned repeatedly by
plausible assumptions that measurement disproved: a "persisted" write that did not survive a save; a schema
saving attributed to the wrong mechanism; three separate sets of payload numbers measured against the wrong
branch; a bundle split defeated by a package `__init__.py` nobody read. **Phase 2 planning added three more** —
the spec cites a path allowlist that was never built, defers an Xvfb rig that already exists on another branch,
and calls a rollback bug a "leak" when it is in fact a whole-file deletion.

**And then Phase 2 planning added a fourth of its own, which is the one to learn from.** An earlier revision of
the plan "corrected" the spec by declaring that `Collection.override_hierarchy_create` /
`ID.override_hierarchy_create` / `ID.override_create` do not exist in Blender 5.2.1. **They do exist**, with
exactly the signature the spec assumed. The false claim came from checking with `dir()` on a `bpy.types.*`
class, which in this build returns **no RNA members for anything** — see §08's methodology rule. The same
method also produced a false "`bpy.types.Library` has no `reload` method". So: measurement beats reasoning,
**but only if the measurement is valid** — a check that cannot distinguish "absent" from "invisible to this
check" is not evidence, and a confident wrong correction is more expensive than the original error because it
carries the authority of having been checked.

**And a fifth, found only on the third pass, which is the subtler failure.** The same sentence of the spec that
mapped `create_override` correctly also claimed `object.override_create()` "returns `None` — verified". That is
**false** — on a linked object it returns the new override ID; `None` is the non-overridable case. It survived
the original draft *and* the correction pass above, because that pass was arguing about whether the methods
existed and simply carried the adjacent clause forward. Nobody ever ran it. **A claim inherited across a
correction is not a checked claim**, and "that part was right and stands" is a sentence to distrust in this
repo's history: it is what a plan says when it has re-read something rather than re-run it.

When a fact is checkable by running something, run it. When you report a number, say how you got it. When you
report an *absence*, state the method and say why that method could have seen the thing if it were there. And
when you correct one clause of a claim, **re-verify the clauses next to it** — do not let them ride on the
credibility of the fix.

**Phase 2 is different from Phase 1 in kind, not just in content.** Phase 1 was subtractive: the worst realistic
outcome was a tool becoming unreachable, detectable by a count. Phase 2 writes to the user's filesystem, swaps
Blender's entire database out from under a running command loop, and touches the dispatch path every one of the
existing 285 tools goes through — and, for the mutating subset, the rollback machinery as well. (Be precise:
`drain_command_queue` → `_run_handler` is universal; `mutation_transaction` is not. `_READ_ONLY_COMMANDS` holds
**54** commands — counted, not estimated — that `_run_handler:1126` routes around the transaction, along with
dynamically-read-only and non-undo commands.) The worst realistic outcome is a destroyed `.blend`, a corrupted
scene, or a client that hangs forever. Calibrate accordingly.

## 02 — Read before doing anything

1. `CLAUDE.md` — project standards. Its threading rules, non-destructive-by-default rules, impact-analysis and
   commit rules are mandatory, not advisory.
2. `docs/superpowers/plans/2026-09-14-phase-2-primitives.md` — **the plan you are executing.** Read **§0
   (Verified current state) in full before anything else**; it is the corrected map of the territory and it
   contradicts the spec in seven places.
3. `docs/superpowers/specs/2026-09-11-episode-consistency-architecture-design.md` — §4.5 (file lifecycle and its
   three open hazards), §4.7 (threading), §5 (delivery contract), §8 (phasing and the Phase 2 gate), §9
   Decisions #7 and #9, §10 Q8. **Where §4.5's operator table disagrees with the plan's §0.3, the plan is
   right — with one documented exception in the other direction:** §0.3 finding 4 now records that the spec's
   `create_override` row was *correct* and an earlier plan revision was wrong about it. Read that finding before
   you trust either document's API claims, and re-verify anything you depend on.
4. `docs/superpowers/plans/PHASE1_TASK_STATE.md` — not for its content (Phase 1 is done) but for its method:
   how rulings were recorded with their cost-if-wrong, how critic cycles were run, and how a measured number is
   treated versus an asserted one. Skim the "Rulings made during session 1" and "Task 5 is not what the plan
   thinks it is" sections in particular.
5. `docs/superpowers/plans/2026-09-11-phase-1-handoff.md` — the protocol this document adapts. Still broadly
   accurate; §07's rubric is replaced by §07 here.

Then read the files you are about to change. **Do not trust this plan's line numbers after the first task
lands** — they were accurate at `523f427` and every task moves them.

## 03 — Non-negotiable constraints

- **No arbitrary code execution, in any form** (spec Decision #7). No `exec`/`eval` path, no "run this Python"
  command, no tool that accepts a code string. If a task seems to need one, stop and escalate.
- **The plugin↔MCP loop is kept** (spec Decision #9). No task may collapse the socket hop.
- **`bpy` is never imported from `src/blender_mcp/server/`.** That code runs outside Blender. Conversely, addon
  code must never import from `src/blender_mcp/server/`.
- **Blender data and `bpy` operators run on Blender's main thread only.** Socket and client threads may receive
  and queue; they must not mutate. **Never call `bpy.app.timers.register()` off the main thread**
  (`server_core.py:383-385`) — `tests/server/test_threading.py`'s stub raises if you do, and that test is there
  because this bug shipped once.
- **No capability may be lost.** Phase 2 adds tools; it removes none. `all`'s count moves up and
  `bundles.py`'s docstring figure moves with it (a test parses that figure).
- **Nothing destructive without explicit confirmation** (CLAUDE.md). Overwriting a `.blend`, resetting the
  session, and unlinking libraries each require a confirmation parameter defaulting to the safe branch. Bulk
  operations are scoped to an explicit list, never "everything".
- **Save only to an explicit caller-provided path.** Never silently overwrite a `.blend`, render, cache or asset
  file.
- **Validate operator results — per operator, not by blanket rule.** `{'CANCELLED'}` or a context error becomes
  an actionable MCP error. But **`{'CANCELLED'}` is not the failure signal for every operator**, and assuming it
  is produces a handler that reports success on every real failure. Measured 2026-09-14 against 5.2.1:
  `wm.open_mainfile`, `wm.save_mainfile` and `wm.save_as_mainfile` **raise `RuntimeError` on every failure mode
  and never return `{'CANCELLED'}`**; `wm.lib_reload` / `wm.lib_relocate` return `{'CANCELLED'}` for a bad
  library name and raise `RuntimeError` for a bad argument shape. Establish which discipline each call site
  needs by running it, and write the answer down next to the call. Do not swallow exceptions or return success
  after a failed operation. **Note:** those two `wm.lib_*` operators are recorded here as measured Blender
  behaviour, **not** as call sites — plan Task 7 rules for the data API (`lib.reload()`, which raises), and
  Task 7 criterion 4 requires that no `wm.lib_reload` / `wm.lib_relocate` call exist in the handler at all. As
  the plan stands, **no Phase 2 call site needs a `{'CANCELLED'}` check**; the per-operator discipline still
  applies to any operator a later task introduces.
- **Never hand a raw Blender exception string to the client.** Blender embeds the full absolute path in its own
  error text, so `except RuntimeError as e: ... str(e)` leaks a path and trips the rubric's automatic-critical
  path-leak item — even though every message *you* wrote was clean. **There are at least *five* distinct shapes,
  all measured 2026-09-14, and they are not interchangeable.** An earlier revision of this handoff listed three
  and the plan built its tests around exactly those three; re-measured from scratch, there are at least five:

  ```
  1 open (missing file):          Error: Cannot read file "<abs>": No such file or directory
  2 open (directory | non-.blend | corrupt magic | EMPTY PATH — four modes, one shape):
                                  Error: File format is not supported in file "<abs>"
  3 open (valid magic, corrupt/truncated body):
                                  Error: Loading "<abs>" failed: Failed to read blend file '<abs>': Missing DNA block
  4 save (unwritable destination): Error: Cannot open file <abs>@ for writing: No such file or directory
  5 reload (invalid path):        Error: Trying to reload library 'LI<name>' from invalid path '<abs>'
  ```

  Four traps, each of which defeats a different plausible sanitizer:
  - **Shape 4's `<abs>@` is a derived temp-write name**, not the caller's literal path string, so a sanitizer
    that works by substituting the caller-supplied path silently fails on it.
  - **Shape 3 carries the absolute path TWICE**, in two different quoting styles (`"…"` then `'…'`). A single
    `str.replace` or a non-global regex removes the first and ships the second. Assert **zero** absolute paths
    remain, never "the first occurrence was removed".
  - **Shape 2 has no meaningful tail after the path at all** — the cause is entirely in the tokens *before* it.
    "Keep the meaningful tail" empties this message; the rule is *keep every non-path token, wherever it sits,
    and remove every occurrence of the path*.
  - **Shape 2's empty-path variant leaks the server's process CWD**, not any caller-supplied path. Substituting
    "the caller's path" there would be a lie as well as ineffective.

  Shapes 2 and 3 are both reachable by input that **passes `resolve_blend_path`'s own validation** (real file,
  `.blend` extension, accepted magic prefix) — a `.blend` truncated to 64 bytes produces shape 3. Detect
  absolute paths structurally; substitute for presentation only. Sanitize before responding; log the full text
  server-side only. Five is a floor, not an enumeration: the message format is not a contract. This applies to
  plan Task 7's `lib.reload()` call sites as much as to Task 6's operator calls.
- **All 647 existing tests pass unmodified.** Following a symbol that moved is allowed; weakening an assertion
  is not. If an existing test looks wrong, stop and raise it.
- **TDD, and prove the test can fail.** Write the failing test, run it, see it fail, implement, then revert the
  fix and confirm it fails again. Record both failure counts. A test that passes with and without the change is
  worthless, and this repo has shipped one.
- **Every Blender-side claim is demonstrated with pasted output.** Not "this should work"; not "the operator
  returns FINISHED". Run it, paste it. Four of the plan's §0.3 corrections exist because someone reasoned
  instead.
- **No incidental churn.** Do not stage `__pycache__`, `uv.lock`, or generated files. Leave the tree clean apart
  from the intended change.
- **Commit per task. Do not push.** The push is a single end-of-phase gate after Task 10, on the user's
  go-ahead.
- **Never put `git commit` on a later line of the same Bash call as an edit script.** A newline is not `&&`; a
  script that aborts mid-way otherwise gets committed half-applied. This happened twice in one Phase 1 session.
  Apply edits independently and gate the commit on the exit code.

## 04 — PHASE2_TASK_STATE.md

Create and maintain `docs/superpowers/plans/PHASE2_TASK_STATE.md`, committed, updated after every task and every
critic cycle. It is what lets a future session resume mid-flight, and it is where every number and every ruling
lives.

```markdown
# Phase 2 Task State
Updated: <ISO timestamp>

Plan: docs/superpowers/plans/2026-09-14-phase-2-primitives.md
Handoff: docs/superpowers/plans/2026-09-14-phase-2-handoff.md
Branch: main. Base: 523f427. Scope: Tasks 1-10. No push until the end-of-phase gate.

## Baseline (measured, never estimated)
| Check | Value at 523f427 | How measured |
|---|---|---|
| pytest | 647 passed | .venv/bin/python -m pytest |
| ruff check . | 9,834 errors | .venv/bin/ruff check . |
| ruff format --check . | 12 unformatted, 326 already formatted | .venv/bin/ruff format --check . |
| basedpyright | 71 errors, 4 warnings | .venv/bin/basedpyright |
| all | 285 tools / 1,181,038 B | scripts/measure_catalog.py all |
| shot | 53 tools / 203,094 B | scripts/measure_catalog.py shot |
| core (default) | 21 tools / 63,395 B | scripts/measure_catalog.py |

## Decisions taken (each with its cost if wrong)
| # | Decision | Evidence | Cost if wrong |
|---|---|---|---|

## Tasks
| # | Task | Tier | Status | Commit | Notes |
|---|---|---|---|---|---|

## Live-Blender evidence
<Every rig run: command, revision, full transcript or the salient excerpt.>

## Known failures / blocked
<task, symptom, root cause if known, what you tried>

## Rubric scores by task
| Dimension | Score | Gate | Pass? |
```

Two sections are **mandatory and specific to this phase**:

- **"Decisions taken"** must carry Task 2's reentrancy decision (with the rule dated *before* the evidence),
  Task 5's default-policy and env-var decisions, **Task 6's `use_scripts_auto_execute` refuse-vs-warn decision**
  (it lives in Task 6, not Task 5 — `file_paths.py` is `bpy`-free and cannot read `bpy.context.preferences`),
  Task 3's epoch ruling (**the epoch does not move on a failed swap, and does not move on a successful save
  either** — a save changes no capability, so bumping it would force a pointless re-handshake on every
  connected process; the filepath change is surfaced via `get_session_info`'s `current_filepath` instead),
  **Task 1's two rulings** (port the *full* `output_roots` wiring including the 30 → 31 protocol bump — the
  phase's only bump; and the rig's local mode speaks the addon socket only, which is what Task 10's scenario is
  written against), **Task 4's item 2a ruling** (`_DATABLOCK_REPLACING_COMMANDS` rather than a
  `blend_import_post` handler, because that handler cannot distinguish a reload from a link),
  Task 7's override-route ruling (Route C), its `Library.reload()` ruling and its **`session_uid`-not-name
  signature ruling**, **Task 9's `openWorldHint` / `_BLEND_FILE_TOOLS` ruling**, and Task 9's ceiling raise
  **and its zero-margin byte budget at ten tools** — each
  with the evidence and the cost of being wrong, in the format Phase 1 used for its twelve rulings. If **Task
  1, Task 6 or Task 7** is downgraded from its Opus tier — all three were re-tiered up from Sonnet on review —
  that too is a decision and belongs in this table with its cost if wrong. The Task 3, Task 4 and Task 7 rulings are already **made** in the plan with their
  measurements; what TASK_STATE records is your **reproduction** of each, and an escalation if your measurement
  disagrees. Overturning any of them requires evidence against it recorded here, not a preference.
- **"Live-Blender evidence"** must carry the actual transcripts. A claim about Blender behaviour that has no
  transcript in this file did not happen.

## 05 — Execution protocol

Execute tasks **in plan order, one at a time.** The plan sequences shared primitives first and the ordering is
load-bearing:

- **Task 1 before everything** — nothing else is provable without the rig.
- **Task 2 before Tasks 3 and 6.** Task 2 decides the protocol those two implement. Starting them first means
  implementing a guess. If Task 2 selects the async branch, the poll surface is `get_session_info` (already one
  of Phase 2's ten tools — it does not add an eleventh), but `open_shot`'s and `get_session_info`'s schemas grow,
  so **re-measure** Task 9's byte delta rather than reusing a figure measured under the synchronous branch.
  Ruling 3's 15,000 B budget is fully committed at ten tools and has no slack to absorb the growth.
- **Tasks 3, 4 and 5 before Tasks 6 and 7.** The handlers rely on the barrier, on rollback being safe, and on
  path validation existing. Writing `open_shot` before Task 4 lands means writing a command that can delete the
  file it just opened.
- **Task 9 after Tasks 6 and 7** — the tools cannot be written before the commands they wrap.
- **Task 8 depends on Tasks 5 *and 6*** — not on Task 5 alone, as an earlier revision of this line said. It
  must describe what Task 5 closed, and its Step 1b must correctly attribute the `.blend`-as-code-execution
  containment across **three** controls, two of which are Task 6's: Task 5 requirement 1 (`use_scripts` never
  exposed in a schema — static), **Task 6 criterion 5** (explicit `use_scripts=False` on every
  `wm.open_mainfile` call — the automatic-critical one), and **Task 6 Step 4b / criterion 6** (the
  `use_scripts_auto_execute` preference check and its refuse-vs-warn decision — the undecided one). Task 8's
  output is spec §4.8, a **permanent section that outlives this plan**, so an attribution written before Task
  6's refuse-vs-warn decision exists is an attribution that will be wrong in the document Phases 4 and 5
  inherit. Otherwise it is independent: after Task 6 it may be done at any point, last or in parallel.

For each task:

1. **Read the current state of the target files yourself.** Do not trust the plan's line numbers — earlier tasks
   changed them. Verify the task's premise still holds; if it does not, say so and re-scope before writing code.
2. **Run impact analysis** on symbols you are about to touch, per CLAUDE.md. Warn explicitly on HIGH or
   CRITICAL and cross-check with `rg`. Three limits already established here — apply them, do not rediscover
   them:
   - **`UNKNOWN` is not "safe."** `drain_command_queue` is a callback passed by reference and returns
     `impactedCount: 0, risk: UNKNOWN`; `CORE_MODULES` and `BUNDLES` are module-scope constants and do the same.
     Confirm every `UNKNOWN` by text search before treating it as low risk.
   - **`mutation_transaction` returns `risk: LOW` with 1 direct caller, and text search confirms it.** An
     earlier revision of this handoff claimed text search found it "imported in three `handlers/liquid/*`
     modules as well" — **it does not; no liquid module imports it.** Re-checked 2026-09-14:
     `rg -n 'mutation_transaction' --glob '*.py' src` returns the definition (`transaction.py:225`), the single
     import and call (`server_core.py:35`, `:1131`), two comments in `server_core.py`, and four *prose* mentions
     in liquid docstrings/comments (`liquid/shot.py:449`, `liquid/quality.py:26`,
     `liquid/inspection_and_setup.py:700`, `liquid/manifest.py:5`). Those modules are *wrapped by* the
     dispatcher; they do not call it. The one-caller reading is correct — which makes this a case where
     `impact` was right and the manual "check" was wrong, so verify the check too.
   - **The index goes stale after every commit.** Prefer `git diff` and `rg` over `detect_changes` unless you
     re-run `node .gitnexus/run.cjs analyze --index-only` (not `npx`).
3. **Dispatch ONE subagent per task** with a self-contained brief: the task's section verbatim, the file state
   you actually read in step 1, the real blast radius from step 2, the constraints in §03, the exact
   verification commands, the model tier the plan recommends, and any judgement call you want reasoned about
   rather than followed. Leave changes uncommitted for your review.
   - **Honour the plan's tier recommendation.** Tasks **1**, 2, 3, 4, 5, **6**, **7** and 8 — **eight of the
     ten** — are scoped for an Opus-class implementer and the plan says why for each. Only Tasks 9 and 10 are
     Sonnet. Downgrading one is a decision to record in TASK_STATE, not a default. **Tasks 1, 6 and 7 were all
     re-tiered from Sonnet to Opus on review** — none because a design question reopened:
     - **Task 6**, because it is where three of §07's automatic-critical items are actually enforced (the
       `os.path.exists` overwrite pre-check, the no-path-leak sanitizer over Blender's own exception text, and
       the explicit `use_scripts=False` on every `open_mainfile` call).
     - **Task 7**, because it owns the only Phase 2 command that outright deletes user data (`unlink_libraries`
       → `bpy.data.libraries.remove` plus optional `orphans_purge`), an automatic-critical path-leak criterion
       on `Library.reload()`'s *most common* failure path (a missing or moved library file), and
       `reload_library` / `relocate_library`, which replace the contents of every linked datablock in place.
     - **Task 1**, because it is the foundation every other task's *evidence* rests on —
       `scripts/blender_rig.py` is named in Tasks 2, 3, 6, 7 and 10, and a live transcript is a hard exit gate
       for eight of the ten, so a rig that exercises the wrong path silently invalidates all of it. Its scope
       was also measurably wrong in three ways under the settled-looking rationale: porting `output_roots.py`
       without its five wiring sites leaves a dead module **and a red ported test** (two of
       `tests/test_output_roots.py`'s own tests assert on `get_addon_info()`); its protocol bump collided with
       one Task 3 was independently claiming; and the port adds **36 new `ruff` errors**, breaching this
       document's `<= 9,834` gate in the phase's first commit. It additionally owns the "what does the rig
       actually reach" boundary that Task 10's scenario is written against — the local mode speaks the addon
       socket only and stands up no MCP server.

     Tiering in this plan is by consequence of being wrong, not by how much is left undecided. All three
     re-tiers corrected a rationale written on the latter criterion — "the decisions are already made", "what
     remains is a precise file port" — an argument about difficulty, which does not lower the cost of a defect.
     **Treat that sentence shape as a tiering smell**; it produced three mis-tierings in one plan.
4. **Do not take the subagent's word for it.** Read the diff yourself and independently re-run every
   verification, including re-running the live-Blender evidence. A pasted transcript you did not produce is a
   claim, not evidence.
5. **Run the critic cycle** (§06) before committing.
6. **Commit** with a conventional-commit message referencing the task number and, where the task changes the
   catalog, **the measured byte delta**. No attribution lines. Update `PHASE2_TASK_STATE.md` in the same commit.
7. **If a gate fails, root-cause it before touching anything else.** Do not retry blind. If it turns out to be
   pre-existing rather than caused by your change, note it in TASK_STATE, fix it narrowly or leave it, and
   continue.

**Per-task gate:**

```
.venv/bin/python -m pytest
.venv/bin/ruff check <files you touched>
.venv/bin/ruff format --check <files you touched>
.venv/bin/basedpyright <files you touched>
.venv/bin/ruff check .            # must still be <= 9,834
.venv/bin/ruff format --check .   # must still be 12 unformatted
.venv/bin/python scripts/measure_catalog.py all   # count must match bundles.py's docstring
```

**The `<= 9,834` line bites hardest on Task 1**, which is the one task that imports foreign code: the nine
files it ports from `docker-blender` carry **36 new `ruff` errors** (35 in `tests/test_output_roots.py`, 1 in
`docker/blender/start_server.py`; `output_roots.py` and `tests/test_docker_rig.py` are clean) — measured
2026-09-14, which would take the repo to 9,870 in the phase's *first* commit. Plan Task 1 Step 3b requires
cleaning them; they are all `D103` / `ANN001` / `PLC0415` / `PLC2701` / `ANN202` / `RUF105` / `I001`, i.e.
mechanical. **If that is overruled, the new number must be written into TASK_STATE, this gate line and §08's
baseline table in the same commit** — a ceiling that drifts without all three moving together stops meaning
anything.

**Additionally, for any task that touches addon behaviour (1, 2, 3, 4, 5, 6, 7, 10):** one live-Blender run,
with its transcript in TASK_STATE. A task in that list with no transcript is not done. **Task 1 is in this
list** — an earlier revision of it omitted Task 1, which was inconsistent with Task 1's own acceptance
criterion 4 (a post-load `ping` transcript pasted into TASK_STATE, plus the timer-registration state) and with
the fact that Task 1 now ports live addon behaviour: the `writable_output_roots` handshake field and the
30 → 31 protocol bump.

**End-of-phase gate (once, after Task 10):** the per-task gate plus the full Task 10 scenario, plus the negative
cases, run from a clean tree.

## 06 — Review loop and critics

After each task's implementation and before its commit, run **at least two cycles** of: *change → examine
through the critics' eyes → diagnose → fix*. Run **four cycles on Tasks 3, 4, 5, 6 and 7** — the concurrency,
data durability and security tasks, where a defect is silent and expensive.

> ### Amendment, 2026-09-15 — cycles are gate-driven from Task 4 onward (user decision)
>
> **Applies to Tasks 4–10. Tasks 1–3 ran under the original rule and their records stand.** The fixed
> four-cycle count is replaced by a stop condition, because Task 3 measured what the count actually bought:
> cycle 1 scored 46/100, cycle 2 scored 62.5, and the gap was concentrated in a handful of *known* defects
> rather than unknown ones. Four cycles is twenty Opus agent runs per task; Task 3 spent ~4.5 hours of agent
> wall time before its third repair round. The user authorised this change on 2026-09-15 after that cost
> became visible.
>
> **1. Stop when the gate is met, not at a fixed count.** Run cycles until **≥90/100 overall, every dimension
> ≥80% of its points, and zero automatically-critical items** — then stop. **Minimum two cycles**; hard
> maximum four. §06's existing structural-escalation rule is unchanged and still bites first: below the gate
> and improving by less than one point across two consecutive complete cycles means stop patching and
> re-examine the approach.
>
> **2. Triage repairs by gate impact.** Classify every critic finding as **gate-blocking** (repair now) or
> **residual** (record in TASK_STATE with an owner and a cost, and move on). Only gate-blocking findings enter
> a repair round. Task 3's cycle-3 round carried 15 items when roughly 7 moved a dimension over its
> threshold; the rest were real but not load-bearing. A residual that is written down with an owner is a
> managed decision — an un-triaged repair queue is not more rigorous, only slower.
>
> **3. Scale the critic panel.** Cycle 1 runs all four lenses — it is the cheap insurance that catches the
> unknown. From cycle 2, run only the lenses that **failed their gate**, plus **Critic 4 (Evidence and
> Contract)**, which is the cheapest and is what catches false claims. A lens that has passed its gate does
> not need re-running unless the repairs touched its subsystem.
>
> **4. Three standing pre-checks, because each prevented failure cost Task 3 a full cycle.** These are not
> optional and they are nearly free:
>
> - **Before accepting an implementer's pushback on any design point, grep `PHASE2_TASK_STATE.md` and the
>   plan for a recorded ruling on that question.** Task 3's cycle-1 failure was exactly this: a recorded Task 2
>   decision (`PHASE2_TASK_STATE.md:2265-2275`) was overruled, uncited, and the reviewer approved it on a
>   plausible equivalence argument that held only against a different design. **A pushback against a cited
>   decision must cite the decision back.**
> - **Every repair greps for sibling instances of the defect class before it is called done.** Task 3
>   hardened `_failure_note` in one cycle and left `_library_summary` — a sibling field in the *same
>   response* — carrying the identical three defects into the next. Fix the class; name the siblings you
>   checked.
> - **Every factual claim written into a docstring must name the committed instrument that produces it, or be
>   deleted.** Four such claims were verified false across Task 3's cycles. This is the Phase 1 lesson already
>   recorded below, restated as a check because restating it as a warning did not work.
>
> **Unchanged and non-negotiable:** the automatically-critical list, the live-Blender transcript requirement,
> revert-matrix coverage of every new node, TDD with both failure counts, and "all existing tests pass
> unmodified". Those are the product, not process overhead, and nothing here relaxes them.

> ### Amendment, 2026-09-16 — findings are triaged by kind, and the score is no longer a gate (user decision)
>
> **Applies to Tasks 4–10 and supersedes points 1–3 of the 2026-09-15 amendment above.** Its point 4 (the
> three standing pre-checks) and its "unchanged and non-negotiable" list still apply.
>
> **Why.** A comparison of Task 3's cycle-1 tree (stash `5e9d482`) with its commit (`2c1d678`) found that
> cycle 2 fixed real defects — a second process answered `success` out of the wrong file (T3-1), and File →
> Save Copy poisoning `current_filepath` (T3-5) — and that cycles 3–6 were driven mainly by the rubric's
> trust-boundary floor, not by defect risk. Of the 42.75 points gained, roughly 13–18 came from real bugs
> (~2 of ~15¾ hours), 20–25 from hardening against a hostile socket peer, and ~5 from record-keeping.
> Three defects were **introduced by hardening repairs** (T3-12's `settimeout(0.0)`, verified absent from
> `5e9d482` and present in `2983041`; T3-16; T3-24's `elif`), each costing a cycle. The socket is
> unauthenticated and loopback-only; the realistic threat model is one local user.
>
> **1. Classify every critic finding as exactly one of:**
>
> - **Bug / automatically critical** — wrong result, data loss, hang, dropped healthy connection, or any item
>   on §07's automatically-critical list (including a test that still passes with its fix reverted).
>   **Fixed before commit.**
> - **Hardening** — matters only if the socket peer is hostile, or after a future auth/remote-exposure change.
>   **Not fixed in the task.** Recorded with its cost in TASK_STATE's hardening backlog, which **Task 8**
>   prioritises in its design deliverable. Implementation is a follow-up after Phase 2 unless the user pulls an
>   item in.
> - **Record-keeping** — false or unbounded docstring claims, stale numbers, record structure. **Fixed once, at
>   commit time, in the same task**, after the code is frozen and re-measured. Not re-reviewed.
>
> **2. At most two cycles.** Cycle 1 runs all four critic lenses. **Cycle 2 runs only if cycle 1 produced
> blocking repairs**, and only the lenses that had blocking findings; each re-run critic also checks the
> repairs for newly introduced defects. If cycle 1 finds nothing blocking, the task closes after one cycle.
> **If a blocking finding is still open after cycle 2, stop and ask the user — there is no cycle 3.**
>
> **3. The score is recorded, not gated.** The stop condition is **zero open bugs and zero
> automatically-critical items**. §07's 100-point scores are still recorded per cycle for comparison, but
> ≥90 overall and ≥80% per dimension no longer block a commit.

**Task 1 is Opus-tier but stays at two cycles**, and the distinction is worth stating rather than leaving as an
apparent inconsistency: its consequence is high because every other task's *evidence* runs through it (see
§05), but its defects are of a kind two cycles find — a missing wiring site makes a ported test fail, a missed
protocol bump makes `tests/test_addon_manager.py` fail, unclean lint fails the gate. The four-cycle list is for
defects that pass every gate and surface later.

Tasks 6 and 7 are in the four-cycle list for the same reason they were re-tiered to Opus:

- **Task 6** is where the overwrite pre-check, the path-leak sanitizer and the explicit `use_scripts=False`
  argument are actually enforced, and all three fail silently. (The
  `preferences.filepaths.use_scripts_auto_execute` check is Task 6's *undecided* element — criterion 6, and a
  refuse-vs-warn judgement — but it is **not** one of §07's automatic-critical items; criterion 5, the explicit
  `use_scripts=False`, is the one on that list.)
- **Task 7** performs the phase's only outright deletion of user data, carries a path-leak criterion on its
  most ordinary failure path, and replaces linked datablock contents in place.

Criticism applies to **the code this task changed**, not to work already committed. For each finding record: the
criticism, severity, affected subsystem, likely root cause, and an actionable correction. Fix systemic issues
before isolated polish.

Where subagents are available, dispatch fresh-context critics who receive only: the task's acceptance criteria,
the diff, and the rubric. Four lenses, matched to this phase's rubric:

### Critic 1 — The Data Durability Auditor *(highest stakes)*
Can this change destroy or silently lose something the user owns? Trace every path that writes, removes, or
purges. Specifically: can a rollback reach datablocks from a file it did not snapshot — **including via a
library reload, not only via a file swap?** `lib.reload()` churns every linked datablock's `session_uid` while
firing only `blend_import_*` and **never `load_post`**, so ask directly: are `reload_library`,
`relocate_library` and `unlink_libraries` outside `mutation_transaction` (plan Task 4's
`_DATABLOCK_REPLACING_COMMANDS`), and is **`link_canon_library` still inside it with its failed-link rollback
intact**? A fix that invalidates on `blend_import_post` unconditionally passes the first question and silently
fails the second, because `libraries.load()` fires that handler too. Demand both assertions, not one. Can a save overwrite
without confirmation — and is the guard an **explicit `os.path.exists` pre-check inside the handler**, not
`check_existing=True`, which is inert outside the interactive file browser and overwrites silently? Is
`relative_remap=False` passed **explicitly** to `save_as_mainfile` rather than inheriting its `True` default,
which rewrites library paths? Can an unlink touch a library that was not named? Does a link that fails leave the
scene recoverable? Does anything call `remove()` on a datablock that may have been freed — and would
`contextlib.suppress(Exception)` hide it if it did? Is every operator failure actually caught — remembering
that the file operators **raise `RuntimeError` and never return `{'CANCELLED'}`**, so a CANCELLED-only check
reports success on every real failure? **Demand that the destructive path be demonstrated, not argued**: show
the test that proves it is blocked, and for the overwrite case, the real file whose bytes did not change.

### Critic 2 — The Concurrency and Liveness Auditor
Can a client hang? For every code path this task added, name the response the client receives and prove it
arrives. Check: does every dequeued command get exactly one response on the right socket? Can a command be
executed against a file it was not queued against? Is the barrier driven by observable addon state rather than
by something one client tracks (`README.md:85-89` and `:277-294` document multiple server processes against one Blender)? Is
`bpy.app.timers.register` still only ever called from the main thread? Does `stop()` still release everything?
A deadlock that only shows under two clients is still a deadlock.

### Critic 3 — The Trust Boundary Auditor
Assume the socket is hostile — it is unauthenticated and, after this phase, it can read and overwrite files.
For each new parameter that becomes a path: can `..` escape after normalisation? Can a symlink? Can a symlinked
*parent*? Is a **bare relative path** (no `//` prefix) actually absolutized — `bpy.path.abspath` passes those
through untouched, so a missing `os.path.abspath` wrapper resolves them against the process CWD? Does the root
check use `commonpath` on realpath'd forms rather than `startswith`? Is the extension check defeated by a
trailing dot or a case change? Does the magic-byte check **accept all three valid `.blend` signatures**
(uncompressed, zstd, legacy gzip) — a false rejection is a bug too? Does an error message leak an absolute path,
a credential, or a full traceback to the client (CLAUDE.md forbids all three) — **including text that came from
Blender's own exceptions**, which embed the absolute path verbatim and defeat a guard that only covers
hand-written messages, **and including `Library.reload()`'s `Trying to reload library '…' from invalid path
'<abs>'`, which is Task 7's problem and not only Task 6's**? **Was the sanitizer proven against at minimum the
five real shapes in §03, each captured from a real run — not the three an earlier revision listed?**
Specifically: does it survive the *save* shape, whose `<abs>@` is a string Blender derived from the caller's
path rather than the caller's path itself — so a substitution-based sanitizer leaves it intact? Does it survive
the `Missing DNA block` shape, which carries the absolute path **twice** in two quoting styles — and is the
assertion that **zero** absolute paths remain, rather than that the first occurrence went? Does it still name
the cause on the `File format is not supported` shape, which has **no tail after the path**, so a
"keep the meaningful tail" implementation returns an empty message? And does the empty-path case — which leaks
the server's **process CWD**, not any caller-supplied path — come back clean? Is `use_scripts` exposed
anywhere, and is `use_scripts=False`
passed explicitly on every `open_mainfile` call (a `.blend` can execute embedded Python on load, and this socket
is unauthenticated)? **Is `preferences.filepaths.use_scripts_auto_execute` checked before the load** (plan Task
6 Step 4b — two safe defaults are not a control), with the refuse-vs-warn choice recorded? Is the active policy
observable by the client, or is it an unenforced default nobody can see?

### Critic 4 — The Evidence and Contract Auditor
Is every Blender-side claim backed by a pasted transcript in TASK_STATE, produced by the reviewer and not only
by the implementer? Can each new test fail — show the revert-and-confirm with both counts? Was any existing
assertion weakened, or any test file edited to accommodate the implementation? Is the envelope unchanged? Is the
addon's capability set and the server's protocol version still in sync (**one bump for the phase, taken by Task
1**), and is `tests/test_addon_manager.py` still green? **Are all three tool annotations right —
`destructiveHint`, `readOnlyHint` *and* `openWorldHint`?** `_documentation.py:612-630` emits every one of them
unconditionally, so a tool missing from the relevant set ships an affirmatively wrong `False`, not a missing
hint; `openWorldHint` in particular is `name in _EXTERNAL_TOOLS or name in _FILE_TOOLS`, and Phase 2's five
filesystem-touching tools (`open_shot`, `save_shot`, `link_canon_library`, `reload_library`,
`relocate_library`) are in neither set by default. Check that the hint was fixed **without** inheriting
`_FILE_TOOLS`' effects prose, which asserts "it does not save the .blend file". **Does any command take a
datablock *name* as a handle where a `session_uid` is required** (plan Task 7 — after a Route C override, two
collections *and* two objects share each name)? Google docstrings on every new function, class and module,
useful enough to render in an IDE, explaining *why*? DRY — a helper used by two modules is an import, never a
copy.

**Regression discipline.** After each repair cycle, compare against the preceding version and label the result
`improved`, `unchanged`, or `regressed`. Fix or roll back any regression. Record the label and the evidence in
TASK_STATE. Phase 1 learned the hard way that a docstring written during a repair is new code and needs the same
verification as new logic — two claims written during a Phase 1 repair cycle were verified false.

**Structural escalation.** If the score is below the exit gate and improves by less than one point across two
consecutive complete cycles, stop patching and do a structural pass — re-read the task and the spec section it
implements, and consider whether the approach itself is wrong.

## 07 — Rubric (100 points, Phase-2 specific)

Phase 1's rubric measured capability preservation and measurement integrity, because Phase 1 was deletion and
regrouping and its silent failure was an unreachable tool. **Neither dimension is where Phase 2 fails.** Phase 2
adds commands that write to disk, swap Blender's database mid-loop, and pass through the rollback machinery
every existing tool already uses. These dimensions measure what can actually go wrong here.

| Dimension | Points | Gate ≥80% |
|---|---|---|
| **Data durability and rollback safety** — no user data destroyed or silently lost; rollback never reaches datablocks from a file it did not snapshot; no confirmation-free overwrite; no `remove()` on a freed datablock; a failed link leaves the scene recoverable; link and override survive save→reopen | 30 | 24 |
| **Concurrency and liveness** — every dequeued command gets exactly one response; no hang, no deadlock, no dropped healthy socket; the file-swap ordering contract holds across multiple client processes (**evidenced by plan Task 3's headless two-socket test — Task 3 Step 2's "on both sockets in a two-client test" and criterion 2's "N commands across two sockets spanning a swap" — and *not* by Task 10's gate scenario, which is single-client throughout**); `bpy` touched only on the main thread; timers registered only from the main thread | 25 | 20 |
| **Filesystem and trust boundary** — paths canonicalized with `os.path.realpath(os.path.abspath(bpy.path.abspath(...)))`, roots enforced with `commonpath`, extension validated and **all three `.blend` magic signatures accepted**, traversal and symlink escape blocked, **no absolute path / credential / traceback leaked in an error — including inside Blender's own exception text**, `use_scripts` never exposed and always forced `False`, active policy observable by the client | 20 | 16 |
| **Evidence quality** — every Blender-side claim backed by a transcript the reviewer reproduced; every new test proven to fail without the fix, with both counts; no number asserted that was not measured | 15 | 12 |
| **Code quality and contract fidelity** — envelope unchanged; addon/server separation intact; capability table and protocol version in sync; **all three MCP tool annotations correct per tool (`destructiveHint`, `readOnlyHint` *and* `openWorldHint` — `_documentation.py` emits every one of them unconditionally, so an omitted tool ships an affirmatively wrong `False`, not a missing hint)**; Google docstrings; SRP and DRY; no incidental churn | 10 | 8 |
| **Total** | 100 | **exit ≥ 90** |

**Exit gates — all mandatory, per task:**

- ≥ 90/100 overall
- ≥ 80% of available points in every dimension
- **Zero critical failures.** Each of the following is automatically critical, regardless of the rest of the
  score:
  - a `.blend`, render, cache or asset file overwritten without explicit confirmation — including the case
    where `check_existing=True` was relied on as the guard, which does **not** block a programmatic overwrite;
  - an absolute filesystem path reaching the client in any error message, including one lifted verbatim from a
    Blender `RuntimeError`;
  - `use_scripts` exposed as a parameter, or a `wm.open_mainfile` call that does not pass `use_scripts=False`
    explicitly (spec Decision #7: no arbitrary code execution, in any form);
  - a rollback path that can remove datablocks belonging to a file the transaction did not snapshot —
    **including the library case, not only the file-swap case**: `lib.reload()` gives every datablock linked
    from that library a fresh `session_uid` (measured, 3 of 3) while firing only `blend_import_pre` /
    `blend_import_post` and **never `load_post`**, so a `reload_library` / `relocate_library` left inside
    `mutation_transaction` deletes the reloaded library's entire contents on any later raise. `orphans_purge`
    and `libraries.remove` (`unlink_libraries`) fire **no handler at all**. Plan Task 4 item 2a is the fix;
    `blend_import_post` alone is **not**, because `link_canon_library`'s own `libraries.load()` fires it too
    and that command's rollback must keep working;
  - any tested path where a client receives no response and no error (a hang);
  - a path that reaches the filesystem outside the configured roots when roots are configured;
  - a server-side tool whose addon command is absent from `_build_command_handlers()`, or vice versa;
  - a test that still passes with its fix reverted;
  - a number reported in a commit message or TASK_STATE that was asserted rather than measured;
  - `bpy` imported from `src/blender_mcp/server/`, or server code imported from the addon.
- `pytest` passes and the per-task gate in §05 is green.
- For tasks **1**, 2, 3, 4, 5, 6, 7 and 10: a live-Blender transcript in TASK_STATE. (Task 1 was omitted from
  an earlier revision of this list, contradicting its own acceptance criterion 4 — the post-load `ping`
  transcript and the timer-registration state — and the fact that it now ports live addon behaviour.)

## 08 — Environment facts (established 2026-09-14; do not rediscover)

**Repository**

- Work on `main`, at `523f427`. The working tree is clean apart from **three untracked files**: `uv.lock` **and
  the two Phase 2 planning documents themselves** (`docs/superpowers/plans/2026-09-14-phase-2-primitives.md`
  and `…-handoff.md`). An earlier revision of this line named only `uv.lock`, which would have a reader treat a
  `git status` showing the plan and handoff as unexpected drift. **All three stay unstaged until the task that
  legitimately commits them**: `uv.lock` is incidental churn and must never be staged (§03); the two plan
  documents are committed with Task 1, alongside `PHASE2_TASK_STATE.md`. `origin` is the user's own fork,
  `jpease/blender-mcp`. Phase 1 is committed and pushed; do not touch `PHASE1_TASK_STATE.md` or any other
  Phase 1 artefact.
- **`poetry.lock` is tracked on `main`; `uv.lock` is not.** The container image installs from `poetry.lock`
  (`docker/blender/Dockerfile:51-57`: `COPY pyproject.toml poetry.lock` then
  `poetry install --only main --no-root`) — **not** from `pyproject.toml`, as the compose file's own comment
  and an earlier revision of plan Task 1 Step 2 both said. So the real reconciliation risk for Task 1 is
  whether `main`'s `poetry.lock` is still in sync with its `pyproject.toml`, which the now-present untracked
  `uv.lock` puts in doubt — dependencies may have been resolved through a second tool without `poetry.lock`
  moving. Check with `poetry check --lock` before assuming the image builds.
- Branches that matter: `docker-blender` carries the Xvfb rig and `output_roots.py` and is 8 ahead / **39
  behind** `main`, with **no `bundles.py`**. It also carries `ADDON_PROTOCOL_VERSION` /
  `EXPECTED_ADDON_PROTOCOL_VERSION` at **31** (`main` is at **30**), bumped for the `writable_output_roots`
  handshake field. The plan's Task 1 ports files forward rather than merging, **takes that bump with them, and
  owns the phase's only protocol bump**; Task 3 asserts the pair is at 31 rather than bumping again. See Task
  1's two rulings for why.

  > **Correction (2026-09-14, Task 1).** "bumped for the `writable_output_roots` handshake field" is false.
  > `git log -S` shows `docker-blender` bumped to 31 in `75a7abf` for the **inline image transport**;
  > `writable_output_roots` was added later on that branch and rode the existing 31. The ruling is unaffected -
  > 31 is simply the next number - but a future port of the inline image transport needs **32**. See
  > `docs/superpowers/plans/PHASE2_TASK_STATE.md` decision 5.


**Tooling** — `.venv/bin/python`, `.venv/bin/pytest`, `.venv/bin/ruff`, `.venv/bin/basedpyright`.

**The shell is fish.** Pass file paths as separate arguments, never through a variable. `--include=*.py` is
globbed by fish and will fail — use `rg --glob '*.py'` instead of `grep --include`. There is no `timeout` binary
on this host.

**Measured baselines at `523f427`** (re-run, not copied from Phase 1):

| Check | Value |
|---|---|
| `pytest` | **647 passed** in ~24 s |
| `ruff check .` | **9,834 errors** (pre-existing debt; the repo-wide gate cannot pass and never could) |
| `ruff format --check .` | **12 files** would be reformatted, 326 already formatted |
| `basedpyright` | **71 errors, 4 warnings** |
| `measure_catalog.py all` | **285 tools / 1,181,038 B / ~328K tokens** |
| `measure_catalog.py shot` | **53 tools / 203,094 B / ~56.4K tokens** |
| `measure_catalog.py` (default) | **21 tools / 63,395 B / ~17.6K tokens** |

The lint gate is **per-file plus a repo-wide before/after comparison**, exactly as Phase 1 ruled. Cleaning
9,834 pre-existing errors is not Phase 2 work.

**`shot` is exactly at its pinned ceiling** (`tests/server/test_bundles.py:509`, `SHOT_MODE_BYTE_CEILING =
203_094`, asserted `<=`). Zero headroom. Plan Task 9 handles this deliberately; do not stumble into it.

**Blender**

- Blender **5.2.2 LTS** at `/opt/homebrew/bin/blender` (build 2026-09-15, hash `d13f752e3b9c`). Host is macOS
  (Darwin 25.6.0 / 27.0.0), Apple Silicon. **Upgraded from 5.2.1 on 2026-09-15**; every API fact in this
  section was re-run against 5.2.2 before Task 2 began — see `PHASE2_TASK_STATE.md`, "5.2.2 re-verification".
- **The addon refuses to start in `--background`** (`server_core.py:152-158`), and **`bpy.app.timers` do not
  fire there** — re-verified 2026-09-14: a `persistent=True` timer registered in `--background` fires **0** times
  over 3 s while `is_registered` stays `True`. Anything needing a live server needs a real event loop: a GUI
  Blender locally, or Xvfb in the container.
- **But `wm.open_mainfile`, `wm.save_as_mainfile`, `wm.read_factory_settings` and `bpy.data.libraries.load` all
  work fine in `--background`.** Use `blender --background --factory-startup --python <script>` for every
  bpy-level check; reserve the live rig for the things that genuinely need the event loop.
- **There is no Xvfb on macOS.** The local rig is a GUI Blender; the container rig is Xvfb. `docker-compose.yml`
  forces `platform: linux/amd64`, so the container runs emulated on this host — correct but slow.
- **How to check whether a Blender API exists — read this before the facts below.** **Never conclude an API is
  absent from `dir()` on a `bpy.types.*` class.** In this build, `dir()` on a *type object* returns **no RNA
  members at all, for anything**; the one-line proof is `'copy' in dir(bpy.types.Collection)` → `False`, for a
  method that obviously exists. An empty `dir(bpy.types.X)` is therefore evidence of nothing whatsoever. The
  valid checks are:
  - `X.bl_rna.functions` / `X.bl_rna.properties` on the class — the authoritative RNA listing. **Authoritative
    for RNA, and RNA is not everything.** A small number of real, working, Python-level convenience properties
    are added by `bpy_types` and never appear in `bl_rna.properties`, so an empty result there is *weaker*
    evidence than it looks — the same "this check cannot distinguish absent from invisible" failure the rule
    above exists to prevent, one level down. The measured example, and it is one Phase 2 actually needs
    (2026-09-14, 5.2.1):

    ```
    'users_id' in [p.identifier for p in bpy.types.Library.bl_rna.properties]  -> False
    'users_id' in dir(bpy.data.libraries[0])                                    -> True
    bpy.data.libraries[0].users_id
      -> [bpy.data.collections['CanonHero'], bpy.data.meshes['HeroMesh'], bpy.data.objects['HeroBody']]
    ```

    `Library.users_id` returns exactly what plan Task 7's `list_libraries` and `unlink_libraries` need —
    "which datablocks came from this library" — and an implementer following the `bl_rna` rule alone would
    conclude it does not exist and hand-roll a full `bpy.data` walk. **Where `bl_rna` and a real instance
    disagree, believe the instance.**
  - `dir()` on an actual **instance** (`dir(bpy.data.collections.new("t"))`) — **the fallback that catches both
    traps**, the invisible-RNA one and the outside-RNA one;
  - `__doc__` on a bound method, which also gives you the real signature. (`inspect.signature` on an RNA method
    returns a useless `(*args, **kwargs)` — use `__doc__`.)
  - cross-check against the official docs with
    `curl -A Mozilla/5.0 https://docs.blender.org/api/5.2/bpy.types.ID.html` — a plain `WebFetch` of that
    domain returns **HTTP 403**.

  This is not a hypothetical. It is the exact mechanism that produced two false "does not exist" claims in the
  Phase 2 plan's own first revision, both listed as corrected below. **Do not repeat it.**

- **Blender 5.2.1 API facts already established** (do not re-derive; do verify if you depend on them). Four

  > **Re-verified against 5.2.2 on 2026-09-15**, after the host upgrade, before Task 2 began. **Every fact in
  > this list still holds**: the override methods and their signatures, `Library.reload`, `Library.users_id`
  > (still absent from `bl_rna.properties`), the handler-firing table (`lib.reload()` churned 3 of 3
  > `session_uid`s and fired `blend_import_*` only; `orphans_purge` fired nothing), all five error shapes
  > including the twice-embedded path and the CWD leak on an empty path, `check_existing`'s inertness (a 27 B
  > file silently overwritten with 95,912 B), the three magic signatures, the operator defaults, `abspath`'s
  > two gaps, and 0 timer fires in `--background`. **One entry is refined, not overturned** — see
  > `PHASE2_TASK_STATE.md`, "5.2.2 re-verification", finding 2: `override_create()` returns `None` on an
  > *indirectly* linked datablock as well as on a local one, so "non-overridable" is a wider class than the
  > "such as a local datablock" gloss below suggests. Transcripts are in TASK_STATE.
  entries here were **wrong in an earlier revision of this handoff and are corrected**; they are marked. One of
  the four (`override_create` returning `None`) survived **two** prior passes, so treat the corrected text as
  the only version and re-run anything you build on:
  - **[CORRECTED]** `Collection.override_hierarchy_create`, `ID.override_hierarchy_create` and
    `ID.override_create` **all exist.** Verified via `bpy.types.Collection.bl_rna.functions` →
    `['override_create', 'override_hierarchy_create']` (and the same on `ID`), and against
    `docs.blender.org/api/5.2/bpy.types.ID.html`. Signature:

    ```
    Collection.override_hierarchy_create(scene, view_layer, *, reference=None, do_fully_editable=False)
    Collection.override_create(*, remap_local_usages=False)
    ```

    The earlier "**do not exist**" claim came from `dir(bpy.types.Collection)` and is false.
  - **[CORRECTED TWICE — the one that survived two passes]** The claim that **"`object.override_create()`
    returns `None`"** is **false**. It appeared in the spec, was endorsed as "right and stands" by this
    handoff's first revision, and survived a correction pass that never re-ran it. Measured from scratch
    2026-09-14: on a **linked, overridable** object `override_create()` returns **the new override ID** (with
    `.override_library.reference` set to the linked original); it returns `None` only on a **non-overridable**
    ID, such as a local datablock. `docs.blender.org/api/5.2/bpy.types.ID.html` documents the return as "New
    overridden local copy of the ID", type `ID`. Plan Task 7 still rules **against** per-object
    `override_create` — but on the correct ground: it overrides **one ID at a time**, whereas the job is to
    override a whole linked collection's hierarchy in one call, which is what `override_hierarchy_create` is
    for. Do not restate the `None` claim anywhere. **The transferable lesson: a claim inherited across a
    correction pass is not a verified claim. Re-run it.**
  - Three override routes were measured
    end to end: `libraries.load(..., create_liboverrides=True)` leaves the objects inside **locked**
    (`is_editable=False`); `override_hierarchy_create(scene, view_layer)` gives editable objects but
    `is_system_override=True`; `override_hierarchy_create(scene, view_layer, do_fully_editable=True)` gives
    editable, user-owned overrides. **Plan Task 7 rules for the last of those**; none of the three needs
    operator context. `bpy.ops.object.make_override_library(collection=0)` also exists (operator form).
  - **[CORRECTED]** The linked/override **name collision is not collection-only.** An earlier revision recorded
    it as "two collections named `CanonHero` now coexist". Re-measured on both routes 2026-09-14: Route A
    leaves two same-named *collections* and exactly **one** object; **Route C — the route Task 7 rules for —
    leaves two same-named collections *and two same-named objects*** (the override, `is_editable=True`, and
    the linked original, `is_editable=False`), plus a local instance empty named after the collection. So
    `bpy.data.objects[name]` after an override is ambiguous by construction and silently returns whichever
    Blender ordered first. **Resolve and report every datablock by `session_uid`** plus
    `library` / `override_library` / `is_system_override` state, at every type, not just collections — plan
    Task 7's command signatures take uids for exactly this reason.
  - `bpy.app.handlers` includes `load_pre`, `load_post`, **`load_post_fail`**, `save_pre`, `save_post`,
    **`save_post_fail`**, `blend_import_pre`, `blend_import_post`, and `persistent`.
  - **Which handlers actually fire for each library operation — measured 2026-09-14, and this is the fact plan
    Task 4 item 2a is built on.** It is not derivable from the API listing above and getting it wrong is a
    data-destruction bug:

    | Operation | Handlers fired | `session_uid` effect |
    |---|---|---|
    | `lib.reload()` | `blend_import_pre`, `blend_import_post` — **never `load_post`** | every datablock linked from that library gets a **fresh** uid (3 of 3 measured) |
    | `bpy.data.libraries.load(link=True)` *and* `load(link=False)` | `blend_import_pre`, `blend_import_post` | new datablocks, fresh uids (correctly "this request's") |
    | a **failed** `lib.reload()` (invalid path) | **none** — it raises first | unchanged |
    | `bpy.data.orphans_purge(...)`, `bpy.data.libraries.remove(...)` | **none at all** | datablocks are freed, not created |

    Two consequences worth stating separately because they pull in opposite directions: a reload inside an open
    `mutation_transaction` is the `open_shot` catastrophe in miniature (fresh uids → `_new_datablocks()`
    classifies the whole reloaded library as new → `_remove_datablocks()` deletes it on any later raise), and
    **`blend_import_post` cannot be used as a blanket invalidation trigger**, because `link_canon_library`
    fires it too and *that* command's rollback must keep working. The fix is command-scoped, not
    handler-scoped — plan Task 4 item 2a's `_DATABLOCK_REPLACING_COMMANDS`.
  - **`Library.users_id` is a working Python-level property that is *not* in `bl_rna.properties`** — see the
    methodology note above. It returns the datablocks linked from that library, which is what `list_libraries`
    needs; do not hand-roll a `bpy.data` walk.
  - **[CORRECTED]** `bpy.types.Library` exposes `filepath`, `version`, `is_missing`,
    `needs_liboverride_resync`, `packed_file`, `users`, `session_uid` — **and `reload()`**, present in
    `bpy.types.Library.bl_rna.functions` and documented in the 5.2 API reference. `lib.filepath = new_path;
    lib.reload()` works headlessly. The earlier claim of "**no** `reload` method, so `wm.lib_reload` is the only
    route" is false and backwards: the *operators* are the awkward route. `wm.lib_reload` / `wm.lib_relocate`
    **require `directory` + `filename` and ignore `filepath`** (passing `library` alone, or `library` +
    `filepath`, raises `RuntimeError: Not a library`), return `{'CANCELLED'}` for a bogus library name, and
    `lib_relocate` **renames the `Library` datablock** to the new basename. Plan Task 7 rules for the data API.
  - **`wm.open_mainfile` / `wm.save_mainfile` / `wm.save_as_mainfile` raise `RuntimeError` on failure and never
    return `{'CANCELLED'}`** (all failure modes tested). A *failed* `open_mainfile` leaves the old database
    completely untouched — `bpy.data.filepath` and the object count are unchanged.
  - **Operator defaults that differ from the obvious assumption:** `save_as_mainfile.relative_remap` defaults to
    **`True`** while `save_mainfile.relative_remap` defaults to `False`; `check_existing` defaults to `True` on
    both but is **inert outside the interactive file browser** — `save_as_mainfile(check_existing=True)` on an
    existing file overwrites it silently. `open_mainfile.use_scripts` defaults to `False` and is additionally
    gated by `preferences.filepaths.use_scripts_auto_execute`, also `False` — it is an arbitrary-code-execution
    vector and plan Task 5 forbids exposing it.
  - **[CORRECTED]** A `.blend` has **three** legitimate magic signatures, and each must be tested as a
    **prefix** of the stated length: `b'BLENDER'` (7 bytes, uncompressed), zstd `b'\x28\xb5\x2f\xfd'`
    (4 bytes), and legacy gzip `b'\x1f\x8b'` (2 bytes). A check that accepts *only* `b'BLENDER'` rejects valid
    compressed files. An earlier revision gave the gzip constant as `b'\x1f\x8b\x08\x08'` and the uncompressed
    one as the full 12-byte `b'BLENDER17-01'`; **both false-reject valid files.** Gzip's 4th byte is the FLG
    field, which is `\x08` only when an FNAME is stored and `\x00` in a normal Blender-written stream; and
    `BLENDER17-01` is 5.x's header encoding, so it rejects files written by older Blenders (`BLENDER-v293`,
    `BLENDER_v279`), which still open. The 5.2.1 uncompressed header measured on this host is
    `BLENDER17-01v050` — use its `BLENDER` prefix, not the whole thing. The failure mode here is a **false
    rejection**, which no negative test catches; plan Task 5 requires three positive acceptance cases.
  - `bpy.path.abspath` does **not** normalise `..` (a following `os.path.realpath` does) and does **not**
    absolutize a bare relative path with no `//` prefix (`'relative.blend'` comes back unchanged). Wrap it:
    `os.path.realpath(os.path.abspath(bpy.path.abspath(raw)))`, mirroring `handlers/rendering.py:552`.
  - `bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=False) -> int` exists.
  - The full gate scenario (link + override + save + reopen) has been demonstrated working through the data API
    in `--background`; the transcript is in plan §0.3 finding 5. Note it used the **locked-objects** route
    (`create_liboverrides=True`), not the route Task 7 rules for. Reproduce it, do not cite it.

**Existing test harnesses you should reuse rather than rebuild**

- `tests/server/test_threading.py:30-107` AST-lifts `BlenderMCPServer` against a stub `bpy` whose
  `timers.register()` raises off-thread, and drives it over a real TCP socket with `_pump()` standing in for
  Blender's main loop. **The whole protocol half of the reentrancy/ordering work is testable here with no
  Blender at all.**
- `tests/conftest.py:load_addon_package()` loads the addon package against mocks (and purges stale submodule
  entries from `sys.modules`, which is why it exists).
- `tests/conftest.py:stub_blender_connection` routes server-side tool dispatch to a recording stub. **This,
  plus calling the tool function directly, is how this repo tests MCP tools — there is no other pattern.**
  `tests/server/tools/test_scene_validate.py:67` is representative:
  `asyncio.run(scene.validate_scene(ctx=None, scene_name="Scene", ...))`, then asserting on
  `connection.calls[0]`. **There is no FastMCP test client and no in-memory transport anywhere in `tests/`**
  (checked 2026-09-14); the only place the real `mcp` object is driven is `tests/server/test_bundles.py`, which
  shells out to a subprocess purely to *count* `asyncio.run(mcp.list_tools())`. Follow this convention for
  every new tool test rather than standing up a server.
- `tests/blender_*_smoke.py` are the precedent for **scripts** that need a real Blender and are not part of the
  default `pytest` run. **They are not a precedent for a skippable test, because they are not collected at
  all** — they `import bpy` at module scope (which fails outside Blender) and `pyproject.toml`'s
  `python_files` pattern does not match their name. Plan Task 10 needs a *real* pytest entry point, so it must
  use a `test_*.py` file with no module-scope `bpy` import that probes for its dependency and calls
  `pytest.skip(..., allow_module_level=True)`. Following the smoke-script shape there produces no entry point
  at all. **`skipped` and `not collected` look the same in a summary line and only one is the thing asked
  for.**

**GitNexus** — re-index with `node .gitnexus/run.cjs analyze --index-only` (not `npx`). The index goes stale
after every commit. `importlib` edges are invisible to it, and callbacks passed by reference return `UNKNOWN`.

## 09 — Scope

Execute **Tasks 1–10 of the plan.** The plan's "Deliberately not in this plan" section lists what is excluded
and why: the artist-facing plugin's agent loop (Phase 3), socket authentication *implementation* (Phase 4,
designed here in Task 8), `load_post` digest re-verification (Phase 3), the §7.1 success bar (Phase 1b), and
variant scoping / Resources / the typed gateway (Phase 1b). Do not start any of them.

If a task cannot be completed — blocked, needs manual verification you cannot perform, or its premise no longer
holds — **stop, say so explicitly, record it in TASK_STATE, and continue with the tasks that do not depend on
it.** Do not invent a workaround that weakens a constraint in §03. In particular:

- If Docker is unavailable, run the rig locally against a GUI Blender and **say so**; do not claim container
  parity you did not observe.
- If Task 2's experiment produces a result that fits neither branch of its decision rule, **escalate rather than
  inventing a third branch.** That rule exists precisely to stop an ambiguous result being resolved by
  preference.
- If a task would require adding a confirmation-free destructive path to make a test pass, the test is wrong or
  the design is wrong. Stop and raise it.

When Tasks 1–10 are done and the end-of-phase gate is green, **stop and report.** The push is the user's
decision, not yours.
