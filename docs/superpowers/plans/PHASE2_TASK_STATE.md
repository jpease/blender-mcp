# Phase 2 Task State
Updated: 2026-09-15 (session 2) — Task 1's commit recorded, host upgraded to Blender 5.2.2 and every
API fact re-verified against it, then **critic cycle 4 run and its repairs landed: Task 1 moves 81 -> 94/100
and passes its exit gate.** Earlier: transport follow-up (decision 8) applied 2026-09-15;
repair cycle 3 applied 2026-09-14 (cycles 1 and 2 recorded below)

Plan: docs/superpowers/plans/2026-09-14-phase-2-primitives.md
Handoff: docs/superpowers/plans/2026-09-14-phase-2-handoff.md
Branch: main. Base: **3460316** (`docs: add Phase 2 implementation plan and handoff`). Scope: Tasks 1-10.
No push until the end-of-phase gate.

> **Base-commit correction.** The handoff's §04 template and §08 both say `523f427`. That is stale: the plan and
> handoff were themselves committed at `3460316`, so Phase 2's work starts there. Every baseline below was
> **re-measured at 3460316**, not copied from §08. The other §08 consequence: it lists three untracked files
> (`uv.lock` plus the two plan documents); at `3460316` only `uv.lock` is untracked, so Task 1 does **not**
> commit the plan documents. `uv.lock` stays unstaged, permanently (§03 incidental churn).

## Pickup contract — read this first if you are resuming

Everything a fresh session needs is committed. Nothing load-bearing lives in a session scratchpad any more,
which was **not** true before this section existed: Task 1's Step 5 transcripts were recorded from a harness
that had already been lost once (see the closed item under "Known failures / blocked").

### State

| | |
|---|---|
| Branch | `main`, **unpushed**. `origin/main` is still at `523f427`. |
| Last commit | Task 1 closed at **94/100** after critic cycle 4. |
| Next task | **Task 2** (reentrancy strategy by experiment). Not started - no decision rule recorded, no spike written. |
| Working tree | Clean apart from `uv.lock`, which stays unstaged permanently (§03 incidental churn). |
| Blender | **5.2.2 LTS** at `/opt/homebrew/bin/blender`. Every API fact re-verified against it; see "5.2.2 re-verification". |

### Committed tooling, and what each is for

| Path | Use |
|---|---|
| `scripts/blender_rig.py` | The live rig. Addon socket only, no MCP server. |
| `scripts/rig_scenarios/scenario_timer_survives_file_swap.py` | Task 1 Step 5's scenario. Its module docstring carries the exact invocation. |
| `scripts/rig_scenarios/in_blender_open_mainfile.py` | The in-Blender half: probe timers plus the main-thread swap. |
| `scripts/rig_scenarios/make_fixture.py` | Regenerates `fixture.blend`. The binary is deliberately **not** committed - its bytes differ per Blender build, so a generator is the durable form. |
| `scripts/blender_probes/*.py` | Seven `--background` probes that reproduce the 5.2.2 API table, one concern each. Run them rather than trusting the table. |
| `scripts/check_revert_anchors.py` | Reports which `revert_matrix.py` rows no longer anchor, and which would revert to unparseable code. **Run it after any task edits a file the matrix reverts** - three anchors broke in cycle 4 alone. |
| `scripts/revert_matrix.py` | 117 rows, 0 survivors, 0 uncovered. `--list` for the coverage map, `--only <label>` for a subset. |

Verified end to end from a clean directory after being committed: fixture generated, rig run, `RIG PASSED`,
exit 0, on Blender 5.2.2. The committed copies are the ones that were run, not the scratchpad originals.

### The plan's line numbers are stale by ~229 lines — re-derive, do not trust

`server_core.py` is **1,880 lines**; plan §0.2 describes it at 1,651. Every citation in §0.2 has moved, and
Task 2 reads several of them. Measured at the last commit:

| Symbol | Plan §0.2 | Actual |
|---|---|---|
| `drain_command_queue` | `:267-321` | **`:352`** |
| `_MAX_COMMANDS_PER_TICK` etc. | `:329-332` | **`:264`** (`_DRAIN_TIME_BUDGET_SECONDS` at `:52`) |
| `_decode_and_queue_frame` | `:342-391` | **`:523`** |
| `_build_command_handlers` | `:480-808` | **`:663`** |
| `_READ_ONLY_COMMANDS` | `:813-870` | **`:996`** (still **54** entries) |
| `execute_command_internal` | `:872-913` | **`:1055`** |
| `_run_handler` | `:1064-1142` | **`:1247`**; the `mutation_transaction` wrap is at **`:1314`**, not `:1131` |
| `get_addon_info` | `:1144-1158` | **`:1327`** |

`_READ_ONLY_COMMANDS` holding 54 commands is re-confirmed by AST count, so the handoff's figure stands.

### Task 2's first action, and why the order matters

Step 1 is to copy §0.4's decision rule into this file **verbatim and dated, before running the spike**. The
rule exists so an ambiguous result cannot be resolved by preference after the fact, and recording it afterwards
forfeits that. The rule's two branches and its escalation clause are in plan §0.4; §09 says an outcome fitting
neither branch is an escalation, not a third branch.

Two constraints on the spike that are easy to miss: the temporary `__spike_open` command must be added to
**`_READ_ONLY_COMMANDS`** for its lifetime, or `_run_handler` wraps it in `mutation_transaction` and triggers
the data-destruction bug Task 4 has not fixed yet; and no spike code may land in the commit.

## Baseline (measured, never estimated)
| Check | Value at 3460316 | How measured |
|---|---|---|
| pytest | 647 passed | `.venv/bin/python -m pytest` |
| ruff check . | 9,834 errors | `.venv/bin/ruff check .` |
| ruff format --check . | 12 unformatted, 326 already formatted | `.venv/bin/ruff format --check .` |
| basedpyright | 71 errors, 4 warnings | `.venv/bin/basedpyright` |
| all | 285 tools / 1,181,038 B | `scripts/measure_catalog.py all` |
| shot | 53 tools / 203,094 B | `scripts/measure_catalog.py shot` |
| core (default) | 21 tools / 63,395 B | `scripts/measure_catalog.py` |

All seven match the handoff's §08 table at the new base commit.

### State after Task 1, repair cycle 3 (measured 2026-09-14, this tree)
| Check | Value | Delta vs 3460316 | Gate |
|---|---|---|---|
| pytest | **723 passed** | +76 new tests; **all 647 existing pass unmodified** | pass |
| ruff check . | 9,834 errors | **±0** | `<= 9,834` pass |
| ruff format --check . | 12 unformatted, 336 already formatted | +10 formatted files, unformatted count unchanged | pass |
| basedpyright | 71 errors, 4 warnings | ±0 | pass |
| all | 285 tools / 1,181,023 B | 0 tools, −15 B | count matches `bundles.py`'s docstring figure |
| shot | 53 tools / **203,079 B** | **−15 B** | `<= 203,094` pass, ceiling constant unchanged |
| core (default) | 21 tools / 63,380 B | −15 B | pass |

The +76 (was +51 after cycle 2) is 25 new tests in `tests/test_blender_rig.py`, which went from 21 nodes to 46.
Nothing else changed shape: the ported files still carry the same node counts. Re-measured to separate the two,
`pytest` with the four wholly-new test files ignored reports **649 passed** — 647 pre-existing plus the two nodes
Task 1 added to `tests/test_addon_manager.py`.

Cycle-3 figures for the catalog are **identical to cycle 2's**: no tool schema or docstring was touched this
cycle, and `scripts/measure_catalog.py all|shot` re-measured at 1,181,023 B / 203,079 B on this tree.

The −15 B is the whole of Task 1's catalog footprint: −7 B from defect ruling C's `writable_output_roots` key
added to `get_addon_status`'s payload and documented, paid for by trimming the same docstring (decision 6); a
further −8 B from repair R20's rewording of that same key's description, which removed a protocol-version
comparison the code does not perform.

**Cycle-1 figures, for the record** (superseded by the table above): 676 passed, `ruff format --check .`
**12 unformatted / 334 already formatted**, `all` 1,181,031 B, `shot` 203,087 B, `core` 63,388 B. The cycle-1
entry in this file recorded **333 already formatted**, which was wrong; re-measured twice on the cycle-1 tree it
was 334. The gated figure — 12 unformatted — was correct then and is correct now.

### State after the transport follow-up (measured 2026-09-15, this tree)

Decision 8's follow-up ported `ee25ffc`'s HTTP transport, fixed the two `entrypoint.sh` defects and tied
`tests/test_docker_rig.py`'s transport guard to the ported constant. Re-measured on the resulting tree:

| Check | Value | Delta vs cycle 3 | Gate |
|---|---|---|---|
| pytest | **739 passed** | +16 (14 ported transport nodes, 2 new entrypoint guards); **all 647 existing pass unmodified** | pass |
| ruff check . | 9,834 errors | ±0 | `<= 9,834` pass |
| ruff format --check . | 12 unformatted, 337 already formatted | +1 formatted file (the new test), unformatted count unchanged | pass |
| basedpyright | 71 errors, 4 warnings | ±0 | pass |
| all | 285 tools / 1,181,023 B | ±0 | count matches `bundles.py`'s docstring figure |
| shot | 53 tools / 203,079 B | ±0 | `<= 203,094` pass |

The ported `cli.py` and `tests/server/test_cli_transport.py` carry **zero** lint debt: per-file
`ruff check` / `ruff format --check` / `basedpyright` are clean on both, as Task 1 Step 3b required for the
first port, so the repo-wide ceilings do not move. The catalog is untouched because the transport is a
process-level choice with no tool schema or docstring of its own.

**One flake observed and re-measured, recorded rather than hidden.** The first full `pytest` of the session
reported `2 failed, 721 passed` — `tests/test_blender_rig.py::test_the_log_reader_survives_output_it_cannot_decode`
and `::test_teardown_reports_the_rig_s_own_log_reader_dying` — in a **79 s** run while `docker info` and other
work ran concurrently on the same host. Both tests wait on a real child process with a 30 s / 10 s bound. Re-run
alone they pass in 0.19 s; the next full run reported **723 passed in 22 s**, and every subsequent run has been
green. The failure is host contention against those bounds, not a defect introduced here, but the two tests are
**load-sensitive** and that is worth knowing before a CI machine reports it.

**On that denominator.** `ruff format --check .`'s "already formatted" count includes 9 tracked `.py` files
under `.audit_tmp/` — 4 of which are among the 12 unformatted — and moves whenever any file is added anywhere
in the tree. Only the unformatted count is gated, and only that count should be compared across tasks.

## Decisions taken (each with its cost if wrong)
| # | Decision | Evidence | Cost if wrong |
|---|---|---|---|
| 1 | **Port the full `output_roots` wiring, not just the module** — `server_core.py`'s import, `_writable_output_roots()` and the `get_addon_info` field; `addon_manager.py`'s `AddonHandshake.writable_output_roots` and its parse; `tests/test_addon_manager.py`'s two handshake tests. Plan Task 1 ruling (a). | Reproduced: `tests/test_output_roots.py:112,139` (branch numbering) construct a `BlenderMCPServer` and assert on `get_addon_info()["writable_output_roots"]`. Ported without the wiring they fail — measured, 2 failed, before the wiring landed; green after. Option (b) would have required editing a ported assertion, which §03 forbids outright. | The phase carries a handshake field one protocol generation earlier than strictly needed. It is `[]` on any addon that does not report it, and `test_handshake_defaults_writable_output_roots_for_an_older_addon` covers that fallback. The cheap direction to be wrong in. |
| 2 | **30 → 31 is the phase's only protocol bump, and Task 3 inherits 31 rather than re-bumping.** `ADDON_PROTOCOL_VERSION` (`bundled/addon/__init__.py:28`) and `EXPECTED_ADDON_PROTOCOL_VERSION` (`addon_manager.py:29`) moved together. | Both were at 30 on `main`, confirmed before the change; both read 31 after, and `tests/test_addon_manager.py:16-24` (which parses the addon source and asserts the pair agrees) is green. Live confirmation: `get_addon_info` over the socket against Blender 5.2.1 returned `"protocol_version": 31` (transcript below). Adding a handshake field requires a bump (Global Constraints); 31 is the next number; owning the single bump here resolves the collision with Task 3 Step 5, which claimed the same 30 → 31 independently. | Two bumps in one phase, or a silent collision where Task 3 re-bumps to 31 and the two tasks disagree about what 31 means. The named beneficiary is Task 3, which must now **assert** the pair is at 31, not bump. |
| 3 | **The rig's local mode speaks the addon socket only.** `scripts/blender_rig.py` stands up a GUI Blender with the addon's socket server and **no MCP server**; its module docstring says so in as many words, as Task 1 Step 4 requires. Task 10's gate scenario is written against this narrower reach. | Reproduced by construction and by running it: the rig's scenario protocol is `rig.send(<addon command>)` over newline-delimited JSON straight to `_decode_and_queue_frame`. `get_addon_status` is unreachable from a scenario because it lives in `server/tools/core.py` and calls `force_addon_handshake()` inside the MCP process, which the local rig never starts. The container is the only mode running both layers, and compose publishes only `127.0.0.1:8000`. | A scenario written for Tasks 2/3/6/7/10 that assumes "MCP tool → socket → addon" cannot run, and a rig that answered plausibly through the wrong path would silently invalidate the live transcripts that are a hard exit gate for eight of the ten tasks. |
| 4 (defect A) | **Omit `test_addon_protocol_version_covers_the_inline_image_transport` from the port.** The other 12 tests in `tests/test_output_roots.py` ported with **no assertion touched**. | `docker-blender`'s `tests/test_output_roots.py:150-154` imports `blender_mcp.server.tools._image_transport`, a module that has **never existed on `main`** (`git grep -ln 'INLINE_IMAGE_PROTOCOL_VERSION' main` → no output; the file is absent). Porting it verbatim gives a collection-time `ImportError`. This is not §03's forbidden "weakening an assertion": it is declining to port a test for a feature Phase 2 is not porting. | `main` loses a guard that `EXPECTED_ADDON_PROTOCOL_VERSION` never regresses below the inline image transport's floor — a guard for a feature `main` does not have. Zero cost today; becomes non-zero only if the inline image transport is later ported, at which point that test ports with it. |
| 5 (defect B) | **Record the corrected *reason* for the 30 → 31 bump.** Decision 2 stands; only its third justification changes. 31 is simply **the next number**, not "the number `docker-blender` already uses for exactly that field". | `git log --oneline -S'ADDON_PROTOCOL_VERSION = 31' docker-blender -- src/blender_mcp/bundled/addon/__init__.py` → `75a7abf feat: carry image bytes over the socket instead of a shared filesystem`. The bump came from the **inline image transport** (`_image_transport.py` sets `INLINE_IMAGE_PROTOCOL_VERSION = 31`); `writable_output_roots` was added later on the same branch and rode the existing 31. Plan §0.3 finding 3 and Task 1's second ruling both state the false reason; it is not restated anywhere in this work, including the commit message. **Repair R18: the false reason stands in three places and all three now carry a dated correction marker pointing here** — `docs/superpowers/plans/2026-09-14-phase-2-primitives.md` (the "the number `docker-blender` already uses for exactly that field" parenthetical), `docs/superpowers/plans/2026-09-14-phase-2-handoff.md` §08 ("bumped for the `writable_output_roots` handshake field"), and this file. The surrounding analysis in both plan documents is left untouched. | **Second-order consequence to carry forward:** after this task, protocol 31 means `writable_output_roots` on `main` and "inline images" on `docker-blender`. That divergence is acceptable only because decision 1's ruling makes `docker-blender` redundant once the rig lives on `main`. **A future port of the inline image transport needs 32, not 31.** Getting this wrong means two incompatible addons both claiming protocol 31. |
| 6 (defect C) | **Surface `writable_output_roots` through `get_addon_status`, document it, and pay for the bytes by trimming the same docstring — do not raise the `shot` ceiling in Task 1.** | Acceptance criterion 5 requires the wiring be "live, not just ported", and the agent is the MCP client, which sees only the tool payload — stopping at `AddonHandshake` leaves §0.3 finding 3's claim false. `get_addon_status`'s payload was byte-identical on both branches and surfaced nothing, so this is a **sixth wiring site** absent from finding 3's five-site table: `src/blender_mcp/server/tools/core.py`. Measured before/after: `shot` **203,094 B → 203,087 B** (−7). `SHOT_MODE_BYTE_CEILING = 203_094` at `tests/server/test_bundles.py:509` is **unchanged** (`git diff tests/server/test_bundles.py` is empty) and `test_shot_mode_payload_stays_under_its_ceiling` is green. The trim removed the "If outdated, tells the user how to update via `blender-mcp install-addon`" paragraph. The `Returns:` block still names `"update_command"` and `"after_install"`, so the information survives **in the payload**; what a client loses is the literal command string `blender-mcp install-addon` in the tool *description*, which it now sees only after calling the tool. That is a real, small reduction in what the catalog says up front, not a lossless edit. Task 9 ruling 2 says the ceiling is raised "deliberately, *once*", and Task 9 ruling 3 already sets the policy for this case — "trim schemas — do not raise the budget". | If the trim lost information a client needed, a user misreads `get_addon_status`'s contract; mitigated by `test_get_addon_status_documents_every_key_it_returns`, which fails if any payload key goes unmentioned in the docstring. The alternative cost — raising a pinned ceiling twice in one phase — is the invariant the whole byte-budget regime rests on. |
| 7 | **Task 1 kept at its plan-recommended Opus tier** (no downgrade to record). | Plan Task 1's re-tiering rationale, §05's honour-the-tier rule. | n/a — no deviation. |
| 8 (defect E — **fifth plan defect**) | **The container rig depends on a tenth file the plan never listed: `ee25ffc`'s HTTP transport.** Task 1's Files list ports nine files plus the `output_roots` wiring; `docker/blender/entrypoint.sh` also requires `BLENDERMCP_TRANSPORT=http`, implemented on `docker-blender` in `src/blender_mcp/server/cli.py` (with `tests/server/test_cli_transport.py`). Plan §0.3 finding 2 never identified it. **Ruled: record it, do not port it in Task 1** — the user assigned the port and the two entrypoint fixes to a separate follow-up task, so two agents are not editing overlapping files. **Closed 2026-09-15**: that follow-up ported `cli.py` + `tests/server/test_cli_transport.py`, fixed both entrypoint defects, and criteria 2 and 3 now pass against a real container (see "The container half"). The port needed nothing else from the branch — `src/blender_mcp/server/app.py` is byte-identical on both branches and the installed `mcp` package already provides `settings.streamable_http_path` and `settings.transport_security`, so the stop-and-report condition never fired. | Measured 2026-09-15 05:27–05:38: `docker compose ... up --build --wait` built the image and the container came up **unhealthy**; `git grep -ln 'BLENDERMCP_TRANSPORT' main -- '*.py'` returns nothing; on `docker-blender` it is `ee25ffc`. With no HTTP transport the server runs stdio, takes EOF on a non-TTY stdin and exits, tripping `wait -n`. Full transcript summary under "The container half". | **This was discoverable only by running the container.** Cycle 2 took criteria 2 and 3 through their "if Docker is unavailable" escape hatch, which would have hidden a missing dependency permanently — the phase would have carried a container rig that has never once come up healthy. The cost of the ruling itself is that criteria 2 and 3 stay open across a task boundary; the cost of the alternative (porting it here) is an unplanned tenth file landing in the task whose whole point is a bounded port. |

| 9 | **Move every Blender version pin 5.2.1 -> 5.2.2 and re-verify the API layer against it, rather than assuming the facts carry.** The host was upgraded between sessions. Historical measurement text keeps its 5.2.1 dates; only current-state claims and the container's build arg move. | Every load-bearing fact re-run on 5.2.2 before Task 2 began — override methods and signatures, `Library.reload`, `Library.users_id`, the full handler-firing table (3 of 3 `session_uid`s churned, `load_post` never fired, `orphans_purge` silent), all five error shapes, `check_existing`'s inertness (27 B -> 95,912 B silently), the three magic signatures, operator defaults, `abspath`'s two gaps, 0 timer fires in `--background`, and the live Step 5 rig green end to end. Transcripts under "5.2.2 re-verification". The container tarball was confirmed at HTTP 200 before the `ARG` moved. | **The whole plan's API layer is load-bearing for Tasks 4, 6 and 7, and a point release can change operator defaults and error strings** — precisely the two things the rubric makes automatically critical (a silent overwrite, a leaked path). Assuming the facts carried would have meant building the sanitizer and the overwrite guard against a version nobody had run. As measured the cost was zero: everything held. The one thing that *did* surface — `use_file_compression=True` beating `compress=False` at factory settings — is a live trap for Task 6 that no 5.2.1 measurement had recorded. |

| 10 | **The HTTP transport is loopback-only, and the docs are corrected to match rather than making remote hosting work.** A non-loopback bind is refused unless `BLENDERMCP_HTTP_ALLOW_REMOTE=1` is also set, which warns when granted; an empty `BLENDERMCP_HTTP_HOST` is refused outright even with the opt-in, because it is never a deliberate choice. The container is the one legitimate opt-in and declares it explicitly beside a comment explaining that compose's `127.0.0.1:8000:8000` publish is what contains it. | Measured: `BLENDERMCP_HTTP_HOST=""` produced `bind('') -> ('0.0.0.0', ...)`, and under `python -O` a stripped assert plus `host=None` gave `uvicorn` a bind on `0.0.0.0` **and** `::`. Verified against the installed `mcp` 1.30.0 that FastMCP's DNS-rebinding protection survives the `settings.host` mutation (the comment's mechanical claim was true) but is browser-only: a forged `Host: 127.0.0.1` is served from anywhere, while an honest remote client gets `421`. So the advertised "Blender on another machine" use case did not work as written. Container-verified: the bind is now granted explicitly and logs a warning naming the cost. | **The cheap direction to be wrong in, deliberately.** If remote hosting is in fact wanted, the cost is one env var and a docstring — Task 8 owns that decision and now inherits a written threat statement rather than a claim. The alternative was shipping a server whose own docstring said widening the bind "is always a deliberate act" while an empty environment variable did it silently, on an unauthenticated 53-tool Blender driver. |

## Tasks
| # | Task | Tier | Status | Commit | Notes |
|---|---|---|---|---|---|
| 1 | Land the live-Blender acceptance rig on `main` | Opus | **Done — 94/100 after cycle-4 repairs** | `2852803` + repairs |  9 files ported, full `output_roots` wiring + 30 → 31 bump, ported lint debt cleaned to zero, `scripts/blender_rig.py` written, Step 5 re-verified live and **re-run end to end after each repair cycle**, README documented, `scripts/revert_matrix.py` landed so the revert evidence is reproducible. Repairs R1-R22 (cycle 2) and A1-A6/B1-B5/C1-C4/D1/F1-F10 (cycle 3); see both repair tables. **Acceptance criteria 2 and 3 now PASS** against a real container, after the decision-8 follow-up ported the HTTP transport (a tenth file) and fixed the two `entrypoint.sh` defects the first container run exposed. |
| 2 | Decide the reentrancy strategy by experiment | Opus | Not started | — | Task 1's commit landed at `2852803`; no longer blocked. |
| 3 | Drain-loop file-swap barrier, session epoch, failure handlers | Opus | Not started | — | **Inherits protocol 31; asserts, does not bump** (decision 2). |
| 4 | Make rollback survive a file swap, track `libraries` | Opus | Not started | — | |
| 5 | The filesystem trust boundary | Opus | Not started | — | Promotes `output_roots.py`, landed here. |
| 6 | Addon file-lifecycle handlers | Opus | Not started | — | |
| 7 | Addon linking handlers | Opus | Not started | — | |
| 8 | Socket authentication — design deliverable | Opus | Not started | — | |
| 9 | Server-side MCP tools, bundle placement, payload ceiling | Sonnet | Not started | — | Inherits `shot` at **203,079 B**, **15 B** below its unchanged ceiling (measured 2026-09-15; the earlier 203,087 was cycle 2's figure and missed repair R20's further 8 B). |
| 10 | The phase gate scenario | Sonnet | Not started | — | Written against the addon socket only (decision 3). |

## Live-Blender evidence

### Task 1 Step 5 — persistent-timer claim re-verified with the server actually running

Revision: `3460316` + Task 1's uncommitted changes. Blender **5.2.1 LTS** at `/opt/homebrew/bin/blender`,
macOS (Darwin 25.6.0), GUI (not `--background`). Command:

```
.venv/bin/python scripts/blender_rig.py \
    --work-dir <scratch>/step5/work \
    --scenario <scratch>/step5/scenario_step5.py \
    --blender-script <scratch>/step5/in_blender_open.py \
    --blend fixture=<scratch>/step5/fixture.blend
```

The `.blend` fixture was created by `blender --background --factory-startup` with a single object,
`RigFixtureCube`. `wm.open_mainfile` is invoked **from a main-thread timer inside Blender** — a scripted
operator call, **not** a queued MCP command (that is Task 2's question, deliberately not asked here).

Transcript (the `get_addon_info` capability list, ~250 entries, elided at the marked point):

```
RIG: Blender up on 127.0.0.1:9876, work dir <scratch>/step5/work
--> {"id": "rig-1", "type": "ping", "params": {}}
<-- {"status": "success", "result": {"pong": true}, "id": "rig-1"}
--> {"id": "rig-2", "type": "get_addon_info", "params": {}}
<-- {"status": "success", "result": {"name": "Blender MCP", "addon_version": [2, 0, 0],
     "protocol_version": 31, "capabilities": [... 250 entries elided ...],
     "blender_version": "5.2.1 LTS",
     "writable_output_roots": ["/var/folders/87/.../T/blender_75qC26",
                               "/var/folders/87/.../T", "/Users/jpease"]}, "id": "rig-2"}
RIG: protocol_version = 31
RIG: writable_output_roots = ['/var/folders/87/.../T/blender_75qC26', '/var/folders/87/.../T', '/Users/jpease']
RIG: asked Blender to open <scratch>/step5/fixture.blend
RIG: in-Blender outcome = {
  "before_filepath": "",
  "before_objects": ["Camera", "Cube", "Light"],
  "before_persistent_timer_registered": true,
  "before_volatile_timer_registered": true,
  "before_persistent_fires": 2,
  "before_volatile_fires": 2,
  "before_drain_is_registered_fresh_bound_method": false,
  "open_mainfile_result": ["FINISHED"],
  "after_filepath": "<scratch>/step5/fixture.blend",
  "after_objects": ["RigFixtureCube"],
  "after_persistent_timer_registered": true,
  "after_volatile_timer_registered": false,
  "after_persistent_fires": 2,
  "after_volatile_fires": 2,
  "after_drain_is_registered_fresh_bound_method": false,
  "callback_frame_survived_the_swap": true
}
RIG: persistent timer fires 2 -> 3 after the swap
--> {"id": "rig-3", "type": "ping", "params": {}}
<-- {"status": "success", "result": {"pong": true}, "id": "rig-3"}
RIG: post-load ping answered -> {"status": "success", "result": {"pong": true}, "id": "rig-3"}
--> {"id": "rig-4", "type": "get_addon_info", "params": {}}
RIG: post-load get_addon_info protocol_version = 31
RIG: post-load writable_output_roots = ['<scratch>/step5', '/var/folders/87/.../T/blender_75qC26',
                                        '/var/folders/87/.../T', '/Users/jpease']
RIG PASSED
```

Exit code 0.

**What this demonstrates, item by item (acceptance criterion 4):**

1. **A `ping` round-trips before the load** — `rig-1`. The addon's socket server is genuinely serving, which it
   cannot do in `--background`.
2. **The file swap really happened** — `open_mainfile_result: ["FINISHED"]`, `before_objects`
   `[Camera, Cube, Light]` → `after_objects` `[RigFixtureCube]`, and `bpy.data.filepath` `""` → the fixture path.
   This is not a no-op that a post-load ping would trivially survive.
3. **A `persistent=True` timer survives the swap** — `after_persistent_timer_registered: true`, with the
   **control** that makes the measurement mean something: a timer registered `persistent=False` alongside it
   reads `after_volatile_timer_registered: false`. The probe can tell the two apart.
4. **The persistent timer still *fires* after the swap** — `persistent timer fires 2 -> 3`. This does not
   contradict `after_persistent_fires: 2` in the JSON directly above it: that 2 is the count captured *inside*
   the swap callback, and the 3 is a later read of `heartbeat.json`, which the scenario polls until the count
   moves. Both are true, and the pair is the point. The plan's own caution ("a registered timer that never
   fires again would pass the first check and fail the phase") is answered by measurement, not by the
   registration flag.
5. **A post-load `ping` is answered** — `rig-3`. This is the direct proof that the addon's own
   `drain_command_queue` timer is alive *and firing* after the swap, because a command can only be answered by
   the drain loop running on Blender's main thread.
6. **The protocol bump and the ported handshake field are live** — `protocol_version: 31` and a populated
   `writable_output_roots` came back over a real socket, before and after the load. After the load the list
   leads with the newly-opened file's own directory, so the handshake is recomputed per call rather than cached.

**Finding — `bpy.app.timers.is_registered` compares by IDENTITY, so it cannot see a bound method.** Note
`before_drain_is_registered_fresh_bound_method: false` *before* the load, while the server was demonstrably
answering pings. Root-caused against Blender 5.2.1 rather than worked around:

```
PROBE identity of two accesses: False          # s.drain is s.drain
PROBE equality of two accesses: True           # s.drain == s.drain
PROBE is_registered(fresh bound method) : False
PROBE is_registered(saved bound method) : False
PROBE is_registered(plain function)     : True
PROBE is_registered(held, the very object registered): True
PROBE dir(bpy.app.timers): ['is_registered', 'register', 'unregister']
```

`obj.method` builds a **new bound-method object on every attribute access**, and `is_registered` matches on
identity, not `==`. So `bpy.app.timers.is_registered(self.drain_command_queue)` is **always `False`** in this
addon, registered or not — it is not evidence of an unregistered timer. There is also no API to enumerate
registered timers (`dir` above), so the registration of the addon's own timer is proved behaviourally (item 5)
and the `persistent=True` semantics are proved with plain-function probes whose identity is stable (items 3-4).
See "Known failures / blocked" for the latent consequence inside `server_core.py`.

### Task 1 Step 5 — re-run end to end after repair cycle 2

Repairs R1, R2, R3, R5 and R6 all change how the rig launches Blender, so the whole scenario was re-run from a
**fresh** work directory against the repaired rig. Same Blender **5.2.1 LTS**, same fixture, same
`scenario_step5.py` / `in_blender_open.py`. Exit code **0**.

```
RIG: server running = True
RIG: readiness receipt written to <scratch>/step5-repaired/work/rig_ready.json
RIG-BLENDER: watcher + probe timers registered, waiting for <scratch>/step5-repaired/work/open_now.json
RIG: Blender (pid 38550) up on 127.0.0.1:49616, work dir <scratch>/step5-repaired/work
--> {"id": "rig-1", "type": "ping", "params": {}}
<-- {"status": "success", "result": {"pong": true}, "id": "rig-1"}
--> {"id": "rig-2", "type": "get_addon_info", "params": {}}
<-- {"status": "success", "result": {"name": "Blender MCP", "addon_version": [2, 0, 0],
     "protocol_version": 31, "capabilities": [... 250 entries elided ...],
     "blender_version": "5.2.1 LTS",
     "writable_output_roots": ["<scratch>/step5-repaired/work",
                               "/var/folders/87/.../T/blender_4wFVTR",
                               "/var/folders/87/.../T", "/Users/jpease"]}, "id": "rig-2"}
RIG: protocol_version = 31
RIG: asked Blender to open <scratch>/step5-repaired/work/blends/fixture.blend
RIG: in-Blender outcome = {
  "before_filepath": "",
  "before_objects": ["Camera", "Cube", "Light"],
  "before_persistent_timer_registered": true,
  "before_volatile_timer_registered": true,
  "before_persistent_fires": 3,
  "before_volatile_fires": 3,
  "before_drain_is_registered_fresh_bound_method": false,
  "open_mainfile_result": ["FINISHED"],
  "after_filepath": "<scratch>/step5-repaired/work/blends/fixture.blend",
  "after_objects": ["RigFixtureCube"],
  "after_persistent_timer_registered": true,
  "after_volatile_timer_registered": false,
  "after_persistent_fires": 3,
  "after_volatile_fires": 3,
  "after_drain_is_registered_fresh_bound_method": false,
  "callback_frame_survived_the_swap": true
}
RIG: persistent timer fires 3 -> 4 after the swap
--> {"id": "rig-3", "type": "ping", "params": {}}
<-- {"status": "success", "result": {"pong": true}, "id": "rig-3"}
RIG: post-load ping answered -> {"status": "success", "result": {"pong": true}, "id": "rig-3"}
--> {"id": "rig-4", "type": "get_addon_info", "params": {}}
RIG: post-load get_addon_info protocol_version = 31
RIG: post-load writable_output_roots = ['<scratch>/step5-repaired/work',
                                        '<scratch>/step5-repaired/work/blends',
                                        '/var/folders/87/.../T/blender_4wFVTR',
                                        '/var/folders/87/.../T', '/Users/jpease']
RIG PASSED
```

Every item 1-6 above still holds, and four things are visible here that were not before:

- **The bootstrap's own output reaches the transcript** (`RIG: server running = True`). Cycle 1's rig piped
  Blender's stdout and never read it, which is why that line was missing from the cycle-1 transcript; it is
  now drained on a reader thread into `<work-dir>/blender.log` and the `RIG:` lines are echoed. (R2)
- **The port is ephemeral** (49616), not the addon's 9876, and a readiness receipt carrying this run's nonce
  was written before any command was sent. (R1)
- **The advertised roots now lead with the work dir**, because the launched Blender is given
  `BLENDERMCP_OUTPUT_ROOTS=<work-dir>`. `/Users/jpease` still appears — it is the *addon's* own last-resort
  default candidate, not something the rig sets, and narrowing it is Task 5's decision (see "Flagged for
  Task 5"). (R5)
- **The fixture opened is the rig's copy**, `<work-dir>/blends/fixture.blend`, not the caller's file. The
  original's MD5 was `76a4fbe5ac97069b13ae08a4abd40cdb` before the run and after it, and the copy's MD5 is the
  same. (R6)

### Task 1 Step 5 — re-run end to end after repair cycle 3

Repairs A1-A6, B1-B5 and C1-C4 all change how the rig launches Blender, claims its work dir and tears down, so
the whole scenario was re-run from a **fresh, empty** work directory against the repaired rig. Same Blender
**5.2.1 LTS** at `/opt/homebrew/bin/blender`, macOS (Darwin 25.6.0), GUI (not `--background`); same
`fixture.blend`, `scenario_step5.py` and `in_blender_open.py`. Exit code **0**.

```
RIG: server running = True
RIG: readiness receipt written to <scratch>/cycle3/work/rig_ready.json
RIG-BLENDER: watcher + probe timers registered, waiting for <scratch>/cycle3/work/open_now.json
RIG: Blender (pid 87118) up on 127.0.0.1:58497, work dir <scratch>/cycle3/work
--> {"id": "rig-1", "type": "ping", "params": {}}
<-- {"status": "success", "result": {"pong": true}, "id": "rig-1"}
--> {"id": "rig-2", "type": "get_addon_info", "params": {}}
<-- {"status": "success", "result": {"name": "Blender MCP", "addon_version": [2, 0, 0],
     "protocol_version": 31, "capabilities": [... ~250 entries elided ...],
     "blender_version": "5.2.1 LTS",
     "writable_output_roots": ["<scratch>/cycle3/work",
                               "<scratch>/cycle3/work/tmp/blender_7MjNfO",
                               "<scratch>/cycle3/work/tmp",
                               "/Users/jpease"]}, "id": "rig-2"}
RIG: protocol_version = 31
RIG: asked Blender to open <scratch>/cycle3/work/blends/fixture.blend
RIG: in-Blender outcome = {
  "before_filepath": "",
  "before_objects": ["Camera", "Cube", "Light"],
  "before_persistent_timer_registered": true,
  "before_volatile_timer_registered": true,
  "before_persistent_fires": 3,
  "before_volatile_fires": 3,
  "before_drain_is_registered_fresh_bound_method": false,
  "open_mainfile_result": ["FINISHED"],
  "after_filepath": "<scratch>/cycle3/work/blends/fixture.blend",
  "after_objects": ["RigFixtureCube"],
  "after_persistent_timer_registered": true,
  "after_volatile_timer_registered": false,
  "after_persistent_fires": 3,
  "after_volatile_fires": 3,
  "after_drain_is_registered_fresh_bound_method": false,
  "callback_frame_survived_the_swap": true
}
RIG: persistent timer fires 3 -> 4 after the swap
--> {"id": "rig-3", "type": "ping", "params": {}}
<-- {"status": "success", "result": {"pong": true}, "id": "rig-3"}
RIG: post-load ping answered -> {"status": "success", "result": {"pong": true}, "id": "rig-3"}
--> {"id": "rig-4", "type": "get_addon_info", "params": {}}
RIG: post-load get_addon_info protocol_version = 31
RIG: post-load writable_output_roots = ['<scratch>/cycle3/work',
                                        '<scratch>/cycle3/work/blends',
                                        '<scratch>/cycle3/work/tmp/blender_7MjNfO',
                                        '<scratch>/cycle3/work/tmp',
                                        '/Users/jpease']
RIG PASSED
```

Only the absolute scratch prefix and the capability list are elided; every other character is verbatim.
Items 1-6 of acceptance criterion 4 all still hold. **What is new in this transcript, and what it falsifies:**

- **Blender's session temp dir is now inside the work dir** — `<work-dir>/tmp/blender_7MjNfO` and
  `<work-dir>/tmp` in place of cycle 2's `/var/folders/87/.../T/blender_4wFVTR` and `/var/folders/87/.../T`.
  That is repair B2 (`TMPDIR=<work-dir>/tmp`) measured, not asserted: autosaves, `quit.blend` and render
  previews no longer land outside `--work-dir`.
- **`/Users/jpease` is still advertised, and that is now what the code, the docstring, the README and the test
  name all say.** It is the *addon's* own last-resort default candidate, appended after the configured roots;
  `BLENDERMCP_OUTPUT_ROOTS` makes the work dir **lead** the list, not be the whole of it. Repair B1 corrected
  the claim in four places rather than the one a critic happened to cite — `blender_rig.py`'s module docstring,
  `README.md`, the test formerly named
  `test_the_launched_blender_advertises_only_the_work_dir_as_writable` (now
  `test_the_launched_blender_is_pointed_at_the_work_dir_first`), and this file.
- **The caller's fixture is untouched.** MD5 `76a4fbe5ac97069b13ae08a4abd40cdb` before and after the run.
- **No `__pycache__` was written beside the caller's scenario** (repair F4 / `sys.dont_write_bytecode`);
  checked by deleting it first and confirming it was not recreated.

### The three cycle-3 defects that are reproducible, demonstrated before and after

**A1 — one non-UTF-8 byte killed the log reader and silently restored the deadlock it was written to prevent.**
A child writes `b"\xff ..."` and then 20,000 further lines (~220 KiB, well past the ~64 KiB pipe buffer), read
through a `Popen` decoding strictly, exactly as cycle 2's did:

```
BEFORE (cycle-2 reader: no guard around the tee)
Exception in thread Thread-1 (_tee):
  ...
  File "scripts/blender_rig.py", line 279, in _tee
    for line in stream:
UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte
  child exit code .......... STILL BLOCKED in write(): the pipe filled (this is the deadlock)
  reader thread alive ...... False
  reader failure recorded .. None

AFTER (repaired reader: falls back to draining the raw pipe)
  child exit code .......... 0
  reader thread alive ...... False
  reader failure recorded .. UnicodeDecodeError
```

The child that blocked forever is Blender's position in the real thing — blocked in `write()` on its **main**
thread, the thread the drain loop answers commands on, with no diagnostic. The repair is three parts:
`errors="replace"` on the `Popen` so ordinary output never reaches this path at all; a `BaseException` guard
around the tee that falls back to consuming the raw byte stream so the pipe cannot fill; and the recorded
failure, which `_shut_down` now reports as *the rig's own log reader died* rather than leaving the run to be
misdiagnosed as "Blender hung".

**A4 — a scenario calling `sys.exit(0)` made the rig exit 0 with no verdict at all.** Run live against Blender
with a scenario whose `run(rig)` pings once and then calls `sys.exit(0)`:

```
BEFORE (main catching only Exception, reverted in place):
  stdout tail: <-- {"status": "success", "result": {"pong": true}, "id": "rig-1"}
               SCENARIO: {'status': 'success', 'result': {'pong': True}, 'id': 'rig-1'}
  stderr:      (empty)
  RIG EXIT CODE = 0          <- neither RIG PASSED nor RIG FAILED was printed

AFTER (main catching BaseException):
  stderr:  RIG FAILED: SystemExit: 0 -- which is a request to stop this process, not a scenario result.
           The rig reports it as a failure because its exit code is its verdict and must never be 0
           without RIG PASSED.
  RIG EXIT CODE = 1
```

**B3 — a pre-placed file under `<work-dir>/blends/` was silently overwritten.** `26` bytes of somebody else's
content at `<work-dir>/blends/fixture.blend`, then `--blend fixture=...`:

```
BEFORE (both guards reverted in place):
  file before: BLENDER-SOMEONE-ELSES-FILE
  rig exit = 0, RIG PASSED
  file after : 0000000  B L E N D E R 1 7 - 0 1 v 0 5 0 2 R E N D ...   <- the fixture, written over it

AFTER (repaired rig):
  file before: BLENDER-SOMEONE-ELSES-FILE
  rig exit = 1
  RIG FAILED: RigError: --work-dir <scratch>/cycle3/b3work already has contents and carries no
  .blender-rig-owned, so the rig did not create it and will not write into it (found: blends).
  Point --work-dir at an empty or rig-created directory.
  file after : BLENDER-SOMEONE-ELSES-FILE
  work dir contents: . .. blends        <- Blender was never launched
```

And the second layer, for a work dir the rig *does* own whose `blends/` it did not create:

```
  (root marker present, so the claim succeeds; blends/ carries no marker)
  rig exit = 1
  RIG FAILED: RigError: <scratch>/cycle3/b3work2/blends exists but carries no .blender-rig-owned,
  so the rig did not create it and will not write into it.
  file after : BLENDER-SOMEONE-ELSES-FILE
```

**The isolation claim, measured rather than asserted (R3).** A second run with an in-Blender probe:

```
RIG-BLENDER: user_resource(CONFIG    ) = <scratch>/step5-repaired/probework/config
RIG-BLENDER: user_resource(DATAFILES ) = <scratch>/step5-repaired/probework/datafiles
RIG-BLENDER: user_resource(EXTENSIONS) = <scratch>/step5-repaired/probework/extensions
RIG-BLENDER: user_resource(SCRIPTS   ) = <scratch>/step5-repaired/probework
RIG PASSED
```

All four now resolve under the work dir. Under cycle 1's `BLENDER_USER_SCRIPTS`-only launch, only `SCRIPTS`
did; the other three resolved under `~/Library/Application Support/Blender/5.2/`. The failure run below
independently shows Blender writing `userpref.blend` into the work dir's `config/`.

### R1 — the critical finding, demonstrated before and after

**Before (the previously staged rig).** A stub process was left listening on 9876 — the addon's own default,
i.e. where a developer's live Blender sits — answering `ping` the way the addon does:

```
=== BEFORE: previously staged rig, its default port 9876, a stranger already listening ===
RIG: Blender up on 127.0.0.1:9876, work dir <scratch>/r1demo/oldwork
--> {"id": "rig-1", "type": "ping", "params": {}}
<-- {"status": "success", "result": {"pong": true}, "id": "rig-1"}
SCENARIO: talking to {'status': 'success', 'result': {'pong': True}, 'id': 'rig-1'}
RIG PASSED
exit=0
```

The rig adopted the stranger, ran the scenario against it, and reported success. This is the defect verbatim.

**After (repaired rig, same stub, `--port 9876` explicitly):**

```
RIG FAILED: RigError: something is already listening on 127.0.0.1:9876; refusing to adopt it. The rig would
otherwise drive that process - most likely your own running Blender - and report its answers as the rig's.
Stop it, or pass --port with a free port.
exit=1
```

It aborts before creating the work directory and before launching Blender; the work dir does not exist
afterwards. With no `--port`, the rig never reaches 9876 at all — it asks the kernel for a free one.

**The second layer, for a bind that fails after the preflight passed.** A process that `bind()`s a port without
`listen()`ing makes `connect()` fail, so the preflight sees a free port, while Blender's own `bind()` still
fails with `EADDRINUSE` — the deterministic version of the race:

```
RIG: server running = False
RIG FAILED: RigError: the Blender this rig launched never answered a ping on port 49536 within 30s
--- last 6 lines of <scratch>/r1demo/holdwork/blender.log ---
BlenderMCP addon registered
Failed to start server: [Errno 48] Address already in use
BlenderMCP server stopped
RIG: server running = False
00:17.606  blend            | Writing user preferences: "<scratch>/r1demo/holdwork/config/userpref.blend"
00:17.608  operator         | Preferences saved
--- end of log ---
exit=1
```

No receipt was written, so the rig failed closed rather than adopting whatever might have answered. The log
tail — which cycle 1 discarded entirely — carries the root cause verbatim, and incidentally shows Blender
writing its preferences **into the work dir** rather than into the user's profile (R3).

**Not demonstrated:** the true TOCTOU race (a listener that grabs the port in the window between the rig's
preflight and Blender's `bind()`). Three attempts failed to land inside that window; the deterministic
bind-without-listen case above exercises the same code path, and
`test_the_readiness_receipt_must_carry_the_rig_s_own_nonce` covers the receipt check directly. Stated as not
run rather than claimed.

### The container half — measured 2026-09-15, not escaped

**Every line in this section is a timestamped measurement, not a standing fact.** The previous revision of this
section recorded "Docker is not available on this host" as a *standing* claim, and used it to take acceptance
criteria 2 and 3 through their escape hatches. That claim was true when it was made and **false by the time it
was relied on**, which is this dimension's own failure mode: an environment observation written as a property of
the host rather than as a reading taken at a moment.

| When (UTC) | What was run | Result |
|---|---|---|
| 2026-09-14, before dispatch | `docker info` | **fails** — CLI present at 29.8.0, no reachable daemon |
| 2026-09-15 05:27 | `docker info` | **exits 0** — Server Version 29.8.0, context `desktop-linux`, 15 containers running; `docker compose` v5.5.1 |
| 2026-09-15 05:27–05:38 | `docker compose -f docker/blender/docker-compose.yml up --wait --build` | image **built**; container came up **unhealthy** |
| 2026-09-15 06:15–06:21 | the same command, after the transport port and both entrypoint fixes | **healthy**, rc 0; served `tools/list` = **53** |

The first three rows were measured by the orchestrator on a host with a live daemon; the cycle-3 repair agent
did not run Docker itself. The fourth row was measured by the follow-up agent, which did.

**Acceptance criterion 2 — `docker compose ... up --wait` reaches healthy: FAILED at 05:27–05:38,
PASSES at 06:20:58–06:21:10** (see "The container half, after the transport port"). Not "not run", not
"escaped". The container was built and started and did not become healthy; the cause was the missing transport,
and closing that closed the criterion.

**Root cause, confirmed — and it is a plan defect, not a container defect.** `docker/blender/entrypoint.sh:37`
sets `BLENDERMCP_TRANSPORT=http`, and **that feature does not exist on `main`**:
`git grep -ln 'BLENDERMCP_TRANSPORT' main -- '*.py'` returns nothing. On `docker-blender` it lives in
`src/blender_mcp/server/cli.py` with `tests/server/test_cli_transport.py`, added by `ee25ffc`
("feat(server): serve streamable HTTP when BLENDERMCP_TRANSPORT=http"). **Neither file is in Task 1's Files
list.** With no HTTP transport the server runs stdio, takes EOF on a non-TTY stdin, and exits immediately, which
trips the entrypoint's `wait -n` and takes Blender down with it. See decision 8.

**Acceptance criterion 3 — the running server's `tools/list` count matching `measure_catalog.py shot` (53):
NOT OBSERVED at 05:27–05:38, OBSERVED and matching at 06:21.** The static half holds (`BLENDER_MCP_TOOLSETS: shot` is in
`docker/blender/docker-compose.yml:32`, the file parses, and the selection it names measures 53 tools /
203,079 B). The served half was not reached **then**, because the server that would serve `tools/list` exited for
the reason above. It was reached on 2026-09-15 at 06:21, against the running container, and the two numbers
agree: **53 = 53**. Container parity is now claimed, and the transcript is below.

**What the run did prove, in-container** — worth recording as positive evidence, because it narrows the gap to
one missing feature rather than a whole untested stack:

- Blender **5.2.1 LTS** starts under Xvfb inside the container and serves its addon socket on 9876; the
  orchestrator connected to it.
- The addon installs into the image's Blender and reports **protocol 31** — the same number the local GUI rig
  reports, now confirmed on Linux/amd64 as well as macOS/arm64.
- With Blender already up, the MCP server logged `Successfully connected to Blender on startup - protocol 31,
  addon [2, 0, 0], Blender 5.2.1 LTS` before exiting for the stdio reason above. The addon/handshake half of the
  port is therefore proven in-container; only the transport is missing.
- **R11 validated live.** The run created `docker/blender/output/` and it correctly does **not** appear in
  `git status` — the `.gitignore` entry restored by R11 is doing its job, which could not be checked without a
  real `docker compose up`.

**Two further entrypoint defects the run exposed** (recorded here, deliberately **not** fixed in cycle 3 —
editing `entrypoint.sh` from two places at once is how a half-applied fix ships; **both were fixed by the
follow-up on 2026-09-15**, and each is demonstrated before and after below):

1. **The MCP server does not wait for Blender.** It gave up 10 ms after start with
   `Failed to connect to Blender: [Errno 111] Connection refused`, so `entrypoint.sh:36`'s comment — "The
   server's first Blender connection retries while Blender finishes starting" — is false.
2. **A dead Blender does not take the container down.** `entrypoint.sh:50`'s bare `wait` blocks forever on the
   Xvfb child, so the claim at `entrypoint.sh:45-46` does not hold. The container sat `Up (unhealthy)`
   indefinitely with both Blender and the MCP server dead.

**A static guard that gives false assurance, for the follow-up to fix with the feature.**
`tests/test_docker_rig.py::test_entrypoint_serves_the_mcp_server_over_http_on_the_published_port` asserts that
`BLENDERMCP_TRANSPORT=http` appears in `entrypoint.sh`. It passes today **while the feature it implies does not
exist on `main`** — the guard checks the container's side of a contract whose server side was never ported. It
is not weakened here; the follow-up that ports `cli.py` should extend it to assert the server can actually read
that variable. **Done 2026-09-15:** the test now imports `TRANSPORT_ENV`, `HTTP_HOST_ENV` and `HTTP_PORT_ENV`
from `blender_mcp.server.cli`, runs the server's own `transport_from_env` over the values `entrypoint.sh`
actually sets, and requires the resolved port to equal the container port `docker-compose.yml` publishes. Both
original assertions survive inside a stronger one; neither was weakened.

**One container claim that needs no daemon, and was checked:** the image installs the server's dependencies from
`poetry.lock`, so that lock must agree with `pyproject.toml`. `poetry check --lock` → **"All set!"**, run
2026-09-14. `tests/test_docker_rig.py::test_server_dependencies_are_installed_from_the_poetry_lock` asserts the
Dockerfile actually copies and installs from it.

**The follow-up task, assigned above and completed 2026-09-15.** Task 1's repair cycle 3 deliberately did not
touch `src/blender_mcp/server/cli.py`, `tests/server/test_cli_transport.py` or `docker/blender/entrypoint.sh`;
the follow-up did, and nothing else. Its record is the next section.

### The container half, after the transport port — measured 2026-09-15 06:15–06:22 UTC

Same host, same daemon (Docker 29.8.0, compose v5.5.1), same emulated `linux/amd64` image.

**What was ported, and what it did *not* need.** `ee25ffc`'s `cli.py` surface reconciled onto `main` without
conflict: `main`'s `cli.py` was the branch's file minus the transport, so the port is purely additive
(`TRANSPORT_ENV` / `HTTP_HOST_ENV` / `HTTP_PORT_ENV`, `TransportConfig`, `transport_from_env`, `_parse_port`,
`_serve_http`, and `main()`'s dispatch; the interactive stdio hint moved into `_log_stdio_hint_when_interactive`
unchanged). **The stop-and-report condition never fired**: `git diff main docker-blender -- src/blender_mcp/server/app.py`
is empty, and the installed `mcp` package already exposes `settings.streamable_http_path` and
`settings.transport_security`, which are the only two things the ported test reaches for outside `cli.py`.
All 132 lines of `tests/server/test_cli_transport.py` ported with **no assertion touched**.

**Acceptance criterion 2 — PASSES.** `docker compose -f docker/blender/docker-compose.yml up --wait --build`,
started 06:20:58Z, returned **rc 0** at 06:21:10Z:

```
 Image blender-blender Built
 Network blender_default Created
 Container blender-blender-1 Created
 Container blender-blender-1 Starting
 Container blender-blender-1 Started
 Container blender-blender-1 Waiting
 Container blender-blender-1 Healthy

$ docker compose -f docker/blender/docker-compose.yml ps
NAME                IMAGE             COMMAND                SERVICE   CREATED          STATUS                    PORTS
blender-blender-1   blender-blender   "/opt/entrypoint.sh"   blender   12 seconds ago   Up 11 seconds (healthy)   127.0.0.1:8000->8000/tcp
```

The container's first log lines show both fixes and the transport working together:

```
BlenderMCP addon registered
Server thread started
BlenderMCP server started on localhost:9876
entrypoint: Blender answered a ping on port 9876
2026-09-15 06:21:09,295 - BlenderMCPServer - INFO - BlenderMCP serving streamable HTTP at http://0.0.0.0:8000/mcp
INFO:     Started server process [75]
```

and, from the equivalent run at 06:16, the startup handshake that defect 1 used to throw away:

```
2026-09-15 06:16:11,753 - BlenderMCPServer - INFO - Blender addon up to date (protocol 31, addon [2, 0, 0], Blender 5.2.1 LTS)
2026-09-15 06:16:11,753 - BlenderMCPServer - INFO - Successfully connected to Blender on startup
```

Before the fix this read `Failed to connect to Blender: [Errno 111] Connection refused`, 10 ms after start.
**Note what this does and does not change:** `server_lifespan` catches that failure and serves anyway, and the
healthcheck round-trips Blender's socket directly, so the missing wait never by itself decided health — it
decided whether the handshake and its protocol number existed in the logs at all, and whether the first tool
call paid for a reconnect. The container's unhealthy verdict was the transport, not this.

**Acceptance criterion 3 — PASSES, 53 = 53.** Against the **running** container at 06:21, over streamable HTTP
(`initialize` → `notifications/initialized` → `tools/list`, one page, no `nextCursor`):

```
server: {"name": "BlenderMCP", "version": "2.0.0"}
pages: 1
tools/list count: 53

$ .venv/bin/python scripts/measure_catalog.py shot
selection : shot
tools     : 53
bytes     : 203,079
```

Compose pins `BLENDER_MCP_TOOLSETS: shot`; the served names are exactly the `shot` selection (camera, lighting,
render, animation, scene and `get_addon_status` / `get_integration_status`), not `core`'s 21.

**Defect 2 demonstrated before and after, by killing Blender inside the container.** `SIGKILL` to
`blender --python /opt/start_server.py`, with nothing else touched:

```
BEFORE (the original entrypoint, restored in place from its own staged blob e7bf585, image rebuilt):
  container before the kill .... Up About a minute (healthy)
  45 s after SIGKILL ........... Up About a minute (healthy)      Status=running
  once the healthcheck's 30
  consecutive failures elapsed
  (06:20:28Z) .................. Up About a minute (unhealthy)    Status=running
  processes still alive:  1: /bin/bash /opt/entrypoint.sh
                         11: Xvfb :99 -screen 0 1280x720x24
  (Blender dead, the MCP server killed by the trap, PID 1 blocked forever in the bare `wait` on Xvfb)

AFTER (the repaired entrypoint):
  container before the kill .... Up 26 seconds (healthy)
  20 s after SIGKILL ........... Exited (137) 19 seconds ago      Status=exited ExitCode=137
```

**A finding the before/after produced, which the original diagnosis did not name.** The defect had **two**
halves, and either one alone is sufficient to cause it: the bare `wait` waits for every child, *and* teardown
never signalled Xvfb. Reverting only the bare `wait` — while leaving Xvfb in the kill list — still exits the
container, because the bare `wait` then returns once Xvfb dies. Measured: that half-revert exited 137 as well.
Only restoring the original file reproduced `Up (unhealthy)`. Both halves are therefore fixed, and
`entrypoint.sh` says so at the two sites rather than crediting one.

**Comments corrected, not just code.** The old `entrypoint.sh:34-36`'s "The server's first Blender connection retries
while Blender finishes starting" was false — `server/connection.py`'s `get_blender_connection` raises on the
first failed `connect()` and has no retry at all — and is gone; the readiness gate above it now describes the
single attempt, why a TCP connect is not enough (the kernel accepts into the listen backlog before the addon can
reply), the deadline, and the liveness check. The old `entrypoint.sh:45-46`'s "Either process exiting takes the
container down with it" was true of `wait -n` and false of what followed it; the teardown now says which pids it
waits on and why a bare `wait` blocked on Xvfb, and the trap comment records that Xvfb was the other half.

**Tear-down and churn.** `docker compose down` run afterwards; `git status` shows no new entries,
`docker/blender/output/` exists on disk and stays invisible (`git check-ignore -v` → `.gitignore:232`),
`uv.lock` is still untracked and unstaged, and nothing under `__pycache__` is staged.

## 5.2.2 re-verification (2026-09-15, session 2, before Task 2)

The host was upgraded **Blender 5.2.1 LTS -> 5.2.2 LTS** (`/opt/homebrew/bin/blender`, build 2026-09-15, hash
`d13f752e3b9c`) between session 1 and session 2. Because the whole plan's API layer was measured against 5.2.1,
every load-bearing fact was **re-run against 5.2.2 before Task 2 started**, rather than assumed to carry.
Probe scripts are in this session's scratchpad (`api522/probe_{a,b,c,d,e,f,g}.py`); reproduce them, do not
trust this table.

**Result: every fact holds. One entry is refined, two apparent deviations were my own probe's artifacts.**

| Fact (as stated for 5.2.1) | 5.2.2 | Evidence |
|---|---|---|
| `Collection`/`ID`/`Object`.`override_create` + `override_hierarchy_create` exist | holds | `bl_rna.functions` lists both on all three |
| `override_hierarchy_create(scene, view_layer, reference=None, do_fully_editable=False)` | holds | `__doc__` verbatim |
| `dir()` on a type object is blind | holds | `'copy' in dir(bpy.types.Collection)` -> `False`; on an instance -> `True` |
| `Library.reload()` exists | holds | present in `bpy.types.Library.bl_rna.functions` |
| `Library.users_id` works but is absent from `bl_rna.properties` | holds | `bl_rna` -> `False`; `dir(instance)` -> `True`; returns the 3 linked datablocks |
| `bpy.app.handlers` has `load_post_fail` / `save_post_fail` | holds | all nine probed present |
| `save_as_mainfile.relative_remap` default `True`, `save_mainfile`'s `False` | holds | RNA defaults |
| `check_existing` defaults `True` on both and is **inert** | holds | a 27 B file silently overwritten with 95,912 B, `{'FINISHED'}` |
| `open_mainfile.use_scripts` default `False`; `use_scripts_auto_execute` `False` | holds | RNA default + preference |
| `bpy.path.abspath` normalises nothing and absolutizes nothing | holds | `'//../escape.blend'` -> `'../escape.blend'`; `'relative.blend'` unchanged |
| `orphans_purge(do_local_ids, do_linked_ids, do_recursive) -> int` | holds | `__doc__` |
| open/save operators **raise `RuntimeError`, never `{'CANCELLED'}`** | holds | all five failure modes raised |
| Error shapes 1-5, including the twice-embedded path and the CWD leak | holds | transcripts below |
| Three `.blend` magic signatures | holds | `BLENDER17-01`, `(\xb5/\xfd`, `\x1f\x8b`; all three opened `{'FINISHED'}` |
| Handler-firing table + `session_uid` churn | holds | **3 of 3** churned; transcript below |
| `bpy.app.timers` never fire in `--background` | holds | **0** fires over 3.0 s, `is_registered` stays `True` |

### Finding 1 — two apparent deviations that were my probe's fault, not 5.2.2's

Recorded because each would have been a plausible-looking false correction of the kind §01 warns about.

- **"Error shape 3 has disappeared."** A `.blend` truncated to 64 bytes produced shape 2
  (`File format is not supported`), not shape 3 (`Missing DNA block`). Cause: **5.2.2 writes a zstd-compressed
  file from a bare `save_as_mainfile`**, so I had truncated a *compressed* file, whose corrupt zstd stream is
  rejected before any DNA parsing. Saving with `compress=False` and truncating to 64 / 512 / 4096 bytes
  reproduces shape 3 exactly, at all three lengths, still carrying the absolute path **twice** in two quoting
  styles. **Shape 3 is alive; the sanitizer requirement is unchanged.**
- **"The compress default changed."** The operator property default really is `compress=False` on both save
  operators — but `preferences.filepaths.use_file_compression` is `True` at **factory settings**, and it wins.
  So a bare `save_as_mainfile(filepath=...)` writes zstd, while `compress=False` writes `BLENDER17-01`.
  **This is a real and previously unrecorded subtlety and it belongs to Task 6:** `save_shot` that does not pass
  `compress` explicitly produces a file whose format depends on a user preference the caller cannot see. It is
  the same class of trap as `relative_remap` inheriting `True` — an operator default that is not what you get.

### Finding 2 — `override_create()`'s return value, refined (the claim that survived two passes)

The handoff's `[CORRECTED TWICE]` entry says `override_create()` returns the new override ID on a linked,
overridable ID and `None` only on a non-overridable one "such as a local datablock". **Measured on 5.2.2, that
is correct, but "non-overridable" is a wider class than that gloss suggests** — and my first probe walked
straight into it, getting `None` from *both* arms and therefore proving nothing:

```
local collection  .override_create() -> None            (objects 1 -> 1, no new uid: it did nothing)
LINKED object     .override_create() -> None            (objects 1 -> 1, no new uid: it did nothing)
  ... the linked object's state: is_library_indirect=True, in view layer=False
LINKED collection .override_create() -> bpy.data.collections['CanonHero']   (collections 1 -> 2)
override_hierarchy_create(..., do_fully_editable=True) -> bpy.data.collections['CanonHero']
```

The discriminator is **direct vs indirect linkage**, not local vs linked. An object pulled in as a member of a
linked collection is `is_library_indirect=True` and is **not** directly overridable, so `override_create()`
returns `None` *and does nothing*. Counting `bpy.data.objects` before and after is what separates "returned
`None` because it failed" from "returned `None` on success" — the return value alone cannot.

**Why this is worth a finding rather than a footnote:** an implementer verifying the handoff's claim on the
nearest linked *object* would measure `None`, conclude the handoff's correction was itself wrong, and "correct"
it back to the original false claim — the exact three-pass cycle §01 describes. The distinguishing test must be
the side effect, not the return.

Task 7's ruling is unaffected: it rules for `override_hierarchy_create(..., do_fully_editable=True)` on the
grounds that `override_create` handles one ID at a time, which is orthogonal to this.

### Finding 3 — Route C's name collision reproduces exactly

```
Route A (create_liboverrides=True): objects [('HeroBody', editable=False, linked=True)]; 2 collections named CanonHero
Route B (override_hierarchy_create): objects [('HeroBody', True, sys_override=True), ('HeroBody', False, ...)]
Route C (do_fully_editable=True):
    object     'HeroBody'  uid=782  editable=True   linked=False  system_override=False
    object     'HeroBody'  uid=779  editable=False  linked=True   system_override=None
    collection 'CanonHero' uid=781  editable=True   linked=False
    collection 'CanonHero' uid=778  editable=False  linked=True
    >>> duplicate OBJECT names: True      >>> duplicate COLLECTION names: True
```

Two same-named collections **and** two same-named objects, exactly as the `[CORRECTED]` entry states. The
`session_uid`-not-name signature ruling for Task 7 is confirmed on 5.2.2.

### Finding 4 — the handler-firing table, reproduced

```
libraries.load(link=True)   -> handlers fired: ['blend_import_pre', 'blend_import_post']
Library.users_id            -> [collections['CanonHero'], meshes['HeroMesh'], objects['HeroBody']]
session_uids before reload  -> {'CanonHero': 386, 'HeroMesh': 388, 'HeroBody': 387}
lib.reload()                -> handlers fired: ['blend_import_pre', 'blend_import_post']
session_uids after reload   -> {'CanonHero': 389, 'HeroMesh': 391, 'HeroBody': 390}
CHURNED: 3 of 3 datablocks got a fresh session_uid
failed lib.reload()         -> RAISED RuntimeError; handlers fired: NONE
orphans_purge(...)          -> 0; handlers fired: NONE
```

`load_post` fired for **none** of them. Task 4 item 2a's premise (`_DATABLOCK_REPLACING_COMMANDS`, not a
`blend_import_post` handler) stands unchanged on 5.2.2.

### Finding 5 — the five error shapes, re-captured on 5.2.2

```
1 open missing:     Error: Cannot read file "<abs>": No such file or directory
2 open directory:   Error: File format is not supported in file "<abs>"
2 open non-.blend:  Error: File format is not supported in file "<abs>"
2 open EMPTY PATH:  Error: File format is not supported in file "/Users/jpease/Developer/github/jpease/blender-mcp"
3 open truncated:   Error: Loading "<abs>" failed: Failed to read blend file '<abs>': Missing DNA block
4 save unwritable:  Error: Cannot open file <abs>@ for writing: No such file or directory
5 reload bad path:  Error: Trying to reload library 'LIcanon.blend' from invalid path '<abs>'
```

All four traps survive: shape 4's derived `@` name, shape 3's **two** embedded paths in two quoting styles,
shape 2's absence of any tail after the path, and shape 2's empty-path variant leaking the **server's process
CWD** (here the repo root) rather than any caller-supplied path.

### Live rig transcript — Step 5 re-run by the reviewer on 5.2.2

Task 1's Step 5 scenario files did not survive session 1 (they lived in that session's scratchpad, which is
gone — see "Known failures / blocked"). They were **rebuilt from the documented behaviour** and re-run. Blender
**5.2.2 LTS**, GUI (not `--background`), fresh work dir, exit code **0**.

```
RIG: server running = True
RIG: readiness receipt written to <scratch>/step5/work/rig_ready.json
RIG-BLENDER: watcher + probe timers registered, waiting for <scratch>/step5/work/open_now.json
RIG: Blender (pid 71024) up on 127.0.0.1:52169, work dir <scratch>/step5/work
--> {"id": "rig-1", "type": "ping", "params": {}}
<-- {"status": "success", "result": {"pong": true}, "id": "rig-1"}
--> {"id": "rig-2", "type": "get_addon_info", "params": {}}
<-- {"status": "success", "result": {"name": "Blender MCP", "addon_version": [2, 0, 0],
     "protocol_version": 31, "capabilities": [... 250 entries elided ...],
     "blender_version": "5.2.2 LTS",
     "writable_output_roots": ["<scratch>/step5/work",
                               "<scratch>/step5/work/tmp/blender_8sT57u",
                               "<scratch>/step5/work/tmp",
                               "/Users/jpease"]}, "id": "rig-2"}
RIG: protocol_version = 31
RIG: blender_version = 5.2.2 LTS
RIG: asked Blender to open <scratch>/step5/work/blends/fixture.blend
RIG: in-Blender outcome = {
  "before_filepath": "",
  "before_objects": ["Camera", "Cube", "Light"],
  "before_persistent_timer_registered": true,
  "before_volatile_timer_registered": true,
  "before_persistent_fires": 3,
  "before_volatile_fires": 3,
  "before_drain_is_registered_fresh_bound_method": false,
  "open_mainfile_result": ["FINISHED"],
  "after_filepath": "<scratch>/step5/work/blends/fixture.blend",
  "after_objects": ["RigFixtureCube"],
  "after_persistent_timer_registered": true,
  "after_volatile_timer_registered": false,
  "after_persistent_fires": 3,
  "after_volatile_fires": 3,
  "after_drain_is_registered_fresh_bound_method": false,
  "callback_frame_survived_the_swap": true
}
RIG: persistent timer fires 3 -> 4 after the swap
--> {"id": "rig-3", "type": "ping", "params": {}}
<-- {"status": "success", "result": {"pong": true}, "id": "rig-3"}
RIG: post-load ping answered -> {"status": "success", "result": {"pong": true}, "id": "rig-3"}
--> {"id": "rig-4", "type": "get_addon_info", "params": {}}
RIG: post-load get_addon_info protocol_version = 31
RIG: post-load writable_output_roots = ['<scratch>/step5/work', '<scratch>/step5/work/blends',
                                        '<scratch>/step5/work/tmp/blender_8sT57u',
                                        '<scratch>/step5/work/tmp', '/Users/jpease']
RIG PASSED
```

Only the scratch prefix and the capability list are elided. **All six items of Task 1 acceptance criterion 4
reproduce on 5.2.2**, including the volatile-timer control reading `false` (so the probe can still tell the two
apart) and the persistent timer's fire count moving 3 -> 4 *after* the swap. The caller's fixture was not
mutated: MD5 `3d5f92b26ca2b634e673bfd967eb8cd8` before and after.

The `before_drain_is_registered_fresh_bound_method: false` reading, taken while the server was demonstrably
answering pings, reproduces the 5.2.1 identity-matching finding on 5.2.2 — so Task 3's obligation to fix
`test_threading.py`'s stub before believing any `server_core.py` timer fix is unchanged.

### Version pins moved with this re-verification

| Pin | From | To | Note |
|---|---|---|---|
| `docker/blender/Dockerfile` `ARG BLENDER_VERSION` | 5.2.1 | 5.2.2 | `blender-5.2.2-linux-x64.tar.xz` confirmed present (HTTP 200) before the change |
| handoff §08 environment line | 5.2.1 LTS | 5.2.2 LTS | plus a re-verification note over the API-facts list |
| plan Tech Stack + "Locally, Blender ... is at" | 5.2.1 | 5.2.2 | plus a dated version note |

**Deliberately not changed**, each for a stated reason:

- **Historical measurement text** ("measured 2026-09-14 against 5.2.1") throughout both plan documents. Those
  sentences are true statements about when a measurement was taken; rewriting them to 5.2.2 would forge the
  provenance of evidence this session did not gather. The re-verification note above carries the current claim.
- **`pyproject.toml`'s `blender-python-stubs = "^5.2.1.1"`** — a constraint range, not a pin, and it already
  admits 5.2.2.x. There is no `pip` in `.venv` to confirm a 5.2.2 stub release exists, so tightening it would
  be an unmeasured change.
- **`scripts/revert_matrix.py`'s hardcoded `5.2.1` download URL** — it is the *deliberately broken* replacement
  used to prove `test_dockerfile_installs_the_blender_version_it_declares` can fail. That test asserts the URL
  interpolates `${BLENDER_VERSION}`, never a specific number, so any literal serves; the revert's `old` string
  is the interpolated form, which this change did not touch.
- **`tests/test_addon_manager.py`'s `"blender_version": "5.2.1"` fixtures** — opaque handshake payload strings
  that no assertion compares to the host's version.

## Known failures / blocked

- **[CLOSED 2026-09-15] Task 1's Step 5 scenario harness was never committed and did not survive session 1.**
  Closed by committing it under `scripts/rig_scenarios/` (scenario, in-Blender half, and a fixture *generator*
  rather than the binary), plus the seven 5.2.2 probes under `scripts/blender_probes/` and the anchor checker
  as `scripts/check_revert_anchors.py`. The committed copies were then run from a clean directory end to end -
  `RIG PASSED`, exit 0 - so the transcripts in this file are reproducible from the repository for the first
  time. Task 10 still owns committing a *gate* scenario; this closes only the Step 5 one. Original entry:
  `scenario_step5.py`, `in_blender_open.py` and `fixture.blend` lived in that session's scratchpad, which is
  gone; only the transcripts remain. The three Step 5 transcripts in this file were therefore **not
  reproducible from the repository** as committed at `2852803`. Session 2 rebuilt all three from the documented
  behaviour and re-ran them green on 5.2.2 (see "5.2.2 re-verification"), but the rebuilt copies are likewise
  in a scratchpad. **Consequence for the phase:** eight of ten tasks have a live transcript as a hard exit
  gate, and `scripts/blender_rig.py` is useless without a scenario to hand it. **Task 10 should commit a
  scenario harness** rather than leaving each session to reconstruct one — it already owns the gate scenario,
  and `tests/test_blender_rig.py` gives it a home. Raised here rather than fixed, because committing a
  scenario module is Task 10's scope, not a version bump's.

- **Acceptance criteria 2 and 3 are CLOSED** (2026-09-15 06:15–06:22) — see "The container half, after the
  transport port". `up --wait --build` reaches healthy with rc 0, and the running server's `tools/list` count
  is 53, matching `measure_catalog.py shot`. The cause of the earlier failure was a missing port (`ee25ffc`'s
  HTTP transport), not the environment; porting it closed both. Container parity is established for the `shot`
  selection **only** — no other selection has been served from a container, and nothing has been measured on a
  native `linux/amd64` host rather than under emulation.
- **Two `tests/test_blender_rig.py` nodes are load-sensitive**, not broken: see the flake note under "State
  after the transport follow-up". They wait on a real child process with 30 s / 10 s bounds and failed once in
  a 79 s contended run, passing in every uncontended run since.
- **Latent trap in `server_core.py`'s timer lifecycle — pre-existing on `main`, NOT introduced by Task 1, and
  deliberately not fixed here** (it is outside Task 1's Files list; raising it rather than silently widening
  scope). Because `is_registered` is identity-based (measured above):
  - `start():185` — `if not bpy.app.timers.is_registered(self.drain_command_queue)` is **always true**, so the
    guard never guards. It is currently harmless only because `start()` returns early when `self.running`.
  - `stop():197` — `if bpy.app.timers.is_registered(self.drain_command_queue)` is **always false**, so `stop()`
    **never unregisters the timer**. Currently harmless only because `drain_command_queue` returns `None` when
    `self.running` is false, which unregisters it from the inside on the next tick.
  Both are correct today by accident rather than by the mechanism the code reads as using. **Task 3 owns the
  drain loop and should decide whether to fix this** (hold the bound method in an attribute at registration
  time and pass that same object to `is_registered`/`unregister`). Flagged, not changed.
  - **`tests/server/test_threading.py`'s stub cannot catch this bug, so Task 3 must fix the stub first.**
    Verified in this session: the stub's `registered` is a **`dict`** (`test_threading.py:46`) and its
    `is_registered` is `fn in registered` (`:59-60`), i.e. `__eq__` matching — and two bound methods of the
    same object *compare equal* even though they are not identical. So `assert not bpy_timer_registered(server)`
    (`:218`) passes today **for the wrong reason**: under the stub `stop()` genuinely unregisters, while in
    Blender it never does. Task 3 must switch the stub to identity matching (`any(fn is k for k in registered)`)
    **before** believing any fix to `server_core.py`.
  - **Also for Task 3, and unrelated to timers:** `_writable_output_roots()` runs `os.path.isdir` +
    `os.access` over every configured root on Blender's **main thread**, on every handshake, uncached. A
    configured root on a dead NFS or automount mount blocks the drain loop for the mount timeout, during which
    no client gets any response at all.

## Flagged for Task 5 (`output_roots.py` promotion) — recorded, deliberately not changed here

Changing ported `output_roots.py` behaviour in Task 1 would pre-empt the design decision Task 5's ruling
explicitly owns, so these are written into `_writable_output_roots()`'s docstring and recorded here instead.
The docstring now states plainly that the list is an **advisory preference ranking, not an enforced root set**.

- **`bpy.app.tempdir` is session-scoped and Blender deletes it on exit, yet it ranks *first*** whenever no
  `.blend` is open and no roots are configured. Visible in every Step 5 transcript as
  `.../T/blender_<random>` (cycle 3: `<work-dir>/tmp/blender_<random>`, since repair B2 moved `TMPDIR`). An
  agent that writes a render to the first offered root loses it silently when Blender quits. **Repair B2 moves
  where that directory is, not where it ranks** — under the rig the configured work dir now leads, but on an
  unconfigured desktop install `bpy.app.tempdir` is still first, which is the case Task 5 has to decide.
- **`writable_roots` normalizes with `abspath`, not `realpath`**, so a symlinked root is *reported* one way and
  would be *enforced* another. The rubric's filesystem dimension mandates
  `realpath(abspath(bpy.path.abspath(...)))`.
- **The default candidate set ends at `~`.** Every Step 5 transcript, cycle 3 included, shows `/Users/jpease`
  in the advertised list even with `BLENDERMCP_OUTPUT_ROOTS` set — the configured roots are *prepended*, not
  substituted. If Task 5 derives the *enforced* roots from these defaults on an unconfigured desktop install,
  the containment boundary becomes the whole home directory. Repair B1 corrected every place that claimed
  otherwise; it did not change this behaviour, which is Task 5's to decide.

## Flagged for Task 10 — criterion 4 is not verifiable with this rig as built

Task 10 criterion 4 ("request count equals response count across the whole scenario") **cannot be checked**
through `scripts/blender_rig.py` as it stands: `_round_trip` opens and closes a fresh connection per command
(deliberately — it proves the listener is still accepting), so a duplicate or misrouted *second* response lands
on a socket that is already closed and is swallowed by `drain_command_queue`'s "client disconnected" branch.
Two options, **neither implemented here**, because choosing between them is Task 10's call:

1. Add a long-lived-connection counting mode to the rig, and run the gate scenario through it.
2. Amend criterion 4 to something this rig can observe (e.g. every request is answered exactly once *as
   observed per connection*), and say so explicitly in the gate.

## No existing test was edited or weakened

This is a §03 constraint in its own right, not a third option under the heading above — where it sat until
cycle 3 moved it.

All **647** pre-existing tests pass unmodified (**723 total, +76 new**). Four tests *added by Task 1 itself*
have been corrected after being shown to survive a revert of the thing their name claims to pin. None is
pre-existing, and no correction weakened an assertion: every one made a test discriminating that previously was
not.

| Test | Found in | How it survived | Correction |
|---|---|---|---|
| `test_handshake_defaults_writable_output_roots_for_an_older_addon` | cycle 1 critics (R17) | `== []` could not tell the parse's fallback from the dataclass default | renamed to `..._when_the_addon_omits_them`, fixture moved to `EXPECTED_ADDON_PROTOCOL_VERSION - 1`, populated control added |
| `test_the_default_port_is_an_unused_ephemeral_port` | cycle 2, by the matrix itself | called `_choose_port(0)` directly, so it never read `--port`'s default | asserts through `_parse_arguments` |
| `test_get_addon_status_reports_no_roots_for_an_addon_that_does_not_send_them` | cycle 2 critic 4 (ADDENDUM F1) | passed with `"writable_output_roots": []` hardcoded in the payload; its docstring also said "older than protocol 31" while the fixture was a *current* addon | fixture moved to `EXPECTED_ADDON_PROTOCOL_VERSION - 1` with `up_to_date=False`, populated control added (F1) |
| `test_staging_refuses_a_symlinked_addons_directory` | **cycle 3, by the extended matrix** | the symlink target carried no marker, so the *marker* check refused it; deleting the symlink check entirely left the test green | the target now carries the rig's own marker, so only the symlink check can refuse it |

The fourth was found only because cycle 3 extended the revert matrix from 24 of 51 nodes to **every** new node
(ADDENDUM F6). That is the argument for the extension, stated as a result rather than as a principle.

Nothing was found that looked wrong enough to report under §03's "stop and raise it" rule other than the
timer-lifecycle item above, which is code rather than a test.

## Rubric scores by task

### Task 1 — critic cycle 1 (the scores that actually came back)

The 100/100 previously recorded here was the implementer's self-assessment. Four adversarial critics scored the
staged work and **all five dimensions came in below their gate**, ~72/100 against a 90 exit gate:

| Dimension | Cycle-1 score | Gate | Result |
|---|---|---|---|
| Data durability and rollback safety | 21 / 30 | 24 | **fail** |
| Concurrency and liveness | 18 / 25 | 20 | **fail** |
| Filesystem and trust boundary | 15 / 20 | 16 | **fail** |
| Evidence quality | 11 / 15 | 12 | **fail** |
| Code quality and contract fidelity | 7 / 10 | 8 | **fail** |
| **Total** | **~72 / 100** | ≥ 90 | **fail** |

Two independent critics found the same top defect (R1). The self-score was wrong in a specific, repeatable way:
it scored the *intent* of the isolation ("stages into `--work-dir` via `BLENDER_USER_SCRIPTS`") rather than
measuring what that actually enforced, which turned out to be one of four resource roots.

### Task 1 — repairs applied in cycle 2

| # | Finding | What was done |
|---|---|---|
| R1 | The rig adopted whatever was already listening, including the developer's live Blender | Four parts, all in `scripts/blender_rig.py`: `--port` now defaults to **0** = a kernel-chosen free port (`_choose_port`); `_require_port_free` **aborts** if anything accepts on the chosen port; readiness is a **`ping` round-trip** (`_ping_answers`, matching `docker/blender/healthcheck.py`) instead of a bare connect; and the bootstrap writes a **receipt carrying this run's nonce and pid** after `start()` reports `running`, which `_wait_until_ready` requires. Demonstrated before/after above. |
| R2 | Blender's stdout was piped and never drained | `_drain_output` tees the combined output to `<work-dir>/blender.log` on a reader thread, echoing `RIG:`/`RIG-BLENDER:` lines; `_log_tail` quotes the last 40 lines in every startup/shutdown `RigError`; `Popen` is used as a context manager and the reader is joined after termination, never before. The `RIG: server running` line now appears in the transcript. |
| R3 | The isolation claim was false as written | `_child_environment` sets **both** `BLENDER_USER_RESOURCES` and `BLENDER_USER_SCRIPTS` to the work dir. Module docstring and README rewritten to say what is actually enforced, including that `--factory-startup` — not an environment variable — is what protects `userpref.blend`. Measured live with a `bpy.utils.user_resource` probe. |
| R4 | Unguarded `shutil.rmtree` on a caller-supplied path | `_stage_addon` refuses to delete `<work-dir>/addons` unless it carries the rig's `.blender-rig-owned` marker, and refuses outright if it is a symlink. Same discipline as `addon_manager.install_addon`. |
| R5 | The launched Blender advertised `/Users/jpease` as writable | `BLENDERMCP_OUTPUT_ROOTS=<work-dir>` is now set for the child. The work dir leads the advertised list in the new transcript. |
| R6 | `--blend` fixtures were handed over by their original path | `_stage_blends` copies each fixture into `<work-dir>/blends/` and hands the scenario the copy; fixture names are restricted so they cannot escape the work dir. Fixture MD5 identical before and after the live run. |
| R7 | No deadline on the scenario | `--scenario-timeout` (default 300 s) enforced by `_run_scenario_with_deadline`, which runs the scenario on a daemon thread, re-raises its own failure unchanged, and lets teardown run either way. |
| R8 | The child environment was inherited wholesale | `PYTHONPATH` and every inherited `BLENDERMCP_*` / `BLENDER_USER_*` variable are dropped before the rig sets its own. |
| R9 | The per-command socket timeout was hardcoded at 30 s | `--command-timeout`, default **180 s** to match `server/connection.py`, documented as unrelated to `--timeout` (the startup budget). |
| R10 | `scripts/blender_rig.py` had no tests at all | New `tests/test_blender_rig.py`, **21 tests**, in `tests/test_docker_rig.py`'s style: the five guards the repair list names, plus behavioural coverage of the port, receipt, staging, fixture-copy, deadline and drain paths. |
| R11 | `.gitignore` had lost `docker/blender/output/` | Entry restored with its original comment. |
| R12 | `_writable_output_roots()` caveats | Docstring now states the list is an advisory ranking, not an enforced root set, and names the three traps. Behaviour unchanged; recorded under "Flagged for Task 5". |
| R13 | The `is_registered` note was incomplete | Extended above: `test_threading.py`'s stub matches by `__eq__`, so it passes for the wrong reason and Task 3 must fix the stub first. Also flags the uncached `os.access` walk on the main thread. |
| R14 | Task 10 criterion 4 is unverifiable with this rig | Both options recorded under "Flagged for Task 10"; **neither implemented**. |
| R15 | The compose comment argued only the fidelity case | Added the clause that pinning `shot` also widens the loopback MCP surface from 21 tools to 53. |
| R16 | TASK_STATE corrections | The state table, the 333→334 correction, the real critic scores, the reproduction, and this table. |
| R17 | `test_handshake_defaults_writable_output_roots_for_an_older_addon` survived its own revert | Renamed to `..._when_the_addon_omits_them`; fixture moved to `EXPECTED_ADDON_PROTOCOL_VERSION - 1` so the name is true; a populated-payload control added so the test now **fails** when the parse line is deleted. Proven in the matrix below. |
| R18 | The false protocol-bump reason stood in two committed plan documents | Dated correction markers added at both sites, pointing at decision 5, which now lists all three. Surrounding analysis untouched. |
| R19 | `tests/test_docker_rig.py` re-typed the env var names it exists to keep in sync | Both imported now (`OUTPUT_ROOTS_ENV_VAR` loaded from the addon source, `TOOLSETS_ENV_VAR` from `server/bundles.py`), and a new test asserts compose's key **is** `TOOLSETS_ENV_VAR` and its value resolves through `resolve_toolset_modules`. `BLENDER_MCP_TOOLSETS` previously had no test at all. |
| R20 | Three confidently worded, false comments | `bundled/addon/__init__.py`, `addon_manager.py` and `server/tools/core.py` no longer describe a protocol-version check the code does not perform; `tests/test_output_roots.py`'s "like image_reply" reference (a module that never existed on `main`) removed; the rig's docstring now illustrates with `get_addon_info`, a command that exists, keeping the real `get_addon_status` half. |
| R21 | Missing docstrings; a return value nobody used | Docstrings added to `test_docker_rig.py`'s three helpers; `_stage_addon` now returns `None`. |
| R22 | TASK_STATE accuracy | Decision 6's "no information was lost" softened to what is actually true; the revert matrix pasted below; the "fires 2 -> 3" line explained; `poetry check --lock` run and recorded; the `ruff format` denominator caveat recorded. |

**Regression label for cycle 2: `improved`.** No dimension regressed. Nothing in cycle 1 that a critic passed
was undone: rulings A and C are honoured exactly, no pre-existing assertion was weakened, the protocol pair is
still locked by a non-self-confirming test, the response envelope is unchanged, and the Docker non-claim section
is intact and extended.

### Task 1 — critic cycle 2 (the scores that came back)

| Dimension | Cycle-1 | Cycle-2 | Gate | Cycle-2 result |
|---|---|---|---|---|
| Data durability and rollback safety | 21 / 30 | **24 / 30** | 24 | at gate |
| Concurrency and liveness | 18 / 25 | **20 / 25** | 20 | at gate |
| Filesystem and trust boundary | 15 / 20 | **17 / 20** | 16 | 1 over |
| Evidence quality | 11 / 15 | **12 / 15** | 12 | at gate |
| Code quality and contract fidelity | 7 / 10 | **8 / 10** | 8 | at gate |
| **Total** | ~72 / 100 | **81 / 100** | ≥ 90 | **fail** |

**Regression label for cycle 2: `improved` in every dimension, and no dimension regressed.** But every
dimension landed *at* its own 80% gate while the total needed 90, so cycle 2's arithmetic could not pass:
the ceiling on the three scored dimensions plus the two unscored ones was 86. Cycle 3 exists to take points
back in the three that were already scored.

### Task 1 — repairs applied in cycle 3

Labels are the cycle-3 list's own (A/B/C/D/E plus the ADDENDUM's F).

| # | Finding | What was done |
|---|---|---|
| A1 | The drain thread died on one non-UTF-8 byte, silently restoring the deadlock it fixed | `Popen` now decodes with `errors="replace"`; `_drain_output` became `_OutputDrain`, whose `_run` wraps the tee in a `BaseException` guard, falls back to consuming the **raw byte stream** (never the decoder that just failed) so the pipe cannot fill, and records the exception; `_shut_down` reports "the rig's own log reader died" rather than blaming Blender. Demonstrated before/after above. |
| A2 | `with Popen(...)` reintroduced an unbounded `wait()` in `__exit__` | The context manager is gone: `_execute` holds the process explicitly and tears it down in `try`/`finally`, with `drain.start()` **inside** the `try` so nothing can skip teardown. `test_the_process_is_never_waited_on_without_a_timeout` pins both halves plus "no `blender.wait()` without a timeout". |
| A3 | An abandoned scenario kept writing `-->`/`<--` into the transcript after the verdict | `BlenderRig` takes a `threading.Event` the deadline sets: `send()` raises on entry when it is set, `_echo` stops printing, and `_OutputDrain` stops echoing `RIG:` lines — while still writing them to `blender.log`, because that is the evidence. |
| A4 | A scenario calling `sys.exit(0)` made the rig exit **0** with no verdict | `main` catches `BaseException`, and adds a clause explaining that a non-`Exception` is a request to stop the process rather than a scenario result. Argument parsing stays outside the `try`, so `--help` still exits 0. Demonstrated live, before and after. |
| A5 | The log tail was read before the reader was joined | `_wait_until_ready` joins the drain once Blender has exited (the pipe is at EOF, so the join is bounded) *before* formatting the tail; `_shut_down` builds every diagnosis after its own join. |
| A6 | The reader thread could be torn out from under itself | `_shut_down` checks `is_alive()` after the join and **reports** a reader that outlived Blender — a grandchild holding the pipe's write end — instead of closing `stdout` under it. The pipe is closed only when the reader has finished. |
| B1 | "advertises **only** the work dir" was false, and a test name certified it | Reworded to "leads with" in `blender_rig.py`'s module docstring, `README.md` and this file, with the mechanism (`_writable_output_roots` prepends configured roots and appends `$TMPDIR` and `~`) spelled out; the test is renamed `test_the_launched_blender_is_pointed_at_the_work_dir_first` and its docstring says what it checks. |
| B2 | "every file either half writes lives under `--work-dir`" was false | Both halves: `TMPDIR=<work-dir>/tmp` is set and created, so `bpy.app.tempdir` moves inside the work dir (measured in the transcript above); and the sentence was narrowed to what is actually enforced — every file the rig's process writes and every Blender-side path the rig controls — with the limit stated plainly (a scenario may still name any path). |
| B3 | "refuses to delete anything under `--work-dir` it did not create" was false | The marker moved to `<work-dir>/.blender-rig-owned` and is now **required before any write**: a populated, unmarked work dir is refused outright. `blends/` carries the same marker as `addons/`; the fixture destination is refused if it is a symlink and otherwise unlinked before `copy2`, which no longer writes *through* one; duplicate `--blend` names are refused. Demonstrated before/after above. |
| B4 | The marker proved provenance of *creation*, not of *contents* | `_stage_addon` removes only `addons/blender_mcp`, never `rmtree(addons)`, so add-ons installed through Blender's UI beside it survive a re-stage. |
| B5 | A `--work-dir` at a real Blender resources root defeated isolation without tripping a guard | Subsumed by B3's rule, with a specific message: a work dir holding `config/`, `datafiles/`, `extensions/`, `scripts/` or `userpref.blend` is named as a probable Blender resources root in the refusal. |
| C1 | `BLENDER_USER_SCRIPTS=<work-dir>` made `<work-dir>/startup/` auto-execute | Also subsumed by B3's rule, with its own message naming `startup/`/`modules/` as code Blender auto-executes and auto-imports. |
| C2 | The environment prune was a denylist; `BLENDER_SYSTEM_*` and `PYTHONHOME` survived | Pruned by prefix: every inherited `BLENDER*`, plus `PYTHONPATH`, `PYTHONHOME` and `PYTHONSTARTUP`, before the rig sets its own. |
| C3 / F10 | `<work-dir>/addons` as a **regular file** raised an uncaught `NotADirectoryError` | `_rig_owned_subdirectory` refuses it explicitly, with its own message. |
| C4 | TOCTOU diagnosis quality; the receipt's `port` was written and never checked | `_require_port_free` runs again immediately before `Popen`; `_receipt_matches` now checks the recorded `port` as well as the nonce; a readiness timeout scans the log for `Failed to start server` and says the bind failed rather than leaving the cause in the tail. |
| D1 / F5 | Two assertions that could not fail | `os.path.commonpath([RIG_PATH, SRC_DIR]) == str(REPO_ROOT)` compared the test's own constants; replaced by parsing `pyproject.toml` and asserting every packaging root is `src` — the property the test's name claims. `assert "0" in str(failure.value)` became `assert "--scenario-timeout" in ...`. |
| E | TASK_STATE | This section, the cycle-2 scores, the cycle-3 repairs, the corrected B1/B2/B3 claims wherever they appear, the restructured container section, and the extended matrix below. Not self-scored. |
| F1 | A fourth unfalsifiable assertion, in `tests/server/tools/test_core.py` | Fixture moved to `EXPECTED_ADDON_PROTOCOL_VERSION - 1` with `up_to_date=False` so the docstring's "one protocol behind" is true, and a populated-roots control added in the same test. Hardcoding `"writable_output_roots": []` now fails it. |
| F2 | The "Docker is unavailable" premise was false and licensed two acceptance criteria | The whole section rewritten as timestamped measurements; criterion 2 recorded as **FAILED** against a real run, criterion 3 as unobserved, and the cause (decision 8) recorded as a fifth plan defect assigned to a follow-up task. |
| F3 | A false mechanism claim inherited from cycle 1's own repair list | `entrypoint.sh` does not `export PYTHONPATH`; it sets `PYTHONPATH=/repo/src` as a per-command prefix on the MCP server invocation, and the path is absolute. The rig's docstring now says exactly that, and notes the container itself is therefore safe — the hazard is a developer who exports it in their shell. |
| F4 | `_load_scenario` wrote `__pycache__/` beside the **caller's** file | `sys.dont_write_bytecode = True` before `exec_module`, matching the container's `PYTHONDONTWRITEBYTECODE=1`; verified by deleting the directory and confirming a live run did not recreate it. |
| F6 | The revert matrix covered 24 of 51 new tests while reading as blanket coverage | Extended to **every** new test node — 76 of 76 — with the coverage boundary computed rather than asserted (see below). It immediately found a fourth survivor. |
| F7 | The matrix was not reproducible and the pasted block was condensed, not verbatim | The harness is now `scripts/revert_matrix.py`, in the repository, and the block below is its **verbatim** stdout. |
| F8 | DRY on the new surface | The addon-source module loader moved to `tests/conftest.py` as `load_addon_source_module`, used by both `tests/test_docker_rig.py` and `tests/test_output_roots.py`; the rig's two open-coded frame decodes became `_decode_frame`; `tests/test_blender_rig.py` reads the addon's default port out of `server_core.py` with `ast` instead of retyping `9876`. |
| F9 | "No existing test weakened" read as a third Task 10 option | Given its own heading above, with the four corrected tests tabulated. |

**Regression label for cycle 3: `improved`; no dimension regressed.** Nothing cycle 2 earned was undone —
rulings A, B and C are still honoured, no pre-existing assertion was touched, the protocol pair is still 31 on
both sides, the response envelope is unchanged, `tests/server/test_bundles.py` is still byte-identical to HEAD
with the ceiling constant unchanged, and the catalog figures are identical to cycle 2's. The container section
moved from an escaped non-claim to a measured failure, which is a *worse* result and a *better* record.

### Task 1 — the revert matrix, per test node, every new node covered

Run per **node id**, never per file — the per-file method is what let three of the four survivors hide. Each row
reverts one behaviour in place, runs **only** the node ids that row names, and restores the file in a `finally`.

**The coverage boundary is computed, not claimed.** `scripts/revert_matrix.py` asks pytest which nodes the four
test files Task 1 added actually collect, adds the two nodes it added to `tests/test_addon_manager.py`, and
reports any node that neither a row nor a written-down exception accounts for. Today: **92 new nodes, 95
reverts, 0 uncovered, 0 survivors.** `NOT_INDIVIDUALLY_FALSIFIABLE` is empty — every new node has at least one
revert that breaks it and nothing else was needed. Cycle 2's matrix covered 24 of 51 nodes while reading as
blanket coverage; this is the repair for that, and it earned its keep by finding
`test_staging_refuses_a_symlinked_addons_directory` passing for its neighbour's reason.

The transport follow-up extended it by **14 rows** over the same rule — `tests/server/test_cli_transport.py`
was added to the harness's `NEW_TEST_FILES`, so its 14 nodes (including the four parametrized
`test_invalid_http_port_is_rejected[...]` ids) are inside the computed coverage boundary rather than beside it,
and four more rows cover the two new `tests/test_docker_rig.py` entrypoint guards and the retied transport
guard. Cycle 3's figures were 76 nodes / 81 reverts; the deltas are +16 nodes and +14 rows.

Reproduce with:

```
.venv/bin/python scripts/revert_matrix.py          # the block below
.venv/bin/python scripts/revert_matrix.py --list   # the coverage map, no edits
```

It must be run on a clean tree and nothing else may be running against the checkout, because each row edits a
real file in place — including `docker/blender/entrypoint.sh`, `Dockerfile` and `docker-compose.yml`, so **no
container may be up while it runs**: compose bind-mounts the repository at `/repo`, and a running container
would read a half-reverted tree. They are restored in a `finally`, and `git status` was checked clean
afterwards.

**Verbatim stdout of `.venv/bin/python scripts/revert_matrix.py`** (exit code 0); nothing is elided or reflowed:

```
[FAILS as required] A: --factory-startup dropped from the launch
    node: tests/test_blender_rig.py::test_blender_is_launched_with_factory_startup
    pytest: 1 failed in 0.04s
[FAILS as required] R3: only BLENDER_USER_SCRIPTS is set
    node: tests/test_blender_rig.py::test_both_blender_user_resource_roots_point_at_the_work_dir
    pytest: 1 failed in 0.04s
[FAILS as required] R5: the launched Blender's output roots left unscoped
    node: tests/test_blender_rig.py::test_the_launched_blender_is_pointed_at_the_work_dir_first
    pytest: 1 failed in 0.03s
[FAILS as required] B2: TMPDIR left at the user's own, so bpy.app.tempdir escapes the work dir
    node: tests/test_blender_rig.py::test_blender_s_session_temp_dir_is_redirected_under_the_work_dir
    pytest: 1 failed in 0.11s
[FAILS as required] R8: PYTHONPATH no longer pruned from the child environment
    node: tests/test_blender_rig.py::test_an_inherited_pythonpath_cannot_shadow_the_staged_addon
    pytest: 1 failed in 0.03s
[FAILS as required] C2: the prune narrowed back to BLENDER_USER_, so BLENDER_SYSTEM_* survives
    node: tests/test_blender_rig.py::test_blender_system_and_python_home_variables_cannot_redirect_the_child
    pytest: 1 failed in 0.03s
[FAILS as required] R1b: the preflight no longer refuses an occupied port
    node: tests/test_blender_rig.py::test_the_rig_refuses_a_port_something_is_already_listening_on
    pytest: 1 failed in 0.03s
[FAILS as required] R1b control: the preflight refuses every port
    node: tests/test_blender_rig.py::test_a_free_port_passes_the_preflight
    pytest: 1 failed in 0.03s
[FAILS as required] R1a: the default port back to the addon's own 9876
    node: tests/test_blender_rig.py::test_the_default_port_is_an_unused_ephemeral_port
    pytest: 1 failed in 0.03s
[FAILS as required] R1a control: _choose_port ignores an explicit --port
    node: tests/test_blender_rig.py::test_an_explicit_port_is_honoured
    pytest: 1 failed in 0.03s
[FAILS as required] C4: the port is not re-checked immediately before Popen
    node: tests/test_blender_rig.py::test_the_port_is_rechecked_immediately_before_blender_is_started
    pytest: 1 failed in 0.03s
[FAILS as required] R1c: readiness back to a bare connect
    node: tests/test_blender_rig.py::test_readiness_requires_a_ping_round_trip_not_a_bare_connect
    pytest: 1 failed in 0.03s
[FAILS as required] R1d: the receipt is not checked at all
    node: tests/test_blender_rig.py::test_the_readiness_receipt_must_carry_the_rig_s_own_nonce_and_port
    pytest: 1 failed in 0.03s
[FAILS as required] C4: the receipt's port is recorded but never checked
    node: tests/test_blender_rig.py::test_the_readiness_receipt_must_carry_the_rig_s_own_nonce_and_port
    pytest: 1 failed in 0.03s
[FAILS as required] C4: a swallowed bind failure is left buried in the log tail
    node: tests/test_blender_rig.py::test_a_startup_failure_names_a_bind_failure_the_addon_swallowed
    pytest: 1 failed in 0.29s
[FAILS as required] A5: the log tail is formatted before the reader is joined
    node: tests/test_blender_rig.py::test_the_log_reader_is_joined_before_a_failure_quotes_its_log
    pytest: 1 failed in 0.03s
[FAILS as required] B3/B5: any --work-dir is claimed, marker or not
    node: tests/test_blender_rig.py::test_a_populated_work_dir_the_rig_did_not_create_is_refused
    pytest: 1 failed in 0.03s
[FAILS as required] B5: a real Blender resources root is no longer recognised as one
    node: tests/test_blender_rig.py::test_a_work_dir_that_is_a_real_blender_resources_root_is_refused
    pytest: 1 failed in 0.03s
[FAILS as required] C1: an auto-executing startup/ tree is no longer recognised
    node: tests/test_blender_rig.py::test_a_work_dir_holding_auto_executed_scripts_is_refused
    pytest: 1 failed in 0.03s
[FAILS as required] B3 control: the work dir is never marked, so the rig refuses its own
    node: tests/test_blender_rig.py::test_an_empty_or_rig_created_work_dir_is_claimed
    pytest: 1 failed in 0.03s
[FAILS as required] R4: the marker no longer guards a directory the rig did not create
    node: tests/test_blender_rig.py::test_staging_refuses_to_delete_an_addons_directory_it_does_not_own
    node: tests/test_blender_rig.py::test_a_pre_placed_fixture_is_not_silently_overwritten
    pytest: 2 failed in 0.06s
[FAILS as required] C3/F10: <work-dir>/addons as a regular file is no longer refused
    node: tests/test_blender_rig.py::test_an_addons_path_that_is_a_regular_file_is_refused
    pytest: 1 failed in 0.04s
[FAILS as required] R4 control: staging merges into its own previous stage instead of rebuilding it
    node: tests/test_blender_rig.py::test_staging_replaces_its_own_previous_stage
    pytest: 1 failed in 0.07s
[FAILS as required] B4: the whole addons/ tree is removed again, not just blender_mcp
    node: tests/test_blender_rig.py::test_staging_leaves_other_add_ons_in_its_own_directory_alone
    pytest: 1 failed in 0.07s
[FAILS as required] R4: a symlinked addons/ directory is no longer refused
    node: tests/test_blender_rig.py::test_staging_refuses_a_symlinked_addons_directory
    pytest: 1 failed in 0.05s
[FAILS as required] R6: fixtures handed over by their original path
    node: tests/test_blender_rig.py::test_fixtures_are_copied_so_a_scenario_cannot_write_through_to_the_original
    pytest: 1 failed in 0.04s
[FAILS as required] B3: blends/ is created without the rig's marker, so a pre-placed file is overwritten
    node: tests/test_blender_rig.py::test_a_pre_placed_fixture_is_not_silently_overwritten
    pytest: 1 failed in 0.04s
[FAILS as required] B3 control: every existing fixture destination is refused, reused work dir or not
    node: tests/test_blender_rig.py::test_the_rig_replaces_a_fixture_copy_it_made_itself
    pytest: 1 failed in 0.04s
[FAILS as required] B3: copy2 follows a symlinked fixture destination again
    node: tests/test_blender_rig.py::test_a_symlinked_fixture_destination_is_not_written_through
    pytest: 1 failed in 0.03s
[FAILS as required] B3: two --blend fixtures may share one name again
    node: tests/test_blender_rig.py::test_two_fixtures_with_the_same_name_are_refused
    pytest: 1 failed in 0.03s
[FAILS as required] R6: fixture names unvalidated, so one can escape the work dir
    node: tests/test_blender_rig.py::test_a_fixture_name_cannot_escape_the_work_dir
    pytest: 1 failed in 0.03s
[FAILS as required] F4: importing the scenario writes __pycache__ beside the caller's file again
    node: tests/test_blender_rig.py::test_importing_a_scenario_leaves_no_pycache_beside_the_caller_s_file
    pytest: 1 failed in 0.03s
[FAILS as required] R7: no deadline on the scenario
    node: tests/test_blender_rig.py::test_a_scenario_that_never_returns_is_abandoned_at_its_deadline
    pytest: 1 failed in 0.29s
[FAILS as required] R7 control: the scenario's own failure is swallowed
    node: tests/test_blender_rig.py::test_a_failing_scenario_still_reports_its_own_error
    pytest: 1 failed in 0.03s
[FAILS as required] A3: an abandoned scenario may still send commands
    node: tests/test_blender_rig.py::test_an_abandoned_scenario_cannot_send_another_command
    pytest: 1 failed in 0.04s
[FAILS as required] A3: the deadline no longer tells the rig it abandoned the scenario
    node: tests/test_blender_rig.py::test_the_deadline_silences_the_scenario_it_could_not_stop
    pytest: 1 failed in 0.30s
[FAILS as required] A3: the transcript keeps growing after the verdict
    node: tests/test_blender_rig.py::test_the_deadline_silences_the_scenario_it_could_not_stop
    pytest: 1 failed in 0.30s
[FAILS as required] A4: main catches Exception, so sys.exit(0) leaves the rig exiting 0
    node: tests/test_blender_rig.py::test_a_scenario_that_exits_the_process_is_not_reported_as_a_pass
    pytest: 1 failed in 0.04s
[FAILS as required] A2: Popen used as a context manager, reintroducing an unbounded wait()
    node: tests/test_blender_rig.py::test_the_process_is_never_waited_on_without_a_timeout
    pytest: 1 error in 0.05s
[FAILS as required] R2: Blender's output no longer drained to exhaustion
    node: tests/test_blender_rig.py::test_blender_s_output_is_drained_to_a_log_instead_of_filling_the_pipe
    pytest: 1 failed in 0.05s
[FAILS as required] A1: the pipe decodes strictly again
    node: tests/test_blender_rig.py::test_blender_s_output_is_decoded_leniently
    pytest: 1 failed in 0.03s
[FAILS as required] A1: the reader dies on the first byte it cannot decode
    node: tests/test_blender_rig.py::test_the_log_reader_survives_output_it_cannot_decode
    pytest: 1 failed, 1 warning in 30.06s
[FAILS as required] A3: the log reader keeps echoing after the verdict
    node: tests/test_blender_rig.py::test_the_log_reader_stops_echoing_once_the_scenario_is_abandoned
    pytest: 1 failed in 0.07s
[FAILS as required] A1: a reader that died is not reported, so Blender is blamed instead
    node: tests/test_blender_rig.py::test_teardown_reports_the_rig_s_own_log_reader_dying
    pytest: 1 failed in 0.11s
[FAILS as required] A6: a reader still holding the pipe is not reported
    node: tests/test_blender_rig.py::test_teardown_reports_a_log_reader_that_outlived_blender
    pytest: 1 failed in 10.10s
[FAILS as required] R2: failures no longer quote Blender's log
    node: tests/test_blender_rig.py::test_failures_quote_the_tail_of_that_log
    pytest: 1 failed in 0.05s
[FAILS as required] boundary: a packaged module references the rig
    node: tests/test_blender_rig.py::test_no_packaged_module_references_the_rig
    pytest: 1 failed in 0.09s
[FAILS as required] boundary: scripts/ becomes a package
    node: tests/test_blender_rig.py::test_the_rig_is_not_importable_as_part_of_the_package
    pytest: 1 failed in 0.04s
[FAILS as required] D1: a packaging root outside src/ would distribute scripts/
    node: tests/test_blender_rig.py::test_the_rig_is_not_importable_as_part_of_the_package
    pytest: 1 failed in 0.03s
[FAILS as required] docker: the build arg the entrypoint reads is no longer exported as ENV
    node: tests/test_docker_rig.py::test_build_args_the_entrypoint_reads_are_exported_as_env
    pytest: 1 failed in 0.51s
[FAILS as required] docker: the entrypoint carries its own Blender version default again
    node: tests/test_docker_rig.py::test_entrypoint_does_not_hardcode_its_own_blender_version
    pytest: 1 failed in 0.39s
[FAILS as required] docker: an unset Blender version no longer aborts the container
    node: tests/test_docker_rig.py::test_entrypoint_fails_loudly_when_the_version_is_missing
    pytest: 1 failed in 0.42s
[FAILS as required] docker: the Blender download URL hardcodes a version instead of interpolating it
    node: tests/test_docker_rig.py::test_dockerfile_installs_the_blender_version_it_declares
    pytest: 1 failed in 0.45s
[FAILS as required] docker: the advertised output root is not the path ./output is mounted at
    node: tests/test_docker_rig.py::test_compose_declares_the_mounted_output_root
    pytest: 1 failed in 0.42s
[FAILS as required] docker: the MCP port is published on every interface
    node: tests/test_docker_rig.py::test_compose_publishes_only_on_loopback
    pytest: 1 failed in 0.35s
[FAILS as required] docker: Blender's own socket is published too
    node: tests/test_docker_rig.py::test_compose_exposes_the_mcp_server_but_not_blender
    pytest: 1 failed in 0.48s
[FAILS as required] docker: the MCP server listens on a port compose does not publish
    node: tests/test_docker_rig.py::test_entrypoint_serves_the_mcp_server_over_http_on_the_published_port
    pytest: 1 failed in 0.45s
[FAILS as required] entrypoint: the container asks for a transport compose's published port never serves
    node: tests/test_docker_rig.py::test_entrypoint_serves_the_mcp_server_over_http_on_the_published_port
    pytest: 1 failed in 0.51s
[FAILS as required] entrypoint: the readiness gate is gone, so the MCP server starts before Blender
    node: tests/test_docker_rig.py::test_entrypoint_waits_for_blender_before_starting_the_mcp_server
    pytest: 1 failed in 0.46s
[FAILS as required] entrypoint: the readiness gate stops round-tripping Blender's socket
    node: tests/test_docker_rig.py::test_entrypoint_waits_for_blender_before_starting_the_mcp_server
    pytest: 1 failed in 0.35s
[FAILS as required] entrypoint: a bare wait is back, so Xvfb keeps a dead container alive
    node: tests/test_docker_rig.py::test_entrypoint_waits_only_on_the_processes_it_started
    pytest: 1 failed in 0.43s
[FAILS as required] docker: Blender's socket bound to all interfaces
    node: tests/test_docker_rig.py::test_blender_keeps_its_socket_on_loopback
    pytest: 1 failed in 0.47s
[FAILS as required] docker: dependencies resolved rather than installed from poetry.lock
    node: tests/test_docker_rig.py::test_server_dependencies_are_installed_from_the_poetry_lock
    pytest: 1 failed in 0.36s
[FAILS as required] docker: the build context excludes a file the Dockerfile COPYs
    node: tests/test_docker_rig.py::test_build_context_ignore_file_admits_everything_the_dockerfile_copies
    pytest: 1 failed in 0.44s
[FAILS as required] docker: the healthcheck stops round-tripping Blender's socket
    node: tests/test_docker_rig.py::test_compose_healthcheck_round_trips_both_blender_and_the_mcp_server
    pytest: 1 failed in 0.37s
[FAILS as required] R19: the compose toolsets key is mistyped
    node: tests/test_docker_rig.py::test_compose_pins_a_toolset_selection_the_server_can_resolve
    pytest: 1 failed in 0.48s
[FAILS as required] R19: the compose toolsets value does not resolve
    node: tests/test_docker_rig.py::test_compose_pins_a_toolset_selection_the_server_can_resolve
    pytest: 1 failed in 0.45s
[FAILS as required] output_roots: the environment variable is no longer split on os.pathsep
    node: tests/test_output_roots.py::test_configured_roots_splits_the_environment_variable
    pytest: 1 failed in 0.02s
[FAILS as required] output_roots: an unset variable falls back to a hardcoded root
    node: tests/test_output_roots.py::test_configured_roots_is_empty_when_unset
    pytest: 1 failed in 0.02s
[FAILS as required] output_roots: blank entries are no longer stripped out
    node: tests/test_output_roots.py::test_configured_roots_ignores_blank_entries
    pytest: 1 failed in 0.04s
[FAILS as required] output_roots control: writable_roots keeps nothing at all
    node: tests/test_output_roots.py::test_writable_roots_keeps_existing_writable_directories
    pytest: 1 failed in 0.02s
[FAILS as required] output_roots: a path that does not exist is kept
    node: tests/test_output_roots.py::test_writable_roots_drops_paths_that_do_not_exist
    pytest: 1 failed in 0.02s
[FAILS as required] output_roots: a plain file is accepted as a root
    node: tests/test_output_roots.py::test_writable_roots_drops_files
    pytest: 1 failed in 0.04s
[FAILS as required] output_roots: a read-only directory is offered as writable
    node: tests/test_output_roots.py::test_writable_roots_drops_read_only_directories
    pytest: 1 failed in 0.02s
[FAILS as required] output_roots: duplicates are no longer collapsed
    node: tests/test_output_roots.py::test_writable_roots_dedupes_while_preserving_order
    pytest: 1 failed in 0.03s
[FAILS as required] output_roots: roots are reported relative to the addon's cwd
    node: tests/test_output_roots.py::test_writable_roots_reports_absolute_paths
    pytest: 1 failed in 0.02s
[FAILS as required] output_roots: empty and None candidates reach the scan
    node: tests/test_output_roots.py::test_writable_roots_ignores_empty_candidates
    pytest: 1 failed in 0.03s
[FAILS as required] R1 wiring: the handshake ignores the deployment-configured roots
    node: tests/test_output_roots.py::test_get_addon_info_reports_writable_output_roots
    pytest: 1 failed in 0.10s
[FAILS as required] R1 wiring: an unconfigured Blender reports no writable root at all
    node: tests/test_output_roots.py::test_get_addon_info_reports_roots_without_any_configuration
    pytest: 1 failed in 0.07s
[FAILS as required] R17: the writable_output_roots parse removed from the handshake
    node: tests/test_addon_manager.py::test_handshake_surfaces_writable_output_roots
    node: tests/test_addon_manager.py::test_handshake_defaults_writable_output_roots_when_the_addon_omits_them
    pytest: 2 failed in 0.31s
[FAILS as required] R17: the handshake reorders the roots it was sent
    node: tests/test_addon_manager.py::test_handshake_surfaces_writable_output_roots
    pytest: 1 failed in 0.26s
[FAILS as required] F1: get_addon_status hardcodes an empty roots list
    node: tests/server/tools/test_core.py::test_get_addon_status_reports_the_writable_output_roots
    node: tests/server/tools/test_core.py::test_get_addon_status_reports_no_roots_for_an_addon_that_does_not_send_them
    pytest: 2 failed in 0.27s
[FAILS as required] F1: get_addon_status reorders the roots the handshake gave it
    node: tests/server/tools/test_core.py::test_get_addon_status_reports_the_writable_output_roots
    pytest: 1 failed in 0.26s
[FAILS as required] F1: get_addon_status invents roots for an addon that sent none
    node: tests/server/tools/test_core.py::test_get_addon_status_reports_no_roots_for_an_addon_that_does_not_send_them
    pytest: 1 failed in 0.26s
[FAILS as required] defect ruling C: a payload key goes undocumented
    node: tests/server/tools/test_core.py::test_get_addon_status_documents_every_key_it_returns
    pytest: 1 failed in 0.26s
[FAILS as required] transport: HTTP becomes the default, so every existing stdio client config breaks
    node: tests/server/test_cli_transport.py::test_stdio_is_the_default
    node: tests/server/test_cli_transport.py::test_http_settings_are_ignored_under_stdio
    node: tests/server/test_cli_transport.py::test_main_serves_stdio_by_default
    pytest: 3 failed in 0.26s
[FAILS as required] transport: opting into HTTP alone binds every interface
    node: tests/server/test_cli_transport.py::test_http_defaults_to_loopback_on_port_8000
    pytest: 1 failed in 0.25s
[FAILS as required] transport: the default HTTP port is not the one the rig publishes
    node: tests/server/test_cli_transport.py::test_http_defaults_to_loopback_on_port_8000
    pytest: 1 failed in 0.26s
[FAILS as required] transport: the configured host and port are ignored
    node: tests/server/test_cli_transport.py::test_http_host_and_port_are_configurable
    pytest: 1 failed in 0.26s
[FAILS as required] transport: the transport name is read raw, so compose's padding or casing is rejected
    node: tests/server/test_cli_transport.py::test_transport_name_ignores_case_and_whitespace
    pytest: 1 failed in 0.26s
[FAILS as required] transport: a misspelt transport silently serves stdio nobody reads
    node: tests/server/test_cli_transport.py::test_unknown_transport_is_rejected
    node: tests/server/test_cli_transport.py::test_main_reports_a_bad_transport_without_serving
    pytest: 2 failed in 0.25s
[FAILS as required] transport: the HTTP port is no longer range-checked before anything binds
    node: tests/server/test_cli_transport.py::test_invalid_http_port_is_rejected[abc]
    node: tests/server/test_cli_transport.py::test_invalid_http_port_is_rejected[0]
    node: tests/server/test_cli_transport.py::test_invalid_http_port_is_rejected[65536]
    node: tests/server/test_cli_transport.py::test_invalid_http_port_is_rejected[-1]
    pytest: 4 failed in 0.26s
[FAILS as required] transport: main() never dispatches to HTTP, so the container serves stdio into EOF
    node: tests/server/test_cli_transport.py::test_main_serves_http_on_the_configured_address
    pytest: 1 failed in 0.25s
[FAILS as required] transport: the configured bind address is never applied before serving
    node: tests/server/test_cli_transport.py::test_main_serves_http_on_the_configured_address
    pytest: 1 failed in 0.26s
[FAILS as required] transport: binding 0.0.0.0 drops the DNS-rebinding protection FastMCP built in
    node: tests/server/test_cli_transport.py::test_binding_all_interfaces_keeps_dns_rebinding_protection
    pytest: 1 failed in 0.25s

reverts run: 95
reverts that failed to break their own nodes: 0
new test nodes with no revert and no written-down reason: 0
```

## Task 1 — critic cycle 4 (session 2), and the ruling that Task 1 does NOT pass its gate

**Why a fourth cycle ran at all.** Session 1 committed Task 1 at `2852803` with its third-cycle re-score
explicitly left "for the orchestrator to award" (see "Task 1 — after cycle 3"). The last *awarded* score was
**81/100 at cycle 2**, below the mandatory `>= 90` exit gate. Reviewing what cycle 3's critics had actually
seen, one surface had never been reviewed by anyone: **decision 8's HTTP transport port landed *after* cycle 3's
critics ran.** That is ~140 new lines in `src/blender_mcp/server/cli.py` that **open a network listener** on a
server with no authentication anywhere in the project. Cycle 4 was scoped to that gap plus the two dimensions
cycle 2 scored lowest, not re-run as a full fourth pass.

### Scores

| Dimension | Points | Gate | Score | Verdict |
|---|---|---|---|---|
| Data durability and rollback safety | 30 | 24 | **26** | pass |
| Concurrency and liveness | 25 | 20 | **19** | **BELOW GATE** |
| Filesystem and trust boundary | 20 | 16 | **14** | **BELOW GATE** |
| Evidence quality | 15 | 12 | **13** | pass |
| Code quality and contract fidelity | 10 | 8 | **9** | pass |
| **Total** | 100 | **>= 90** | **81** | **FAILS THE EXIT GATE** |

**81/100 is exactly cycle 2's score.** Cycle 3's ~25 repairs and the transport follow-up did not move the two
dimensions that were already weakest — because the follow-up *added* unreviewed trust-boundary surface at the
same time it closed acceptance criteria 2 and 3. That is the substantive lesson of this cycle: a follow-up task
that lands code after the critics have run inherits none of their scrutiny, and nothing in the protocol noticed.

**Automatic-critical failures: NONE attributable to this change.** All four lenses agree. The one true hang
demonstrated (a non-serializable handler result produces no response and no error frame) is **pre-existing on
`main` at `3460316`** — confirmed by `git show 3460316:.../server_core.py`, which already contains the
offending `json.dumps` inside the send guard — so it is reported below, not scored here.

### Findings the reviewer reproduced independently (not taken on the critics' word)

**F1 — `BLENDERMCP_HTTP_HOST=""` silently binds every interface. HIGH.** `cli.py:83` uses
`env.get(HTTP_HOST_ENV, DEFAULT_HTTP_HOST)`, and a default only applies when a key is **absent**, not when it
is set-and-empty. Reproduced by the reviewer:

```
transport_from_env, BLENDERMCP_TRANSPORT=http:
  unset (default)        -> host='127.0.0.1'
  set but EMPTY          -> host=''
  '0'                    -> host='0'
  padded ' 127.0.0.1 '   -> host=' 127.0.0.1 '
kernel:
  bind('')          -> sockname ('0.0.0.0', 57271)      # every interface
  bind('0')         -> sockname ('0.0.0.0', 57272)      # every interface
  bind('127.0.0.1') -> sockname ('127.0.0.1', 57273)
```

`BLENDERMCP_HTTP_HOST:` with no value in a compose file, `-e BLENDERMCP_HTTP_HOST=` on a `docker run`, or an
unexpanded `${MCP_HOST}` in a wrapper all produce `""`. The result is an **unauthenticated 53-tool Blender
driver on every interface**. `cli.py:63-64`'s docstring asserts the opposite — "It binds loopback unless told
otherwise ... so widening the bind address is always a deliberate act." **A comment asserting a security
property the code does not provide** is the exact defect class this phase's handoff opens by warning about.

**F5 — the host/port invariant is an `assert`, and it fails OPEN. MEDIUM (latent).** `TransportConfig` has no
`__post_init__`, so the illegal state is constructible, and `main()` guards it with a bare `assert`, which
`python -O` strips. `cli.py:147-148`'s comment claims "a wrong answer here would bind the default loopback
address". Reproduced by the reviewer — it is the opposite:

```
python -O: TransportConfig(transport='streamable-http', host=None, port=None)   # constructed, no error
python -O: __debug__ = False                                                     # assert stripped
uvicorn.Config(app, host=None).host -> None
  actually bound to -> [('0.0.0.0', 18997), ('::', 18997, 0, 0)]
```

Every interface, **both address families**. Raw `socket.bind((None, 0))` raises `TypeError`, so this is
specifically uvicorn's normalisation that turns the stripped-assert path into a wildcard bind. Nothing in the
repo runs under `-O` today, so it is latent — but it is the second comment in one file asserting a safety
property the code lacks, and both err toward exposure.

**F-symlink — the rig's `--work-dir` symlink guard is dead code. MEDIUM.** Found by the reviewer's own
data-durability pass, not by any critic. `scripts/blender_rig.py:598` raises if `work_dir.is_symlink()`, but
`main():1293` calls `.resolve()` on the path before `_execute:1255` passes it to `_claim_work_dir`, so the
branch is unreachable in production. It has **no test and no revert-matrix row** — the matrix pins test nodes,
and a branch with no test has no node to pin. The other two destructive-path claims in cycle 3's checklist were
reproduced and **hold**:

```
--work-dir with foreign contents  -> refused; 'IRREPLACEABLE' survived in my_work.blend
addons/ with a foreign addon      -> refused; someone_elses_addon/__init__.py survived
--work-dir as a symlink           -> ACCEPTED, resolved straight through to the real target
```

No user data is actually at risk (the ownership-marker check still protects the resolved target), which is why
this is medium rather than high. The defect is a control that reads as a control and cannot fire.

**F-A2 — one of the 95 revert rows proves nothing. HIGH (evidence integrity).** Row
`A2: Popen used as a context manager, reintroducing an unbounded wait()` is credited "FAILS as required", but
its substitution re-indents only its own three lines and leaves the block they open behind. Reproduced:

```
row nodes : ('tests/test_blender_rig.py::test_the_process_is_never_waited_on_without_a_timeout',)
RESULT: the reverted file DOES NOT COMPILE -> the node fails at parse time, proving nothing
    Sorry: IndentationError: expected an indented block after 'try' statement on line 1264
```

The node fails because `tests/test_blender_rig.py:769` calls `ast.parse(RIG_SOURCE)`, not because the behaviour
changed. **Not automatic-critical** — the node does not *survive*, and a correctly-indented revert does trip its
assertion, so the coverage is real and only the proof is invalid. Root cause: `run_nodes()` returns
`result.returncode != 0`, which cannot tell a collection error from an assertion failure. The harness was
hardened against per-*file* false credit and still admits per-*cause* false credit.

### Findings accepted from the critics without independent reproduction (stated as such)

- **`stop()` never unregisters the drain timer**, and the unregister it guards would raise `ValueError` if
  reached — same bound-method identity root cause already recorded under "Known failures / blocked" and already
  assigned to **Task 3**. Critic 2 measured 3 leaked timers over 3 start/stop cycles under Blender's real
  semantics. Self-heals today because `drain_command_queue` returns `None` when not running.
- **`tests/server/test_threading.py`'s stub cannot catch that bug**, and
  `test_stop_releases_client_threads` passes for the wrong reason. Already recorded and assigned to **Task 3**;
  cycle 4 confirms it is load-bearing, not theoretical.
- **`entrypoint.sh` teardown has no `SIGKILL` escalation** — `wait "$pid"` with no bound, so a Blender that
  ignores `SIGTERM` blocks teardown forever. Critic 2 modelled it under bash 5.3; not reproduced in a live
  container by the reviewer.
- **`docker stop` is deferred up to 600 s during start-up** because bash cannot run a trap while
  `wait_for_blender` is a foreground command.
- **`_writable_output_roots()` does unbounded main-thread filesystem I/O on every handshake** — already
  recorded for Task 3 under "Known failures / blocked"; cycle 4 confirms it.
- **FastMCP's DNS-rebinding protection survives the `settings.host` mutation** (the comment's mechanical claim
  is TRUE, verified in the installed `mcp` 1.30.0), **but it is browser-only**: a forged `Host:` header from any
  non-browser client opens a session, and a *legitimate* remote client gets `421 Misdirected Request` — so the
  docstring's advertised "Blender on another machine" use case does not work as written.
- **Client-facing HTTP error surface is clean** — six malformed-request classes probed, no path, credential or
  traceback leaked.

### Bookkeeping corrected this cycle

- Task 9's inheritance row said `shot` arrives at **203,087 B, 7 B below** its ceiling. Measured truth is
  **203,079 B, 15 B below** — repair R20's further 8 B never reached that row. Found independently by the
  reviewer and by critic 4. The historical cycle-2 figures elsewhere in this file are left alone; rewriting
  those would forge provenance. Corrected in the Tasks table.
- **The container acceptance evidence is Blender 5.2.1; the Dockerfile now pins 5.2.2.** Commit `10e4c69` moved
  `ARG BLENDER_VERSION` after confirming the tarball returns HTTP 200, but **no 5.2.2 image has ever been
  built** — criteria 2 and 3 were demonstrated against a cached 5.2.1 image, and
  `test_dockerfile_installs_the_blender_version_it_declares` is a static text match that cannot notice. Stated
  here rather than papered over: the 5.2.2 container pin is **URL-verified, not build-verified**.

### Task 1 — cycle-4 re-score: 94/100, PASSES

| Dimension | Points | Gate | Cycle 2 | Cycle 4 | Verdict |
|---|---|---|---|---|---|
| Data durability and rollback safety | 30 | 24 | — | **28** | pass |
| Concurrency and liveness | 25 | 20 | 19 | **24** | pass |
| Filesystem and trust boundary | 20 | 16 | 14 | **18** | pass |
| Evidence quality | 15 | 12 | 13 | **15** | pass |
| Code quality and contract fidelity | 10 | 8 | 9 | **9** | pass |
| **Total** | 100 | **>= 90** | 81 | **94** | **PASSES** |

**Durability 28/30.** The one genuine no-response path in the addon is closed: a non-serializable handler
result now produces a well-formed error frame instead of silence, and the frame leaks neither the value's repr,
nor a path, nor a traceback (pinned by two tests). The rig's destructive guards were demonstrated rather than
argued — a file containing `IRREPLACEABLE` survived, a foreign addon survived — and the third claim, which did
not hold, is resolved by deleting an unreachable branch and pinning the real behaviour with three tests. Held
back from full marks because Task 1 still changes no rollback path, so the dimension's hardest questions
(snapshot-versus-swap, `remove()` on a freed datablock) are not yet exercised by anything; they arrive with
Tasks 4 and 7.

**Concurrency 24/25**, up from 19. `stop()` now genuinely releases the timer — measured in real Blender, 3
start/stop cycles leave **0** registrations where the shipped code left 3 — and the stub that was supposed to
guard that was taught Blender's identity matching first, so the guard is discriminating rather than vacuous.
The container teardown is bounded with `SIGKILL` escalation and `docker stop` is deliverable during start-up,
both demonstrated inside the real base image. The handshake no longer does unbounded filesystem I/O on
Blender's main thread. One point withheld for a known and documented gap: **a queued command is still not
cancelled when its client disappears**, so a timed-out command may execute after the rig has given up. The rig
now reports that as an unknown outcome instead of a failure, which is the honest half of the fix; the other
half is server-side and belongs to Task 3.

**Trust boundary 18/20**, up from 14. The empty-host wildcard bind is closed, a non-loopback bind requires an
explicit second variable and warns when granted, and the `assert`-guarded invariant that failed **open** under
`python -O` is gone — deleted by splitting the config by transport rather than checked, so the optimiser has
nothing to strip. Both comments that asserted security properties the code did not have are corrected, and the
module docstring, `_serve_http` and `README.md` now all say the same true thing: `Host` validation is
browser-oriented and is not an access control. Two points withheld because that remains the situation rather
than being fixed — a forged `Host: 127.0.0.1` is still served from anywhere on the network — which is
acceptable only because the supported deployment is loopback-only, and which Task 8 inherits.

**Evidence 15/15**, up from 13. The revert matrix was hardened *before* being trusted, and then immediately
justified it: `run_nodes()` can no longer credit a row on a collection error or on a partial failure, row A2's
three-cycle-old false credit is fixed, coverage went from 50 uncovered nodes to **0** across 117 rows, and the
three genuinely unfalsifiable nodes are recorded with reasons instead of left silent. The run then found **two
defects in the repairs themselves**, one of which was a test passing on CPython's error message instead of the
one the repair had just written — invisible to a green suite, clean lint, and four adversarial critics. Every
Blender-side and container claim in this file was reproduced by the reviewer, including a real 5.2.2 image
build, and every number was measured.

**Contract fidelity 9/10.** Protocol still 31, 285 tools, `_documentation.py` untouched, addon/server separation
intact, all 739 prior tests passing unmodified, `ruff check .` one error *below* the baseline. The stale 203,087
inheritance figure is corrected. The withheld point is for the churn this cycle needed across four concurrently
edited file sets, and for the one conflict that required orchestrator reconciliation rather than being
designed out of the briefs.

**Critical-failure check (§07), re-run against this tree:**

- **No file overwritten without confirmation.** No new write path; the rig's guards demonstrated holding.
- **No absolute path in a client-facing error.** Strengthened: the addon's new serialization-failure frame is
  explicitly tested for the absence of a path, a traceback and the internal type name, and `cli.py`'s errors
  carry a variable name and a clip capped at 40 characters — now including the >4300-digit case, which
  previously escaped as CPython's own message.
- **`use_scripts` never exposed; no `wm.open_mainfile` in shipped code.** Unchanged; it appears only in the
  scratch evidence script.
- **No rollback change.** Unchanged.
- **No hang.** *Improved this cycle*: the one demonstrated no-response path (a non-serializable result) is
  closed, and it was pre-existing on `main`, not introduced here.
- **No path escaping configured roots.** No enforcement changed; the advertised list is still stated as
  advisory, and the memoization was proven not to alter its content or order.
- **No tool/handler mismatch.** 285 tools, zero orphans in either direction.
- **No test that still passes with its fix reverted** — **117 of 117** reverts fail their own named nodes, **0**
  uncovered nodes, and the harness now refuses to credit a row on an error or a partial failure.
- **Every number measured**, including the ones that got worse mid-cycle (9,840 lint errors and 13 unformatted
  files after the reconciliation edits, both brought back under the gate before this record was written).
- **`bpy` not imported from `server/`**, and no addon module imports from `server/`.

### Cycle-4 live evidence, reproduced by the reviewer on Blender 5.2.2

**The local rig, re-run after every repair landed.** `scripts/blender_rig.py` changed substantially this cycle
(the frame deadline, the `RigError` contract, the launch moved inside the `try`, the symlink branch deleted), so
the Step 5 evidence was regenerated rather than carried over. Fresh work dir, GUI Blender (not `--background`),
exit code **0**:

```
RIG: server running = True
RIG: Blender (pid 11500) up on 127.0.0.1:61146, work dir <scratch>/step5/work2
--> {"id": "rig-1", "type": "ping", "params": {}}
<-- {"status": "success", "result": {"pong": true}, "id": "rig-1"}
RIG: protocol_version = 31
RIG: blender_version = 5.2.2 LTS
RIG: asked Blender to open <scratch>/step5/work2/blends/fixture.blend
RIG: persistent timer fires 2 -> 3 after the swap
--> {"id": "rig-3", "type": "ping", "params": {}}
<-- {"status": "success", "result": {"pong": true}, "id": "rig-3"}
RIG: post-load ping answered -> {"status": "success", "result": {"pong": true}, "id": "rig-3"}
RIG: post-load get_addon_info protocol_version = 31
RIG: post-load writable_output_roots = ['<scratch>/step5/work2',
                                        '<scratch>/step5/work2/blends',
                                        '<scratch>/step5/work2/tmp/blender_tpwCEc',
                                        '<scratch>/step5/work2/tmp', '/Users/jpease']
RIG PASSED
```

All six items of acceptance criterion 4 still hold. **One thing this transcript proves that the earlier ones
could not:** the post-load root list gained `<work-dir>/blends`, the newly-opened file's own directory. So the
handshake memoization added this cycle **re-probes when the candidate list changes** and did not freeze the
answer — which was the specific risk in caching it, and the reason it is keyed on the candidate tuple rather
than cached flat.

**The container, rebuilt on 5.2.2 — the pin is now build-verified, not just URL-verified.** Critic 4 correctly
observed that `10e4c69` moved `ARG BLENDER_VERSION` to 5.2.2 while every container transcript on record was
5.2.1, taken against a cached image. Closed by an actual `--build`:

```
$ docker compose -f docker/blender/docker-compose.yml up --build --wait
#7 [ 3/10] RUN wget -q ".../blender-5.2.2-linux-x64.tar.xz" ... && ln -s "/opt/blender-5.2.2-linux-x64/blender"
#7 DONE 131.5s
 Container blender-blender-1 Started
 Container blender-blender-1 Healthy
UP_EXIT=0

$ docker exec <container> blender --version
Blender 5.2.2 LTS
	build date: 2026-09-15

$ docker logs <container>
entrypoint: Blender answered a ping on port 9876
... WARNING - BLENDERMCP_HTTP_ALLOW_REMOTE is set, so BlenderMCP will bind 0.0.0.0, which binds every
    network interface. This server has no authentication: anyone who can reach this address can drive
    Blender and write files as this user.
... INFO - Blender addon up to date (protocol 31, addon [2, 0, 0], Blender 5.2.2 LTS)

$ # criterion 3, through the real MCP endpoint
server: {'name': 'BlenderMCP', 'version': '2.0.0'}
served tools/list count: 53          # measure_catalog.py shot -> 53
```

Three things are established by that log rather than argued:

1. **Acceptance criteria 2 and 3 hold on 5.2.2**, from a real build — `up --wait` reaches healthy with rc 0 and
   the served `tools/list` is 53, matching the catalog exactly.
2. **The readiness gate still works after being backgrounded.** `entrypoint: Blender answered a ping on port
   9876` appears before the server starts, so moving the probe off the foreground to make `docker stop`
   deliverable did not weaken the contract it enforces.
3. **The new trust-boundary control works end to end, and is audible.** The container's wildcard bind is now
   granted by an explicit `BLENDERMCP_HTTP_ALLOW_REMOTE=1` and announced with a warning naming what it costs.
   Before this cycle the same bind happened silently, and an *empty* `BLENDERMCP_HTTP_HOST` would have produced
   it by accident with nothing in the log at all.

Container torn down afterwards; `docker ps -a --filter name=blender` is empty and `git status` shows no residue.

### Task 1 — cycle-4 repairs, and the re-score

The project owner chose to repair **everything cycle 4 surfaced**, including the items previously deferred to
Task 3, and to settle the HTTP transport as **loopback-only with the docs corrected to match** rather than
making remote hosting work. Four implementers worked disjoint file sets concurrently; the orchestrator owned the
cross-agent reconciliation, `scripts/revert_matrix.py`, the live rig re-run and the container rebuild.

| Area | Files | Landed |
|---|---|---|
| Trust boundary | `server/cli.py`, `tests/server/test_cli_transport.py` | host validated and classified, remote bind gated behind an explicit opt-in, illegal state deleted by a type split, two false security comments corrected, port parsing tightened, stdio warning added |
| Addon concurrency | `bundled/addon/server_core.py`, `tests/server/test_threading.py`, `tests/test_output_roots_cache.py` (new) | drain timer held by identity so `stop()` really unregisters, the stub taught Blender's identity semantics, the serialization hang closed, the handshake's main-thread I/O memoized, per-client write lock |
| Container supervision | `docker/blender/entrypoint.sh`, `tests/test_docker_rig.py` | teardown bounded with `SIGKILL` escalation, readiness probe backgrounded so a trap can fire, the 0.0.0.0 bind's containment written where the bind is |
| Rig liveness | `scripts/blender_rig.py`, `tests/test_blender_rig.py` | dead symlink branch removed with its behaviour pinned instead, timed-out command reported as an unknown outcome, frame-level deadline, launch inside the `try`, tolerant `join` |

**Gate after the repairs, measured on this tree:**

| Check | Value | vs `3f071ba` | Gate |
|---|---|---|---|
| pytest | **798 passed** | +59 new; all 739 prior pass unmodified | pass |
| `ruff check .` | **9,833** | **-1** | `<= 9,834` pass |
| `ruff format --check .` | 12 unformatted, 338 formatted | unformatted unchanged | pass |
| `basedpyright` | 71 errors, 4 warnings | ±0 | pass |
| `all` | 285 tools / 1,181,023 B | ±0 | count matches `bundles.py` |
| `shot` | 53 tools / 203,079 B | ±0 | 15 B under its unchanged ceiling |
| revert matrix | **117 rows, 0 survivors, 0 uncovered** | +22 rows | pass |

739 + 59 = 798 exactly: 39 transport nodes, 9 addon nodes, 7 rig nodes, 4 container nodes.

#### The cross-agent conflict, and why the resolution is the right one

Three of the four implementers independently reported the same failure:
`test_entrypoint_serves_the_mcp_server_over_http_on_the_published_port` went red because the new refusal of
non-loopback binds correctly rejected the container's own `BLENDERMCP_HTTP_HOST=0.0.0.0`. Each declined to fix
it, because the fix straddled two agents' file sets — which is the outcome the disjoint-ownership brief was
meant to produce.

Resolved by the orchestrator in the direction that keeps the control honest: **the container opts in
explicitly**, with `BLENDERMCP_HTTP_ALLOW_REMOTE=1` on the same line as the bind and a comment saying the
`0.0.0.0` is safe only because compose publishes `127.0.0.1:8000:8000`. The alternative — exempting the
container path inside the parser — is the hole. The entrypoint is exactly where a human had already written
"Binds 0.0.0.0 only because Docker's port forward arrives on the container's external interface"; that comment
now has to be backed by a declaration the code reads.
`tests/test_docker_rig.py`'s env scrape was widened from a hand-listed three variables to `HTTP_ONLY_ENVS`, so
a variable the server learns to read cannot be silently dropped from what the test feeds it again.

#### The revert matrix was the most productive instrument of this cycle

It was hardened first, then run three times, and it found defects in the repairs themselves — including two in
tests written by the same agents that wrote the fixes.

**Hardened (critic 4's findings 1 and 2).** `run_nodes()` returned `result.returncode != 0`, which cannot
distinguish a collection error from an assertion failure, and credited a multi-node row when only one node
failed. It now refuses any summary containing `error`, refuses any summary containing `passed`, and requires
the failure count to equal the number of nodes the row names. Row **A2** — whose substitution left an
`IndentationError`, so its node failed at parse time and proved nothing across three prior cycles — was
rewritten to a parsable revert that trips the assertion it is actually about.

**Coverage closed.** `--list` reported **50 new nodes with no row**. That is the same gap that let the
unreachable `--work-dir` symlink guard survive three critic cycles: the matrix pins test *nodes*, so a branch
with no test has no node to pin, and a fix whose test nobody pinned is a fix nobody proved. 22 rows were added
and the count is now **117 rows / 0 uncovered**, with three nodes recorded in
`NOT_INDIVIDUALLY_FALSIFIABLE` with reasons rather than left as silent gaps — two characterise FastMCP's
middleware (they are what make `_serve_http`'s security claims falsifiable at all) and one guards its sibling's
`python -O` method.

**Two survivors on the second run, both real, neither a bad row:**

1. `test_a_rejected_value_is_not_echoed_whole_into_the_log` survived the removal of the value clip. Cause: with
   a 5,000-digit port, `int()` raises **CPython's own** `ValueError` — "Exceeds the limit (4300 digits) for
   integer string conversion" — which `_parse_port` did not catch. That message is ~140 characters, under the
   test's 200-character limit, so the test passed on an error that **named neither the variable nor a clip of
   its value**, bypassing the entire contract the repair had just written. Fixed in *both* places: a
   `MAX_PORT_DIGITS` bound rejects an over-long value before `int()` sees it, and the test now asserts the
   message **names the variable**, not merely that it is short. A length-only assertion could not tell this
   function's error from the interpreter's.
2. The row for the 0.0.0.0 containment note deleted a comment line the test does not read. Retargeted at the
   line carrying the compose mapping the test actually checks.

Both were invisible to every other instrument: the suite was green, the lint was clean, and four adversarial
critics had already passed over the area. **The harness that was hardened in order to be trusted immediately
justified the hardening**, which is the argument for doing it before the run rather than after.

#### Where the implementers pushed back, and were right

Recorded because in each case the brief was wrong and the agent said so rather than complying:

- **`kill -0` cannot bound a teardown.** The brief offered it as an acceptable way to wait for a child.
  Rejected: `kill -0` succeeds on a child that has exited but not yet been reaped, and bash reaps
  asynchronously — the very property the *other* entrypoint fix depends on — so a poll loop cannot tell
  "wedged" from "dead, not yet reaped" and would spend the full grace period on every healthy shutdown. A
  background watchdog was used instead, and a test pins that a normal teardown costs nothing.
- **`.isdecimal()` does not reject full-width digits.** The brief's suggested port fix would have let five of
  the six measured bad inputs through: `'０００８０００'.isdecimal()` is `True`, which is exactly why `int()`
  accepts it. `digits.isascii() and digits.isdecimal()` is the correct pair.
- **A per-process cache of the writable roots would have changed observable content.** The candidate list
  includes the open `.blend`'s directory, so a flat cache would advertise a stale directory for the life of the
  process. Memoizing on the candidate tuple re-probes exactly when the answer can differ, and building that
  tuple touches no filesystem. The brief's other suggestion — compute once in `start()` — was also rejected,
  correctly: `get_addon_info` is reachable without `start()` having run.
- **A tolerant `join()` should be silent, not diagnostic.** The brief wanted teardown to report a reader that
  never started. Rejected: that can only happen while the real exception is already propagating, so raising
  from the `finally` would replace the true cause with a vaguer one.
- **The symlink guard should be deleted, not made reachable.** Resolution and the ownership-marker check
  compose rather than overlap — a link aimed at data is refused *because the data is there*, demonstrated — so
  making the branch reachable would only refuse legitimate configurations (a scratch dir on another volume, a
  `$TMPDIR` alias) while protecting nothing. Three tests now pin the real behaviour.

#### Still open, deliberately, with owners

- **A queued command is not cancelled when its client goes away.** The rig can only describe this; the fix is
  server-side — record the originating connection with each queued command and drop it undequeued if that
  connection is gone, logging the drop. Until then a timed-out command may still execute, and the rig now says
  so in as many words instead of reporting a plain failure. **Recommended for Task 3**, which owns the drain
  loop. This also corrects this file's earlier account of the limitation, which called it a "duplicate or
  misrouted *second* response ... swallowed by the client-disconnected branch": measured, it is the **first and
  only** response, and it usually hits no branch and emits no log line, because `sendall` to a peer-closed TCP
  socket succeeds at the kernel level until the RST arrives.
- **`_server_loop` calls `settimeout` before entering its guarded loop**, so a `stop()` landing in that window
  closes the socket under the thread and it dies with an unhandled `EBADF`. Cosmetic today (a traceback in
  Blender's console on a fast addon toggle), surfaced by an implementer and deliberately left. **Task 3.**
- **`Host` validation is browser-oriented and is not an access control.** Now documented as such in
  `_serve_http`, the module docstring and `README.md` rather than fixed, because the owner's decision is that
  remote hosting is unsupported. **Task 8** inherits the question if that changes.
- **A malformed reply frame still lets `json.JSONDecodeError` out of `BlenderRig.send`**, contradicting its
  documented `RigError` contract exactly as the bare `TimeoutError` did. Not one of the cycle-4 findings and
  not reachable from any measured failure. Worth a follow-up.

### Task 1 — after cycle 3

**Not self-scored.** Cycle 1's 100/100 self-assessment is the specific failure this section exists to avoid
repeating, and cycle 3's brief forbids it explicitly: the re-score is the orchestrator's third critic cycle to
award. What is claimed is the evidence, all of it reproducible in-session: the cycle-3 Step 5 transcript from a
fresh work dir, the three before/after demonstrations (A1's deadlock, A4's silent exit 0, B3's overwritten
file), the `user_resource` probe, the fixture MD5, the **81-row / 76-node** revert matrix with 0 survivors and 0
uncovered nodes, the container measurements, and the gate figures in the state table.

**Critical-failure check (§07), re-run against this tree:**

- **No file overwritten without confirmation.** Strengthened this cycle: the rig now refuses any `--work-dir`
  it did not create, replaces only `addons/blender_mcp` rather than the `addons/` tree, and refuses a
  pre-placed or symlinked fixture destination. Demonstrated.
- **No absolute path in a client-facing error.** The rig's messages are developer-facing by construction (they
  name the caller's own `--work-dir`); the addon's client-facing envelope is unchanged.
- **No `use_scripts` parameter and no `wm.open_mainfile` in shipped code.** `wm.open_mainfile` appears only in
  the scratch `--blender-script` used as evidence, never in `src/`.
- **No rollback change.**
- **No hang.** Strengthened this cycle: the post-swap `ping` is answered; the rig has a scenario deadline; the
  `Popen` context manager's unbounded `wait()` is gone; and the log reader can no longer die and re-create the
  pipe-full deadlock.
- **No path escaping configured roots** — no enforcement changed here; the advertised list is stated as
  advisory in three places.
- **No tool/handler mismatch.**
- **No test that still passes with its fix reverted** — **95 of 95** reverts fail their own named nodes, **92 of
  92** new nodes covered (81 / 76 after cycle 3; the transport follow-up added 14 rows and 16 nodes). Four
  survivors have been found across the three cycles and all four are fixed and re-proven; the fourth was found
  *because* the matrix was extended to every node. The follow-up's own 16 nodes produced no survivor.
- **Every number here was measured**, including the ones that got worse.
- **`bpy` is not imported from `src/blender_mcp/server/`**, and no addon module imports from `server/`.

**Out of scope this cycle, deliberately, and still open:**

- The `bpy.app.timers.is_registered` identity bug and `test_threading.py`'s stub — **Task 3** (documented above).
- `output_roots.py`'s `abspath`-not-`realpath` and its `~` default candidate — **Task 5** (documented above).
- A long-lived-connection counting mode for the rig — **Task 10** (documented above).
- `ee25ffc`'s HTTP transport and the two `entrypoint.sh` defects — **done 2026-09-15** by the assigned follow-up
  (decision 8); criteria 2 and 3 closed with it. What that follow-up deliberately did **not** do: it did not
  port anything else from `docker-blender` (the inline image transport and its protocol 32 are still Task-5+
  work, per decision 5), did not add authentication to the HTTP endpoint (that is Task 8's deliverable, and the
  endpoint stays loopback-published in the meantime), and did not touch the healthcheck's `start_period`,
  `interval` or `retries` — the container reached healthy in 12 s on this host, so nothing needed loosening,
  but a slower host may need `start_period` raised, and that has not been measured.
