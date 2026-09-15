# Episode Consistency Architecture

**Status:** Design — revision 3. Supersedes `2026-09-10-episode-consistency-architecture-design.md`.
**Date:** 2026-09-11
**Repo:** `jpease/blender-mcp` (fork of `josuemontano/blender-mcp`, in turn of `ahujasid/blender-mcp`)

This is a rewrite, not a revision. The previous document accumulated eight rounds of
patches, appended corrections beside the text they corrected, and carried retracted
decisions next to their retractions. Its audit trail is in git; its surviving findings are
in Appendix A. Nothing here should require reading it.

Every code citation and measurement below was verified by execution against `main` with the
imported source asserted. Three prior adversarial reviews are dispositioned in Appendix A.

---

## 1. The problem, and what this project owns

A studio produces a long-running animated series, originally in Maya and moving toward USD.
They want generative AI to help produce episodes. Characters and key locations must not
drift in appearance between shots or between episodes; generative authoring makes drift
cheap and invisible, so continuity must become something the system checks and can prove.

The hosted pipeline has five hops:

```
Media Center  →  AIGW  →  headless agent runtime  │  Blender MCP  →  headless Blender  →  file output  │  →  Media Center
     └──────────── other teams ──────────────────┘  └──────────────── this project ──────────────────┘
```

**This project owns the middle.** Runtime selection, prompting and model choice sit upstream;
any generative step sits downstream. What this project owns is the contract at both seams:
what a client may ask for, and what the file output guarantees.

**The file output is `.blend` plus rendered files, both required. USD is possible, not
committed.** A second delivery surface — an artist-facing Blender add-on on a local
workstation — drives the same server through a long-lived interactive session.

Two properties follow from the pipeline and constrain everything below:

- **The client is undecided and may not be Claude.** Vendor agnosticism is a requirement, so
  no Claude-specific mechanism may be load-bearing.
- **Hosted execution is job-per-request**, possibly TTL-cached and warm-pooled. There is no
  long-lived in-memory scene; the `.blend` on disk is the state carrier.

## 2. The four risks that decide whether this works

Ordered by how much they threaten the goal, which is the reverse of how much attention they
have historically received.

### 2.1 The deliverable is not what we fingerprint — **unsolved, highest**

Every mechanism in §4.2 reads *inputs* (linked libraries) or *live session state*. **Nothing
reads the artifacts that ship.** Those provably disagree. Verified:

```
write to linked object:  location.x = 5.0
  evaluated depsgraph →  (5.0, 0.0, 0.0)    ← the render sees it
  save, reopen        →  (0.0, 0.0, 0.0)    ← the .blend does not
```

So a job can pass `assert_consistency`, pass `publish_shot`, emit a manifest whose digests
all verify, and **ship a `.blend` that does not reproduce its own frames** — while the only
repair path, reopening the `.blend`, shows nothing wrong.

That is a class, not an instance. It also covers render determinism (samples, denoiser,
seed, compute device — and a warm pool is heterogeneous hardware by definition) and any
`format` field changed after the fingerprint is taken.

**§4.2 addresses this with a deliverable fingerprint and a declared extraction point per
facet. It is the newest part of this design and the least proven.**

### 2.2 Pinning rests on invariants levied on a system we do not own — **open**

`content_digest` works (§4.1, spike-verified). Its *premise* — that a published version is
byte-immutable — is four invariants imposed on the studio's asset manager, which §1 places
outside this project. A manager that recompresses, repacks or normalises on ingest voids
all four silently, and nothing in the design would notice.

**Mitigation adopted:** this system computes and records the digest **at publish, on the
bytes it produced**, and re-checks on first read back. That converts an assumption about
someone else's system into a detection this system performs. It does not make the
assumption true.

### 2.3 Canon identifier stability has no owner — **open, external**

Pinning assumes `(canon_id, version)` is stable across Maya→Blender conversion runs. If the
conversion project regenerates identifiers per run, pinning breaks at the root. That project
has no named owner for this question and no date. **This is the cheapest thing on this list
to resolve and the most damaging to discover late.**

### 2.4 The context surface — **measured, least load-bearing, and first to build**

285 tools; the full `tools/list` is ~328K tokens and shot mode is ~50K (§4.6). This is real and it
is the one problem fully inside this project's control, which is why it has historically
absorbed attention out of proportion to its weight. It is §4.6, near the end, deliberately.

**It is nonetheless Phase 1 (§8), and that is not a contradiction.** Risks are ordered by
damage; phases are ordered by independence. This work waits on nobody, adds no domain tools,
and survives the consistency design being wrong — which makes it the right thing to build
first and still the least important thing on this list.

## 3. Constraints (verified by execution)

| Constraint | Evidence | Consequence |
|---|---|---|
| **The addon refuses to run in background mode** — `if bpy.app.background: return` | `addon/server_core.py:152-155` | **Every hosted job needs a display server** (`xvfb-run`, as `docker-blender`'s image already does). This is a per-job cost, an image-size cost, and a pooling constraint. |
| Commands execute on Blender's main thread via a timer returning `0.05` | `addon/server_core.py:184,321` | ~25 ms floor per call; favours coarse tools. |
| Listener and client handlers are daemon threads | `addon/server_core.py:177,250` | A plugin calling the server from the main thread deadlocks (§4.7). |
| `bpy.app.timers.register()` must not be called off the main thread | `addon/server_core.py:383-385` | Queue + one persistent timer registered once on the main thread. |
| One in-flight request per connection; a UUID is checked as a desync backstop, not a correlation mechanism | `connection.py:191-196,202,222-230` | Multi-artist needs N Blenders and a router. |
| `bpy.ops.render.render()` is synchronous inside the drain timer; client timeout 180 s | `handlers/rendering.py:612-615`; `connection.py:160,222` | Long renders desync the stream. Renders must become polled jobs. |
| Each command pushes its own undo step | `addon/transaction.py:137,157` | One undo step per intent call, or a 12-command assembly costs twelve. |
| Rollback does not track `libraries` | `addon/transaction.py:17-36` | A failed link leaks a library datablock. |
| Advertised `capabilities` already vary with scene flags saved in the `.blend` | `addon/server_core.py:774,784,793`, `:1156`; gated at `connection.py:187` | Opening a different file already shifts the handshake. `open_shot` makes this per-job (§4.5). |
| Multiple server processes against one Blender is documented | `README.md:243+` | A process-scoped mode guard is not an invariant (§4.3). |
| `tools/list` is re-sent on **every** request and occupies context on every turn | MCP is stateless | Caching makes it cheap in dollars, not tokens. The budget is permanent occupancy. |

## 4. Architecture

### 4.1 Canon registry and `content_digest`

A **canon entity** is a versioned identity for something that must look the same everywhere.

```python
@dataclass(frozen=True)
class CanonRef:
    kind: Literal["character", "location", "prop", "rig", "material"]
    canon_id: str          # "char_hero"
    version: str           # "v012" — immutable; never "latest" once recorded
    content_digest: str    # SHA-256, recorded by THIS system at publish
```

Resolution goes through a port so the studio's asset manager is an adapter:
`list`, `resolve`, `latest`, `publish_asset`, `publish_shot`, and — new, per §2.2 —
`verify_on_read`, which re-checks the digest the first time a version is read back.
`publish_shot` is separate because a shot is not a canon entity.

**Pinning policy: shots pin concrete versions and never float.** An approved shot that
changes when an upstream asset republishes is a defect that reaches air. Upgrading is
explicit (`update_canon_version`) and re-fingerprints.

**`content_digest` is SHA-256 over the published `.blend` bytes.** Spike-verified
(Blender 5.2.1):

| Operation | Effect on the published file |
|---|---|
| Open it | no change |
| Link it into a shot | no change |
| Copy it to a node-local cache | byte-identical |
| **Re-save it** | **non-deterministic** — two fresh-process re-saves differ from each other (~0.03–0.06% of bytes, heap addresses at a regular stride) |

Cost ~7 ms for an 18 MB asset, I/O-bound, and **no Blender process is needed to verify** —
so any worker in any language can check a pin.

Four invariants follow. All four are constraints on the asset manager (§2.2), which is why
`verify_on_read` exists:

1. **Publish uncompressed.** `compress=True` turns a 0.03% raw difference into 97.8% of
   compressed bytes and diverges file size.
2. **Never re-save a published version.** The only way to reach the non-determinism.
3. **A Blender upgrade is a re-publish and re-pin, not an in-place save.** **Cost this
   before committing:** it invalidates every `CanonRef` in every published shot and every
   episode baseline, for a multi-year series, with no rollback — the old bytes are not
   reproducible. Owner and schedule: unassigned (§10 Q4).
4. **The digest covers the `.blend` only.** Unpacked textures, caches and nested libraries
   are path strings and sit outside it. Canon must pack textures or publish a digested
   dependency manifest.

**Why a digest at all:** Blender's own change detection reports *only* failed name
resolution. A moved vertex, a changed modifier parameter, a changed shader value and a
changed transform are all silent, and `bpy.types.Library` exposes no checksum, mtime or
size.

**Verification cost is not free at scale.** A 20-entity shot re-verified per job is ~360 MB
of hashing inside a `load_post` handler on the main thread. Digest-on-open is therefore
**opt-in per job**, not unconditional (§10 Q5).

### 4.2 Consistency: input, session, and deliverable

Not one hash — independently comparable **facet rows**, each declaring **where it is read
from**. That declaration is new in this revision and is what §2.1 requires: the same facet
extracted live and post-reload can differ, so a fingerprint that does not say which is not a
fingerprint of anything definite.

```python
@dataclass(frozen=True)
class FingerprintRow:
    facet: str      # see table
    subject: str    # library-qualified: (library_id, datablock_name)
    source: Literal["input", "session", "deliverable"]   # ← where it was read
    digest: str     # fast path
    values: dict    # canonicalized values — the verdict, compared with tolerance
```

Digest equality passes immediately; inequality falls through to a tolerance comparison on
`values` before anything is reported as drift, because float32 round-trips make exact
equality fragile.

| Facet | Source | Captures | Grade |
|---|---|---|---|
| `asset` | **input** | `(canon_id, version, content_digest)` per linked entity | enforced |
| `material` | **session** | node topology keyed by socket *identifier*, identity-input values, **plus content hashes of referenced images** | enforced |
| `transform` | **session, evaluated** | world-space scale and bounds of each entity's override root **and its pose bones** | enforced |
| `color` | **session** | view transform, look, display device, **exposure, gamma**, world, **OCIO config identity**, engine | enforced |
| `format` | **session** | resolution, pixel aspect, fps, frame range | enforced |
| `render` | **deliverable** | engine build, device, samples, denoiser + version, seed | enforced |
| `artifact` | **deliverable** | content hashes of the `.blend` and every rendered file, and the AOV/pass inventory | enforced |
| `light` | session | preset provenance **and** measured light parameters | advisory |

**The `render` and `artifact` facets are what §2.1 demands.** `artifact` is the only row
computed *after* the deliverable exists, and it is what makes the manifest describe the
files rather than the intention.

**The round-trip check.** Because a session-sourced facet can differ from the same facet
after reload, `publish_shot` must extract session facets, save, reopen, re-extract, and
**fail on mismatch**. That is the only mechanism that catches the §2.1 class, and it costs
one extra open per publish.

**Canonicalization is specified, not deferred.** Key node sockets by identifier; exclude
`location`, `width`, `label`, and `.001` name suffixes; quantize floats to float32 before
hashing; sort by a stable key; record the Blender version that produced the reading, because
node trees are versioned in memory on load and the same pinned library read under two
releases can differ.

**Baselines and waivers.** An episode baseline is the enforced rows it must match, stored in
the resolver alongside episodes, shots and approvals. A per-shot
`ConsistencyException(shot_id, facet, subject, reason, approved_by, expires)` exists because
legitimate deviations exist — a burned costume, a wet-hair variant — and a check whose only
remedy is advancing the episode-global baseline is a check people stop running.

**Purity.** A `bpy`-dependent extractor emits plain dicts; a pure server-side hasher,
differ and policy layer consumes them. The pure half holds most tests; the extractor is
tested in the container (§7).

**Not yet covered, and known:** `hide_render`, view-layer `material_override`, constraints
and drivers on an override root, `unit_settings.scale_length`, shape keys, particle systems,
compositor node trees, geometry-node inputs on an override. Each is reachable from shot mode
today. They are listed in §10 Q6 rather than silently absent.

### 4.3 Modes and enforcement

Two modes with near-disjoint surfaces: `shot` assembles, animates, lights and renders;
`asset` authors or revises canon through a checkout-and-publish flow.

**Enforcement lives in the addon, keyed on the open `.blend`**, because tool registration is
per server process and `README.md:243+` documents running several against one Blender. A
process-scoped guard is bypassable by configuration; the addon is the only layer every path
traverses. It must reject **at dispatch** and leave the handshake's advertised
`capabilities` set unchanged, since the client gates on it (`connection.py:187`).

**What the guard actually refuses — corrected, and narrower than previously claimed.**
Verified:

```
linked object:        location.x = 5.0  → accepted, reaches the evaluated depsgraph,
                                          and is GONE after save/reload
override object:      library is None, is_editable True   ← a `library is not None`
                                                             predicate does NOT fire
override's mesh data: data.library is not None             ← still linked, still writable
```

Three consequences:

1. **Writes to linked data are non-durable but render-affecting.** They are not "drift that
   persists"; they are worse — they change the frames and vanish from the file (§2.1).
2. **The predicate must reach sub-datablocks.** `library is not None` on the *object* is
   empty in the intended workflow, because §4.3 permits overrides and an override's object
   is local. The guard must test the **datablock being written**, including `obj.data`.
3. **Drift through an override cannot be refused** without refusing legitimate shot work.

So: **the guard refuses writes to still-linked datablocks; the fingerprint catches drift
through overrides; the round-trip check (§4.2) catches the non-durable class.** Three
mechanisms, three non-overlapping jobs. Earlier revisions claimed prevention alone, then
detection alone; it is both, in defined places.

**Unresolved:** Blender has no file-level property, only per-`Scene`, so a two-scene `.blend`
has two modes. The default for a new, foreign, or hand-edited file is unspecified —
permissive is bypassable with File → New, restrictive bricks ordinary sessions. See §10 Q7.
Note also that several handlers already check editability independently
(`geometry_nodes/_shared.py:39`, `texture/materials.py:174`,
`rigid_body/inspection_and_setup.py:535`) while `handlers/scene.py:865-898`
(`set_object_transform`) does not — the guard partly duplicates existing checks and partly
covers a real hole, and the survey has not been done.

### 4.4 Presets

One provider protocol (`list` / `describe` / `apply`), three providers: `blend`
(artist-authored, Phase 3), `recipe` (parameterized code, Phase 4), `captured` (deferred).
Applied presets stamp provenance on what they create, which the `light` facet reads
alongside measured parameters.

**Constraint:** the addon registers its full handler set regardless of which tools a process
advertises, so a `recipe` preset can construct lights even when light-construction *tools*
are out of shot mode (§4.6). **`capture_preset` is the exception** — it cannot snapshot a
setup the session cannot build, so the `captured` provider is unavailable from shot mode.

### 4.5 File lifecycle and linking

None of this exists today: zero hits for library overrides, `bpy.app.handlers`,
`save_mainfile` and `open_mainfile`, and the one `bpy.data.libraries.load` call is
`link=False` in `handlers/polyhaven.py:362`. **This server cannot currently open or save a
`.blend`**, which §5 makes a required deliverable. It is the critical path.

**Enabling fact:** the server survives a file load. `bpy.types.blendermcp_server` is module
state, not file data, and `server_core.py:184` already registers the drain timer
`persistent=True` — verified to survive where an otherwise identical `persistent=False`
timer does not. *(The `--background` caveat this carried is **closed**: re-verified 2026-09-14
on Blender 5.2.1 and again 2026-09-15 on 5.2.2, with the server actually running under the live rig,
both as a scripted main-thread call (Phase 2 Task 1) and from inside the drain callback
servicing a queued socket command (Task 2). `server._drain_timer` reads registered on both
sides of the swap and the next queued command is answered.)*

| Command | `bpy` | Notes |
|---|---|---|
| `open_shot` | `wm.open_mainfile` | Invalidates every prior datablock reference; resets scene, so mode and the capability set must be re-read; clears undo; **requires a re-handshake**. **Must refuse when `bpy.data.is_dirty` unless the caller passes an explicit discard flag** — there is no "Save changes?" path from a timer callback, so the swap otherwise destroys unsaved work in silence (measured) |
| `save_shot` | `wm.save_mainfile` / `save_as_mainfile` | Canon publishes pass `compress=False`; needs an explicit `relative_remap` policy |
| `reset_session` | `wm.read_factory_settings(use_empty=True)` | The pool load-or-reset step |
| `link_canon_library` | `bpy.data.libraries.load(link=True)` | Path allowlist; `//`-relative path policy required |
| `create_override` | `collection.override_hierarchy_create(scene, view_layer, reference=instance)` | **`object.override_create()` returns `None`** — verified |
| `list_libraries` | walk `bpy.data.libraries` + digest | No checksum/mtime/size available (§4.1) |
| `reload_library` / `relocate_library` | `wm.lib_reload` / `wm.lib_relocate` | For `update_canon_version` |
| `unlink_libraries` | purge | Required before reuse; previously missing |
| `load_post` hook | `bpy.app.handlers` + `@persistent` | Opt-in digest re-verification (§4.1) |

**Open hazards, all Phase 2 work:**

- **Reentrancy — decided 2026-09-15 by experiment; no longer open.** The drain callback
  **survives** `wm.open_mainfile` called from inside its own frame and can still answer the
  client afterwards. Measured on Blender 5.2.2 under the live rig: the frame resumes after
  the operator returns (18/18 successful swaps over six runs), `drain_command_queue` returns its normal
  `0.05` and never raises (3/3 instrumented ticks), the response reaches the client process
  on the same connection, and the next queued command executes against the new file. So
  `open_shot` is **synchronous validate-then-swap, answering after the swap** — the only
  option whose answer is truthful by construction, and the one that keeps one request → one
  response, which `connection.py:195-231` already depends on. The uncaught-failure path was
  measured too: a load that raises inside the handler reaches the client as a normal
  `{"status": "error"}` frame carrying Blender's own message, and the server keeps serving.
  **That message embeds the full absolute path of the file that failed** — five failure modes
  measured, yielding three distinct texts (`Cannot read file`, `File format is not supported`,
  and `Missing DNA block` for a truncated-but-valid-header file, which leaks the path twice) — so
  `open_shot` must route it through Task 5's error sanitizer before it leaves the process;
  passing Blender's text through verbatim is the automatic-critical path-leak item.
  **What the load does free is `bpy.context.window`, not the Python frame** — it reads `None`
  for the remainder of the swapping tick, on every swap measured, while `bpy.context.scene`
  resolves and is the **new** file's scene (measured against fixtures with deliberately
  different scene names). Three things follow, all measured rather than reasoned: the window
  is still present in `bpy.data.window_managers[0].windows`, so `temp_override` has something
  to override with; a context-light operator (`object.select_all`) **still succeeds on the
  inherited context** despite `bpy.context.window` being `None`; and `bpy.context.window` is
  non-`None` again by the next tick, so the loss does not outlive the swap. **Because
  `bpy.context.scene` follows the new file, the scene-gated capability set does too** —
  `_build_command_handlers()` reads `scene.blendermcp_use_polyhaven` / `_use_sketchfab` /
  `_use_nd` and is rebuilt on every command, which is what makes the re-handshake in the
  command table above a correctness requirement and not a convenience.
- **Ordering — characterised 2026-09-15; the hazard is real and now demonstrated.** The drain
  loop keeps draining *after* the swap within the same tick: with four commands queued into
  one tick and the swap second, all four were answered from that one tick, and the
  `list_scene_objects` queued **before** the swap but drained **after** it silently reported
  the **new** file's contents (3/3 rounds). Commands are therefore not ordered against the
  database they were issued against, and a client's queued work can be answered from a file
  it never asked about. Measured on both sides of the drain budget: a 494 KB fixture loads in
  ~3 ms and its siblings are answered from the **same** tick (3/3 rounds), while a 1.05 GB
  fixture loads in ~4.6 s, ends the tick on the budget, and its siblings are answered from the
  **following** ticks — still against the new database. Commands that *arrive during* a slow
  load are queued by the client threads and drained after it, so a barrier cannot classify the
  queue by inspecting it after the swap: the epoch has to be stamped at enqueue time, on the
  client thread, and compared at dequeue. This is what the Phase 2 barrier and session epoch
  are for.
- **Rollback.** `transaction.py` backups reference datablocks that a load frees. `open_shot`
  must invalidate the transaction state, not merely clear undo.

> **Correction, 2026-09-15.** The reentrancy and ordering bullets above previously read, in full:
>
> > **Reentrancy.** `wm.open_mainfile` from inside the drain-timer callback frees that
> > callback's context. Untestable headlessly (timers do not fire in `--background`), so it
> > needs the Xvfb rig. **The obvious mitigation — defer the load and answer first — is
> > wrong**: it commits a success response before the load can fail on a missing file, bad
> > permissions, a corrupt `.blend`, or an allowlist rejection, and the envelope has no way to
> > report that afterward. A correct design needs either a two-phase `open_shot`
> > (validate-then-load, answering after validation but before the swap) or an async job with
> > polling.
> >
> > **Ordering.** The drain loop processes up to 8 commands per tick; a deferred load has no
> > defined ordering against queued commands, and multiple client processes may interleave
> > against a file mid-swap.
>
> Both offered options existed only because this section assumed the callback could not answer
> *after* the swap. **That assumption was never tested, and it is false.** It is also imprecise
> about what is freed: the callback's *window* context goes, the callback itself does not. The
> frame holds `command`, `client` (a plain Python socket) and `response`, and `self` lives on
> `bpy.types.blendermcp_server` — module state, not file data. Nothing in it is an RNA reference,
> so nothing in it is invalidated by the load.
>
> The experiment and its decision rule are recorded in
> `docs/superpowers/plans/PHASE2_TASK_STATE.md` under "Task 2 — the reentrancy strategy"; the rule
> was committed (`969df10`) **before** the spike was written, so the branch could not be chosen
> after seeing the result. The rejected alternative is the **async job with polling**: it is not
> wrong, it is unnecessary, and its disqualifying observation is that the callback demonstrably
> answers after the swap, which is the premise it exists to work around. Two-phase
> answer-before-swap stays rejected for the reason this section already gave.
>
> The ordering bullet's "a deferred load" no longer describes anything: under the decided design
> the load is not deferred. What replaces it is a measurement rather than a prediction.

**Security.** `open_shot` and `save_shot` add arbitrary-path read and overwrite to a socket
with **no authentication** (§10 Q8) — currently the dominant risk for a pooled deployment,
and not addressed by the link/append allowlist, which does not cover them. They also add a
**disclosure channel**: every `wm.open_mainfile` failure shape measured embeds the full
absolute path in its `RuntimeError` text, and the drain loop returns that text to the client
verbatim unless something strips it.

### 4.6 The context surface

**Almost none of this is additive, which is why it is Phase 1.** Bundle splits regroup
entries in a `bundles.py` that already exists; variant scoping edits schemas already
written; Resources are a protocol primitive that adds no tools; the measurement harness is
tooling. The only additions are the gateway's three machinery tools, and they are net
strongly negative — 3 added to stop advertising 264.

**Two figures, not to be conflated.** `core-shared` after the splits is projected at **21 tools /
17.6K tokens** — that is the *default* surface, and it is what the ~19K gate refers to. A working
`shot` surface adds camera, lighting and rendering on top and lands near **36K tokens** (rung 3
below). Neither figure assumes a gateway; both are reachable by bundle splitting alone. The
gateway's contribution is to make the tools *outside* the selected mode still reachable, not to
shrink the mode itself.

**Policy: minimize, do not fill.** There is no target size; unneeded bytes buy nothing. The
context-window arithmetic yields a **ceiling, not a goal** — 50K tokens, a 200K floor at 25%
of permanent occupancy. Landing under it is necessary, not sufficient.

Where the bytes are (re-measured 2026-09-11 at `a1ec347` by `scripts/measure_catalog.py`,
`exclude_none=True`, what FastMCP actually serialises): 285 tools, **1,180,620 B / ~328K
tokens** for `all`. **Input schemas are 77.5% of the payload and `$defs` are 38% of shot
mode** — nested model definitions, not parameter counts. That is why scoping works and
consolidation does not: a discriminated union of 30 variants costs 37,263 B against 39,729 B
split, a **6.2%** saving, because `oneOf` still serializes every variant.

**The ladder, with every tool named.** Byte and token columns re-measured 2026-09-11; rung 0 at
`3b27c1d` (before Phase 1 changed anything), rungs 1-3 at `a1ec347`. Rung 1 has since landed as
Phase 1 Task 3; rungs 2 and 3 are Task 5.

| Rung | Removed | Tools | Bytes | Tokens |
|---|---|---|---|---|
| 0 | shot mode today | 67 | 179,203 | **49.8K** |
| 1 | `create_geometry_object`, `remove_scene_objects`, `reset_scene` | 64 | 150,569 | 41.8K |
| 2 | `camera.rigs` — `create_camera_path_rig`, `create_crane_camera_rig`, `create_dolly_camera_rig`, `create_orbit_camera_rig`, `duplicate_camera_rig`, `match_camera_transform` | 58 | 142,218 | 39.5K |
| 3 | `lighting.construction` — `create_light`, `configure_light`, `aim_light`, `configure_light_linking`, `create_studio_lighting` (**five**, presets replace them) | 53 | 129,961 | **36.1K** |

**After three rungs, shot mode is 36.1K against a 50K ceiling — 13.9K under, before a single
intent tool.** Shot mode is in fact already marginally under the ceiling at rung 0. That is a
materially better position than this section claimed before it was re-measured, and it changes
what the rest of the section is for: the levers below are no longer needed to reach the ceiling,
so they must be justified by the minimize policy (Decision 5) on their own merits, or deferred.

> **Correction, 2026-09-11.** The byte and token columns above previously read 277,083 / 77.0K,
> 248,449 / 69.0K, 230,220 / 64.0K and 203,094 / 56.4K, and the paragraph concluded that shot mode
> was "over by 6.4K". Those figures did not reproduce: rung 0 was overstated by 97,880 B, and the
> rung 2 and rung 3 deltas by roughly 2.2x. Every *tool count* in the ladder was and is exact, as
> are the rung 0->1 delta of 28,634 B and the per-tool sizes listed below, which is why the error
> survived review. The measured group deltas are `camera.rigs` **8,351 B** (was 18,229) and
> `lighting.construction` **12,257 B** (was 27,126).
>
> **The byte column does not correspond to any state of `main` that could be found.** The obvious
> explanation — that it predates the schema work of 2026-09-09 (`04d5f36` bundle-leak fix,
> `73c9c4b` and `0b052ec` union refactors) — was tested and is false. Measured at `f9f51d5`, the
> commit before all three, rung 0 is 249,194 B, still not 277,083; and the `camera.rigs` and
> `lighting.construction` groups measure **8,351 B and 12,257 B there too — byte-identical to
> today**. Those two group sizes have never changed, so 18,229 and 27,126 were not measurements of
> this repository at any point. What the schema work did shrink was `core`, from 189,625 B to
> 119,634 B, which is the whole of rung 0's drop. Treat the old byte column as unsourced.
>
> Phase 1's implementation plan inherited these figures; see
> `docs/superpowers/plans/PHASE1_TASK_STATE.md` for the measurement method and provenance rules.

Further candidates, by size:
`configure_render_settings` 19,577 B, `configure_lighting_quality` 10,882 B,
`configure_camera_render_gate` 8,848 B, `configure_camera` 8,210 B, `create_camera` 6,971 B,
`manage_nla_tracks` 6,893 B, `add_camera_constraint` 6,800 B, `configure_procedural_sky`
6,325 B. Engine scoping (dropping Eevee variants from `configure_render_settings` and
`configure_lighting_quality`) saves a further 5,203 B but **removes capability** — shot mode
could then configure Cycles only, while the `color` facet enforces engine as a variable.

**The 1 KB intent-tool ceiling is not achievable at this repo's documentation standard, and
buying it costs what §7 measures.** Only 3 of 285 tools are under 1,000 B, all trivial
utilities. `nd_boolean` — three string parameters — costs 1,596 B, of which **754 B is the
description**. Descriptions are the binding term, and they are the only surface an agent has
for canon and consistency semantics. **Treat ~1.5 KB as the realistic intent-tool target and
accept that the ceiling does not close the gap by itself.**

Required bundle splits: `CORE_MODULES` → `core-shared` (24) / `core-authoring` (16);
`texture-lighting` → separate; `scene` → authoring and destructive tools out; `camera` →
`rigs` out; `lighting` → construction out.

**Levers, ranked.** Portable first, because the client is undecided:

| Lever | Verdict |
|---|---|
| Mode scoping + bundle splits | **Adopt** — portable, removes 57% of payload for shot work |
| Variant scoping (fewer typed variants, not merged ones) | **Adopt** — portable; consolidation itself saves only 6.2% |
| MCP Resources for catalog data | **Adopt narrowly** — portable; zero `@mcp.resource` exist today |
| Typed gateway (`list`/`describe`/`invoke_capability`) | **Adopt as fallback** — typed only, never Python source. Defers cost for unused surface; saves nothing for used surface |
| Skills | **Thin pointer only** — Claude-only, so never load-bearing; guidance lives in Resources and docstrings |
| Tool search / `defer_loading` | **Not adopted** — Anthropic-specific; vendor agnosticism forbids depending on it |
| Programmatic Tool Calling | **Rejected** — verified still incompatible with MCP tools |

### 4.7 The local plugin

The add-on is the MCP *client*; the existing addon remains the socket server; the loop closes
inside one Blender process. Keep it — collapsing it builds a second code path that transfers
nothing to the hosted deployment, and ~25 ms is noise against inference.

**The agent loop must not run on Blender's main thread** or it deadlocks: the main thread
blocks awaiting the response, the server sends a socket command, the addon queues it, and
the drain timer cannot run. Results return via a `queue.Queue` drained by **one persistent
timer registered once on the main thread** — never by registering timers from the worker,
which `server_core.py:383-385` forbids.

**The plugin is disposable; the server is the asset.** The hosted path brings a different
runtime with its own loop and prompts, so anything enforced only in the plugin is lost — which
is why §4.3's guard lives in the addon.

## 5. Delivery contract

A job's output is a **delivery**: artifacts plus a manifest, produced by `publish_shot`.

| Artifact | Status |
|---|---|
| `.blend` | **required** — carries `shot_recipe`, fingerprint rows, pinned canon versions |
| Rendered files | **required** |
| USD | possible, uncommitted — see below |

**The manifest must be sufficient alone; a consumer should never open the `.blend` to learn
what it is.** It carries: artifact paths and content hashes; all fingerprint rows including
`render` and `artifact` (§4.2); pinned `(canon_id, version, content_digest)` per linked
entity; applied preset provenance; the producing Blender version **and engine build and
device**; the **extractor/facet-schema version**, without which a facet-definition change
silently reinterprets every stored baseline; external texture hashes; the AOV/pass
inventory; job id and timestamp; any `ConsistencyException` in force with its expiry; and
the round-trip check's result.

**USD — corrected.** An earlier revision claimed the export "silently drops" provenance and
that canon linkage "is not preserved by the format." Both were wrong.
`export_custom_properties` defaults to **True** and is already used in this repo
(`handlers/cloth/exporting.py:191`); `bpy.types.USDHook` covers layer metadata; and
**`UsdModelAPI.assetInfo` ships `identifier`/`name`/`version` with a pluggable Ar resolver** —
USD already standardizes what §4.1 invents. The defensible statement is narrower:
*Blender's exporter does not author the composition arcs USD provides.* That is an
implementation gap, not a format property.

**The real lossiness is materials.** `generate_preview_surface` is default-True and supports
only simple node trees (Diffuse/Principled BSDF, Image Texture, UVMap, Separate RGB);
`generate_materialx_network` is default-False. For a design whose `material` facet is
enforced and is the primary identity carrier, that is the citable risk. Also lossy: library
overrides flatten, geometry subsets are not time-sampled, hair exports parent strands only,
perspective cameras only.

**Strategic note:** if the studio is moving to USD, `assetInfo` + Ar is a candidate
*replacement* for §4.1's mechanism, not a hazard downstream of it. That question is open
(§10 Q9) and should be answered before §4.1 is built twice.

## 6. Tool surface

Fifteen intent tools in the artist's vocabulary — `list_canon`, `link_canon`,
`update_canon_version`, `assemble_shot`, `place_character`, `list_presets`,
`describe_preset`, `apply_preset`, `capture_preset`, `set_shot_camera`, `inspect_shot`,
`compute_consistency_fingerprint`, `diff_consistency`, `assert_consistency`, `publish_shot`
— plus `audit_episode`, which runs headlessly rather than against an interactive session.

All return the existing envelope (`ok`, `data`, `warnings`, `changed_objects`,
`changed_resources`). No new response contract.

**`apply_preset` and `set_shot_camera` take a `params` object whose schema is only available
after a `describe_preset` round trip.** Advertising it as an unconstrained object is type
erasure — the mechanism §4.6 ranks last. This is a real tension and it is unresolved: either
those two tools carry a fatter typed schema, or the intent layer accepts erasure in exactly
the place the rest of the design avoids it.

## 7. Testing

| Area | Blender? |
|---|---|
| Canonical hashing, tolerance comparison, differ, float32 round-trip stability | No |
| Baseline, exception and expiry policy | No |
| Pinning: concrete versions recorded, `latest` never persisted, digest re-check | No |
| `AssetResolver` contract tests across both adapters | No |
| Mode guard: out-of-mode refused at dispatch; **`obj.data.library` case covered**; two processes with different toolsets cannot bypass | No |
| Bundle splits: expected sets; payload under ceiling; no transitive leakage | No |
| Path allowlist incl. the Poly Haven `.blend` path | No |
| `content_digest`: stable across open/link/copy; publish rejects `compress=True`; re-save refused | Partly |
| **Round-trip check**: session facets survive save/reopen; a linked-data write is caught | **Container** |
| Extractor correctness against real scenes | **Container** |
| File lifecycle: open/save/reset/link/override/unlink; reentrancy under Xvfb | **Container** |

The container is `docker-blender`'s image (**which requires Xvfb — see §3**). That branch
has no `bundles.py` and registers all 285 tools, so **it must merge or cherry-pick
`bundles.py` before it is used for anything beyond local testing** — currently scheduled in
no phase.

### 7.1 The task-success bar

§4.6 says "as small as still works well." That is unbounded without a definition of *works
well*, and over-cutting fails silently at the payload level while failing expensively at the
task level.

**The bar:** ~a dozen representative shot-assembly tasks with machine-checkable outcomes —
link a canon character and place it; apply a lighting preset; set a camera to a named
framing; assemble a two-character shot; reproduce a fingerprint and diff it; catch a
deliberately introduced drift.

**Sampling.** Twelve tasks x five repeats = 60 trials per arm, paired: the same tasks, the
same fixtures, and the same seeds where seedable, run against both the baseline revision and
the cut. Pairing matters more than n here - it removes task difficulty as a variance source,
which is the dominant one.

**Primary metric.** Task success rate over the machine-checkable outcomes above.

**The rule, stated as non-inferiority rather than equality.** A cut **fails** if the lower
bound of a 90% bootstrap confidence interval on `(cut - baseline)` falls below **-10
percentage points**. The question a cut must answer is not "did nothing change" but "did
nothing meaningfully get worse"; an equality test on 60 binary trials rejects on noise alone.
The -10pp margin is chosen against the task set, not picked from the air: one task class of
twelve failing outright is 8.3pp, so the margin says *no more than a single task class may
regress, and it must be within noise of that*.

**Secondary metric, replacing first-try argument validity.** That metric is dropped: it is
not observable from inside this project and instrumenting someone else's client is not a
dependency worth taking. It is replaced by **dispatch count to completion**, counted
server-side - every tool call crosses the dispatch helper, so the server can count them
without client cooperation. A cut whose success rate holds while its dispatch count climbs
has not saved anything; it has moved cost from advertised bytes into round-trips, which is
precisely the failure mode a gateway risks. **Rule:** median dispatches per task must not
rise by more than 25%.

**Vendor agnosticism, honestly scoped.** A bar is model-specific by construction and no
amount of design changes that. So it is run on **at least two models** and a cut must be
non-inferior on both; the models used are recorded with every result. This does not make the
bar portable - it makes the claim falsifiable on more than one runtime, which is the most
this method can offer.

**Ordering - resolved, and no longer a blocker.** The original objection was that the bar
must exist before §4.6's cuts or it validates nothing. The cuts have since been made, but the
objection dissolves: every pre-cut state is still addressable in git. `upstream/main`
registers all 285 tools with no bundle selection at all, and each Phase 1 cut landed as its
own commit. The bar can therefore be written now and run **retrospectively** against any
revision in that range, which satisfies the requirement by version control rather than by
calendar order. Any bar result must name the two revisions it compared.

**What remains genuinely open.** Whether ~a dozen tasks is enough breadth to represent shot
assembly at all. The statistics above are sound for the tasks chosen; they say nothing about
whether those tasks are the right ones, and no amount of repeats fixes a badly chosen suite.

## 8. Phasing

**Implementation plan:** `docs/superpowers/plans/2026-09-11-phase-1-catalog.md` covers the
measurement harness, the bundle splits and the mode presets — Phase 1a, the part that is fully
specified. The
gateway, Resources, variant scoping and the success bar are deferred there with reasons; the
bar in particular cannot be planned until §7.1's threshold question is closed.

**Sequenced by independence, not by weight.** Phase 1 is the context work even though §2.4
ranks it the least load-bearing risk, because it is the only phase that depends on none of
the open questions, adds no domain tools, and is not invalidated if the consistency design
in §4.2 turns out wrong. The risks are ordered by damage in §2; the phases are ordered by
what can be built without waiting on anyone.

**Phase 1 adds no domain tools.** Everything in it is subtractive or editorial except three
gateway machinery tools, which exist to stop advertising 264 others. Adding tools beyond the
285 that exist is deferred to Phase 3, and then only where no existing tool covers the job.

**Phase 1 is split, and Phase 2 runs before its remainder.** Tasks 1-6 of the catalog plan -
the measurement harness, the bundle splits, and the mode presets - land first and are done
when `shot` and `asset` are selectable. Everything else Phase 1 once contained (variant
scoping, MCP Resources, the typed gateway, the Xvfb rig) moves **after Phase 2**, for two
reasons. First, the gateway's value proposition is "defer cost for unused surface", and
§7.1's bar cannot say which surface goes unused until there are mode-shaped baselines to
measure against - those come out of Task 6. Second, and more bluntly: this server cannot
open or save a `.blend` file. Tuning which tools an artist is offered, while the artist
cannot open their own shot, optimises the wrong surface. Phase 2 is the smaller gate and
the one a user would notice first.

**The ~19K token gate belongs to the default surface, not to `shot`.** Measured against the
current tree, bundle splitting alone brings shot-shaped work to roughly 36K tokens (§4.6's
rung 3). Reaching ~19K for a working shot surface needs variant scoping and the gateway on
top, which is why the gate is restated per-phase below rather than claimed once.

| Phase | Content | Gate |
|---|---|---|
| **1a — Catalog** | Measurement harness (payload by tool, family and bundle, as a test). Bundle splits: `CORE_MODULES` → `core-shared`/`core-authoring`, `texture`/`lighting` separated, `camera-rigs`, `lighting-construction`, `scene` (authoring + destructive out, **done**). **Mode presets `shot` and `asset`** resolving to curated bundle sets, so a client selects one word. §7.1 bar written and run retrospectively against `upstream/main`. | `shot` and `asset` are selectable; `all` still advertises 285 tools with **no capability lost and no domain tool added**; the bar scores no worse after the cuts than before |
| **1b — Deferral** (after Phase 2) | Variant scoping on the heaviest schemas. MCP Resources for reference content. Typed gateway (3 machinery tools), which needs its own spec section first. Xvfb rig. | Advertised payload for a working `shot` surface falls to ~19K tokens, with the §7.1 bar non-inferior on both metrics |
| **2 — Primitives** | Plugin↔MCP loop on a worker thread. `open_mainfile` reentrancy under a real event loop; re-verify the persistent-timer result with the server actually running. Then the §4.5 file-lifecycle family; `transaction.py` gains `libraries`; `docker-blender` takes `bundles.py`. **The first genuinely additive work, and inescapable — this server cannot currently open or save a `.blend`.** | Open a shot, link canon, create an override, save, reopen with the link intact; no hang |
| **3 — Consistency** | Canon registry + `LocalMirrorResolver`; `content_digest`; extractor + hasher for `asset`, `material`, `color`, `format`, `artifact`; the round-trip check; `blend` presets — seeded from `create_studio_lighting`, which is already a preset in all but name. Artist-facing plugin (agent loop, UI, credentials). One canon character and location, hand-built. | Two shots; a deliberate drift caught and a legitimate change not; a `.blend` that reproduces its own frames |
| **4 — Enforcement** | Addon mode guard including `obj.data`. Baselines, exceptions, `assert_consistency`, `publish_shot`, manifest. `load_post` opt-in digest re-check. `shot_recipe` recording. Socket authentication. `StudioAssetResolver`, `recipe` presets, `audit_episode`. | A shot cannot publish inconsistent from any client configuration |
| **5 — Hosted** | Session model, router, pooling, concurrency, multi-provider evaluation. | Media Center drives a shot end to end |

## 9. Decisions

| # | Decision |
|---|---|
| 1 | Consistency is facet rows with a declared source, digest as fast path and tolerance on values as verdict. |
| 2 | Shots pin concrete canon versions and never float. |
| 3 | Enforcement is three mechanisms: addon guard for linked-datablock writes, fingerprint for override drift, round-trip check for the non-durable class. |
| 4 | `content_digest` is SHA-256 over published bytes, recorded by this system at publish and re-verified on read. |
| 5 | Context policy is minimize, not fill; 50K is a ceiling, not a goal. Intent tools target ~1.5 KB. |
| 6 | Portable levers rank above Claude-specific ones; tool search is not adopted, Skills are a thin pointer. |
| 7 | No arbitrary code execution, in any form, including as a gateway capability. |
| 8 | Generative identity conditioning is out of scope; this project owns one `shot_recipe` field binding a shot to the identity its passes were built for. |
| 9 | The plugin↔MCP loop is kept rather than collapsed. |
| 10 | USD is designed for, not built; provenance is default-on, and the material network is the real loss. |
| 11 | Phases are sequenced by independence, not by risk weight. No domain tool is added before Phase 3, and then only where no existing tool covers the job — `create_studio_lighting` already covers lighting presets, `set_object_transform` covers placement, and the camera surface covers framing. |

## 10. Open questions

Ordered by damage, not by how easy they are to answer.

1. **Does the deliverable actually match the fingerprint?** §2.1. The round-trip check is
   designed but unbuilt and unproven; the `render` and `artifact` facets are new.
2. **Will the studio asset manager honour the four §4.1 invariants** — and does it expose
   immutable, content-stable version references? `verify_on_read` detects violations; it
   cannot prevent them. **No owner.**
3. **Does the Maya→Blender conversion preserve `canon_id` across runs?** §2.3. **No owner,
   cheapest to answer, most damaging late.**
4. **What does a Blender upgrade cost?** Invariant 3 re-publishes and re-pins every asset and
   every baseline, with no rollback. Unscoped.
5. **Is digest-on-open affordable per job?** ~360 MB of hashing for a 20-entity shot on the
   main thread. Opt-in by default; needs measurement.
6. **Which remaining drift vectors get facets?** `hide_render`, view-layer overrides,
   constraints and drivers on an override root, `unit_settings.scale_length`, shape keys,
   particles, compositor trees, geometry-node inputs.
7. **How is `mode` carried and defaulted?** Blender has no file-level property; a two-scene
   file has two modes; new/foreign/hand-edited files have no defined default.
8. **Socket authentication.** None exists. Dominant risk once pooled and multi-tenant, and
   `open_shot`/`save_shot` widen it to arbitrary-path read and overwrite.
9. **Does USD `assetInfo` + Ar replace §4.1** rather than sit downstream of it? Answer before
   building §4.1 twice.
10. **What are the cost envelopes?** Tokens per shot, container-minutes for `audit_episode`
    over 200 shots, sidecar storage and retention. Never estimated.
11. **Registry concurrency and facet-schema evolution.** Two jobs publishing one `shot_id`;
    two artists advancing one baseline; adding a facet invalidating stored baselines.
12. **Who fingerprints the canon library itself?** `audit_episode` audits shots. When
    `char_hero` republishes as v013, nothing checks that v013 is internally consistent with
    v012 in the ways that matter — and the batch upgrade then re-pins every shot to it.
13. **What is the repair workflow?** `assert_consistency` fails on forty shots at once.
    There is no triage, no bulk waiver, no "accept the new baseline for these subjects only",
    no notification and no named owner. A check whose failure mode has no workflow is a check
    that gets disabled.

## Appendix A — Findings ledger

Three adversarial reviews (2026-09-11). Full text in git history of the superseded document.
**Resolved** means the design changed; **open** means it is in §10.

| Source | Finding | Status |
|---|---|---|
| R1 | "Structurally impossible" enforcement was false; process-scoped guard bypassable | **Resolved** — §4.3, addon-side, three mechanisms |
| R1 | Context arithmetic counted tools, not bytes | **Resolved** — §4.6 measured |
| R1 | Missing: exposure/gamma, texture content, editorial format, measured light params | **Resolved** — §4.2 facets |
| R1 | `rig` facet tautological under linking | **Resolved** — replaced by `transform`, now incl. pose bones |
| R1 | No open-time re-verification, episode audit, batch upgrade, or waivers | **Resolved** — §4.1, §4.2 |
| R2 | Consolidation saves 6.2%, not 97%; precedent was type erasure | **Resolved** — §4.6 |
| R2 | Phases depended on four subsystems with zero code | **Resolved** — §4.5, Phase 2 |
| R2 | `content_digest` never defined | **Resolved** — §4.1 |
| R2 | Poly Haven loads network `.blend` (`handlers/polyhaven.py:362`) | **Open** — live vulnerability, §10 Q8 |
| R2 | Socket has no authentication | **Open** — §10 Q8 |
| R2 | USD caveat backwards | **Resolved** — §5 |
| R3 | Ladder scored against a rejected ceiling; rung 3 misdescribed (5 tools, not 4) | **Resolved** — §4.6 re-derived, tools named |
| R3 | "Writes to linked data persist" was false — they are non-durable but render-affecting | **Resolved** — §2.1, §4.3 |
| R3 | Guard predicate empty on overrides; must reach `obj.data.library` | **Resolved** — §4.3 |
| R3 | Addon refuses background mode; every hosted job needs a display server | **Resolved** — §3, §7, Phase 1 (Xvfb rig) |
| R3 | Deliverable never fingerprinted | **Partly** — `render`/`artifact` facets and round-trip check designed, unproven (§10 Q1) |
| R3 | Task-success bar has no threshold; key metric unobservable here | **Open** — §7.1 states the limits |
| R3 | 1 KB intent ceiling unachievable; only 3 of 285 tools are under it | **Resolved** — §4.6, target raised to ~1.5 KB |
| R1–R3 | Absent: cost envelopes, registry concurrency, facet-schema evolution | **Open** — §10 Q10, Q11 |
| R1–R3 | Absent: canon-library self-fingerprinting, repair workflow | **Open** — §10 Q12, Q13 |
