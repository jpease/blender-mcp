# Phase 2 — Primitives (file lifecycle and linking) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for
> tracking. Read `docs/superpowers/plans/2026-09-14-phase-2-handoff.md` first — it carries the constraints,
> protocol and rubric this plan assumes.

**Phase gate, verbatim from spec §8:**

> **Open a shot, link canon, create an override, save, reopen with the link intact; no hang.**

That single sentence is the top-level acceptance criterion. Task 10 is the scenario that demonstrates exactly
it; Tasks 1–9 exist to make that scenario possible, safe and provable.

**Goal:** give this server the ability to open, save and reset a `.blend`, to link a canon library into a shot
and override it, and to do all of that from inside a socket-driven addon whose command loop runs on Blender's
main thread — without hanging a client, without destroying user data, and without turning an unauthenticated
loopback socket into arbitrary filesystem read/write.

**Architecture:** Phase 1 was subtractive. **Phase 2 is the first genuinely additive work**, and its risk
profile inverts accordingly. Nothing here is a regrouping; every task either adds a command that mutates the
user's filesystem, or hardens the single code path (`drain_command_queue` → `_run_handler` → **and, for
mutating commands, `mutation_transaction`**) that the existing 285 tools already traverse. Be precise about the
last hop: **`drain_command_queue` → `_run_handler` is universal, `mutation_transaction` is not.**
`_READ_ONLY_COMMANDS` (`server_core.py:813-870`) holds **54** addon commands — counted 2026-09-14, not
estimated — and `_run_handler:1126` routes every one of them, plus dynamically-read-only and non-undo commands,
around the transaction entirely. So a change to the drain loop reaches everything; a change to
`mutation_transaction` reaches the mutating subset. A mistake in Tasks 3, 4 or 5 does not lose a tool — it
corrupts a scene, deletes a file, or hangs every client.

**Tech Stack:** Python 3.13, Blender 5.2.2 (target API: 5.1+), FastMCP, Pydantic v2, pytest, ruff,
basedpyright, Docker + Xvfb for the live-Blender rig.

> **Version note (2026-09-15).** The host moved 5.2.1 -> 5.2.2 after this plan was written. Measurements below
> that cite 5.2.1 are left as the historical record of when they were taken; all of them were re-run against
> 5.2.2 and **every one still holds**, with one refinement to `override_create`'s return value recorded in
> `PHASE2_TASK_STATE.md`.

**Spec:** `docs/superpowers/specs/2026-09-11-episode-consistency-architecture-design.md` — §4.5 (file lifecycle
and linking, and the three open hazards), §4.7 (the local plugin's threading contract), §5 (delivery contract),
§8 (phasing), §9 Decisions #7 and #9, §10 Q8 (socket authentication).

**Prior phase:** `docs/superpowers/plans/2026-09-11-phase-1-catalog.md` and
`docs/superpowers/plans/PHASE1_TASK_STATE.md`. Phase 1 is complete, committed and pushed at `523f427`.

---

## 0. Verified current state — read this before any task

Everything in this section was established by reading the tree at `523f427` and by executing against Blender
5.2.1 at `/opt/homebrew/bin/blender` on 2026-09-14. **Where it contradicts the spec, this section is right and
the spec is stale** — the spec's §4.5 table is a design sketch that was never executed. Phase 1 was burned
three separate times by plausible numbers nobody ran; do not repeat it.

### 0.1 What exists today (confirmed absent or present)

| Claim | Verified result |
|---|---|
| `wm.open_mainfile` / `wm.save_mainfile` / `save_as_mainfile` anywhere in the repo | **Zero hits.** `rg -n 'open_mainfile\|save_mainfile\|save_as_mainfile' --glob '*.py'` returns nothing outside this plan. The server genuinely cannot open or save a `.blend`. |
| `bpy.app.handlers` used in production code | **Zero hits.** The only occurrences are test fixtures stubbing the module (`tests/test_mutation_transaction.py:182`, `tests/server/tools/test_sketchfab.py:32`, and three others). |
| `bpy.data.libraries.load` | **One call**, `handlers/polyhaven.py:362`, with `link=False`. No linking exists. |
| Library overrides (`override_create`, `override_hierarchy_create`, `make_override_library`) | **Zero hits *in this repo*.** All three exist in Blender 5.2.1 — see §0.3 finding 4. This row is a repo grep, not an API claim. |
| `wm.lib_reload` / `wm.lib_relocate` | **Zero hits.** |
| A filesystem **path allowlist** | **Does not exist anywhere in the repo.** See §0.3 finding 1. |
| The drain timer's `persistent=True` | Present: `server_core.py:183-184`, `bpy.app.timers.register(self.drain_command_queue, persistent=True)`. |
| `Library` datablocks in transaction rollback | **Absent.** `transaction.py:17-36` `_TRACKED_COLLECTIONS` lists 18 collections; `libraries` is not one of them. |

### 0.2 The code Phase 2 lives inside (exact citations)

**`src/blender_mcp/bundled/addon/server_core.py`** (1651 lines)

- `:152-158` — `start()` returns early when `bpy.app.background` is true, printing the `xvfb-run` hint.
- `:177-179` / `:250-252` — listener and per-client handler threads, both `daemon = True`.
- `:183-184` — the drain timer is registered exactly once, from `start()` (main thread), `persistent=True`.
- `:267-321` — `drain_command_queue()`. Returns `None` when `self.running` is false (unregistering itself),
  otherwise `0.05`. Loop condition `:283`: `while processed < self._MAX_COMMANDS_PER_TICK and
  time.monotonic() < deadline`.
- `:299` — the request `id` is echoed onto the response. `:304-316` — response framed with `b"\n"`, size-capped,
  `client.sendall(payload)`.
- `:329-332` — `_MAX_MESSAGE_BYTES = 64 MiB`, `_MAX_QUEUED_COMMANDS = 256`, `_MAX_COMMANDS_PER_TICK = 8`,
  `_DRAIN_TIME_BUDGET_SECONDS = 0.02`.
- `:342-391` — `_decode_and_queue_frame()`, the only place a command enters the queue. Validates `id`, `type`,
  `params` shapes at the transport boundary and answers `queue.Full` with a retry message.
- `:480-808` — `_build_command_handlers()`, the single dispatch table. Provider blocks gated on scene flags at
  `:774` (Poly Haven), `:784` (Sketchfab), `:793` (ND).
- `:813-870` — `_READ_ONLY_COMMANDS`, the frozenset that decides whether a command is wrapped in a transaction.
- `:872-913` — `execute_command_internal()`; `ping` is special-cased at `:888` before the table is built.
- `:1064-1142` — `_run_handler()`. `:1126` takes the read-only path; `:1131` otherwise enters
  `mutation_transaction(cmd_type, targets, capture_geometry)`.
- `:1144-1158` — `get_addon_info()`. **`:1156` derives `capabilities` from `_build_command_handlers()`**, so a
  new handler automatically becomes an advertised capability — there is no second list to update.

**`src/blender_mcp/bundled/addon/transaction.py`** (270 lines)

- `:17-36` `_TRACKED_COLLECTIONS` — 18 `bpy.data` collection names, **no `libraries`**.
- `:39-61` `_snapshot_ids()` — records `session_uid` of every datablock in every tracked collection.
- `:64-87` `_new_datablocks()` — anything whose `session_uid` is not in the snapshot is "created by this request".
- `:90-112` `_remove_datablocks()` — removes them, objects first, in reverse order, each in `suppress(Exception)`.
- `:186-198` `Transaction.begin()` / `.rollback()`.

**`src/blender_mcp/bundled/addon/object_state.py`** (226 lines) — `ObjectState.__init__` (`:35-59`) stores
**live `bpy` references**: `self.obj`, `self.parent`, `self.collections`, `self.materials`, and optionally
`self.geometry_backup = obj.data.copy()`.

**`src/blender_mcp/server/connection.py`** (381 lines)

- `:150` / `:214` — 180-second socket timeout on both send and receive.
- `:185-190` — **every outbound command is gated on the cached handshake's `capabilities` set.** A tool whose
  addon command is not in that set raises before a byte is sent.
- `:195-196` — one in-flight request per connection, enforced by `self._lock`.
- `:223-231` — response `id` mismatch raises "the connection to Blender is desynced".
- `:350-370` — `force_addon_handshake()` already exists and is already exposed through
  `get_addon_status` (`server/tools/core.py:57-103`).

**Protocol version** — `bundled/addon/__init__.py:28` `ADDON_PROTOCOL_VERSION = 30`;
`src/blender_mcp/addon_manager.py:29` (**not** under `server/` — it sits at the package root)
`EXPECTED_ADDON_PROTOCOL_VERSION = 30`; `tests/test_addon_manager.py:20-21` asserts the
two agree by parsing the addon source. **Any task that adds an addon command must bump both, together.**

**Path handling precedent** — `handlers/rendering.py:550-556` is the established pattern and the one Phase 2
should mirror rather than invent around:

```python
if not isinstance(filepath, str) or not filepath.strip():
    raise ValueError("filepath must be a non-empty string")
output = os.path.abspath(bpy.path.abspath(filepath))
directory = os.path.dirname(output)
if not directory or not os.path.isdir(directory):
    ...
if mode == "STILL" and os.path.exists(output) and not confirm_overwrite:
    ...
```

**Test harness that already exists and is load-bearing for Phase 2** —
`tests/server/test_threading.py:30-107` AST-lifts the `BlenderMCPServer` class out of `server_core.py`, compiles
it against a stub `bpy` whose `timers.register()` *raises if called off the main thread*, and drives it over a
real TCP socket with `_pump()` (`:126-131`) standing in for Blender's main loop. `tests/conftest.py` carries
`load_addon_package()` for loading the addon package against mocks, and `stub_blender_connection` for
server-side tool tests.

**Measured payload state at `523f427`** (`.venv/bin/python scripts/measure_catalog.py`):

| Selection | Tools | Bytes |
|---|---|---|
| `all` | 285 | 1,181,038 |
| default (`core`) | 21 | 63,395 |
| `shot` | 53 | **203,094** |

`tests/server/test_bundles.py:509` pins `SHOT_MODE_BYTE_CEILING = 203_094` and asserts `<=`. **`shot` is
exactly at its ceiling; there is zero headroom.** `tests/server/test_bundles.py:541-551` parses the "285 tools"
figure out of `bundles.py:8`'s docstring and asserts the catalog matches it. Both must be handled deliberately
in Task 9.

### 0.3 Seven places the spec is stale, wrong — or, in one case, right after all

These are recorded the way Phase 1 recorded "Task 5 is not what the plan thinks it is". Do not build around the
spec's prose where it conflicts with this list.

**Finding 4 is different from the other six and is worth reading first.** The other six correct the spec.
Finding 4 corrects *this plan's own earlier correction of the spec*, **and then corrects the part of the spec
that the correction pass left alone**. A previous revision declared the spec's `create_override` API
non-existent, on the strength of an introspection method that cannot detect any RNA member in this build; the
spec's API mapping was right and that correction was wrong. But the *same sentence* of the spec also claimed
`object.override_create()` "returns `None`", and **that claim is false and survived two passes** because each
pass only re-checked the half it was already arguing about. Both are kept here, with their superseded text,
because a plan that hides its own retractions teaches the next reader nothing — and because the two methodology
errors (an invalid absence check, and an unverified claim inherited across a correction) are the transferable
part. See also the "Introspect, never guess" global constraint below, which now names both traps.

1. **There is no link/append allowlist.** Spec §4.5 says the security gap is "not addressed by the link/append
   allowlist, which does not cover them", and §7 lists a test row for "Path allowlist incl. the Poly Haven
   `.blend` path". `rg -ni 'allowlist|allow_list|whitelist' --glob '*.py' src tests` returns **55 hits**
   (re-counted 2026-09-14), and **every one of them is a *property-name* allowlist inside a handler** — e.g.
   `handlers/scene.py:778`, `handlers/node_graph.py:76`, `handlers/lighting/_shared.py:344`. Not one is a
   filesystem control. **No filesystem path is validated against anything, anywhere.** Phase 2 is not hardening
   an existing control; it is building the first one.

2. **The Xvfb rig already exists — on the wrong branch.** Spec §8 defers "Xvfb rig" to Phase 1b as unbuilt work.
   In fact `docker-blender` carries `docker/blender/{Dockerfile,entrypoint.sh,docker-compose.yml,healthcheck.py,
   start_server.py}` plus `tests/test_docker_rig.py`. `entrypoint.sh` already runs `Xvfb :99 -screen 0
   1280x720x24`, installs the addon into the right Blender version's addons dir, starts Blender and the MCP
   server, and compose has a healthcheck that round-trips both. What is missing is exactly what §8's Phase 2 row
   asks for: **`docker-blender` has no `bundles.py`** (`git cat-file -e docker-blender:src/blender_mcp/server/
   bundles.py` fails) and is 8 commits ahead / 39 behind `main`.

3. **A writable-roots mechanism already exists, also on `docker-blender`, and the spec does not know about it.**
   `src/blender_mcp/bundled/addon/output_roots.py` (+ `tests/test_output_roots.py`) provides
   `configured_roots()` reading `BLENDERMCP_OUTPUT_ROOTS` (`os.pathsep`-separated) and `writable_roots()`
   reducing candidates to existing, writable, absolute, deduplicated directories. It is **advisory only** — the
   addon reports the roots through the handshake so the agent knows where it can write; nothing enforces them.
   It is `bpy`-free and already tested. It is the obvious seed for Task 5 and it should be promoted, not
   reinvented.

   **`output_roots.py` is not a standalone module — it is wired into five other places, and an earlier
   revision of Task 1 ported only the module and its test.** Enumerated from the branch on 2026-09-14
   (`git grep -n 'output_roots\|writable_output_roots' docker-blender -- '*.py'`):

   | Site on `docker-blender` | What it does |
   |---|---|
   | `bundled/addon/server_core.py:36` | `from .output_roots import configured_roots, writable_roots` |
   | `bundled/addon/server_core.py:1160` | `"writable_output_roots": self._writable_output_roots()` inside `get_addon_info()` |
   | `bundled/addon/server_core.py:1164` | the `_writable_output_roots()` staticmethod itself |
   | `addon_manager.py:216` | `writable_output_roots: list[str]` field on `AddonHandshake` |
   | `addon_manager.py:539` | parses it out of the handshake payload, defaulting to `[]` for an older addon |
   | `tests/test_addon_manager.py:203-231` | two tests: the field surfaces, and it defaults for an older addon |

   **Decisive consequence:** `tests/test_output_roots.py` — a file Task 1 ports verbatim — itself contains two
   tests (`:112` `test_get_addon_info_reports_writable_output_roots` and `:139`) that construct a
   `BlenderMCPServer` and assert on `get_addon_info()["writable_output_roots"]`. **The ported test file cannot
   pass without the `server_core.py` wiring.** Porting the module alone does not merely leave dead code; it
   leaves Task 1's own Step 3 red. See Task 1's "Ruling: port the full wiring, including the protocol bump".

   `docker-blender` also carries `ADDON_PROTOCOL_VERSION = 31` / `EXPECTED_ADDON_PROTOCOL_VERSION = 31`
   (`main` is at 30 in both places), bumped **specifically for this handshake field**. That bump therefore
   belongs to Task 1, not to Task 3 — see the Global Constraints bullet on protocol bumps.

4. **`create_override`'s prescribed API *does* exist in Blender 5.2.1 — the spec is right here, and an earlier
   draft of this plan was wrong.** *(Corrected 2026-09-14 after re-introspection; the superseded text is
   preserved at the end of this finding.)*

   Spec §4.5 maps `create_override` to
   `collection.override_hierarchy_create(scene, view_layer, reference=instance)`. **That mapping holds.**
   Introspected against 5.2.1 the *correct* way — `bl_rna.functions`, not `dir()` on the type object:

   ```
   [f.identifier for f in bpy.types.Collection.bl_rna.functions if 'override' in f.identifier.lower()]
     -> ['override_create', 'override_hierarchy_create']
   [f.identifier for f in bpy.types.ID.bl_rna.functions if 'override' in f.identifier.lower()]
     -> ['override_create', 'override_hierarchy_create']

   # signature, from the bound method's __doc__ on a real instance:
   Collection.override_hierarchy_create(scene, view_layer, *, reference=None, do_fully_editable=False)
   Collection.override_create(*, remap_local_usages=False)
   ```

   This matches `docs.blender.org/api/5.2/bpy.types.ID.html` exactly. (That domain returns HTTP 403 to a plain
   `WebFetch`; fetch it with `curl -A Mozilla/5.0 https://docs.blender.org/api/5.2/bpy.types.ID.html`.)

   **The spec's *other* half does not hold, and this plan repeated the error twice before catching it.** The
   spec notes that `object.override_create()` "returns `None` — verified", and two earlier revisions of this
   plan endorsed that as "right and stands". **It is false.** Re-measured from scratch on 2026-09-14 against
   5.2.1: link a library with `bpy.data.libraries.load(link=True)`, then call `override_create()` on the linked
   object and on a local one —

   ```
   # (a) a linked, overridable object
   linked_obj.override_create(remap_local_usages=True)
     -> bpy.data.objects['Cube']          # the NEW override ID, with .override_library set
                                          # and .override_library.reference == the linked original
   # (b) a local, non-overridable object
   local_obj.override_create(remap_local_usages=True)
     -> None
   ```

   The 5.2 API reference agrees: `override_create` is documented as returning "New overridden local copy of
   the ID", return type `ID`. `None` is what you get when the ID is **not overridable** — a local datablock, or
   anything the operation does not support — not what you get on the linked-object path the spec was describing.

   **This does not change Task 7's ruling, but it changes the reason for it.** Per-object `override_create`
   is not rejected because it fails; it is rejected because it overrides **one ID at a time**, and the job is
   to override an entire collection's hierarchy in one call. `override_hierarchy_create` is the hierarchy-level
   API and is therefore the right tool. See Task 7.

   **The methodology trap that produced the earlier "does not exist" claim, stated so nobody repeats it**
   (the `returns None` claim above has a *different* cause — see the second superseded block): in this build,
   `dir()` on a `bpy.types.*` **type object** returns **no RNA members at all, for anything**. The
   self-refutation is one line —

   ```
   'copy' in dir(bpy.types.Collection)   -> False     # Collection.copy() obviously exists
   [m for m in dir(bpy.types.Collection) if 'override' in m.lower()]  -> []
   ```

   — so an empty `dir(bpy.types.X)` result is **evidence of nothing**. The three valid absence checks are
   `X.bl_rna.functions` / `X.bl_rna.properties`, `dir()` on an actual *instance*, or `__doc__` on a bound
   method. On an instance the same query returns
   `['override_create', 'override_hierarchy_create', 'override_library']`.

   Also present and genuinely useful in 5.2.1: the operator
   `bpy.ops.object.make_override_library(collection=0)`, the outliner operators
   `bpy.ops.outliner.liboverride_operation` / `liboverride_property_remove` /
   `liboverride_troubleshoot_operation`, the read-only `ID.override_library` property
   (`IDOverrideLibrary` with `hierarchy_root`, `reference`, `is_system_override`, `is_in_hierarchy`,
   `properties`), and **keyword arguments on the data API itself**:

   ```
   bpy.data.libraries.load(filepath, *, link=False, pack=False, relative=False, set_fake=False,
       recursive=False, reuse_local_id=False, assets_only=False, clear_asset_data=False,
       create_liboverrides=False, reuse_liboverrides=False, create_liboverrides_runtime=False)
   ```

   **Which of these Task 7 should use is decided in Task 7's "Current state, verified" section, on measured
   behaviour rather than availability** — all three routes exist; they differ in whether the *objects inside*
   the override come out editable.

   > **Superseded text 1 (kept so the correction is auditable):** *"`create_override`'s prescribed API does not
   > exist in Blender 5.2.1 … Neither method exists."* That claim rested entirely on the three empty
   > `dir(bpy.types.*)` results above and is false. **Cause: an invalid absence check.**

   > **Superseded text 2 (corrected 2026-09-14, after surviving two passes):** *"The spec's other half also
   > holds: `object.override_create()` returns `None` — that part of the earlier analysis was right and
   > stands. Do not reach for per-object `override_create`; it is not the route."* The conclusion was right and
   > the premise was false. **Cause: not an invalid check but *no* check.** The claim was inherited from the
   > spec, then re-endorsed by a correction pass whose attention was on the *other* half of the same sentence.
   > Nobody ran `override_create()` on a linked object until now. **A neighbouring claim is not verified by the
   > correction next to it — re-run the whole statement you are editing.**

5. **The whole gate scenario already works through the data API, verified end to end in `--background`.**
   Executed on 2026-09-14 against 5.2.1 (script kept in the session scratchpad; reproduce it, do not trust it):
   link a collection with `bpy.data.libraries.load(lib, link=True, create_liboverrides=True,
   reuse_liboverrides=True)`, link the resulting override collection into the scene, `wm.save_as_mainfile(
   compress=False, relative_remap=False)`, `wm.open_mainfile()`. Result:

   ```
   after load: collections = [('CanonHero', library=False, override=True), ('CanonHero', library=True, override=False)]
   libraries  = [('canon_hero.blend', '<abs path>', version=(5, 2, 44), is_missing=False)]
   override hierarchy_root = Collection("CanonHero")
   REOPENED libs      = [('canon_hero.blend', '<abs path>', is_missing=False)]
   REOPENED overrides = [('CanonHero', True), ('CanonHero', False)]
   ```

   Three consequences. (a) **`link` and `create_override` can be one data-API call**, needing no operator
   context, no selection and no active object. That is a genuine convenience, but it is **not** a discriminator
   between routes: `Collection.override_hierarchy_create(scene, view_layer)` is *also* pure data API and *also*
   needs no operator context — verified by calling it with `bpy.data.scenes[0]` / `scene.view_layers[0]` and no
   `bpy.context` involvement at all. (b) `wm.open_mainfile` and `wm.save_as_mainfile` **work fine in
   `--background`**, so the *bpy* half of Tasks 6 and 7 is testable without a display. (c) **The override this
   route produces is collection-level only, and the objects inside it stay locked**:
   `library=True, override_library=None, is_editable=False`. That is a real limitation of *this* route, not of
   library overrides generally — `override_hierarchy_create(..., do_fully_editable=True)` produces editable
   object overrides. Task 7 selects between them on that basis. (d) **The name collision is not confined to
   collections, and an earlier revision of this finding said it was.** Re-measured 2026-09-14 on both routes:

   ```
   # Route A (libraries.load(..., create_liboverrides=True)) — collection-level collision only
   COLLECTION 'CanonHero' library=False override=True  session_uid=389
   COLLECTION 'CanonHero' library=True  override=False session_uid=386
   OBJECT     'HeroBody'  library=True  override=False is_editable=False     # exactly ONE object

   # Route C (override_hierarchy_create(..., do_fully_editable=True)) — collision at OBJECT level too
   OBJECT 'CanonHero' library=False override=False is_editable=True   # the instance empty
   OBJECT 'HeroBody'  library=False override=True  is_editable=True   # the override
   OBJECT 'HeroBody'  library=True  override=False is_editable=False  # the linked original
   ```

   Route A leaves one `HeroBody`; **Route C leaves two objects sharing a name**, differing only in
   `library` / `override_library` / `is_editable` — and **Task 7 rules for Route C**, so this is the shape
   Phase 2 actually ships. The disambiguation requirement therefore applies to **every datablock type an
   override touches, objects included**, not only to `bpy.data.collections`. `list_libraries`,
   `create_override` and every library/override handle that crosses a call must be resolved and reported by
   `session_uid` plus `library` / `override_library` / `is_system_override` state, **never by name**. A
   `bpy.data.objects[name]` lookup after a Route C override is ambiguous by construction and silently returns
   whichever Blender ordered first. Task 7's command signatures are written accordingly.

6. **"Untestable headlessly" is over-broad.** Spec §4.5 says reentrancy is untestable headlessly because timers
   do not fire in `--background`. The timer half is true — verified: registering a `persistent=True` timer in
   `--background` and spinning for 3 s fires it **0 times** while `is_registered` stays `True`. But
   `tests/server/test_threading.py` already exercises `drain_command_queue` over a real socket with a stub `bpy`
   and a hand-rolled pump, so the **protocol** half of reentrancy — ordering against queued commands, the
   response/swap sequence, queue rejection, the barrier — is testable today with no display at all. Only the
   question "does the callback frame survive a real `wm.open_mainfile`" needs a live Blender event loop. Scope
   the Xvfb dependency to that one question (Task 2), not to the whole hazard.

7. **The rollback hazard is worse than the spec states, and it is a data-destruction bug, not a leak.** Spec
   §4.5 says only that "`transaction.py` backups reference datablocks that a load frees". Traced against the
   real code: `open_shot` will not be in `_READ_ONLY_COMMANDS`, so `_run_handler:1131` wraps it in
   `mutation_transaction`. `Transaction.begin()` snapshots `session_uid`s of the **old** file. The handler loads
   a new file. Every datablock in the new file carries a fresh `session_uid`, so if anything raises afterwards —
   including a handler returning a failure shape, which `_run_handler:1136-1138` converts to
   `HandlerReportedError` **inside** the transaction — `rollback()` calls `_new_datablocks()`, classifies
   *every datablock in the freshly opened file* as newly created, and `_remove_datablocks()` deletes them.
   Separately, `ObjectState` holds live `bpy` references across the load and `discard_backup()` calls
   `collection.remove(self.geometry_backup)` on a freed datablock. This is Task 4 and it is the single most
   dangerous line item in the phase.

   **And it is not only `open_shot`. The same mechanism reaches `reload_library`, `relocate_library` and
   `unlink_libraries`, which are *not* session-swap commands and so stay inside `mutation_transaction`.**
   Measured 2026-09-14 against 5.2.1, on a scene with one linked library:

   ```
   uids BEFORE lib.reload(): GR CanonHero=649  OB HeroBody=650  ME HeroMesh=651
   handlers fired:           ['blend_import_pre', 'blend_import_post']      # NOT load_post
   uids AFTER  lib.reload(): GR CanonHero=652  OB HeroBody=653  ME HeroMesh=654
   session_uid changed for:  3 of 3 datablocks
   ```

   Every datablock linked from the library gets a **fresh `session_uid`**, exactly as a file load does — so a
   raise anywhere after the reload inside the same transaction makes `_new_datablocks()` classify the whole
   reloaded library's contents as newly created and `_remove_datablocks()` delete them. And **`load_post` never
   fires**, so Task 4's session-swap invalidation as originally specified does not trigger. Two further
   measurements bound the fix:

   - `bpy.data.libraries.load(link=True)` and `load(link=False)` fire `blend_import_pre` / `blend_import_post`
     **too** — so a blanket "invalidate on `blend_import_post`" would also disarm the transaction on
     `link_canon_library`, defeating Task 4's own item 4 (a failed link must roll its `Library` datablock back).
     `blend_import_post` cannot distinguish replace-in-place from additive-link.
   - `bpy.data.orphans_purge(...)` and `bpy.data.libraries.remove(...)` fire **no handler at all** — so for
     `unlink_libraries` the hazard is the mirror image (datablocks *freed* under a live `_before_ids` /
     `ObjectState`, with no handler-based invalidation available even in principle).

   Task 4 owns the fix and Task 7 carries it forward; see Task 4's "What to build" item 2a.

### 0.4 Two open design questions, and how this plan closes them

> **Q1 was closed on 2026-09-15 by Task 2**, whose experiment is recorded in
> `PHASE2_TASK_STATE.md` under "Task 2 — the reentrancy strategy". **Decided: synchronous
> validate-then-swap, answering after the swap.** The async-job-with-polling branch described below
> is **rejected** — the callback demonstrably survives and answers — and is kept here only because
> the rule that selected against it has to stay readable. Nothing below should be implemented as
> written; read the TASK_STATE section for what Tasks 3, 5, 6 and 7 actually inherit. Q2 remains
> open and is still Task 8's deliverable.

Spec §4.5 left the reentrancy strategy explicitly undecided, and §10 Q8 names socket authentication as "the
dominant risk for a pooled deployment" without proposing anything. Carrying both forward as prose would repeat
the spec's own failure. This plan closes them as follows.

**Q1 — reentrancy strategy.** The spec offers two options (two-phase validate-then-load answering *before* the
swap, or an async job with polling) and rejects a third (defer and answer first) because it cannot report a
post-answer failure. All three framings assume the drain callback cannot survive the load and therefore cannot
answer after it. **That assumption has never been tested.** Task 2 tests it, under a decision rule stated in
advance so the answer cannot be rationalised after the fact:

- **If the callback survives the swap and can still `sendall` on its client socket** → adopt
  **synchronous validate-then-swap, answer after the swap**. It is the only option whose answer is truthful by
  construction, it needs no new job/polling surface, and it keeps one request → one response, which
  `connection.py:195-231` already depends on.
- **If the callback does not survive** → adopt the **async job with polling** (`open_shot` returns a job id;
  **`get_session_info`** reports `queued`/`loading`/`ready`/`failed`). The spec is right that two-phase
  answer-before-swap cannot report a post-validation failure, so it is not the fallback. **The poll surface is
  `get_session_info`** — the command Task 3 item 4 defines, which exists in both branches. An earlier revision
  called it `get_session_status` here and `get_session_info` everywhere else; there is one command and its name
  is `get_session_info`. This branch adds **fields** to it (job id, state), not a second command, so it does not
  add an eleventh tool to Task 9's budget.

Either way, Blender 5.2.1 provides a truthfulness backstop the spec does not mention: **`bpy.app.handlers`
carries `load_post`, `load_pre`, `load_post_fail`, `save_pre`, `save_post`, `save_post_fail`,
`blend_import_pre`, `blend_import_post` and `@persistent`** (verified by introspection). A `@persistent`
`load_post_fail` / `save_post_fail` handler records the failure in module state so the *next* command — or the
async job's status — reports it accurately. Task 3 installs those handlers regardless of which branch Task 2
selects.

**Q2 — socket authentication.** Full authentication is spec §8 Phase 4 work and this plan does not implement it.
What Phase 2 *does* own is the half that cannot wait, because Phase 2 is what creates the exposure:

- **Task 5 builds the enforcing filesystem boundary** — path canonicalization, root enforcement, extension and
  magic-byte validation, mandatory overwrite confirmation, and a retrofit onto `handlers/polyhaven.py:362`,
  which today loads a network-fetched `.blend` with no path check at all (spec Appendix A, R2, still **open**).
- **Task 8 produces the authentication design itself as a written deliverable** — a new spec section, a threat
  model, and a decision record — with no code. That is the honest scope: designing it inside Phase 2 means
  Phase 4 implements a reviewed design instead of inventing one under deadline, and it means the Phase 2 risk
  statement ("we widened an unauthenticated socket to arbitrary-path read/write") is answered with a dated plan
  rather than a shrug.

---

## Global Constraints

- **Python 3.13+, Blender 5.1+ API only.** No compatibility shims for removed 3.x/4.x APIs. Target and verify
  against the installed **Blender 5.2.1**.
- **Introspect, never guess — and introspect *correctly*.** Per CLAUDE.md, real Blender API introspection beats
  a guessed operator argument. But **absence of an API may never be concluded from `dir()` on a `bpy.types.*`
  class**: in this build that returns no RNA members for *anything* (`'copy' in dir(bpy.types.Collection)` is
  `False`), so an empty result proves nothing. Use `X.bl_rna.functions` / `X.bl_rna.properties`, or `dir()` on
  an actual *instance*, or `__doc__` on a bound method. §0.3 finding 4 records the three failures this rule
  exists to prevent: a spec that guessed; a plan "correction" that used the wrong check and declared a real API
  missing; and a false claim carried forward through two revisions because it sat next to the claim being
  corrected. **Re-verify the whole statement you are editing, not only the clause you came to change.**
- **`bl_rna` is authoritative for RNA members — and a small number of real Python-level properties live
  outside it.** The rule above makes `X.bl_rna.properties` the authoritative listing, and for RNA it is. But
  `bpy_types` adds convenience properties in Python that never appear there, and concluding "absent" from an
  empty `bl_rna.properties` result is the *same* class of error the rule exists to prevent. Measured
  2026-09-14 against 5.2.1:

  ```
  'users_id' in [p.identifier for p in bpy.types.Library.bl_rna.properties]  -> False
  'users_id' in dir(bpy.data.libraries[0])                                   -> True
  bpy.data.libraries[0].users_id
    -> [bpy.data.collections['CanonHero'], bpy.data.meshes['HeroMesh'], bpy.data.objects['HeroBody']]
  ```

  `Library.users_id` is exactly what `list_libraries` and `unlink_libraries` need — "which datablocks came
  from this library" — and an implementer who checked only `bl_rna.properties` would conclude it does not
  exist and hand-roll a full `bpy.data` walk instead. **`dir()` on a real instance is the fallback for these**,
  and it is the check that catches both traps at once. Where the two disagree, believe the instance.
- **The response envelope is unchanged**: `{"ok", "data", "error", "warnings", "changed_objects",
  "changed_resources"}`, built by `server/tools/envelope.py:ok()`. Phase 2 adds no new response contract.
- **`bpy` is never imported from `src/blender_mcp/server/`.** That code runs outside Blender.
- **No arbitrary code execution, in any form** (spec Decision #7). No task may add an `exec`/`eval` path, a
  "run this Python" command, or a tool that takes a code string.
- **The plugin↔MCP loop is kept, not collapsed** (spec Decision #9). No task may replace the socket hop with a
  direct call.
- **Every new addon command must appear in `_build_command_handlers()`**, which is what makes it an advertised
  capability (`server_core.py:1156`) and therefore what lets `connection.py:185-190` send it at all. A
  server-side tool whose addon command is missing from that table is dead on arrival.
- **Bump `ADDON_PROTOCOL_VERSION` and `EXPECTED_ADDON_PROTOCOL_VERSION` together**, in the task that first
  changes the handshake — whether by adding a command or by adding a handshake field.
  `tests/test_addon_manager.py:20-21` enforces the pairing. **In this plan that task is Task 1, which lands the
  `writable_output_roots` handshake field and takes `main` from 30 to 31** (the number `docker-blender` already
  uses for exactly that field — §0.3 finding 3). **There is one bump in Phase 2, not two.** Task 3 adds
  `get_session_info`, `session_epoch` and `filepath` in the same protocol generation and **asserts** the pair is
  at 31 rather than bumping again; it bumps to 32 only if Task 1 did not land. An earlier revision had both
  tasks claiming the 30→31 bump independently, which would have produced either a double bump or a silent
  collision.

  > **Correction (2026-09-14, Task 1).** The parenthetical reason above is false. `git log -S` shows
  > `docker-blender` bumped to 31 in `75a7abf` for the **inline image transport**, not for
  > `writable_output_roots`, which was added later on that branch and rode the existing 31. The ruling is
  > unaffected - 31 is simply the next number - but a future port of the inline image transport needs **32**.
  > See `docs/superpowers/plans/PHASE2_TASK_STATE.md` decision 5.

- **No capability may be lost.** `all` must keep advertising every pre-existing tool. The count moves *up* as
  Phase 2 adds tools; `bundles.py:8`'s docstring figure and the test that parses it move with it.
- **Destructive operations require explicit confirmation** (CLAUDE.md). `save_shot` over an existing file,
  `reset_session`, and `unlink_libraries` all take a confirmation parameter; the default is the safe branch.
- **All 647 existing tests pass unmodified.** Following a symbol that moved is allowed; weakening an assertion
  is not.
- **TDD, and prove the test can fail.** Write the failing test, run it, see it fail, implement, then revert the
  fix and confirm the test fails again. Phase 1 found a shipped test asserting a constant against itself.
- **Every Blender-side claim is demonstrated, not argued.** Paste the command and its output. "It should work"
  is not evidence; §0.3 has four entries that existed only because someone reasoned instead of running.

---

## File Structure

| File | Responsibility |
|---|---|
| `docker/blender/` | **New on `main` (ported from `docker-blender`).** Xvfb + Blender + MCP server rig; the Phase 2 acceptance environment. |
| `tests/test_docker_rig.py` | **New on `main` (ported).** Static guards on the rig's wiring. |
| `src/blender_mcp/bundled/addon/output_roots.py` | **New on `main` (ported, then promoted).** Deployment-configured writable roots. Becomes the input to the enforcing path policy. |
| `tests/test_output_roots.py` | **New on `main` (ported, then extended).** Two of its tests assert on `get_addon_info()`, so it requires the wiring below. |
| `src/blender_mcp/addon_manager.py` | **Modify (Task 1).** `AddonHandshake.writable_output_roots` field + its parse; `EXPECTED_ADDON_PROTOCOL_VERSION` 30 → 31. **Modify again (Task 3)** for the session fields, without a second bump. |
| `tests/test_addon_manager.py` | **Modify (Task 1, ported).** The two `writable_output_roots` handshake tests. |
| `scripts/blender_rig.py` | **New.** Launch a live Blender with the addon and run a scripted socket scenario against it; the harness Tasks 2, 6, 7 and 10 report evidence from. |
| `src/blender_mcp/bundled/addon/file_paths.py` | **New.** `bpy`-free path validation: canonicalization, root enforcement, extension and magic-byte checks, overwrite policy. |
| `tests/test_file_paths.py` | **New.** Adversarial coverage for the above. |
| `src/blender_mcp/bundled/addon/session.py` | **New.** Session epoch, file-swap barrier state, `@persistent` `load_post`/`load_post_fail`/`save_post`/`save_post_fail` handlers. (`save_post` maintains `current_filepath` and dirty state only — it does **not** bump the epoch; see Task 3's ruling.) |
| `src/blender_mcp/bundled/addon/handlers/file_lifecycle.py` | **New — created in Task 3** with `get_session_info`, **extended in Task 6** with `open_shot`, `save_shot`, `reset_session`. Task 3 creates it because Task 3's acceptance criteria test `get_session_info`; Task 6 must not re-create it. |
| `src/blender_mcp/bundled/addon/handlers/linking.py` | **New.** `link_canon_library`, `create_override`, `list_libraries`, `reload_library`, `relocate_library`, `unlink_libraries`. |
| `src/blender_mcp/bundled/addon/server_core.py` | **Modify (Task 1).** The `output_roots` import, `_writable_output_roots()`, and the `writable_output_roots` handshake field. **Modify (Tasks 3, 4, 6, 7).** Drain-loop barrier; register the two new handler mixins; extend `_READ_ONLY_COMMANDS` (with `get_session_info` and `list_libraries`) and the session-swap / datablock-replacing constants; add `session_epoch` and `filepath` to `get_addon_info`. |
| `src/blender_mcp/bundled/addon/transaction.py` | **Modify.** `libraries` tracking; session-swap invalidation. |
| `src/blender_mcp/bundled/addon/object_state.py` | **Modify.** Invalidation so a freed datablock is never touched. |
| `src/blender_mcp/server/tools/file_lifecycle.py` | **New.** MCP tools wrapping the addon's file and linking commands. |
| `src/blender_mcp/server/bundles.py` | **Modify.** A new `file_lifecycle` **core module** (`CORE_MODULES`) — **not** a shared bundle added to both modes, which `test_shot_and_asset_modes_share_only_the_core_surface` forbids. Raise the `shot` ceiling and add the new default-surface ceiling; update the docstring tool count. See Task 9 ruling 1. |
| `src/blender_mcp/server/tools/_documentation.py` | **Modify.** `_DESTRUCTIVE_TOOLS` entries for **five** tools (six with `save_shot`'s belt-and-braces entry); **four of the ten deliberately get none**. Plus `openWorldHint` coverage for the five file-touching tools. See Task 9 Step 4. |
| `tests/server/test_bundles.py` | **Modify.** New bundle/mode coverage; deliberate ceiling change. |
| `README.md` | **Modify.** Document the new bundle, the new env var, and the file-path policy. |
| `docs/superpowers/specs/2026-09-11-episode-consistency-architecture-design.md` | **Modify (Tasks 2, 8).** §4.5 corrections; new §4.8 socket trust boundary. |

---

## Task 1: Land the live-Blender acceptance rig on `main`

**Recommended implementer: Opus-class.** *(Re-tiered from Sonnet — the reasoning is recorded rather than
changed silently.)* The superseded rationale read: *"The risky judgement (merge vs. port, and which files) is
decided below; what remains is a precise file port plus a new launcher script with stated verification
commands."* **That is the difficulty criterion this plan explicitly disavows**, in the same words that
mis-tiered Tasks 6 and 7 — "what remains is careful work against a decided design". Tiering here is by
**consequence of getting it wrong**, and Task 1's consequence profile is not small:

1. **Every other task's acceptance evidence runs through this task's artefact.** `scripts/blender_rig.py` is
   named in Tasks 2, 3, 6, 7 and 10, and the handoff makes a live-Blender transcript a hard exit gate for seven
   of the ten tasks. A rig that answers plausibly but exercises the wrong path silently invalidates every one of
   those transcripts — and a transcript is the one form of evidence this plan treats as unfalsifiable.
2. **Getting the port's *scope* wrong has already been demonstrated to break the task**, twice, by the review
   that produced this section: porting `output_roots.py` without its five wiring sites leaves Task 1's own
   Step 3 red and `main` carrying a dead module (§0.3 finding 3), and the accompanying protocol bump collides
   with a bump Task 3 was independently claiming. Neither is hypothetical; both were measured.
3. **It fails its own lint gate on day one unless the ported debt is cleaned** — 36 measured new `ruff` errors,
   which is an acceptance-criterion failure and a handoff-baseline breach in the *first* commit of the phase
   (Step 3b below).
4. **It owns the "what does the rig actually reach" boundary** that Task 10's gate scenario is written against.
   The local and container modes do not have the same reach, and an earlier revision of Task 10 assumed they
   did (see the ruling below and Task 10's "What to build").

None of that is difficult. All of it is consequential, and three of the four defects above were present in a
version of this task that read as settled. If a reviewer prefers to keep this at Sonnet, that is defensible —
but it must be **recorded in TASK_STATE as a deliberate downgrade with its cost if wrong** (invalidated
evidence for seven tasks, a dead module, a protocol collision, or a broken baseline), per the handoff's §05
rule.

### Why

Nothing else in Phase 2 is provable without it. Spec §7 puts "File lifecycle: open/save/reset/link/override/
unlink; reentrancy under Xvfb" in the **Container** column, and §3 records that the addon refuses to start in
`--background` (`server_core.py:152-158`) while `bpy.app.timers` do not fire there either — re-verified on
2026-09-14: a `persistent=True` timer registered in `--background` fires **0** times in 3 s while
`is_registered` stays `True`. Spec §8's Phase 2 row also asks for two things this task delivers: "re-verify the
persistent-timer result with the server actually running" and "`docker-blender` takes `bundles.py`".

### Current state, verified

Per §0.3 finding 2, the rig exists on `docker-blender`:

```
docker/blender/Dockerfile               docker/blender/entrypoint.sh      docker/blender/healthcheck.py
docker/blender/docker-compose.yml       docker/blender/start_server.py    tests/test_docker_rig.py
docker/blender/Dockerfile.dockerignore
src/blender_mcp/bundled/addon/output_roots.py   tests/test_output_roots.py
```

**`Dockerfile.dockerignore` is load-bearing, not incidental.** `tests/test_docker_rig.py:168` reads it
directly (`test_build_context_ignore_file_admits_everything_the_dockerfile_copies` parses its `!`-prefixed
lines against the Dockerfile's `COPY` list). Omitting it makes a ported test fail on a missing file. That is
**nine** files, not six — an earlier revision of the ruling below said six and the list above said eight.

**And `output_roots.py` is wired into five further sites plus two tests** — see §0.3 finding 3 for the
enumerated table. `tests/test_output_roots.py:112` and `:139` construct a `BlenderMCPServer` and assert on
`get_addon_info()["writable_output_roots"]`, so the ported test file **cannot pass** without that wiring.
`docker-blender` carries `ADDON_PROTOCOL_VERSION`/`EXPECTED_ADDON_PROTOCOL_VERSION` at **31** for exactly that
handshake field; `main` is at **30**.

`entrypoint.sh` runs `Xvfb :99 -screen 0 1280x720x24`, copies the addon from the read-only `/repo` mount into
`$HOME/.config/blender/${BLENDER_MAJOR_MINOR}/scripts/addons`, then starts `blender --python
/opt/start_server.py` and the MCP server with `BLENDERMCP_TRANSPORT=http`. `docker-compose.yml` publishes only
`127.0.0.1:8000`, sets `BLENDERMCP_OUTPUT_ROOTS: /output`, forces `platform: linux/amd64` (so it runs emulated
on Apple Silicon — slow but correct), and has a healthcheck that round-trips both Blender's socket and the MCP
endpoint. `docker-blender` is 8 commits ahead of `main` and **39 behind**, and has **no `bundles.py`**.

Locally, Blender 5.2.2 is at `/opt/homebrew/bin/blender` and the host is macOS (Darwin 25.6.0) — **there is no
Xvfb on macOS**, but there is a real display, so a GUI Blender serves the same purpose for a local rig.

### Ruling: port the files to `main`, do not merge the branch

A `git merge docker-blender` into `main` drags 8 commits across 39 commits of Phase 1 divergence, and the
highest-value thing Phase 1 produced (`bundles.py`, the mode/bundle split, the lazy package machinery) is
exactly what `docker-blender` predates. A bad conflict resolution there silently reverts Phase 1. Porting
**nine** files forward is mechanical and reviewable, and it satisfies "`docker-blender` takes `bundles.py`"
more completely than merging would — once the rig lives on `main`, the rig *is* built from a tree that has
`bundles.py`, and the branch becomes redundant rather than needing a second sync forever.

### Ruling: port the **full** `output_roots` wiring, including the 30 → 31 protocol bump

An earlier revision of this task ported `output_roots.py` and its test and stopped there. **That produces a
dead module, a false claim in §0.3, and a red Step 3**, and it was chosen over the alternative without either
option being stated. Both options, and why this one:

- **(a) Port the full wiring here** — the `server_core.py` import, `_writable_output_roots()`, the
  `writable_output_roots` handshake field, `addon_manager.py`'s dataclass field and its parse, and
  `tests/test_addon_manager.py`'s two tests — **and take the 30 → 31 protocol bump with it.**
- **(b) Defer the handshake wiring to a later task**, port only the `bpy`-free module, and strike §0.3 finding
  3's "the addon reports the roots through the handshake" claim until that task lands.

**Chosen: (a).** Three reasons, in order of force:

1. **(b) is not actually available without editing a ported test.** `tests/test_output_roots.py:112` and
   `:139` assert on `get_addon_info()["writable_output_roots"]`. Under (b), Step 3 fails, and the only way to
   make it pass is to delete or weaken two assertions in a file this task is porting verbatim — which the
   handoff's §03 forbids outright ("weakening an assertion is not" allowed). A deferral option that requires
   breaking a non-negotiable constraint on its first step is not a real option.
2. **It fits the plan's existing task boundaries rather than cutting across them.** Every other file in this
   plan is created by the task that first needs it and extended by a later one — `output_roots.py` is created
   here and *promoted* in Task 5; `handlers/file_lifecycle.py` is created in Task 3 and extended in Task 6.
   Splitting one module's wiring across two tasks is the one shape this plan does not use anywhere else.
3. **It resolves the protocol-bump collision instead of deferring it.** `docker-blender` bumped to 31 *for this
   field*. Task 3 Step 5 separately claimed the same 30 → 31 bump for `get_session_info`. Under (a) there is
   exactly one bump in the phase, owned by the first task that changes the handshake, which is what the Global
   Constraints bullet says; Task 3 then asserts the pair is at 31 rather than bumping again. Under (b) the
   collision simply moves to whichever later task picks the wiring up.

**Cost if wrong:** the phase carries a handshake field one protocol generation earlier than strictly needed —
a field that is `[]` on any addon that does not report it, with a test already covering that fallback
(`test_addon_manager.py:220`). That is the cheap direction to be wrong in.

### Ruling: the rig's two modes do not have the same reach, and Task 10 is written against the narrower one

This matters because Task 10's scenario is built on top of it, so state it here rather than discovering it
there. The two modes reach different layers:

| Mode | What it stands up | What a scenario can call |
|---|---|---|
| **Local (macOS)** — `scripts/blender_rig.py` | A GUI Blender with the addon's socket server | **Addon socket commands only** — newline-delimited JSON, straight to `_decode_and_queue_frame`. No MCP server process exists. |
| **Container** — `docker compose up` | Xvfb + Blender + the addon socket **and** the real MCP server on `127.0.0.1:8000` (`entrypoint.sh` starts both; `BLENDERMCP_TRANSPORT=http`) | Both layers — but only the MCP layer is published; Blender's own socket deliberately never leaves the container. |

So there is **no mode in which one scenario can drive both layers**, and the local mode — the one available on
this host, and the fallback the acceptance criteria allow when Docker is unavailable — reaches only the addon
socket. **Task 10's gate scenario is therefore written entirely against addon-level socket commands**, and the
MCP-tool-wrapper layer is verified separately by ordinary `pytest`. See Task 10's "What to build" for the
decision and its justification; `scripts/blender_rig.py`'s docstring must state this boundary so the next
reader does not assume the rig speaks MCP.

### Files

- Port from `docker-blender` (**nine files**): `docker/blender/{Dockerfile, Dockerfile.dockerignore,
  entrypoint.sh, healthcheck.py, docker-compose.yml, start_server.py}`, `tests/test_docker_rig.py`,
  `src/blender_mcp/bundled/addon/output_roots.py`, `tests/test_output_roots.py`.
- Port the wiring into: `src/blender_mcp/bundled/addon/server_core.py` (import, `_writable_output_roots()`,
  the `get_addon_info` field), `src/blender_mcp/addon_manager.py` (the `AddonHandshake` field and its parse,
  plus `EXPECTED_ADDON_PROTOCOL_VERSION` 30 → 31), `src/blender_mcp/bundled/addon/__init__.py`
  (`ADDON_PROTOCOL_VERSION` 30 → 31), `tests/test_addon_manager.py` (the branch's two handshake tests).
- Create: `scripts/blender_rig.py`.
- Modify: `README.md` (a "Live Blender rig" section under Development).

### Steps

- [ ] **Step 1 — Port the nine files.** `git checkout docker-blender -- docker/blender tests/test_docker_rig.py
  src/blender_mcp/bundled/addon/output_roots.py tests/test_output_roots.py` (`docker/blender` carries
  `Dockerfile.dockerignore`; confirm it arrived — `tests/test_docker_rig.py:168` reads it). Read every ported
  file in full before going further; do not assume it fits `main`.
- [ ] **Step 1b — Port the `output_roots` wiring and take the protocol bump.** Per the ruling above and §0.3
  finding 3's table: `server_core.py`'s import, `_writable_output_roots()` and the `writable_output_roots`
  entry in `get_addon_info()`; `addon_manager.py`'s `AddonHandshake.writable_output_roots` field and its parse;
  `tests/test_addon_manager.py`'s two handshake tests; and `ADDON_PROTOCOL_VERSION` /
  `EXPECTED_ADDON_PROTOCOL_VERSION` 30 → 31 **together**. Do not hand-write these — take them from the branch
  and reconcile, since `server_core.py` and `addon_manager.py` have both moved 39 commits on `main`. **This is
  the phase's only protocol bump**; record in TASK_STATE that Task 3 inherits 31 rather than bumping again.
- [ ] **Step 2 — Reconcile the ported files against `main`.** `start_server.py` does
  `from blender_mcp.server_core import BlenderMCPServer` — confirm that resolves against the addon layout on
  `main` and fix it if not. **The Dockerfile installs from `poetry.lock`, not `pyproject.toml`** — it does
  `COPY pyproject.toml poetry.lock` then `poetry install --only main --no-root` (`Dockerfile:51-57`; the
  compose comment saying "from pyproject.toml" is itself inaccurate and should be corrected while you are
  there). So the real reconciliation risk is whether **`main`'s `poetry.lock` is still in sync with its
  `pyproject.toml`** — check it (`poetry check --lock`) before assuming the image builds, and note that the
  now-present untracked `uv.lock` means dependencies may have been resolved through a second tool without
  `poetry.lock` moving. Do not stage `uv.lock`. **Set `BLENDER_MCP_TOOLSETS` explicitly in
  `docker-compose.yml`** (this is the concrete form of "`docker-blender` takes `bundles.py`") — use `shot` so
  the rig exercises the surface the phase gate is about, and document why in a comment.
- [ ] **Step 3 — Run the ported tests.** `.venv/bin/python -m pytest tests/test_docker_rig.py
  tests/test_output_roots.py tests/test_addon_manager.py -v`. These are static guards and need no Docker.
  Expect green; if not, the port is incomplete. **`tests/test_output_roots.py` will fail here if Step 1b was
  skipped** — two of its tests assert on `get_addon_info()`; that failure is the port being incomplete, never
  a reason to touch the test.
- [ ] **Step 3b — Clean the ported lint debt before landing.** Measured 2026-09-14 by running
  `ruff check --config pyproject.toml` over the branch's files: the port introduces **36 new errors** —
  **35 in `tests/test_output_roots.py`** and **1 in `docker/blender/start_server.py`**. `output_roots.py`
  itself and `tests/test_docker_rig.py` are clean. Unfixed, that takes the repo from the handoff's baseline of
  **9,834** to **9,870**, which breaches the per-task gate's `<= 9,834` line (handoff §05) and fails this
  task's own acceptance criterion 5 — **in the phase's first commit.** The breakdown, so the work is
  estimable rather than open-ended:

  | Rule | Count | What it is |
  |---|---|---|
  | `D103` undocumented-public-function | 13 | test functions with no docstring |
  | `ANN001` missing-type-function-argument | 11 | untyped `monkeypatch` / `tmp_path` params |
  | `PLC0415` import-outside-top-level | 6 | deferred imports inside the test helpers |
  | `PLC2701` import-private-name | 3 | reaching into private helpers |
  | `ANN202` missing-return-type-private-function | 1 | |
  | `RUF105` noqa-comments | 1 | |
  | `I001` unsorted-imports | 1 | (`--fix`-able) |

  **Clean all 36 rather than restating the baseline**, and state the reason in the commit message: the
  alternative — raising the repo-wide ceiling to 9,870 on the phase's first commit — would spend the "no new
  debt" invariant that every later task's gate is measured against, to avoid roughly an hour of docstrings and
  type annotations on a file this task is already touching. None of the 36 is a genuine pre-existing-debt case;
  they are all mechanical. If a reviewer nonetheless overrules this, the **exact** new baseline (9,870) must be
  written into TASK_STATE, the handoff's §05 gate line and §08's baseline table in the same commit, with the
  justification — a ceiling that drifts without all three moving together is how the gate stops meaning
  anything. Re-run the count yourself; do not trust this one.
- [ ] **Step 4 — Write `scripts/blender_rig.py`.** A developer CLI (not imported by the server) that:
  launches `/opt/homebrew/bin/blender` **with a GUI** (macOS local mode) or reports that the caller should use
  the container (Linux/CI mode); enables the addon and starts the socket server without UI interaction (reuse
  `docker/blender/start_server.py`'s approach); waits for the port; runs a caller-supplied scenario module that
  speaks the newline-delimited JSON protocol directly; prints every request/response pair; and exits non-zero on
  any failure. It must take the `.blend` fixtures it needs as arguments and must never write outside a
  caller-supplied directory.

  **Its module docstring must state the reach boundary from the third ruling above, in as many words:** this
  rig speaks the **addon's** socket protocol and does **not** stand up an MCP server, so a scenario driven
  through it can call `open_shot` (the addon command) but not `open_shot` (the MCP tool) and not
  `get_addon_status` (which is server-side only — `server/tools/core.py`, calling `force_addon_handshake()` in
  the MCP process). The container is the only mode that runs a real MCP server, and it publishes *only* that
  layer. Tasks 2, 3, 6, 7 and 10 all report evidence from this script; a reader who assumes it exercises
  "MCP tool → socket → addon" will write a scenario that cannot run.
- [ ] **Step 5 — Re-verify the persistent-timer claim with the server actually running.** Spec §4.5's caveat:
  the `persistent=True` survival result was verified in `--background`, "where §3 says the server would not have
  started". Use the rig: start the server, confirm a `ping` round-trips, run `wm.open_mainfile` on a fixture
  `.blend` **from the Blender Python console / a scripted operator call (not from a queued MCP command — that is
  Task 2)**, then confirm `bpy.app.timers.is_registered(server.drain_command_queue)` is still true *and* that a
  second `ping` still round-trips. Record both the registration state and the round-trip, because a registered
  timer that never fires again would pass the first check and fail the phase.
- [ ] **Step 6 — Document the rig** in README under Development, including the macOS-vs-container split, the
  amd64 emulation note, and the fact that Blender's socket has no authentication and must stay on loopback.
- [ ] **Step 7 — Gates and commit.** Per-task gate from the handoff.

### Acceptance criteria

1. `pytest tests/test_docker_rig.py tests/test_output_roots.py tests/test_addon_manager.py` passes on `main`,
   **with no assertion in any ported test file edited or removed.**
2. `docker compose -f docker/blender/docker-compose.yml up --wait` reaches healthy, **or**, if Docker is
   unavailable in the session, `scripts/blender_rig.py` completes a `ping` round-trip against a local GUI
   Blender. One of the two must be demonstrated with pasted output; "the rig exists" is not acceptance.
3. The container advertises a bundle-selected surface — `BLENDER_MCP_TOOLSETS` is set in compose and the
   running server's `tools/list` count matches `scripts/measure_catalog.py` for that selection. **If Docker is
   unavailable** (the same escape hatch criterion 2 carries, and for the same reason — `tools/list` comes from
   the MCP server, which only the container runs), the compose setting is verified statically and
   `scripts/measure_catalog.py shot`'s count is recorded against it, with the un-run half stated explicitly as
   not demonstrated rather than implied. Do not claim container parity you did not observe.
4. **Step 5's evidence is pasted into TASK_STATE**: timer still registered *and* a post-load `ping` answered.
5. **`ADDON_PROTOCOL_VERSION` and `EXPECTED_ADDON_PROTOCOL_VERSION` are both 31**, `tests/test_addon_manager.py`
   is green, and `get_addon_status` reports `writable_output_roots` end to end — the wiring is live, not just
   ported. TASK_STATE records that this is the phase's only protocol bump.
6. Full suite still 647 passed plus the ported tests; repo-wide ruff/format/basedpyright counts **at or below**
   the handoff's baselines — specifically `ruff check .` still reports **`<= 9,834`**, which requires Step 3b's
   36 errors to have been cleaned (or, if the reviewer overruled that, the new number written into TASK_STATE,
   the handoff's §05 gate line and §08's baseline table in the same commit).

---

## Task 2: Decide the reentrancy strategy by experiment

**Recommended implementer: Opus-class.** This resolves a design question the spec explicitly left undecided, by
designing an experiment whose result must be trusted for the rest of the phase; a plausible-but-unfalsified
answer here propagates into every later task.

### Why

Spec §4.5, verbatim: *"`wm.open_mainfile` from inside the drain-timer callback frees that callback's context.
Untestable headlessly (timers do not fire in `--background`), so it needs the Xvfb rig. The obvious mitigation —
defer the load and answer first — is wrong: it commits a success response before the load can fail on a missing
file, bad permissions, a corrupt `.blend`, or an allowlist rejection, and the envelope has no way to report that
afterward. A correct design needs either a two-phase `open_shot` (validate-then-load, answering after validation
but before the swap) or an async job with polling."*

Both offered options exist only because the spec assumes the callback cannot answer *after* the swap. §0.3
finding 6 shows that assumption was never tested, and the rig from Task 1 can test it directly.

### Hazards carried forward (all three of §4.5's, explicitly)

- **Reentrancy** — the load happens inside `drain_command_queue`'s own frame (`server_core.py:267-321`). The
  frame holds `command`, `client` (a plain Python socket) and `response`; `self` lives on
  `bpy.types.blendermcp_server`, which is module state, not file data. Nothing in the frame is an RNA reference.
  Whether Blender tolerates the operator call from a timer is the empirical question.
- **Ordering** — up to 8 commands per tick (`_MAX_COMMANDS_PER_TICK = 8`, `:331`) with a 0.02 s budget, and up
  to 256 queued (`:330`). A file swap has no defined ordering against the rest of that batch, and multiple
  client processes may have queued work against the *old* file. Task 3 owns the fix; Task 2 must characterise
  the behaviour so Task 3's fix is aimed at something real.
- **Rollback** — `transaction.py`'s snapshot refers to the pre-load database. Task 4 owns the fix; Task 2 must
  **not** route its spike through `mutation_transaction`, or it will trigger §0.3 finding 7 during the spike.

### Current state, verified

See §0.2 for the exact drain-loop code, and §0.3 findings 5 and 6: `wm.open_mainfile` works in `--background`
(so the operator itself is fine); timers do not fire there (so the *callback context* question needs the rig);
and `tests/server/test_threading.py` can already exercise the protocol half headlessly.

### Files

- Create (throwaway, deleted before the task's commit): a spike under the session scratchpad, driven by
  `scripts/blender_rig.py`.
- Modify: `docs/superpowers/specs/2026-09-11-episode-consistency-architecture-design.md` §4.5 — replace the
  "Open hazards" reentrancy bullet with the decided design and a dated correction block, in the same style §4.6
  already uses for its measurement correction.
- Modify: `docs/superpowers/plans/PHASE2_TASK_STATE.md` — the decision record.

### Steps

- [ ] **Step 1 — State the decision rule before running anything.** Copy §0.4's rule into TASK_STATE verbatim,
  with today's date, so the result cannot be reinterpreted afterwards.
- [ ] **Step 2 — Build the minimal spike.** A temporary addon command `__spike_open` registered into
  `_build_command_handlers()` (and **added to `_READ_ONLY_COMMANDS` for the duration of the spike**, so it
  bypasses `mutation_transaction` — see the rollback hazard above) that calls
  `bpy.ops.wm.open_mainfile(filepath=<fixture>)` and then returns a normal result dict.
- [ ] **Step 3 — Run it under the live rig and record, precisely, what happens.** At minimum:
  (a) does Blender survive the call; (b) does `drain_command_queue` return normally; (c) does
  `client.sendall(...)` after the load reach the client; (d) is the timer still registered; (e) does the *next*
  queued command execute against the new file; (f) what does `bpy.context` look like immediately after the load
  (scene name, `bpy.data.filepath`); (g) what happens to the other commands queued in the same tick.
- [ ] **Step 4 — Run the same spike with a deliberately failing load** — a path that does not exist, a
  directory, a non-`.blend` file with a `.blend` extension, and a `.blend` with corrupted magic bytes. Record
  whether the operator raises, returns `{'CANCELLED'}`, or reports through the info/report system, and whether
  the callback survives each. **Expected, from `--background` measurement on 2026-09-14: it raises
  `RuntimeError` on all four and never returns `{'CANCELLED'}`, and the database is left untouched** (see Task
  6's "three measured behaviours" and Task 3's epoch ruling). What this step adds that the headless measurement
  cannot is the *callback survival* half — whether the drain frame lives through a raise inside a timer. Report
  a divergence from the expected operator behaviour as a finding rather than folding it in silently.
- [ ] **Step 5 — Verify the `load_post_fail` backstop.** Register a `@persistent` `load_post_fail` handler
  (verified present in 5.2.1 — see §0.4) and confirm it fires on each failure mode from Step 4. This is what
  makes a post-swap answer truthful in the "callback survives" branch and what makes the async job's status
  truthful in the other.
- [ ] **Step 6 — Apply the decision rule and record it.** Write the decision, the evidence for it, and the
  rejected alternative with the specific observation that rejected it. If the rule's two branches do not cleanly
  apply (e.g. the callback survives but `sendall` fails intermittently), **stop and escalate rather than
  inventing a third branch** — that is exactly the ambiguity this rule exists to avoid resolving informally.
- [ ] **Step 7 — Also record the ordering observation** from Step 3(g), verbatim, as Task 3's input.
- [ ] **Step 8 — Delete the spike.** No spike code lands. Correct spec §4.5 and commit the decision as a docs
  commit.

### Acceptance criteria

1. TASK_STATE contains the decision rule **dated before** the evidence, the evidence, the decision, and the
   rejected alternative with its disqualifying observation.
2. All seven observations from Step 3 and all four failure modes from Step 4 are recorded with pasted output.
3. `load_post_fail` is demonstrated firing on at least the missing-file and corrupt-file cases.
4. Spec §4.5's reentrancy bullet is rewritten to the decided design, with a dated correction block preserving
   the old text.
5. `git status` shows no spike code left in the tree.
6. This task changes no production code. `pytest` is still 647 passed (plus Task 1's additions).

---

## Task 3: Drain-loop file-swap barrier, session epoch, and failure handlers

**Recommended implementer: Opus-class.** This edits the one callback every command in the server traverses, and
its failure mode is a hang or a silently mis-sequenced mutation — the hardest class to detect after the fact.
`impact` on `drain_command_queue` returns `risk: UNKNOWN` with zero resolved callers (it is a callback passed by
reference), so the graph cannot help; the safety net is reasoning plus the existing threading harness.

### Why

Spec §4.5's **ordering** hazard: *"The drain loop processes up to 8 commands per tick; a deferred load has no
defined ordering against queued commands, and multiple client processes may interleave against a file
mid-swap."* Spec §3 adds that multiple server processes against one Blender is documented behaviour
(`README.md:85-89` and `:277-294`), so "one client at a time" is not an invariant this design may assume. And §4.5's `open_shot`
row says the swap *"requires a re-handshake"* because the advertised capability set is scene-dependent
(`server_core.py:774,784,793` read `bpy.context.scene.blendermcp_use_*`, which are per-`.blend` values).

### Current state, verified

- `drain_command_queue` (`server_core.py:267-321`) dequeues up to 8 commands per tick with no notion of a
  command that invalidates the ones behind it.
- `_decode_and_queue_frame` (`:342-391`) is the only entry point into the queue and already answers protocol
  violations without touching `bpy` — the right precedent for a barrier rejection.
- **The server-side re-handshake mechanism already exists**: `connection.py:350-370` `force_addon_handshake()`,
  exposed through `server/tools/core.py:57` `get_addon_status`. What is missing is not the mechanism but the
  **signal** that a re-handshake is needed. The spec treats this as unbuilt; it is half-built.
- `get_addon_info` (`server_core.py:1144-1158`) is where a signal can be published, and
  `connection.py:185-190` is where the client already consumes `capabilities`.
- `bpy.app.handlers` in 5.2.1 provides `load_pre`, `load_post`, `load_post_fail`, `save_pre`, `save_post`,
  `save_post_fail` and `@persistent` (introspected 2026-09-14).
- **A *failed* `open_mainfile` leaves the old database completely untouched** — verified 2026-09-14 against
  5.2.1 across every failure mode that actually applies to *opening* a file (missing file, a directory, a
  non-`.blend` with a `.blend` extension, a corrupt magic header, an empty path): `bpy.data.filepath` is
  unchanged and the object count is unchanged. (**"A read-only destination" is deliberately absent from this
  list**: it is a *save* failure mode, not an open one — opening a file from a read-only location succeeds
  normally. The six-item list in Task 6 behaviour 1 is correct *there*, because it covers all three file
  operators; it must not be copied into a sentence about `open_mainfile` alone without dropping that item.)
  See the epoch ruling below; this observation is what decides it.

### Ruling — `session_epoch` does **not** increment on a failed swap

An earlier draft of this task specified incrementing the epoch "once per swap, and once on a failed swap too."
**That is wrong, and the evidence above is why.** The epoch exists for exactly one purpose: to tell a client
that its cached capability set (`connection.py:185-190`) is stale because the `.blend` behind it changed. After
a failed `open_mainfile` the `.blend` behind it has *not* changed — same `filepath`, same datablocks, same
scene flags, therefore the same `capabilities`. Bumping the epoch there would invalidate every connected
client's cache and force a pointless re-handshake across every process, on an event that changed nothing.

**Specification:** increment `session_epoch` **only when the advertised capability set can actually have
changed** — i.e. from the `load_post` handler and the successful `reset_session` path, never from
`load_post_fail` / `save_post_fail`, **and never from `save_post`**. A failure is reported through
`last_load_error` / `last_save_error` and the command's own error response, which is where a failure belongs.

**A successful save does not bump the epoch either, and for the ruling's own reason.** An earlier revision of
this specification listed `save_post` among the increment triggers, which contradicts the paragraph above it:
the epoch's sole purpose is to tell a client its cached capability set is stale, and **a save changes no
capability**. Verify it rather than taking it on faith — `bpy.context.scene.blendermcp_use_polyhaven` /
`_use_sketchfab` / `_use_nd` (and the rest of the `blendermcp_use_*` flags `server_core.py:774,784,793` read)
are identical before and after `wm.save_mainfile`, and `_build_command_handlers()` returns the same key set,
so `get_addon_info`'s `capabilities` payload is byte-identical across a save. Bumping there would invalidate
every connected client's cache and force a re-handshake across every process on an event that changed nothing
they cache — the exact cost this ruling rejects on the failed-swap path. The one thing a save *does* change,
the file path, is already surfaced as `get_session_info`'s `current_filepath` field (item 4 below) and needs no
epoch movement to be observable. `save_post` still maintains `current_filepath` and the dirty state; it just
does not touch the counter.

**The barrier is unaffected**: queued commands behind a swap
attempt are still drained and answered with an error (see item 2), because at dequeue time the outcome is not
yet known — the barrier is driven by "a swap command was processed", not by "the epoch moved".

If an implementer wants to overrule this, the cost of being wrong is a re-handshake storm under a client that
retries a bad path, so record the justification in TASK_STATE rather than changing it silently.

### What to build

1. **`src/blender_mcp/bundled/addon/session.py`** — module state, `bpy`-light, holding:
   - `session_epoch: int`, incremented on every **completed file swap** (`load_post`, successful
     `reset_session`) and **not** on a failed one — **and not on a save**, successful or otherwise (see the
     ruling above);
   - `last_load_error: str | None` / `last_save_error: str | None`, set by the failure handlers;
   - `current_filepath: str | None`;
   - `@persistent` handlers for `load_post`, `load_post_fail`, `save_post`, `save_post_fail` that maintain the
     above, registered once at addon `register()` and removed at `unregister()` (idempotently — re-enabling the
     addon must not stack duplicate handlers, which is the classic Blender handler bug).
2. **A file-swap barrier in `drain_command_queue`.** When a command in `_SESSION_SWAP_COMMANDS`
   (`open_shot`, `reset_session`, and `save_shot` only if Task 2's decision requires it) is dequeued:
   - it is the **last** command processed in that tick — `break` after sending its response, never continuing
     the batch against a different file;
   - every command already sitting in the queue when the swap **is attempted** is **drained and answered with an
     explicit error**, not executed — including when the swap failed, because at dequeue time those commands
     were queued against an assumption that is no longer safe to act on and the outcome was not yet known. It
     must be a normal `{"status": "error", ...}` response on the right socket, so no client hangs.
   - **The error carries the current `session_epoch`; it does not assert that the epoch moved.** On a successful
     swap it tells the client to re-handshake. On a failed swap the epoch is unchanged (see the ruling above),
     so the error says the batch was discarded because a swap was attempted and names the unchanged epoch — a
     client comparing epochs then correctly skips a pointless re-handshake and simply resends. Wording that
     hard-codes "the epoch changed" would be a lie on the failure path.
   - a command that arrives *after* the swap is serviced normally.
3. **`session_epoch` and `filepath` added to `get_addon_info`'s payload**, and surfaced through
   `AddonHandshake` → `get_addon_status`. A client that sees the epoch move knows its cached capability set is
   stale. **Do not remove or rename any existing handshake field** — `connection.py:186` gates on
   `capabilities` and `src/blender_mcp/addon_manager.py` parses the rest.
4. **A new `get_session_info` addon command** (read-only) returning epoch, filepath, dirty state, last load/save
   error, and the library list summary. This is the poll surface if Task 2 selected the async branch (under that
   name — see §0.4; there is no separate `get_session_status`), and a cheap liveness/diagnostic command either
   way.

   **This task creates its owning file: `handlers/file_lifecycle.py`, containing `get_session_info` only.**
   An earlier revision of this plan gave `get_session_info` no home — it was specified here while the File
   Structure table placed it in `handlers/file_lifecycle.py`, which Task 6 does not create until three tasks
   later. The file is created here, for two reasons that are not preferences: this task's own acceptance
   criteria already assert that `get_session_info` reports `last_load_error` while leaving the epoch unmoved
   (Step 2), which is untestable if the command does not yet exist; and this is how the rest of the plan
   sequences file creation — whichever task first needs a file creates it, and a later task extends it
   (`output_roots.py` is created in Task 1 and promoted in Task 5, the same shape). **Task 6 extends this
   module** with `open_shot` / `save_shot` / `reset_session`; it does not create it. No new row is added to the
   File Structure table — the file's listed home is unchanged, only its creating task is now stated.

   Register the mixin in `server_core.py:78-97` and the command in `_build_command_handlers()`, and add
   `get_session_info` to `_READ_ONLY_COMMANDS` so it bypasses `mutation_transaction`.

### Hazards carried forward

- **Reentrancy** — implement whichever protocol Task 2 decided, and cite the decision by its TASK_STATE section.
  Do not re-litigate it here.
- **Ordering** — this task *is* the ordering fix. The barrier must hold for the multi-process case, which means
  the rejection must be driven by observed epoch change in the addon, not by anything a single client tracks.
- **Rollback** — `_SESSION_SWAP_COMMANDS` must not be wrapped in `mutation_transaction`. Task 4 is what makes
  that safe and enforced; this task's barrier and Task 4's invalidation must agree on the same constant. Land
  the constant here and have Task 4 consume it. **Task 4 adds a *second*, separate constant
  (`_DATABLOCK_REPLACING_COMMANDS`, for `reload_library` / `relocate_library` / `unlink_libraries`) which also
  bypasses the transaction but does **not** trip this task's barrier** — those commands replace linked content
  in place, they do not swap the session. Do not merge the two sets; see Task 4 item 2a.

### Files

- Create: `src/blender_mcp/bundled/addon/session.py`,
  `src/blender_mcp/bundled/addon/handlers/file_lifecycle.py` (**`get_session_info` only**; Task 6 extends it),
  `tests/test_session_state.py`.
- Modify: `src/blender_mcp/bundled/addon/server_core.py`, `src/blender_mcp/bundled/addon/__init__.py`
  (handler registration/unregistration), `src/blender_mcp/addon_manager.py` (handshake fields),
  `src/blender_mcp/server/connection.py` (carry the new fields), `src/blender_mcp/server/tools/core.py`
  (surface them in `get_addon_status`), `tests/server/test_threading.py` (barrier coverage),
  `tests/test_addon_manager.py`.

### Steps

- [ ] **Step 1 — Extend the existing threading harness rather than building a second one.**
  `tests/server/test_threading.py:30-107` already AST-lifts `BlenderMCPServer` against a stub `bpy` and pumps
  the drain loop over a real socket. Add a stub `bpy.ops.wm.open_mainfile` to that namespace that records the
  call and mutates stub state. **Everything in this task is testable with no Blender at all** (§0.3 finding 6);
  do not defer it to the rig.
- [ ] **Step 2 — Write the failing tests first.** At minimum:
  - queueing `[cmd_a, open_shot, cmd_b, cmd_c]` in one tick results in `cmd_a` executed, `open_shot` executed
    and answered, and `cmd_b`/`cmd_c` **answered with an error, not executed** — assert on the stub's recorded
    call list, not just on the responses;
  - **every** queued command receives exactly one response — no client is left waiting. Assert the count of
    responses equals the count of commands sent, on both sockets in a two-client test;
  - `session_epoch` increments exactly once per **successful** swap, and **does not move at all** on a failed
    one (the ruling above) — assert both directions, since only the negative assertion catches a regression;
  - **`session_epoch` does not move on a *successful save* either** — drive `save_post` and assert the counter
    is identical before and after, while `current_filepath` *does* update. This is the assertion that pins the
    ruling's own logic (a save changes no capability, so it must not invalidate any client's cache), and it is
    the case an earlier revision of this plan had backwards, so it needs its own named test rather than being
    folded into the swap test;
  - a `load_post_fail` sets `last_load_error`, and `get_session_info` reports it, **while leaving the epoch
    where it was**;
  - the handlers are registered exactly once after a simulated disable/enable cycle.
- [ ] **Step 3 — Run them and watch them fail.** Record the failure counts.
- [ ] **Step 4 — Implement `session.py`**, then the barrier, then the handshake fields, then
  `handlers/file_lifecycle.py` carrying **`get_session_info` only** (item 4 above) — registered as a mixin, in
  `_build_command_handlers()`, and in `_READ_ONLY_COMMANDS`. Task 6 extends that same module; leave it
  extensible and say so in its docstring. Keep the barrier logic small enough to read in one screen; if it is
  not, the state belongs in `session.py`, not in the loop.
- [ ] **Step 5 — Assert the protocol pair is already at 31; do not bump it again.** **Task 1 owns the phase's
  single 30 → 31 bump**, taken with the `writable_output_roots` handshake field (see Task 1's second ruling and
  the Global Constraints bullet). An earlier revision of this step claimed the same 30 → 31 bump here, which —
  had both tasks done it — would have produced a double bump or a silent collision. Read both constants,
  confirm they are 31 and equal, and add the session fields inside that same protocol generation. **Bump to 32
  only if Task 1 did not land**, and say so in TASK_STATE. `tests/test_addon_manager.py:20-21` enforces the
  pairing either way.
- [ ] **Step 6 — Prove the tests can fail.** Revert the barrier, re-run, confirm the ordering tests fail and the
  no-hang test fails; restore.
- [ ] **Step 7 — Live check on the rig.** With the real server running, send a batch containing a real
  `open_shot`-shaped spike command and confirm the barrier behaves as the headless tests predict. If it does
  not, the headless stub is lying and must be corrected before the task is done.
- [ ] **Step 8 — Gates and commit.**

### Acceptance criteria

1. Every test in Step 2 exists, passes, and has been demonstrated to fail with the implementation reverted
   (both failure counts recorded).
2. **No-hang is asserted mechanically**, not argued: a test sends N commands across two sockets spanning a swap
   and asserts N responses arrive within a bounded time.
3. `session_epoch` is visible end to end, verified against a live Blender — **and be precise about which layer
   the verification reaches**, per Task 1's third ruling. The rig's local mode speaks the addon socket only, so
   what it can demonstrate live is `get_addon_info`'s and `get_session_info`'s payloads carrying the epoch.
   That the *server-side* `get_addon_status` then surfaces it is the tool-wrapper half, and it is proven the way
   this repo proves every tool wrapper: an ordinary `pytest` test calling the tool function directly against
   `stub_blender_connection` and asserting the field survives `AddonHandshake` (see
   `tests/server/tools/test_scene_validate.py:67` for the pattern). If Docker *is* available, the container
   runs a real MCP server and can demonstrate the whole chain — record that as a supplement, and do not claim
   it when Docker was not available.
4. Handler registration is idempotent across addon disable/enable — verified, not assumed.
5. Protocol version asserted already at 31 in both places (bumped to 32 only if Task 1 did not land, per Step
   5); `tests/test_addon_manager.py` green.
6. Full suite green; repo-wide lint/type counts unchanged.

---

## Task 4: Make rollback survive a file swap, and track `libraries`

**Recommended implementer: Opus-class.** This is the data-destruction task. The current code will delete the
entire contents of a freshly opened `.blend` on a rollback path that Phase 2 is about to make reachable, and the
fix has to be correct on the first attempt because the failure is unrecoverable for the user.

### Why

Spec §4.5: *"`transaction.py` backups reference datablocks that a load frees. `open_shot` must invalidate the
transaction state, not merely clear undo."* And §3: *"Rollback does not track `libraries` — a failed link leaks
a library datablock."*

§0.3 finding 7 establishes that the first of those is understated. Restating it as a mechanism, because the
implementer must be able to reproduce it deliberately:

- `open_shot` will not be in `_READ_ONLY_COMMANDS` (`server_core.py:813-870`), so `_run_handler:1131` wraps it
  in `mutation_transaction`.
- `Transaction.begin()` (`transaction.py:186-189`) snapshots `session_uid`s of the **pre-load** database.
- The handler loads a new file. Every datablock in it has a `session_uid` absent from that snapshot.
- Any exception afterwards — including `_run_handler:1136-1138` converting a handler's returned failure shape
  into `HandlerReportedError` **inside** the `with` block — reaches `mutation_transaction`'s `except` at
  `transaction.py:264-266` and calls `rollback()`.
- `rollback()` (`:191-198`) calls `restore_object_states()` on `ObjectState`s holding freed RNA pointers, then
  `_remove_datablocks(_new_datablocks(self._before_ids, ...))`, which now enumerates **the whole new file** and
  removes it, objects first.

### Current state, verified

- `transaction.py:17-36` — `_TRACKED_COLLECTIONS`, 18 entries, no `libraries`.
- `transaction.py:90-112` — `_remove_datablocks` wraps each removal in `contextlib.suppress(Exception)`, so a
  use-after-free would be *silent*, not loud.
- `object_state.py:35-59` — `ObjectState` stores `self.obj`, `self.parent`, `self.collections`,
  `self.materials`, `self.geometry_backup`: all live RNA references.
- `object_state.py:151-161` — `discard_backup()` calls `collection.remove(self.geometry_backup, do_unlink=True)`
  on the success path, inside `suppress(Exception)`.
- `mutation_transaction` has exactly **one** production caller, `server_core.py:1131` (GitNexus `impact`:
  `risk: LOW`, 1 direct caller). **Confirmed by text search 2026-09-14, and the single-caller reading is
  correct.** `rg -n 'mutation_transaction' --glob '*.py' src` returns five other hits and **none of them is an
  import**: `transaction.py:225` (the definition), `server_core.py:35` (the import) and `:811`/`:1066`
  (comments), plus four *prose* mentions in docstrings and comments inside the liquid handlers —
  `handlers/liquid/shot.py:449`, `handlers/liquid/quality.py:26`,
  `handlers/liquid/inspection_and_setup.py:700`, `handlers/liquid/manifest.py:5`. Those modules rely on the
  dispatcher wrapping *them*; they do not call it. **No `handlers/liquid/*` module imports
  `mutation_transaction`**, so changing its signature does not reach them.

### What to build

1. **`_SESSION_SWAP_COMMANDS` (defined in Task 3) bypasses the transaction entirely.** In `_run_handler`, a
   session-swap command takes the same branch as a read-only command: no snapshot, no rollback, no
   `undo_push`. A file load is not a mutation that can be rolled back; pretending otherwise is the bug.
   This must be **enforced by a test that fails if the command is ever added to the wrapped path**, not by a
   comment.
2. **`Transaction.invalidate()`** — for the case where a swap happens while a transaction *is* open (a handler
   that loads a file as a side effect, now or in future). It drops `_before_ids`, drops `_states` without
   touching them, and marks the transaction so `rollback()` becomes a no-op that returns a warning. `session.py`
   calls it from the `load_post` handler.

2a. **The same hazard reaches `reload_library`, `relocate_library` and `unlink_libraries`, and `load_post` does
   not fire for any of them. This is the item an earlier revision of this task missed entirely, and it is the
   phase's headline data-destruction hazard reappearing on three more commands.**

   Unlike `open_shot`, none of those three is a session swap, so none is in `_SESSION_SWAP_COMMANDS`, so
   `_run_handler:1131` **does** wrap each in `mutation_transaction`. Measured 2026-09-14 against 5.2.1 (§0.3
   finding 7 carries the transcript):

   | Operation | Handlers fired | Effect on `session_uid`s |
   |---|---|---|
   | `lib.reload()` (`reload_library`, and `relocate_library` after assigning `filepath`) | `blend_import_pre`, `blend_import_post` — **never `load_post`** | **every** datablock linked from that library gets a fresh one (3 of 3 measured) |
   | `bpy.data.libraries.load(link=True)` / `load(link=False)` (`link_canon_library`) | `blend_import_pre`, `blend_import_post` | new datablocks, fresh uids — **correctly** "created by this request" |
   | a **failed** `lib.reload()` (invalid path) | **none** | unchanged — it raises before touching anything |
   | `bpy.data.orphans_purge(...)`, `bpy.data.libraries.remove(...)` (`unlink_libraries`) | **none at all** | datablocks are *freed*, not re-created |

   Two consequences, and they point in opposite directions:

   - **A reload inside an open transaction is the `open_shot` catastrophe in miniature.** `Transaction.begin()`
     snapshots the pre-reload uids; `lib.reload()` replaces every one of them; any later raise — including
     `_run_handler:1136-1138`'s own `HandlerReportedError` conversion of a returned failure shape — makes
     `_new_datablocks()` classify **the entire reloaded library's contents** as newly created and
     `_remove_datablocks()` delete them, silently, inside `suppress(Exception)`.
   - **But `blend_import_post` is not a usable discriminator**, because `link_canon_library` fires it too — and
     that command's new datablocks genuinely *are* this request's and genuinely *must* roll back (item 4
     below). **Listening on `blend_import_post` and invalidating unconditionally would disarm the rollback that
     item 4 exists to provide.** That is why "also register on `blend_import_post`" is the wrong shape of fix,
     and it is worth stating because it is the obvious one.

   **Specification — the invalidation is command-scoped, not handler-scoped, because no handler can tell the
   two cases apart:**

   1. **Add `_DATABLOCK_REPLACING_COMMANDS = frozenset({"reload_library", "relocate_library",
      "unlink_libraries"})`** alongside Task 3's `_SESSION_SWAP_COMMANDS`, and route it around
      `mutation_transaction` in `_run_handler` the same way — no snapshot, no rollback. The justification is
      item 1's, verbatim: replacing or freeing datablocks the transaction did not create is **not a mutation
      that can be rolled back**, and pretending otherwise is the bug. These commands are **not** read-only and
      must **not** be added to `_READ_ONLY_COMMANDS` as a shortcut (Task 6's hazard note says why); use the
      dedicated constant. Enforce it with a test that fails if any of the three ever enters the wrapped path.
   2. **Keep `load_post` as Task 4's whole-database invalidation trigger** (item 2 above) — that is the correct
      and sufficient handler for a *file swap*, and it is the one that fires for it.
   3. **Additionally register a `@persistent` `blend_import_post` handler that invalidates only when a
      module-level "replace in progress" flag is set** by the `reload_library` / `relocate_library` handler
      immediately before it calls `lib.reload()`, and cleared in a `finally`. This is defence in depth for the
      case where a *future* handler reloads a library as a side effect from inside a transaction that item 1
      does not cover. The flag is what keeps `link_canon_library` unaffected. If this proves to add more
      machinery than it prevents, dropping it is defensible — **dropping item 1 is not.**
   4. **For `unlink_libraries` there is no handler at all**, so the handler route is unavailable in principle
      and item 1 is the *only* mechanism. Say so in the handler's docstring rather than leaving the absence
      looking like an oversight.

   **Which handlers actually fire for every command that can replace or free datablock contents in place, as
   verified:** `load_post` for `open_shot` / `reset_session`; `blend_import_pre` / `blend_import_post` for
   `reload_library`, `relocate_library` **and** `link_canon_library`; **nothing** for `unlink_libraries`. That
   list is the answer to "listen on whichever handlers actually fire", and it is also the demonstration that
   handlers alone cannot carry this fix.
3. **`ObjectState.invalidate()`** — clears `obj`, `parent`, `collections`, `materials` and, critically,
   `geometry_backup` **without calling `remove()` on it**, because after a load that datablock is gone and the
   removal would be a use-after-free that `suppress(Exception)` would hide.
4. **`libraries` added to `_TRACKED_COLLECTIONS`**, so a failed `link_canon_library` removes the library
   datablock it created instead of leaking it. This needs care: `bpy.data.libraries.remove(lib)` on a library
   that still has users will orphan or delete those users. The rollback must remove the linked datablocks first
   (they are already tracked, and `_remove_datablocks` already processes objects first and everything else in
   reverse creation order) and the library last. **Verify the ordering empirically against a real link**, and if
   `libraries` cannot be safely removed generically, say so and add it as a *reported* leak in the result's
   `warnings` rather than a silent one — a documented limitation beats a wrong removal.

### Files

- Modify: `src/blender_mcp/bundled/addon/transaction.py`, `src/blender_mcp/bundled/addon/object_state.py`,
  `src/blender_mcp/bundled/addon/server_core.py`, `src/blender_mcp/bundled/addon/session.py`.
- Modify: `tests/test_mutation_transaction.py` (extend; do not weaken).
- Create: `tests/test_transaction_session_swap.py`.

### Steps

- [ ] **Step 1 — Reproduce the catastrophe first.** Write a test, against the existing stubbed-`bpy` harness in
  `tests/test_mutation_transaction.py`, that: begins a transaction; replaces the stub `bpy.data` contents
  wholesale with new datablocks carrying fresh `session_uid`s (simulating a load); raises; and asserts that
  rollback removed **all** of them. **This test must pass against the unmodified code.** It is the proof the
  hazard is real, and it is what the fix then inverts. Record its output.
- [ ] **Step 1b — Reproduce the *reload* catastrophe too, separately.** Same harness, but instead of replacing
  `bpy.data` wholesale, replace only the datablocks attributed to one stub `Library` with fresh
  `session_uid`s — the shape `lib.reload()` actually produces (§0.3 finding 7's transcript: 3 of 3 uids
  changed). Assert rollback removes all of them **against the unmodified code**. This is a distinct
  reproduction from Step 1 because it is reachable by a *non*-session-swap command and therefore is not fixed
  by item 1's `_SESSION_SWAP_COMMANDS` bypass alone. Record its output.
- [ ] **Step 2 — Write the failing tests for the fix.** Same scenario, but the expected behaviour: nothing is
  removed, a warning is returned, and `ObjectState.geometry_backup` is dropped without `remove()` being called
  (assert on the stub's recorded calls). Plus: `open_shot` takes the untransacted branch in `_run_handler`;
  `libraries` is in `_TRACKED_COLLECTIONS`; a failed link removes the library. **Plus, for item 2a:**
  - **`reload_library`, `relocate_library` and `unlink_libraries` each take the untransacted branch** —
    asserted the same way `open_shot` is, by a test that fails if any of them ever enters the wrapped path;
  - **`link_canon_library` still takes the *transacted* branch and a failed link still rolls its `Library`
    datablock back** — this is the negative assertion that catches the tempting "just invalidate on
    `blend_import_post`" fix, and without it that regression passes silently;
  - **the `blend_import_post` handler invalidates only when the replace-in-progress flag is set** — drive it
    both ways against the stub handler list and assert the transaction survives the flag-clear case;
  - **the handler set registered at `register()` now includes `blend_import_post`**, and registration is still
    idempotent across a disable/enable cycle (Task 3's criterion 4 extends to the new handler — a second
    handler is a second chance to stack duplicates).
- [ ] **Step 3 — Run them; watch them fail.**
- [ ] **Step 4 — Implement.** Convert the Step 1 test into its inverse (documenting in its docstring that it
  previously asserted the destructive behaviour and why), or keep both with the original renamed to a
  regression guard.
- [ ] **Step 5 — Verify the `libraries` rollback against a real Blender**, not against stubs: link a library,
  force a failure, confirm the library datablock is gone and nothing else is.
- [ ] **Step 5b — Verify item 2a against a real Blender.** Reproduce §0.3 finding 7's reload transcript with
  your own fixture: register probe handlers for `load_pre` / `load_post` / `blend_import_pre` /
  `blend_import_post`, record every linked datablock's `session_uid`, call `lib.reload()`, and paste which
  handlers fired and which uids changed. Then do the same for `bpy.data.libraries.load(link=True)` and for
  `bpy.data.orphans_purge(...)`. **The expected results are the table in item 2a.** If `load_post` fires for
  `lib.reload()` on your build, stop and escalate — the whole shape of item 2a rests on it not firing.
- [ ] **Step 6 — Prove the tests can fail.** Revert each change independently and record which tests fail for
  each: the `_SESSION_SWAP_COMMANDS` bypass, `Transaction.invalidate()`, `ObjectState.invalidate()`, the
  `libraries` tracking, **the `_DATABLOCK_REPLACING_COMMANDS` bypass** (expect the Step 1b reproduction to go
  destructive again), and **the replace-in-progress flag** (expect the failed-link rollback test to fail,
  because the handler now invalidates on every import).
- [ ] **Step 7 — Gates and commit.**

### Acceptance criteria

1. **Both** reproductions — Step 1 (whole-file swap) and **Step 1b (library reload)** — are recorded in
   TASK_STATE with their pre-fix output. The hazard is **demonstrated**, not asserted, on both of the paths that
   reach it.
2. A session-swap command provably cannot enter the transacted path, enforced by a test. **So do
   `reload_library`, `relocate_library` and `unlink_libraries`, via `_DATABLOCK_REPLACING_COMMANDS`** — and
   **`link_canon_library` provably still does**, with its failed-link rollback intact. Both directions are
   asserted; the second is what stops the fix from being applied too broadly.
2a. **Step 5b's live-Blender handler/uid transcript is pasted**, showing that `lib.reload()` fires
   `blend_import_*` and not `load_post`, that it churns every linked datablock's `session_uid`, and that
   `orphans_purge` fires nothing. That transcript is the evidence for item 2a's whole shape.
3. `ObjectState.invalidate()` never calls `remove()` on a freed datablock, proven by asserting on recorded stub
   calls rather than by inspection.
4. `libraries` rollback verified against a real Blender with pasted output, **or** the documented-limitation
   fallback is taken with the warning surfaced in the envelope.
5. All pre-existing `tests/test_mutation_transaction.py` assertions unchanged.
6. Full suite green.

---

## Task 5: The filesystem trust boundary

**Recommended implementer: Opus-class.** Spec §4.5 calls the arbitrary-path exposure *"currently the dominant
risk for a pooled deployment"*, and this is the only enforcing control Phase 2 ships against it. Path
canonicalization is a domain where a plausible implementation is routinely wrong (symlinks, `..` after
normalisation, macOS case-insensitivity, Blender's own `//` scheme), and the failure is silent.

### Why

`open_shot` and `save_shot` add arbitrary-path read and **overwrite** to a socket with no authentication
(spec §4.5, §10 Q8). §0.3 finding 1: no path is validated against anything today. Appendix A also carries an
**open** finding that `handlers/polyhaven.py:362` loads a network-fetched `.blend` with no path check — a live
issue that this task can close cheaply while it is building the mechanism anyway.

### Current state, verified

- No allowlist exists (§0.3 finding 1).
- `output_roots.py` (ported to `main` in Task 1) gives `configured_roots()` reading `BLENDERMCP_OUTPUT_ROOTS`
  and `writable_roots()` reducing candidates to existing writable absolute directories. **It is advisory** — the
  addon reports the roots in the handshake; nothing refuses a path outside them.
- `handlers/rendering.py:550-556` is the repo's existing path pattern: `os.path.abspath(bpy.path.abspath(...))`,
  directory-must-exist, `confirm_overwrite` for an existing file. Mirror it; do not invent a second dialect.
- `docker-compose.yml` publishes only `127.0.0.1:8000` with a comment stating plainly that there is no
  authentication. That comment is the current control.

### The policy to implement

A new `bpy`-free module `src/blender_mcp/bundled/addon/file_paths.py`:

- **`resolve_blend_path(raw, *, must_exist: bool) -> str`**
  - reject non-strings, empty and whitespace-only values;
  - expand Blender's `//` prefix via `bpy.path.abspath` **at the call site** (keep `file_paths.py` free of
    `bpy`; take the already-expanded string);
  - expand `~`, **then `os.path.abspath`, then `os.path.realpath`** — in that order, and all three are load-
    bearing. `realpath` rather than `abspath` alone is what stops a symlink pointing outside a configured root.
    But `bpy.path.abspath` alone is **not** sufficient to hand `realpath` an absolute path, and two measured
    traps say why (verified 2026-09-14 against 5.2.1):

    | Input | `bpy.path.abspath` returns | Consequence |
    |---|---|---|
    | `'//../escape.blend'` | `/…/fx/../escape.blend` — the literal `..` is **preserved, not normalised** | Harmless *here*, because the following `realpath` does resolve it. Do not remove that `realpath` on the assumption `bpy.path.abspath` normalised anything. |
    | `'relative.blend'` (no `//` prefix) | `'relative.blend'` — **unchanged, still relative** | This is the dangerous one. A bare relative path falls through `bpy.path.abspath` untouched and would be resolved by `realpath` against the *process* CWD, which is wherever Blender was launched from, not the blend's directory. |

    The fix is the pattern **this repo already uses** at `handlers/rendering.py:552`,
    `output = os.path.abspath(bpy.path.abspath(filepath))` — wrap the `bpy.path.abspath` result in
    `os.path.abspath` before `realpath`. **Follow that prior art; do not invent a second dialect.**
  - require a `.blend` suffix (case-insensitively; reject a trailing dot or space, which macOS/Windows treat
    inconsistently);
  - when `must_exist`, require `os.path.isfile` and verify the file's **magic bytes**. A valid `.blend` has
    **three legitimate signatures**, not one — measured 2026-09-14 against 5.2.1, all three open successfully.
    **Each constant is a *prefix*, and the prefix lengths below are load-bearing**: an earlier revision of this
    plan specified two of them over-specifically, in a way that false-rejects valid files (see the note below).

    | Form | Constant to test | Why this length |
    |---|---|---|
    | uncompressed | `b'BLENDER'` (7 bytes) | The 5.2.1 header measured here is `BLENDER17-01v050`, but the `17-01v050` tail encodes 5.x's header format. Pre-5.x Blenders write a different tail (`BLENDER-v293`, `BLENDER_v279`: pointer-size char, endianness char, 3 version digits). `BLENDER` is the part every version shares. |
    | zstd-compressed (`compress=True`, the 5.x default compression) | `b'\x28\xb5\x2f\xfd'` (4 bytes) | The zstd frame magic; fixed by the format. |
    | legacy gzip-compressed (written by older Blenders, still opens) | `b'\x1f\x8b'` (2 bytes) | Only the first two bytes are gzip's magic. Byte 3 is CM (`\x08` = deflate) and **byte 4 is FLG, which varies** — `\x08` only when an FNAME is stored in the stream, `\x00` otherwise, which is what a Blender-written gzip `.blend` normally has. |

    > **Corrected 2026-09-14 — the superseded constants and why they were wrong.** An earlier revision gave
    > the gzip signature as `b'\x1f\x8b\x08\x08'` and the uncompressed one as the full 12-byte
    > `b'BLENDER17-01'`. Both **defeat their own purpose by false-rejecting valid files**: the 4-byte gzip
    > constant pins the variable FLG byte and so rejects every gzip `.blend` written without an FNAME (the
    > normal case), and the 12-byte constant pins 5.x's header encoding and so rejects every valid `.blend`
    > written by an older Blender. Verified by constructing both byte sequences: against the corrected
    > prefixes a `\x1f\x8b\x08\x00…` gzip header and a `BLENDER-v293` header are **accepted**; against the
    > superseded constants both are **rejected**. The failure mode of a magic-byte check is a false
    > rejection, and no negative test catches it — which is why acceptance criterion 3 and Step 1's three
    > positive cases exist.

    **A check that only accepts `b'BLENDER'` and nothing else rejects valid compressed `.blend` files** and will
    fail on any canon library saved with compression on. Accept all three prefixes. Name the three constants in
    the module and test one file of each form.
  - when not `must_exist`, require the parent directory to exist and be writable.
- **`enforce_roots(path, roots) -> None`** — if `roots` is non-empty, `path` must be inside one of them, tested
  with `os.path.commonpath` on the **realpath**ed forms (not `str.startswith`, which accepts `/output-evil` for
  root `/output`). Raise a message that names the roots and does **not** echo the rejected absolute path back
  in full if it lies outside them.
- **`sanitize_blender_error(exc) -> str` — path-leak containment for Blender's *own* exception text.** The
  rubric's "no absolute path leaked in an error" item and Critic 3's path-leak question are, as written,
  satisfied only for messages *this plan authors*. They are defeated by Blender itself.

  **There are at least *five* distinct real message shapes, not one and not three.** An earlier revision of this
  plan enumerated three and built Step 6 and criterion 5 around exactly those three; re-measured from scratch
  2026-09-14 against 5.2.1, each captured from a real `RuntimeError`, there are at least five — and two of the
  additional ones are reachable by input that **passes `resolve_blend_path`'s own validation** (a real file,
  a `.blend` extension, one of the three accepted magic-byte prefixes) and still fails to load:

  | # | Failing call / mode | Message |
  |---|---|---|
  | 1 | `wm.open_mainfile` — missing file | `Error: Cannot read file "<abs>": No such file or directory` |
  | 2 | `wm.open_mainfile` — **directory, non-`.blend`, corrupt magic, *and* empty path** (four modes, one shape) | `Error: File format is not supported in file "<abs>"` |
  | 3 | `wm.open_mainfile` — valid magic prefix, corrupt or truncated body | `Error: Loading "<abs>" failed: Failed to read blend file '<abs>': Missing DNA block` |
  | 4 | `wm.save_as_mainfile` — unwritable destination | `Error: Cannot open file <abs>@ for writing: No such file or directory` |
  | 5 | `Library.reload()` — invalid path | `Error: Trying to reload library '<LI+name>' from invalid path '<abs>'` |

  They differ in quoting (`"…"`, bare, `'…'`, **and both `"…"` and `'…'` inside one message**), in wording, in
  **how many times the path appears**, and — the one that breaks a naive implementation — in **whether the
  absolute path in the message is the caller's string at all**. Five consequences the implementer must design
  for:

  1. **The save shape's `<abs>@` is a *derived* string, not the caller-supplied path.** Blender appends `@` to
     form the temp-write filename it actually opens. So a sanitizer built on "find the caller's path and
     replace it" **will not match**, and the path will pass through intact. The requirement below is therefore
     stated as *find absolute paths and remove them*, with substitution of the caller's string as a
     presentation nicety — never as the detection mechanism.
  2. **Shape 3 embeds the absolute path *twice*, in two different quoting styles** — `Loading "<abs>" failed:`
     then `blend file '<abs>':`. A sanitizer that calls `str.replace(path, placeholder)` once, or a regex
     applied without a global flag, removes the first occurrence and **ships the second**. This is the shape
     that defeats a single-replace implementation, and it needs its own named test asserting **zero** absolute
     paths remain, not "the first occurrence was removed".
  3. **Shape 2 has no meaningful tail after the path at all** — the message ends at the closing quote. A
     sanitizer written as "cut everything up to the path and keep the rest" produces an empty or
     content-free message here. Keep the tokens that precede the path (`File format is not supported in file`),
     because in this shape they are the *entire* cause.
  4. **Shape 2 is also the shape of the empty-path case, and there the `<abs>` is the server's process CWD**,
     not anything the caller supplied — measured: `open_mainfile(filepath="")` reports
     `File format is not supported in file "<process cwd>"`. Substituting "the caller's path" is therefore not
     merely ineffective there, it would be a *lie*; and the leak is a disclosure of the server's own working
     directory. Detect structurally; never assume the path in the text came from the caller.
  5. **The reload shape's library name is `LI`-prefixed** (`'LIlib_plain.blend'`, the raw ID name with its
     two-character type code). Strip or normalise it if it is surfaced; do not present it to a client as the
     library's name.

  **Do not write a per-shape regex table and consider the job done.** These five are the shapes Phase 2 is
  *known* to reach; they are evidence that the message format is not a contract, not an enumeration of it. The
  three-shape claim this section replaces was itself produced by enumerating only the failure modes the plan had
  happened to try — which is exactly how a fourth and fifth shape go unnoticed.

  **Requirement:** no handler in Tasks 6 or 7 may pass a raw `bpy`/operator exception string to the client.
  Every `except` around an operator or data-API call routes its exception through a sanitizer that **detects
  absolute paths in the text structurally** — any token that looks like an absolute filesystem path, whatever
  surrounds it, including one with a trailing `@` or other derived suffix — and removes them, substituting the
  caller-supplied path *as the caller supplied it* where that is a faithful description, and `<path>` where it
  is not.

  **The keep/remove rule is shape-agnostic, because the shapes are not:** keep **every non-path token,
  regardless of where it sits relative to the path**, and remove **every occurrence of the path, not just the
  first**. Do not write the rule as "keep the meaningful tail" — shape 2 has no tail after the path (the cause
  is entirely in the tokens *before* it) and shape 3's meaningful tail (`Missing DNA block`) sits behind a
  *second* path occurrence, so a tail-keeping sanitizer either empties shape 2 or leaks shape 3. Log the full
  text server-side only. Test it with a path the client never sent — e.g. a symlink target, the save shape's
  `<abs>@`, and the empty-path case's process CWD — and assert none appears in the response.
- **Default policy when no roots are configured.** Recommended and to be implemented unless the reviewer
  overrules it with a recorded reason: **unset `BLENDERMCP_FILE_ROOTS` means permissive**, because the local
  artist case is a GUI Blender on the user's own machine driven by a loopback socket, and a deny-by-default
  there bricks ordinary use for no gain; **a pooled or container deployment sets the variable and is the
  deployment the risk statement is about**, and there the boundary is enforced. This asymmetry must be stated in
  the module docstring, in the README, and in the handshake (so a client can tell which mode it is in) — an
  unenforced default that nobody can observe is worse than no default.
- **Reuse `BLENDERMCP_OUTPUT_ROOTS` or add `BLENDERMCP_FILE_ROOTS`?** Decide and justify. Reuse keeps one
  concept; a separate variable lets a deployment allow renders to `/output` while allowing `.blend` reads from a
  read-only canon mount. The recommendation is **two variables, with `BLENDERMCP_FILE_ROOTS` defaulting to
  `BLENDERMCP_OUTPUT_ROOTS` when unset**, because the canon library mount is genuinely read-only and folding it
  into "where I may write" would be wrong. Record the decision either way.
- **Overwrite policy** — mirror `rendering.py:556`: writing over an existing file requires an explicit
  `confirm_overwrite=True`. Default `False`.
- **`use_scripts` is an arbitrary-code-execution vector and this policy owns it.** `wm.open_mainfile` takes a
  `use_scripts` parameter. A `.blend` file can carry embedded Python (registered auto-run text datablocks,
  driver expressions) that Blender executes **on load** when scripts are enabled. Measured defaults in 5.2.1
  (2026-09-14): `wm.open_mainfile.use_scripts` defaults to `False`, and it is additionally gated by
  `bpy.context.preferences.filepaths.use_scripts_auto_execute`, which also defaults to `False`. Two defaults
  being safe is not a control — a user preference or an app template can flip the preference, and nothing in
  the current design would notice.

  This matters more here than in an ordinary Blender session because of who is asking: `open_shot` is reachable
  from an **unauthenticated loopback socket** after this phase, and this plan's own Global Constraints say
  **"No arbitrary code execution, in any form"** (spec Decision #7). A caller who can (a) reach the socket and
  (b) place or name a `.blend` inside the configured roots would otherwise have a code-execution path that
  never goes near `exec`/`eval`.

  **Requirements — two here, one in Task 6:**
  1. **`use_scripts` is never exposed as a tool or command parameter.** Not as an opt-in, not "for advanced
     users". It is not part of any schema in Tasks 6, 7 or 9.
  2. **Every `wm.open_mainfile` call passes `use_scripts=False` explicitly**, rather than relying on the
     operator default. Assert it in a test against the recorded stub call, the same way `compress=False` is
     asserted in Task 6.
  3. **The runtime check of `preferences.filepaths.use_scripts_auto_execute` belongs to Task 6, not here.**
     An earlier revision of this plan assigned it to this task, which cannot implement it: criterion 2 below
     mandates that `file_paths.py` import no `bpy`, and `bpy.context.preferences` is reachable only from the
     addon handler that actually calls `wm.open_mainfile`. **Task 6 Step 4b owns the check and the
     refuse-vs-warn decision.** This task owns the *policy statement* (that the preference is part of the ACE
     surface and must be checked before a load) and nothing more; state it in the module docstring and README
     so the requirement has a written home, and cross-reference Task 6 for the implementation.

  Task 8's threat model names this as a risk in its own right; the static containment lives here, the runtime
  containment lives in Task 6.

### Steps

- [ ] **Step 1 — Write the adversarial tests first**, one per attack, each named after what it defends:
  `..` traversal after normalisation; a symlink inside a root pointing outside it; a symlinked *parent
  directory*; `/output-evil` against root `/output` (the `startswith` bug); `~` expansion; a `.blend` with a
  trailing dot or space; a non-`.blend` extension; a directory passed where a file is expected; a file whose
  extension is `.blend` but whose magic bytes are not; an empty string; a non-string; a path containing a NUL
  byte; a **bare relative path with no `//` prefix** (the `bpy.path.abspath` passthrough trap above — assert the
  result is absolute); Blender's `//relative` form; and `'//../escape.blend'`. Assert the *message* does not leak
  an out-of-root absolute path. Add **four positive** magic-byte cases as well — an uncompressed 5.x `.blend`, a
  zstd-compressed one, a gzip-compressed one **written without an FNAME flag** (`\x1f\x8b\x08\x00`, the normal
  Blender case), and a **pre-5.x-style `BLENDER-v293` header** — all four must be **accepted**, since the
  failure mode there is a false rejection, which no negative test catches. The last two are exactly what the
  superseded over-specific constants rejected (see the correction note above), so they are the cases that make
  this criterion worth having.
- [ ] **Step 2 — Run them; watch them fail** (`ModuleNotFoundError` for the first run is fine, but each
  individual attack must be seen failing once the module exists as a stub).
- [ ] **Step 3 — Implement `file_paths.py`.** No `bpy` import. Pure functions, fully docstringed.
- [ ] **Step 4 — Promote `output_roots.py` from advisory to enforcing** for file commands: keep its existing
  advisory role in the handshake unchanged (it is already tested), and add the enforcing read path.
- [ ] **Step 5 — Retrofit `handlers/polyhaven.py:362`** so the network-fetched `.blend` is validated before
  `bpy.data.libraries.load`. Keep the existing behaviour for the enabled-provider happy path; the check is a
  guard, not a redesign. Verify the existing Poly Haven tests still pass unmodified.
- [ ] **Step 6 — Implement `sanitize_blender_error` and prove it against *at minimum these five real* Blender
  exception shapes**, each **captured from a real run**, not a hand-written string and not only the
  `open_mainfile` one. Five is the floor, not the enumeration — if a sixth shape turns up while you are
  capturing these, add it. In `--background`, capture the actual `RuntimeError` text from each of:
  - `bpy.ops.wm.open_mainfile(filepath=<missing>)` → `Cannot read file "<abs>": No such file or directory`
  - `bpy.ops.wm.open_mainfile(filepath=<a directory>)` → `File format is not supported in file "<abs>"`
    (the same shape also covers a non-`.blend`, a corrupt magic header, and `filepath=""` — capture the
    empty-path one too, because its `<abs>` is the **process CWD**, a path the caller never sent)
  - `bpy.ops.wm.open_mainfile(filepath=<valid magic prefix, truncated body>)` → `Loading "<abs>" failed:
    Failed to read blend file '<abs>': Missing DNA block`. Build the fixture by truncating a real `.blend` to
    its first ~64 bytes: it is a real file, has a `.blend` extension, and carries the `BLENDER` prefix, so it
    **passes `resolve_blend_path` and fails the load** — this is the post-validation failure path, not a case
    validation catches.
  - `bpy.ops.wm.save_as_mainfile(filepath=<unwritable dir>/x.blend)` → `Cannot open file <abs>@ for writing: …`
  - `lib.filepath = <missing>; lib.reload()` on a linked library → `Trying to reload library '<LI+name>' from
    invalid path '<abs>'`

  For each, assert the sanitized form contains no absolute path while still naming the cause, and paste the raw
  and sanitized forms side by side. **Two shapes each need their own named test, because each is a distinct way
  for a plausible sanitizer to fail:**
  - **`test_sanitizer_removes_derived_temp_write_path`** (the save shape): its `<abs>@` is a string Blender
    *derived* from the caller's path, so a substitution-based sanitizer leaves it in place.
  - **`test_sanitizer_removes_every_occurrence_of_the_path`** (the `Missing DNA block` shape): the absolute
    path appears **twice**, in two different quoting styles (`"…"` then `'…'`). Assert **zero** absolute paths
    remain in the sanitized output — count them, e.g. assert no substring of the sanitized text starts with
    `/` and resolves like a path, or assert `sanitized.count(abs_path) == 0` *and* that no other absolute-path
    token survives. Asserting only that "the first occurrence was removed", or that the raw path string is not
    equal to the output, passes against a single-`replace` implementation that leaks the second copy.

  A sanitizer that passes the open and reload shapes and fails either of those two is the expected failure
  mode, so each is asserted separately rather than folded into one "no path leaks" test.
- [ ] **Step 7 — Prove the tests can fail.** Revert `realpath` to `abspath` and confirm the symlink tests fail;
  revert `commonpath` to `startswith` and confirm the `/output-evil` test fails; drop the `os.path.abspath`
  wrapper and confirm the bare-relative-path test fails; narrow the magic-byte check to `b'BLENDER'` only and
  confirm the compressed-`.blend` acceptance tests fail; **restore the two superseded over-specific constants
  (`b'\x1f\x8b\x08\x08'` and the 12-byte `b'BLENDER17-01'`) and confirm the no-FNAME-gzip and pre-5.x-header
  acceptance tests fail** — this is the revert that proves Fix 6 was a real bug and not a stylistic preference;
  bypass the sanitizer and confirm the leak test fails, **including the `save_as_mainfile` `<abs>@` case
  separately**. Record every count.
- [ ] **Step 8 — Document** the two env vars, the permissive-when-unset asymmetry, the overwrite rule, **and the
  `use_scripts` policy** in README. Add the active path policy to the handshake so a client can read it.
- [ ] **Step 9 — Gates and commit.**

### Acceptance criteria

1. Every attack in Step 1 has its own named test, and every revert in Step 7 is demonstrated failing against a
   deliberately weakened implementation, with counts.
2. `file_paths.py` imports no `bpy` (assert it, the way the repo already asserts `bpy` stays out of
   `server/`).
3. **All three `.blend` magic signatures are accepted** — uncompressed, zstd and legacy gzip — each proven by a
   test against a real file of that form, not a fixture byte string. Blender 5.2.1 writes only the first two
   (`compress=False` → `BLENDER17-01v050`; `compress=True` → zstd), so produce the gzip case by gzipping a real
   uncompressed `.blend` — `gzip.compress(...)` with **no FNAME**, which yields the `\x1f\x8b\x08\x00` header a
   Blender-written gzip `.blend` normally has and which the superseded 4-byte constant rejected. Add a fourth
   acceptance case for a **pre-5.x-style header** (`BLENDER-v293…`): it need not be a file an installed Blender
   can write, but the check must accept it, because that is the case the superseded 12-byte constant rejected.
4. **The path pipeline is `os.path.realpath(os.path.abspath(bpy.path.abspath(raw)))`**, matching
   `handlers/rendering.py:552`, and a bare relative path provably comes out absolute.
5. **No Blender exception text reaches the client unsanitized**, proven against **at minimum the five**
   captured real `RuntimeError` shapes from Step 6 — `Cannot read file`, `File format is not supported`
   (including the empty-path variant whose path is the process CWD), `Missing DNA block`, `save_as_mainfile`'s
   `<abs>@`, and `Library.reload()`'s `invalid path` — **each captured from a real run** and each with its raw
   and sanitized form pasted. Two of them must have their own named tests:
   - the `save_as_mainfile` shape, because its `<abs>@` is derived from the caller's path rather than equal to
     it and so defeats substitution-based sanitizing;
   - the `Missing DNA block` shape, because it carries the absolute path **twice** in two quoting styles; the
     assertion is that **zero** absolute paths remain in the sanitized output, not that the first occurrence
     was removed.

   Five is the floor. An earlier revision of this plan fixed this criterion at "all three", which is how the
   two-occurrence shape and the four-modes-one-shape case went unnoticed for four review passes.
6. **`use_scripts` appears in no schema**, and the policy that `preferences.filepaths.use_scripts_auto_execute`
   must be checked before a load is documented in the module docstring and README, with the implementation
   pointed at Task 6 Step 4b. (The `use_scripts=False`-passed-explicitly assertion and the preference check
   itself are Task 6's acceptance criteria 5 and 6; this task cannot satisfy either, because `file_paths.py`
   imports no `bpy`.)
7. The Poly Haven `.blend` path is validated — spec §7's test row is satisfied and Appendix A's R2 finding can
   be moved from **open** to **resolved** for the path half.
8. The active path policy is readable from the handshake.
9. All pre-existing Poly Haven tests pass unmodified.
10. Full suite green.

---

## Task 6: Addon file-lifecycle handlers — `open_shot`, `save_shot`, `reset_session`

**Recommended implementer: Opus-class.** *(Re-tiered from Sonnet — the reasoning is recorded rather than
changed silently.)* The original Sonnet tiering rested on "the hard decisions are made (Task 2), the ordering
and epoch machinery exists (Task 3), rollback is safe (Task 4) and paths are validated (Task 5)". All of that is
still true, and it is still true that no *design question* is open here. But tiering in this plan is by
**consequence of getting it wrong**, not by how much remains undecided — that is the stated basis for Task 4's
Opus tiering, where the mechanics are also simple. On that basis Task 6 does not belong at Sonnet: it is the
task where **three** of §07's ten automatic-critical rubric items are actually enforced, and each is a case
where the obvious implementation looks correct and is not.

1. **The overwrite pre-check.** `check_existing=True` looks like the guard and is inert outside the interactive
   file browser (behaviour 3 below); the real guard is an `os.path.exists` pre-check inside the handler. Getting
   this wrong silently destroys a user's `.blend` — the same unrecoverable class as Task 4.
2. **The path-leak containment.** Every message this task writes can be clean while Blender's own `RuntimeError`
   text carries the absolute path straight through (Task 5's sanitizer must actually be on every `except`).
3. **The explicit `use_scripts=False` on every `wm.open_mainfile` call** (this task's criterion 5). §07 lists
   as automatically critical "`use_scripts` exposed as a parameter, or a `wm.open_mainfile` call that does not
   pass `use_scripts=False` explicitly" — relying on the operator's safe *default* satisfies neither half. This
   is the item on the automatic-critical list; do not confuse it with the preference check below.

**A fourth item, adjacent but distinct — and an earlier revision of this plan conflated it with item 3.** The
runtime check of `preferences.filepaths.use_scripts_auto_execute` (Step 4b, this task's criterion 6, which moved
here from Task 5) is **not** on §07's automatic-critical list — that list's `use_scripts` entry is about
exposure and the explicit argument, which is item 3. The preference check matters for a different reason: it
carries a refuse-vs-warn decision that is a security judgement on an unauthenticated socket, and it is **the one
item in this task that is genuinely *undecided***, not merely mechanical. It belongs in the tiering argument as
an open judgement, not as a rubric item.

Individually each check is a few lines. That is precisely why they are easy to write plausibly and wrong: the
failure mode in all three automatic-critical items is silent, and only the preference check announces itself as
a decision. If a reviewer prefers to
keep this at Sonnet, that is defensible — the implementation work is small — but it must be **recorded in
TASK_STATE as a deliberate downgrade with its cost if wrong** (a destroyed `.blend`, a leaked path, or a live
ACE vector), per the handoff's §05 rule, and the four-cycle critic loop should be applied regardless.

### Why

Spec §5 makes the `.blend` a **required** deliverable, and §4.5 opens with: *"This server cannot currently open
or save a `.blend`... It is the critical path."*

### Current state, verified

Zero of these exist. Blender 5.2.1 signatures, introspected 2026-09-14:

```
bpy.ops.wm.open_mainfile(filepath="", ... , load_ui=..., use_scripts=..., display_file_selector=...)
bpy.ops.wm.save_mainfile(filepath="", check_existing=True, ... , compress=..., relative_remap=...)
bpy.ops.wm.save_as_mainfile(filepath="", check_existing=True, ... , compress=..., relative_remap=..., copy=...)
bpy.ops.wm.read_factory_settings(use_factory_startup_app_template_only=False, app_template="Template", use_empty=False)
```

**Introspect the full argument lists yourself before writing code** — the `head` above is truncated, and §0.3
finding 4 is what happens when a plan's operator signature is trusted (and how it goes wrong in the other
direction when the introspection itself is done with the wrong tool). `wm.open_mainfile` and
`wm.save_as_mainfile` both work in `--background`, verified.

**Three measured behaviours of these three operators that contradict what a reader would reasonably assume.**
All verified 2026-09-14 against 5.2.1; each is load-bearing for a step or an acceptance criterion below.

1. **They raise `RuntimeError` on failure. They never return `{'CANCELLED'}`.** Tested on every failure mode —
   a missing file, a directory passed as the filepath, a non-`.blend`, a `.blend` with a corrupt magic header,
   an empty path, and a read-only destination — `wm.open_mainfile`, `wm.save_mainfile` and
   `wm.save_as_mainfile` each raise. A handler that only checks the return value for `{'CANCELLED'}` **will
   report success on every one of those failures**, because the exception propagates past the check entirely (or
   worse, is swallowed by a bare `except`). The error path for these three operators is `try`/`except
   RuntimeError`, and the sanitizer from Task 5 is what the caught text goes through.

   **This does not generalise into the opposite blanket rule either.** `{'CANCELLED'}` is a real failure signal
   for other Blender operators, so the result-validation rule is **per-operator, established by running that
   operator**, not a phase-wide policy in either direction. Write down which discipline each call site uses,
   next to the call. **As it happens, no call site in Phase 2 ends up needing a `{'CANCELLED'}` check** — Task 7
   rules for the data API (`lib.filepath = ...; lib.reload()`) over `wm.lib_reload` / `wm.lib_relocate`, and
   `lib.reload()` raises. That is an outcome of Task 7's ruling, not a licence to stop checking: if a later task
   introduces a different operator, establish its failure signal before writing the handler.

2. **`save_as_mainfile`'s `relative_remap` defaults to `True`; `save_mainfile`'s defaults to `False`.** Measured
   straight off the operator RNA:

   ```
   save_as_mainfile relative_remap default = True      save_as_mainfile check_existing default = True
   save_mainfile    relative_remap default = False     save_mainfile    check_existing default = True
   ```

   Left at its default, `save_as_mainfile` **rewrites linked-library paths into `//`-relative form** — confirmed
   by comparing `Library.filepath` before and after a save with the default versus an explicit
   `relative_remap=False`. The plan's `relative_remap=False` recommendation is correct, but it must be **passed
   explicitly on every `save_as_mainfile` call**; inheriting the default silently does the opposite of what is
   intended, and it would break Task 10's gate assertion that compares library filepaths across save→reopen.

3. **`check_existing=True` does not prevent an overwrite when the operator is called programmatically.**
   Verified: `wm.save_as_mainfile(filepath=<an existing file>, check_existing=True)` **overwrites it silently**
   and returns `{'FINISHED'}`. That parameter only drives the interactive file-browser's "Save Over?" warning
   dialog; outside that context it is inert. Relying on it as the `confirm_overwrite` mechanism would
   **silently destroy a user's `.blend`** — which §07 of the handoff lists as an automatic critical failure. The
   gate must be an explicit pre-check **inside the handler** (`os.path.exists(target)` before the operator is
   called, refusing unless `confirm_overwrite=True`), exactly as `handlers/rendering.py:556` already does.

### Hazards carried forward

- **Reentrancy** — implement Task 2's decided protocol exactly; cite its TASK_STATE section in the handler's
  docstring so the next reader knows the choice was made by experiment, not preference.
- **Ordering** — `open_shot` and `reset_session` are members of `_SESSION_SWAP_COMMANDS` (Task 3) and therefore
  trip the barrier. **Whether `save_shot` is also a member depends on Task 2's reentrancy decision** — Task 3
  defines the constant as "`open_shot`, `reset_session`, and `save_shot` only if Task 2's decision requires
  it". **Read the constant and TASK_STATE's Task 2 record before writing this handler; do not assume `save_shot`
  is in the set, and do not assume it is out.** An earlier revision of this section stated its membership as
  settled fact, which it is not. Whatever the membership turns out to be, do not add a second, local ordering
  mechanism.
- **Rollback** — these commands take the untransacted branch (Task 4). Do not add them to `_READ_ONLY_COMMANDS`
  as a shortcut for that: they are not read-only, and `_READ_ONLY_COMMANDS` is also read by anything that
  reasons about mutation. Use the dedicated constant.
- **Security** — every path goes through Task 5's `resolve_blend_path` + `enforce_roots`. `save_shot` over an
  existing file requires `confirm_overwrite=True`. `reset_session` requires its own confirmation because it
  discards unsaved work.

### What to build

**Extend** `src/blender_mcp/bundled/addon/handlers/file_lifecycle.py` — the mixin **Task 3 created**, which
already carries `get_session_info` and is already in `BlenderMCPServer`'s bases (`server_core.py:78-97`). Do not
create a second module or a second mixin; add three commands to the existing one and register them in
`_build_command_handlers()`:

| Command | Behaviour |
|---|---|
| `open_shot(filepath, *, load_ui=False)` | Validate → **check `bpy.context.preferences.filepaths.use_scripts_auto_execute` (Step 4b)** → open → report `{filepath, scene_name, session_epoch, object_count, libraries: [...], capabilities_changed: bool}`. Must state in its result that the client should re-handshake, and the epoch is how it knows. **Calls `wm.open_mainfile(..., use_scripts=False)` explicitly** (Task 5's ACE policy) and wraps the call in `try/except RuntimeError` (behaviour 1 above) with the caught text routed through `sanitize_blender_error`. `use_scripts` is **not** a parameter of this command. |
| `save_shot(filepath=None, *, compress=False, relative_remap=False, confirm_overwrite=False)` | `filepath=None` saves in place via `wm.save_mainfile` and **requires an already-saved file**; a `filepath` uses `save_as_mainfile`. **`compress=False` by default** — spec §4.1 invariant 1 requires canon publishes be uncompressed, and a compressed re-save destroys `content_digest` stability. **`relative_remap=False` by default with an explicit opt-in**, and — because `save_as_mainfile`'s own default is `True` (behaviour 2 above) — **passed explicitly on every call**, never inherited. `confirm_overwrite` is enforced by an `os.path.exists` pre-check **before** the operator runs; `check_existing` is inert here and must not be used for it (behaviour 3 above). Failure is `RuntimeError`, not `{'CANCELLED'}`. |
| `reset_session(*, confirm=False)` | `wm.read_factory_settings(use_empty=True)` — spec §4.5's "pool load-or-reset step". Refuses without `confirm=True`. |

All three return the standard result dict; the server-side tool wraps it in `ok()`.

### Steps

- [ ] **Step 1 — Introspect the three operators' full signatures and their *defaults*** against 5.2.1 and paste
  the output into the task record. Read the defaults off the RNA, not off the docs
  (`[(p.identifier, p.default) for p in bpy.ops.wm.save_as_mainfile.get_rna_type().properties]`) — behaviour 2
  above is exactly the class of fact that a truncated signature hides.
- [ ] **Step 2 — Write the failing tests** against the stubbed-`bpy` harness (`tests/conftest.py`'s
  `load_addon_package`): path validation is called; `compress=False` is the default and is actually passed;
  **`relative_remap=False` is passed explicitly in the recorded `save_as_mainfile` call** (assert it is present
  in the kwargs, not merely that it is falsey — an omitted argument would inherit Blender's `True`);
  **`use_scripts=False` is passed explicitly in the recorded `open_mainfile` call**, and `use_scripts` is absent
  from the command's own parameter schema; saving over an existing file without `confirm_overwrite` raises and
  **does not call the operator at all** (assert the stub recorded zero calls — `check_existing` must not be
  load-bearing); `reset_session` without `confirm` raises; `save_shot()` with no filepath on an unsaved file
  raises with an actionable message; **a `RuntimeError` from any of the three operators becomes a clean error
  response, not a success and not a dropped socket** — and the response text contains no absolute path.
- [ ] **Step 2b — Prove the destructive default is actually blocked, against a real file.** In `--background`,
  point `save_shot` at an existing `.blend` without `confirm_overwrite` and assert the file's bytes are
  unchanged afterwards. This is the automatic-critical rubric item; a stub assertion is not sufficient evidence
  for it, because behaviour 3 is precisely a case where the operator looks guarded and is not.
- [ ] **Step 3 — Run; watch fail.**
- [ ] **Step 4 — Implement**, extending the `file_lifecycle` mixin Task 3 created (it already carries
  `get_session_info`) with the three new commands, registering them in `_build_command_handlers()`. Task 3
  already asserts the protocol pair at 31 — do not bump it again here; if Task 3 did not land, follow its
  own fallback (bump to 32) rather than inventing a second one.
- [ ] **Step 4b — Implement the `use_scripts_auto_execute` runtime check, and decide refuse-vs-warn.** Task 5
  states the policy but **cannot implement it**: `file_paths.py` is `bpy`-free by mandate, and
  `bpy.context.preferences.filepaths.use_scripts_auto_execute` is only reachable from this handler, which is
  also where the `wm.open_mainfile` call actually happens. Before calling the operator, read that preference.
  It defaults to `False` (measured 2026-09-14), but a user preference or an app template can flip it, and with
  it on a `.blend` can execute embedded Python on load — an arbitrary-code-execution path on an
  unauthenticated socket, which this plan's Global Constraints forbid outright (spec Decision #7).
  **Decide and record in TASK_STATE whether `open_shot` refuses outright or proceeds with a loud `warnings`
  entry naming the preference**, with the reason. The recommendation, to be followed unless the reviewer
  overrules it with a recorded reason, is **refuse**: `use_scripts=False` is already passed explicitly, so a
  refusal costs an artist who has the preference on nothing they actually need from `open_shot`, whereas a
  warning on an unauthenticated socket is a control nobody reads. Note the asymmetry with Task 5's
  permissive-when-unset root policy and say why this one is stricter (there, the default is *unconfigured*;
  here, the default is *safe* and someone has changed it). Test both branches against the stub preference.
- [ ] **Step 5 — Verify against a real Blender in `--background`** (the operators work there; only the timer
  does not): open a fixture, save to a temp path, confirm the file exists and reopens, reset and confirm the
  scene is empty. Also run each of the six failure modes from behaviour 1 **against the operator each one
  actually applies to** and paste the actual exception text, confirming it is a `RuntimeError` and not a
  `{'CANCELLED'}` return, and that the sanitized form reaching the client carries no absolute path. Be precise
  about applicability rather than running all six against all three: **a read-only destination is a *save*
  failure mode and is not reachable through `open_mainfile`** — opening from a read-only location succeeds. So
  drive the missing file, the directory, the non-`.blend`, the corrupt magic header and the empty path through
  `open_mainfile`, and the read-only destination through `save_mainfile` / `save_as_mainfile`. Add the
  post-validation open failure as well — a real `.blend` truncated to its first ~64 bytes, which passes
  `resolve_blend_path` and still fails to load with the `Missing DNA block` shape that carries the absolute
  path **twice** (Task 5's shape 3). Paste the output.
- [ ] **Step 6 — Verify under the live rig** that a queued `open_shot` over the socket behaves as Task 2
  predicted and Task 3's barrier expects. This is the first end-to-end exercise of the decided protocol.
- [ ] **Step 7 — Prove the tests can fail; gates; commit.**

### Acceptance criteria

1. All three commands appear in `_build_command_handlers()` and therefore in `get_addon_info`'s `capabilities`
   — verified by an actual handshake, not by reading the source. **`get_session_info` (Task 3's) is still
   advertised too**: this task extends `handlers/file_lifecycle.py`, it does not replace it, and a handshake
   showing three file commands but no `get_session_info` means the module was re-created rather than extended.
2. `compress=False` and `relative_remap=False` are **asserted by tests as explicitly-passed kwargs**, not just
   documented; spec §4.1 invariant 1 depends on the first and Task 10's link-survival assertion on the second.
   An assertion that merely accepts the operator default is a failing criterion, because
   `save_as_mainfile`'s `relative_remap` default is `True`.
3. Saving over an existing file without confirmation is refused **before** the operator is called — the stub
   records zero operator calls, **and** Step 2b shows a real file's bytes unchanged. `check_existing` is not
   used as the mechanism anywhere in the handler.
4. **A `RuntimeError` from `open_mainfile` / `save_mainfile` / `save_as_mainfile` is caught and surfaced as an
   error**, with no absolute path in the client-visible text. These three operators do not return
   `{'CANCELLED'}`, so a CANCELLED-only check does not satisfy this criterion. The reverse is also not a rule:
   establish each operator's failure signal by running it.
5. **`use_scripts=False` is passed explicitly on every `open_mainfile` call and `use_scripts` is exposed
   nowhere**, per Task 5's policy — asserted by test.
6. **The `use_scripts_auto_execute` preference is checked before the load** (Step 4b), the refuse-vs-warn
   choice is recorded in TASK_STATE with its reason, and **both branches are covered by tests** driving the stub
   preference `True` and `False`. This is the runtime half of Task 5's ACE policy and it lives here because
   `file_paths.py` cannot reach `bpy.context.preferences`.
7. Real-Blender evidence pasted for Step 5 and Step 6.
8. Full suite green.

---

## Task 7: Addon linking handlers — link, override, list, reload, relocate, unlink

**Recommended implementer: Opus-class.** *(Re-tiered from Sonnet — the reasoning is recorded rather than
changed silently.)* The original Sonnet rationale read: "the API risk is retired below by live introspection and
end-to-end testing of all three override routes; the route is chosen here, so what remains is careful handler
work against a decided design." **Every clause of that is still true, and it is written on the criterion this
plan explicitly disavows.** Tiering here is by **consequence of getting it wrong, not by how much remains
undecided** — that is the stated basis on which Task 4 was tiered up front and Task 6 was re-tiered on review.
"The decisions are already made" is an argument about *difficulty*; it does not lower the cost of a defect.

Applied honestly, the consequence profile of Task 7 is the one that already moved Task 6:

1. **It owns the only Phase 2 command that outright deletes user data.** `unlink_libraries` calls
   `bpy.data.libraries.remove` and, optionally, `bpy.data.orphans_purge`. Nothing else in the phase removes
   datablocks the user owns — Task 6 overwrites a file (recoverable from the artist's own backups; guarded by a
   confirmation), Task 4 *prevents* a deletion. This one performs it, on an unauthenticated socket, and its
   scoping guard ("never touch a library that was not named") is a few lines that look obviously correct and
   fail silently when they are not.
2. **It carries a path-leak-critical acceptance criterion on the single most common failure path in the
   module.** Criterion 8 (added in the previous correction pass) requires that `Library.reload()`'s
   `Trying to reload library '…' from invalid path '<abs>'` never reaches the client — and a missing or moved
   library file is the *ordinary* way a relocate or reload fails, not an edge case. That is an
   automatic-critical rubric item sitting on the module's happy-path-adjacent error branch.
3. **`reload_library` and `relocate_library` replace the contents of every linked datablock in place.** They
   invalidate references held by anything mid-flight, and `relocate_library` can rename the `Library` datablock
   out from under a caller's handle. In-place replacement of data the user is looking at is the same class as
   Task 4's, and the failure is not visibly a failure.

Each of those is individually small to implement. That is exactly the pattern that justified Task 6's re-tier:
small, plausible-looking, silent when wrong. If a reviewer prefers to keep this at Sonnet, that is defensible —
but it must be **recorded in TASK_STATE as a deliberate downgrade with its cost if wrong** (destroyed linked
data, a leaked absolute path, or a silently invalidated handle), per the handoff's §05 rule, and the four-cycle
critic loop should be applied regardless.

The API risk genuinely *is* retired below by live introspection and end-to-end testing of all three override
routes — that part of the original rationale stands as a statement about the design's maturity. It is simply
not the question tiering answers.

### Why

Spec §4.5's linking family is the other half of the phase gate: *"link canon, create an override... reopen with
the link intact"*. Spec §4.1 adds that a pinned canon version must be identifiable, and §3 records that a failed
link currently leaks a library datablock (fixed in Task 4).

### Current state, verified — read §0.3 findings 4 and 5 first

**The spec's `create_override` row is correct for Blender 5.2.1.** An earlier draft of this plan claimed
`Collection.override_hierarchy_create`, `ID.override_hierarchy_create` and `ID.override_create` did not exist;
that claim was produced by `dir()` on a `bpy.types.*` class, which returns nothing for *any* RNA member in this
build, and it is false. All three exist (§0.3 finding 4):

```
Collection.override_hierarchy_create(scene, view_layer, *, reference=None, do_fully_editable=False)
Collection.override_create(*, remap_local_usages=False)
```

**The spec's other half does *not* hold, and two earlier revisions of this plan let it through unchecked.**
Both the spec and those revisions claim "`object.override_create()` returns `None` — verified". Re-measured
from scratch on 2026-09-14 (see §0.3 finding 4 for the transcript): on a **linked, overridable** object
`override_create()` returns **the new override ID**, with `.override_library.reference` pointing back at the
linked original. It returns `None` only on an ID that is **not overridable** — a local datablock, for
instance. The 5.2 API reference documents the return as "New overridden local copy of the ID", type `ID`.

**The ruling below is unchanged; its justification is not.** Do not reach for per-object `override_create`
because it overrides **one ID at a time**, while `create_override`'s job is to override an entire linked
collection's hierarchy — the collection *and the objects inside it* — in a single call.
`override_hierarchy_create` is the hierarchy-level API and is therefore the correct tool. It is **not**
rejected on the grounds that it fails or returns nothing; that reasoning was false and must not be repeated
in a docstring, a comment or TASK_STATE.

#### The three routes, measured end to end against a real linked library

All three were exercised against an actual `.blend` on 2026-09-14, and they differ in exactly one thing that
matters — whether the **objects inside** the override come out editable:

| Route | Call | Collection override | Objects inside | Operator context needed |
|---|---|---|---|---|
| **A** | `bpy.data.libraries.load(link=True, create_liboverrides=True, reuse_liboverrides=True)` | override, editable | `library=True`, `override_library=None`, `is_editable=False` — **objects stay locked** | none |
| **B** | `collection.override_hierarchy_create(scene, view_layer)` | override, editable | `override_library` set, `is_editable=True`, but **`is_system_override=True`** | none |
| **C** | `collection.override_hierarchy_create(scene, view_layer, do_fully_editable=True)` | override, editable | `override_library` set, `is_editable=True`, **`is_system_override=False`** | none |

**Route A's advertised advantage is not real.** An earlier draft preferred Route A because it needs "no operator
context required". Routes B and C need none either — both were driven with `bpy.data.scenes[0]` and
`scene.view_layers[0]`, with `bpy.context` never touched, and both worked headlessly in `--background`. Context
is therefore not a discriminator between the three; editability is the only one.

#### Ruling: Route C is the primary route for `create_override`

`collection.override_hierarchy_create(scene, view_layer, do_fully_editable=True)`.

The reasoning, stated so it can be overturned on evidence rather than taste:

- **Shot assembly needs editable object overrides.** The whole point of overriding linked canon in a shot is to
  move, pose, re-parent and locally tweak the instances — that is what §4.5's "create an override" step is for,
  and what §4.1's canon-vs-shot split assumes. Route A produces an override the artist and the agent **cannot
  edit anything inside**, which satisfies the gate sentence literally while failing its purpose.
- **`do_fully_editable=True` (C) rather than the bare call (B)** because B's objects come out with
  `is_system_override=True`. A system override is Blender's "I made this for you automatically, I may resync or
  reclaim it" marking; it is not a stable surface for deliberate, saved, agent-authored edits. C produces
  user-owned overrides (`is_system_override=False`), which is what a shot file should contain.
- **Route A is still used — for the link, not for the override.** `link_canon_library` keeps
  `bpy.data.libraries.load(link=True, ...)`. What changes is that it does **not** pass
  `create_liboverrides=True` as the way to make an override; the override is a separate, explicit
  `override_hierarchy_create(..., do_fully_editable=True)` call. That also keeps `link` and `create_override`
  as two honestly separate commands, which is what the spec's tool table asks for.
- **If an implementer concludes Route A's locked objects are actually wanted** for some deployment (a strict
  read-only canon-reference mode, say), that is a legitimate finding — but it must be **stated explicitly with
  its reasoning in TASK_STATE**, and it must not be arrived at silently by leaving `create_liboverrides=True`
  in place because it was already written. The cost of getting this wrong is a shot file whose canon content
  cannot be touched, discovered in Phase 3.

Step 2 below still requires the implementer to reproduce all three routes themselves. The ruling tells them
what they should expect to see and what to do if they do not.

`ID.override_library` remains the read-only inspection surface: `hierarchy_root`, `reference`,
`is_system_override`, `is_in_hierarchy`, `properties`. **Report `is_system_override`** — it is how a reader
tells a C override from a B one after the fact.

**A working end-to-end run of the whole gate scenario through the data API is recorded in §0.3 finding 5.**
Reproduce it as the task's first step; it is the design's proof of life. Note that finding 5's transcript used
**Route A**, so its objects were locked — reproduce it, then extend it to C.

#### `Library.reload()` exists, and the operators are the awkward route — not the other way round

An earlier draft of this plan stated that `bpy.types.Library` has "no `reload` method, so `wm.lib_reload` /
`wm.lib_relocate` are indeed the only routes". **That is false, and backwards.** Measured 2026-09-14:

- **`Library.reload()` exists** — present in `bpy.types.Library.bl_rna.functions`, documented in the 5.2 API
  reference, and it **works headlessly**: `lib.filepath = new_path; lib.reload()` produces the correctly
  updated library with the new file's datablocks. (`dir(bpy.types.Library)` finds no `reload`, for the same
  reason it finds no `copy` on `Collection` — see §0.3 finding 4.)
- **The operators fail exactly as an earlier draft of this task invoked them.** The `what to build` table used
  to map `reload_library(library_name)` / `relocate_library(library_name, filepath)` onto `wm.lib_reload` /
  `wm.lib_relocate` passing `library` + `filepath`. Every one of those forms raises:

  ```
  lib_relocate(library=name)                                 -> RuntimeError: Not a library
  lib_relocate(library=name, filepath=...)                   -> RuntimeError: Not a library
  lib_relocate(library, directory, filename, filepath)       -> {'FINISHED'}
  lib_relocate(library, directory, filename)                 -> {'FINISHED'}
  lib_reload(library=name)                                   -> RuntimeError: Not a library
  lib_reload(library=name, filepath=...)                     -> RuntimeError: Not a library
  lib_reload(library, directory, filename)                   -> {'FINISHED'}
  ```

  Both operators **require `directory` + `filename` and ignore `filepath` entirely**. Two further traps: a
  **bogus library name returns `{'CANCELLED'}`** rather than raising — the inverse of the real-failure
  behaviour, so a CANCELLED check catches the typo case and misses the substantive one; and `lib_relocate`
  **renames the `Library` datablock** to the new file's basename, invalidating any name-based handle the client
  is holding across the call.

**Ruling: use the data API (`lib.filepath = ...; lib.reload()`), not the operators.** It is simpler, verified
working headlessly, takes the path in the form the handler already has it, has no `directory`/`filename`
splitting to get wrong, raises on failure like the rest of the phase's error handling, and does not silently
rename the datablock out from under the caller's handle. `relocate_library` becomes "assign `filepath`, then
`reload()`", which is exactly what the operator does anyway minus the file-browser plumbing.

Two consequences the implementer must carry:

1. **Resolve the library by `session_uid`, not by name, wherever a handle crosses a call.** Names are not
   stable across a relocate under either route. **The command signatures in "What to build" implement this
   literally** — `create_override(collection_uid)`, `reload_library(library_uid)`,
   `relocate_library(library_uid, ...)`, `unlink_libraries(library_uids, ...)`. An earlier revision stated this
   ruling and then wrote name-based parameters two paragraphs later; the one documented exception is
   `link_canon_library`'s `collections` / `objects`, which name contents of the **file being opened** and
   therefore cannot be uids, because no local datablock exists yet.
2. **If a reviewer overrules this and keeps the operator route**, then the parameter mapping must be fixed —
   split the caller-supplied `filepath` into `directory` + `filename` with `os.path.split` and pass both,
   never `filepath` alone — and the docstring and the tool description must document the rename side effect and
   the inverted `{'CANCELLED'}`-on-bad-name behaviour. Record the override and its reason in TASK_STATE.

Also verified: `bpy.types.Library` exposes `filepath`, `version`, `is_missing`, `needs_liboverride_resync`,
`packed_file`, `is_archive`, `archive_libraries`, `parent`, `users`, `session_uid` — **and `reload`**. Spec
§4.1's remark that `Library` "exposes no checksum, mtime or size" is true, but **`version` and `is_missing` are
available and are worth reporting** — `list_libraries` should surface them. `bpy.data.libraries.remove` and
`bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=False) -> int` exist for unlinking.

**Reporting wrinkle this task must handle, and it is bigger than an earlier revision said.** Any override route
leaves **two collections named `CanonHero`** in `bpy.data.collections` (the linked one and the override).
**Route C — the route this task rules for — additionally leaves two *objects* sharing a name**, the override
and the linked original, plus a local instance empty named after the collection. Measured 2026-09-14; the
transcript is in §0.3 finding 5(d). So the collision is not a collection-level quirk to special-case in one
report; it is the normal state of every datablock type an override touches. `list_libraries`,
`create_override` and every handle that crosses a call disambiguate by `session_uid` and by
`library` / `override_library` / `is_system_override` state — **never by name** — and the command signatures in
"What to build" are written that way rather than taking names and hoping.

### Hazards carried forward

- **Security** — every library path goes through Task 5. A canon library is typically on a read-only mount;
  `BLENDERMCP_FILE_ROOTS` is what constrains it.
- **Path leaks — this task's data-API calls leak absolute paths exactly as Task 6's operators do.** Do not
  assume the sanitizer is only Task 6's problem because Task 6 is where `open_mainfile` lives.
  `Library.reload()` embeds the full absolute path in its own exception text. Measured 2026-09-14 against
  5.2.1, after pointing a linked library at a missing file:

  ```
  RuntimeError: Error: Trying to reload library 'LIlib_plain.blend' from invalid path
                '/private/tmp/…/scratchpad/bl/gone.blend'
  ```

  So `reload_library` and `relocate_library` — the two commands that call `lib.reload()` — reach the same
  automatic-critical rubric item ("an absolute filesystem path reaching the client in any error message") on
  their most ordinary failure path. Every `except` around a data-API or operator call in this module routes its
  text through Task 5's `sanitize_blender_error` before it reaches the client. See acceptance criterion 8.
- **Rollback — this is the hazard that was missing, and it is the phase's data-destruction hazard reappearing
  on three of this module's six commands.** An earlier revision of this section carried only the failed-link
  *leak*. The larger hazard runs the other way: **`reload_library`, `relocate_library` and `unlink_libraries`
  are not session swaps, so `_run_handler:1131` wraps each of them in `mutation_transaction`** — and
  measured 2026-09-14 against 5.2.1 (§0.3 finding 7, Task 4 item 2a):

  - `lib.reload()` gives **every datablock linked from that library a fresh `session_uid`** (3 of 3 measured),
    so a raise anywhere later in the same transaction — including `_run_handler:1136-1138` converting a
    returned failure shape into `HandlerReportedError` — makes `rollback()` classify the whole reloaded
    library's contents as newly created and **delete them**, silently, inside `suppress(Exception)`;
  - **`load_post` never fires for a reload** — only `blend_import_pre` / `blend_import_post` — so Task 4's
    original `load_post`-only invalidation does not trigger;
  - **and `blend_import_post` cannot be used as a blanket trigger**, because `link_canon_library`'s own
    `bpy.data.libraries.load()` fires it too, and *that* command's new datablocks must still roll back;
  - **`unlink_libraries` has no handler at all** — `orphans_purge` and `libraries.remove` fire nothing — and
    its hazard is the mirror image: datablocks *freed* under a live `_before_ids` / `ObjectState`.

  **Task 4 owns the fix** (`_DATABLOCK_REPLACING_COMMANDS`, bypassing the transaction entirely, plus a
  flag-guarded `blend_import_post` handler). **This task consumes it and must verify it against the real
  handlers**, not only against Task 4's stubs: assert that all three of these commands take the untransacted
  branch, and that `link_canon_library` still takes the transacted one with its failed-link rollback intact.
  If Task 4 has not landed, **do not write these three handlers** — a `reload_library` shipped before that
  constant exists can delete a linked library's entire contents on its own error path.
- **Ordering** — `reload_library` and `relocate_library` mutate linked data in place; they are **not** session
  swaps and must not trip Task 3's barrier (they are in `_DATABLOCK_REPLACING_COMMANDS`, which bypasses the
  transaction without invoking the barrier — the two constants are separate and must not be merged), but they
  do invalidate datablock references held by anything mid-flight. State that in their docstrings.
- **Non-destructive-by-default (CLAUDE.md)** — `unlink_libraries` removes data. It requires explicit
  confirmation, must be scoped to an explicit library list (never "purge everything"), and must report exactly
  what it removed.

### What to build

`src/blender_mcp/bundled/addon/handlers/linking.py`:

**Signatures: `session_uid` where a handle crosses a call, and no mutable defaults.** An earlier revision of
this table contradicted its own ruling two paragraphs above it — it said "resolve by `session_uid`, never by
name" and then wrote `create_override(collection_name=...)` and `unlink_libraries(library_names, ...)`. It also
carried two straightforward Python defects. Both are fixed here rather than left for the implementer to notice:

- **`collections=[]` / `objects=[]` were mutable default arguments** — evaluated once at definition and shared
  across every call. Use `collections: list[str] | None = None` and an internal `or []`.
- **`create_override` was both a *parameter* of `link_canon_library` and the *name of a sibling command*** in
  the same table. That shadowing is confusing in a schema a client reads and in a handler a reviewer reads.
  Renamed to **`as_override`**.

**Which identifier each parameter takes, and why they are not all the same.** The ruling is about handles that
**cross a call**, not about every string in the module:

- **`collections` / `objects` on `link_canon_library` are datablock names by necessity** —
  `bpy.data.libraries.load()` takes names out of the *file being opened*, before any local datablock exists to
  have a `session_uid`. There is nothing else they could be. Document that they name contents of the
  **library file**, not of `bpy.data`.
- **Everything that refers to an already-loaded datablock takes a `session_uid`**, because §0.3 finding 5(d)
  measured the collision at **both** collection and object level on Route C — the route this task rules for —
  and because `relocate_library` can change a `Library`'s name mid-call.
- Name-based lookup stays available only as a **resolution helper that refuses on ambiguity**: given a name
  that matches more than one datablock, it must raise an actionable error listing the candidates with their
  `session_uid`s, never silently return the first. That is the one safe way a client that has only a name can
  get to a uid, and it is the shape `list_libraries`' output feeds.

| Command | Route |
|---|---|
| `link_canon_library(filepath, *, collections=None, objects=None, as_override=False, relative=False)` | `bpy.data.libraries.load(link=True, ...)` for the link. `collections` / `objects` are names **inside the library file** (see above); `None` means "none of that type", normalised internally to `[]`. When `as_override=True`, follow the link with the Route C call below rather than passing `create_liboverrides=True` — the two are not equivalent (see the ruling). Returns what was linked, the library's `filepath`/`version`/`session_uid`, and the override's `session_uid` when created. |
| `create_override(collection_uid, *, ...)` | **Route C:** `collection.override_hierarchy_create(scene, view_layer, do_fully_editable=True)`, with `scene` / `view_layer` taken from `bpy.data`, not from `bpy.context`. **Takes the linked collection's `session_uid`**, not its name — after any override route there are two collections with the same name, and after Route C two *objects* with the same name as well (§0.3 finding 5(d)). Reports `hierarchy_root`, the override's `session_uid`, and `is_system_override` (expected `False`). |
| `list_libraries(*, limit, offset)` | Walk `bpy.data.libraries`; report `filepath`, `name`, `version`, `is_missing`, `needs_liboverride_resync`, `users`, `session_uid`, and the datablocks linked from each — **`Library.users_id` gives you that list directly** (it is a Python-level property absent from `bl_rna.properties`; see Global Constraints), so do not hand-roll a `bpy.data` walk. Report each linked datablock's `session_uid` alongside its name, since the names are not unique. **Paginated** per CLAUDE.md's bounded-inspection rule. **This is a pure read: add it to `_READ_ONLY_COMMANDS`** — see the note below the table. |
| `reload_library(library_uid)` | **Data API:** resolve the `Library` by `session_uid`, call `lib.reload()`. Raises on failure — there is no `{'CANCELLED'}` to check on this route. In `_DATABLOCK_REPLACING_COMMANDS` (Task 4 item 2a), so it bypasses `mutation_transaction`. |
| `relocate_library(library_uid, filepath)` | **Data API:** validate the new path through Task 5, then `lib.filepath = <validated>; lib.reload()`. Do **not** use `wm.lib_relocate` — it needs `directory` + `filename` (it ignores `filepath`), returns `{'CANCELLED'}` for a bad name, and renames the `Library` datablock. **The rename is exactly why this takes a `session_uid`**: a name-based handle is invalid the moment the call returns. Report the library's `name` before and after. In `_DATABLOCK_REPLACING_COMMANDS`. |
| `unlink_libraries(library_uids, *, confirm=False, purge_orphans=False)` | `bpy.data.libraries.remove` on the libraries whose `session_uid`s were **explicitly named**, and nothing else; `orphans_purge` only when explicitly asked. Reports counts and the uids acted on. In `_DATABLOCK_REPLACING_COMMANDS` — and note that `orphans_purge` fires **no handler at all**, so the bypass is the only protection available (Task 4 item 2a.4). |

**`_READ_ONLY_COMMANDS` extension.** `list_libraries` reads and mutates nothing, so it belongs in
`_READ_ONLY_COMMANDS` (currently at `server_core.py:813-870`, **54 entries** — re-read the actual file and line
range before editing; Task 3 already added `get_session_info` and every earlier task moves these numbers).
Without the entry it gets a full 18-collection `_snapshot_ids()` plus a rollback wrapper on every call, for a
command that changes nothing — pure cost, and misleading to anything that reasons about which commands mutate.
An earlier revision of this plan added `get_session_info` there and never mentioned `list_libraries`. **The
other five commands in this module do not go in that set** — they are not read-only, and the three that replace
or free datablocks use `_DATABLOCK_REPLACING_COMMANDS` instead (Task 6's hazard note says why
`_READ_ONLY_COMMANDS` must not be used as a shortcut for "skip the transaction").

### Steps

- [ ] **Step 1 — Reproduce §0.3 finding 5's smoke test yourself**, with your own fixture, and paste the output.
  Do not start from this plan's transcript.
- [ ] **Step 2 — Confirm the override-route ruling, do not re-open it blind.** Run all three routes (A, B, C)
  against your own fixture and, for each, record for the collection *and for one object inside it*:
  `library`, `override_library`, `is_editable`, `is_system_override`, and whether `bpy.context` was needed. The
  expected outcome is the table above and the ruling for Route C. **If your measurements match, implement C and
  cite this section.** If they do not match, stop — a divergence here means either the fixture or the build
  differs, and guessing past it is exactly the failure §0.3 finding 4 records. Record the transcript either way.
- [ ] **Step 2b — Confirm the `reload` ruling.** Show `Library.reload` present in
  `bpy.types.Library.bl_rna.functions`, then exercise `lib.filepath = <new>; lib.reload()` end to end and paste
  the before/after datablock listing. If you also try the operators, paste their `RuntimeError` / `{'CANCELLED'}`
  behaviour so the record shows *why* the data API was chosen rather than only that it was.
- [ ] **Step 3 — Write the failing tests** against the stubbed harness: link validates its path; a link failure
  removes the library; `list_libraries` paginates and reports `is_missing`; `unlink_libraries` refuses without
  confirmation and never touches a library that was not named; `create_override` passes
  `do_fully_editable=True` **explicitly** in the recorded call (assert the kwarg is present — the default is
  `False` and inheriting it silently produces system overrides); `create_override` sources `scene` /
  `view_layer` from `bpy.data`, not `bpy.context`; `reload_library` / `relocate_library` call `lib.reload()`
  and **never** `bpy.ops.wm.lib_reload` / `lib_relocate`; a library is resolved by `session_uid` where a handle
  crosses a call; same-named linked and override collections **and same-named linked and override *objects***
  are reported distinguishably (finding 5(d) — assert on the object case too, since Route C is what ships);
  a name-based resolution helper **refuses on ambiguity** rather than returning the first match;
  `link_canon_library`'s `collections` / `objects` default to `None` and are **not** shared across calls
  (call it twice with no arguments and assert the second call sees an empty list); `list_libraries` is in
  `_READ_ONLY_COMMANDS`; **`reload_library`, `relocate_library` and `unlink_libraries` take the untransacted
  branch while `link_canon_library` does not** (Task 4 item 2a — assert both directions against these real
  handlers, not only against Task 4's stubs); **and a `RuntimeError` raised by `lib.reload()` reaches the client
  sanitized, with no absolute path in it** (criterion 8 — capture the real exception, do not hand-write the
  string).
- [ ] **Step 4 — Run; watch fail; implement.**
- [ ] **Step 5 — Real-Blender verification** of link → override → save → reopen, with the library reported
  `is_missing=False` after the reopen **and an object inside the override still `is_editable=True`,
  `is_system_override=False`** — the second half is what distinguishes a Route C result from a Route A one and
  it is the clause the gate's purpose depends on. This is the phase gate's core, exercised in isolation before
  Task 10 exercises it over the socket.
- [ ] **Step 6 — Prove the tests can fail; gates; commit.**

### Acceptance criteria

1. Step 1's reproduction, Step 2's three-route measurements and Step 2b's `reload` evidence are recorded, and
   any divergence from this plan's rulings is escalated rather than silently resolved.
2. Link → override → save → reopen demonstrated against a real Blender, with the reopened library intact **and
   the override's objects still editable and not system overrides**.
3. `create_override` uses `override_hierarchy_create(..., do_fully_editable=True)` with the kwarg passed
   explicitly, asserted by test; `create_liboverrides=True` is not used as the override mechanism.
4. `reload_library` / `relocate_library` use `Library.reload()`; no `wm.lib_reload` or `wm.lib_relocate` call
   exists in the handler — asserted by test, not by inspection. If the operator route was chosen instead, the
   `directory` + `filename` mapping, the rename side effect and the inverted-`{'CANCELLED'}` behaviour are all
   implemented, documented and tested, and the override is recorded in TASK_STATE.
5. `unlink_libraries` cannot touch a library whose `session_uid` was not explicitly passed — proven by a test.
6. `list_libraries` is paginated, reports `is_missing` and `version`, and is in `_READ_ONLY_COMMANDS` (asserted,
   not assumed — it is a pure read and must not be paying for a transaction).
7. A failed link leaves no `Library` datablock behind — verified against real handlers. **And the converse:
   `reload_library` / `relocate_library` / `unlink_libraries` provably do not enter `mutation_transaction` at
   all** (Task 4's `_DATABLOCK_REPLACING_COMMANDS`), asserted against these handlers. Without that, a reload
   that raises anywhere on its error path deletes every datablock the library contributed — §0.3 finding 7
   measured every one of their `session_uid`s changing across a single `lib.reload()`.
7a. **No parameter of any command in this module takes a datablock *name* as a handle into `bpy.data`** —
   asserted by test against the command schemas. The one permitted exception is `link_canon_library`'s
   `collections` / `objects`, which name contents of the library file; the test names it explicitly so the
   exception stays deliberate.
8. **No absolute path reaches the client from any failure in this module** — the mirror of Task 6's criterion 4,
   and it applies here for the same reason: Blender writes the absolute path into its own exception text.
   `Library.reload()`'s failure message is
   `Error: Trying to reload library '<name>' from invalid path '<abs path>'` (measured 2026-09-14). Prove it
   against a *captured real* `RuntimeError` — point a linked library at a missing file, call `lib.reload()`,
   catch it — and assert the sanitized response still names the cause ("invalid path", the library) while
   containing no absolute path. A hand-written string is not evidence for this criterion.
9. Full suite green.

---

## Task 8: Socket authentication — design deliverable, no implementation

**Recommended implementer: Opus-class.** This is the second of the two open design questions, it is a threat
model rather than a code change, and a weak answer here is the one that Phase 5's pooled deployment inherits.

### Why

Spec §10 Q8: *"Socket authentication. None exists. Dominant risk once pooled and multi-tenant, and
`open_shot`/`save_shot` widen it to arbitrary-path read and overwrite."* Appendix A lists it as **open** with no
proposal. Spec §8 puts "Socket authentication" in Phase 4. Phase 2 is what creates the exposure, so Phase 2
owes the design even though it does not owe the code.

### Current state, verified

- No authentication of any kind, at any of the three places the work is split across. **Be precise about which
  function does what — an earlier revision of this line said `handle_client` "accepts any connection on the
  bound port and executes anything in the dispatch table", and it does neither:**
  - **`_server_loop` (`server_core.py:239-265`) accepts** — `socket.accept()` in a loop, spawning a daemon
    handler thread per client, with no credential check of any kind;
  - **`handle_client` (`:393-460`) frames and queues** — it reads bytes, splits on `b"\n"`, and hands each
    frame to `_decode_and_queue_frame` (`:342-391`), which validates only the `id` / `type` / `params`
    *shapes*. It executes nothing;
  - **`drain_command_queue` (`:267-321`) → `execute_command` → `execute_command_internal` (`:872-913`)
    executes**, on Blender's main thread, against the `_build_command_handlers()` table.

  The absence of authentication is therefore an absence at `_server_loop` (anyone may connect) and at
  `_decode_and_queue_frame` (any well-shaped frame is queued), and §4.8's mechanism has to name which of the
  two it attaches to. That is a design constraint, not pedantry: a per-connection secret belongs at the first,
  a per-frame one at the second, and they have different failure modes for the multi-process case below.
- The only current control is deployment shape: the addon binds `localhost` by default
  (`server_core.py:100`), and `docker/blender/docker-compose.yml` publishes only `127.0.0.1:8000` with an
  explicit comment that there is no authentication.
- `README.md:85-89` and `:277-294` document running **multiple server processes against one Blender**, so any design must work
  for N clients against one addon.
- `connection.py:195-196` gives one in-flight request per connection; `server_core.py:1156` already carries a
  per-connection-agnostic capability handshake that a token could ride alongside.
- Task 5 will have shipped the filesystem boundary, which is a *containment* control, not an *authentication*
  one. The design must say clearly which risks each covers.

### What to produce

No code. Three documents:

1. **A new spec section `§4.8 Socket trust boundary`**, inserted after §4.7, covering:
   - the threat model: who can reach the port in each of the three deployments (local artist workstation,
     the Docker rig, the hosted warm pool), and what they gain;
   - what is and is not authenticated today, stated plainly;
   - the proposed mechanism, with the alternatives considered and rejected. At minimum evaluate: a shared
     secret in the handshake frame (simplest, works for N processes, must not be logged —
     CLAUDE.md forbids logging credentials); a Unix domain socket with filesystem permissions (strongest
     locally, does not exist on Windows, changes the transport); per-process tokens issued by the addon UI; and
     mutual TLS (almost certainly over-engineered — say why);
   - how it interacts with the existing handshake and with `connection.py:185-190`'s capability gate;
   - how it degrades: what an un-authenticated client sees, and whether an unset secret means permissive (the
     same asymmetry Task 5 takes, and it should be resolved the same way or the divergence justified);
   - the failure mode that matters most — a secret leaking into a log or an error message — and the specific
     code paths that must be audited for it. **The citation list below replaces an earlier one
     (`server_core.py:294`, `:318`, `:366`) that named sites which cannot leak anything.** Re-read at source
     2026-09-14; **re-verify the line numbers yourself before writing §4.8, since every earlier task moves
     them** — grep for `print(` and `traceback` in `server_core.py` rather than trusting these:

     | Site | Why it is leak-capable |
     |---|---|
     | `server_core.py:292` `print(f"Error executing command: {e!s}")` | interpolates an arbitrary exception, which in this phase carries absolute paths (Task 5's five shapes) |
     | `server_core.py:293` `traceback.print_exc()` | **CLAUDE.md forbids shipping a full traceback**; it also prints every frame's locals-adjacent context to the addon's stdout, where `docker logs` collects it |
     | `server_core.py:365` `print(f"Discarding malformed message: {e!s}")` | the `JSONDecodeError` / `UnicodeDecodeError` text **echoes frame content**, which is where a handshake secret would sit |
     | `server_core.py:476-477`, `:909-910`, `:1226-1227` | three further `print(f"...{e!s}")` + `traceback.print_exc()` pairs on the same pattern — the audit must cover the class, not three instances of it |
     | `connection.py:206`, `:234-241`, `:260` | server-side; unchanged from the earlier list |

     **The two sites the earlier list named that cannot leak**, recorded so the correction is auditable:
     `server_core.py:318` is `print("Failed to send response - client disconnected")` — a **constant string**
     with no interpolation at all; and `:360` is `print(f"Client sent an oversized frame ({len(line)} bytes)
     - disconnecting")`, which interpolates only a **length**. Neither can carry a secret. Citing them as the
     audit targets while the real `{e!s}` and `traceback.print_exc()` sites went unnamed is the failure mode
     §4.8 is supposed to prevent, appearing inside §4.8's own instructions.
2. **A decision record in `PHASE2_TASK_STATE.md`**, in the same "cost if wrong" format Phase 1 used for its
   twelve rulings.
3. **A scoped Phase 4 work item** — the tasks implementation would need, so Phase 4 inherits a plan rather than
   a paragraph.

### Steps

- [ ] **Step 1 — Enumerate the reachable surface.** For each of the three deployments, state exactly what an
  attacker who can open a TCP connection to the addon port can do *after Phase 2*, naming commands. Include
  `open_shot` (arbitrary read, subject to Task 5's roots), `save_shot` (arbitrary overwrite, same), and the
  pre-existing `render_scene` / export tools that already write files.
- [ ] **Step 1b — Name the `.blend`-as-code-execution risk explicitly, as its own threat-model entry.** A
  `.blend` can carry embedded Python that Blender runs on load. `wm.open_mainfile`'s `use_scripts` defaults to
  `False` and is additionally gated by `preferences.filepaths.use_scripts_auto_execute`, also `False` by default
  (both measured 2026-09-14) — but **two safe defaults are not a control**, and this plan's own Global
  Constraints say "No arbitrary code execution, in any form" (spec Decision #7). The threat is: an unauthenticated
  caller who can reach the socket and cause a chosen `.blend` to exist inside the configured roots gets code
  execution inside Blender *without touching `exec`/`eval`*, if that preference is ever on. **The containment is
  split across two tasks and §4.8 must attribute it correctly, because §4.8 is a permanent spec section that
  outlives this plan:**
  **There are three controls here, not two, and they belong to three different places.** An earlier revision of
  this step attributed both the explicit argument and the preference check to "Task 6 Step 4b", collapsing a
  distinction that Task 6's own tier rationale and the handoff's §06 were **already corrected** to separate.
  That matters more here than anywhere else in the plan, because §4.8 is a **permanent spec section that
  outlives this plan** — a conflation shipped here is inherited by Phases 4 and 5 as the record of what was
  decided. Attribute it as three:

  | # | Control | Owner | Kind |
  |---|---|---|---|
  | 1 | **`use_scripts` is never exposed in any tool or command schema** — not as an opt-in, not "for advanced users" — in Tasks 6, 7 or 9 | **Task 5**, requirement 1 (its criterion 6) | *Static.* A property of the schemas, checkable without running anything. |
  | 2 | **Every `wm.open_mainfile` call passes `use_scripts=False` explicitly**, rather than inheriting the operator default | **Task 6**, criterion 5 | *Runtime, and **on §07's automatic-critical list**.* The rubric entry reads "`use_scripts` exposed as a parameter, **or** a `wm.open_mainfile` call that does not pass `use_scripts=False` explicitly" — controls 1 and 2 are its two halves. Relying on the safe default satisfies neither. |
  | 3 | **`preferences.filepaths.use_scripts_auto_execute` is read before the load**, with a refuse-vs-warn decision | **Task 6 Step 4b**, criterion 6 | *Runtime, and **not** on the automatic-critical list.* It is the phase's one genuinely **undecided** element (recommendation: refuse), carrying a security judgement on an unauthenticated socket. |

  Controls 2 and 3 both live in Task 6 and are still **not the same control**: 2 is mechanical, automatic, and
  automatically critical if missed; 3 is a judgement that must be recorded in TASK_STATE with its reason.
  Neither can live in Task 5 — its criterion 2 mandates that `file_paths.py` import no `bpy`, so it can reach
  neither the operator call nor `bpy.context.preferences`. **Do not restate the superseded attribution that
  gave the preference check to Task 5, and do not restate the one that folded control 2 into Step 4b.**

  §4.8 owes the **threat statement**: who can reach it in each deployment, what authentication would
  and would not change about it, and the fact that this is the one Phase 2 exposure that authentication alone
  does not close — a *legitimately authenticated* client opening a hostile `.blend` is the same execution. Say
  which control actually closes it (the preference check and, later, content provenance) and which merely
  narrows who can attempt it.
- [ ] **Step 2 — State what Task 5 did and did not close.** Be precise: root enforcement is containment; it does
  not stop an unauthorised client from driving Blender inside the roots.
- [ ] **Step 3 — Evaluate the four mechanisms** above against the constraints: works for N server processes
  against one addon; works on macOS, Linux and Windows; vendor-agnostic (spec §1 — the client may not be
  Claude); no arbitrary code execution (Decision #7); does not break the existing handshake.
- [ ] **Step 4 — Choose one, and write §4.8.** Include the rejected options and the specific constraint that
  rejected each.
- [ ] **Step 5 — Write the Phase 4 work item and the decision record.**
- [ ] **Step 6 — Commit as a docs change.** No `src/` file may be touched by this task.

### Acceptance criteria

1. Spec §4.8 exists, names a chosen mechanism, and records the rejected alternatives with disqualifying reasons.
2. The threat model covers all three deployments and names commands, not categories, **and carries the
   `use_scripts` / hostile-`.blend` execution risk as a named entry** with the distinction between what
   authentication closes and what it does not. **Its containment is attributed to all three of Step 1b's
   controls, to the right owners** — Task 5 requirement 1 (never exposed in a schema), Task 6 criterion 5
   (explicit `use_scripts=False`, automatic-critical), Task 6 Step 4b / criterion 6 (the preference check, the
   undecided refuse-vs-warn one). A §4.8 that names two controls where there are three fails this criterion,
   because §4.8 outlives the plan that would otherwise correct it.
3. The secret-leakage audit lists specific `file:line` log/error sites, **each verified leak-capable at the
   revision §4.8 is written against** — an interpolated `{e!s}`, a `traceback.print_exc()`, or an echoed frame.
   A constant-string `print` cited as an audit target is a failing entry, not a harmless extra one.
4. `git diff --stat` for this task shows changes only under `docs/`.
5. Spec §10 Q8 and Appendix A's "Socket has no authentication — open" row are updated to point at §4.8.

---

## Task 9: Server-side MCP tools, bundle placement, and the payload ceiling

**Recommended implementer: Sonnet-class.** Mechanically similar to Phase 1's work, against a test suite that
already pins everything that could silently go wrong — provided the ceiling decision below is followed rather
than rediscovered.

### Why

The addon commands from Tasks 6 and 7 are unreachable from an MCP client until tools exist for them, and
`connection.py:185-190` will refuse to send anything the handshake does not advertise.

### Current state, verified — the ceiling is the trap

- `shot` measures **exactly 203,094 B** and `tests/server/test_bundles.py:509` pins
  `SHOT_MODE_BYTE_CEILING = 203_094` with a `<=` assertion. **There is zero headroom.** Any tool added to a
  bundle inside `shot` mode fails that test immediately.
- The ceiling's own comment says raising it "requires a deliberate decision recorded in the commit message".
- `tests/server/test_bundles.py:541-551` parses `285` out of `bundles.py:8`'s docstring and asserts the catalog
  matches; adding tools moves that number.
- `test_readme_documents_every_bundle_name` and `test_readme_documents_every_mode_name`
  (`test_bundles.py:598-617`) require every bundle and mode name to appear backticked in README.
- `test_shot_and_asset_modes_share_only_the_core_surface` requires the two modes to be disjoint **outside
  core** — so a bundle added to *both* modes would break it unless the tools live in `CORE_MODULES` instead.
- `tools/_documentation.py` decides `destructiveHint` / `readOnlyHint` from **three** mechanisms, not two.
  Re-read at source 2026-09-14 (`_is_destructive`, `_documentation.py:548-565`), which returns the `or` of:
  1. **a name prefix** — `name.startswith(_DESTRUCTIVE_PREFIXES)`;
  2. **explicit-set membership** — `name in _DESTRUCTIVE_TOOLS`;
  3. **a `conditional_flags` check against the tool's own schema property names** —
     `conditional_flags.intersection(schema.get("properties", {}))`, where `conditional_flags` is
     `{apply, commit, confirm_baked_removal, confirm_delete_baked_cache, confirm_free, confirm_free_bake,
     confirm_overwrite, confirm_replace_weights, overwrite, replace_existing}`. **A tool that takes a
     `confirm_overwrite` parameter is therefore auto-classified destructive with no explicit entry at all.**
     An earlier revision of this section described only mechanisms 1 and 2 and so mis-counted what needs
     registering. Read `_is_destructive` yourself before writing Step 4; do not trust this list's membership.

  `readOnlyHint` is `_is_read_only(name)` — `_READ_ONLY_PREFIXES` minus `_MUTATING_READ_PREFIXES` — prefix only,
  with no explicit set and no schema mechanism.

- **There is a THIRD hint, `openWorldHint`, and an earlier revision of this task never mentioned it.** Read at
  source 2026-09-14, `_documentation.py:629`:

  ```python
  openWorldHint = tool.name in _EXTERNAL_TOOLS or tool.name in _FILE_TOOLS
  ```

  It is emitted **unconditionally, on every registered tool**, by the same
  `finalize_tool_documentation` call that emits the other two — so the argument that already carried
  `destructiveHint` applies here verbatim and with the same force: a file-touching tool absent from both sets
  does not ship "no hint", it ships **`openWorldHint=False`**, an affirmative statement to the client that the
  call does not reach anything outside Blender's own data. For `save_shot` that is simply untrue; for
  `open_shot` and the three library commands it is untrue *and* it is the hint a cautious client would use to
  decide whether a call needs review.

  `_FILE_TOOLS` today is `{bake_retopology_maps, export_cloth_simulation, export_liquid_simulation,
  export_rigid_body_animation, manage_geometry_nodes_bake, render_lighting_preview, setup_liquid_shot,
  render_scene}` and `_EXTERNAL_TOOLS` is the six Poly Haven / Sketchfab tools. **Five of Phase 2's ten tools
  touch the filesystem and are in neither set:** `open_shot`, `save_shot`, `link_canon_library`,
  `reload_library`, `relocate_library`. (`reset_session` reads nothing from disk —
  `wm.read_factory_settings` — and `create_override`, `list_libraries`, `unlink_libraries`,
  `get_session_info` operate purely on in-memory data. Four of the ten correctly stay out, for the same
  reason four correctly stay out of `_DESTRUCTIVE_TOOLS`, and the counts are coincidental — they are not the
  same four: `unlink_libraries` is destructive but not open-world, `link_canon_library` is open-world but not
  destructive.)

  **`_FILE_TOOLS` is not just a hint set — it also selects prose**, at `_documentation.py:593`:
  *"Side effects: may write the explicit output path and may evaluate Blender data; **it does not save the
  .blend file**."* That trailing clause is **false for `save_shot` and misleading for `open_shot`**, so simply
  adding the five to `_FILE_TOOLS` fixes the hint and ships a wrong description. Step 4 must handle both — see
  the ruling there.

  **`finalize_tool_documentation` (`:612-630`) emits `readOnlyHint=` and `destructiveHint=` *unconditionally*,
  on every registered tool.** So a destructive tool omitted from `_DESTRUCTIVE_TOOLS` does **not** ship with "no
  hint" — it ships with **`destructiveHint=False`**, an affirmatively wrong hint telling a client the call is
  safe. That is strictly worse than silence and it is why the omission matters.

  Checked per tool against the real tables on 2026-09-14, for **all ten** Phase 2 tools:
  - **Need an explicit `_DESTRUCTIVE_TOOLS` entry — five:** `open_shot`, `reset_session` (`reset_` is *not* in
    `_DESTRUCTIVE_PREFIXES`; only `remove_`, `clear_`, `clean_` and friends are), `unlink_libraries`,
    `relocate_library`, and — **easily missed, and missed once already** — `reload_library`. Each matches no
    prefix, is in no set, and exposes no `conditional_flags` property, so each currently resolves to
    `destructiveHint=False`.
  - **Already destructive via mechanism 3, no entry strictly required — one:** `save_shot`, because its schema
    carries `confirm_overwrite` (Task 6's table). **Add an explicit entry anyway.** The hint would otherwise
    depend silently on a schema key: rename or drop `confirm_overwrite` in a later phase and `save_shot`
    quietly becomes non-destructive with no test failing for the right reason. Redundancy here is cheap; the
    silent coupling is not. Record that it is belt-and-braces, not a fix for an actual gap.
  - **Correctly need NO entry — two:** `link_canon_library` and `create_override`. Both **add** rather than
    destroy, and the precedent is `create_geometry_object`, which is deliberately *not* in `_DESTRUCTIVE_TOOLS`
    — confirm that against `tests/server/test_bundles.py` (the existing assertion is "create_geometry_object
    adds an object; marking it destructive would devalue the hint"). Marking these two destructive would devalue
    the hint the same way.
  - **Prefix-derived read-only, no entry needed — two:** `list_libraries` (`list_`) and `get_session_info`
    (`get_`).

  So the single correct count is: **five tools require a new `_DESTRUCTIVE_TOOLS` entry** (six if `save_shot`'s
  recommended belt-and-braces entry is added, as it should be); the other four need none, for two different and
  both-correct reasons. `test_scene_authoring_tools_advertise_their_destructiveness` is the precedent for
  asserting the new hints, and Step 2's test must cover every one of the ten **by name** — including the four
  that need no entry — so that a regression in either direction fails rather than passes.

### The three rulings this task must follow

1. **The file tools go in `CORE_MODULES`, not in a bundle added to both modes.** Both `shot` and `asset` need to
   open and save (spec §4.3: `asset` "authors or revises canon through a checkout-and-publish flow"), and the
   disjointness test forbids a shared non-core bundle. Opening a file is also the closest thing this server has
   to a universal operation — `bundles.py:13-14`'s own test for core membership is "essentially every workflow
   needs it". Put them in a new `file_lifecycle` core module. If that proves wrong, the fallback is a `files`
   bundle named by both modes **and** an amendment to the disjointness test with a recorded reason — do not
   amend that test casually; it is the test that proves the modes are worth having.
2. **The `shot` ceiling is raised deliberately, once, with the measured number.** Phase 2 is additive by design
   (spec §8: "The first genuinely additive work"); a ceiling that forbids the phase's whole point is a ceiling
   applied to the wrong thing. Raise it to the newly measured `shot` figure, record the delta and the
   justification in the commit message and in TASK_STATE, and **add a second ceiling for the default `core`
   surface at the same time**, since core is now where the growth lands and it is currently pinned by nothing.
3. **Budget: the Phase 2 tools must add no more than 15,000 B to the `shot` surface — and at ten tools that
   budget has *zero* margin.** Do the arithmetic honestly, because an earlier revision of this ruling did not:
   it said "nine tools is ~13.5 KB", implying ~1.5 KB of slack. **Phase 2 defines ten addon commands**
   (`get_session_info`, `open_shot`, `save_shot`, `reset_session`, `link_canon_library`, `create_override`,
   `list_libraries`, `reload_library`, `relocate_library`, `unlink_libraries`) and therefore ten tools. At spec
   Decision #5's ~1.5 KB-per-intent-tool target, **10 × 1,500 B = 15,000 B — exactly the whole budget, with
   nothing left over.**

   The consequence to plan around, rather than discover at Step 5: **every tool that lands above ~1.5 KB must be
   paid for by one that lands below it.** There is no headroom to absorb a single verbose schema. Decision #5's
   policy is minimize, not fill, so treat 1.5 KB as a per-tool ceiling and not an allowance.

   Measure with `scripts/measure_catalog.py`. If the addition overruns 15,000 B, **trim schemas — do not raise
   the budget**, and do not raise the `shot` ceiling by more than the trimmed figure. Report the measured
   per-tool and total deltas either way, so the next phase inherits a real number instead of a target. If Task 2
   selected the async branch and a genuinely *eleventh* tool is required, this budget must be re-derived and
   re-justified, not stretched. (Note that the async branch's poll tool is `get_session_info`, which is already
   one of the ten — see §0.4.)

### Files

- Create: `src/blender_mcp/server/tools/file_lifecycle.py`, `tests/server/tools/test_file_lifecycle.py`.
- Modify: `src/blender_mcp/server/bundles.py`, `src/blender_mcp/server/tools/_documentation.py`,
  `tests/server/test_bundles.py`, `README.md`.

### Steps

- [ ] **Step 1 — Measure the baseline first.** `scripts/measure_catalog.py` for `all`, `shot`, `asset` and the
  default, at the current revision. Record with the revision, as Phase 1 did.
- [ ] **Step 2 — Write the failing tests**: every new tool is reachable from both modes; each advertises the
  right `destructive` / `read_only` **and `open_world`** hint, asserted per tool by name in both directions
  (Step 4b — three hints × ten tools); `all`'s count matches `bundles.py`'s docstring figure; the default
  surface stays under its new ceiling; `shot` stays under its new ceiling.
- [ ] **Step 3 — Write the tools**, one per addon command, in the existing style (`@mcp.tool()`, `Context`
  first, Google docstring with a full `Args`/`Returns`, returning `ok(...)` from `envelope.py`, raising
  `ToolError` on failure). **Descriptions are the binding term** (spec §4.6): they are the only place a client
  learns that `save_shot` defaults to uncompressed, that `confirm_overwrite` is required, and that `open_shot`
  invalidates the session. Write them for an agent, and keep each tool near the ~1.5 KB target.
- [ ] **Step 4 — Register the annotations** in `_documentation.py`. **"Register all ten" is not implementable
  and an earlier revision of this plan said it anyway** — `_DESTRUCTIVE_TOOLS` is the only registration surface
  here, and four of the ten must *stay out* of it (two are read-only by prefix, two must not be marked
  destructive at all). What actually needs registering:
  - **Add to `_DESTRUCTIVE_TOOLS` — five required:** `open_shot`, `reset_session`, `unlink_libraries`,
    `relocate_library`, **`reload_library`**. `reload_library` belongs here because `lib.reload()` **replaces
    the contents of every datablock linked from that library** — the same class of in-place data replacement as
    `relocate_library`, and it invalidates references held by anything mid-flight (Task 7's ordering hazard says
    so explicitly).
  - **Add to `_DESTRUCTIVE_TOOLS` — one recommended:** `save_shot`. It is *already* destructive via
    `_is_destructive`'s third mechanism, because its schema exposes `confirm_overwrite` and that key is in
    `conditional_flags`. Add the explicit entry anyway so the hint does not silently depend on a schema key
    surviving a later refactor, and say in the commit message that it is redundant-on-purpose rather than a
    fix.
  - **Register nothing — two, deliberately:** `link_canon_library` and `create_override`. They add rather than
    destroy; mirror the `create_geometry_object is not destructive` precedent (assert it, per
    `tests/server/test_bundles.py`). Leaving them out is the *correct* outcome here, not an omission — but it
    must be an asserted one, because "absent from the set" and "forgotten" look identical in a diff.
  - **Register nothing — two, by prefix:** `list_libraries` (`list_`) and `get_session_info` (`get_`) are
    read-only via `_READ_ONLY_PREFIXES` and correct as-is.

  Verify each name against `_documentation.py`'s real tables rather than assuming, and **read `_is_destructive`
  itself** — checked 2026-09-14, `reload_` and `reset_` match no entry in `_READ_ONLY_PREFIXES` and none in
  `_DESTRUCTIVE_PREFIXES`, so each gets a correct hint **only** if added explicitly. Five required entries + one
  recommended + four deliberate non-entries = **ten**, which must equal the number of tools Step 3 wrote.

  **Why an omission is not benign:** `finalize_tool_documentation` emits `destructiveHint=` on every tool
  unconditionally, so a destructive tool left out ships `destructiveHint=False` — a client is told the call is
  safe. Step 2's test must therefore assert the expected hint for all ten **by name**, in both directions.
- [ ] **Step 4b — Register `openWorldHint` for the five file-touching tools, with the same rigor.** This is the
  third hint, it is emitted unconditionally by the same function, and an earlier revision of this task's
  analysis covered only `destructiveHint` / `readOnlyHint` — so under it, `open_shot`, `save_shot`,
  `link_canon_library`, `reload_library` and `relocate_library` would all have shipped `openWorldHint=False`,
  an affirmatively wrong "does not touch external resources" claim, for exactly the reason the
  `destructiveHint=False` omission was judged worse than silence.

  **Ruling: add a new `_BLEND_FILE_TOOLS` set rather than extending `_FILE_TOOLS`**, and make `openWorldHint`
  the `or` of all three sets. The reason is the prose coupling noted above: `_FILE_TOOLS` membership selects
  the effects sentence ending *"it does not save the .blend file"* (`_documentation.py:593`), which is false
  for `save_shot` and misleading for `open_shot` and the library commands. A set whose membership means two
  different things cannot be extended by a tool that needs one and contradicts the other. The new set gets its
  own effects branch, worded for what these five actually do: read or write a `.blend` at an explicit
  caller-supplied path, subject to the configured roots (Task 5) and, for `save_shot`, to
  `confirm_overwrite`.

  **Verify all of this against `_documentation.py` yourself before writing it** — read `_is_destructive`,
  `_is_read_only` *and* the `openWorldHint=` line at source, exactly as this section's `destructiveHint`
  analysis instructs. The membership lists here were true on 2026-09-14 and Task 9 runs last.

  Step 2's test must assert **`openWorldHint` for all ten by name too**, in both directions — the five that
  must be `True` and the five that must be `False` — and the two directions are not the same partition as the
  destructive one, so the tests cannot be folded together (`unlink_libraries` is destructive and **not**
  open-world; `link_canon_library` is open-world and **not** destructive). Assert all three hints per tool;
  that is thirty assertions, and they are cheap next to a client acting on a wrong one.
- [ ] **Step 5 — Measure again, record the delta, and apply ruling 3's budget check.**
- [ ] **Step 6 — Raise the ceilings with the measured numbers** and update `bundles.py`'s docstring count and
  the README tables.
- [ ] **Step 7 — Prove the new tests can fail; gates; commit** with the measured byte delta in the message, per
  Phase 1's convention.

### Acceptance criteria

1. **All ten** Phase 2 addon commands have exactly one MCP tool each, and each is reachable from both `shot` and
   `asset` — proven by `_tool_names_for_toolsets`, not by reading `bundles.py`. Assert the count is ten.
2. `all`'s tool count and `bundles.py`'s docstring figure agree (the existing test enforces it).
3a. **`openWorldHint` asserted per tool, for all ten by name**, in both directions: `True` for `open_shot`,
   `save_shot`, `link_canon_library`, `reload_library`, `relocate_library`; `False` for the other five. It is
   emitted unconditionally like the other two, so an omitted file-touching tool ships an affirmatively wrong
   "does not touch external resources". The `_FILE_TOOLS` prose coupling is handled per Step 4b's ruling — a
   tool that ships `openWorldHint=True` with the effects sentence "it does not save the .blend file" fails this
   criterion.
3. Destructive/read-only hints asserted **per tool, for all ten by name**, in both directions — the six that
   must be destructive, the two that must be read-only, and the two (`link_canon_library`, `create_override`)
   that must be **neither**, so that a well-meaning later addition to `_DESTRUCTIVE_TOOLS` fails too. Includes
   `reload_library`, which matches no prefix in `_documentation.py` and, absent an explicit entry, would ship
   `destructiveHint=False` — an affirmatively wrong hint, not a missing one, because
   `finalize_tool_documentation` emits the field unconditionally.
4. Measured byte delta recorded. **At ten tools the 15,000 B budget has zero margin** (10 × Decision #5's
   ~1.5 KB target), so an overrun is trimmed rather than accepted, and any overrun that is nonetheless accepted
   is justified explicitly against ruling 3 with per-tool figures — not waved through on "it is only a little
   over".
5. Both ceilings (shot and default) pinned to measured values, with the raise justified in the commit message.
6. README documents every new name (the two README drift tests enforce it).
7. Full suite green.

---

## Task 10: The phase gate scenario

**Recommended implementer: Sonnet-class.** By construction this task writes no new production behaviour; it
composes what Tasks 1–9 built into the exact scenario the spec names, and reports the evidence.

### Why

Spec §8's Phase 2 gate, verbatim: **"Open a shot, link canon, create an override, save, reopen with the link
intact; no hang."** Every preceding task is scoped to make this runnable; this task runs it and proves it.

### Ruling: the scenario drives **addon socket commands**, and the MCP-tool layer is proven by `pytest`

An earlier revision of this section said the scenario drives the gate "entirely over the MCP path — server tool
→ socket → addon handler → response" through `scripts/blender_rig.py`. **Those two halves do not connect, and
item 4 below made the gap concrete** by calling `get_addon_status`, which is implemented in
`src/blender_mcp/server/tools/core.py` and calls `force_addon_handshake()` **inside the MCP server process**.
It is not an addon command, it is not in `_build_command_handlers()`, and nothing reachable over the addon's
newline-delimited JSON socket can invoke it. Task 1's rig speaks that socket and stands up no MCP server
(Task 1's third ruling). The scenario as written could not have run.

Two ways to close it, and the repo's own conventions decide between them:

- **(a) Extend Task 1's rig with a mode that also stands up the MCP server** (or imports its tool functions
  in-process) alongside the addon connection, so the scenario can exercise the true MCP-tool → socket → addon
  path.
- **(b) Write the scenario against addon-level socket commands only** — what the rig actually provides — and
  state explicitly that the MCP-tool-wrapper layer (Tasks 6, 7, 9) is proven by ordinary `pytest` unit tests
  against a stubbed connection, because it is a **different concern**: tool-to-socket-command *mapping*, versus
  socket-command-to-Blender *behaviour*.

**Chosen: (b).** The reasons are the repo's, not a preference:

1. **It is already how this repo tests MCP tools, without exception.** Every server-side tool test calls the
   decorated function's underlying coroutine directly, in-process, against `tests/conftest.py`'s
   `stub_blender_connection` — e.g. `tests/server/tools/test_scene_validate.py:67`,
   `asyncio.run(scene.validate_scene(ctx=None, scene_name="Scene", ...))`, then asserting on
   `connection.calls[0]`. **There is no FastMCP test client or in-memory transport anywhere in `tests/`**
   (checked 2026-09-14); the only place the real `mcp` object is driven is `tests/server/test_bundles.py`,
   which shells out to a subprocess purely to *count* `asyncio.run(mcp.list_tools())`. Option (a) would invent
   a second, live-process convention for one scenario.
2. **The two layers fail in different ways and want different evidence.** Whether `open_shot` the *tool* sends
   the right command with the right kwargs is a pure mapping question, settled deterministically by asserting
   on a recording stub's `calls` — no Blender, no display, no flake, and it runs in the default `pytest`.
   Whether `open_shot` the *command* actually swaps the file without hanging the drain loop needs a real event
   loop and proves nothing about the mapping. Routing the first through a live rig makes it slower and less
   diagnosable without making it more true.
3. **The narrower path is the one that always exists.** The local rig (the Docker-unavailable fallback the
   acceptance criteria allow, and the only mode available on this host) reaches only the addon socket. A
   scenario written against the MCP layer would be unrunnable in precisely the situation the plan already
   plans for.

**What this costs, stated rather than glossed:** nothing in the live gate exercises the tool → connection hop
end to end. Task 9's tests cover the mapping and `connection.py:185-190`'s capability gate covers reachability
(a tool whose command is missing from `_build_command_handlers()` raises before a byte is sent, and §07 makes
that an automatic-critical failure). If Docker *is* available, Step 3 below closes the remainder: the container
runs the real MCP server on `127.0.0.1:8000`, so a `tools/list` + one round-tripped tool call there
demonstrates the hop — as a **supplement**, not as the gate.

### What to build

An end-to-end scenario, runnable through `scripts/blender_rig.py` (Task 1), that drives a **live Blender with
the addon server running** over the **addon socket protocol** — queued command → `drain_command_queue` →
`_run_handler` → addon handler → framed response — and asserts each step. Fixtures are built by the scenario,
not checked in as binaries.

The scenario, step by step, each asserted. **Every name below is an addon command, not an MCP tool.**

1. Build a canon library `.blend` containing a named collection, saved with `compress=False`.
1b. **Build the shot fixture.** Step 3 opens `<shot fixture>`, and **an earlier revision of this scenario never
   created it** — step 1 builds the *canon library*, which is the file that gets linked *into* the shot, not
   the shot itself. Build a second, separate `.blend`: an empty or minimal scene standing in for "the shot",
   saved with `compress=False` to its own path, containing no libraries. It must be a distinct file from step
   1's, because step 5 links step 1's file into the scene step 3 opened, and step 7 saves the result to a
   third path. Three `.blend` paths, all created by the scenario, none checked in.
2. `reset_session(confirm=True)` — start from a known empty state.
3. `open_shot(<shot fixture>)` — assert `session_epoch` incremented, `bpy.data.filepath` matches, and the
   client's next command still works (**this is the "no hang" assertion**).
4. **`get_addon_info`** — assert the handshake payload reports the new epoch, so a client comparing epochs
   would know to re-handshake. **Not `get_addon_status`**, which is a server-side MCP tool
   (`server/tools/core.py`) calling `force_addon_handshake()` in the MCP process and is unreachable over this
   socket — see the ruling above. `get_addon_info` is the addon-side payload `get_addon_status` wraps, so it
   is the same fact one hop earlier. Cross-check it against **`get_session_info`**, which reports the epoch,
   `current_filepath`, dirty state and the last load/save error (Task 3 item 4); the two must agree.
5. `link_canon_library(<canon fixture>, collections=[...])` — assert the library appears in `list_libraries`
   with `is_missing=False`. **Record the library's `filepath` and `session_uid` verbatim here**; step 9
   compares against both, and the uid is what survives a name change (Task 7).
6. `create_override(<the linked collection's session_uid>)` — assert an override exists with a
   `hierarchy_root`, and that an **object inside it** is `is_editable=True` with `is_system_override=False`
   (Task 7's Route C ruling — a Route A result would pass the `hierarchy_root` check and fail this one).
   Resolve the collection by `session_uid`, never by name: after the override there are two collections **and
   two objects** carrying the same names (§0.3 finding 5(d)).
7. `save_shot(<out path>, confirm_overwrite=True)` — assert the file exists on disk and is uncompressed.
   **`save_shot` must have passed `relative_remap=False` explicitly**; if it inherits `save_as_mainfile`'s
   `True` default, Blender rewrites the library paths to `//`-relative form and step 9's filepath comparison
   fails. Assert the recorded call's kwargs, not just the outcome, so the failure is diagnosable.
   **Assert `session_epoch` is unchanged across this step** — a successful save changes no capability, so it
   must not move the counter (Task 3's ruling). Record the epoch before and after and compare; a bump here
   would force a pointless re-handshake on every connected process, and it is the direction an earlier
   revision of the plan had wrong, so the scenario asserts it rather than assuming it.
8. `open_shot(<out path>)` — reopen.
9. `list_libraries` — **assert the link survived**: the filepath **byte-identical to the one recorded in step
   5**, `is_missing=False`, and the override still present with its objects still editable.
10. Throughout: assert **every** request received exactly one response, and that total wall-clock stays far
    below the client's 180 s timeout (`connection.py:150`).

### Steps

- [ ] **Step 1 — Write the scenario** as a runnable script plus a **real** pytest entry point, marked so it is
  **skipped when no live Blender is available** — it must not make the default `pytest` run depend on Blender.

  **Do not follow `tests/blender_*_smoke.py` as the precedent here, as an earlier revision of this step said
  to.** Those scripts are explicitly *not* part of the default run and are **not collected at all**: they
  `import bpy` at module scope (which fails outside Blender) and `pyproject.toml`'s `python_files` pattern does
  not match their name. Structured that way, this task produces **no pytest entry point whatsoever** — which
  contradicts the instruction in the same sentence. The handoff's §08 lists them as "the precedent for scripts
  that need a real Blender and are not part of the default `pytest` run", and that is exactly what they are:
  a precedent for *scripts*, not for a test.

  What actually satisfies the ask: a file matching the standard **`test_*.py`** pattern so pytest collects it,
  importing **no `bpy` at module scope**, which at collection time probes for its dependency — a live rig
  reachable, or `docker compose` available — and calls `pytest.skip(..., allow_module_level=True)` (or a
  `@pytest.mark.skipif` on a module-level availability check) when it is not there. The scenario body lives in
  the runnable script; the test is a thin wrapper that shells out to it and asserts the exit code, so the same
  code serves both entry points and there is no second copy to drift. Register the marker in `pyproject.toml`
  so `-m` can select or exclude it. **Verify by running the default `pytest` on a machine with no live Blender
  and confirming the test reports as *skipped*, not as *not collected*** — those look identical in a summary
  line and only one of them is the thing being asked for.
- [ ] **Step 2 — Run it and capture the full transcript**, request and response.
- [ ] **Step 3 — Run it a second time against the container rig** if Docker is available, to prove it is not
  macOS-specific. If it is not available, say so explicitly rather than claiming parity. **If it is available,
  add the one thing the local rig cannot show:** the container runs the real MCP server on `127.0.0.1:8000`, so
  issue a `tools/list` and one round-tripped tool call against it and paste the result. That demonstrates the
  MCP-tool → socket → addon hop the ruling above deliberately keeps out of the gate scenario. It is a
  **supplement**; its absence does not fail the gate, and claiming it when Docker was unavailable does.
- [ ] **Step 4 — Run the negative cases**: a missing file, a path outside the configured roots, a save without
  `confirm_overwrite`, a compressed `.blend` passed to `open_shot` (which must **succeed** — it is the
  magic-byte false-rejection case, and the only negative-case entry whose expected result is success), and a
  command queued behind an `open_shot`. Each must produce a clean error and a live connection afterwards — **no
  hang, no dropped socket** (spec §4.5, and CLAUDE.md's "a valid command that Blender rejects should not
  unnecessarily drop a healthy socket connection"). For the save-without-confirmation case, assert the target
  file's bytes are **unchanged on disk afterwards**, not merely that an error came back — `check_existing` does
  not block a programmatic overwrite, so only the byte comparison proves the handler's own pre-check ran. For
  every error response, assert **no absolute path appears in the message**, including in text that originated
  from Blender's own exceptions.
- [ ] **Step 4b — Assert the session epoch did not move on any failed case.** A failed `open_shot` leaves the
  database untouched (Task 3's ruling), so the epoch must be identical before and after, and a client that was
  connected across the failure must **not** be told to re-handshake.
- [ ] **Step 5 — Record the result in TASK_STATE** with the transcript, the timings, and the revision.

### How this task's evidence maps to §07's rubric — and the one dimension it does not carry alone

State this explicitly rather than letting the scenario imply coverage it does not have.

| Rubric dimension | What in this task is the evidence |
|---|---|
| Data durability and rollback safety (30) | Steps 2, 7 and 9 (link + override survive save → reopen, filepath byte-identical) and Step 4's unconfirmed-overwrite case, proven by the target file's bytes being unchanged |
| **Concurrency and liveness (25)** | Steps 3, 4 and 10 for "exactly one response, no hang" — **but see below for the multi-process clause** |
| Filesystem and trust boundary (20) | Step 4's out-of-roots, missing-file and compressed-`.blend` cases, and the no-absolute-path assertion on every error response |
| Evidence quality (15) | Steps 2, 3 and 5 — transcripts, reproduced by the reviewer |
| Code quality and contract fidelity (10) | Step 7's end-of-phase gate |

**The concurrency dimension's multi-process clause is *not* evidenced by this task, and the rubric text is
corrected to say so.** §07 scores "the file-swap ordering contract holds **across multiple client processes**"
inside that 25-point dimension, and multi-process operation against one Blender is documented, real behaviour
(`README.md:85-89`, `:277-294`). **This scenario is single-client throughout.** It cannot demonstrate that
clause, and reading its green result as though it had is the kind of unearned coverage claim §07 exists to
prevent.

**The evidence for that clause is Task 3's existing headless two-socket test** — the one required by Task 3
Step 2 ("every queued command receives exactly one response … on both sockets in a two-client test") and Task 3
criterion 2 ("a test sends N commands across two sockets spanning a swap and asserts N responses arrive within
a bounded time"), built on `tests/server/test_threading.py`'s real-TCP harness. Cite it by name in TASK_STATE's
rubric table for this dimension. **Do not build a second multi-client scenario into this task**: that is added
scope beyond what the gate sentence asks for, the existing test already covers the claim at the level the claim
is about (queue ordering, not Blender behaviour), and §0.3 finding 6 establishes that the protocol half of
reentrancy is exactly the part that needs no display.

- [ ] **Step 6 — Update the spec**: mark the Phase 2 row's gate met (§8), with the date and revision. **Also
  correct §7's now-stale claim** that `docker-blender` "has no `bundles.py` and registers all 285 tools, so it
  must merge or cherry-pick `bundles.py` before it is used for anything beyond local testing — currently
  scheduled in no phase" (spec `:526-527`). Task 1's ruling supersedes every clause of that: the rig was ported
  **forward onto `main`**, so it is now built from a tree that has `bundles.py`, `BLENDER_MCP_TOOLSETS` is set
  explicitly in compose (Task 1 Step 2), and the work is no longer unscheduled — it landed in Phase 2 Task 1.
  Rewrite the row with a dated correction block rather than deleting it, in the style §4.6 already uses.
- [ ] **Step 7 — End-of-phase gate; commit.**

### Acceptance criteria

1. The full scenario passes, with the transcript pasted into TASK_STATE.
2. **The reopened `.blend` reports the link intact** — that is the gate's load-bearing clause and it must be an
   assertion, not an observation. "Intact" means the library filepath is byte-identical to the pre-save value
   (which requires `relative_remap=False` to have been passed explicitly) **and** the override's objects are
   still editable and not system overrides.
3. Every negative case returns a clean error on a connection that still works afterwards, with **no absolute
   path in any error message** and **no epoch movement on a failed swap**; the unconfirmed-overwrite case is
   proven by the target file's bytes being unchanged.
4. Request count equals response count across the whole scenario. **This is single-client evidence**; the
   multi-process clause of §07's concurrency dimension is carried by Task 3's two-socket test, cited by name in
   TASK_STATE's rubric table rather than implied by this scenario's green result.
5. The default `pytest` run does not require Blender, **and the scenario's pytest entry point is collected and
   reports as `skipped`** when no live Blender is available — demonstrated by pasted output, not asserted.
   "Not collected" is a failing result for this criterion, not an equivalent one (Step 1).
5a. **Every command the scenario issues is an addon command present in `_build_command_handlers()`** — no MCP
   tool name appears in the scenario or its checklist. `get_addon_status` in particular is server-side only and
   is replaced by `get_addon_info` + `get_session_info` (the ruling above). All three `.blend` fixtures — canon
   library, shot, and save target — are created by the scenario itself.
6. End-of-phase gate green: `pytest` (647 + Phase 2's additions) passing; `measure_catalog.py all` reporting
   the new expected count; per-file ruff/format/basedpyright clean; repo-wide counts at or below the handoff's
   baselines.

---

## How the tasks add up to the gate

| Gate clause | Delivered by | Proven by |
|---|---|---|
| "Open a shot" | Task 6 (`open_shot`), Task 5 (path safety), Task 2 (protocol), Task 9 (tool) | Task 10 step 3 |
| "link canon" | Task 7 (`link_canon_library`), Task 4 (`libraries` rollback) | Task 10 step 5 |
| "create an override" | Task 7 (`create_override` via Route C, `override_hierarchy_create(scene, view_layer, do_fully_editable=True)`) | Task 10 step 6, including the objects-still-editable assertion |
| "save" | Task 6 (`save_shot`, `compress=False`, explicit `relative_remap=False`, `os.path.exists` overwrite pre-check) | Task 10 step 7 |
| "reopen with the link intact" | Tasks 6 + 7 together; the **explicitly passed** `relative_remap=False` policy (`save_as_mainfile`'s own default is `True`) | Task 10 steps 8–9 |
| "no hang" | Task 3 (barrier, one response per command), Task 2 (decided protocol), Task 1 (live rig) | Task 10 **scenario items 3 and 10** (single-client) plus **Step 4** (every negative case leaves a live connection). An earlier revision cited "steps 3, 4, 10", mixing the scenario-item numbering with the checklist-step numbering — items 3 and 10 are scenario items; 4 is a checklist step. The **multi-process** half of §07's concurrency dimension is carried by **Task 3's two-socket test**, not by this scenario, which is single-client throughout. |

Supporting, not on the gate's critical path but required by spec §8's Phase 2 row or by this project's
standards: Task 1 (`docker-blender` takes `bundles.py`; persistent-timer re-verification), Task 4
(`transaction.py` gains `libraries`), Task 8 (the security question Phase 2 creates).

## Implementer tier summary

| Task | Tier | Reason for Opus, where applicable |
|---|---|---|
| 1 — live-Blender rig on `main` | **Opus** *(re-tiered from Sonnet)* | Every other task's acceptance evidence runs through this task's artefact (`scripts/blender_rig.py` is named in Tasks 2, 3, 6, 7 and 10, and a live transcript is a hard exit gate for eight of ten), so a rig that exercises the wrong path silently invalidates all of it. Getting the port's *scope* wrong is not hypothetical: review measured a dead `output_roots.py` module with a red ported test, a protocol-version collision with Task 3, and 36 new `ruff` errors breaching the repo baseline in the phase's first commit. It also owns the "what does the rig actually reach" boundary Task 10's scenario is written against. The superseded Sonnet rationale — "what remains is a precise file port plus a new launcher script" — is the same difficulty framing this plan disavows and that mis-tiered Tasks 6 and 7. |
| 2 — reentrancy decision | **Opus** | Resolves a design question the spec left explicitly undecided; the experiment's design and the discipline of applying a pre-stated rule are the deliverable. |
| 3 — drain-loop barrier, epoch, handlers | **Opus** | Concurrency correctness in the single callback every command traverses; failure mode is a hang or a mis-sequenced mutation, and `impact` returns `UNKNOWN` so the graph offers no safety net. |
| 4 — transaction invalidation, `libraries` | **Opus** | Data destruction: the current code will delete a freshly opened file's entire contents on a reachable path. Unrecoverable if wrong. |
| 5 — filesystem trust boundary | **Opus** | The only enforcing control against what the spec calls the dominant risk; path canonicalization is a classic silent-failure domain. |
| 6 — file-lifecycle handlers | **Opus** *(re-tiered from Sonnet)* | Enforces three of §07's ten automatic-critical items — the `os.path.exists` overwrite pre-check (`check_existing` is inert and looks like a guard), the no-path-leak sanitizer on Blender's own exception text, and the explicit `use_scripts=False` on every `open_mainfile` call (criterion 5). Separately, it carries the `use_scripts_auto_execute` preference check that moved here from Task 5 (criterion 6) — *not* an automatic-critical item, but the phase's one undecided refuse-vs-warn judgement. Each check is small; each fails silently. Tiering here is by consequence, as it is for Task 4. |
| 7 — linking handlers | **Opus** *(re-tiered from Sonnet)* | Owns the only Phase 2 command that outright deletes user data (`unlink_libraries` → `bpy.data.libraries.remove` plus optional `orphans_purge`), whose "never touch an unnamed library" scoping guard fails silently. Carries an automatic-critical path-leak criterion (its own criterion 8) on `Library.reload()`'s *most common* failure path — a missing or moved library file. `reload_library` / `relocate_library` replace the contents of every linked datablock in place and can rename the `Library` datablock out from under a caller's handle. The superseded Sonnet rationale ("what remains is careful handler work against a decided design") was written on the difficulty criterion this plan disavows; tiering here is by consequence, as it is for Tasks 4 and 6. |
| 8 — socket authentication design | **Opus** | The second undecided question; a threat model whose weaknesses are inherited by Phases 4 and 5. |
| 9 — MCP tools, bundles, ceiling | Sonnet | — |
| 10 — phase gate scenario | Sonnet | — |

**Eight of ten.** That ratio is higher than a normal phase and is deliberate: Phase 2 concentrates every
data-durability, concurrency and security decision in the project's history into eight tasks, and closes the two
questions the spec could not. Only Tasks 9 and 10 remain Sonnet-class.

**Three of the eight — Tasks 1, 6 and 7 — were re-tiered on review rather than tiered up front, and for the
same reason.** None carries an open design question. All three had a Sonnet rationale of the form "the hard
decisions are made; what remains is careful work against a decided design" — an argument about *difficulty*,
where this plan tiers on *consequence*. Task 6 is where three automatic-critical controls are actually
enforced; Task 7 owns the phase's only outright deletion of user data, an automatic-critical path-leak
criterion on its most ordinary failure path, and two commands that replace linked datablock contents in place;
Task 1 is the foundation every other task's evidence rests on, and its scope was measurably wrong in three
separate ways under the settled-looking rationale. In every case the individual pieces are a handful of lines,
which is precisely why they are easy to write plausibly and wrong, and in every case the failure is silent.

**The pattern is worth naming, because it produced three mis-tierings in one plan:** a task whose design is
settled reads as low-risk, and "nothing left to decide" is the sentence that smuggles the difficulty criterion
back in. It appeared three times, in almost the same words each time — "what remains is careful handler work
against a decided design" (Task 7), "the hard decisions are made" (Task 6), "what remains is a precise file
port" (Task 1). **Treat that sentence shape as a tiering smell.** Check the consequence of the defect, not the
openness of the question. Downgrading any of the three again is allowed; doing so silently is not.

## Deliberately not in this plan

Stated here rather than silently omitted, in the style of Phase 1's catalog plan.

**The artist-facing plugin's agent loop.** Spec §8's Phase 2 row opens with "Plugin↔MCP loop on a worker
thread", but §8's **Phase 3** row owns "Artist-facing plugin (agent loop, UI, credentials)", and the Phase 2
gate does not mention the plugin at all. Building an agent loop requires model selection, prompting and
credential handling — none of which Phase 2 can decide, and all of which §1 places partly upstream of this
project. What Phase 2 *does* owe the plugin is the threading contract it will depend on: §4.7's rules (the agent
loop must not run on Blender's main thread; results drain via one persistent timer registered once on the main
thread; never register a timer from a worker — `server_core.py:383-385`). Tasks 2 and 3 establish and prove that
contract under a real event loop, which is the part that would otherwise block Phase 3. **Ruling: the contract
is Phase 2; the loop is Phase 3.** If a reviewer disagrees, the cost of being wrong is that Phase 3 discovers a
threading constraint late — which is exactly what Tasks 2 and 3 are designed to prevent.

**Socket authentication implementation.** Task 8 designs it; spec §8 puts implementation in Phase 4. Shipping a
half-designed auth mechanism under Phase 2's schedule is worse than shipping the containment control (Task 5)
plus a reviewed design.

**`load_post` opt-in digest re-verification.** Spec §4.5's last table row. It depends on `content_digest`,
which is Phase 3 (§8), and on §10 Q5's unanswered affordability question (~360 MB of hashing on the main thread
for a 20-entity shot). Task 3 installs the `load_post` handler infrastructure, so Phase 3 has a hook to attach
to; the digest work itself stays in Phase 3.

**The §7.1 success bar.** Fully specified in the spec, designed to run retrospectively, and still unexecuted at
the end of Phase 1. It measures the *context surface*, not the file lifecycle, and Phase 2 adds tools rather
than cutting them. Running it belongs with Phase 1b, alongside the cuts it exists to judge. Task 9's measured
deltas are what a later bar run will need.

**Variant scoping, MCP Resources, the typed gateway.** Phase 1b, unchanged. See the Phase 1 catalog plan's
closing section for why each is deferred.

## Self-review

**Spec coverage.** §4.5's tool table: Tasks 6, 7, 9 (the `create_override` row's **API mapping is confirmed
correct** against 5.2.1 — `override_hierarchy_create` exists with the signature the spec assumed; what Task 7
adds is the `do_fully_editable=True` argument the spec did not specify and the measurement that shows why it is
needed. The row's parenthetical claim that `object.override_create()` "returns `None`" is **false** and is
corrected in §0.3 finding 4 — on a linked object it returns the new override ID; the hierarchy API is chosen
because it overrides a whole hierarchy in one call, not because the per-object call fails).
§4.5's three open hazards: reentrancy → Tasks 2 and 3; ordering → Task 3; rollback → Task 4. §4.5's security
paragraph and §10 Q8: Tasks 5 and 8. §4.7's threading contract: Tasks 2 and 3 (loop deferred, with reason
above). §8's Phase 2 row: Task 1 covers "`docker-blender` takes `bundles.py`" and the persistent-timer
re-verification; Task 4 covers "`transaction.py` gains `libraries`". §8's Phase 2 gate: Task 10. Decisions #7
and #9: Global Constraints. §5's "`.blend` required": Task 6.

**Placeholder scan.** No "TBD", no "add error handling", no "similar to Task N". **Exactly two questions are
deliberately left to the implementer**, each with a stated recommendation and a mandatory TASK_STATE record;
everything else is decided in the plan:

1. **Task 6 Step 4b — refuse vs. warn on `preferences.filepaths.use_scripts_auto_execute`.** Task 6's own tier
   rationale calls this "the one item in this task that is genuinely undecided". **Recommendation: refuse**,
   unless the reviewer overrules it with a recorded reason — `use_scripts=False` is already passed explicitly,
   so refusing costs an artist with the preference on nothing they need from `open_shot`, whereas a warning on
   an unauthenticated socket is a control nobody reads.
2. **Task 5 — `BLENDERMCP_OUTPUT_ROOTS` reuse vs. a separate `BLENDERMCP_FILE_ROOTS`.** Stated in that task as
   "decide and justify". **Recommendation: two variables, with `BLENDERMCP_FILE_ROOTS` defaulting to
   `BLENDERMCP_OUTPUT_ROOTS` when unset**, because a canon library mount is genuinely read-only and folding it
   into "where I may write" would be wrong.

Both are *judgements* the plan cannot settle by measurement — the first is a security posture call, the second
a deployment-ergonomics call — which is why each carries a recommendation rather than a ruling. Neither may be
resolved silently: the decision and its reason go in TASK_STATE's "Decisions taken" table with its cost if
wrong. An earlier revision of this section claimed "no open question is deferred to an implementer", which was
false on both counts.

Everything else *is* decided here, with the measurement that decided it: the override route (Task 7, Route C),
the reload route (Task 7, `Library.reload()`), the epoch rules (Task 3 — no bump on a failed swap, and none on
a successful save), the `relative_remap` and overwrite mechanisms (Task 6), and the static half of the
`use_scripts` policy (Task 5 — never exposed in any schema). What the implementer owes on those is
*reproduction*, not resolution — Task 6 Step 1 (introspect the operators' signatures **and defaults**),
Task 7 Steps 1, 2 and 2b (reproduce the smoke test, all three override routes, and the `reload` route), and
Task 9 Step 1 (measure before pinning). Those steps exist because guessing produces a wrong constant or a wrong
API, and because §0.3 finding 4 now records **three** ways that goes wrong: a spec that guessed an API; a plan
revision that used `dir()` on a `bpy.types.*` class and declared a real API missing; and — the one that lasted
longest — a claim (`override_create()` returns `None`) that was *inherited* across two revisions, including a
correction pass, because nobody re-ran the half of the sentence that was not under review. Reproduce; escalate
on divergence; do not re-litigate a ruling without evidence against it; and **do not carry a neighbouring claim
forward unverified just because the claim next to it was the one being fixed.**

**Type and name consistency.** `_SESSION_SWAP_COMMANDS` is introduced in Task 3 and consumed in Tasks 4 and 6.
**`_DATABLOCK_REPLACING_COMMANDS` is introduced in Task 4 (item 2a) and consumed in Task 7** — it is a
*separate* set from `_SESSION_SWAP_COMMANDS`: both bypass `mutation_transaction`, only the first trips Task 3's
barrier. Do not merge them.
`_READ_ONLY_COMMANDS` gains **`get_session_info` in Task 3 and `list_libraries` in Task 7**, and nothing else —
the other eight Phase 2 commands mutate, and the three that replace or free datablocks use the dedicated
constant rather than borrowing this one.
`session.py`'s epoch and error fields are introduced in Task 3, consumed by Tasks 6, 7 and 10.
`handlers/file_lifecycle.py` is **created in Task 3** (`get_session_info`) and **extended in Task 6**
(`open_shot`, `save_shot`, `reset_session`) — one module, two tasks, never re-created.
`file_paths.resolve_blend_path` / `enforce_roots` / `sanitize_blender_error` are introduced in Task 5 and
consumed by Tasks 6 **and 7** (Task 7's `lib.reload()` leaks absolute paths exactly as Task 6's operators do).
The `use_scripts` policy is **stated** in Task 5 and **enforced at runtime** in Task 6 Step 4b, because
`file_paths.py` is `bpy`-free and cannot read `bpy.context.preferences`.
`scripts/blender_rig.py` is introduced in Task 1 and used by Tasks 2, 3, 6, 7 and 10 — **and it speaks the
addon socket only; no mode of it stands up an MCP server**, which is why Task 10's scenario names addon
commands and never `get_addon_status` (Task 1's third ruling, Task 10's first). `output_roots.py` is ported in
Task 1 **with its five wiring sites and the 30 → 31 protocol bump** and promoted in Task 5; **Task 1 is the only
task that bumps the protocol**, and Task 3 asserts rather than re-bumps. The poll surface is `get_session_info`
in every branch and every document; `get_session_status` is not a name this plan uses.

**The File Structure table is part of this sweep, and it silently diverged once.** Two of its rows contradicted
Task 9's own rulings until this pass: it said to create a new `files` bundle and add it to both modes (Task 9
ruling 1 puts the file tools in **`CORE_MODULES`**, because `test_shot_and_asset_modes_share_only_the_core_
surface` forbids a shared non-core bundle), and it said to add "annotation entries for the new tools" (Task 9
Step 4: **four of the ten must deliberately get none**, and "register all ten" is not implementable). Both are
now corrected. **Re-read the File Structure table against each task's rulings as part of every review cycle** —
it is a summary of decisions made elsewhere, which is exactly the kind of artefact that goes stale without
anything failing, and it is the first thing an implementer reads. Specifically check: the `bundles.py` row
against Task 9 ruling 1, the `_documentation.py` row against Task 9 Steps 4 and 4b (three hints, not two), the
`server_core.py` row against Tasks 1, 3, 4 and 7's constants, and every "New in Task N" attribution against the
task that actually creates the file.

**Known risk this plan does not remove.** Task 2's outcome can invalidate Task 3's and Task 6's shape. That is
intentional — the alternative is picking a protocol blind — but it means **Tasks 3 and 6 must not be started
before Task 2's decision is recorded**. The async branch does **not** add an eleventh tool: its poll surface is
`get_session_info`, which Task 3 item 4 defines in either branch and which is already one of Task 9's ten. What
that branch does change is `open_shot`'s and `get_session_info`'s *schemas* (a job id and a state field), so
**re-measure** Task 9's byte delta rather than reusing a figure measured under the synchronous branch — and note
in TASK_STATE that ruling 3's budget has zero margin to absorb the growth. Say so in TASK_STATE if that branch
is taken.

**Second known risk.** Task 9's ceiling ruling deliberately raises a number Phase 1 spent a whole task pinning.
That is a real cost, and the mitigation is the second ceiling on the default surface plus the 15,000 B budget —
without both, "raise the ceiling" becomes a habit rather than a decision. **Note that the budget is fully
committed at ten tools** (10 × ~1.5 KB = 15,000 B, ruling 3), so it constrains only if schemas are actually kept
at the target; it provides no slack to absorb a single verbose one.
